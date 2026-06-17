# SolarGrowwe — web dashboard

A single-file, lightweight website that auto-logs into the SEMS Portal and shows
the live power-flow diagram (Solar / Grid / Load / Battery) plus an energy panel
(today / yesterday / this & last month). All figures come straight from the SEMS
API — nothing is summed locally.

## Run locally

```bash
pip install -r requirements.txt
python web.py            # → http://127.0.0.1:8000
```

Credentials are read from `config.json` (next to `web.py`) when running locally:

```json
{ "email": "...", "password": "...", "station_id": "..." }
```

`config.json` is gitignored — it is never committed.

## Host it (Render / Railway / Fly / VPS)

1. Deploy this `web/` folder.
2. Set environment variables (these override `config.json`):
   - `SEMS_EMAIL`
   - `SEMS_PASSWORD`
   - `SEMS_STATION_ID` (optional)
3. Start command (see `Procfile`):

   ```bash
   gunicorn web:app
   ```

The server caches live data for ~8s and the energy figures for ~5min, so it
stays light even with several viewers.

> Note: the page is unauthenticated — anyone with the URL can see your system.
