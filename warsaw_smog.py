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
WIDTH, HEIGHT = 360, 482

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
USER_AGENT = "warsaw-smog-widget/0.1 (personal desktop widget)"

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "warsaw-smog"
STATE_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "warsaw-smog.json"

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
    hover: int | None = None          # station id under the pointer
    demo: bool = False
    district: str = ""                # "" = whole city, else a key of DISTRICTS
    hits: list = field(default_factory=list)   # [(x, y, station)] from last draw
    chip: tuple = (0.0, 0.0, 0.0, 0.0)         # district selector rect from last draw
    chip_hover: bool = False
    tiles: dict = field(default_factory=dict)  # {(z, x, y): cairo surface}


def map_rect() -> tuple[float, float, float, float]:
    return 12.0, 62.0, WIDTH - 24.0, HEIGHT - 62.0 - 98.0


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
    # draw worst last so it stays on top where stations overlap
    for s in sorted(view.stations, key=lambda s: -1 if s.index is None else s.index):
        px, py = mercator_px(s.lat, s.lon, zoom)
        x, y = ox + px, oy + py
        if not (mx <= x <= mx + mw and my <= y <= my + mh):
            continue
        view.hits.append((x, y, s))
        hovered = s.id == view.hover
        ctx.arc(x, y, 9.5 if hovered else 7.5, 0, 2 * math.pi)
        ctx.set_source_rgb(*(INDEX[s.index][1] if s.index is not None else NO_INDEX[1]))
        ctx.fill_preserve()
        ctx.set_line_width(2.2 if hovered else 1.4)
        ctx.set_source_rgba(1, 1, 1, 0.95 if hovered else 0.75)
        ctx.stroke()
    ctx.restore()

    # detail line: hovered station; else in a district view the station nearest
    # its centre (most districts have none of their own); else the city mean
    y0 = my + mh + 20
    sel = next((s for s in view.stations if s.id == view.hover), None)
    note = ""
    if sel is None and view.district and view.stations:
        sel = min(ranked or view.stations,      # prefer one that is actually reporting
                  key=lambda s: distance_km(clat, clon, s.lat, s.lon))
        note = f"nearest · {distance_km(clat, clon, sel.lat, sel.lon):.1f} km from centre"
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

    # footer: status + attribution (required by both GIOŚ and OSM terms)
    if view.error:
        status, col = view.error, (0.95, 0.55, 0.45)
    elif view.updated:
        times = sorted(s.index_time for s in view.stations if s.index_time)
        data_t = f"data {times[-1][11:16]} · " if times else ""
        status, col = data_t + "checked " + time.strftime("%H:%M", time.localtime(view.updated)), dim
    else:
        status, col = "", dim
    _text(ctx, status, 14, H - 10, 9, col, maxw=W / 2 - 14)
    _text(ctx, "Data: GIOŚ · Map © OpenStreetMap", W - 14, H - 10, 9, dim, right=True)


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
            err, stations = "", None
            try:
                _, _, mw, mh = map_rect()
                ensure_tiles(mw, mh, view.district)
                if demo:
                    stations = demo_stations()
                else:
                    stations = load_metadata(CITY)
                    fetch_readings(stations)
            except RateLimited:
                err = "GIOŚ rate limit hit, retrying later"
            except Exception as e:
                err = f"{type(e).__name__}: {getattr(e, 'reason', e)}"

            def apply():
                view.loading = False
                view.error = err
                if stations is not None and not err:
                    view.stations, view.updated = stations, time.time()
                load_tile_surfaces(view)
                area.queue_draw()
            GLib.idle_add(apply)

        threading.Thread(target=work, daemon=True).start()
        return True     # keep the GLib timer running

    def on_motion(_w, ev):
        hit = min(view.hits, key=lambda h: (h[0] - ev.x) ** 2 + (h[1] - ev.y) ** 2, default=None)
        hid = hit[2].id if hit and (hit[0] - ev.x) ** 2 + (hit[1] - ev.y) ** 2 <= 14 ** 2 else None
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
        save_state()
        load_tile_surfaces(view)        # whatever is cached shows immediately
        area.queue_draw()

        def work():                     # fetch the rest off the UI thread
            _, _, mw, mh = map_rect()
            ensure_tiles(mw, mh, name)
            GLib.idle_add(lambda: (load_tile_surfaces(view), area.queue_draw()) and False)
        threading.Thread(target=work, daemon=True).start()

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
        if ev.button == 1 and in_chip(ev):
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
            for text, cb in (("Refresh now", lambda *_: refresh()), ("Quit", quit_app)):
                item = Gtk.MenuItem(label=text)
                item.connect("activate", cb)
                menu.append(item)
            win.menu = menu     # keep a reference while it is open
            menu.show_all()
            menu.popup_at_pointer(ev)

    def save_state():
        try:
            STATE_FILE.write_text(json.dumps(
                {"pos": list(win.get_position()), "district": view.district}, ensure_ascii=False))
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


def snapshot(path: str, demo: bool, district: str | None = None) -> None:
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
    args = ap.parse_args()
    if args.snapshot:
        snapshot(args.snapshot, args.demo, args.district)
    else:
        run_gtk(args.demo, args.district)


if __name__ == "__main__":
    sys.exit(main())
