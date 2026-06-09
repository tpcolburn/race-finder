#!/usr/bin/env python3
"""
Fetch US running race data from multiple sources and save to data/races.json.
Sources:
  - RunSignUp  API  (api.runsignup.com/rest/races)
  - UltraSignup     (DID-range scan of register.aspx)
  - RunReg     API  (runreg.com/api/search)
"""

import re
import os
import json
import time
import hashlib
import requests
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

SOLON_LAT = 41.3895
SOLON_LON = -81.4412

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, '..', 'data', 'races.json')

BOT_UA = 'RaceFinder/1.0 (+https://github.com/tpcolburn/race-finder; tpcolburn@gmail.com)'
BROWSER_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

DISTANCE_CATEGORIES = [
    '1 Mile', '5K', '8K', '10K', '12K', '15K', '20K',
    'Half Marathon', '25K', '30K', 'Marathon',
    '50K', '50M', '100K', '100M+', 'Other Ultra',
]

TRAIL_KEYWORDS = [
    'trail', 'mountain', 'wilderness', 'summit', 'ridge', 'creek', 'mud run',
    'tough mudder', 'spartan', 'off road', 'off-road', 'backcountry',
    'singletrack', 'single track', 'vertical', 'gorge', 'canyon', 'forest',
    'cross country', 'cross-country', 'xterra', 'dirt', 'gravel',
]

CANADIAN_PROVINCES = {'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC', 'SK', 'YT'}


# ── Helpers ───────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    if not all([lat1, lon1, lat2, lon2]):
        return None
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return round(2 * R * asin(sqrt(a)), 1)


def normalize_distances(raw):
    if not raw:
        return []
    text = str(raw).lower().strip()
    patterns = [
        (r'100\s*mi|100\s*mile', '100M+'),
        (r'100\s*km|100k\b', '100K'),
        (r'50\s*mi|50\s*mile', '50M'),
        (r'50\s*km|50k\b', '50K'),
        (r'ultra(?!signup)', 'Other Ultra'),
        (r'marathon|26\.2', 'Marathon'),
        (r'half.?marathon|13\.1|21\.0?\s*km', 'Half Marathon'),
        (r'30\s*km|30k\b', '30K'),
        (r'25\s*km|25k\b', '25K'),
        (r'20\s*km|20k\b', '20K'),
        (r'15\s*km|15k\b', '15K'),
        (r'12\s*km|12k\b', '12K'),
        (r'10\s*km|10k\b|6\.2\s*mi', '10K'),
        (r'8\s*km|8k\b', '8K'),
        (r'5\s*km|5k\b|3\.1\s*mi', '5K'),
        (r'\b1\s*mi(?:le)?\b', '1 Mile'),
    ]
    matched = []
    for pat, label in patterns:
        if re.search(pat, text) and label not in matched:
            matched.append(label)
    return matched


def infer_race_type(name, description=''):
    combined = ((name or '') + ' ' + (description or '')).lower()
    for kw in TRAIL_KEYWORDS:
        if kw in combined:
            return 'trail'
    return 'road'


def make_dedup_key(name, date, state):
    key = f"{re.sub(r'[^a-z0-9]', '', (name or '').lower())}_{date or ''}_{(state or '').lower()}"
    return hashlib.md5(key.encode()).hexdigest()[:10]


def sort_distances(dist_set):
    return sorted(dist_set, key=lambda d: DISTANCE_CATEGORIES.index(d) if d in DISTANCE_CATEGORIES else 99)


# ── RunSignUp ─────────────────────────────────────────────────────────────────

def fetch_runsignup():
    print("Fetching RunSignUp...")
    races  = []
    page   = 1
    today  = datetime.now().strftime('%Y-%m-%d')
    end_dt = (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')

    session = requests.Session()
    session.headers['User-Agent'] = BOT_UA

    while True:
        try:
            resp = session.get(
                'https://api.runsignup.com/rest/races',
                params={
                    'format':           'json',
                    'results_per_page': 500,
                    'page':             page,
                    'start_date':       today,
                    'end_date':         end_dt,
                    'events':           'T',
                    'include_event_days': 'T',
                    'only_partner_races': 'F',
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
            race    = item.get('race', item)
            address = race.get('address') or {}

            try:
                lat = float(address.get('lat') or 0) or None
                lon = float(address.get('lng') or 0) or None
            except (ValueError, TypeError):
                lat = lon = None

            city  = address.get('city') or ''
            state = address.get('state') or ''

            # next_date is a string in MM/DD/YYYY format (e.g. "08/09/2026")
            raw_dt = race.get('next_date') or ''
            first_date = ''
            if raw_dt and isinstance(raw_dt, str):
                try:
                    first_date = datetime.strptime(raw_dt.strip(), '%m/%d/%Y').strftime('%Y-%m-%d')
                except ValueError:
                    first_date = raw_dt[:10]

            if not first_date:
                continue

            # Distances come from the events list (included via events=T param)
            all_distances = set()
            for ev in (race.get('events') or []):
                if isinstance(ev, dict):
                    dist_raw = (ev.get('distance') or ev.get('name') or '')
                    for d in normalize_distances(dist_raw):
                        all_distances.add(d)

            name = race.get('name', '').strip()
            desc = race.get('description', '') or ''

            races.append({
                'id':                  f"rsu_{race.get('race_id', make_dedup_key(name, first_date, state))}",
                'name':                name,
                'source':              'RunSignUp',
                'date':                first_date,
                'city':                city,
                'state':               state,
                'lat':                 lat,
                'lon':                 lon,
                'distance_from_solon': haversine(SOLON_LAT, SOLON_LON, lat, lon) if lat and lon else None,
                'url':                 race.get('url') or f"https://runsignup.com/Race/{race.get('race_id')}",
                'distances':           sort_distances(all_distances),
                'race_type':           infer_race_type(name, desc),
                'elevation_gain':      None,
                'description':         desc[:500],
                'price':               None,
            })

        page += 1
        if len(batch) < 500:
            break
        time.sleep(0.5)

    print(f"  RunSignUp total: {len(races)}")
    return races


# ── UltraSignup ───────────────────────────────────────────────────────────────

def _us_max_did(session):
    """Binary-search for approximate current max DID by checking for actual event content."""
    lo, hi = 120_000, 180_000
    while lo < hi - 200:
        mid = (lo + hi) // 2
        try:
            r = session.get(f'https://ultrasignup.com/register.aspx?did={mid}', timeout=10)
            # A real event page contains "ContentPlaceHolder" and an h2 race name
            if r.status_code == 200 and 'ContentPlaceHolder' in r.text and '<h2' in r.text and len(r.text) > 8000:
                lo = mid
            else:
                hi = mid
        except Exception:
            hi = mid
    print(f"  UltraSignup max DID found: {lo}")
    return lo


def _parse_us_page(html, did, today, end_dt):
    """Parse a single UltraSignup register.aspx page; return race dict or None."""
    soup = BeautifulSoup(html, 'lxml')

    # Name: first <h2> on page
    h2 = soup.find('h2')
    if not h2:
        return None
    name = h2.get_text(strip=True)
    if not name or 'ultrasignup' in name.lower():
        return None

    # Date: text node just before the <h2>, or a span/div with a date pattern
    date_str = ''
    date_pat = re.compile(
        r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+'
        r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+'
        r'\d{1,2},?\s+\d{4}',
        re.IGNORECASE,
    )
    # Search in the full text
    for m in date_pat.finditer(soup.get_text()):
        raw = m.group(0).strip()
        for fmt in ('%A, %b %d, %Y', '%a, %b %d, %Y', '%A, %B %d, %Y',
                    '%a %b %d, %Y', '%A %b %d %Y'):
            try:
                date_str = datetime.strptime(raw, fmt).strftime('%Y-%m-%d')
                break
            except ValueError:
                continue
        if date_str:
            break

    if not date_str or date_str < today or date_str > end_dt:
        return None

    # Location: first <h3>
    h3 = soup.find('h3')
    city, state = '', ''
    if h3:
        loc = h3.get_text(strip=True)
        parts = [p.strip() for p in loc.split(',')]
        if len(parts) >= 2:
            city  = parts[0]
            state = parts[1][:2].upper()

    # Drop Canadian provinces and anything that looks non-US
    if state in CANADIAN_PROVINCES:
        return None

    # Lat/lon from any Google Maps link
    lat = lon = None
    for a in soup.find_all('a', href=True):
        m = re.search(r'(?:q|ll)=(-?[\d.]+),(-?[\d.]+)', a['href'])
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            break

    # Distances from button text (e.g. "50K - $100")
    dist_set = set()
    for el in soup.find_all(['a', 'button', 'span'],
                             string=re.compile(r'\d+\s*[km]|mile|marathon|ultra', re.I)):
        for d in normalize_distances(el.get_text(strip=True)):
            dist_set.add(d)
    if not dist_set:
        # Fall back to page title or name
        for d in normalize_distances(name):
            dist_set.add(d)
    if not dist_set:
        dist_set.add('Other Ultra')

    return {
        'id':                  f"us_{did}",
        'name':                name,
        'source':              'UltraSignup',
        'date':                date_str,
        'city':                city,
        'state':               state,
        'lat':                 lat,
        'lon':                 lon,
        'distance_from_solon': haversine(SOLON_LAT, SOLON_LON, lat, lon) if lat and lon else None,
        'url':                 f'https://ultrasignup.com/register.aspx?did={did}',
        'distances':           sort_distances(dist_set),
        'race_type':           'trail',
        'elevation_gain':      None,
        'description':         '',
        'price':               None,
    }


def fetch_ultrasignup():
    # UltraSignup's REST endpoint is decommissioned and their search page requires
    # JavaScript rendering. DID scanning is unreliable because upcoming events span
    # a wide, non-contiguous DID range. Skipping for now.
    print("UltraSignup: skipped (requires JS rendering — future improvement)")
    return []

def _fetch_ultrasignup_did_scan():
    session = requests.Session()
    session.headers['User-Agent'] = BROWSER_UA

    today  = datetime.now().strftime('%Y-%m-%d')
    end_dt = (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')

    max_did   = _us_max_did(session)
    start_did = max(120_000, max_did - 12_000)
    end_did   = max_did + 3_000
    dids      = list(range(start_did, end_did + 1))
    print(f"  max DID ~{max_did}, scanning {start_did}–{end_did} ({len(dids)} pages)")

    def fetch_one(did):
        try:
            r = session.get(
                f'https://ultrasignup.com/register.aspx?did={did}',
                timeout=10,
            )
            if r.status_code != 200:
                return None
            return _parse_us_page(r.text, did, today, end_dt)
        except Exception:
            return None

    races     = []
    completed = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fetch_one, did): did for did in dids}
        for future in as_completed(futures):
            result = future.result()
            if result:
                races.append(result)
            completed += 1
            if completed % 1000 == 0:
                print(f"  {completed}/{len(dids)} scanned, {len(races)} found")

    print(f"  UltraSignup total: {len(races)}")
    return races


# ── RunReg ────────────────────────────────────────────────────────────────────

def fetch_runreg():
    print("Fetching RunReg...")
    races  = []
    today  = datetime.now().strftime('%Y-%m-%d')
    end_dt = (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')

    session = requests.Session()
    session.headers['User-Agent'] = BOT_UA

    # RunReg doesn't support pagination — one call returns the full result set
    try:
        resp = session.get(
            'https://www.runreg.com/api/search',
            params={'startdate': today, 'enddate': end_dt},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  RunReg error: {e}")
        return races

    events = data.get('MatchingEvents', [])
    print(f"  {len(events)} events")

    for ev in events:
        name = (ev.get('EventName') or '').strip()
        if not name:
            continue

        # RunReg dates are .NET ticks: "/Date(1780977600000-0400)/"
        raw_date = ev.get('EventDate') or ''
        date_str = ''
        tick_m = re.search(r'/Date\((\d+)', raw_date)
        if tick_m:
            try:
                ts = int(tick_m.group(1)) / 1000
                date_str = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
            except Exception:
                pass
        if not date_str:
            date_str = raw_date[:10]

        city  = ev.get('EventCity', '')
        state = ev.get('EventState', '')
        if state and len(state) != 2:
            continue

        try:
            lat = float(ev.get('Latitude') or 0) or None
            lon = float(ev.get('Longitude') or 0) or None
        except (ValueError, TypeError):
            lat = lon = None

        categories = ev.get('Categories') or []
        dist_set = set()
        for cat in categories:
            cat_name = cat.get('CategoryName', '') if isinstance(cat, dict) else str(cat)
            for d in normalize_distances(cat_name):
                dist_set.add(d)

        permalink = ev.get('EventPermalink', '')
        url = ev.get('EventUrl') or ev.get('EventWebsite') or (
            f'https://www.runreg.com{permalink}' if permalink else ''
        )

        races.append({
            'id':                  f"rr_{ev.get('EventId', make_dedup_key(name, date_str, state))}",
            'name':                name,
            'source':              'RunReg',
            'date':                date_str,
            'city':                city,
            'state':               state,
            'lat':                 lat,
            'lon':                 lon,
            'distance_from_solon': haversine(SOLON_LAT, SOLON_LON, lat, lon) if lat and lon else None,
            'url':                 url,
            'distances':           sort_distances(dist_set),
            'race_type':           infer_race_type(name),
            'elevation_gain':      None,
            'description':         (ev.get('EventNotes') or '')[:500],
            'price':               None,
        })

    print(f"  RunReg total: {len(races)}")
    return races


# ── Dedup & main ──────────────────────────────────────────────────────────────

def deduplicate(all_races):
    seen, out = {}, []
    for race in all_races:
        key = make_dedup_key(race['name'], race.get('date', ''), race.get('state', ''))
        if key not in seen:
            seen[key] = True
            out.append(race)
    return out


def main():
    all_races = []
    all_races.extend(fetch_runsignup())
    all_races.extend(fetch_ultrasignup())
    all_races.extend(fetch_runreg())

    print(f"\nTotal before dedup: {len(all_races)}")
    races = deduplicate(all_races)
    print(f"Total after dedup:  {len(races)}")

    races.sort(key=lambda r: r.get('date') or '')

    output = {
        'updated': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'count':   len(races),
        'races':   races,
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(output, f, separators=(',', ':'))

    print(f"Saved {len(races)} races → {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
