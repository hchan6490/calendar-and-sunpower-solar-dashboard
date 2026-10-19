#!/usr/bin/env python3
"""
Solar Proxy for SunPower PVS6 (newer authenticated firmware)
--------------------------------------------------------------
Runs a tiny local web server on your PC. It logs into your PVS6 gateway
over HTTPS (with the self-signed cert it uses), pulls live production /
consumption / inverter data, and serves it to the dashboard.html page
running in your browser at http://localhost:8765

WHY THIS EXISTS: browsers block webpages from making authenticated
cross-device HTTPS requests to devices like your PVS6 (CORS security).
This script isn't a webpage, so it doesn't have that restriction -
it does the login/auth itself, then hands clean JSON to your browser.

SETUP (do this before running):
1. Copy config.example.json to config.json and fill it in:
   - pvs_ip: your gateway's local IP address
   - pvs_password: the LAST 5 CHARACTERS of your PVS serial number
     (find the full serial in the SunStrong Connect app under
     Profile -> System Info, or on a sticker on the gateway itself)
   - zip_code: used for weather
   - calendars: one entry per Google Calendar (secret iCal URL, name, color)
2. Put this file, dashboard.html and config.json in the same folder.
3. Run it:  python solar_proxy.py   (or python3 solar_proxy.py)
4. Open http://localhost:8765 in your browser.
5. Leave the terminal window open while you use the dashboard - closing
   it stops the local server. Press Ctrl+C to stop it on purpose.
"""

import base64
import json
import ssl
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import sys
import threading
import time
import sqlite3
import bisect
import calendar
import math
import statistics
from datetime import datetime, timedelta

with open('config.json', 'r') as f:
    config = json.load(f)

try:
    import icalendar
    import recurring_ical_events
    CALENDAR_LIBS_AVAILABLE = True
except ImportError:
    CALENDAR_LIBS_AVAILABLE = False

# ---------------- Settings (all come from config.json) ----------------
PVS_IP = config['pvs_ip']                   # your gateway's local IP
PVS_PASSWORD = config['pvs_password']       # last 5 characters of your PVS serial number
ZIP_CODE = config['zip_code']               # for weather logging

DEFAULT_CALENDAR_COLORS = ["#f5a623", "#4fd1c5", "#d97c7c", "#8fb3ff", "#c39bd3", "#7bd389"]


def _load_calendars(cfg):
    """Calendars are listed in config.json, one entry each:
         "calendars": [{"name": "Family", "url": "<secret iCal URL>", "color": "#f5a623"}, ...]
       (color is optional). Each URL comes from Google Calendar > Settings and sharing >
       Integrate calendar > "Secret address in iCal format".
       Older configs that used one "<name>_calendar_url" key per calendar still work:
       the display name is taken from the key ("family_calendar_url" -> "Family")."""
    entries = cfg.get("calendars")
    if entries is None:
        entries = [{"name": k[:-len("_calendar_url")].replace("_", " ").title(), "url": v}
                   for k, v in cfg.items() if k.endswith("_calendar_url")]
    return [{"name": e.get("name", f"Calendar {i + 1}"),
             "url": e.get("url", ""),
             "color": e.get("color", DEFAULT_CALENDAR_COLORS[i % len(DEFAULT_CALENDAR_COLORS)])}
            for i, e in enumerate(entries)]


CALENDARS = _load_calendars(config)
# ------------------------------------------

LOCAL_PORT = 8765
USERNAME = "ssm_owner"

# Ignore the self-signed cert the PVS6 uses (same as curl -k in the docs)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_session_cookie = None
_lock = threading.Lock()

# ---------------- Health / status tracking ----------------
# Remembers when each data source last worked (or failed) so the dashboard can
# show "last updated" times and raise a visible warning when something stops.
_status = {
    "pvs": {"last_success_ts": None, "last_error": None, "last_error_ts": None, "consecutive_failures": 0},
    "weather": {"last_ok_ts": None, "last_error": None, "last_error_ts": None},
    "calendar": {"last_ok_ts": None, "last_error": None, "last_error_ts": None},
}


def _note_ok(source):
    s = _status[source]
    if source == "pvs":
        s["last_success_ts"] = time.time()
        s["consecutive_failures"] = 0
    else:
        s["last_ok_ts"] = time.time()


def _note_error(source, err):
    s = _status[source]
    msg = str(err) or err.__class__.__name__
    s["last_error"] = msg if len(msg) <= 200 else msg[:197] + "..."
    s["last_error_ts"] = time.time()
    if source == "pvs":
        s["consecutive_failures"] += 1

# ---------------- History tracking ----------------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solar_history.db")
BACKGROUND_LOG_INTERVAL_SECONDS = 1800  # check in every 30 minutes even if nobody has the dashboard open
LOG_INTERVAL_SECONDS = 1800  # don't write a new row to the database more often than this (30 minutes)


def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS readings (ts REAL PRIMARY KEY, pv_en REAL)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS weather (
            date TEXT PRIMARY KEY,
            weathercode INTEGER,
            temp_max REAL,
            temp_min REAL,
            precip_sum REAL,
            sunshine_hours REAL,
            shortwave_radiation REAL
        )"""
    )
    conn.commit()
    conn.close()


# ---------------- Weather ----------------
_geo_lock = threading.Lock()
_LAT = None
_LON = None
_PLACE_NAME = None
_current_weather_cache = {"ts": 0, "data": None}
CURRENT_WEATHER_CACHE_SECONDS = 600  # don't hit the API more than needed


def _geocode_zip():
    global _LAT, _LON, _PLACE_NAME
    with _geo_lock:
        if _LAT is not None:
            return
        url = f"https://api.zippopotam.us/us/{ZIP_CODE}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        place = data["places"][0]
        _LAT = float(place["latitude"])
        _LON = float(place["longitude"])
        _PLACE_NAME = f"{place['place name']}, {place['state abbreviation']}"
        print(f"[proxy] Weather location resolved: {_PLACE_NAME} ({_LAT}, {_LON})")


def _fetch_historical_weather(start_date, end_date):
    """start_date/end_date are 'YYYY-MM-DD' strings. Fetches and stores daily
    weather for that range from Open-Meteo's free historical archive."""
    _geocode_zip()
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={_LAT}&longitude={_LON}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=weathercode,temperature_2m_max,temperature_2m_min,"
        f"precipitation_sum,sunshine_duration,shortwave_radiation_sum"
        f"&temperature_unit=fahrenheit"
        f"&timezone=auto"
    )
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode())

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    if not dates:
        return 0

    conn = sqlite3.connect(DB_PATH)
    for i, d in enumerate(dates):
        conn.execute(
            """INSERT OR REPLACE INTO weather
               (date, weathercode, temp_max, temp_min, precip_sum, sunshine_hours, shortwave_radiation)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                d,
                daily.get("weathercode", [None] * len(dates))[i],
                daily.get("temperature_2m_max", [None] * len(dates))[i],
                daily.get("temperature_2m_min", [None] * len(dates))[i],
                daily.get("precipitation_sum", [None] * len(dates))[i],
                (daily.get("sunshine_duration", [None] * len(dates))[i] or 0) / 3600.0,
                daily.get("shortwave_radiation_sum", [None] * len(dates))[i],
            ),
        )
    conn.commit()
    conn.close()
    return len(dates)


def _get_current_weather():
    try:
        result = _get_current_weather_inner()
    except Exception as e:
        _note_error("weather", e)
        raise
    _note_ok("weather")
    return result


def _get_current_weather_inner():
    now = time.time()
    if _current_weather_cache["data"] and (now - _current_weather_cache["ts"] < CURRENT_WEATHER_CACHE_SECONDS):
        return _current_weather_cache["data"]

    _geocode_zip()
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={_LAT}&longitude={_LON}"
        f"&current=temperature_2m,cloud_cover,precipitation,weathercode"
        f"&temperature_unit=fahrenheit"
        f"&timezone=auto"
    )
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read().decode())

    current = data.get("current", {})
    result = {
        "place": _PLACE_NAME,
        "temp_f": current.get("temperature_2m"),
        "cloud_cover_pct": current.get("cloud_cover"),
        "precipitation": current.get("precipitation"),
        "weathercode": current.get("weathercode"),
    }
    _current_weather_cache["data"] = result
    _current_weather_cache["ts"] = now
    return result


def backfill_weather_history():
    """Fills in weather for every day we're missing, from the earliest solar
    reading through yesterday (the archive has a short lag for very recent
    days, so today's weather isn't reliably available here - use
    _get_current_weather() for today's live snapshot instead)."""
    rows = _all_readings()
    if not rows:
        return
    earliest = datetime.fromtimestamp(rows[0][0]).strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    existing = set(r[0] for r in conn.execute("SELECT date FROM weather").fetchall())
    conn.close()

    if earliest > yesterday:
        return  # solar history only goes back to today, nothing to backfill yet

    # If we're missing anything in range, just refetch the whole range - it's
    # one lightweight API call and keeps this logic simple.
    needed_start = earliest
    if existing:
        # only fetch from the day after our latest stored weather, if any gap exists
        latest_have = max(existing)
        if latest_have >= yesterday:
            return  # already fully backfilled through yesterday
        needed_start = (datetime.strptime(latest_have, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        count = _fetch_historical_weather(needed_start, yesterday)
        print(f"[proxy] Weather backfill: logged {count} day(s) from {needed_start} to {yesterday}")
        _note_ok("weather")
    except Exception as e:
        print(f"[proxy] Weather backfill failed: {e}")
        _note_error("weather", e)


# ---------------- Calendar ----------------
_calendar_cache = {}  # keyed by (start_date_str, end_date_str) -> {"ts":, "events":, "error":}
CALENDAR_CACHE_SECONDS = 900  # 15 minutes - no need to hit Google more often than that


def _fetch_calendar_events(start, end):
    """start/end are datetime objects defining the range to fetch."""
    if not CALENDAR_LIBS_AVAILABLE:
        raise RuntimeError(
            "Calendar libraries not installed. Run: pip install icalendar recurring-ical-events --break-system-packages"
        )
    if not CALENDARS:
        raise RuntimeError("No calendars configured - add a \"calendars\" list to config.json (see config.example.json).")

    all_events = []
    errors = []
    any_success = False

    def iso(d):
        if isinstance(d, datetime):
            return d.isoformat()
        return d.isoformat() + "T00:00:00"

    for cal_cfg in CALENDARS:
        name = cal_cfg.get("name", "Calendar")
        url = cal_cfg.get("url", "")
        color = cal_cfg.get("color", "#f5a623")

        if not url or url == "PASTE_YOUR_SECRET_ICAL_URL_HERE" or url == "PASTE_ANOTHER_SECRET_ICAL_URL_HERE":
            errors.append(f"{name}: URL not set")
            continue

        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                raw = resp.read()
            cal = icalendar.Calendar.from_ical(raw)
            occurrences = recurring_ical_events.of(cal).between(start, end)

            for e in occurrences:
                summary = str(e.get("SUMMARY", "(no title)"))
                location = str(e.get("LOCATION", "")) if e.get("LOCATION") else ""
                dtstart = e.get("DTSTART").dt
                dtend = e.get("DTEND").dt if e.get("DTEND") else dtstart
                all_day = not isinstance(dtstart, datetime)

                all_events.append({
                    "summary": summary,
                    "location": location,
                    "start": iso(dtstart),
                    "end": iso(dtend),
                    "all_day": all_day,
                    "calendar_name": name,
                    "calendar_color": color,
                })
            any_success = True
        except Exception as e:
            errors.append(f"{name}: {e}")

    if not any_success and errors:
        raise RuntimeError("; ".join(errors))

    all_events.sort(key=lambda x: x["start"])
    return all_events, errors


def get_calendar_events(start, end):
    """start/end are datetime objects. Results are cached per date-range key."""
    cache_key = (start.date().isoformat(), end.date().isoformat())
    now = time.time()
    cached = _calendar_cache.get(cache_key)

    if cached and cached["events"] is not None and (now - cached["ts"] < CALENDAR_CACHE_SECONDS):
        return cached["events"], cached["error"]

    try:
        events, partial_errors = _fetch_calendar_events(start, end)
        error = "; ".join(partial_errors) if partial_errors else None
        _calendar_cache[cache_key] = {"ts": now, "events": events, "error": error}
        if error:
            _note_error("calendar", error)
        else:
            _note_ok("calendar")
        return events, error
    except Exception as e:
        _note_error("calendar", e)
        _calendar_cache[cache_key] = {"ts": now, "events": (cached["events"] if cached else None), "error": str(e)}
        return (cached["events"] if cached else None), str(e)


def _weather_background_logger():
    # Give the main login/data flow a moment to settle first
    time.sleep(15)
    while True:
        try:
            backfill_weather_history()
        except Exception as e:
            print(f"[proxy] Weather background logging failed: {e}")
        time.sleep(6 * 3600)  # re-check every 6 hours - cheap, and catches newly available archive days


_last_logged_ts = None


def _record_reading(ts, pv_en):
    global _last_logged_ts
    if pv_en is None:
        return
    if _last_logged_ts is not None and (ts - _last_logged_ts) < LOG_INTERVAL_SECONDS:
        return  # too soon since the last write - skip it, keeps the dashboard's 30s polling from flooding the db
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO readings (ts, pv_en) VALUES (?, ?)", (ts, pv_en)
        )
        conn.commit()
        conn.close()
        _last_logged_ts = ts
    except Exception as e:
        print(f"[proxy] Failed to record history reading: {e}")


def _first_reading_on_or_after(ts):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT ts, pv_en FROM readings WHERE ts >= ? ORDER BY ts ASC LIMIT 1", (ts,)
    ).fetchone()
    conn.close()
    return row


def _latest_reading():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT ts, pv_en FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    conn.close()
    return row


def compute_history():
    curve = _build_curve()
    if curve is None:
        return None
    latest_ts = curve.ts[-1]

    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day)
    # Sunday-Saturday week: Python's weekday() is Mon=0..Sun=6, so shift by (weekday+1)%7 to land on Sunday
    days_since_sunday = (today_start.weekday() + 1) % 7
    week_start = today_start - timedelta(days=days_since_sunday)
    month_start = datetime(now.year, now.month, 1)
    year_start = datetime(now.year, 1, 1)

    def total_since(start_dt):
        res = energy_between(curve, start_dt.timestamp(), latest_ts)
        return res[0] if res else None

    return {
        "today": total_since(today_start),
        "week": total_since(week_start),
        "month": total_since(month_start),
        "ytd": total_since(year_start),
        "earliest_reading_ts": _earliest_reading_ts(),
    }


def _earliest_reading_ts():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT MIN(ts) FROM readings").fetchone()
    conn.close()
    return row[0] if row else None


def _all_readings():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT ts, pv_en FROM readings ORDER BY ts ASC").fetchall()
    conn.close()
    return rows


def get_weather_for_month(year, month):
    month_start = f"{year:04d}-{month:02d}-01"
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    month_end_exclusive = f"{next_y:04d}-{next_m:02d}-01"

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT date, weathercode, temp_max, temp_min, precip_sum, sunshine_hours, shortwave_radiation
           FROM weather WHERE date >= ? AND date < ? ORDER BY date ASC""",
        (month_start, month_end_exclusive),
    ).fetchall()
    conn.close()

    return [
        {
            "date": r[0], "weathercode": r[1], "temp_max": r[2], "temp_min": r[3],
            "precip_sum": r[4], "sunshine_hours": r[5], "shortwave_radiation": r[6],
        }
        for r in rows
    ]


# ---------------- Gap-aware energy totals ----------------
# The PVS reports a lifetime kWh counter, and every total on the dashboard is
# the difference between that counter at two moments. When the logger wasn't
# running there's no reading exactly at midnight (or the start of the week or
# month), so we estimate what the counter read at that moment:
#   * between two readings close together, a straight line is plenty accurate
#   * across a long gap, the gap's energy is spread using the hours of
#     daylight each day AND each day's measured solar radiation (from the
#     weather data) - so nighttime gets none, and a sunny day gets a bigger
#     share than a cloudy one.
# This replaces the old behavior of dumping a whole gap onto a single day.

LINEAR_MAX_GAP_SECONDS = 2 * 3600   # gaps longer than this get the daylight/weather-weighted treatment
FALLBACK_SUNRISE_HOUR = 6           # only used if the zip code couldn't be geocoded yet
FALLBACK_SUNSET_HOUR = 18


class EnergyCurve:
    def __init__(self, rows, radiation_by_date, lat=None, lon=None):
        self.ts = [r[0] for r in rows]
        self.vals = [r[1] for r in rows]
        self.rad = radiation_by_date or {}
        self.lat = lat
        self.lon = lon
        known = [v for v in self.rad.values() if v is not None and v > 0]
        self.default_weight = (sum(known) / len(known)) if known else 1.0
        self._window_cache = {}

    def _window(self, d):
        """(sunrise_ts, sunset_ts) for local calendar date d, from standard NOAA solar equations."""
        cached = self._window_cache.get(d)
        if cached:
            return cached
        if self.lat is None or self.lon is None:
            sr = datetime(d.year, d.month, d.day, FALLBACK_SUNRISE_HOUR).timestamp()
            ss = datetime(d.year, d.month, d.day, FALLBACK_SUNSET_HOUR).timestamp()
        else:
            n = d.timetuple().tm_yday
            g = 2 * math.pi / 365 * (n - 1)
            eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                               - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
            decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
                    - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
                    - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
            lat_r = math.radians(self.lat)
            cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lat_r) * math.cos(decl))
                      - math.tan(lat_r) * math.tan(decl))
            ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
            sunrise_min = 720 - 4 * (self.lon + ha) - eqtime   # minutes after 00:00 UTC of date d
            sunset_min = 720 - 4 * (self.lon - ha) - eqtime
            base = calendar.timegm((d.year, d.month, d.day, 0, 0, 0))
            sr, ss = base + sunrise_min * 60, base + sunset_min * 60
        self._window_cache[d] = (sr, ss)
        return sr, ss

    @staticmethod
    def _fraction_done(t, sr, ss):
        """Share of a day's production completed by time t (panel output follows a half-sine through the day)."""
        if t <= sr:
            return 0.0
        if t >= ss:
            return 1.0
        x = (t - sr) / (ss - sr)
        return (1 - math.cos(math.pi * x)) / 2

    def _expected(self, t1, t2):
        """Relative amount of production we'd expect between two moments."""
        total = 0.0
        d = datetime.fromtimestamp(t1).date()
        last = datetime.fromtimestamp(t2).date()
        while d <= last:
            sr, ss = self._window(d)
            w = self.rad.get(d.isoformat())
            w = self.default_weight if w is None else max(w, self.default_weight * 0.05)
            total += w * (self._fraction_done(t2, sr, ss) - self._fraction_done(t1, sr, ss))
            d += timedelta(days=1)
        return total

    def value_at(self, t):
        """Estimated lifetime counter (kWh) at time t, or None if t is outside the logged range."""
        i = bisect.bisect_right(self.ts, t)
        if i == 0:
            return None
        t1, v1 = self.ts[i - 1], self.vals[i - 1]
        if t == t1:
            return v1
        if i == len(self.ts):
            return None
        t2, v2 = self.ts[i], self.vals[i]
        delta = max(0.0, v2 - v1)
        if t2 - t1 > LINEAR_MAX_GAP_SECONDS:
            total = self._expected(t1, t2)
            if total > 1e-9:
                return v1 + delta * (self._expected(t1, t) / total)
        return v1 + delta * (t - t1) / (t2 - t1)

    def boundary_is_estimated(self, t):
        """True if moment t sits inside a long logging gap that has daylight production on BOTH sides of t -
        meaning the split between the two neighboring days is an estimate rather than a measurement."""
        i = bisect.bisect_right(self.ts, t)
        if i == 0 or i >= len(self.ts):
            return False
        t1, t2 = self.ts[i - 1], self.ts[i]
        if t == t1 or t2 - t1 <= LINEAR_MAX_GAP_SECONDS:
            return False
        before, after = self._expected(t1, t), self._expected(t, t2)
        total = before + after
        return total > 0 and min(before, after) / total > 0.02


def energy_between(curve, start_ts, end_ts, day_date=None):
    """kWh produced between two moments, as (kwh, partial, estimated) - or None if there's no data in that window.
    partial: the window extends past the newest reading.  estimated: a window edge fell inside a logging gap.
    day_date: pass the calendar date when the window is a single day - a day isn't "partial" once the newest
    reading was taken after sunset, because nothing more can be produced that day."""
    first_ts, last_ts = curve.ts[0], curve.ts[-1]
    if end_ts <= first_ts or start_ts >= last_ts:
        return None
    s = max(start_ts, first_ts)
    e = min(end_ts, last_ts)
    kwh = curve.value_at(e) - curve.value_at(s)
    estimated = curve.boundary_is_estimated(start_ts) or curve.boundary_is_estimated(end_ts)
    partial = end_ts > last_ts
    if partial and day_date is not None and last_ts >= curve._window(day_date)[1]:
        partial = False
    return round(kwh, 2), partial, estimated


def _build_curve():
    rows = _all_readings()
    if not rows:
        return None
    conn = sqlite3.connect(DB_PATH)
    rad = {d: r for d, r in conn.execute(
        "SELECT date, shortwave_radiation FROM weather WHERE shortwave_radiation IS NOT NULL")}
    conn.close()
    return EnergyCurve(rows, rad, _LAT, _LON)


def compute_daily_breakdown(year=None, month=None):
    """Returns a list of {date, kwh, partial, estimated} for each day in the given
    calendar month (defaults to the current month). 'partial' marks a day that
    hasn't fully elapsed yet; 'estimated' marks a day whose edge fell inside a
    logging gap, so its total was spread using daylight and weather data."""
    curve = _build_curve()
    if curve is None:
        return []

    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    month_start = datetime(year, month, 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    month_end_exclusive = datetime(next_y, next_m, 1)

    is_current_month = (year == now.year and month == now.month)
    day_limit = datetime(now.year, now.month, now.day) if is_current_month else (month_end_exclusive - timedelta(days=1))

    results = []
    day = month_start
    while day <= day_limit:
        next_day = day + timedelta(days=1)
        res = energy_between(curve, day.timestamp(), next_day.timestamp(), day_date=day.date())
        if res is not None:
            kwh, partial, estimated = res
            results.append({
                "date": day.strftime("%Y-%m-%d"),
                "kwh": kwh,
                "partial": partial,
                "estimated": estimated,
            })
        day = next_day

    return results


def compute_monthly_summary():
    """Returns a list of {year, month, kwh, partial, estimated} for every calendar
    month we have data for. 'partial' means the month isn't over yet (or logging
    stopped mid-month), so the kwh figure is only as of the most recent reading."""
    curve = _build_curve()
    if curve is None:
        return []

    first_dt = datetime.fromtimestamp(curve.ts[0])
    last_dt = datetime.fromtimestamp(curve.ts[-1])

    results = []
    y, m = first_dt.year, first_dt.month
    while (y, m) <= (last_dt.year, last_dt.month):
        start_dt = datetime(y, m, 1)
        next_y, next_m = (y + 1, 1) if m == 12 else (y, m + 1)
        next_dt = datetime(next_y, next_m, 1)

        res = energy_between(curve, start_dt.timestamp(), next_dt.timestamp())
        if res is not None:
            kwh, partial, estimated = res
            results.append({
                "year": y, "month": m,
                "kwh": kwh,
                "partial": partial,
                "estimated": estimated,
            })

        y, m = next_y, next_m

    return results


def _attempt_level(source):
    """'warn' if the most recent attempt for this source failed, 'ok' if it last succeeded, else 'unknown'."""
    s = _status[source]
    if s["last_error_ts"] and (s["last_ok_ts"] is None or s["last_error_ts"] > s["last_ok_ts"]):
        return "warn"
    return "ok" if s["last_ok_ts"] else "unknown"


def _pvs_level(now):
    p = _status["pvs"]
    last_ok, fails = p["last_success_ts"], p["consecutive_failures"]
    if last_ok is None:
        return "bad" if fails > 0 else "unknown"
    age = now - last_ok
    if fails >= 3 or age > 3 * 3600:
        return "bad"
    if fails >= 1 or age > 1.5 * BACKGROUND_LOG_INTERVAL_SECONDS:
        return "warn"
    return "ok"


def compute_status():
    now = time.time()
    p = _status["pvs"]
    pvs_level = _pvs_level(now)

    latest = _latest_reading()
    last_reading_ts = latest[0] if latest else None
    history_level = "unknown" if last_reading_ts is None else "ok"
    pvs_fresh = p["last_success_ts"] is not None and (now - p["last_success_ts"]) < 600
    if last_reading_ts is not None and pvs_fresh and (now - last_reading_ts) > 2 * LOG_INTERVAL_SECONDS + 600:
        history_level = "warn"  # live data is arriving but readings aren't being saved

    def simple(source):
        s = _status[source]
        return {"level": _attempt_level(source), "last_ok_ts": s["last_ok_ts"],
                "last_error": s["last_error"], "last_error_ts": s["last_error_ts"]}

    return {
        "now": now,
        "interval_seconds": BACKGROUND_LOG_INTERVAL_SECONDS,
        "solar": {
            "level": pvs_level,
            "last_success_ts": p["last_success_ts"],
            "last_error": p["last_error"],
            "last_error_ts": p["last_error_ts"],
            "consecutive_failures": p["consecutive_failures"],
        },
        "history": {"level": history_level, "last_reading_ts": last_reading_ts},
        "weather": simple("weather"),
        "calendar": simple("calendar"),
    }


# ---------------- Sunlight-adjusted output (cleaning signal) ----------------
# Each complete day: kWh produced divided by that day's "sun-hours" (solar radiation
# in kWh/m2, i.e. hours of full 1000 W/m2 sun). Cloudy days stop looking like dirty-
# panel days, so a slow drift downward that rain partly reverses points to dust.
EFFICIENCY_MIN_RADIATION_MJ = 6.0   # on very dim days the ratio gets too noisy to trust
EFFICIENCY_ROLLING_DAYS = 14
EFFICIENCY_ROLLING_MIN_POINTS = 5
RAIN_MM_THRESHOLD = 1.0             # a day with at least this much rain counts as a "rinse"
MJ_PER_KWH = 3.6
TEMP_COEFF_PER_C = -0.0035          # typical silicon panels lose ~0.35% output per degree C of heat


def _temperature_factor(tmax, tmin):
    """Rough heat derating (1.0 = panels at 25C). Panel temp is estimated as daytime air temp + ~25C."""
    if tmax is None:
        return None
    if tmin is None:
        tmin = tmax
    if tmax < 44:  # a SoCal daily high under 44 can only be Celsius - rows saved before the Fahrenheit fix
        tmax, tmin = tmax * 9 / 5 + 32, tmin * 9 / 5 + 32
    day_c = (0.75 * tmax + 0.25 * tmin - 32) * 5 / 9
    return max(0.6, 1 + TEMP_COEFF_PER_C * day_c)   # (panel temp - 25C) works out to day_c


def compute_efficiency(days=90, adjust_temp=True):
    curve = _build_curve()
    if curve is None:
        return {"days": [], "summary": None, "adjusted": bool(adjust_temp)}

    conn = sqlite3.connect(DB_PATH)
    weather = {r[0]: r[1:] for r in conn.execute(
        "SELECT date, shortwave_radiation, precip_sum, temp_max, temp_min FROM weather")}
    conn.close()

    today = datetime.now().date()
    d = datetime.fromtimestamp(curve.ts[0]).date()
    rows = []
    while d <= today:
        start = datetime(d.year, d.month, d.day)
        res = energy_between(curve, start.timestamp(), (start + timedelta(days=1)).timestamp(), day_date=d)
        rad, precip, tmax, tmin = weather.get(d.isoformat(), (None, None, None, None))
        row = {"date": d.isoformat(), "kwh": None, "sun_hours": None, "yield": None, "rolling": None,
               "rain": bool(precip is not None and precip >= RAIN_MM_THRESHOLD), "precip_mm": precip}
        # only complete, measured days with enough sunshine to give a trustworthy ratio
        if res is not None and not res[1] and not res[2] and rad is not None and rad >= EFFICIENCY_MIN_RADIATION_MJ:
            kwh = res[0]
            sun_hours = rad / MJ_PER_KWH
            y = kwh / sun_hours
            if adjust_temp:
                factor = _temperature_factor(tmax, tmin)
                if factor:
                    y /= factor
            row["kwh"], row["sun_hours"], row["yield"] = kwh, round(sun_hours, 2), round(y, 3)
        rows.append(row)
        d += timedelta(days=1)

    # trailing 14-day median smooths out day-to-day noise
    by_date = {r["date"]: r for r in rows}
    for r in rows:
        if r["yield"] is None:
            continue
        day0 = datetime.strptime(r["date"], "%Y-%m-%d").date()
        window = []
        for k in range(EFFICIENCY_ROLLING_DAYS):
            rr = by_date.get((day0 - timedelta(days=k)).isoformat())
            if rr and rr["yield"] is not None:
                window.append(rr["yield"])
        if len(window) >= EFFICIENCY_ROLLING_MIN_POINTS:
            r["rolling"] = round(statistics.median(window), 3)

    def median_in(first_day, last_day):
        vals = [r["yield"] for r in rows
                if r["yield"] is not None and first_day.isoformat() <= r["date"] <= last_day.isoformat()]
        return statistics.median(vals) if len(vals) >= EFFICIENCY_ROLLING_MIN_POINTS else None

    recent = median_in(today - timedelta(days=EFFICIENCY_ROLLING_DAYS - 1), today)
    peak_candidates = [r["rolling"] for r in rows
                       if r["rolling"] is not None and r["date"] >= (today - timedelta(days=120)).isoformat()]
    peak = max(peak_candidates + ([recent] if recent else [])) if (peak_candidates or recent) else None
    last_year = median_in(today - timedelta(days=365 + EFFICIENCY_ROLLING_DAYS - 1), today - timedelta(days=365))
    rain_days = [r["date"] for r in rows if r["rain"]]
    last_rain = rain_days[-1] if rain_days else None

    vs_peak = (recent - peak) / peak * 100 if recent and peak else None
    vs_last_year = (recent - last_year) / last_year * 100 if recent and last_year else None
    if recent is None:
        status = "not_enough_data"
    elif vs_peak <= -5:
        status = "attention"
    elif vs_peak <= -2.5:
        status = "watch"
    else:
        status = "ok"

    summary = {
        "recent": round(recent, 3) if recent else None,
        "peak": round(peak, 3) if peak else None,
        "vs_peak_pct": round(vs_peak, 1) if vs_peak is not None else None,
        "vs_last_year_pct": round(vs_last_year, 1) if vs_last_year is not None else None,
        "last_rain_date": last_rain,
        "days_since_rain": (today - datetime.strptime(last_rain, "%Y-%m-%d").date()).days if last_rain else None,
        "status": status,
        "valid_days": sum(1 for r in rows if r["yield"] is not None),
    }

    if days and days > 0:
        cutoff = (today - timedelta(days=days)).isoformat()
        rows = [r for r in rows if r["date"] >= cutoff]
    return {"days": rows, "summary": summary, "adjusted": bool(adjust_temp)}


def _background_logger():
    while True:
        try:
            data = get_live_data()
            pv_en = num(data.get("livedata", {}).get("/sys/livedata/pv_en"))
            _record_reading(time.time(), pv_en)
        except Exception as e:
            print(f"[proxy] Background logging attempt failed: {e}")
        time.sleep(BACKGROUND_LOG_INTERVAL_SECONDS)


def _login():
    global _session_cookie
    auth = base64.b64encode(f"{USERNAME}:{PVS_PASSWORD}".encode()).decode()
    url = f"https://{PVS_IP}/auth?login"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {auth}"},
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as resp:
            # Pull the session cookie out of the Set-Cookie header
            raw_cookie = resp.headers.get("Set-Cookie", "")
            cookie_value = raw_cookie.split(";")[0] if raw_cookie else None
            if not cookie_value:
                # Some firmware returns the session only in the JSON body instead
                body = json.loads(resp.read().decode())
                session = body.get("session")
                if session:
                    cookie_value = f"session={session}"
            _session_cookie = cookie_value
            print("[proxy] Logged in to PVS OK")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"[proxy] LOGIN FAILED: {url}")
        print(f"[proxy]   status={e.code} reason={e.reason}")
        print(f"[proxy]   (pvs_password is {len(PVS_PASSWORD)} characters long - it should be exactly 5)")
        print(f"[proxy]   server response body: {detail!r}")
        raise
    except Exception as e:
        print(f"[proxy] LOGIN FAILED with non-HTTP error: {type(e).__name__}: {e}")
        raise


def _query(match_expr, cache=None):
    global _session_cookie
    if not _session_cookie:
        _login()

    encoded_match = urllib.parse.quote(match_expr, safe="")
    cache_part = f"&cache={cache}" if cache else ""

    def do_request():
        url = f"https://{PVS_IP}/vars?match={encoded_match}&fmt=obj{cache_part}"
        req = urllib.request.Request(
            url,
            headers={"Cookie": _session_cookie},
        )
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            print(f"[proxy] QUERY FAILED: {url}")
            print(f"[proxy]   status={e.code} reason={e.reason}")
            print(f"[proxy]   server response body: {detail!r}")
            raise

    try:
        return do_request()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # session expired, log in again and retry once
            _login()
            return do_request()
        raise


def _group_by_device(flat, prefix):
    """Turns {'/sys/devices/meter/0/p3phsumKw': '1.87', ...} into
    {'0': {'p3phsumKw': '1.87', ...}, '1': {...}}"""
    result = {}
    marker = prefix + "/"
    for key, val in flat.items():
        if not key.startswith(marker):
            continue
        remainder = key[len(marker):]  # e.g. "0/p3phsumKw"
        parts = remainder.split("/", 1)
        if len(parts) != 2:
            continue
        idx, field = parts
        result.setdefault(idx, {})[field] = val
    return result


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_live_data():
    with _lock:
        livedata = _query("livedata")

        try:
            raw_meters = _query("meter")
        except urllib.error.HTTPError as e:
            print(f"[proxy] 'meter' query failed ({e.code}) - likely transient, continuing without meter detail")
            raw_meters = {}

        try:
            raw_inverters = _query("inverter")
        except urllib.error.HTTPError as e:
            print(f"[proxy] 'inverter' query failed ({e.code}) - likely inverters asleep (nighttime/no sun), continuing without inverter detail")
            raw_inverters = {}

        meters = _group_by_device(raw_meters, "/sys/devices/meter")
        inverters = _group_by_device(raw_inverters, "/sys/devices/inverter")

        # Record the baseline reading regardless of whether meter/inverter detail succeeded -
        # this is the number that matters most for daily/weekly/monthly history.
        pv_en = num(livedata.get("/sys/livedata/pv_en"))
        _record_reading(time.time(), pv_en)

        return {"livedata": livedata, "meters": meters, "inverters": inverters}


def get_live_data():
    """Fetches live data from the PVS and records whether the attempt worked."""
    try:
        data = _fetch_live_data()
    except Exception as e:
        _note_error("pvs", e)
        raise
    _note_ok("pvs")
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the terminal quiet

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            self._send_json(compute_status())
            return

        if self.path.startswith("/history/efficiency"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                days = int(qs.get("days", ["90"])[0])
                adjust = qs.get("adjust_temp", ["1"])[0] != "0"
                self._send_json(compute_efficiency(days, adjust))
            except Exception as e:
                self._send_json({"error": str(e)}, 502)
            return

        if self.path == "/data":
            try:
                data = get_live_data()
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                msg = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return

        if self.path == "/history":
            try:
                hist = compute_history()
                body = json.dumps(hist).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                msg = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return

        if self.path.startswith("/calendar/upcoming"):
            try:
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                if "start" in qs and "end" in qs:
                    start = datetime.strptime(qs["start"][0], "%Y-%m-%d")
                    end = datetime.strptime(qs["end"][0], "%Y-%m-%d")
                else:
                    start = datetime.now()
                    end = start + timedelta(days=30)
                events, err = get_calendar_events(start, end)
                body = json.dumps({"events": events or [], "error": err}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                msg = json.dumps({"events": [], "error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return

        if self.path == "/weather/current":
            try:
                w = _get_current_weather()
                body = json.dumps(w).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                msg = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return

        if self.path.startswith("/weather/daily"):
            try:
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                now = datetime.now()
                year = int(qs["year"][0]) if "year" in qs else now.year
                month = int(qs["month"][0]) if "month" in qs else now.month
                w = get_weather_for_month(year, month)
                body = json.dumps(w).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                msg = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return

        if self.path.startswith("/history/mtd") or self.path.startswith("/history/daily"):
            try:
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                year = int(qs["year"][0]) if "year" in qs else None
                month = int(qs["month"][0]) if "month" in qs else None
                daily = compute_daily_breakdown(year, month)
                body = json.dumps(daily).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                msg = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return

        if self.path == "/history/monthly":
            try:
                monthly = compute_monthly_summary()
                body = json.dumps(monthly).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                msg = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return

        # Serve dashboard.html (or index.html) for everything else
        filename = "dashboard.html"
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"dashboard.html not found - keep it in the same folder as this script.")


class QuietServer(ThreadingHTTPServer):
    """Same server, but it doesn't print a traceback when a browser simply hangs up
    mid-response (closing a tab, refreshing, or a newer request replacing an older
    one). That's routine for an auto-refreshing page and harmless - real errors
    still print normally."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    if PVS_PASSWORD == "CHANGE_ME":
        print("=" * 60)
        print("STOP: fill in config.json first (copy config.example.json).")
        print("Set pvs_ip, pvs_password, zip_code and your calendars.")
        print("=" * 60)
        return

    print(f"[proxy] Attempting initial login to https://{PVS_IP} ...")
    try:
        _login()
    except Exception as e:
        print(f"[proxy] Initial login failed: {e}")
        print("[proxy] Will keep serving the page and retry on each request.")

    _init_db()
    threading.Thread(target=_background_logger, daemon=True).start()
    threading.Thread(target=_weather_background_logger, daemon=True).start()
    print(f"[proxy] History logging to {DB_PATH} every {BACKGROUND_LOG_INTERVAL_SECONDS}s (and on every dashboard refresh)")
    print(f"[proxy] Weather logging for zip {ZIP_CODE}, checking every 6 hours")

    server = QuietServer(("localhost", LOCAL_PORT), Handler)
    print(f"[proxy] Serving dashboard at http://localhost:{LOCAL_PORT}")
    print("[proxy] Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[proxy] Stopped.")


if __name__ == "__main__":
    main()
