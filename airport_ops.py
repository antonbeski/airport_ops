#!/usr/bin/env python3
"""
AeroOps Terminal - Live Airport Ops Intelligence Dashboard
============================================================
Free, live data sources (no API keys):
  - adsb.lol (api.adsb.lol)      -> live aircraft state vectors
  - Open-Meteo (api.open-meteo.com) -> live weather (current + sunrise/sunset)
  - OurAirports (GitHub)          -> static airport + runway reference data

Runs entirely in your terminal. No browser, no server, no database.

Usage:
    python3 airport_ops.py                              # interactive search: pick an airport
    python3 airport_ops.py --icao VOCI                  # live dashboard, refreshes every 15s
    python3 airport_ops.py --icao KJFK --radius 40      # custom radius in nautical miles
    python3 airport_ops.py --icao EGLL --once           # single snapshot, no refresh loop
    python3 airport_ops.py --icao VOCI --interval 10    # custom refresh interval (seconds)

While the live dashboard is running, use these hotkeys (no need to quit!):
    s   search for / switch to a different airport
    r   change the search radius
    i   change the refresh interval
    +/- nudge the refresh interval up/down by 5s
    p   pause / resume auto-refresh
    q   quit

Dependencies (install once):
    pip install requests rich
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import csv
import io
import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.live import Live
    from rich.text import Text
    from rich.align import Align
    from rich.prompt import Prompt
    from rich import box
except ImportError:
    print("Missing dependency. Install with:\n    pip install requests rich")
    sys.exit(1)

console = Console()

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".aeroops_lite"
CACHE_DIR.mkdir(exist_ok=True)
AIRPORTS_CSV_CACHE = CACHE_DIR / "airports.csv"
RUNWAYS_CSV_CACHE = CACHE_DIR / "runways.csv"
AIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RUNWAYS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"

ADSB_LOL_BASE = "https://api.adsb.lol/v2"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

HTTP_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

NM_TO_KM = 1.852
EARTH_RADIUS_KM = 6371.0
KT_TO_KMH = 1.852

MIN_INTERVAL = 5
MAX_INTERVAL = 120

# Rough turnaround duration defaults (minutes) by aircraft size bucket
TURNAROUND_DEFAULTS = {
    "regional": 30,
    "narrowbody": 40,
    "widebody": 70,
    "unknown": 40,
}

CATEGORY_LABELS = {
    "A0": "unknown", "A1": "light", "A2": "small", "A3": "large",
    "A4": "high-vortex", "A5": "heavy", "A6": "high-perf", "A7": "rotorcraft",
    "B0": "unknown", "B1": "glider", "B2": "lighter-than-air", "B3": "parachutist",
    "B4": "ultralight", "B6": "UAV", "B7": "space vehicle",
    "C0": "unknown", "C1": "emergency veh", "C2": "service veh",
    "C3": "point obstacle", "C4": "cluster obstacle", "C5": "line obstacle",
}

# WMO weather codes (subset) -> human-readable description
WEATHER_CODE_MAP = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight rain showers", 81: "Rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm w/ hail",
}

SPARK_CHARS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"  # ▁▂▃▄▅▆▇█

# ---------------------------------------------------------------------------
# HTTP helpers with retries (keeps the app resilient to transient failures)
# ---------------------------------------------------------------------------

def http_get_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT,
                                 headers={"User-Agent": "AeroOpsTerminal/2.0"})
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    console.log(f"[yellow]Warning:[/yellow] request to {url} failed after {MAX_RETRIES} attempts: {last_error}")
    return None


def http_get_text(url: str) -> Optional[str]:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT,
                                 headers={"User-Agent": "AeroOpsTerminal/2.0"})
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    console.log(f"[yellow]Warning:[/yellow] request to {url} failed after {MAX_RETRIES} attempts: {last_error}")
    return None


# ---------------------------------------------------------------------------
# Geometry / wind helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass_arrow(deg: Optional[float]) -> str:
    """Return a single-character arrow pointing in the direction wind blows TOWARD."""
    if deg is None:
        return "?"
    arrows = ["\u2191", "\u2197", "\u2192", "\u2198", "\u2193", "\u2199", "\u2190", "\u2196"]
    # wind "from" deg -> arrow shows direction it blows TOWARD (opposite), sailor/aviation convention
    toward = (deg + 180) % 360
    idx = int(((toward + 22.5) % 360) / 45)
    return arrows[idx]


def wind_components(runway_heading_deg: Optional[float], wind_from_deg: Optional[float],
                     wind_speed: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Return (headwind, crosswind) components in the same units as wind_speed.
    Positive headwind = wind aiding into-wind operations on that runway heading.
    Positive crosswind = crosswind from the right; sign only indicates side."""
    if runway_heading_deg is None or wind_from_deg is None or wind_speed is None:
        return None, None
    angle = math.radians(wind_from_deg - runway_heading_deg)
    headwind = wind_speed * math.cos(angle)
    crosswind = wind_speed * math.sin(angle)
    return headwind, crosswind


# ---------------------------------------------------------------------------
# OurAirports: static airport reference data (downloaded once, cached locally)
# ---------------------------------------------------------------------------

@dataclass
class AirportRef:
    icao: str
    iata: str
    name: str
    lat: float
    lon: float
    elevation_ft: Optional[float]
    airport_type: str
    country: str


_CSV_MEMORY_CACHE: Optional[str] = None
_RUNWAY_CSV_MEMORY_CACHE: Optional[str] = None


def _load_cached_csv(url: str, cache_path: Path, memory_cache_name: str) -> Optional[str]:
    global _CSV_MEMORY_CACHE, _RUNWAY_CSV_MEMORY_CACHE
    if memory_cache_name == "airports" and _CSV_MEMORY_CACHE is not None:
        return _CSV_MEMORY_CACHE
    if memory_cache_name == "runways" and _RUNWAY_CSV_MEMORY_CACHE is not None:
        return _RUNWAY_CSV_MEMORY_CACHE

    csv_text = None
    if cache_path.exists():
        try:
            csv_text = cache_path.read_text(encoding="utf-8")
        except OSError:
            csv_text = None

    if csv_text is None:
        console.print(f"[dim]Downloading {cache_path.name} reference dataset (one-time)...[/dim]")
        csv_text = http_get_text(url)
        if csv_text is None:
            console.print(f"[red]Could not download {cache_path.name} and no local cache exists.[/red]")
            return None
        try:
            cache_path.write_text(csv_text, encoding="utf-8")
        except OSError:
            pass

    if memory_cache_name == "airports":
        _CSV_MEMORY_CACHE = csv_text
    else:
        _RUNWAY_CSV_MEMORY_CACHE = csv_text
    return csv_text


def load_airports_csv() -> Optional[str]:
    return _load_cached_csv(AIRPORTS_CSV_URL, AIRPORTS_CSV_CACHE, "airports")


def load_runways_csv() -> Optional[str]:
    return _load_cached_csv(RUNWAYS_CSV_URL, RUNWAYS_CSV_CACHE, "runways")


def _row_to_airport_ref(row: dict) -> Optional[AirportRef]:
    try:
        return AirportRef(
            icao=row["ident"],
            iata=row.get("iata_code", "") or "",
            name=row.get("name", "Unknown"),
            lat=float(row["latitude_deg"]),
            lon=float(row["longitude_deg"]),
            elevation_ft=float(row["elevation_ft"]) if row.get("elevation_ft") else None,
            airport_type=row.get("type", "unknown"),
            country=row.get("iso_country", "??"),
        )
    except (KeyError, ValueError):
        return None


def load_airport_reference(icao: str) -> Optional[AirportRef]:
    csv_text = load_airports_csv()
    if csv_text is None:
        return None
    icao = icao.strip().upper()
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        if row.get("ident", "").upper() == icao:
            ref = _row_to_airport_ref(row)
            if ref:
                return ref
    return None


AIRPORT_TYPES_SEARCHABLE = ("large_airport", "medium_airport", "small_airport")


def search_airports(query: str, limit: int = 15) -> list[dict]:
    csv_text = load_airports_csv()
    if csv_text is None:
        return []
    q = query.strip().lower()
    if not q:
        return []

    scored: list[tuple[int, dict]] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        if row.get("type") not in AIRPORT_TYPES_SEARCHABLE:
            continue
        ident = (row.get("ident") or "").lower()
        iata = (row.get("iata_code") or "").lower()
        name = (row.get("name") or "").lower()
        muni = (row.get("municipality") or "").lower()
        country = (row.get("iso_country") or "").lower()

        score = 0
        if ident == q or iata == q:
            score = 100
        elif ident.startswith(q) or iata.startswith(q):
            score = 85
        elif name.startswith(q) or muni.startswith(q):
            score = 65
        elif q in ident or q in iata:
            score = 55
        elif q in name or q in muni:
            score = 35
        elif q in country:
            score = 15
        if score:
            if row.get("type") == "large_airport":
                score += 3
            elif row.get("type") == "medium_airport":
                score += 1
            scored.append((score, row))

    scored.sort(key=lambda pair: -pair[0])
    return [row for _, row in scored[:limit]]


def interactive_airport_search(prompt_title: str = "AeroOps Terminal") -> Optional[AirportRef]:
    console.print(Panel(
        f"[bold]{prompt_title}[/bold] — search for an airport by ICAO, IATA, name, or city\n"
        "[dim]Examples: VOCI · JFK · \"heathrow\" · \"paris\" — type q to quit[/dim]",
        box=box.ROUNDED,
    ))
    while True:
        query = Prompt.ask("[cyan]Search airport[/cyan]").strip()
        if query.lower() in ("q", "quit", "exit"):
            return None
        if not query:
            continue

        results = search_airports(query, limit=15)
        if not results:
            console.print("[yellow]No matches. Try a different ICAO/IATA code, name, or city.[/yellow]")
            continue

        table = Table(title=f"Matches for '{query}'", box=box.SIMPLE_HEAVY)
        table.add_column("#", justify="right")
        table.add_column("ICAO", style="cyan")
        table.add_column("IATA")
        table.add_column("Name")
        table.add_column("City")
        table.add_column("Country")
        for i, row in enumerate(results, start=1):
            table.add_row(
                str(i),
                row.get("ident", ""),
                row.get("iata_code", "") or "-",
                row.get("name", "Unknown"),
                row.get("municipality", "") or "-",
                row.get("iso_country", "??"),
            )
        console.print(table)

        choice = Prompt.ask(
            "[cyan]Pick a number[/cyan], press Enter to search again, or q to quit",
            default="",
        ).strip()
        if choice.lower() in ("q", "quit", "exit"):
            return None
        if not choice:
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(results):
            ref = _row_to_airport_ref(results[int(choice) - 1])
            if ref:
                return ref
            console.print("[red]That airport's reference data looks incomplete. Try another.[/red]")
        else:
            console.print("[yellow]Not a valid number — try again.[/yellow]")


# ---------------------------------------------------------------------------
# OurAirports: runway reference data + wind advisory
# ---------------------------------------------------------------------------

@dataclass
class RunwayRef:
    length_ft: Optional[float]
    width_ft: Optional[float]
    surface: str
    lighted: bool
    closed: bool
    le_ident: str
    le_heading_deg: Optional[float]
    he_ident: str
    he_heading_deg: Optional[float]


def _to_float(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def load_runways_for_airport(icao: str) -> list[RunwayRef]:
    csv_text = load_runways_csv()
    if csv_text is None:
        return []
    icao = icao.strip().upper()
    out = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        if (row.get("airport_ident") or "").upper() != icao:
            continue
        try:
            out.append(RunwayRef(
                length_ft=_to_float(row.get("length_ft")),
                width_ft=_to_float(row.get("width_ft")),
                surface=row.get("surface", "") or "unknown",
                lighted=row.get("lighted") == "1",
                closed=row.get("closed") == "1",
                le_ident=row.get("le_ident", "") or "-",
                le_heading_deg=_to_float(row.get("le_heading_degT")),
                he_ident=row.get("he_ident", "") or "-",
                he_heading_deg=_to_float(row.get("he_heading_degT")),
            ))
        except (KeyError, ValueError):
            continue
    return out


def runway_wind_advisory(runways: list[RunwayRef], wind_from_deg: Optional[float],
                          wind_speed_kmh: Optional[float]) -> list[dict]:
    """For each open runway, compute headwind/crosswind for both ends and
    recommend the better-aligned (more into-wind) end."""
    advisories = []
    if wind_from_deg is None or wind_speed_kmh is None:
        return advisories
    for rw in runways:
        if rw.closed:
            continue
        le_hw, le_cw = wind_components(rw.le_heading_deg, wind_from_deg, wind_speed_kmh)
        he_hw, he_cw = wind_components(rw.he_heading_deg, wind_from_deg, wind_speed_kmh)
        if le_hw is None and he_hw is None:
            continue
        # Prefer the end with the larger headwind (least tailwind)
        if le_hw is None:
            best = (rw.he_ident, he_hw, he_cw)
        elif he_hw is None:
            best = (rw.le_ident, le_hw, le_cw)
        else:
            best = (rw.le_ident, le_hw, le_cw) if le_hw >= he_hw else (rw.he_ident, he_hw, he_cw)
        advisories.append({
            "runway": f"{rw.le_ident}/{rw.he_ident}",
            "best_end": best[0],
            "headwind_kmh": best[1],
            "crosswind_kmh": abs(best[2]) if best[2] is not None else None,
            "length_ft": rw.length_ft,
            "surface": rw.surface,
        })
    advisories.sort(key=lambda a: -(a["headwind_kmh"] or -999))
    return advisories


# ---------------------------------------------------------------------------
# adsb.lol: live aircraft data
# ---------------------------------------------------------------------------

@dataclass
class Aircraft:
    hex: str
    flight: str
    lat: Optional[float]
    lon: Optional[float]
    alt_baro: Optional[object]  # number or "ground"
    ground_speed: Optional[float]
    track: Optional[float]
    vertical_rate: Optional[float]
    squawk: Optional[str]
    category: Optional[str]
    seen_s: Optional[float]
    seen_pos_s: Optional[float]
    messages: Optional[int]
    is_military: bool = False
    is_pia: bool = False
    distance_km: Optional[float] = field(default=None)
    bearing_from_airport: Optional[float] = field(default=None)

    def vertical_trend(self) -> str:
        if self.vertical_rate is None:
            return "\u2192"  # →  level/unknown
        if self.vertical_rate > 150:
            return "\u2191"  # ↑ climbing
        if self.vertical_rate < -150:
            return "\u2193"  # ↓ descending
        return "\u2192"  # → level

    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category or "", self.category or "-")


def _parse_aircraft(raw: dict, is_military=False, is_pia=False) -> Aircraft:
    return Aircraft(
        hex=raw.get("hex", "??????"),
        flight=(raw.get("flight") or raw.get("r") or "").strip() or "N/A",
        lat=raw.get("lat"),
        lon=raw.get("lon"),
        alt_baro=raw.get("alt_baro"),
        ground_speed=raw.get("gs"),
        track=raw.get("track"),
        vertical_rate=raw.get("baro_rate"),
        squawk=raw.get("squawk"),
        category=raw.get("category"),
        seen_s=raw.get("seen"),
        seen_pos_s=raw.get("seen_pos"),
        messages=raw.get("messages"),
        is_military=is_military,
        is_pia=is_pia,
    )


def fetch_point_aircraft(lat: float, lon: float, radius_nm: int) -> list[Aircraft]:
    radius_nm = min(radius_nm, 250)
    url = f"{ADSB_LOL_BASE}/point/{lat}/{lon}/{radius_nm}"
    data = http_get_json(url)
    if not data:
        return []
    return [_parse_aircraft(a) for a in data.get("ac", [])]


def fetch_military_near(lat: float, lon: float, radius_km: float) -> list[Aircraft]:
    data = http_get_json(f"{ADSB_LOL_BASE}/mil")
    if not data:
        return []
    out = []
    for raw in data.get("ac", []):
        if raw.get("lat") is None or raw.get("lon") is None:
            continue
        dist = haversine_km(lat, lon, raw["lat"], raw["lon"])
        if dist <= radius_km:
            ac = _parse_aircraft(raw, is_military=True)
            ac.distance_km = dist
            out.append(ac)
    return out


def fetch_pia_near(lat: float, lon: float, radius_km: float) -> list[Aircraft]:
    data = http_get_json(f"{ADSB_LOL_BASE}/pia")
    if not data:
        return []
    out = []
    for raw in data.get("ac", []):
        if raw.get("lat") is None or raw.get("lon") is None:
            continue
        dist = haversine_km(lat, lon, raw["lat"], raw["lon"])
        if dist <= radius_km:
            ac = _parse_aircraft(raw, is_pia=True)
            ac.distance_km = dist
            out.append(ac)
    return out


# ---------------------------------------------------------------------------
# Open-Meteo: live weather (current conditions + sunrise/sunset)
# ---------------------------------------------------------------------------

@dataclass
class WeatherNow:
    temperature_c: Optional[float]
    windspeed_kmh: Optional[float]
    winddirection_deg: Optional[float]
    wind_gusts_kmh: Optional[float]
    weathercode: Optional[int]
    humidity_pct: Optional[float]
    pressure_hpa: Optional[float]
    cloud_cover_pct: Optional[float]
    precipitation_mm: Optional[float]
    is_day: Optional[int]
    sunrise: Optional[str]
    sunset: Optional[str]

    def weather_description(self) -> str:
        if self.weathercode is None:
            return "unknown"
        return WEATHER_CODE_MAP.get(int(self.weathercode), f"code {self.weathercode}")

    def risk_label(self) -> str:
        if self.windspeed_kmh is None:
            return "unknown"
        gust = self.wind_gusts_kmh or 0
        wc = self.weathercode or 0
        precip = self.precipitation_mm or 0
        if self.windspeed_kmh >= 55 or gust >= 75 or wc >= 95 or precip >= 10:
            return "severe"
        if self.windspeed_kmh >= 35 or gust >= 50 or wc >= 61 or precip >= 2.5:
            return "moderate"
        return "low"


def fetch_weather(lat: float, lon: float) -> Optional[WeatherNow]:
    data = http_get_json(OPEN_METEO_BASE, params={
        "latitude": lat,
        "longitude": lon,
        "current": ("temperature_2m,relative_humidity_2m,precipitation,weather_code,"
                    "pressure_msl,cloud_cover,wind_speed_10m,wind_direction_10m,"
                    "wind_gusts_10m,is_day"),
        "daily": "sunrise,sunset",
        "timezone": "auto",
    })
    if not data:
        return None

    cur = data.get("current")
    daily = data.get("daily", {}) or {}
    sunrise = (daily.get("sunrise") or [None])[0]
    sunset = (daily.get("sunset") or [None])[0]

    if cur:
        return WeatherNow(
            temperature_c=cur.get("temperature_2m"),
            windspeed_kmh=cur.get("wind_speed_10m"),
            winddirection_deg=cur.get("wind_direction_10m"),
            wind_gusts_kmh=cur.get("wind_gusts_10m"),
            weathercode=cur.get("weather_code"),
            humidity_pct=cur.get("relative_humidity_2m"),
            pressure_hpa=cur.get("pressure_msl"),
            cloud_cover_pct=cur.get("cloud_cover"),
            precipitation_mm=cur.get("precipitation"),
            is_day=cur.get("is_day"),
            sunrise=sunrise,
            sunset=sunset,
        )

    # Fallback to legacy current_weather block if "current" wasn't returned
    cw = data.get("current_weather")
    if cw:
        return WeatherNow(
            temperature_c=cw.get("temperature"),
            windspeed_kmh=cw.get("windspeed"),
            winddirection_deg=cw.get("winddirection"),
            wind_gusts_kmh=None,
            weathercode=cw.get("weathercode"),
            humidity_pct=None,
            pressure_hpa=None,
            cloud_cover_pct=None,
            precipitation_mm=None,
            is_day=cw.get("is_day"),
            sunrise=sunrise,
            sunset=sunset,
        )
    return None


def fmt_iso_time(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "-"
    try:
        return iso_str.split("T")[1]
    except IndexError:
        return iso_str


# ---------------------------------------------------------------------------
# Risk engine (rules-based, explainable, no ML dependency)
# ---------------------------------------------------------------------------

def classify_phase(ac: Aircraft, airport: AirportRef) -> str:
    if ac.lat is None or ac.lon is None:
        return "no-position"
    dist_km = haversine_km(airport.lat, airport.lon, ac.lat, ac.lon)
    ac.distance_km = dist_km
    ac.bearing_from_airport = bearing_deg(airport.lat, airport.lon, ac.lat, ac.lon)

    alt = ac.alt_baro
    on_ground = alt == "ground"
    alt_ft = 0 if on_ground else (alt if isinstance(alt, (int, float)) else 99999)

    if on_ground and dist_km < 8:
        return "on-stand/taxi"
    if dist_km < 15 and alt_ft < 3000:
        trend = ac.vertical_trend()
        if trend == "\u2191":
            return "departure (climb)"
        if trend == "\u2193":
            return "approach (descent)"
        return "approach/departure"
    if dist_km < 50 and alt_ft < 10000:
        return "terminal-area"
    return "en-route-nearby"


def aircraft_class_bucket(category: Optional[str]) -> str:
    if not category:
        return "unknown"
    if category in ("A1",):
        return "regional"
    if category in ("A2", "A3"):
        return "narrowbody"
    if category in ("A4", "A5"):
        return "widebody"
    return "unknown"


def congestion_score(aircraft: list[Aircraft], weather: Optional[WeatherNow]) -> int:
    nearby = [a for a in aircraft if a.distance_km is not None and a.distance_km <= 100]
    recent_terminal = [a for a in nearby if a.distance_km <= 50]
    score = 0.0
    score += min(len(nearby) * 2.0, 40)
    score += min(len(recent_terminal) * 3.0, 40)
    if weather:
        risk = weather.risk_label()
        score += {"low": 0, "moderate": 10, "severe": 20, "unknown": 5}[risk]
    return int(min(round(score), 100))


def turnaround_estimate(ac: Aircraft, weather: Optional[WeatherNow]) -> int:
    bucket = aircraft_class_bucket(ac.category)
    base = TURNAROUND_DEFAULTS[bucket]
    buffer_min = 0
    if weather and weather.risk_label() == "moderate":
        buffer_min = 10
    elif weather and weather.risk_label() == "severe":
        buffer_min = 25
    return base + buffer_min


def traffic_statistics(aircraft: list[Aircraft]) -> dict:
    """Aggregate stats for the traffic-analytics panel."""
    stats = {
        "phase_counts": collections.Counter(),
        "category_counts": collections.Counter(),
        "climbing": 0, "descending": 0, "level": 0,
        "altitudes": [], "speeds": [],
    }
    for ac in aircraft:
        if ac.distance_km is None:
            continue
        trend = ac.vertical_trend()
        if trend == "\u2191":
            stats["climbing"] += 1
        elif trend == "\u2193":
            stats["descending"] += 1
        else:
            stats["level"] += 1
        stats["category_counts"][ac.category_label()] += 1
        if isinstance(ac.alt_baro, (int, float)):
            stats["altitudes"].append(ac.alt_baro)
        if ac.ground_speed:
            stats["speeds"].append(ac.ground_speed)
    return stats


def sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    return "".join(
        SPARK_CHARS[int((v - lo) / rng * (len(SPARK_CHARS) - 1))] for v in values
    )


# ---------------------------------------------------------------------------
# Rendering (rich terminal UI)
# ---------------------------------------------------------------------------

def fmt_alt(alt) -> str:
    if alt is None:
        return "-"
    if alt == "ground":
        return "GND"
    try:
        return f"{int(alt):,} ft"
    except (TypeError, ValueError):
        return str(alt)


def fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def build_dashboard(airport: AirportRef, aircraft: list[Aircraft], military: list[Aircraft],
                     pia: list[Aircraft], weather: Optional[WeatherNow],
                     runways: list[RunwayRef], errors: list[str],
                     session_start: float, refresh_count: int,
                     score_history: "collections.deque", count_history: "collections.deque",
                     radius_nm: int, interval: int, paused: bool) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=3),
        Layout(name="right", ratio=2),
    )
    layout["left"].split_column(
        Layout(name="flightboard", ratio=3),
        Layout(name="stats", ratio=1),
    )
    layout["right"].split_column(
        Layout(name="weather_runway", ratio=2),
        Layout(name="special", ratio=1),
        Layout(name="alerts", ratio=1),
    )

    # --- Header / KPIs ---
    score = congestion_score(aircraft, weather)
    score_history.append(score)
    count_history.append(len(aircraft))

    src_ok = lambda ok: "[green]\u25CF[/green]" if ok else "[red]\u25CF[/red]"
    status_str = (f"ADS-B {src_ok(bool(aircraft) or 'adsb.lol: no aircraft data returned (network issue or empty airspace)' not in errors)}  "
                  f"Wx {src_ok(weather is not None)}  "
                  f"RWY {src_ok(bool(runways))}")

    weather_str = "no data"
    if weather:
        weather_str = f"{weather.temperature_c:.0f}\u00b0C, wind {weather.windspeed_kmh:.0f} km/h ({weather.risk_label()})"

    day_night = ""
    if weather and weather.is_day is not None:
        day_night = " \u2600\ufe0f day" if weather.is_day == 1 else " \ud83c\udf19 night"

    header_text = (f"[bold]{airport.name}[/bold] ({airport.icao}"
                   + (f"/{airport.iata}" if airport.iata else "") + f")  |  "
                   f"Aircraft: [bold]{len(aircraft)}[/bold]  |  "
                   f"Congestion: [bold]{score}/100[/bold]  |  "
                   f"Wx: {weather_str}{day_night}  |  {status_str}"
                   + ("  |  [yellow]PAUSED[/yellow]" if paused else ""))
    layout["header"].update(Panel(header_text, box=box.ROUNDED))

    # --- Flight board (left top) ---
    table = Table(title="Live Flight Board (adsb.lol)", box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("Callsign", style="cyan")
    table.add_column("Hex")
    table.add_column("Phase")
    table.add_column("Trend", justify="center")
    table.add_column("Alt")
    table.add_column("GS (kt)")
    table.add_column("Dist (km)")
    table.add_column("Turn (min)")
    table.add_column("Type")
    table.add_column("Squawk")

    sorted_ac = sorted(
        [a for a in aircraft if a.distance_km is not None],
        key=lambda a: a.distance_km,
    )[:25]

    for ac in sorted_ac:
        phase = classify_phase(ac, airport)
        turn = turnaround_estimate(ac, weather) if phase in (
            "on-stand/taxi", "approach/departure", "approach (descent)", "departure (climb)") else "-"
        squawk_style = "red bold" if ac.squawk in ("7500", "7600", "7700") else ""
        table.add_row(
            ac.flight,
            ac.hex,
            phase,
            ac.vertical_trend(),
            fmt_alt(ac.alt_baro),
            f"{ac.ground_speed:.0f}" if ac.ground_speed else "-",
            f"{ac.distance_km:.1f}" if ac.distance_km is not None else "-",
            str(turn),
            ac.category_label(),
            f"[{squawk_style}]{ac.squawk}[/{squawk_style}]" if squawk_style else (ac.squawk or "-"),
        )
    if not sorted_ac:
        table.add_row("no aircraft currently reporting in range", "", "", "", "", "", "", "", "", "")
    layout["flightboard"].update(table)

    # --- Traffic statistics (left bottom) ---
    stats = traffic_statistics(aircraft)
    stats_table = Table(title="Traffic Analytics", box=box.SIMPLE, expand=True)
    stats_table.add_column("Metric")
    stats_table.add_column("Value")
    stats_table.add_row("Climbing / Level / Descending",
                         f"[green]{stats['climbing']}\u2191[/green] / {stats['level']}\u2192 / "
                         f"[yellow]{stats['descending']}\u2193[/yellow]")
    if stats["altitudes"]:
        stats_table.add_row("Altitude avg / min / max",
                             f"{sum(stats['altitudes'])/len(stats['altitudes']):,.0f} / "
                             f"{min(stats['altitudes']):,.0f} / {max(stats['altitudes']):,.0f} ft")
    if stats["speeds"]:
        stats_table.add_row("Ground speed avg / max",
                             f"{sum(stats['speeds'])/len(stats['speeds']):.0f} / "
                             f"{max(stats['speeds']):.0f} kt")
    top_categories = ", ".join(f"{k} ({v})" for k, v in stats["category_counts"].most_common(4)) or "-"
    stats_table.add_row("Top aircraft types", top_categories)
    stats_table.add_row("Congestion trend", f"{sparkline(list(score_history))} ({score}/100)")
    stats_table.add_row("Traffic count trend", f"{sparkline(list(count_history))} ({len(aircraft)})")
    layout["stats"].update(stats_table)

    # --- Weather & runway wind advisory (right top) ---
    wr_lines = []
    if weather:
        arrow = compass_arrow(weather.winddirection_deg)
        wr_lines.append(f"[bold]{weather.weather_description()}[/bold]  {day_night.strip()}")
        wr_lines.append(f"Temp: {weather.temperature_c:.0f}\u00b0C   "
                         f"Humidity: {weather.humidity_pct if weather.humidity_pct is not None else '-'}%   "
                         f"Pressure: {weather.pressure_hpa if weather.pressure_hpa is not None else '-'} hPa")
        wr_lines.append(f"Wind: {arrow} {weather.windspeed_kmh:.0f} km/h from {weather.winddirection_deg:.0f}\u00b0"
                         + (f"  (gusts {weather.wind_gusts_kmh:.0f} km/h)" if weather.wind_gusts_kmh else ""))
        wr_lines.append(f"Cloud cover: {weather.cloud_cover_pct if weather.cloud_cover_pct is not None else '-'}%   "
                         f"Precip: {weather.precipitation_mm if weather.precipitation_mm is not None else '-'} mm")
        wr_lines.append(f"Sunrise {fmt_iso_time(weather.sunrise)}  \u00b7  Sunset {fmt_iso_time(weather.sunset)}")
    else:
        wr_lines.append("[dim]No weather data available[/dim]")

    wr_lines.append("")
    advisories = runway_wind_advisory(runways, weather.winddirection_deg if weather else None,
                                       weather.windspeed_kmh if weather else None)
    if advisories:
        rwy_table = Table(box=box.MINIMAL, show_edge=False, pad_edge=False)
        rwy_table.add_column("Runway")
        rwy_table.add_column("Best end")
        rwy_table.add_column("Headwind")
        rwy_table.add_column("Crosswind")
        rwy_table.add_column("Length")
        for adv in advisories[:6]:
            cw = adv["crosswind_kmh"]
            cw_style = "red bold" if cw and cw >= 37 else ("yellow" if cw and cw >= 20 else "")
            rwy_table.add_row(
                adv["runway"],
                adv["best_end"],
                f"{adv['headwind_kmh']:+.0f} km/h",
                f"[{cw_style}]{cw:.0f} km/h[/{cw_style}]" if cw is not None and cw_style else f"{cw:.0f} km/h" if cw is not None else "-",
                f"{adv['length_ft']:,.0f} ft" if adv["length_ft"] else "-",
            )
    elif runways:
        rwy_table = Text("Runway data available but wind data missing.", style="dim")
    else:
        rwy_table = Text("No runway reference data for this airport.", style="dim")

    wr_panel = Table.grid(expand=True)
    wr_panel.add_row(Text.from_markup("\n".join(wr_lines)))
    wr_panel.add_row(rwy_table)
    layout["weather_runway"].update(Panel(wr_panel, title="Weather & Runway Wind Advisory", box=box.ROUNDED))

    # --- Military / PIA (right middle) ---
    special_table = Table(title="Military & PIA nearby", box=box.SIMPLE, expand=True)
    special_table.add_column("Type")
    special_table.add_column("Hex")
    special_table.add_column("Dist (km)")
    for ac in sorted(military, key=lambda a: a.distance_km or 9e9)[:6]:
        special_table.add_row("MIL", ac.hex, f"{ac.distance_km:.1f}" if ac.distance_km else "-")
    for ac in sorted(pia, key=lambda a: a.distance_km or 9e9)[:6]:
        special_table.add_row("PIA", ac.hex, f"{ac.distance_km:.1f}" if ac.distance_km else "-")
    if not military and not pia:
        special_table.add_row("none detected", "", "")
    layout["special"].update(special_table)

    # --- Alerts (right bottom) ---
    alert_lines = []
    emergency = [a for a in aircraft if a.squawk in ("7500", "7600", "7700")]
    code_meaning = {"7500": "hijack", "7600": "radio failure", "7700": "general emergency"}
    for ac in emergency:
        alert_lines.append(f"[red bold]EMERGENCY[/red bold] {ac.flight} squawking {ac.squawk} "
                            f"({code_meaning.get(ac.squawk, '')})")
    if weather and weather.risk_label() in ("moderate", "severe"):
        alert_lines.append(f"[yellow]Weather risk: {weather.risk_label()} "
                            f"(wind {weather.windspeed_kmh:.0f} km/h)[/yellow]")
    if advisories and advisories[0]["crosswind_kmh"] and advisories[0]["crosswind_kmh"] >= 37:
        alert_lines.append(f"[red]Strong crosswind on best runway: "
                            f"{advisories[0]['crosswind_kmh']:.0f} km/h[/red]")
    if score >= 70:
        alert_lines.append(f"[yellow]High congestion score: {score}/100[/yellow]")
    for err in errors:
        alert_lines.append(f"[dim red]{err}[/dim red]")
    if not alert_lines:
        alert_lines.append("[green]No active alerts[/green]")
    layout["alerts"].update(Panel("\n".join(alert_lines), title="Alerts", box=box.ROUNDED))

    # --- Footer / command bar ---
    uptime = fmt_uptime(time.time() - session_start)
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    footer_text = (f"[dim]{now_str} \u00b7 refresh #{refresh_count} \u00b7 uptime {uptime} \u00b7 "
                   f"radius {radius_nm}nm \u00b7 interval {interval}s \u00b7 "
                   f"adsb.lol (ODbL) \u00b7 Open-Meteo \u00b7 OurAirports[/dim]\n"
                   f"[bold]Keys:[/bold] [cyan]s[/cyan]=search airport  [cyan]r[/cyan]=radius  "
                   f"[cyan]i[/cyan]=interval  [cyan]+/-[/cyan]=nudge interval  "
                   f"[cyan]p[/cyan]=pause  [cyan]q[/cyan]=quit")
    layout["footer"].update(Panel(footer_text, box=box.MINIMAL))
    return layout


# ---------------------------------------------------------------------------
# Non-blocking keyboard listener (enables hotkeys without stopping refresh)
# ---------------------------------------------------------------------------

class KeyListener:
    """Reads single keypresses from stdin in a background thread without
    blocking the main render loop. Falls back to a no-op if stdin isn't a
    real terminal (e.g. output is piped)."""

    def __init__(self):
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._supported = sys.stdin.isatty()
        self._old_settings = None
        self._posix = os.name == "posix"

    def start(self):
        if not self._supported:
            return
        if self._posix:
            import termios
            import tty
            self._old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        if self._posix:
            import select
            while not self._stop_event.is_set():
                try:
                    ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                    if ready:
                        ch = sys.stdin.read(1)
                        if ch:
                            self._queue.put(ch)
                except Exception:
                    return
        else:
            try:
                import msvcrt
            except ImportError:
                return
            while not self._stop_event.is_set():
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    self._queue.put(ch)
                else:
                    time.sleep(0.1)

    def get_nowait(self) -> Optional[str]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def stop(self):
        self._stop_event.set()
        if self._supported and self._posix and self._old_settings is not None:
            import termios
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main data-fetch + dashboard assembly
# ---------------------------------------------------------------------------

def run_once(icao: str, radius_nm: int, session_start: float, refresh_count: int,
             score_history: "collections.deque", count_history: "collections.deque",
             radius_nm_display: int, interval: int, paused: bool) -> tuple[Optional[Layout], bool]:
    errors = []
    airport = load_airport_reference(icao)
    if airport is None:
        console.print(f"[red]Airport '{icao}' not found in OurAirports reference data. "
                       f"Check the ICAO code, or run without --icao to search by name/city/IATA.[/red]")
        return None, False

    radius_km = radius_nm * NM_TO_KM

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        fut_aircraft = pool.submit(fetch_point_aircraft, airport.lat, airport.lon, radius_nm)
        fut_military = pool.submit(fetch_military_near, airport.lat, airport.lon, radius_km)
        fut_pia = pool.submit(fetch_pia_near, airport.lat, airport.lon, radius_km)
        fut_weather = pool.submit(fetch_weather, airport.lat, airport.lon)

        aircraft = fut_aircraft.result()
        military = fut_military.result()
        pia = fut_pia.result()
        weather = fut_weather.result()

    runways = load_runways_for_airport(icao)

    if not aircraft:
        errors.append("adsb.lol: no aircraft data returned (network issue or empty airspace)")
    if weather is None:
        errors.append("Open-Meteo: weather data unavailable")

    for ac in aircraft:
        if ac.lat is not None and ac.lon is not None:
            classify_phase(ac, airport)

    layout = build_dashboard(airport, aircraft, military, pia, weather, runways, errors,
                              session_start, refresh_count, score_history, count_history,
                              radius_nm_display, interval, paused)
    return layout, True


def run_live_dashboard(icao: str, radius_nm: int, interval: int, key_listener: KeyListener) -> tuple[str, int, int]:
    """Runs the Live dashboard for a single airport until the user requests a
    search/radius/interval change or quit. Returns (command, radius_nm, interval)."""
    session_start = time.time()
    refresh_count = 0
    paused = False
    score_history: "collections.deque" = collections.deque(maxlen=40)
    count_history: "collections.deque" = collections.deque(maxlen=40)
    next_refresh = 0.0

    with Live(console=console, refresh_per_second=4, screen=True) as live:
        while True:
            now = time.time()
            if not paused and now >= next_refresh:
                layout, ok = run_once(icao, radius_nm, session_start, refresh_count,
                                       score_history, count_history, radius_nm, interval, paused)
                if not ok:
                    return "quit", radius_nm, interval
                refresh_count += 1
                live.update(layout)
                next_refresh = now + interval

            ch = key_listener.get_nowait()
            if ch:
                if ch in ("q", "Q"):
                    return "quit", radius_nm, interval
                if ch in ("s", "S"):
                    return "search", radius_nm, interval
                if ch in ("r", "R"):
                    return "radius", radius_nm, interval
                if ch in ("i", "I"):
                    return "interval", radius_nm, interval
                if ch == "+":
                    interval = min(interval + 5, MAX_INTERVAL)
                elif ch == "-":
                    interval = max(interval - 5, MIN_INTERVAL)
                elif ch in ("p", "P"):
                    paused = not paused

            time.sleep(0.15)


def main():
    parser = argparse.ArgumentParser(description="AeroOps Terminal - live airport ops dashboard")
    parser.add_argument("--icao", required=False, default=None,
                         help="ICAO airport code, e.g. VOCI, KJFK, EGLL. "
                              "Omit to launch the interactive airport search.")
    parser.add_argument("--radius", type=int, default=50, help="Radius in nautical miles (max 250, default 50)")
    parser.add_argument("--interval", type=int, default=15, help="Refresh interval in seconds (default 15)")
    parser.add_argument("--once", action="store_true", help="Fetch a single snapshot and exit (no live refresh)")
    args = parser.parse_args()

    if args.icao:
        icao = args.icao.strip().upper()
    else:
        chosen = interactive_airport_search()
        if chosen is None:
            console.print("[dim]No airport selected. Bye.[/dim]")
            sys.exit(0)
        icao = chosen.icao
        console.print(f"[green]Selected {chosen.name} ({chosen.icao}"
                      f"{'/' + chosen.iata if chosen.iata else ''})[/green]")

    if args.once:
        layout, ok = run_once(icao, args.radius, time.time(), 0,
                               collections.deque(maxlen=1), collections.deque(maxlen=1),
                               args.radius, args.interval, False)
        if ok:
            console.print(layout)
        sys.exit(0 if ok else 1)

    radius_nm = max(1, min(args.radius, 250))
    interval = max(MIN_INTERVAL, min(args.interval, MAX_INTERVAL))

    key_listener = KeyListener()
    key_listener.start()
    if not key_listener._supported:
        console.print("[dim]Note: hotkeys need an interactive terminal (TTY) — "
                       "running without live hotkey support.[/dim]")

    try:
        while True:
            cmd, radius_nm, interval = run_live_dashboard(icao, radius_nm, interval, key_listener)

            if cmd == "quit":
                break

            elif cmd == "search":
                chosen = interactive_airport_search("Switch airport")
                if chosen is None:
                    console.print("[dim]Keeping current airport. Press s to search again, q to quit.[/dim]")
                    time.sleep(1.0)
                else:
                    icao = chosen.icao
                    console.print(f"[green]Switched to {chosen.name} ({chosen.icao}"
                                  f"{'/' + chosen.iata if chosen.iata else ''})[/green]")
                    time.sleep(0.6)

            elif cmd == "radius":
                new_radius = Prompt.ask(
                    f"[cyan]New search radius in nautical miles[/cyan] (1-250, current {radius_nm})",
                    default=str(radius_nm),
                ).strip()
                if new_radius.isdigit():
                    radius_nm = max(1, min(int(new_radius), 250))

            elif cmd == "interval":
                new_interval = Prompt.ask(
                    f"[cyan]New refresh interval in seconds[/cyan] "
                    f"({MIN_INTERVAL}-{MAX_INTERVAL}, current {interval})",
                    default=str(interval),
                ).strip()
                if new_interval.isdigit():
                    interval = max(MIN_INTERVAL, min(int(new_interval), MAX_INTERVAL))

    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
    finally:
        key_listener.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
