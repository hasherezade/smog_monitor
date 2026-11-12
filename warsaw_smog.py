#!/usr/bin/env python3
"""Warsaw smog desktop widget.

Shows GIOŚ monitoring stations on a small map of Warsaw, coloured by the
Polish air quality index, with PM10 / PM2.5 readings on hover.

Data:  Główny Inspektorat Ochrony Środowiska (GIOŚ), https://powietrze.gios.gov.pl
Map:   © OpenStreetMap contributors

Dependencies: python3-gi, python3-gi-cairo, gir1.2-gtk-3.0 (all in a default
Ubuntu desktop install). Everything else is stdlib.

Layout of this file:
  1. config          - constants you may want to change
  2. GIOŚ client     - pure functions, no GUI imports (unit-testable)
  2b. Airly client   - optional second source, needs a personal API key
  2c. Sensor.Community client - optional third source, open data, no key
  3. map tiles       - Web Mercator maths + on-disk tile cache
  4. renderer        - draws onto any cairo context (also used by --snapshot)
  5. GTK shell       - window, input, refresh timer
  6. district outlines - boundary data for "which district is this?"
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ----------------------------------------------------------------------------
# 1. config
# ----------------------------------------------------------------------------
CITY = "Warszawa"                 # matched against "Nazwa miasta"
MAP_CENTER = (52.232, 21.045)     # lat, lon
MAP_ZOOM = 10                     # z10 fits the whole city in ~320 px
WIDTH, HEIGHT = 360, 598

# Map navigation: drag the map to move it, scroll over it to zoom.
ZOOM_MIN, ZOOM_MAX = 10, 15       # OSM tile levels; 10 shows the whole city
DISTRICT_ZOOM = 11                # from this zoom in, the map counts as a district view
PAN_BOUNDS = (52.07, 20.80, 52.40, 21.32)   # the map centre stays in this lat/lon box: Warsaw plus a margin
TILE_MEMORY = 120                 # map tiles kept in memory at once (each about 0.25 MB)
STREETS_PER_VIEW = 60             # street lookups started per view, nearest the map centre first
AIRLY_SETTLE_MS = 2000            # Airly is asked only once the map has rested this long on a district

# District views: approximate centre (lat, lon) and zoom. These are hand-placed
# centre points, not official boundaries - adjust to taste. z12 shows roughly
# 8 x 7.5 km; the two largest districts use z11.
DISTRICTS = {
    "Bemowo":         (52.239, 20.913, 12),
    "Białołęka":      (52.320, 20.990, 11),
    "Bielany":        (52.285, 20.935, 12),
    "Mokotów":        (52.193, 21.035, 12),
    "Ochota":         (52.212, 20.972, 12),
    "Praga-Południe": (52.239, 21.085, 12),
    "Praga-Północ":   (52.262, 21.035, 12),
    "Rembertów":      (52.260, 21.165, 12),
    "Śródmieście":    (52.232, 21.015, 12),
    "Targówek":       (52.285, 21.060, 12),
    "Ursus":          (52.195, 20.885, 12),
    "Ursynów":        (52.140, 21.040, 12),
    "Wawer":          (52.200, 21.170, 11),
    "Wesoła":         (52.240, 21.225, 12),
    "Wilanów":        (52.155, 21.105, 12),
    "Włochy":         (52.185, 20.945, 12),
    "Wola":           (52.237, 20.960, 12),
    "Żoliborz":       (52.270, 20.985, 12),
}
REFRESH_MINUTES = 30              # default; GIOŚ publishes hourly values
# History on disk: one CSV per month in DATA_DIR, appended as readings arrive.
# Individual Airly sensor readings are left out by default, because Airly's API
# terms forbid building a database from its data (district averages, which
# blend all sources, are still written). Set to True only if your agreement
# with Airly allows it.
_SHARE = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "warsaw-smog"
DATA_DIR = _SHARE / "data"        # next to app/, where install.sh puts the widget
CSV_INCLUDE_AIRLY = False
CSV_FIELDS = ["recorded_at", "measured_at", "kind", "source", "id", "name", "district",
              "lat", "lon", "pm10", "pm25", "category", "category_name", "sensors"]
HISTORY_MAX = 2000                # readings kept in memory per sensor / district
HISTORY_GAP = 2 * 3600            # readings further apart than this count as an observation gap
REFRESH_CHOICES = (5, 10, 15, 30, 60)   # offered in the widget, remembered between runs
METADATA_TTL = 7 * 24 * 3600      # station / sensor lists rarely change

API = "https://api.gios.gov.pl/pjp-api/v1/rest"
# OSM tile usage policy: identify yourself, cache tiles, show attribution.
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# OSM and Sensor.Community both ask clients to identify themselves so they can
# get in touch about excessive use. Optionally put an e-mail or URL here; it is
# sent to every host the widget contacts, as part of the User-Agent header.
CONTACT = ""
USER_AGENT = f"warsaw-smog-widget/0.2 ({CONTACT or 'personal desktop widget'})"

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "warsaw-smog"
_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
STATE_FILE = _CONFIG / "warsaw-smog.json"

# Airly (optional). The city's own sensor network - about 108 units in the
# districts, mostly at schools - is Airly hardware and is only reachable through
# Airly's API, which needs a personal key and allows 100 requests per day on the
# free plan. One request returns one sensor, so readings are fetched only for
# the district you are looking at, capped and cached to stay inside that budget:
#   steady state = AIRLY_MAX_PER_VIEW * 24h / AIRLY_MAX_AGE  (8 * 24/3 = 64 / day)
# which leaves ~30 requests a day for switching districts.
AIRLY_API = "https://airapi.airly.eu/v2"
AIRLY_KEY_FILE = _CONFIG / "warsaw-smog-airly.key"   # or env AIRLY_API_KEY
AIRLY_DAILY_BUDGET = 95           # stop a little short of the hard limit of 100
AIRLY_MAX_PER_VIEW = 8            # sensors fetched per district (nearest to its centre)
AIRLY_MAX_AGE = 3 * 3600          # reuse a reading for this long before refetching
AIRLY_RADIUS_KM = 20              # installation list radius around MAP_CENTER

# Sensor.Community (formerly Luftdaten): volunteer-run sensors, open data, no
# key. One request returns every sensor in the radius with its readings from
# the last 5 minutes, so there is no per-sensor budget to manage.
SC_URL = "https://data.sensor.community/airrohr/v1/filter/area={lat},{lon},{km}"
SC_RADIUS_KM = 20
SC_MAX_AGE = 4 * 60               # reuse the area snapshot for this long (< shortest refresh)

# Community sensors come without an address, so the street is looked up from
# the coordinates with OSM's Nominatim. Its usage policy: at most 1 request per
# second, identify yourself, cache results. Each sensor is looked up once and
# kept in ~/.cache/warsaw-smog/geocode.json. Note that Sensor.Community blurs
# sensor positions on purpose, so the street is "near", not exact.
GEOCODE_URL = ("https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=17"
               "&addressdetails=1&accept-language=pl&lat={lat}&lon={lon}")
GEOCODE_INTERVAL = 1.1            # seconds between lookups

# Polish air quality index, 0 (best) .. 5 (worst). Colours follow the GIOŚ portal.
INDEX = [
    ("Very good",  (0.34, 0.69, 0.03)),
    ("Good",       (0.69, 0.87, 0.06)),
    ("Moderate",   (1.00, 0.85, 0.07)),
    ("Sufficient", (0.90, 0.51, 0.00)),
    ("Bad",        (0.90, 0.00, 0.00)),
    ("Very bad",   (0.60, 0.00, 0.00)),
]
NO_INDEX = ("No index", (0.55, 0.58, 0.62))
# Upper bounds (µg/m³, 1 h) of categories 0..4 in the Polish index; above the
# last one is category 5. Used to put Airly readings on the same colour scale.
PL_INDEX_LIMITS = {"pm10": (20, 50, 80, 110, 150), "pm25": (13, 35, 55, 75, 110)}


# ----------------------------------------------------------------------------
# 2. GIOŚ client
# ----------------------------------------------------------------------------
class RateLimited(Exception):
    pass


def http_get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(url) from e
        raise


def get_json(url: str) -> dict:
    return json.loads(http_get(url).decode("utf-8"))


def get_paged(url: str, key: str, size: int = 500) -> list:
    """Collect a list-valued key across all pages of a v1 endpoint."""
    out, page = [], 0
    while True:
        doc = get_json(f"{url}?page={page}&size={size}")
        out.extend(doc.get(key) or [])
        page += 1
        if page >= int(doc.get("totalPages") or 1):
            return out


@dataclass
class Station:
    id: int
    name: str
    lat: float
    lon: float
    street: str = ""
    sensors: dict = field(default_factory=dict)   # {"PM10": [sensor ids], "PM2.5": [...]}
    index: int | None = None                      # 0..5 or None
    index_time: str = ""                          # "YYYY-MM-DD HH:MM:SS", local time
    pm10: float | None = None
    pm25: float | None = None
    source: str = "GIOŚ"                          # or "Airly", "Community"

    @property
    def uid(self) -> str:                         # ids are only unique per source
        return f"{self.source}:{self.id}"


def parse_stations(rows: list, city: str) -> list[Station]:
    out = []
    for r in rows:
        if (r.get("Nazwa miasta") or "").strip().lower() != city.lower():
            continue
        try:
            out.append(Station(
                id=int(r["Identyfikator stacji"]),
                name=str(r.get("Nazwa stacji") or ""),
                lat=float(r["WGS84 φ N"]),
                lon=float(r["WGS84 λ E"]),
                street=str(r.get("Ulica") or ""),
            ))
        except (KeyError, TypeError, ValueError):
            continue    # malformed row: skip rather than fail the whole refresh
    return out


def parse_sensors(rows: list) -> dict:
    out: dict[str, list[int]] = {}
    for r in rows:
        code = r.get("Wskaźnik - kod")
        if code in ("PM10", "PM2.5"):
            out.setdefault(code, []).append(int(r["Identyfikator stanowiska"]))
    return out


def parse_index(doc: dict) -> tuple[int | None, str]:
    aq = doc.get("AqIndex") or {}
    v = aq.get("Wartość indeksu")
    when = aq.get("Data danych źródłowych, z których policzono wartość indeksu dla wskaźnika st") \
        or aq.get("Data wykonania obliczeń indeksu") or ""
    if isinstance(v, int) and 0 <= v < len(INDEX):
        return v, when
    return None, when


def parse_latest(doc: dict) -> float | None:
    """Most recent non-null hourly value (the newest hour is often still null)."""
    rows = [r for r in doc.get("Lista danych pomiarowych") or [] if r.get("Wartość") is not None]
    if not rows:
        return None
    return float(max(rows, key=lambda r: r.get("Data") or "")["Wartość"])


def load_metadata(city: str) -> list[Station]:
    """Station + sensor lists, cached on disk: those endpoints have tight rate limits."""
    path = CACHE_DIR / "metadata.json"
    cached = None
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    fresh = cached and cached.get("city") == city and time.time() - cached.get("ts", 0) < METADATA_TTL
    if fresh and all(s.get("sensors") is not None for s in cached["stations"]):
        return [Station(**s) for s in cached["stations"]]

    if fresh:
        stations = [Station(**{**s, "sensors": s.get("sensors") or {}}) for s in cached["stations"]]
        known = {s["id"] for s in cached["stations"] if s.get("sensors") is not None}
    else:
        try:
            stations = parse_stations(get_paged(f"{API}/station/findAll", "Lista stacji pomiarowych"), city)
        except Exception:
            if cached and cached.get("city") == city:      # stale beats nothing
                return [Station(**{**s, "sensors": s.get("sensors") or {}}) for s in cached["stations"]]
            raise
        known = set()

    # Sensor lists are fetched lazily: if we get throttled, carry on with index
    # only and pick up the rest on the next refresh.
    complete = {}
    for s in stations:
        if s.id in known:
            complete[s.id] = True
            continue
        try:
            s.sensors = parse_sensors(get_paged(
                f"{API}/station/sensors/{s.id}", "Lista stanowisk pomiarowych dla podanej stacji"))
            complete[s.id] = True
        except RateLimited:
            break
        except Exception:
            continue

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "city": city,
        "ts": cached["ts"] if fresh else time.time(),
        "stations": [
            {"id": s.id, "name": s.name, "lat": s.lat, "lon": s.lon, "street": s.street,
             "sensors": s.sensors if complete.get(s.id) else None}
            for s in stations
        ],
    }, ensure_ascii=False))
    return stations


def fetch_readings(stations: list[Station]) -> None:
    """Fill index / PM values in place. Per-station failures are tolerated."""
    for s in stations:
        try:
            s.index, s.index_time = parse_index(get_json(f"{API}/aqindex/getIndex/{s.id}"))
        except RateLimited:
            raise
        except Exception:
            s.index = None
        for code, attr in (("PM10", "pm10"), ("PM2.5", "pm25")):
            for sensor_id in s.sensors.get(code, []):
                try:
                    v = parse_latest(get_json(f"{API}/data/getData/{sensor_id}?size=24"))
                except RateLimited:
                    raise
                except Exception:
                    v = None
                if v is not None:
                    setattr(s, attr, v)
                    break


# ----------------------------------------------------------------------------
# 2b. Airly client (optional second source)
# ----------------------------------------------------------------------------
def pl_index(pm10: float | None, pm25: float | None) -> int | None:
    """Polish index category from PM values: the worse of the two pollutants."""
    cats = []
    for key, v in (("pm10", pm10), ("pm25", pm25)):
        if v is not None:
            cats.append(sum(v > limit for limit in PL_INDEX_LIMITS[key]))
    return max(cats) if cats else None


def parse_airly_installations(rows: list) -> list[dict]:
    out = []
    for r in rows:
        if r.get("airly") is False:
            continue        # third-party stations mirrored by Airly, e.g. GIOŚ itself
        try:
            a = r.get("address") or {}
            name = " ".join(x for x in (a.get("street"), a.get("number")) if x) \
                or a.get("displayAddress2") or a.get("displayAddress1") or f"Airly {r['id']}"
            out.append({"id": int(r["id"]), "name": str(name),
                        "lat": float(r["location"]["latitude"]),
                        "lon": float(r["location"]["longitude"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_airly_measurement(doc: dict, inst: dict) -> Station:
    cur = doc.get("current") or {}
    vals = {v.get("name"): v.get("value") for v in cur.get("values") or []}
    pm10, pm25 = vals.get("PM10"), vals.get("PM25")
    when = ""
    try:
        from datetime import datetime
        when = datetime.fromisoformat(cur["tillDateTime"].replace("Z", "+00:00")) \
            .astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (KeyError, AttributeError, ValueError):
        pass
    return Station(inst["id"], inst["name"], inst["lat"], inst["lon"],
                   index=pl_index(pm10, pm25), index_time=when if vals else "",
                   pm10=pm10, pm25=pm25, source="Airly")


def airly_key() -> str:
    key = os.environ.get("AIRLY_API_KEY", "").strip()
    if not key:
        try:
            key = AIRLY_KEY_FILE.read_text().strip()
        except OSError:
            pass
    return key


def in_view(lat: float, lon: float, district: str, w: float, h: float) -> bool:
    clat, clon, z = viewport(district)
    cx, cy = mercator_px(clat, clon, z)
    px, py = mercator_px(lat, lon, z)
    return abs(px - cx) <= w / 2 and abs(py - cy) <= h / 2


class Airly:
    """Airly API client that keeps itself inside the daily request budget."""

    def __init__(self, key: str):
        self.key = key
        self.lock = threading.Lock()
        self.readings: dict[int, tuple[float, Station]] = {}   # memory only, never on disk
        self.budget_file = CACHE_DIR / "airly_budget.json"

    def _budget(self) -> dict:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        try:
            b = json.loads(self.budget_file.read_text())
            if b.get("day") == today:
                return b
        except (OSError, ValueError):
            pass
        return {"day": today, "used": 0, "remaining": None}

    def left(self) -> int:
        b = self._budget()
        n = AIRLY_DAILY_BUDGET - b["used"]
        return max(0, n if b["remaining"] is None else min(n, b["remaining"]))

    def _get(self, path: str):
        if self.left() <= 0:
            raise RateLimited(path)
        b = self._budget()
        b["used"] += 1
        req = urllib.request.Request(AIRLY_API + path, headers={
            "apikey": self.key, "Accept": "application/json",
            "Accept-Language": "en", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                rem = resp.headers.get("X-RateLimit-Remaining-day")
                if rem is not None and rem.isdigit():
                    b["remaining"] = int(rem)
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                b["remaining"] = 0
                raise RateLimited(path) from e
            raise
        finally:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self.budget_file.write_text(json.dumps(b))

    def installations(self) -> list[dict]:
        path = CACHE_DIR / "airly_installations.json"
        try:
            c = json.loads(path.read_text())
            if time.time() - c["ts"] < METADATA_TTL:
                return c["items"]
        except (OSError, ValueError, KeyError):
            pass
        items = parse_airly_installations(self._get(
            f"/installations/nearest?lat={MAP_CENTER[0]}&lng={MAP_CENTER[1]}"
            f"&maxDistanceKM={AIRLY_RADIUS_KM}&maxResults=-1"))
        path.write_text(json.dumps({"ts": time.time(), "items": items}, ensure_ascii=False))
        return items

    def update(self, district: str) -> tuple[list[Station], str]:
        """Sensors for one district view, plus a one-line status for the map."""
        with self.lock:
            _, _, mw, mh = map_rect()
            clat, clon, _ = viewport(district)
            picked = sorted(
                (i for i in self.installations() if in_view(i["lat"], i["lon"], district, mw, mh)),
                key=lambda i: distance_km(clat, clon, i["lat"], i["lon"]))[:AIRLY_MAX_PER_VIEW]
            out, limited = [], False
            for inst in picked:
                ts, st = self.readings.get(inst["id"], (0.0, None))
                if time.time() - ts > AIRLY_MAX_AGE and not limited:
                    try:
                        st = parse_airly_measurement(self._get(
                            f"/measurements/installation?installationId={inst['id']}"), inst)
                        self.readings[inst["id"]] = (time.time(), st)
                    except RateLimited:
                        limited = True
                    except urllib.error.HTTPError as e:
                        if e.code in (401, 403):
                            raise
                    except Exception:
                        pass
                # no fresh reading: show a stale one (max a day old) or a grey marker
                if st is None or time.time() - self.readings.get(inst["id"], (0.0,))[0] > 86400:
                    st = Station(inst["id"], inst["name"], inst["lat"], inst["lon"], source="Airly")
                out.append(st)
            if limited:
                return out, "Airly: daily request limit reached"
            if not picked:
                return out, "Airly: no sensors in this view"
            return out, f"Airly: {len(out)} sensor{'s' * (len(out) != 1)} · {self.left()} req. left"


def airly_view(client: Airly | None, district: str, demo: bool) -> tuple[list[Station], str]:
    """What to show for Airly in the given view. Never raises."""
    if not district:
        return [], ""           # city view stays GIOŚ-only: 100+ dots, and no budget for them
    if demo:
        return demo_airly(district), "Airly: demo sensors"
    if client is None:
        return [], "Airly: no key"
    try:
        return client.update(district)
    except RateLimited:
        return [], "Airly: daily request limit reached"
    except urllib.error.HTTPError as e:
        return [], "Airly: key rejected" if e.code in (401, 403) else f"Airly: HTTP {e.code}"
    except Exception as e:
        return [], f"Airly unavailable ({type(e).__name__})"


# ----------------------------------------------------------------------------
# 2c. Sensor.Community client (optional third source, no key)
# ----------------------------------------------------------------------------
def parse_community(rows: list) -> list[Station]:
    """One Station per outdoor dust sensor, averaging its entries in the window."""
    acc: dict[int, dict] = {}
    for r in rows:
        try:
            loc = r["location"]
            if int(loc.get("indoor") or 0):
                continue
            vals = {}
            for v in r.get("sensordatavalues") or []:
                if v.get("value_type") in ("P1", "P2"):       # P1 = PM10, P2 = PM2.5
                    x = float(v["value"])
                    if 0 <= x < 1000:                         # sensor range; drops error codes
                        vals[v["value_type"]] = x
            if not vals:
                continue                                      # e.g. a temperature-only sensor
            a = acc.setdefault(int(r["sensor"]["id"]), {
                "lat": float(loc["latitude"]), "lon": float(loc["longitude"]),
                "P1": [], "P2": [], "ts": ""})
            for k, x in vals.items():
                a[k].append(x)
            a["ts"] = max(a["ts"], str(r.get("timestamp") or ""))
        except (KeyError, TypeError, ValueError):
            continue
    out = []
    for sid, a in acc.items():
        pm10, pm25 = _mean(a["P1"]), _mean(a["P2"])
        when = ""
        try:                                                  # timestamps are UTC
            from datetime import datetime, timezone
            when = datetime.strptime(a["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) \
                .astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
        out.append(Station(sid, f"Community sensor #{sid}", a["lat"], a["lon"],
                           index=pl_index(pm10, pm25), index_time=when,
                           pm10=pm10, pm25=pm25, source="Community"))
    return out


def parse_geocode(doc: dict) -> str:
    """Street (or the closest thing to one) from a Nominatim reverse result."""
    a = doc.get("address") or {}
    for k in ("road", "pedestrian", "square", "residential", "neighbourhood", "quarter", "suburb"):
        if a.get(k):
            return str(a[k])
    return ""


class Geocoder:
    """Coordinates -> street name, rate-limited and cached on disk for good."""

    def __init__(self):
        self.lock = threading.Lock()
        self.path = CACHE_DIR / "geocode.json"
        self.last = 0.0
        try:
            self.cache = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.cache = {}

    @staticmethod
    def _key(s: Station) -> str:
        return f"{s.lat:.5f},{s.lon:.5f}"

    def apply(self, stations: list[Station]) -> None:
        """Name the stations we already know; no network."""
        for s in stations:
            road = self.cache.get(self._key(s))
            if road:
                s.name = f"near {road}"

    def resolve(self, stations: list[Station], on_progress=lambda text: None,
                wanted=lambda: True) -> None:
        """Preload the street of every community sensor given, one lookup per
        GEOCODE_INTERVAL. Call from a worker thread; on_progress gets a short
        status text ("" when everything is loaded). Stops if wanted() turns false."""
        todo = [s for s in stations if s.source == "Community"]
        for n, s in enumerate(todo, 1):
            if not wanted():
                return
            with self.lock:
                key = self._key(s)
                fetched = key not in self.cache
                if fetched:
                    time.sleep(max(0.0, self.last + GEOCODE_INTERVAL - time.time()))
                    self.last = time.time()
                    try:
                        lat, lon = key.split(",")
                        road = parse_geocode(get_json(GEOCODE_URL.format(lat=lat, lon=lon)))
                    except Exception as e:      # offline, blocked or throttled: say so,
                        on_progress(f"street lookup failed ({type(e).__name__})")
                        return                  # and try again on the next refresh
                    self.cache[key] = road      # "" is cached too: nothing to find there
                    try:
                        CACHE_DIR.mkdir(parents=True, exist_ok=True)
                        self.path.write_text(json.dumps(self.cache, ensure_ascii=False))
                    except OSError:
                        pass                    # still held in memory for this session
            if self.cache.get(key):
                s.name = f"near {self.cache[key]}"
            if fetched:
                on_progress(f"streets {n}/{len(todo)}")
        on_progress("")


class Community:
    def __init__(self):
        self.lock = threading.Lock()
        self.ts, self.sensors = 0.0, []

    def all(self) -> list[Station]:
        with self.lock:
            if time.time() - self.ts > SC_MAX_AGE:
                url = SC_URL.format(lat=MAP_CENTER[0], lon=MAP_CENTER[1], km=SC_RADIUS_KM)
                self.sensors = parse_community(get_json(url))
                self.ts = time.time()
            return self.sensors


def community_view(client: "Community | None", district: str, demo: bool,
                   geocoder: "Geocoder | None" = None) -> tuple[list[Station], str]:
    """Community sensors inside the given district view. Never raises."""
    if not district:
        return [], ""           # too dense for the city view
    _, _, mw, mh = map_rect()
    try:
        sensors = demo_community(district) if demo else client.all()
    except Exception as e:
        return [], f"Community unavailable ({type(e).__name__})"
    shown = [s for s in sensors if in_view(s.lat, s.lon, district, mw, mh)]
    if geocoder and not demo:
        geocoder.apply(shown)   # cached streets only; new ones are resolved afterwards
    return shown, f"Community: {len(shown)}"


def all_community_sensors(client: "Community | None", district: str, demo: bool,
                          geocoder: "Geocoder | None" = None) -> list[Station]:
    """Every outdoor community sensor in the city, for drawing wherever the map
    is moved to (community_view gives just the open district's). Never raises."""
    try:
        sensors = (demo_community(district) if district else []) if demo else client.all()
    except Exception:
        return []
    if geocoder and not demo:
        geocoder.apply(sensors)     # streets already known
    return sensors


def demo_community(district: str) -> list[Station]:
    """Fake sensors scattered round a district centre. Not real locations."""
    clat, clon, _ = viewport(district)
    pts = [(0.020, -0.034, 25, 15), (0.007, -0.006, 55, 36), (-0.003, -0.030, 41, 30),
           (-0.020, 0.012, 88, 57), (0.014, 0.020, 33, 14), (-0.011, 0.034, 70, 49),
           (-0.024, -0.026, 120, 82)]
    return [Station(1000 + i, f"near Demo street {i + 1}", clat + dy, clon + dx,
                    index=pl_index(p10, p25), index_time="2026-11-10 14:03:00",
                    pm10=p10, pm25=p25, source="Community")
            for i, (dy, dx, p10, p25) in enumerate(pts)]


def demo_airly(district: str) -> list[Station]:
    """Fake sensors scattered round a district centre. Not real locations."""
    clat, clon, _ = viewport(district)
    pts = [(0.012, -0.020, 30, 19), (-0.008, 0.015, 62, 41), (0.004, 0.028, 18, 9),
           (-0.016, -0.012, 95, 60), (0.019, 0.006, None, None)]
    return [Station(i, f"demo sensor {i + 1}", clat + dy, clon + dx, index=pl_index(p10, p25),
                    index_time="2026-11-10 14:00:00" if p10 else "", pm10=p10, pm25=p25,
                    source="Airly")
            for i, (dy, dx, p10, p25) in enumerate(pts)]


def demo_stations() -> list[Station]:
    """Plausible fake data for --demo / layout work. Not real measurements."""
    rows = [
        ("Warszawa, ul. Kondratowicza", 52.2909, 21.0425, 1, 21, 12),
        ("Warszawa, al. Niepodległości", 52.2193, 21.0047, 3, 78, 51),
        ("Warszawa, ul. Marszałkowska", 52.2251, 21.0147, 2, 49, 30),
        ("Warszawa, ul. Wokalna", 52.1608, 21.0339, 2, 44, 27),
        ("Warszawa, ul. Chrościckiego", 52.2078, 20.9063, 4, 112, 80),
        ("Warszawa, ul. Tołstoja", 52.2853, 20.9336, 0, 12, 7),
        ("Warszawa, ul. Bajkowa", 52.1883, 21.1763, None, None, None),
    ]
    return [Station(i, n, la, lo, "", {}, ix, "2026-11-10 14:00:00", p10, p25)
            for i, (n, la, lo, ix, p10, p25) in enumerate(rows)]


# ----------------------------------------------------------------------------
# 3. map tiles
# ----------------------------------------------------------------------------
def mercator_px(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """WGS84 -> global Web Mercator pixel coordinates (256 px tiles)."""
    n = 256.0 * (1 << zoom)
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


_PL = "aąbcćdeęfghijklłmnńoóprsśtuwyzźż"
PL_SORT_KEY = lambda name: [_PL.find(c) for c in name.lower()]


def viewport(district: str) -> tuple[float, float, int]:
    """(lat, lon, zoom) for a district name, or the whole city for ""."""
    return DISTRICTS.get(district) or (*MAP_CENTER, MAP_ZOOM)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular approximation; error is negligible at city scale."""
    dx = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    dy = math.radians(lat2 - lat1)
    return 6371.0 * math.hypot(dx, dy)


def unproject(x: float, y: float, zoom: int) -> tuple[float, float]:
    """Global Web Mercator pixel coordinates -> (lat, lon). Inverse of mercator_px."""
    n = 256.0 * (1 << zoom)
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))), x / n * 360.0 - 180.0


def clamp_view(lat: float, lon: float, zoom: int) -> tuple[float, float, int]:
    """Keep a map view inside the allowed zoom range and the box round Warsaw."""
    return (min(max(lat, PAN_BOUNDS[0]), PAN_BOUNDS[2]), min(max(lon, PAN_BOUNDS[1]), PAN_BOUNDS[3]),
            min(max(int(zoom), ZOOM_MIN), ZOOM_MAX))


def pan_by(center: tuple, zoom: int, dx: float, dy: float) -> tuple[float, float]:
    """New map centre after the map is dragged by (dx, dy) pixels."""
    cx, cy = mercator_px(*center, zoom)
    return unproject(cx - dx, cy - dy, zoom)


def zoom_about(center: tuple, zoom: int, new_zoom: int, dx: float, dy: float) -> tuple[float, float]:
    """New map centre when zooming so that the point (dx, dy) pixels from the
    middle of the map stays where it is."""
    cx, cy = mercator_px(*center, zoom)
    px, py = mercator_px(*unproject(cx + dx, cy + dy, zoom), new_zoom)
    return unproject(px - dx, py - dy, new_zoom)


def in_viewport(lat: float, lon: float, center: tuple, zoom: int, w: float, h: float) -> bool:
    """Is the point inside a w x h px map centred on `center` at `zoom`?"""
    cx, cy = mercator_px(*center, zoom)
    px, py = mercator_px(lat, lon, zoom)
    return abs(px - cx) <= w / 2 and abs(py - cy) <= h / 2


_SHAPES: dict = {}
SHAPE_SLACK_KM = 0.1      # the outlines are simplified to ~40 m, so allow for thin gaps between neighbours


def district_shapes() -> dict:
    """{name: ((lon0, lat0, lon1, lat1), [(lon, lat), ...])}, decoded once from DISTRICT_SHAPES."""
    if not _SHAPES:
        for name, text in DISTRICT_SHAPES.items():
            ring = [(20 + int(a) / 1e4, 52 + int(b) / 1e4) for a, b in (pt.split(",") for pt in text.split())]
            xs, ys = [pt[0] for pt in ring], [pt[1] for pt in ring]
            _SHAPES[name] = ((min(xs), min(ys), max(xs), max(ys)), ring)
    return _SHAPES


def _in_ring(lon: float, lat: float, ring: list) -> bool:
    """Ray-casting point-in-polygon test."""
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        (xi, yi), (xj, yj) = ring[i], ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _edge_km(lat: float, lon: float, ring: list) -> float:
    """Distance from a point to the nearest edge of a polygon."""
    kx, ky = 111.32 * math.cos(math.radians(lat)), 110.57      # km per degree here
    best = float("inf")
    for i in range(len(ring)):
        ax, ay = (ring[i - 1][0] - lon) * kx, (ring[i - 1][1] - lat) * ky
        bx, by = (ring[i][0] - lon) * kx, (ring[i][1] - lat) * ky
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else min(1.0, max(0.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def district_at(lat: float, lon: float) -> str:
    """Name of the district the point is in, or "" if it is outside Warsaw."""
    shapes = district_shapes()
    for name, (b, ring) in shapes.items():
        if b[0] <= lon <= b[2] and b[1] <= lat <= b[3] and _in_ring(lon, lat, ring):
            return name
    best, best_d = "", SHAPE_SLACK_KM       # in no outline: on a seam between two, or outside
    for name, (b, ring) in shapes.items():
        if b[0] - 0.003 <= lon <= b[2] + 0.003 and b[1] - 0.002 <= lat <= b[3] + 0.002:
            d = _edge_km(lat, lon, ring)
            if d < best_d:
                best, best_d = name, d
    return best


def locate(lat: float, lon: float, zoom: int) -> tuple[str, bool]:
    """(district, outside) for a map view: which district its centre is in.

    Zoomed out to the whole city there is no district: ("", False). Zoomed in
    over Warsaw: (name, False). Zoomed in beyond the city boundary: ("", True).
    """
    if zoom < DISTRICT_ZOOM:
        return "", False
    name = district_at(lat, lon)
    return name, not name


def tiles_for_view(w: float, h: float, lat: float, lon: float, z: int) -> list[tuple[int, int, int]]:
    """(z, x, y) of every tile intersecting a w x h px map centred on (lat, lon)."""
    cx, cy = mercator_px(lat, lon, z)
    x0, x1 = int((cx - w / 2) // 256), int((cx + w / 2) // 256)
    y0, y1 = int((cy - h / 2) // 256), int((cy + h / 2) // 256)
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def tile_path(z: int, x: int, y: int) -> Path:
    return CACHE_DIR / "tiles" / str(z) / str(x) / f"{y}.png"


def ensure_tiles(w: float, h: float, lat: float, lon: float, zoom: int) -> None:
    """Download missing tiles once; they are kept on disk indefinitely."""
    for z, x, y in tiles_for_view(w, h, lat, lon, zoom):
        p = tile_path(z, x, y)
        if p.exists():
            continue
        try:
            data = http_get(TILE_URL.format(z=z, x=x, y=y))
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(p)
        except Exception:
            pass    # map is decoration; the widget works without it


# ----------------------------------------------------------------------------
# 4. renderer
# ----------------------------------------------------------------------------
@dataclass
class View:
    stations: list[Station] = field(default_factory=list)
    updated: float = 0.0
    error: str = ""
    loading: bool = True
    hover: str | None = None          # Station.uid under the pointer
    airly: list[Station] = field(default_factory=list)   # sensors for the current district
    airly_status: str = ""
    community: list[Station] = field(default_factory=list)
    community_status: str = ""
    street_status: str = ""           # progress of the street preload
    demo: bool = False
    district: str = ""                # a key of DISTRICTS; "" = whole city, or outside Warsaw
    outside: bool = False             # zoomed in, but the middle of the map is outside Warsaw
    center: tuple | None = None       # (lat, lon) in the middle of the map; None = the district's standard view
    zoom: int | None = None
    dragging: bool = False            # the map is being dragged right now
    gen: int = 0                      # goes up with every map move, so background work for an old view can stop
    community_all: list = field(default_factory=list)    # city-wide community sensors, for drawing
    airly_for: str | None = None      # the district view.airly was fetched for
    hits: list = field(default_factory=list)   # [(x, y, station)] from last draw
    chip: tuple = (0.0, 0.0, 0.0, 0.0)         # district selector rect from last draw
    chip_hover: bool = False
    refresh_minutes: int = REFRESH_MINUTES
    refreshing: bool = False
    btn_refresh: tuple = (0.0, 0.0, 0.0, 0.0)  # "Refresh" button rect from last draw
    rate_chip: tuple = (0.0, 0.0, 0.0, 0.0)    # auto-refresh selector rect from last draw
    gaps: list = field(default_factory=list)   # [(x0, y0, x1, y1, t_from, t_to)] from last draw
    gap_hover: tuple | None = None             # (t_from, t_to) of the gap under the pointer
    hot: str = ""                              # "refresh" / "rate" while the pointer is on one
    labels: bool = False                       # draw a name next to every marker
    toggle: tuple = (0.0, 0.0, 0.0, 0.0)       # "labels" checkbox rect from last draw
    log: "HistoryLog | None" = None                # CSV log; None = not saving (demo, snapshot)
    history: dict = field(default_factory=dict)    # key -> [(time, pm10, pm25, mark)]: today + this run
    tiles: dict = field(default_factory=dict)  # {(z, x, y): cairo surface}

    def __post_init__(self):
        lat, lon, zoom = viewport(self.district)
        if self.center is None:
            self.center = (lat, lon)
        if self.zoom is None:
            self.zoom = zoom


def district_sensors(view: "View") -> list[Station]:
    """The sensors behind the open district's figures: everything inside the
    district's standard map rectangle, wherever the map itself has been moved."""
    if not view.district:
        return []
    _, _, mw, mh = map_rect()
    return [s for s in view.stations + view.community + view.airly
            if in_view(s.lat, s.lon, view.district, mw, mh)]


def drawn_sensors(view: "View") -> list[Station]:
    """Low-cost sensors to draw (zoomed-in views only): community sensors from
    the whole city, so they show wherever the map is, plus the open district's
    Airly sensors."""
    if view.zoom < DISTRICT_ZOOM:
        return []
    seen: dict = {}
    for s in view.community_all + view.community + view.airly:
        seen.setdefault(s.uid, s)
    return list(seen.values())


def map_rect() -> tuple[float, float, float, float]:
    return 12.0, 62.0, WIDTH - 24.0, HEIGHT - 62.0 - 214.0


def _rounded(ctx, x, y, w, h, r):
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    ctx.close_path()


def _text(ctx, s, x, y, size=11.0, rgb=(0.92, 0.94, 0.96), bold=False, right=False, maxw=None):
    import cairo
    ctx.select_font_face("Ubuntu", cairo.FONT_SLANT_NORMAL,
                         cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(size)
    if maxw is not None:
        while len(s) > 4 and ctx.text_extents(s).x_advance > maxw:
            s = s[:-2].rstrip() + "…"
    if right:
        x -= ctx.text_extents(s).x_advance
    ctx.set_source_rgb(*rgb)
    ctx.move_to(x, y)
    ctx.show_text(s)


def short_label(s: Station) -> str:
    """Compact marker label: the street, or the sensor number if none is known."""
    if s.source == "Community" and s.name.startswith("Community sensor"):
        return f"#{s.id}"
    n = s.name.split(",", 1)[-1].strip()
    for prefix in ("near ", "ul. ", "ulica "):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n


def _overlap(a, b) -> float:
    """Overlapping area of two (x, y, w, h) boxes."""
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def place_labels(markers: list, bounds: tuple, fixed: list = ()) -> list:
    """Choose a box for every label; none is ever dropped.

    markers: [(x, y, label_width)] in priority order. For each, eight positions
    round the marker are tried and the one covering the least of what is
    already there (earlier labels, other markers, fixed UI boxes, the map edge)
    wins. In a tight cluster that can still mean some overlap, which is better
    than a missing label.
    """
    H, G = 13.0, 10.0                       # label height, gap from marker centre
    bx, by, bw, bh = bounds
    dots = [(x - 8, y - 8, 16, 16) for x, y, _ in markers]
    placed = []
    for i, (x, y, w) in enumerate(markers):
        candidates = [
            (x + G, y - H / 2),             # right (preferred)
            (x - G - w, y - H / 2),         # left
            (x - w / 2, y - G - H),         # above
            (x - w / 2, y + G),             # below
            (x + G - 3, y - G - H + 3),     # upper right
            (x + G - 3, y + G - 3),         # lower right
            (x - G - w + 3, y - G - H + 3), # upper left
            (x - G - w + 3, y + G - 3),     # lower left
        ]
        best, best_cost = None, None
        for rank, (cx, cy) in enumerate(candidates):
            box = (cx, cy, w, H)
            outside = w * H - _overlap(box, (bx + 2, by + 2, bw - 4, bh - 4))
            cost = (4 * sum(_overlap(box, b) for b in placed)
                    + 4 * sum(_overlap(box, b) for b in fixed)
                    + 2 * sum(_overlap(box, d) for k, d in enumerate(dots) if k != i)
                    + 6 * outside + 0.01 * rank)
            if best_cost is None or cost < best_cost:
                best, best_cost = box, cost
        placed.append(best)
    return placed


SOURCE_RANK = {"Community": 0, "Airly": 1, "GIOŚ": 2}     # draw order, reference on top


def _marker(ctx, source: str, x: float, y: float, r: float) -> None:
    """Path for a station marker: circle GIOŚ, diamond Airly, triangle community."""
    if source == "GIOŚ":
        ctx.new_sub_path()
        ctx.arc(x, y, r, 0, 2 * math.pi)
    elif source == "Airly":
        ctx.move_to(x, y - r)
        ctx.line_to(x + r, y)
        ctx.line_to(x, y + r)
        ctx.line_to(x - r, y)
    else:
        ctx.move_to(x, y - r)
        ctx.line_to(x + r, y + r * 0.8)
        ctx.line_to(x - r, y + r * 0.8)
    ctx.close_path()


def scale_position(pm10: float | None, pm25: float | None, index: int | None = None) -> float | None:
    """Where a reading sits on the six-segment colour scale: 0.0 .. 6.0.

    The integer part is the category, the fraction how far through that
    category's concentration range the value is; the worse of PM10 / PM2.5
    decides, as for the index. The open-ended last category is given the same
    width as the one before it. If an index is supplied and disagrees with the
    PM values (a GIOŚ index can be driven by NO2 or O3, or the station may have
    no PM sensor), the dot goes to the middle of that index's segment.
    """
    best = None
    for key, v in (("pm10", pm10), ("pm25", pm25)):
        if v is None:
            continue
        limits = (0,) + PL_INDEX_LIMITS[key]
        cat = sum(v > limit for limit in limits[1:])
        lo = limits[cat]
        hi = limits[cat + 1] if cat + 1 < len(limits) else lo + (lo - limits[cat - 1])
        pos = cat + min(max((v - lo) / (hi - lo), 0.0), 1.0)
        best = pos if best is None else max(best, pos)
    if index is not None and (best is None or min(int(best), len(INDEX) - 1) != index):
        if best is None or not (index <= best <= index + 1):
            return index + 0.5
    return best


def _iso(ts) -> str:
    """Local ISO 8601 time with UTC offset, from epoch seconds or 'YYYY-MM-DD HH:MM:SS'."""
    from datetime import datetime
    try:
        d = datetime.fromtimestamp(ts) if isinstance(ts, (int, float)) \
            else datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return d.astimezone().isoformat(timespec="seconds")
    except (ValueError, TypeError, OSError):
        return ""


def read_today(path: Path, today: str, block: int = 65536) -> list[dict]:
    """Today's rows of a history CSV, oldest first, without reading the whole file.

    Rows are appended in time order, so today's are a block at the end: the
    file is read backwards from its end, a chunk at a time, and reading stops
    at the first row recorded on another date. `today` is 'YYYY-MM-DD'.
    Damaged lines (e.g. a write cut short by a crash) are skipped.
    """
    rows = []
    try:
        with open(path, "rb") as f:
            fields = next(csv.reader([f.readline().decode("utf-8", "replace")]), [])
            start = f.tell()                    # first data byte
            pos = f.seek(0, os.SEEK_END)
            tail = b""
            while pos > start:
                step = min(block, pos - start)
                pos -= step
                f.seek(pos)
                lines = (f.read(step) + tail).split(b"\n")
                # unless we are at the start, the first piece may be half a line
                tail = lines.pop(0) if pos > start else b""
                for raw in reversed(lines):
                    text = raw.decode("utf-8", "replace").strip("\r")
                    if not text:
                        continue
                    values = next(csv.reader([text]), [])
                    if len(values) != len(fields):
                        continue
                    row = dict(zip(fields, values))
                    if row.get("recorded_at", "")[:10] != today:
                        return rows[::-1]
                    rows.append(row)
    except (OSError, csv.Error):
        pass
    return rows[::-1]


def _mark(measured: str, pm10, pm25) -> tuple:
    """Identity of a reading, in the precision the CSV keeps, so that a reading
    restored from the file and the same one fetched live compare equal."""
    r = lambda v: None if v is None else round(float(v), 1)
    return (measured, r(pm10), r(pm25))


def restore_history(view: "View", rows: list[dict]) -> int:
    """Put rows from the CSV (see read_today) back into the on-screen history."""
    from datetime import datetime
    n = 0
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["recorded_at"]).timestamp()
            pm10 = float(r["pm10"]) if r.get("pm10") else None
            pm25 = float(r["pm25"]) if r.get("pm25") else None
            kind = r["kind"]
            key = {"sensor": f"{r['source']}:{r['id']}", "district": "district:" + r["name"],
                   "city": "city"}[kind]
        except (KeyError, ValueError, TypeError):
            continue
        if pm10 is None and pm25 is None:
            continue
        series = view.history.setdefault(key, [])
        series.append((ts, pm10, pm25, _mark(r.get("measured_at", ""), pm10, pm25)))
        del series[:-HISTORY_MAX]
        n += 1
    return n


class HistoryLog:
    """Appends readings to DATA_DIR/history-YYYY-MM.csv."""

    def __init__(self, directory: Path | None = None, now: float | None = None):
        self.dir = Path(directory) if directory else DATA_DIR
        if not directory:
            self._adopt_old_files()
        self.enabled = True
        self.error = ""
        now = time.time() if now is None else now
        # Today's rows: shown again in the widget (restore_history), and used so
        # that a restart does not log the same measurement twice.
        self.today_rows = read_today(self.path(now), time.strftime("%Y-%m-%d", time.localtime(now)))
        self.last: dict[tuple, str] = {     # (source, id) -> measured_at already on disk
            (r.get("source"), r.get("id")): r["measured_at"]
            for r in self.today_rows if r.get("kind") == "sensor" and r.get("measured_at")}

    def _adopt_old_files(self) -> None:
        """Earlier versions wrote the CSVs one level up; move them into data/."""
        try:
            for old in _SHARE.glob("history-*.csv"):
                new = self.dir / old.name
                if not new.exists():
                    self.dir.mkdir(parents=True, exist_ok=True)
                    old.rename(new)
        except OSError:
            pass                # leave them where they are; nothing is lost

    def path(self, now: float) -> Path:
        return self.dir / f"history-{time.strftime('%Y-%m', time.localtime(now))}.csv"

    def write(self, rows: list[dict], now: float) -> None:
        if not self.enabled:
            return
        keep = []
        for r in rows:
            if r["kind"] == "sensor":
                if r["source"] == "Airly" and not CSV_INCLUDE_AIRLY:
                    continue
                key = (r["source"], str(r["id"]))
                if r["measured_at"] and self.last.get(key) == r["measured_at"]:
                    continue
                self.last[key] = r["measured_at"]
            keep.append(r)
        if not keep:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.path(now)
            new = not path.exists() or path.stat().st_size == 0
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                if new:
                    w.writeheader()
                w.writerows(keep)
            self.error = ""
        except OSError as e:
            self.error = f"history not saved ({e.strerror or type(e).__name__})"


def record_history(view: "View", now: float | None = None) -> None:
    """Add the current readings to the on-screen history, and to the CSV log if on.

    One series per sensor on screen, one for the open district's average and
    one for the city mean. A reading is added only when it differs from the
    last one recorded, so re-fetching unchanged data does not skew the stats.
    """
    now = time.time() if now is None else now
    rows = []

    def rec(key, pm10, pm25, measured, save=True, **row):
        if pm10 is None and pm25 is None:
            return
        measured = _iso(measured)
        mark = _mark(measured, pm10, pm25)
        series = view.history.setdefault(key, [])
        if series and series[-1][3] == mark:
            return
        series.append((now, pm10, pm25, mark))
        del series[:-HISTORY_MAX]
        if not save:
            return
        cat = row.pop("category", None)
        if cat is None:
            cat = pl_index(pm10, pm25)
        rows.append({
            "recorded_at": _iso(now), "measured_at": measured, "district": view.district,
            "pm10": "" if pm10 is None else round(float(pm10), 1),
            "pm25": "" if pm25 is None else round(float(pm25), 1),
            "category": "" if cat is None else cat,
            "category_name": "" if cat is None else INDEX[cat][0],
            **row})

    visible = view.stations + (view.community + view.airly if view.district else [])
    for s in visible:
        name = s.name.split(",", 1)[-1].strip() if s.source == "GIOŚ" else s.name
        rec(s.uid, s.pm10, s.pm25, s.index_time,
            kind="sensor", source=s.source, id=s.id, name=" ".join(name.split()),
            lat=s.lat, lon=s.lon, category=s.index, sensors="")
    # Sensors that are only on screen because the map was moved or zoomed out
    # beyond the district's standard view: stats while the widget runs, not saved.
    if view.zoom >= DISTRICT_ZOOM:
        _, _, mw, mh = map_rect()
        known = {s.uid for s in visible}
        for s in view.community_all:
            if s.uid not in known and in_viewport(s.lat, s.lon, view.center, view.zoom, mw, mh):
                rec(s.uid, s.pm10, s.pm25, s.index_time, save=False)
    if view.district:
        clat, clon, _ = viewport(view.district)
        _, pm10, pm25, last, n = summarise(district_sensors(view))
        rec("district:" + view.district, pm10, pm25, last,
            kind="district", source="average", id="", name=view.district,
            lat=clat, lon=clon, sensors=n)
    pm10, pm25 = _mean(s.pm10 for s in view.stations), _mean(s.pm25 for s in view.stations)
    rec("city", pm10, pm25, max((s.index_time for s in view.stations), default=""),
        kind="city", source="GIOŚ average", id="", name=CITY,
        lat=MAP_CENTER[0], lon=MAP_CENTER[1],
        sensors=sum(s.pm10 is not None or s.pm25 is not None for s in view.stations))
    if view.log is not None and rows:
        view.log.write(rows, now)


def history_stats(series: list) -> dict:
    """{"n", "since", "pm10": (min, mean, max) | None, "pm25": ...} for one series."""
    out = {"n": len(series), "since": series[0][0] if series else None}
    for key, col in (("pm10", 1), ("pm25", 2)):
        vals = [r[col] for r in series if r[col] is not None]
        out[key] = (min(vals), sum(vals) / len(vals), max(vals)) if vals else None
    return out


def gap_text(t_from: float, t_to: float) -> str:
    """Hover description of an observation gap in the trend line."""
    mins = int(round((t_to - t_from) / 60))
    length = f"{mins // 60} h {mins % 60:02d} min" if mins >= 60 else f"{mins} min"
    day = "%H:%M" if time.localtime(t_from)[:3] == time.localtime(t_to)[:3] else "%d %b %H:%M"
    return (f"observation gap · {time.strftime(day, time.localtime(t_from))} – "
            f"{time.strftime(day, time.localtime(t_to))} · no readings for {length}")


def rate_label(minutes: int) -> str:
    return "every hour" if minutes == 60 else f"every {minutes} min"


def _chip(ctx, x: float, y: float, label: str, hot: bool, arrow: bool = False) -> tuple:
    """Draw a small pill button; returns its (x, y, w, h) for hit-testing."""
    import cairo
    ctx.select_font_face("Ubuntu", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(10)
    w = ctx.text_extents(label).x_advance + 18 + (12 if arrow else 0)
    rect = (x, y, w, 20.0)
    _rounded(ctx, *rect, 10)
    ctx.set_source_rgba(1, 1, 1, 0.20 if hot else 0.10)
    ctx.fill_preserve()
    ctx.set_line_width(1)
    ctx.set_source_rgba(1, 1, 1, 0.35)
    ctx.stroke()
    _text(ctx, label, x + 9, y + 13.5, 10)
    if arrow:
        ax = x + w - 12
        ctx.move_to(ax - 3.5, y + 8)
        ctx.line_to(ax + 3.5, y + 8)
        ctx.line_to(ax, y + 12.5)
        ctx.close_path()
        ctx.set_source_rgb(0.92, 0.94, 0.96)
        ctx.fill()
    return rect


def summarise(stations: list[Station]):
    """Average of a set of sensors: (index, mean PM10, mean PM2.5, last time, n).

    Every sensor with a reading counts equally, whatever its source. The index
    is the Polish category of the mean concentrations, not a mean of categories.
    """
    live = [s for s in stations if s.pm10 is not None or s.pm25 is not None]
    pm10, pm25 = _mean(s.pm10 for s in live), _mean(s.pm25 for s in live)
    last = max((s.index_time for s in live if s.index_time), default="")
    return pl_index(pm10, pm25), pm10, pm25, last, len(live)


def _fmt(v):
    return "–" if v is None else f"{v:.0f}"


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def render(ctx, view: View) -> None:
    import cairo
    W, H = WIDTH, HEIGHT
    dim = (0.62, 0.66, 0.71)

    # panel
    ctx.set_operator(cairo.OPERATOR_SOURCE)
    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)
    _rounded(ctx, 0, 0, W, H, 14)
    ctx.set_source_rgba(0.07, 0.09, 0.11, 0.90)
    ctx.fill()

    # header: city-wide verdict = worst station (a mean would hide hot spots)
    ranked = [s for s in view.stations if s.index is not None]
    worst = max(ranked, key=lambda s: s.index) if ranked else None
    label, colour = INDEX[worst.index] if worst else NO_INDEX
    ctx.arc(26, 31, 9, 0, 2 * math.pi)
    ctx.set_source_rgb(*colour)
    ctx.fill()
    # district selector: a clickable "chip" at the top right (left-click opens the list)
    chip_label = view.district or ("Outside Warsaw" if view.outside else "Whole city")
    ctx.select_font_face("Ubuntu", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(11)
    cw = ctx.text_extents(chip_label).x_advance + 36
    view.chip = (W - 14 - cw, 12, cw, 24)
    _rounded(ctx, *view.chip, 12)
    ctx.set_source_rgba(1, 1, 1, 0.20 if view.chip_hover else 0.10)
    ctx.fill_preserve()
    ctx.set_line_width(1)
    ctx.set_source_rgba(1, 1, 1, 0.35)
    ctx.stroke()
    _text(ctx, chip_label, view.chip[0] + 10, 28, 11)
    ax = W - 14 - 18                                  # arrow drawn, not a font glyph
    ctx.move_to(ax - 4, 22)
    ctx.line_to(ax + 4, 22)
    ctx.line_to(ax, 27)
    ctx.close_path()
    ctx.set_source_rgb(0.92, 0.94, 0.96)
    ctx.fill()
    _text(ctx, "Warsaw air" + (" (demo)" if view.demo else ""), 44, 27, 13, bold=True,
          maxw=view.chip[0] - 52)
    if worst:   # always the city-wide verdict, also in a district view
        sub = f"City worst: {label} · {len(ranked)}/{len(view.stations)} stations reporting"
    elif view.loading:
        sub = "Loading…"
    else:
        sub = "No index available"
    _text(ctx, sub, 44, 44, 10.5, dim, maxw=W - 56)

    # map
    mx, my, mw, mh = map_rect()
    ctx.save()
    _rounded(ctx, mx, my, mw, mh, 10)
    ctx.clip()
    ctx.set_source_rgb(0.12, 0.15, 0.18)
    ctx.paint()
    zoom = view.zoom
    cx, cy = mercator_px(*view.center, zoom)
    ox, oy = mx + mw / 2 - cx, my + mh / 2 - cy       # global px -> widget px
    for (tz, tx, ty), surf in view.tiles.items():
        if tz != zoom:
            continue
        ctx.set_source_surface(surf, round(ox + tx * 256), round(oy + ty * 256))
        ctx.paint_with_alpha(0.55)                    # dim the basemap so dots carry
    view.hits = []
    visible = view.stations + drawn_sensors(view)
    # reference stations on top of low-cost sensors; within each source, worst last
    for s in sorted(visible, key=lambda s: (SOURCE_RANK[s.source], -1 if s.index is None else s.index)):
        px, py = mercator_px(s.lat, s.lon, zoom)
        x, y = ox + px, oy + py
        if not (mx <= x <= mx + mw and my <= y <= my + mh):
            continue
        view.hits.append((x, y, s))
        hovered = s.uid == view.hover
        r = {"GIOŚ": 7.5, "Airly": 7.0, "Community": 6.0}[s.source] + (2.0 if hovered else 0.0)
        _marker(ctx, s.source, x, y, r)
        ctx.set_source_rgb(*(INDEX[s.index][1] if s.index is not None else NO_INDEX[1]))
        ctx.fill_preserve()
        ctx.set_line_width(2.2 if hovered else 1.4)
        ctx.set_source_rgba(1, 1, 1, 0.95 if hovered else 0.75)
        ctx.stroke()
    # always-on labels (the "labels" checkbox): every marker gets one
    # (left out while the map is being dragged, to keep the dragging smooth)
    if view.labels and not view.dragging:
        ctx.select_font_face("Ubuntu", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        ctx.set_font_size(8.5)
        items = []
        for x, y, s in sorted(view.hits, key=lambda h: -SOURCE_RANK[h[2].source]):
            t = short_label(s)
            while len(t) > 4 and ctx.text_extents(t).x_advance > 110:
                t = t[:-2].rstrip() + "…"
            if t:
                items.append((x, y, ctx.text_extents(t).x_advance + 6, t))
        for (x, y, w, t), box in zip(items, place_labels(
                [(x, y, w) for x, y, w, _ in items], (mx, my, mw, mh))):
            _rounded(ctx, *box, 4)
            ctx.set_source_rgba(0.07, 0.09, 0.11, 0.78)
            ctx.fill()
            _text(ctx, t, box[0] + 3, box[1] + 10, 8.5, (0.88, 0.90, 0.93))
    ctx.restore()

    # Two control rows under the map (nothing is drawn over the map itself).
    # Row 1: what the marker shapes mean, and the "labels" checkbox.
    ry = my + mh + 6
    used = {s.source for _, _, s in view.hits} | {s.source for s in district_sensors(view)}
    shown = ["GIOŚ"] + [src for src in ("Airly", "Community") if src in used]
    kx = 14.0
    for src in shown:
        _marker(ctx, src, kx + 4, ry + 8.5, 4.0)
        ctx.set_source_rgb(*dim)
        ctx.fill()
        _text(ctx, src.lower() if src == "Community" else src, kx + 12, ry + 12, 9, dim)
        kx += 28 + ctx.text_extents(src).x_advance
    bx, by = W - 14 - 44, ry + 3
    view.toggle = (bx - 4, ry - 1, 52, 18)            # click target, a little generous
    ctx.rectangle(bx + 0.5, by + 0.5, 9, 9)
    ctx.set_line_width(1)
    ctx.set_source_rgba(1, 1, 1, 0.75)
    ctx.stroke()
    if view.labels:
        ctx.move_to(bx + 2, by + 5)
        ctx.line_to(bx + 4.2, by + 7.5)
        ctx.line_to(bx + 8, by + 2.5)
        ctx.set_line_width(1.6)
        ctx.set_source_rgb(0.92, 0.94, 0.96)
        ctx.stroke()
    _text(ctx, "labels", bx + 15, by + 8.5, 9, (0.80, 0.83, 0.87))

    # Row 2: refresh now, auto-refresh rate, and when the data was last fetched.
    r2 = ry + 23
    view.btn_refresh = _chip(ctx, 14, r2, "Refresh", view.hot == "refresh")
    view.rate_chip = _chip(ctx, 14 + view.btn_refresh[2] + 6, r2, rate_label(view.refresh_minutes),
                           view.hot == "rate", arrow=True)
    if view.refreshing:
        when = "refreshing…"
    elif view.updated:
        times = sorted(s.index_time for s in view.stations if s.index_time)
        # the GIOŚ time is the hour its newest published value belongs to; the
        # other sources have their own, newer, times (shown in the detail line)
        when = (f"GIOŚ {times[-1][11:16]} · " if times else "") \
            + "checked " + time.strftime("%H:%M", time.localtime(view.updated))
    else:
        when = ""
    _text(ctx, when, W - 14, r2 + 13.5, 9, dim, right=True)

    # detail line: hovered station; else the average of the district in the
    # middle of the map; else "outside"; else (whole city) the city mean
    y0 = my + mh + 70
    sel = next((s for s in visible if s.uid == view.hover), None)
    note = ""
    if sel is not None:
        note = (f"Community #{sel.id}" if sel.source == "Community" else sel.source) \
            + (f" · {sel.index_time[11:16]}" if sel.index_time else "")
    if sel:
        pointer = scale_position(sel.pm10, sel.pm25, sel.index)
        hist_key = sel.uid
        name = sel.name.split(",", 1)[-1].strip() or sel.name
        lab, col = INDEX[sel.index] if sel.index is not None else NO_INDEX
        if note:
            _text(ctx, note, W - 14, y0, 10.5, dim, right=True)
            _text(ctx, name, 14, y0, 12, bold=True, maxw=W - 40 - ctx.text_extents(note).x_advance)
        else:
            _text(ctx, name, 14, y0, 12, bold=True, maxw=W - 28)
        _text(ctx, lab, 14, y0 + 18, 11, col, bold=True)
        _text(ctx, f"PM10 {_fmt(sel.pm10)}   PM2.5 {_fmt(sel.pm25)}  µg/m³", W - 14, y0 + 18, 11, right=True)
    elif view.district:
        idx, pm10, pm25, last, n = summarise(district_sensors(view))
        pointer = scale_position(pm10, pm25)
        hist_key = "district:" + view.district
        lab, col = INDEX[idx] if idx is not None else NO_INDEX
        _text(ctx, view.district, 14, y0, 12, bold=True)
        if n:
            info = f"{n} sensor{'s' * (n != 1)}" + (f" · last measurement {last[11:16]}" if last else "")
        else:
            info = "loading…" if view.loading else "no sensors reporting here"
        _text(ctx, info, W - 14, y0, 10.5, dim, right=True)
        _text(ctx, "average: ", 14, y0 + 18, 11, dim)
        _text(ctx, lab if n else "no data", 14 + ctx.text_extents("average: ").x_advance, y0 + 18, 11,
              col, bold=True)
        _text(ctx, f"PM10 {_fmt(pm10)}   PM2.5 {_fmt(pm25)}  µg/m³", W - 14, y0 + 18, 11, right=True)
    elif view.outside:
        pointer, hist_key = None, ""
        _text(ctx, "Outside the monitored area", 14, y0, 12, bold=True)
        _text(ctx, "move the map back over Warsaw, or pick a district", 14, y0 + 18, 10.5, dim, maxw=W - 28)
    else:
        _text(ctx, "City mean", 14, y0, 12, bold=True)
        _text(ctx, "hover a station for details", W - 14, y0, 10.5, dim, right=True)
        pm10, pm25 = _mean(s.pm10 for s in view.stations), _mean(s.pm25 for s in view.stations)
        pointer = scale_position(pm10, pm25)
        hist_key = "city"
        _text(ctx, f"PM10 {_fmt(pm10)}   PM2.5 {_fmt(pm25)}  µg/m³", 14, y0 + 18, 11)

    # legend
    ly, lw = y0 + 30, (W - 28) / len(INDEX)
    for i, (_, col) in enumerate(INDEX):
        ctx.rectangle(14 + i * lw, ly, lw - 2, 5)
        ctx.set_source_rgb(*col)
        ctx.fill()
    if pointer is not None:     # what the line above describes: hovered sensor or the average
        px = min(max(14 + pointer * lw - 1, 14 + 5), W - 14 - 5)
        ctx.arc(px, ly + 2.5, 5, 0, 2 * math.pi)
        ctx.set_source_rgb(0.97, 0.98, 0.99)
        ctx.fill_preserve()
        ctx.set_line_width(2)
        ctx.set_source_rgb(0.07, 0.09, 0.11)
        ctx.stroke()
    _text(ctx, "very good", 14, ly + 16, 9, dim)
    _text(ctx, "very bad", W - 14, ly + 16, 9, dim, right=True)

    # stats since the first reading today (earlier ones are reloaded from the CSV
    # at start), for the same thing the lines above describe
    sy = ly + 26
    series = view.history.get(hist_key, [])
    st = history_stats(series)
    if st["n"]:
        first = time.localtime(st["since"])     # date too, once a run spans midnight
        fmt = "%H:%M" if first[:3] == time.localtime()[:3] else "%d %b %H:%M"
        head = f"history · since {time.strftime(fmt, first)} · {st['n']} reading{'s' * (st['n'] != 1)}"
    else:
        head = "history · none outside Warsaw" if hist_key == "" else "history · no readings yet"
    if view.gap_hover:              # pointer is on a dashed part of the trend line
        _text(ctx, gap_text(*view.gap_hover), 14, sy + 9, 9, (0.92, 0.94, 0.96), maxw=W - 28)
    else:
        _text(ctx, head, 14, sy + 9, 9, dim)
    for row, (key, name) in enumerate((("pm10", "PM10"), ("pm25", "PM2.5"))):
        yy = sy + 25 + row * 15
        _text(ctx, name, 14, yy, 10, dim)
        if st[key]:
            lo, avg, hi = st[key]
            x = 52.0
            for tag, v in (("min", lo), ("avg", avg), ("max", hi)):
                _text(ctx, tag, x, yy, 9, dim)
                _text(ctx, _fmt(v), x + 21, yy, 10.5)
                x += 58
        else:
            _text(ctx, "–", 52, yy, 10.5, dim)
    # sparkline of the worse pollutant's place on the scale, coloured by category
    pts = [(t, scale_position(a, b)) for t, a, b, _ in series]
    pts = [(t, v) for t, v in pts if v is not None]
    gx, gy, gw, gh = W - 14 - 104.0, sy + 15.0, 104.0, 26.0
    view.gaps = []
    if pts:
        t0, t1 = pts[0][0], pts[-1][0]
        lo, hi = min(v for _, v in pts), max(v for _, v in pts)
        pad = max(0.0, 1.0 - (hi - lo)) / 2          # show at least one category of range
        lo, hi = lo - pad, hi + pad
        xy = [(gx + (gw * (t - t0) / (t1 - t0) if t1 > t0 else gw), gy + gh * (1 - (v - lo) / (hi - lo)), v)
              for t, v in pts]
        ctx.set_line_width(1.6)
        for k, ((xa, ya, _), (xb, yb, vb)) in enumerate(zip(xy, xy[1:])):
            if pts[k + 1][0] - pts[k][0] > HISTORY_GAP:
                # widget was off / district closed: a dashed grey line, not data
                span = (pts[k][0], pts[k + 1][0])
                pad = max(0.0, 8 - (xb - xa)) / 2        # keep narrow gaps hoverable
                view.gaps.append((xa - pad, gy - 4, xb + pad, gy + gh + 4, *span))
                ctx.move_to(xa, ya)
                ctx.line_to(xb, yb)
                ctx.set_dash([3, 3])
                ctx.set_source_rgb(*((0.92, 0.94, 0.96) if view.gap_hover == span else NO_INDEX[1]))
                ctx.stroke()
                ctx.set_dash([])
                ctx.arc(xb, yb, 1.2, 0, 2 * math.pi)     # where readings resume
                ctx.set_source_rgb(*INDEX[min(int(vb), len(INDEX) - 1)][1])
                ctx.fill()
                continue
            ctx.move_to(xa, ya)
            ctx.line_to(xb, yb)
            ctx.set_source_rgb(*INDEX[min(int(vb), len(INDEX) - 1)][1])
            ctx.stroke()
        xl, yl, vl = xy[-1]
        ctx.arc(xl, yl, 2.6, 0, 2 * math.pi)
        ctx.set_source_rgb(*INDEX[min(int(vl), len(INDEX) - 1)][1])
        ctx.fill()
    # footer: source status or error, then attribution (required by the data terms)
    extra = " · ".join(t for t in (view.airly_status, view.community_status, view.street_status) if t)
    problem = view.error or (view.log.error if view.log else "")
    if problem:
        _text(ctx, problem, 14, H - 23, 9, (0.95, 0.55, 0.45), maxw=W - 28)
    elif extra and view.zoom >= DISTRICT_ZOOM:
        _text(ctx, extra, 14, H - 23, 9, dim, maxw=W - 28)
    credit = ", ".join("Sensor.Community" if src == "Community" else src for src in shown)
    _text(ctx, f"Data: {credit} · Map © OpenStreetMap", 14, H - 10, 9, dim, maxw=W - 28)


def load_tile_surfaces(view: View) -> None:
    """Read the tiles the current map view needs from disk into memory."""
    import cairo
    _, _, mw, mh = map_rect()
    needed = tiles_for_view(mw, mh, *view.center, view.zoom)
    if len(view.tiles) > TILE_MEMORY:       # forget tiles from places looked at earlier
        for xy in [xy for xy in view.tiles if xy not in needed][:len(view.tiles) - TILE_MEMORY // 2]:
            del view.tiles[xy]
    for xy in needed:
        if xy in view.tiles:
            continue
        p = tile_path(*xy)
        if p.exists():
            try:
                view.tiles[xy] = cairo.ImageSurface.create_from_png(str(p))
            except Exception:
                p.unlink(missing_ok=True)   # corrupt download: refetch next cycle


# ----------------------------------------------------------------------------
# 5. GTK shell
# ----------------------------------------------------------------------------
def run_gtk(demo: bool, district: str | None = None) -> None:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    try:
        saved = json.loads(STATE_FILE.read_text())
    except Exception:
        saved = {}
    start = None
    if district is None:                # no --district: carry on where the map was left
        try:
            start = clamp_view(float(saved["center"][0]), float(saved["center"][1]), int(saved["zoom"]))
        except Exception:
            district = saved.get("district", "")
    if start is None:
        start = viewport(district if district in DISTRICTS else "")
    name, outside = locate(*start)
    view = View(demo=demo, district=name, outside=outside, center=start[:2], zoom=start[2])
    key = airly_key()
    airly = Airly(key) if key else None
    community = Community()
    geocoder = Geocoder()
    view.labels = bool(saved.get("labels", False))
    if not demo:                        # demo readings are made up: never logged
        view.log = HistoryLog()
        restore_history(view, view.log.today_rows)      # today's earlier readings
        view.log.enabled = bool(saved.get("csv", True))
    if saved.get("refresh_minutes") in REFRESH_CHOICES:
        view.refresh_minutes = saved["refresh_minutes"]
    timer = {"id": 0}
    pending = {"id": 0}                 # timer that fires once the map has stopped moving
    drag = {"on": False, "x": 0.0, "y": 0.0}
    scroll = {"acc": 0.0}

    def preload_streets():
        """Look up streets for the community sensors in view, in the background:
        the open district's own first, then the others on the map, nearest its
        middle first. Stops as soon as the map moves on."""
        if demo:
            return
        _, _, mw, mh = map_rect()
        (clat, clon), gen = view.center, view.gen
        on_map = sorted((s for s in view.community_all
                         if in_viewport(s.lat, s.lon, view.center, view.zoom, mw, mh)),
                        key=lambda s: distance_km(clat, clon, s.lat, s.lon))
        todo = list({s.uid: s for s in view.community + on_map}.values())[:STREETS_PER_VIEW]
        if not todo:
            return

        def progress(text):
            def ui():
                if view.gen == gen:
                    view.street_status = text
                    area.queue_draw()
            GLib.idle_add(ui)
        threading.Thread(target=geocoder.resolve, daemon=True,
                         args=(todo, progress, lambda: view.gen == gen)).start()

    def sync_community():
        """The open district's community sensors, from the city-wide list in memory."""
        if not view.district:
            view.community, view.community_status = [], ""
            return
        _, _, mw, mh = map_rect()
        view.community = [s for s in view.community_all if in_view(s.lat, s.lon, view.district, mw, mh)]
        view.community_status = f"Community: {len(view.community)}"

    win = Gtk.Window(title="Warsaw smog")
    win.set_default_size(WIDTH, HEIGHT)
    win.set_resizable(False)
    win.set_decorated(False)
    win.set_app_paintable(True)
    # The hints below are honoured on X11. On Wayland (GNOME) clients cannot
    # place or pin themselves, so there it behaves as a small borderless window.
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_keep_below(True)
    win.stick()
    visual = win.get_screen().get_rgba_visual()
    if visual:
        win.set_visual(visual)

    area = Gtk.DrawingArea()
    area.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK
                    | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
                    | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
    win.add(area)

    def refresh(*_):
        if view.refreshing:             # one at a time, however often the button is clicked
            return True
        view.refreshing = True
        area.queue_draw()

        d, (lat, lon), zoom = view.district, view.center, view.zoom

        def work():
            err, stations = "", None
            try:
                _, _, mw, mh = map_rect()
                ensure_tiles(mw, mh, lat, lon, zoom)
                if demo:
                    stations = demo_stations()
                else:
                    stations = load_metadata(CITY)
                    fetch_readings(stations)
            except RateLimited:
                err = "GIOŚ rate limit hit, retrying later"
            except Exception as e:
                err = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            air = airly_view(airly, d, demo)       # independent of GIOŚ succeeding
            com = community_view(community, d, demo, geocoder)
            allc = all_community_sensors(community, d, demo, geocoder) if zoom >= DISTRICT_ZOOM else []

            def apply():
                view.loading = view.refreshing = False
                view.error = err
                if stations is not None and not err:
                    view.stations, view.updated = stations, time.time()
                if view.district == d:              # the map may have moved on meanwhile
                    view.community_all = allc
                    view.airly, view.airly_status, view.airly_for = air[0], air[1], d
                    view.community, view.community_status = com
                elif not demo:
                    view.community_all = allc
                    sync_community()
                record_history(view)
                load_tile_surfaces(view)
                area.queue_draw()
                preload_streets()
            GLib.idle_add(apply)

        threading.Thread(target=work, daemon=True).start()
        return True     # keep the GLib timer running

    def on_motion(_w, ev):
        if drag["on"]:                  # the map is being dragged
            if not ev.state & Gdk.ModifierType.BUTTON1_MASK:
                end_drag()              # the release happened where we could not see it
            else:
                dx, dy = ev.x - drag["x"], ev.y - drag["y"]
                if view.dragging or abs(dx) + abs(dy) > 3:      # ignore a shaky click
                    view.dragging = True
                    drag["x"], drag["y"] = ev.x, ev.y
                    set_view(*pan_by(view.center, view.zoom, dx, dy), view.zoom)
                return
        hit = min(view.hits, key=lambda h: (h[0] - ev.x) ** 2 + (h[1] - ev.y) ** 2, default=None)
        hid = hit[2].uid if hit and (hit[0] - ev.x) ** 2 + (hit[1] - ev.y) ** 2 <= 14 ** 2 else None
        chip = in_chip(ev)
        hot = "refresh" if inside(view.btn_refresh, ev) else "rate" if inside(view.rate_chip, ev) else ""
        gap = next(((g[4], g[5]) for g in view.gaps
                    if g[0] <= ev.x <= g[2] and g[1] <= ev.y <= g[3]), None)
        if hid != view.hover or chip != view.chip_hover or hot != view.hot or gap != view.gap_hover:
            view.hover, view.chip_hover, view.hot, view.gap_hover = hid, chip, hot, gap
            area.queue_draw()

    def on_leave(_w, _ev):
        if view.hover is not None or view.chip_hover or view.hot or view.gap_hover:
            view.hover, view.chip_hover, view.hot, view.gap_hover = None, False, "", None
            area.queue_draw()

    def inside(rect, ev):
        x, y, w, h = rect
        return x <= ev.x <= x + w and y <= ev.y <= y + h

    def arm_timer():
        if timer["id"]:
            GLib.source_remove(timer["id"])
        timer["id"] = GLib.timeout_add_seconds(view.refresh_minutes * 60, refresh)

    def set_rate(minutes):
        if minutes != view.refresh_minutes:
            view.refresh_minutes = minutes
            save_state()
            arm_timer()                 # the new interval counts from now
            area.queue_draw()

    def rate_menu():
        sub = Gtk.Menu()
        for m in REFRESH_CHOICES:
            item = Gtk.CheckMenuItem(label=rate_label(m))
            item.set_draw_as_radio(True)
            item.set_active(m == view.refresh_minutes)
            item.connect("activate", lambda _i, m=m: set_rate(m))
            sub.append(item)
        return sub

    def popup_below(menu, rect_xywh, ev):
        menu.attach_to_widget(area)
        win.menu = menu     # keep a reference while it is open
        menu.show_all()
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = (int(v) for v in rect_xywh)
        menu.popup_at_rect(area.get_window(), rect, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, ev)

    def set_view(lat, lon, zoom, delay=350):
        """Move the map. It is redrawn at once from what is already in memory;
        anything that needs the network waits until the map has been still for
        `delay` ms, so dragging across the city does not fire a request per pixel."""
        lat, lon, zoom = clamp_view(lat, lon, zoom)
        if (lat, lon) == tuple(view.center) and zoom == view.zoom:
            return
        view.center, view.zoom = (lat, lon), zoom
        view.gen += 1                   # background work for the previous view stops
        name, view.outside = locate(lat, lon, zoom)
        view.hover = view.gap_hover = None
        if name != view.district:       # the middle of the map crossed into another district
            view.district = name
            view.airly, view.airly_for = [], None
            view.airly_status = "Airly: loading…" if name else ""
            view.street_status = ""
            if demo:
                view.community_all = demo_community(name) if name else []
            sync_community()
        load_tile_surfaces(view)        # tiles already on disk show at once
        area.queue_draw()
        if pending["id"]:
            GLib.source_remove(pending["id"])
        pending["id"] = GLib.timeout_add(delay, settle)

    def settle():
        """The map has stopped moving: fetch what the new view needs."""
        pending["id"] = 0
        gen, d, (lat, lon), zoom = view.gen, view.district, view.center, view.zoom
        save_state()

        def work():
            _, _, mw, mh = map_rect()
            ensure_tiles(mw, mh, lat, lon, zoom)
            GLib.idle_add(lambda: (load_tile_surfaces(view), area.queue_draw()) and False)
            if view.gen != gen:
                return                  # moved again while the tiles were downloading
            allc = all_community_sensors(community, d, demo, geocoder) if zoom >= DISTRICT_ZOOM else []

            def apply():
                if view.gen != gen:
                    return              # moved again: the next settle takes over
                view.community_all = allc
                sync_community()
                if d and view.airly_for != d:
                    # Airly costs one request per sensor, so wait until the map
                    # has really come to rest on this district before asking.
                    GLib.timeout_add(AIRLY_SETTLE_MS, fetch_airly, gen, d)
                else:
                    record_history(view)
                area.queue_draw()
                preload_streets()
            GLib.idle_add(apply)
        threading.Thread(target=work, daemon=True).start()
        return False                    # one-shot timer

    def fetch_airly(gen, d):
        if view.gen != gen or view.district != d:
            return False                # the map moved on: do not spend the requests

        def work():
            air = airly_view(airly, d, demo)

            def apply():
                if view.district == d:
                    view.airly, view.airly_status, view.airly_for = air[0], air[1], d
                    record_history(view)        # all sources for this district are in now
                    area.queue_draw()
            GLib.idle_add(apply)
        threading.Thread(target=work, daemon=True).start()
        return False

    def set_district(name):
        """Jump to a district's standard view ("" = the whole city)."""
        set_view(*viewport(name), delay=0)

    def zoom_at(x, y, step):
        """Zoom one level in or out, keeping the point under the pointer in place."""
        zoom = min(max(view.zoom + step, ZOOM_MIN), ZOOM_MAX)
        if zoom == view.zoom:
            return
        if zoom < DISTRICT_ZOOM:        # all the way out: the standard whole-city view
            return set_view(*viewport(""))
        mx, my, mw, mh = map_rect()
        set_view(*zoom_about(view.center, view.zoom, zoom, x - (mx + mw / 2), y - (my + mh / 2)), zoom)

    def on_scroll(_w, ev):
        if not inside(map_rect(), ev):
            return False
        if ev.direction == Gdk.ScrollDirection.UP:
            step = 1
        elif ev.direction == Gdk.ScrollDirection.DOWN:
            step = -1
        elif ev.direction == Gdk.ScrollDirection.SMOOTH:     # touchpads, and wheels on some setups
            scroll["acc"] += ev.delta_y
            if abs(scroll["acc"]) < 1:
                return True
            step = -1 if scroll["acc"] > 0 else 1
            scroll["acc"] = 0.0
        else:
            return False
        zoom_at(ev.x, ev.y, step)
        return True

    def end_drag():
        drag["on"] = False
        if view.dragging:
            view.dragging = False
            area.queue_draw()           # the labels come back

    def on_release(_w, ev):
        if ev.button == 1 and drag["on"]:
            end_drag()

    def toggle_csv(*_):
        view.log.enabled = not view.log.enabled
        save_state()

    def open_history(*_):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        Gtk.show_uri_on_window(win, DATA_DIR.as_uri(), Gdk.CURRENT_TIME)

    def toggle_labels(*_):
        view.labels = not view.labels
        save_state()
        area.queue_draw()

    def in_toggle(ev):
        x, y, w, h = view.toggle
        return x <= ev.x <= x + w and y <= ev.y <= y + h

    def in_chip(ev):
        x, y, w, h = view.chip
        return x <= ev.x <= x + w and y <= ev.y <= y + h

    def district_menu():
        sub = Gtk.Menu()
        for name in ["", *sorted(DISTRICTS, key=PL_SORT_KEY)]:
            item = Gtk.CheckMenuItem(label=name or "Whole city")
            item.set_draw_as_radio(True)
            item.set_active(name == view.district and not view.outside)     # before connect: no spurious signal
            item.connect("activate", lambda _i, n=name: set_district(n))
            sub.append(item)
            if not name:
                sub.append(Gtk.SeparatorMenuItem())
        return sub

    def on_press(_w, ev):
        if ev.button == 1 and in_toggle(ev):
            toggle_labels()
        elif ev.button == 1 and inside(view.btn_refresh, ev):
            refresh()
        elif ev.button == 1 and inside(view.rate_chip, ev):
            popup_below(rate_menu(), view.rate_chip, ev)
        elif ev.button == 1 and in_chip(ev):
            menu = district_menu()
            menu.attach_to_widget(area)
            win.menu = menu     # keep a reference while it is open
            menu.show_all()
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = (int(v) for v in view.chip)
            menu.popup_at_rect(area.get_window(), rect,
                               Gdk.Gravity.SOUTH_EAST, Gdk.Gravity.NORTH_EAST, ev)
        elif ev.button == 1 and inside(map_rect(), ev):
            drag.update(on=True, x=ev.x, y=ev.y)        # dragging the map moves the map
        elif ev.button == 1:                            # dragging anywhere else moves the widget
            win.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
        elif ev.button == 3:
            menu = Gtk.Menu()
            menu.attach_to_widget(area)
            sub = district_menu()
            pick = Gtk.MenuItem(label="District")
            pick.set_submenu(sub)
            menu.append(pick)
            lab = Gtk.CheckMenuItem(label="Show labels")
            lab.set_active(view.labels)
            lab.connect("activate", toggle_labels)
            menu.append(lab)
            rate = Gtk.MenuItem(label="Auto-refresh")
            rate.set_submenu(rate_menu())
            menu.append(rate)
            if view.log is not None:
                keep = Gtk.CheckMenuItem(label="Save history to CSV")
                keep.set_active(view.log.enabled)
                keep.connect("activate", toggle_csv)
                menu.append(keep)
                folder = Gtk.MenuItem(label="Open history folder")
                folder.connect("activate", open_history)
                menu.append(folder)
            for text, cb in (("Refresh now", refresh),
                             ("Open Airly map", lambda *_: Gtk.show_uri_on_window(
                                 win, "https://airly.org/map/en/", Gdk.CURRENT_TIME)),
                             ("Quit", quit_app)):
                item = Gtk.MenuItem(label=text)
                item.connect("activate", cb)
                menu.append(item)
            win.menu = menu     # keep a reference while it is open
            menu.show_all()
            menu.popup_at_pointer(ev)

    def save_state():
        try:
            STATE_FILE.write_text(json.dumps(
                {"pos": list(win.get_position()), "district": view.district, "labels": view.labels,
                 "center": list(view.center), "zoom": view.zoom,
                 "refresh_minutes": view.refresh_minutes,
                 "csv": view.log.enabled if view.log else saved.get("csv", True)}, ensure_ascii=False))
        except OSError:
            pass

    def quit_app(*_):
        save_state()
        Gtk.main_quit()

    area.connect("draw", lambda _w, ctx: render(ctx, view))
    area.connect("motion-notify-event", on_motion)
    area.connect("leave-notify-event", on_leave)
    area.connect("button-press-event", on_press)
    area.connect("button-release-event", on_release)
    area.connect("scroll-event", on_scroll)
    win.connect("delete-event", quit_app)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, quit_app)   # SIGTERM at logout

    try:
        x, y = saved["pos"]
        win.move(int(x), int(y))
    except Exception:
        pass

    win.show_all()
    refresh()
    arm_timer()
    Gtk.main()


def snapshot(path: str, demo: bool, district: str | None = None, labels: bool = False) -> None:
    """Render one frame to a PNG without opening a window."""
    import cairo
    view = View(demo=demo, loading=False, district=district or "")
    _, _, mw, mh = map_rect()
    ensure_tiles(mw, mh, *view.center, view.zoom)
    if demo:
        view.stations = demo_stations()
    else:
        view.stations = load_metadata(CITY)
        fetch_readings(view.stations)
    view.updated = time.time()
    key = airly_key()
    view.airly, view.airly_status = airly_view(Airly(key) if key else None, view.district, demo)
    geocoder = Geocoder()
    view.labels = labels
    view.community, view.community_status = community_view(Community(), view.district, demo, geocoder)
    view.community_all, view.airly_for = view.community, view.district
    if not demo:
        geocoder.resolve(view.community)
    record_history(view)
    load_tile_surfaces(view)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH * 2, HEIGHT * 2)
    ctx = cairo.Context(surf)
    ctx.scale(2, 2)
    render(ctx, view)
    surf.write_to_png(path)


# ----------------------------------------------------------------------------
# 6. district outlines (data)
# ----------------------------------------------------------------------------
# Used to tell which district the middle of the map is in, and whether it is
# outside Warsaw. Source: https://github.com/andilabs/warszawa-dzielnice-geojson
# (warszawa-dzielnice.geojson), simplified here to about 40 m. Each point is
# "(lon - 20) * 10000,(lat - 52) * 10000".
DISTRICT_SHAPES = {
    "Bemowo":
        "8731,2167 8728,2152 8719,2153 8710,2104 8702,2104 8709,2089 8768,2094 8805,2090 8877,2108 "
        "9141,2141 9157,2173 9213,2183 9243,2206 9245,2223 9216,2278 9214,2297 9249,2412 9262,2434 "
        "9449,2536 9476,2565 9472,2571 9518,2614 9536,2619 9431,2647 9415,2663 9390,2671 9197,2710 "
        "8937,2772 8891,2806 8880,2802 8860,2806 8843,2791 8812,2797 8787,2652 8637,2500 8727,2493 "
        "8839,2473 8846,2482 8850,2473 8908,2471 8914,2450 8938,2439 8930,2399 8895,2320 8797,2320 "
        "8728,2288 8725,2257 8753,2248 8746,2167",
    "Białołęka":
        "9962,2873 9978,2880 9971,2894 9988,2903 9989,2930 10006,2933 10029,2913 10097,2930 10127,2943 "
        "10130,2975 10146,2979 10180,2959 10234,2978 10229,2985 10238,2988 10244,2982 10410,3020 "
        "10606,3070 10614,3083 10621,3084 10623,3074 10686,3079 10836,3078 10778,3220 10895,3238 "
        "10860,3320 10878,3323 10875,3329 10867,3327 10855,3347 10850,3373 10826,3372 10811,3431 "
        "10857,3434 10815,3540 10787,3536 10738,3673 10615,3657 10606,3626 10541,3631 10493,3618 "
        "10487,3637 10443,3640 10408,3630 10307,3617 10275,3651 10254,3634 10234,3574 10215,3567 "
        "10136,3560 10133,3568 10088,3571 10083,3601 10057,3641 9947,3618 9919,3628 9781,3590 9713,3590 "
        "9661,3682 9542,3679 9523,3665 9449,3667 9452,3662 9440,3660 9436,3664 9393,3618 9351,3632 "
        "9392,3584 9385,3581 9340,3598 9309,3579 9275,3595 9241,3596 9230,3570 9124,3530 9233,3436 "
        "9258,3361 9250,3280 9257,3248 9278,3214 9310,3182 9373,3147 9475,3107 9513,3078 9561,3026 "
        "9626,2984 9705,2958 9854,2926 9912,2905",
    "Bielany":
        "8771,2941 8758,2805 8840,2791 8860,2806 8880,2802 8891,2806 8937,2772 9197,2710 9390,2671 "
        "9415,2663 9431,2647 9536,2619 9557,2635 9587,2683 9631,2715 9650,2741 9670,2741 9701,2754 "
        "9852,2834 9855,2857 9866,2864 9905,2848 9962,2873 9912,2905 9854,2926 9705,2958 9626,2984 "
        "9561,3026 9513,3078 9475,3107 9373,3147 9310,3182 9266,3231 9251,3294 9226,3268 9221,3237 "
        "9131,3218 9105,3241 9078,3222 9071,3221 9066,3234 8952,3214 8915,3176 8898,3136 8869,3132 "
        "8798,3138 8763,3149 8762,3140 8793,3117 8809,3092 8762,2987",
    "Mokotów":
        "10309,2069 10286,2072 10259,2093 10245,2114 10207,2133 9984,2091 9879,2103 9820,1929 9846,1918 "
        "9865,1892 9868,1711 9877,1706 9883,1715 10080,1671 10086,1679 10114,1669 10120,1655 10157,1655 "
        "10158,1648 10182,1642 10195,1668 10243,1665 10257,1677 10306,1668 10374,1688 10422,1724 "
        "10433,1749 10452,1745 10538,1694 10600,1702 10679,1697 10685,1702 10641,1741 10660,1748 "
        "10653,1756 10688,1764 10815,1845 10894,1812 10898,1806 10950,1809 10945,1823 10955,1854 "
        "10979,1861 10996,1873 10999,1910 11054,1941 11041,2041 11008,2097 10928,2154 10827,2182 "
        "10635,2189 10612,2166 10540,2154 10543,2147 10494,2140 10491,2147 10464,2143 10474,2114 "
        "10447,2097 10363,2096 10349,2069",
    "Ochota":
        "10015,2281 9757,2220 9690,2195 9683,2200 9592,2181 9546,2146 9443,2144 9383,2122 9343,2119 "
        "9327,2111 9372,2091 9461,2014 9820,1929 9879,2103 9984,2091 10070,2107 10050,2172 10047,2231",
    "Praga-Południe":
        "10929,2153 11195,2324 11247,2325 11274,2333 11324,2391 11302,2408 11323,2434 11218,2524 "
        "11189,2535 11143,2568 11146,2595 10883,2605 10766,2594 10660,2577 10622,2556 10452,2493 "
        "10436,2476 10436,2430 10409,2406 10369,2386 10494,2254 10558,2212 10635,2189 10827,2182",
    "Praga-Północ":
        "10131,2948 10116,2936 10029,2913 10006,2933 9989,2930 9988,2903 9971,2894 9978,2880 9962,2873 "
        "10003,2836 10031,2794 10054,2683 10094,2612 10210,2489 10369,2386 10409,2406 10436,2430 "
        "10436,2476 10457,2497 10622,2556 10652,2575 10742,2590 10383,2680 10319,2708 10266,2721 "
        "10211,2798 10207,2861 10150,2911",
    "Rembertów":
        "11324,2391 11382,2432 11394,2432 11418,2391 11617,2430 11636,2416 11734,2435 11769,2452 "
        "11840,2463 11846,2469 11830,2495 11869,2504 11870,2518 11888,2523 11902,2497 11912,2500 "
        "11905,2562 11793,2567 11824,2579 11842,2598 11849,2661 11839,2678 11758,2724 11706,2769 "
        "11724,2886 11693,2909 11661,2903 11635,2921 11629,2915 11639,2894 11613,2854 11559,2836 "
        "11491,2793 11078,2744 11095,2708 11086,2705 11089,2685 11076,2683 11105,2597 11146,2595 "
        "11143,2568 11189,2535 11218,2524 11323,2434 11302,2408",
    "Śródmieście":
        "10369,2386 10210,2489 10094,2612 10035,2594 10001,2591 9866,2598 9810,2594 9826,2546 9954,2397 "
        "9982,2332 10047,2231 10050,2172 10070,2107 10207,2133 10245,2114 10259,2093 10286,2072 "
        "10349,2069 10363,2096 10447,2097 10474,2114 10464,2143 10491,2147 10494,2140 10543,2147 "
        "10540,2154 10613,2166 10635,2189 10558,2212 10508,2242",
    "Targówek":
        "10369,2686 10556,2633 10740,2590 10883,2605 11105,2597 11076,2683 11089,2685 11086,2705 "
        "11095,2708 11078,2744 11049,2741 10987,2865 10956,2863 10950,2882 10875,2955 10835,3029 "
        "10855,3032 10836,3078 10686,3079 10623,3074 10621,3084 10614,3083 10606,3070 10410,3020 "
        "10244,2982 10238,2988 10229,2985 10234,2978 10180,2959 10146,2979 10130,2975 10126,2957 "
        "10146,2917 10207,2861 10208,2806 10247,2738 10271,2718 10319,2708",
    "Ursus":
        "8519,1997 8538,1953 8567,1913 8560,1877 8671,1908 8672,1825 8977,1825 8993,1841 9090,1845 "
        "9092,1859 9148,1858 9192,1938 9154,1927 9121,1943 9105,1960 9007,1995 9001,2017 8812,2089 "
        "8784,2093 8658,2083 8568,2067 8526,2037",
    "Ursynów":
        "9796,1295 9796,1276 9825,1239 9890,1237 9849,1204 9873,1157 9837,1117 9867,1106 9845,1056 "
        "9874,1051 9869,1039 9916,1031 10184,1037 10186,1060 10355,1062 10352,1067 10437,1130 10508,1090 "
        "10580,1072 10800,979 10826,987 10866,1028 10893,1039 10919,1046 10960,1046 11035,1058 11018,1078 "
        "10982,1088 10948,1139 10882,1144 10881,1148 10940,1147 10916,1176 10895,1174 10886,1182 "
        "10915,1185 10925,1204 10912,1206 10837,1286 10793,1359 10771,1352 10754,1354 10738,1373 "
        "10723,1372 10715,1400 10677,1444 10696,1452 10613,1524 10617,1532 10607,1555 10551,1612 "
        "10467,1681 10538,1694 10452,1745 10433,1749 10422,1724 10374,1688 10306,1668 10257,1677 "
        "10243,1665 10195,1668 10182,1642 10158,1648 10157,1655 10120,1655 10114,1669 10086,1679 "
        "10080,1671 9883,1715 9877,1706 9868,1711 9892,1419 9839,1420 9830,1404 9847,1392 9792,1339",
    "Wawer":
        "11054,1941 11102,1868 11147,1825 11212,1786 11315,1745 11379,1703 11417,1654 11440,1584 "
        "11471,1550 11502,1531 11597,1494 11650,1447 11714,1454 11759,1467 11759,1485 11884,1505 "
        "11886,1493 12151,1518 12347,1589 12488,1530 12471,1516 12557,1472 12623,1527 12626,1531 "
        "12540,1574 12566,1607 12604,1620 12615,1657 12631,1674 12677,1674 12676,1684 12708,1685 "
        "12703,1696 12675,1701 12673,1733 12696,1734 12700,1720 12712,1722 12702,1738 12598,1826 "
        "12634,1840 12654,1860 12402,1959 12388,1970 12163,1974 12157,2007 12164,2011 12155,2021 "
        "12166,2047 12184,2053 12146,2093 12153,2122 12132,2144 12122,2143 12065,2205 12073,2206 "
        "12073,2276 12119,2300 12071,2332 12009,2347 11970,2346 11840,2398 11765,2451 11734,2435 "
        "11636,2416 11617,2430 11418,2391 11394,2432 11382,2432 11324,2391 11274,2333 11247,2325 "
        "11195,2324 10929,2153 10992,2112 11025,2074 11047,2015",
    "Wesoła":
        "12617,2101 12505,2226 12495,2226 12504,2319 12544,2353 12548,2387 12488,2366 12454,2361 "
        "12395,2363 12379,2450 12537,2458 12546,2532 12524,2533 12505,2627 11905,2562 11912,2500 "
        "11902,2497 11888,2523 11870,2518 11869,2504 11830,2495 11846,2469 11840,2463 11765,2451 "
        "11840,2398 11970,2346 12009,2347 12071,2332 12119,2300 12073,2276 12073,2206 12065,2205 "
        "12122,2143 12132,2144 12153,2122 12146,2093 12184,2053 12166,2047 12155,2021 12164,2011 "
        "12157,2007 12163,1974 12388,1970 12402,1959 12430,1984 12430,1993 12457,2009 12509,2025 "
        "12486,2042 12482,2060 12491,2070 12541,2094",
    "Wilanów":
        "11332,1230 11347,1235 11358,1277 11387,1313 11405,1320 11401,1329 11452,1356 11478,1385 "
        "11502,1398 11537,1401 11532,1427 11547,1436 11650,1447 11597,1494 11502,1531 11471,1550 "
        "11440,1584 11417,1654 11379,1703 11315,1745 11212,1786 11147,1825 11102,1868 11054,1941 "
        "10999,1910 10996,1873 10979,1861 10955,1854 10945,1823 10950,1809 10898,1806 10894,1812 "
        "10815,1845 10688,1764 10653,1756 10660,1748 10641,1741 10685,1702 10679,1697 10600,1702 "
        "10467,1681 10551,1612 10607,1555 10617,1532 10613,1524 10696,1452 10677,1444 10715,1400 "
        "10723,1372 10738,1373 10754,1354 10771,1352 10793,1359 10837,1286 10912,1206 10925,1204 "
        "10915,1185 10886,1182 10895,1174 10916,1176 10940,1147 10881,1148 10882,1144 10948,1139 "
        "10964,1106 10984,1086 11009,1081 11001,1084 11101,1132 11089,1145 11160,1186 11192,1188 "
        "11229,1204 11264,1241 11271,1261 11295,1272",
    "Włochy":
        "9830,1404 9839,1420 9892,1419 9869,1626 9865,1892 9841,1922 9461,2014 9372,2091 9325,2114 "
        "9268,2095 9233,2105 9226,2121 9244,2134 9238,2137 9216,2135 9183,2146 9147,2146 9141,2141 "
        "8877,2108 8811,2094 8805,2090 9001,2017 9007,1995 9105,1960 9121,1943 9154,1927 9192,1938 "
        "9112,1796 9170,1785 9168,1778 9187,1774 9192,1711 9209,1710 9232,1689 9247,1684 9254,1672 "
        "9239,1662 9305,1640 9300,1633 9377,1601 9470,1572 9538,1561 9538,1530 9584,1499 9697,1454 "
        "9776,1435",
    "Wola":
        "10015,2281 9982,2332 9954,2397 9826,2546 9810,2594 9656,2543 9600,2558 9547,2553 9516,2555 "
        "9480,2568 9427,2522 9262,2434 9249,2412 9214,2297 9216,2278 9245,2223 9243,2206 9213,2183 "
        "9157,2173 9147,2146 9183,2146 9216,2135 9238,2137 9244,2134 9229,2126 9229,2109 9256,2095 "
        "9268,2095 9343,2119 9383,2122 9443,2144 9546,2146 9592,2181 9683,2200 9690,2195 9757,2220",
    "Żoliborz":
        "9810,2594 9866,2598 10001,2591 10035,2594 10094,2612 10054,2683 10031,2794 10003,2836 9962,2873 "
        "9905,2848 9866,2864 9855,2857 9852,2834 9701,2754 9670,2741 9650,2741 9631,2715 9578,2674 "
        "9557,2635 9532,2615 9518,2614 9472,2571 9476,2565 9516,2555 9547,2553 9600,2558 9656,2543",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--demo", action="store_true", help="use built-in fake readings (no GIOŚ requests)")
    ap.add_argument("--snapshot", metavar="PNG", help="render one frame to a file and exit")
    ap.add_argument("--district", choices=sorted(DISTRICTS, key=PL_SORT_KEY), metavar="NAME",
                    help="start zoomed on a district (default: last one selected). One of: "
                         + ", ".join(sorted(DISTRICTS, key=PL_SORT_KEY)))
    ap.add_argument("--labels", action="store_true", help="with --snapshot: draw marker labels")
    args = ap.parse_args()
    if args.snapshot:
        snapshot(args.snapshot, args.demo, args.district, args.labels)
    else:
        run_gtk(args.demo, args.district)


if __name__ == "__main__":
    sys.exit(main())
