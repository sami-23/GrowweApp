# Solar Reports

A Flask web app for monitoring a GoodWe solar system through the **SEMS Portal**
API. It provides a live power-flow dashboard, daily/weekly/monthly reports,
savings & environmental-impact estimates, and CSV/PDF export.

Current version: **v1.2**

## Features

- **Live monitoring** — real-time PV, grid, load, and battery SOC tiles
  (auto-refresh every 30 s), animated SVG power-flow diagram, color-coded
  tiles, configurable grid-import push alerts.
- **Reports** — Daily (5-min line/bar/donut), Weekly (last 7 days), and Monthly
  (day-by-day) charts with stat cards, self-sufficiency %, and a power-source
  breakdown (solar / grid / battery hours). Progressive per-period loading.
  - The **Monthly** tab defaults to the **last completed month** (selectable up
    to the current month) and spans the full month.
- **Savings & impact** — cost saved vs full grid, grid cost, CO₂ offset,
  best/worst day, and a monthly bill estimator.
- **Export** — CSV (`/api/export/csv`) and PDF (browser print with print CSS).
- **Settings** — electricity tariff, CO₂ factor, currency, grid alert threshold,
  timezone, rated/inverter kW, and location (lat/long) for solar potential.
- **PWA** — installable with an offline app shell.

## Tech stack

- Python 3.12, Flask, aiohttp (concurrent async fetches)
- Chart.js 4.4 (frontend), vanilla JS
- GoodWe SEMS Portal API (`hk.semsportal.com`), Open-Meteo for solar potential

## Running locally

```bash
pip install -r requirements.txt
python app.py
```

The app serves on `http://localhost:5000` (override with the `PORT` env var).
Set `SECRET_KEY` in production. Log in with your SEMS Portal email and password
— credentials are kept in the browser session only.

## Deployment

Configured for Railway / gunicorn via `Procfile` and `runtime.txt`. Reads
`PORT` and `SECRET_KEY` from the environment.

## Project files

| File              | Purpose                                            |
|-------------------|----------------------------------------------------|
| `app.py`          | Flask app, SEMS API helpers, all routes            |
| `templates/`      | `index.html` (login), `report.html` (the SPA)      |
| `static/`         | PWA manifest, service worker, icons                |
| `settings.json`   | Persisted user settings (tariff, CO₂, location, …) |
| `updatelog.txt`   | Version-by-version change log                       |
| `todo.txt`        | Feature checklist                                   |

## Notes

- The SEMS API is unofficial/undocumented. There is no dedicated monthly/yearly
  endpoint, so per-day chart calls are aggregated client-side.
- Sign convention: a negative `PCurve_Power_Meter` value means grid import.
