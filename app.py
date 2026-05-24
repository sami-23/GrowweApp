import asyncio
import datetime
import json
import os
import re
import time

import aiohttp
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'sems-solar-reports-local-dev')

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), 'settings.json')
DEFAULT_SETTINGS = {
    'tariff': 50.0, 'currency': 'PKR', 'co2_factor': 0.40,
    'alert_threshold': 1000, 'timezone': 'Asia/Karachi', 'power_unit': 'W',
    'rated_kw': 0.0, 'inverter_kw': 0.0, 'latitude': 33.6007, 'longitude': 73.0679,
}

def load_settings():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            return {**DEFAULT_SETTINGS, **json.load(f)}
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(data):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def extract_watts(s):
    """Parse a value like '1484W' or '29.66W' into a float."""
    if s is None:
        return 0.0
    m = re.match(r'(-?\d+\.?\d*)', str(s).strip())
    return float(m.group(1)) if m else 0.0

LOGIN_URL = 'https://www.semsportal.com/api/v1/Common/CrossLogin'
LOGIN_TOKEN = '{"version":"v2.1.0","client":"ios","language":"en"}'
INTERVAL_H = 5 / 60  # 5-minute chart intervals in hours
TOKEN_TTL  = 7000    # re-login after ~2 hours (SEMS tokens expire around 3h)


# ── SEMS API helpers ──────────────────────────────────────────────────────────

async def sems_login(username: str, password: str) -> tuple[dict, str]:
    """Return (login_data, api_base_url)."""
    async with aiohttp.ClientSession() as sess:
        r = await sess.post(
            LOGIN_URL,
            headers={'Content-Type': 'application/json', 'Token': LOGIN_TOKEN},
            json={'account': username, 'pwd': password},
            timeout=aiohttp.ClientTimeout(total=10),
        )
        body = json.loads(await r.text(encoding='utf-8'))
    if body.get('hasError') or body.get('code') != 0:
        raise ValueError(body.get('msg', 'Login failed. Check your credentials.'))
    data = body['data']
    api = body.get('api', 'https://hk.semsportal.com/api/')
    return data, api


def _token_header(login_data: dict) -> dict:
    return {
        'Content-Type': 'application/json',
        'Token': json.dumps(login_data, separators=(',', ':')),
    }


async def get_station_id(sess: aiohttp.ClientSession, api: str, headers: dict) -> str:
    r = await sess.post(
        api + 'PowerStation/GetPowerStationIdByOwner',
        headers=headers, json={},
        timeout=aiohttp.ClientTimeout(total=10),
    )
    body = json.loads(await r.text(encoding='utf-8'))
    sid = body.get('data')
    if not sid:
        raise ValueError('No power station found on this account.')
    return str(sid)


async def get_station_name(sess: aiohttp.ClientSession, api: str, headers: dict, sid: str) -> str:
    r = await sess.post(
        api + 'v2/PowerStationMonitor/QueryPowerStationMonitorForApp',
        headers=headers, json={'powerStationId': sid},
        timeout=aiohttp.ClientTimeout(total=10),
    )
    body = json.loads(await r.text(encoding='utf-8'))
    items = body.get('data') or []
    if items:
        return items[0].get('stationname', 'Solar Plant')
    return 'Solar Plant'


async def fetch_day(
    sess: aiohttp.ClientSession, api: str, headers: dict, sid: str, date: datetime.date
) -> dict:
    """Fetch one day's chart. Returns {'solar_kwh', 'grid_kwh', 'points': [(time, pv_w, grid_w)]}."""
    date_str = date.strftime('%Y-%m-%d')
    r = await sess.post(
        api + 'v2/Charts/GetPlantPowerChart',
        headers=headers,
        json={'id': sid, 'date': date_str, 'full_script': False},
        timeout=aiohttp.ClientTimeout(total=15),
    )
    body = json.loads(await r.text(encoding='utf-8'))
    data = body.get('data') or {}

    # Solar total from generateData
    gen_data = {g['key']: g['value'] for g in data.get('generateData', [])}
    solar_kwh = float(gen_data.get('Generation', 0) or 0)

    # Grid import: PCurve_Power_Meter negative = importing from grid
    lines = {l['key']: l['xy'] for l in data.get('lines', [])}
    meter_xy = lines.get('PCurve_Power_Meter', [])
    pv_xy    = lines.get('PCurve_Power_PV', [])

    # Negative meter = grid import (confirmed: matches live powerflow values)
    grid_kwh = sum(
        abs(float(p['y'])) * INTERVAL_H / 1000
        for p in meter_xy if (p['y'] or 0) < 0
    )

    # Build point list for the daily chart (in kW)
    pv_map = {p['x']: float(p['y'] or 0) / 1000 for p in pv_xy}
    grid_map = {
        p['x']: abs(float(p['y'] or 0)) / 1000
        for p in meter_xy if (p['y'] or 0) < 0
    }
    all_times = sorted(set(pv_map) | set(grid_map))
    points = [(t, round(pv_map.get(t, 0), 3), round(grid_map.get(t, 0), 3)) for t in all_times]

    return {'solar_kwh': round(solar_kwh, 3), 'grid_kwh': round(grid_kwh, 3), 'points': points}


async def fetch_days_concurrent(
    api: str, login_data: dict, sid: str, dates: list[datetime.date],
    max_concurrent: int = 6,
) -> list[dict]:
    """Fetch multiple days with bounded concurrency to avoid SEMS rate-limiting."""
    headers = _token_header(login_data)
    sem = asyncio.Semaphore(max_concurrent)
    async with aiohttp.ClientSession() as sess:
        async def limited(d):
            async with sem:
                return await fetch_day(sess, api, headers, sid, d)
        results = await asyncio.gather(*[limited(d) for d in dates], return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, Exception):
            out.append({'solar_kwh': 0, 'grid_kwh': 0, 'points': []})
        else:
            out.append(r)
    return out


def run_async(coro):
    """Run an async coroutine from sync Flask context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Session token refresh ─────────────────────────────────────────────────────

def ensure_fresh_token():
    """Re-login with stored credentials if the session token is older than TOKEN_TTL."""
    if time.time() - session.get('login_time', 0) < TOKEN_TTL:
        return
    try:
        login_data, api_url = run_async(sems_login(session['username'], session['password']))
        session['login_data'] = login_data
        session['api_url']    = api_url
        session['login_time'] = time.time()
    except Exception:
        pass  # keep existing credentials and let the API call surface the error


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/', methods=['GET', 'POST'])
def index():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            error = 'Please enter both email and password.'
        else:
            try:
                login_data, api_url = run_async(sems_login(username, password))
                session['username'] = username
                session['password'] = password
                session['login_data'] = login_data
                session['api_url'] = api_url

                # Fetch station ID at login time so it's cached
                headers = _token_header(login_data)
                async def _get_station():
                    async with aiohttp.ClientSession() as sess:
                        sid  = await get_station_id(sess, api_url, headers)
                        name = await get_station_name(sess, api_url, headers, sid)
                    return sid, name
                sid, name = run_async(_get_station())
                session['station_id']   = sid
                session['station_name'] = name
                session['login_time']   = time.time()
                return redirect(url_for('report'))
            except Exception as e:
                error = str(e) or 'Login failed — check your credentials and try again.'
    return render_template('index.html', error=error)


@app.route('/report')
def report():
    if 'username' not in session:
        return redirect(url_for('index'))
    return render_template('report.html', username=session['username'])


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/api/report-data')
def report_data():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    ensure_fresh_token()
    try:
        login_data = session['login_data']
        api_url    = session['api_url']
        sid        = session['station_id']
        name       = session['station_name']
        date_str = request.args.get('date')
        period   = request.args.get('period', 'all')  # daily|weekly|monthly|yearly|all
        try:
            today = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
        except ValueError:
            today = datetime.date.today()

        import calendar

        week_dates  = [today - datetime.timedelta(days=i) for i in range(6, -1, -1)]
        month_dates = [today.replace(day=d) for d in range(1, today.day + 1)]
        year_dates  = []
        for m in range(1, today.month + 1):
            last_day = today.day if m == today.month else calendar.monthrange(today.year, m)[1]
            for d in range(1, last_day + 1):
                year_dates.append(datetime.date(today.year, m, d))

        fetch_daily   = period in ('daily',   'all')
        fetch_weekly  = period in ('weekly',  'all')
        fetch_monthly = period in ('monthly', 'all')
        fetch_yearly  = period in ('yearly',  'all')

        dates_needed: set = set()
        if fetch_daily:   dates_needed.add(today)
        if fetch_weekly:  dates_needed.update(week_dates)
        if fetch_monthly: dates_needed.update(month_dates)
        if fetch_yearly:  dates_needed.update(year_dates)

        all_dates = sorted(dates_needed)
        results   = run_async(fetch_days_concurrent(api_url, login_data, sid, all_dates))
        day_cache = {d: r for d, r in zip(all_dates, results)}

        def get(d):
            return day_cache.get(d, {'solar_kwh': 0, 'grid_kwh': 0, 'points': []})

        def day_hours(points):
            """Return (solar_h, grid_h) for a day's 5-min interval points."""
            THRESH = 0.05  # kW
            DT     = 5 / 60
            s = g  = 0.0
            for _, pv, grd in points:
                if pv  > THRESH: s += DT
                if grd > THRESH: g += DT
            return s, g

        def mk(labels, solar, grid, solar_total, grid_total, days_points=None):
            out = {
                'labels':      labels,
                'solar':       [round(v, 2) for v in solar],
                'grid':        [round(v, 2) for v in grid],
                'solar_total': round(solar_total, 2),
                'grid_total':  round(grid_total, 2),
            }
            if days_points is not None:
                sh = gh = 0.0
                for pts in days_points:
                    s, g = day_hours(pts)
                    sh += s; gh += g
                bh = max(0.0, len(days_points) * 24 - sh - gh)
                out['solar_hrs']  = round(sh, 2)
                out['grid_hrs']   = round(gh, 2)
                out['bat_hrs']    = round(bh, 2)
            return out

        resp = {'station_name': name, 'as_of': datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}

        if fetch_daily:
            today_data = get(today)
            resp['daily'] = mk(
                [p[0] for p in today_data['points']],
                [p[1] for p in today_data['points']],
                [p[2] for p in today_data['points']],
                today_data['solar_kwh'], today_data['grid_kwh'],
            )

        if fetch_weekly:
            w_solar  = [get(d)['solar_kwh'] for d in week_dates]
            w_grid   = [get(d)['grid_kwh']  for d in week_dates]
            w_points = [get(d)['points']    for d in week_dates]
            resp['weekly'] = mk([d.strftime('%a %d %b') for d in week_dates],
                                w_solar, w_grid, sum(w_solar), sum(w_grid), w_points)

        if fetch_monthly:
            m_solar  = [get(d)['solar_kwh'] for d in month_dates]
            m_grid   = [get(d)['grid_kwh']  for d in month_dates]
            m_points = [get(d)['points']    for d in month_dates]
            resp['monthly'] = mk([d.strftime('%d %b') for d in month_dates],
                                 m_solar, m_grid, sum(m_solar), sum(m_grid), m_points)

        if fetch_yearly:
            y_months = list(range(1, today.month + 1))
            y_labels, y_solar, y_grid = [], [], []
            for m in y_months:
                y_labels.append(datetime.date(today.year, m, 1).strftime('%b %Y'))
                last_day = today.day if m == today.month else calendar.monthrange(today.year, m)[1]
                month_ds = [datetime.date(today.year, m, d) for d in range(1, last_day + 1)]
                y_solar.append(round(sum(get(d)['solar_kwh'] for d in month_ds), 2))
                y_grid.append( round(sum(get(d)['grid_kwh']  for d in month_ds), 2))
            resp['yearly'] = mk(y_labels, y_solar, y_grid, sum(y_solar), sum(y_grid))

        return jsonify(resp)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/live')
def live_data():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    ensure_fresh_token()
    try:
        login_data = session['login_data']
        api_url    = session['api_url']
        sid        = session['station_id']
        headers    = _token_header(login_data)

        async def _fetch():
            async with aiohttp.ClientSession() as sess:
                r = await sess.post(
                    api_url + 'v2/PowerStation/GetPowerflow',
                    headers=headers,
                    json={'PowerStationId': sid},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                body = json.loads(await r.text(encoding='utf-8'))
                pf_body = body.get('data') or {}
                pf   = pf_body.get('powerflow') or {}

                r2 = await sess.post(
                    api_url + 'v2/PowerStationMonitor/QueryPowerStationMonitorForApp',
                    headers=headers,
                    json={'powerStationId': sid},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                body2  = json.loads(await r2.text(encoding='utf-8'))
                kpi    = (body2.get('data') or [{}])[0]

                # Try to get battery device details (temperature, SOH, count, etc.)
                bat_info = {}
                try:
                    r3 = await sess.post(
                        api_url + 'v2/DeviceDataServer/GetPowerStationCountAndCapacity',
                        headers=headers,
                        json={'powerStationId': sid},
                        timeout=aiohttp.ClientTimeout(total=8),
                    )
                    body3 = json.loads(await r3.text(encoding='utf-8'))
                    bat_info = (body3.get('data') or {})
                except Exception:
                    pass

            return pf, kpi, bat_info

        pf, kpi, bat_info = run_async(_fetch())

        grid_w      = extract_watts(pf.get('grid') or '0')
        grid_status = int(pf.get('gridStatus') or 1)

        # Extract battery details — GoodWe API uses various field names
        def _first(*keys, src=None, cast=None):
            for k in keys:
                v = (src or pf).get(k)
                if v is not None and str(v).strip() not in ('', '--', 'N/A'):
                    try: return cast(v) if cast else v
                    except Exception: pass
            return None

        bat_soh  = _first('soh', 'batterySoh', 'batSoh', cast=float)
        bat_temp = _first('temperature', 'batteryTemperature', 'batTemperature',
                          'tempBat', 'batteryTemp', cast=float)
        bat_volt = _first('voltage', 'batteryVoltage', 'batVoltage', cast=float)
        bat_cap  = _first('batteryCapcity', 'batteryCapacity', 'batCapcity', cast=float)
        # count / capacity sometimes come from the capacity endpoint
        bat_count = _first('batteryCount', 'batCount', 'count',
                           src={**pf, **(bat_info or {})}, cast=int)
        bat_total_cap = _first('totalCapacity', 'capacity', 'batTotalCap',
                               src={**pf, **(bat_info or {})}, cast=float)

        return jsonify({
            'pv_w':           extract_watts(pf.get('pv') or '0'),
            'grid_w':         grid_w,
            'grid_importing': grid_status >= 0,
            'load_w':         extract_watts(pf.get('load') or '0'),
            'battery_w':      extract_watts(pf.get('bettery') or '0'),
            'soc':            int(pf.get('soc') or 0),
            'bat_soh':        bat_soh,
            'bat_temp':       bat_temp,
            'bat_volt':       bat_volt,
            'bat_cap':        bat_cap,
            'bat_count':      bat_count,
            'bat_total_cap':  bat_total_cap,
            'eday_kwh':      float(kpi.get('eday', 0) or 0),
            'emonth_kwh':    float(kpi.get('emonth', 0) or 0),
            'etotal_kwh':    float(kpi.get('etotal', 0) or 0),
            'timestamp':     datetime.datetime.now().strftime('%H:%M:%S'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/csv')
def export_csv():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    ensure_fresh_token()
    try:
        import calendar as _cal, io, csv
        from flask import Response
        login_data = session['login_data']
        api_url    = session['api_url']
        sid        = session['station_id']
        s          = load_settings()
        period     = request.args.get('period', 'monthly')
        date_str   = request.args.get('date')
        try:
            ref = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
        except ValueError:
            ref = datetime.date.today()

        if period == 'daily':
            dates = [ref]
        elif period == 'weekly':
            dates = [ref - datetime.timedelta(days=i) for i in range(6, -1, -1)]
        elif period == 'yearly':
            dates = []
            for m in range(1, ref.month + 1):
                last = ref.day if m == ref.month else _cal.monthrange(ref.year, m)[1]
                for d in range(1, last + 1):
                    dates.append(datetime.date(ref.year, m, d))
        else:  # monthly
            dates = [ref.replace(day=d) for d in range(1, ref.day + 1)]

        results = run_async(fetch_days_concurrent(api_url, login_data, sid, dates))
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(['Date', 'Solar (kWh)', 'Grid (kWh)',
                    f'Savings ({s["currency"]})', 'CO2 Offset (kg)'])
        for date, r in zip(dates, results):
            w.writerow([date.isoformat(), r['solar_kwh'], r['grid_kwh'],
                        round(r['solar_kwh'] * s['tariff'], 2),
                        round(r['solar_kwh'] * s['co2_factor'], 2)])
        return Response(out.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition':
                                 f'attachment;filename=solar_{period}_{ref}.csv'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/solar-potential')
def solar_potential():
    s = load_settings()
    lat      = float(s.get('latitude',  33.6007) or 33.6007)
    lon      = float(s.get('longitude', 73.0679) or 73.0679)
    rated_kw = float(s.get('rated_kw',  0)       or 0)

    async def _fetch():
        url = (
            f'https://api.open-meteo.com/v1/forecast'
            f'?latitude={lat}&longitude={lon}'
            f'&current=shortwave_radiation,direct_radiation,diffuse_radiation,temperature_2m'
            f'&timezone=auto'
        )
        async with aiohttp.ClientSession() as sess:
            r = await sess.get(url, timeout=aiohttp.ClientTimeout(total=10))
            return json.loads(await r.text(encoding='utf-8'))

    try:
        data    = run_async(_fetch())
        cur     = data.get('current') or {}
        ghi     = float(cur.get('shortwave_radiation', 0) or 0)
        direct  = float(cur.get('direct_radiation',    0) or 0)
        diffuse = float(cur.get('diffuse_radiation',   0) or 0)

        if ghi < 30:
            sky = 'Night'
        elif ghi < 150 or (diffuse / max(ghi, 1)) > 0.75:
            sky = 'Overcast'
        elif (diffuse / max(ghi, 1)) > 0.40:
            sky = 'Partly Cloudy'
        else:
            sky = 'Clear Sky'

        potential_kw = round(rated_kw * ghi / 1000, 2) if rated_kw > 0 else None

        temp_c = cur.get('temperature_2m')
        return jsonify({
            'ghi_wm2':      round(ghi, 1),
            'direct_wm2':   round(direct, 1),
            'diffuse_wm2':  round(diffuse, 1),
            'sky':          sky,
            'potential_kw': potential_kw,
            'rated_kw':     rated_kw,
            'temperature_c': round(float(temp_c), 1) if temp_c is not None else None,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stations')
def stations_api():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    return jsonify({'stations': [{'id': session['station_id'], 'name': session['station_name']}],
                    'active': session['station_id']})


@app.route('/api/settings', methods=['GET', 'POST'])
def settings_api():
    if request.method == 'POST':
        data = request.get_json() or {}
        s = load_settings()
        for key in ('tariff', 'co2_factor', 'alert_threshold'):
            if key in data:
                try:
                    s[key] = float(data[key])
                except (TypeError, ValueError):
                    pass
        if 'currency' in data:
            s['currency'] = str(data['currency'])[:10].strip()
        if 'timezone' in data:
            s['timezone'] = str(data['timezone'])[:50].strip()
        if 'power_unit' in data and data['power_unit'] in ('W', 'kW'):
            s['power_unit'] = data['power_unit']
        for key in ('rated_kw', 'inverter_kw', 'latitude', 'longitude'):
            if key in data:
                try:
                    s[key] = float(data[key])
                except (TypeError, ValueError):
                    pass
        save_settings(s)
        return jsonify({'ok': True})
    return jsonify(load_settings())


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
