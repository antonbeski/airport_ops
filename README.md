# AeroOps Lite

A free, live, terminal-based airport operations dashboard. No API keys, no browser, no server.

```
python airport_ops.py
```

![status](https://img.shields.io/badge/status-active-brightgreen)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

## Features

- **Interactive airport search** — search by ICAO, IATA, airport name, or city; pick from ranked results
- **Live aircraft board** — real-time state vectors around any airport (position, altitude, speed, squawk, flight phase)
- **Military & PIA aircraft tracking** — nearby military and privacy-ICAO-address aircraft
- **Live weather** — temperature, wind speed/direction, and a derived operational risk label
- **Congestion score** — a 0–100 score blending nearby traffic density and weather risk
- **Turnaround estimates** — rough gate turnaround times by aircraft size, adjusted for weather
- **Emergency squawk alerts** — flags 7500 (hijack), 7600 (radio failure), 7700 (general emergency)
- **Low latency** — all live data sources are fetched concurrently, so each refresh is as fast as the slowest single request
- **Live-refreshing terminal UI** built with [rich](https://github.com/Textualize/rich)

## Data sources (all free, no API key required)

| Source | Data |
|---|---|
| [adsb.lol](https://api.adsb.lol) | Live ADS-B aircraft state vectors |
| [Open-Meteo](https://open-meteo.com) | Live weather |
| [OurAirports](https://github.com/davidmegginson/ourairports-data) | Static airport reference data (ICAO/IATA, name, coordinates) |

## Install

```bash
git clone https://github.com/<your-username>/aeroops-lite.git
cd aeroops-lite
pip install -r requirements.txt
```

Requires Python 3.9+.

## Usage

```bash
# Interactive search — pick an airport from live search results
python airport_ops.py

# Jump straight to a known airport
python airport_ops.py --icao KJFK

# Custom search radius (nautical miles, max 250)
python airport_ops.py --icao EGLL --radius 100

# Custom refresh interval (seconds)
python airport_ops.py --icao VOCI --interval 10

# Single snapshot, no live refresh loop
python airport_ops.py --icao VOCI --once
```

Press `Ctrl+C` to quit the live dashboard.

On first run, the app downloads a one-time ~10 MB airport reference dataset and caches it locally in `~/.aeroops_lite/` (or `%USERPROFILE%\.aeroops_lite\` on Windows), so subsequent runs and searches are instant.

## How it works

- Airport lookup and search run against a locally cached copy of the OurAirports dataset — no repeated downloads.
- Each dashboard refresh fires the aircraft, military, PIA, and weather requests concurrently via a thread pool, so total latency is bounded by the slowest single call rather than their sum.
- Flight phase (on-stand/taxi, approach/departure, terminal-area, en-route-nearby) is derived from distance to the airport and barometric altitude.
- The congestion score and turnaround estimates are simple, explainable, rules-based calculations — no ML dependency.

## License

MIT — see [LICENSE](LICENSE).
