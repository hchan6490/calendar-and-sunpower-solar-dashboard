# Home Dashboard

A locally-hosted dashboard that replaced SunPower's paywalled monitoring app. It runs entirely on your own network — no cloud service, no subscription — and has grown to cover solar production, weather, and a shared household calendar.

## What it does

- **Live solar monitoring**: current production, consumption, and grid import/export, pulled directly from your PVS6 gateway
- **Historical solar tracking**: Today / This Week / This Month / Year-to-Date totals, a day-by-day chart for any month, and a month-over-month comparison chart (useful for spotting year-over-year panel degradation)
- **Weather logging**: current conditions plus a historical record correlated against your production data, so a low-output day can be checked against whether it was cloudy
- **Shared calendar**: upcoming events from one or more Google Calendars, color-coded, with support for recurring and multi-day events

## Files in this project

| File | Purpose |
|---|---|
| `solar_proxy.py` | The local server. Talks to your PVS6 gateway, Open-Meteo (weather), and Google Calendar. Serves the dashboard and its data. |
| `dashboard.html` | The dashboard itself — what you see in the browser. |
| `config.json` | Your secrets (PVS IP/password, zip code, calendar URLs). **Not meant to be shared or committed anywhere public.** |
| `solar_history.db` | SQLite database, created automatically. Stores every logged solar reading and daily weather. |
| `fix_gap.py` | Legacy utility for manually correcting a logging gap. No longer needed — gaps are estimated automatically (see "Logging gaps" below). |

All files should live in the same folder.

---

## Setup

### 1. Prerequisites

- Python 3 installed on your PC
- One-time package install for calendar support:
  ```
  pip install icalendar recurring-ical-events --break-system-packages
  ```

### 2. `config.json`

Create this file in the same folder, filled in with your own values:

```json
{
  "pvs_ip": "192.168.x.x",
  "pvs_password": "xxxxx",
  "zip_code": "91761"
}
```

- **pvs_ip**: your PVS6 gateway's local IP address. Find it via your router's connected-devices list (or, for eero, the eero app's device list — look for a name like `pvs6-...`).
  - In the Eero App > Network Settings > Reservations & Port Forwarding > Add Reservation > Locate PVS > Add.
- **pvs_password**: the **last 5 characters** of your PVS serial number. Find the full serial in the SunStrong Connect app under Profile → System Info, or on a sticker on the gateway itself.
- **zip_code**: used to look up your location for weather data.

Calendar URLs are configured separately, directly in `solar_proxy.py` (see below) since each calendar also needs a name and color.

### 3. Calendars

In `solar_proxy.py`, find the `CALENDARS` list and add one entry per calendar:

```python
CALENDARS = [
    {"name": "Family", "url": "https://calendar.google.com/calendar/ical/...", "color": "#f5a623"},
    {"name": "Work", "url": "https://calendar.google.com/calendar/ical/...", "color": "#4fd1c5"},
]
```

To get a calendar's URL: in Google Calendar, hover over the calendar in the sidebar → three dots → **Settings and sharing** → scroll to **"Integrate calendar"** → copy the **"Secret address in iCal format."** This is read-only and doesn't require any Google login/OAuth setup — the tradeoff is that events can only be viewed here, not created or edited from the dashboard.

### 4. Run it

```
python solar_proxy.py
```

Leave that terminal window open, then open **http://localhost:8765** in your browser.

---

## Using the dashboard

The header reads **🏠 Home Dashboard**, with two top-level tabs:

### Calendar (default tab)
Upcoming events for the next 30 days, grouped by day ("Today," "Tomorrow," then date). Each event shows its time (start–end for timed events, a date range for multi-day events, "All day" for all-day events), and is color-coded by which calendar it came from.

### Solar
Has two sub-tabs, defaulting to **Overview**:

- **Overview**: live production/consumption/net numbers, Generation History cards (Today/Week/Month/YTD), today's weather, and a panel-by-panel inverter status table (with panel temperature in °F).
- **Chart**: a "Daily Production" line chart (pick any month you have data for) with weather icons above each day's data point and hover tooltips showing exact kWh plus that day's conditions — and a "Monthly Analysis" line chart comparing any two months side by side. Below those is **Sunlight-Adjusted Output** (see below).

---

## Status line and warnings

Under the title is a status line showing when each data source last worked: **Solar** (last successful pull from the gateway), **History** (last reading saved to the database), **Weather**, and **Calendar**. Each has a colored dot: green = fine, amber = needs a look, red = not working, grey = no attempt yet. Hover an item to see its last error.

If something stops working, a **warning banner** appears across the top of every tab, with the last error in plain text and a hint about the likely fix (for example, "gateway isn't answering — check the eero app and `pvs_ip`"). While solar data is stale, the live numbers and panel table are dimmed so old values aren't mistaken for current ones. The banner also appears if the dashboard can't reach `solar_proxy.py` at all (script closed, PC asleep).

The thresholds: solar is **amber** after a failed pull or when the last success is more than 45 minutes old, and **red** after 3 failures in a row or 3 hours without data. The "History" item turns amber if live data is arriving but readings aren't being saved.

## Sunlight-adjusted output (the cleaning signal)

On the Solar → Chart tab. For each complete day it divides the kWh your panels produced by that day's "sun-hours" (hours of full-strength sun, from the logged solar radiation). Because the sunshine is divided out, cloudy days don't look like problems, and a healthy system stays roughly flat. Dust shows up as a slow downward drift that rain (blue marks) partly reverses.

- **The chart:** one dot per usable day, a 14-day median line, and rain markers (days with at least 1 mm).
- **The tiles:** the last 14 days' median, the change versus the recent peak, the change versus the same two weeks last year (once there's a year of history), and the date of the last rain. A plain-English note under them says whether it looks steady, worth watching (2.5% below peak), or worth attention (5% below peak) — and whether recent rain makes dust less likely.
- **"Adjust for temperature"** (on by default) removes the heat dip so summer and winter are comparable. It's a rough estimate (about 0.35% per °C of panel heat), so treat the trend as the signal, not the exact number.
- **What's left out:** partial days, days next to a logging gap (they're estimates, not measurements), and very dim days (under 6 MJ/m² of sunshine) where the ratio gets noisy.
- **Limits:** the sunshine figures are modeled for your area, not measured on your roof, so single days are noisy. A sudden one-day drop is more often shade, an offline panel, or bad data than dirt.

---

## How the historical logging actually works

Think of it like a car odometer: your PVS6 gateway tracks *lifetime* energy production as a number that only ever goes up. The script doesn't calculate daily/weekly/monthly totals directly — it just takes periodic snapshots of that number (timestamp + reading), and every total you see on the dashboard is just **subtraction between two snapshots** — the same way you'd figure out miles driven from an odometer reading at the start and end of a trip.

**What this means in practice:**
- You don't need the script running every single second — you just need *a* snapshot reasonably close to the start of each day/week/month/year, and another whenever you check. The gateway keeps counting in hardware the whole time regardless of whether the script is running.
- **Gaps are handled automatically.** If the script wasn't running for a while, there's no reading exactly at midnight (or the start of the week/month), so the dashboard estimates what the counter read at that moment. Between readings that are close together it draws a straight line. Across a long gap it spreads the gap's energy using the hours of daylight each day and that day's measured solar radiation from the weather data — so nighttime gets none, and a sunny day gets a bigger share than a cloudy one. The most common case (you forgot to start it in the morning, and the last reading was from the evening before) comes out exact, because nothing is produced overnight. Days that fall inside a longer gap are marked **(estimated)** in the chart tooltips, since those totals are good estimates rather than measurements.
- For the cleanest daily/weekly numbers, it helps to have the script running (or run it briefly) once each morning before sunrise — timing doesn't matter precisely since no generation happens overnight anyway.

**Logging frequency:** the dashboard polls live data every 30 seconds for responsiveness, but only writes a new row to the database every `LOG_INTERVAL_SECONDS` (currently 30 minutes) to avoid bloating the database with redundant snapshots. A separate background thread also checks in every `BACKGROUND_LOG_INTERVAL_SECONDS` even if the browser tab isn't open, so logging continues as long as the script is running.

### The 24/7 question

Continuous logging genuinely requires *something* powered on and running the script at all times — that's inherent to reading a live device, not a limitation of this particular setup. Two ways to approach it:
- **Automate on your existing PC**: use Windows Task Scheduler to launch `solar_proxy.py` automatically (at startup or on a schedule), removing the need to remember to run it manually.
- **Move to dedicated always-on hardware**: a Raspberry Pi (or similar), running 24/7 independent of your PC — the more permanent solution, especially if you eventually want the dashboard mounted on its own screen.

---

## Weather integration

- Uses **Open-Meteo** (free, no API key or signup required).
- Your zip code is geocoded once (via a free lookup) to get coordinates, cached for the life of the running script.
- **Current weather** and **historical weather** both pull from the same host (`api.open-meteo.com`) — historical data uses that endpoint's `start_date`/`end_date` range rather than the separate `archive-api.open-meteo.com` subdomain, which had recurring SSL certificate issues.
- All temperatures are requested in Fahrenheit directly from the API (`&temperature_unit=fahrenheit`), so no manual conversion is needed anywhere in the code.
- Historical weather backfills automatically in the background (checked every 6 hours), covering from your earliest solar reading through yesterday. Today's weather always comes from the separate live "current conditions" call, since very recent days have a short lag in the historical data.

---

## The database

`solar_history.db` is a standard SQLite file with two tables:

**`readings`** — one row per logged snapshot
- `ts`: Unix timestamp (seconds since Jan 1, 1970)
- `pv_en`: lifetime production reading at that moment, in kWh

**`weather`** — one row per calendar day
- `date`, `weathercode`, `temp_max`, `temp_min`, `precip_sum`, `sunshine_hours`, `shortwave_radiation`

**To browse or query it directly:**
```
python -m sqlite3 solar_history.db
```
```sql
SELECT datetime(ts, 'unixepoch', 'localtime'), pv_en FROM readings ORDER BY ts DESC LIMIT 10;
```
(Or use a GUI tool like DB Browser for SQLite.)

### Logging gaps and `fix_gap.py`

Gap handling is now built into the dashboard's calculations, so `fix_gap.py` is no longer needed for new gaps. It's still in the folder if you want it, and any checkpoints it inserted in the past are treated like real readings. (Optional: those past checkpoints force an even split between two days. If you'd rather have the weather-weighted estimate for those days, they can be removed — they're the rows whose `ts` is a whole number landing exactly on local midnight. Back up `solar_history.db` first.)

---

## Troubleshooting notes

- **A `401`/`403` followed immediately by `Logged in to PVS OK`** in the terminal is normal — the gateway's login session expires periodically, and the script automatically re-authenticates and retries. Only worry if this repeats on *every* single request.
- **"Failed to fetch"** in the dashboard means the browser couldn't reach the local server at all — usually means `solar_proxy.py` isn't running, was left on an old/crashed process, or needs a restart after an edit.
- **A `400 Bad Request` with errorcode `0x0040`** from the PVS gateway means the specific variable path being queried (e.g., `meter/data`) doesn't exist under that name on your firmware version — this gateway's firmware uses flatter paths like `/sys/devices/meter/0/...` instead of the nested paths shown in SunPower's own documentation.
- **CORS / why this needs a local Python server at all**: browsers block webpages from making authenticated cross-device HTTPS requests directly to your gateway. `solar_proxy.py` isn't a webpage, so it isn't subject to that restriction — it does the login/auth itself and hands clean JSON to the dashboard running in your browser.

---

## API endpoints (for reference)

All served locally by `solar_proxy.py` at `http://localhost:8765`:

| Endpoint | Returns |
|---|---|
| `/` | The dashboard itself |
| `/data` | Live production/consumption/inverter data |
| `/history` | Today/Week/Month/YTD totals |
| `/history/daily?year=Y&month=M` | Day-by-day breakdown for a given month |
| `/history/monthly` | Total kWh per calendar month, across all logged history |
| `/weather/current` | Live current conditions |
| `/weather/daily?year=Y&month=M` | Logged daily weather for a given month |
| `/calendar/upcoming` | Upcoming events across all configured calendars |
| `/status` | When each data source last worked, plus its health level and last error |
| `/history/efficiency?days=N&adjust_temp=0/1` | Daily sunlight-adjusted output, 14-day median, rain days, and the summary used by the cleaning-signal chart |

---

## Possible future directions

- **Raspberry Pi migration**: move the whole setup to always-on dedicated hardware with a permanently attached monitor.
- **Task Scheduler automation**: auto-launch on Windows startup/schedule as a lighter-weight alternative or stepping stone to the Pi.
- **Further dashboard reorg**: as this has grown from a solar-only tool into a general household dashboard, the layout may continue to evolve (e.g., a combined "at a glance" landing view).
- **Per-panel tracking**: the PVS reports each inverter, so logging them separately would show *which* panel is under-performing, not just that the system as a whole dipped.
