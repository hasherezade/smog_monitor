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
"""
from __future__ import annotations

import argparse
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
WIDTH, HEIGHT = 360, 496

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
REFRESH_MINUTES = 30              # GIOŚ publishes hourly values
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
SC_MAX_AGE = 10 * 60              # reuse the area snapshot for this long

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


def tiles_for_view(w: float, h: float, district: str = "") -> list[tuple[int, int, int]]:
    """(z, x, y) of every tile intersecting a w x h px view of the district."""
    lat, lon, z = viewport(district)
    cx, cy = mercator_px(lat, lon, z)
    x0, x1 = int((cx - w / 2) // 256), int((cx + w / 2) // 256)
    y0, y1 = int((cy - h / 2) // 256), int((cy + h / 2) // 256)
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def tile_path(z: int, x: int, y: int) -> Path:
    return CACHE_DIR / "tiles" / str(z) / str(x) / f"{y}.png"


def ensure_tiles(w: float, h: float, district: str = "") -> None:
    """Download missing tiles once; they are kept on disk indefinitely."""
    for z, x, y in tiles_for_view(w, h, district):
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
    district: str = ""                # "" = whole city, else a key of DISTRICTS
    hits: list = field(default_factory=list)   # [(x, y, station)] from last draw
    chip: tuple = (0.0, 0.0, 0.0, 0.0)         # district selector rect from last draw
    chip_hover: bool = False
    labels: bool = False                       # draw a name next to every marker
    toggle: tuple = (0.0, 0.0, 0.0, 0.0)       # "labels" checkbox rect from last draw
    tiles: dict = field(default_factory=dict)  # {(z, x, y): cairo surface}


def map_rect() -> tuple[float, float, float, float]:
    return 12.0, 62.0, WIDTH - 24.0, HEIGHT - 62.0 - 112.0


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
    chip_label = view.district or "Whole city"
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
    clat, clon, zoom = viewport(view.district)
    cx, cy = mercator_px(clat, clon, zoom)
    ox, oy = mx + mw / 2 - cx, my + mh / 2 - cy       # global px -> widget px
    for (tz, tx, ty), surf in view.tiles.items():
        if tz != zoom:
            continue
        ctx.set_source_surface(surf, round(ox + tx * 256), round(oy + ty * 256))
        ctx.paint_with_alpha(0.55)                    # dim the basemap so dots carry
    view.hits = []
    visible = view.stations + (view.community + view.airly if view.district else [])
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
    if view.labels:
        ctx.select_font_face("Ubuntu", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        ctx.set_font_size(8.5)
        items = []
        for x, y, s in sorted(view.hits, key=lambda h: -SOURCE_RANK[h[2].source]):
            t = short_label(s)
            while len(t) > 4 and ctx.text_extents(t).x_advance > 110:
                t = t[:-2].rstrip() + "…"
            if t:
                items.append((x, y, ctx.text_extents(t).x_advance + 6, t))
        fixed = [(mx + 6, my + 6, 62, 18)]                      # the checkbox
        if view.district and (view.airly_status or view.community_status):
            fixed.append((mx + 6, my + mh - 24, mw * 0.62, 18))  # the status line
        for (x, y, w, t), box in zip(items, place_labels(
                [(x, y, w) for x, y, w, _ in items], (mx, my, mw, mh), fixed)):
            _rounded(ctx, *box, 4)
            ctx.set_source_rgba(0.07, 0.09, 0.11, 0.78)
            ctx.fill()
            _text(ctx, t, box[0] + 3, box[1] + 10, 8.5, (0.88, 0.90, 0.93))
    # the checkbox itself, top-left corner of the map
    view.toggle = (mx + 6, my + 6, 62, 18)
    _rounded(ctx, *view.toggle, 6)
    ctx.set_source_rgba(0.07, 0.09, 0.11, 0.80)
    ctx.fill()
    bx, by = mx + 12, my + 10
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
    extra = " · ".join(t for t in (view.airly_status, view.community_status, view.street_status) if t)
    if view.district and extra:
        ctx.select_font_face("Ubuntu", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        ctx.set_font_size(9)
        tw = min(ctx.text_extents(extra).x_advance, mw - 28)
        _rounded(ctx, mx + 6, my + mh - 24, tw + 12, 18, 6)
        ctx.set_source_rgba(0.07, 0.09, 0.11, 0.80)
        ctx.fill()
        _text(ctx, extra, mx + 12, my + mh - 11.5, 9, (0.80, 0.83, 0.87), maxw=mw - 28)
    ctx.restore()

    # detail line: hovered station; else in a district view the station nearest
    # its centre (most districts have none of their own); else the city mean
    y0 = my + mh + 20
    sel = next((s for s in visible if s.uid == view.hover), None)
    note = ""
    if sel is not None:
        note = (f"Community #{sel.id}" if sel.source == "Community" else sel.source) \
            + (f" · {sel.index_time[11:16]}" if sel.index_time else "")
    elif view.district and visible:
        sel = min([s for s in visible if s.index is not None] or visible,   # prefer a reporting one
                  key=lambda s: distance_km(clat, clon, s.lat, s.lon))
        note = f"nearest to centre · {distance_km(clat, clon, sel.lat, sel.lon):.1f} km"
    if sel:
        name = sel.name.split(",", 1)[-1].strip() or sel.name
        lab, col = INDEX[sel.index] if sel.index is not None else NO_INDEX
        if note:
            _text(ctx, note, W - 14, y0, 10.5, dim, right=True)
            _text(ctx, name, 14, y0, 12, bold=True, maxw=W - 40 - ctx.text_extents(note).x_advance)
        else:
            _text(ctx, name, 14, y0, 12, bold=True, maxw=W - 28)
        _text(ctx, lab, 14, y0 + 18, 11, col, bold=True)
        _text(ctx, f"PM10 {_fmt(sel.pm10)}   PM2.5 {_fmt(sel.pm25)}  µg/m³", W - 14, y0 + 18, 11, right=True)
    else:
        _text(ctx, "City mean", 14, y0, 12, bold=True)
        _text(ctx, "hover a station for details", W - 14, y0, 10.5, dim, right=True)
        _text(ctx, f"PM10 {_fmt(_mean(s.pm10 for s in view.stations))}   "
                   f"PM2.5 {_fmt(_mean(s.pm25 for s in view.stations))}  µg/m³", 14, y0 + 18, 11)

    # legend
    ly, lw = y0 + 30, (W - 28) / len(INDEX)
    for i, (_, col) in enumerate(INDEX):
        ctx.rectangle(14 + i * lw, ly, lw - 2, 5)
        ctx.set_source_rgb(*col)
        ctx.fill()
    _text(ctx, "very good", 14, ly + 16, 9, dim)
    _text(ctx, "very bad", W - 14, ly + 16, 9, dim, right=True)
    # marker key + attribution, only for the sources actually on screen
    shown = ["GIOŚ"] + [src for src, lst in (("Airly", view.airly), ("Community", view.community))
                         if view.district and lst]
    if len(shown) > 1:
        kx = 100.0
        for src in shown:
            _marker(ctx, src, kx, ly + 12.5, 4.0)
            ctx.set_source_rgb(*dim)
            ctx.fill()
            _text(ctx, src.lower() if src == "Community" else src, kx + 8, ly + 16, 9, dim)
            kx += 16 + ctx.text_extents(src).x_advance

    # footer: status + attribution (required by both GIOŚ and OSM terms)
    if view.error:
        status, col = view.error, (0.95, 0.55, 0.45)
    elif view.updated:
        times = sorted(s.index_time for s in view.stations if s.index_time)
        data_t = f"data {times[-1][11:16]} · " if times else ""
        status, col = data_t + "checked " + time.strftime("%H:%M", time.localtime(view.updated)), dim
    else:
        status, col = "", dim
    _text(ctx, status, 14, H - 23, 9, col, maxw=W - 28)
    credit = ", ".join("Sensor.Community" if src == "Community" else src for src in shown)
    _text(ctx, f"Data: {credit} · Map © OpenStreetMap", 14, H - 10, 9, dim, maxw=W - 28)


def load_tile_surfaces(view: View) -> None:
    import cairo
    _, _, mw, mh = map_rect()
    for xy in tiles_for_view(mw, mh, view.district):
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
    if district is None:
        district = saved.get("district", "")
    view = View(demo=demo, district=district if district in DISTRICTS else "")
    key = airly_key()
    airly = Airly(key) if key else None
    community = Community()
    geocoder = Geocoder()
    view.labels = bool(saved.get("labels", False))

    def preload_streets(stations, d):
        """Start loading every street for district d now, in its own thread."""
        def progress(text):
            def ui():
                if view.district == d:
                    view.street_status = text
                    area.queue_draw()
            GLib.idle_add(ui)
        threading.Thread(target=geocoder.resolve, daemon=True,
                         args=(stations, progress, lambda: view.district == d)).start()

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
                    | Gdk.EventMask.LEAVE_NOTIFY_MASK)
    win.add(area)

    def refresh():
        def work():
            err, stations, d = "", None, view.district
            try:
                _, _, mw, mh = map_rect()
                ensure_tiles(mw, mh, d)
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

            def apply():
                view.loading = False
                view.error = err
                if stations is not None and not err:
                    view.stations, view.updated = stations, time.time()
                if view.district == d:              # user may have switched meanwhile
                    view.airly, view.airly_status = air
                    view.community, view.community_status = com
                load_tile_surfaces(view)
                area.queue_draw()
            GLib.idle_add(apply)
            if not demo:    # sensors that appeared since the district was opened
                preload_streets(com[0], d)

        threading.Thread(target=work, daemon=True).start()
        return True     # keep the GLib timer running

    def on_motion(_w, ev):
        hit = min(view.hits, key=lambda h: (h[0] - ev.x) ** 2 + (h[1] - ev.y) ** 2, default=None)
        hid = hit[2].uid if hit and (hit[0] - ev.x) ** 2 + (hit[1] - ev.y) ** 2 <= 14 ** 2 else None
        chip = in_chip(ev)
        if hid != view.hover or chip != view.chip_hover:
            view.hover, view.chip_hover = hid, chip
            area.queue_draw()

    def on_leave(_w, _ev):
        if view.hover is not None or view.chip_hover:
            view.hover, view.chip_hover = None, False
            area.queue_draw()

    def set_district(name):
        if name == view.district:
            return
        view.district, view.hover = name, None
        view.airly, view.airly_status = [], ("Airly: loading…" if name and airly else "")
        view.community, view.community_status = [], ("Community: loading…" if name else "")
        view.street_status = ""
        save_state()
        load_tile_surfaces(view)        # whatever is cached shows immediately
        area.queue_draw()

        def work():                     # fetch the rest off the UI thread
            _, _, mw, mh = map_rect()
            ensure_tiles(mw, mh, name)
            GLib.idle_add(lambda: (load_tile_surfaces(view), area.queue_draw()) and False)
            com = community_view(community, name, demo, geocoder)

            def apply_com():                # one request, arrives first
                if view.district == name:
                    view.community, view.community_status = com
                    area.queue_draw()
            GLib.idle_add(apply_com)
            if not demo:                    # all streets, straight away, alongside Airly
                preload_streets(com[0], name)
            air = airly_view(airly, name, demo)

            def apply():
                if view.district == name:
                    view.airly, view.airly_status = air
                    area.queue_draw()
            GLib.idle_add(apply)
        threading.Thread(target=work, daemon=True).start()

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
            item.set_active(name == view.district)      # before connect: no spurious signal
            item.connect("activate", lambda _i, n=name: set_district(n))
            sub.append(item)
            if not name:
                sub.append(Gtk.SeparatorMenuItem())
        return sub

    def on_press(_w, ev):
        if ev.button == 1 and in_toggle(ev):
            toggle_labels()
        elif ev.button == 1 and in_chip(ev):
            menu = district_menu()
            menu.attach_to_widget(area)
            win.menu = menu     # keep a reference while it is open
            menu.show_all()
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = (int(v) for v in view.chip)
            menu.popup_at_rect(area.get_window(), rect,
                               Gdk.Gravity.SOUTH_EAST, Gdk.Gravity.NORTH_EAST, ev)
        elif ev.button == 1:
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
            for text, cb in (("Refresh now", lambda *_: refresh()),
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
                {"pos": list(win.get_position()), "district": view.district, "labels": view.labels}, ensure_ascii=False))
        except OSError:
            pass

    def quit_app(*_):
        save_state()
        Gtk.main_quit()

    area.connect("draw", lambda _w, ctx: render(ctx, view))
    area.connect("motion-notify-event", on_motion)
    area.connect("leave-notify-event", on_leave)
    area.connect("button-press-event", on_press)
    win.connect("delete-event", quit_app)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, quit_app)   # SIGTERM at logout

    try:
        x, y = saved["pos"]
        win.move(int(x), int(y))
    except Exception:
        pass

    win.show_all()
    refresh()
    GLib.timeout_add_seconds(REFRESH_MINUTES * 60, refresh)
    Gtk.main()


def snapshot(path: str, demo: bool, district: str | None = None, labels: bool = False) -> None:
    """Render one frame to a PNG without opening a window."""
    import cairo
    view = View(demo=demo, loading=False, district=district or "")
    _, _, mw, mh = map_rect()
    ensure_tiles(mw, mh, view.district)
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
    if not demo:
        geocoder.resolve(view.community)
    load_tile_surfaces(view)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH * 2, HEIGHT * 2)
    ctx = cairo.Context(surf)
    ctx.scale(2, 2)
    render(ctx, view)
    surf.write_to_png(path)


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
