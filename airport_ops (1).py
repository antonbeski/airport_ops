#!/usr/bin/env python3
"""
AeroOps Lite - Terminal Airport Ops Intelligence Dashboard
============================================================
Free, live data sources:
  - adsb.lol (api.adsb.lol)  -> live aircraft state vectors (no API key)
  - Open-Meteo (api.open-meteo.com) -> live weather (no API key)
  - OurAirports (davidmegginson/ourairports-data on GitHub) -> static
    airport reference data (icao, iata, name, lat, lon, elevation, type)

Runs entirely in your terminal. No browser, no server, no database server.

Usage:
    python3 airport_ops.py                              # interactive search: pick an airport
    python3 airport_ops.py --icao VOCI                  # live dashboard, refreshes every 15s
    python3 airport_ops.py --icao KJFK --radius 40      # custom radius in nautical miles
    python3 airport_ops.py --icao EGLL --once           # single snapshot, no refresh loop
    python3 airport_ops.py --icao VOCI --interval 10    # custom refresh interval (seconds)

Dependencies (install once):
    pip install requests rich
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import io
import math
import sys
import time
from dataclasses import dataclass, field
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
AIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

ADSB_LOL_BASE = "https://api.adsb.lol/v2"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

HTTP_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

NM_TO_KM = 1.852
EARTH_RADIUS_KM = 6371.0

# Rough turnaround duration defaults (minutes) by aircraft size bucket
TURNAROUND_DEFAULTS = {
    "regional": 30,
    "narrowbody": 40,
    "widebody": 70,
    "unknown": 40,
}

# ---------------------------------------------------------------------------
# HTTP helper with retries (keeps the app resilient to transient failures)
# ---------------------------------------------------------------------------

def http_get_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT,
                                 headers={"User-Agent": "AeroOpsLite/1.0"})
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
                                 headers={"User-Agent": "AeroOpsLite/1.0"})
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    console.log(f"[yellow]Warning:[/yellow] request to {url} failed after {MAX_RETRIES} attempts: {last_error}")
    return None


# ---------------------------------------------------------------------------
# Geometry helpers
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


_CSV_MEMORY_CACHE: Optional[str] = None  # in-process cache so repeated lookups in one run are instant


def load_airports_csv() -> Optional[str]:
    """Download (or read from local/in-memory cache) the full OurAirports CSV.
    Cached to disk (~/.aeroops_lite/airports.csv) so it's only ever downloaded
    once per machine, and cached in-memory so a single process only parses
    the file's text off disk once."""
    global _CSV_MEMORY_CACHE
    if _CSV_MEMORY_CACHE is not None:
        return _CSV_MEMORY_CACHE

    csv_text = None
    if AIRPORTS_CSV_CACHE.exists():
        try:
            csv_text = AIRPORTS_CSV_CACHE.read_text(encoding="utf-8")
        except OSError:
            csv_text = None

    if csv_text is None:
        console.print("[dim]Downloading OurAirports reference dataset (one-time, ~10 MB)...[/dim]")
        csv_text = http_get_text(AIRPORTS_CSV_URL)
        if csv_text is None:
            console.print("[red]Could not download OurAirports data and no local cache exists.[/red]")
            return None
        try:
            AIRPORTS_CSV_CACHE.write_text(csv_text, encoding="utf-8")
        except OSError:
            pass  # non-fatal: we can still use the in-memory copy this run

    _CSV_MEMORY_CACHE = csv_text
    return csv_text


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
    """Load a single airport's reference data from OurAirports by exact ICAO/ident match."""
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
    """Fuzzy search the OurAirports dataset by ICAO, IATA, name, municipality
    or country. Returns raw CSV row dicts ranked by relevance, best first."""
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
            # small bonus for larger airports so major hubs sort first on ties
            if row.get("type") == "large_airport":
                score += 3
            elif row.get("type") == "medium_airport":
                score += 1
            scored.append((score, row))

    scored.sort(key=lambda pair: -pair[0])
    return [row for _, row in scored[:limit]]


def interactive_airport_search() -> Optional[AirportRef]:
    """Terminal search-as-you-go picker: type a query, see ranked matches,
    pick one by number, or refine the search. Returns the chosen AirportRef,
    or None if the user quits."""
    console.print(Panel(
        "[bold]AeroOps Lite[/bold] — search for an airport by ICAO, IATA, name, or city\n"
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
    seen_s: Optional[float]      # seconds since last message
    seen_pos_s: Optional[float]  # seconds since last position update
    messages: Optional[int]
    is_military: bool = False
    is_pia: bool = False
    distance_km: Optional[float] = field(default=None)
    bearing_from_airport: Optional[float] = field(default=None)


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
    """Aircraft within `radius_nm` nautical miles of a point (max 250 nm)."""
    radius_nm = min(radius_nm, 250)
    url = f"{ADSB_LOL_BASE}/point/{lat}/{lon}/{radius_nm}"
    data = http_get_json(url)
    if not data:
        return []
    return [_parse_aircraft(a) for a in data.get("ac", [])]


def fetch_military_near(lat: float, lon: float, radius_km: float) -> list[Aircraft]:
    """/v2/mil returns ALL military aircraft globally, so we filter client-side
    by distance to the airport."""
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
    """/v2/pia returns all PIA (Privacy ICAO Address) aircraft globally,
    filtered client-side by distance, same approach as military."""
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
# Open-Meteo: live weather
# ---------------------------------------------------------------------------

@dataclass
class WeatherNow:
    temperature_c: Optional[float]
    windspeed_kmh: Optional[float]
    winddirection_deg: Optional[float]
    weathercode: Optional[int]
    is_day: Optional[int]

    def risk_label(self) -> str:
        if self.windspeed_kmh is None:
            return "unknown"
        if self.windspeed_kmh >= 55 or (self.weathercode and self.weathercode >= 95):
            return "severe"
        if self.windspeed_kmh >= 35 or (self.weathercode and self.weathercode >= 61):
            return "moderate"
        return "low"


def fetch_weather(lat: float, lon: float) -> Optional[WeatherNow]:
    data = http_get_json(OPEN_METEO_BASE, params={
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
        "timezone": "auto",
    })
    if not data or "current_weather" not in data:
        return None
    cw = data["current_weather"]
    return WeatherNow(
        temperature_c=cw.get("temperature"),
        windspeed_kmh=cw.get("windspeed"),
        winddirection_deg=cw.get("winddirection"),
        weathercode=cw.get("weathercode"),
        is_day=cw.get("is_day"),
    )


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
        return "approach/departure"
    if dist_km < 50 and alt_ft < 10000:
        return "terminal-area"
    return "en-route-nearby"


def aircraft_class_bucket(category: Optional[str]) -> str:
    # ADS-B emitter category codes: A1 light, A2/A3 small-medium, A5 heavy
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


def build_dashboard(airport: AirportRef, aircraft: list[Aircraft], military: list[Aircraft],
                     pia: list[Aircraft], weather: Optional[WeatherNow],
                     errors: list[str]) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=1),
    )

    # --- Header / KPIs ---
    score = congestion_score(aircraft, weather)
    weather_str = "no data"
    if weather:
        weather_str = (f"{weather.temperature_c:.0f}°C, wind {weather.windspeed_kmh:.0f} km/h "
                        f"({weather.risk_label()})")
    header_text = (f"[bold]{airport.name}[/bold] ({airport.icao}"
                   + (f"/{airport.iata}" if airport.iata else "") + f")  |  "
                   f"Nearby aircraft: [bold]{len(aircraft)}[/bold]  |  "
                   f"Congestion score: [bold]{score}/100[/bold]  |  "
                   f"Weather: {weather_str}")
    layout["header"].update(Panel(header_text, box=box.ROUNDED))

    # --- Flight board (left) ---
    table = Table(title="Live Flight Board (adsb.lol)", box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("Callsign", style="cyan")
    table.add_column("Hex")
    table.add_column("Phase")
    table.add_column("Alt")
    table.add_column("GS (kt)")
    table.add_column("Dist (km)")
    table.add_column("Turn (min)")
    table.add_column("Squawk")

    sorted_ac = sorted(
        [a for a in aircraft if a.distance_km is not None],
        key=lambda a: a.distance_km,
    )[:25]

    for ac in sorted_ac:
        phase = classify_phase(ac, airport)
        turn = turnaround_estimate(ac, weather) if phase in ("on-stand/taxi", "approach/departure") else "-"
        squawk_style = "red bold" if ac.squawk in ("7500", "7600", "7700") else ""
        table.add_row(
            ac.flight,
            ac.hex,
            phase,
            fmt_alt(ac.alt_baro),
            f"{ac.ground_speed:.0f}" if ac.ground_speed else "-",
            f"{ac.distance_km:.1f}" if ac.distance_km is not None else "-",
            str(turn),
            f"[{squawk_style}]{ac.squawk}[/{squawk_style}]" if squawk_style else (ac.squawk or "-"),
        )
    if not sorted_ac:
        table.add_row("no aircraft currently reporting in range", "", "", "", "", "", "", "")
    layout["left"].update(table)

    # --- Right column: military/PIA + alerts ---
    right_layout = Layout()
    right_layout.split_column(
        Layout(name="special", ratio=1),
        Layout(name="alerts", ratio=1),
    )

    special_table = Table(title="Military & PIA nearby", box=box.SIMPLE, expand=True)
    special_table.add_column("Type")
    special_table.add_column("Hex")
    special_table.add_column("Dist (km)")
    for ac in sorted(military, key=lambda a: a.distance_km or 9e9)[:8]:
        special_table.add_row("MIL", ac.hex, f"{ac.distance_km:.1f}" if ac.distance_km else "-")
    for ac in sorted(pia, key=lambda a: a.distance_km or 9e9)[:8]:
        special_table.add_row("PIA", ac.hex, f"{ac.distance_km:.1f}" if ac.distance_km else "-")
    if not military and not pia:
        special_table.add_row("none detected", "", "")
    right_layout["special"].update(special_table)

    alert_lines = []
    emergency = [a for a in aircraft if a.squawk in ("7500", "7600", "7700")]
    for ac in emergency:
        code_meaning = {"7500": "hijack", "7600": "radio failure", "7700": "general emergency"}
        alert_lines.append(f"[red bold]EMERGENCY[/red bold] {ac.flight} squawking {ac.squawk} "
                            f"({code_meaning.get(ac.squawk, '')})")
    if weather and weather.risk_label() in ("moderate", "severe"):
        alert_lines.append(f"[yellow]Weather risk: {weather.risk_label()} "
                            f"(wind {weather.windspeed_kmh:.0f} km/h)[/yellow]")
    if score >= 70:
        alert_lines.append(f"[yellow]High congestion score: {score}/100[/yellow]")
    for err in errors:
        alert_lines.append(f"[dim red]{err}[/dim red]")
    if not alert_lines:
        alert_lines.append("[green]No active alerts[/green]")
    right_layout["alerts"].update(Panel("\n".join(alert_lines), title="Alerts", box=box.ROUNDED))

    layout["right"].update(right_layout)

    layout["footer"].update(Panel(
        f"[dim]Data: adsb.lol (live ADS-B, ODbL) · Open-Meteo (weather) · "
        f"OurAirports (reference data) · Ctrl+C to quit[/dim]",
        box=box.MINIMAL,
    ))
    return layout


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once(icao: str, radius_nm: int) -> tuple[Layout, bool]:
    errors = []
    airport = load_airport_reference(icao)
    if airport is None:
        console.print(f"[red]Airport '{icao}' not found in OurAirports reference data. "
                       f"Check the ICAO code, or run without --icao to search by name/city/IATA.[/red]")
        return None, False

    radius_km = radius_nm * NM_TO_KM

    # Fire all four independent network calls concurrently -- latency is now
    # bounded by the slowest single request instead of their sum.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        fut_aircraft = pool.submit(fetch_point_aircraft, airport.lat, airport.lon, radius_nm)
        fut_military = pool.submit(fetch_military_near, airport.lat, airport.lon, radius_km)
        fut_pia = pool.submit(fetch_pia_near, airport.lat, airport.lon, radius_km)
        fut_weather = pool.submit(fetch_weather, airport.lat, airport.lon)

        aircraft = fut_aircraft.result()
        military = fut_military.result()
        pia = fut_pia.result()
        weather = fut_weather.result()

    if not aircraft:
        errors.append("adsb.lol: no aircraft data returned (network issue or empty airspace)")
    if weather is None:
        errors.append("Open-Meteo: weather data unavailable")

    for ac in aircraft:
        if ac.lat is not None and ac.lon is not None:
            classify_phase(ac, airport)  # populates distance/bearing in place

    layout = build_dashboard(airport, aircraft, military, pia, weather, errors)
    return layout, True


def main():
    parser = argparse.ArgumentParser(description="AeroOps Lite - terminal airport ops dashboard")
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
        # No --icao given: drop into the interactive search-and-pick UI
        # instead of erroring out.
        chosen = interactive_airport_search()
        if chosen is None:
            console.print("[dim]No airport selected. Bye.[/dim]")
            sys.exit(0)
        icao = chosen.icao
        console.print(f"[green]Selected {chosen.name} ({chosen.icao}"
                      f"{'/' + chosen.iata if chosen.iata else ''})[/green]")

    if args.once:
        layout, ok = run_once(icao, args.radius)
        if ok:
            console.print(layout)
        sys.exit(0 if ok else 1)

    try:
        with Live(console=console, refresh_per_second=1, screen=True) as live:
            while True:
                layout, ok = run_once(icao, args.radius)
                if ok:
                    live.update(layout)
                else:
                    live.stop()
                    sys.exit(1)
                time.sleep(args.interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
        sys.exit(0)


if __name__ == "__main__":
    main()
