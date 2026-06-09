#!/usr/bin/env python3
"""
Fetch US running race data from multiple sources and save to data/races.json.
Sources: RunSignUp API, UltraSignup (scrape), RunReg API
"""

import requests
import json
import os
import time
import hashlib
import re
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt

SOLON_LAT = 41.3895
SOLON_LON = -81.4412

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, '..', 'data', 'races.json')

DISTANCE_CATEGORIES = [
    '1 Mile', '5K', '8K', '10K', '12K', '15K', '20K',
    'Half Marathon', '25K', '30K', 'Marathon',
    '50K', '50M', '100K', '100M+', 'Other Ultra',
]

TRAIL_KEYWORDS = [
    'trail', 'mountain', 'wilderness', 'summit', 'ridge', 'creek', 'mud run',
    'mud obstacle', 'tough mudder', 'spartan', 'off road', 'off-road',
    'backcountry', 'singletrack', 'single track', 'vertical', 'gnarly',
    'rugged', 'gorge', 'canyon', 'forest', 'nature', 'cross country',
    'cross-country', 'xterra', 'dirt', 'gravel',
]


def haversine(lat1, lon1, lat2, lon2):
    if not all([lat1, lon1, lat2, lon2]):
        return None
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return round(2 * R * asin(sqrt(a)), 1)


def normalize_distances(raw):
    """Map a raw distance string to one or more standard category labels."""
    if not raw:
        return []
    text = str(raw).lower().strip()

    # Order matters: check longer strings first
    patterns = [
        (r'100\s*mi|100\s*mile', '100M+'),
        (r'100\s*km|100\s*k\b', '100K'),
        (r'50\s*mi|50\s*mile', '50M'),
        (r'50\s*km|50\s*k\b', '50K'),
        (r'marathon|26\.2', 'Marathon'),
        (r'half.?marathon|13\.1|21\.0\s*km|21\.1\s*km', 'Half Marathon'),
        (r'30\s*km|30\s*k\b', '30K'),
        (r'25\s*km|25\s*k\b', '25K'),
        (r'20\s*km|20\s*k\b', '20K'),
        (r'15\s*km|15\s*k\b', '15K'),
        (r'12\s*km|12\s*k\b', '12K'),
        (r'10\s*km|10\s*k\b|6\.2\s*mi', '10K'),
        (r'8\s*km|8\s*k\b', '8K'),
        (r'5\s*km|5\s*k\b|3\.1\s*mi', '5K'),
        (r'\b1\s*mi\b|1\s*mile\b', '1 Mile'),
        (r'ultra', 'Other Ultra'),
    ]

    matched = []
    for pattern, label in patterns:
        if re.search(pattern, text):
            matched.append(label)

    return matched if matched else []


def infer_race_type(name, description=''):
    combined = (name + ' ' + (description or '')).lower()
    for kw in TRAIL_KEYWORDS:
        if kw in combined:
            return 'trail'
    return 'road'


def make_dedup_key(name, date, state):
    key = f"{re.sub(r'[^a-z0-9]', '', name.lower())}_{date}_{state.lower()}"
    return hashlib.md5(key.encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------
# RunSignUp
# ---------------------------------------------------------------------------

def fetch_runsignup():
    print("Fetching RunSignUp...")
    races = []
    page = 1
    start_date = datetime.now().strftime('%Y-%m-%d')
    end_date = (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')

    session = requests.Session()
    session.headers['User-Agent'] = 'RaceFinder/1.0 race aggregator (tpcolburn@gmail.com)'

    while True:
        try:
            resp = session.get(
                'https://runsignup.com/rest/races.json',
                params={
                    'format': 'json',
                    'results_per_page': 500,
                    'page': page,
                    'start_date': start_date,
                    'end_date': end_date,
                    'events': 'T',
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  RunSignUp page {page} error: {e}")
            break

        batch = data.get('races', [])
        print(f"  page {page}: {len(batch)}")
        if not batch:
            break

        for item in batch:
            race = item.get('race', item)
            address = race.get('address') or {}

            try:
                lat = float(address.get('lat') or 0) or None
                lon = float(address.get('lng') or 0) or None
            except (ValueError, TypeError):
                lat = lon = None

            city = address.get('city', '')
            state = address.get('state', '')

            next_dates = race.get('next_date') or []
            if not isinstance(next_dates, list):
                next_dates = [next_dates]

            # Collect all unique distances across events for this race
            all_distances = set()
            first_date = ''
            for nd in next_dates:
                if isinstance(nd, dict):
                    if not first_date:
                        raw_dt = nd.get('event_date', '')
                        first_date = raw_dt[:10] if raw_dt else ''
                    dist_raw = nd.get('distance', '') or nd.get('name', '')
                    for d in normalize_distances(dist_raw):
                        all_distances.add(d)

            if not first_date:
                continue

            name = race.get('name', '').strip()
            desc = race.get('description', '') or ''

            races.append({
                'id': f"rsu_{race.get('race_id', make_dedup_key(name, first_date, state))}",
                'name': name,
                'source': 'RunSignUp',
                'date': first_date,
                'city': city,
                'state': state,
                'lat': lat,
                'lon': lon,
                'distance_from_solon': haversine(SOLON_LAT, SOLON_LON, lat, lon) if lat and lon else None,
                'url': race.get('url') or f"https://runsignup.com/Race/{race.get('race_id')}",
                'distances': sorted(all_distances, key=lambda d: DISTANCE_CATEGORIES.index(d) if d in DISTANCE_CATEGORIES else 99),
                'race_type': infer_race_type(name, desc),
                'elevation_gain': None,
                'description': desc[:500],
                'price': None,
            })

        page += 1
        if len(batch) < 500:
            break
        time.sleep(0.5)

    print(f"  RunSignUp total: {len(races)}")
    return races


# ---------------------------------------------------------------------------
# UltraSignup
# ---------------------------------------------------------------------------

def fetch_ultrasignup():
    print("Fetching UltraSignup...")
    races = []

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://ultrasignup.com/',
    })

    # UltraSignup path-based endpoint — http only, no query params for search
    page = 1
    while True:
        try:
            url = f'http://ultrasignup.com/service/events.svc/GetFeaturedEventsSearch/p={page}/q='
            resp = session.get(url, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  UltraSignup page {page} error: {e}")
            break

        events = data if isinstance(data, list) else (data.get('events') or [])
        # Filter to US only (endpoint returns global results)
        events = [e for e in events if (e.get('state') or e.get('country', '') or '').upper() not in ('', ) or e.get('country', 'US') == 'US']
        events = [e for e in events if len(e.get('state', '')) == 2]  # US state codes are 2 chars
        print(f"  page {page}: {len(events)} US events")
        if not events:
            break

        for ev in events:
            name = (ev.get('eventName') or ev.get('event_name') or ev.get('name') or '').strip()
            if not name:
                continue

            raw_date = ev.get('eventDate') or ev.get('start_date') or ev.get('date') or ''
            date_str = ''
            for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%Y-%m-%dT%H:%M:%S'):
                try:
                    date_str = datetime.strptime(raw_date[:len(fmt.replace('%Y', '0000').replace('%m', '00').replace('%d', '00').replace('%H', '00').replace('%M', '00').replace('%S', '00'))], fmt).strftime('%Y-%m-%d')
                    break
                except Exception:
                    continue
            if not date_str:
                # try simple ISO prefix
                try:
                    date_str = raw_date[:10]
                    datetime.strptime(date_str, '%Y-%m-%d')
                except Exception:
                    date_str = raw_date[:10]

            city = ev.get('city', '')
            state = ev.get('state', '')

            try:
                lat = float(ev.get('latitude') or ev.get('lat') or 0) or None
                lon = float(ev.get('longitude') or ev.get('lng') or 0) or None
            except (ValueError, TypeError):
                lat = lon = None

            # eventDistances is the field name from the GetFeaturedEventsSearch endpoint
            raw_dists = ev.get('eventDistances') or ev.get('distances') or ev.get('distance') or ''
            dist_set = set()
            if isinstance(raw_dists, list):
                for d in raw_dists:
                    dist_name = d.get('distance_name', '') or d.get('distanceName', '') if isinstance(d, dict) else str(d)
                    for cat in normalize_distances(dist_name):
                        dist_set.add(cat)
            elif raw_dists:
                for cat in normalize_distances(str(raw_dists)):
                    dist_set.add(cat)

            if not dist_set:
                dist_set.add('Other Ultra')

            elev = ev.get('elevation_gain') or ev.get('gain')
            try:
                elev = int(elev) if elev else None
            except (ValueError, TypeError):
                elev = None

            event_id = ev.get('eventId') or ev.get('event_id', '')
            races.append({
                'id': f"us_{event_id or make_dedup_key(name, date_str, state)}",
                'name': name,
                'source': 'UltraSignup',
                'date': date_str,
                'city': city,
                'state': state,
                'lat': lat,
                'lon': lon,
                'distance_from_solon': haversine(SOLON_LAT, SOLON_LON, lat, lon) if lat and lon else None,
                'url': f"https://ultrasignup.com/register.aspx?did={event_id}" if event_id else 'https://ultrasignup.com/register.aspx',
                'distances': sorted(dist_set, key=lambda d: DISTANCE_CATEGORIES.index(d) if d in DISTANCE_CATEGORIES else 99),
                'race_type': 'trail',
                'elevation_gain': elev,
                'description': (ev.get('description') or '')[:500],
                'price': None,
            })

        page += 1
        if len(events) < 100:
            break
        time.sleep(0.3)

    print(f"  UltraSignup total: {len(races)}")
    return races


# ---------------------------------------------------------------------------
# RunReg / Outside API
# ---------------------------------------------------------------------------

def fetch_runreg():
    print("Fetching RunReg...")
    races = []

    session = requests.Session()
    session.headers['User-Agent'] = 'RaceFinder/1.0'

    start_date = datetime.now().strftime('%Y-%m-%d')
    end_date = (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')

    page = 1
    while True:
        try:
            resp = session.get(
                'https://www.runreg.com/api/search',
                params={
                    'num': 100,
                    'page': page,
                    'startdate': start_date,
                    'enddate': end_date,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  RunReg page {page} error: {e}")
            break

        events = data.get('MatchingEvents', [])
        print(f"  page {page}: {len(events)} events")
        if not events:
            break

        for ev in events:
            name = (ev.get('EventName') or '').strip()
            if not name:
                continue

            date_str = (ev.get('EventDate') or '')[:10]
            city = ev.get('EventCity', '')
            state = ev.get('EventState', '')

            # Skip non-US entries
            if state and len(state) != 2:
                continue

            try:
                lat = float(ev.get('Latitude') or 0) or None
                lon = float(ev.get('Longitude') or 0) or None
            except (ValueError, TypeError):
                lat = lon = None

            # Categories contains distances + fees
            categories = ev.get('Categories') or []
            dist_set = set()
            for cat in categories:
                cat_name = cat.get('CategoryName', '') if isinstance(cat, dict) else str(cat)
                for d in normalize_distances(cat_name):
                    dist_set.add(d)

            # Also try EventTypes
            event_types = ev.get('EventTypes') or []
            for et in event_types:
                et_name = et.get('TypeName', '') if isinstance(et, dict) else str(et)
                for d in normalize_distances(et_name):
                    dist_set.add(d)

            url = ev.get('EventUrl') or ev.get('EventWebsite') or f"https://www.runreg.com{ev.get('EventPermalink', '')}"

            races.append({
                'id': f"rr_{ev.get('EventId', make_dedup_key(name, date_str, state))}",
                'name': name,
                'source': 'RunReg',
                'date': date_str,
                'city': city,
                'state': state,
                'lat': lat,
                'lon': lon,
                'distance_from_solon': haversine(SOLON_LAT, SOLON_LON, lat, lon) if lat and lon else None,
                'url': url,
                'distances': sorted(dist_set, key=lambda d: DISTANCE_CATEGORIES.index(d) if d in DISTANCE_CATEGORIES else 99),
                'race_type': infer_race_type(name),
                'elevation_gain': None,
                'description': (ev.get('EventNotes') or '')[:500],
                'price': None,
            })

        page += 1
        if len(events) < 100:
            break
        time.sleep(0.3)

    print(f"  RunReg total: {len(races)}")
    return races


# ---------------------------------------------------------------------------
# Deduplication & main
# ---------------------------------------------------------------------------

def deduplicate(all_races):
    seen = {}
    out = []
    for race in all_races:
        key = make_dedup_key(race['name'], race.get('date', ''), race.get('state', ''))
        if key not in seen:
            seen[key] = True
            out.append(race)
        else:
            # If duplicate from another source has elevation data and primary doesn't, merge
            pass
    return out


def main():
    all_races = []
    all_races.extend(fetch_runsignup())
    all_races.extend(fetch_ultrasignup())
    all_races.extend(fetch_runreg())

    print(f"\nTotal before dedup: {len(all_races)}")
    races = deduplicate(all_races)
    print(f"Total after dedup: {len(races)}")

    races.sort(key=lambda r: r.get('date') or '')

    output = {
        'updated': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'count': len(races),
        'races': races,
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(output, f, separators=(',', ':'))

    print(f"Saved {len(races)} races → {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
