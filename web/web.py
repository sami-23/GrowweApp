"""
SolarGrowwe lightweight website
===============================
A single-file Flask app that auto-logs into the SEMS Portal and shows the live
power-flow diagram (Solar / Grid / Load / Battery) plus an energy panel
(today / yesterday / this & last month), in any browser. Designed to be hosted
anywhere (Render, Railway, Fly, a VPS, …) or run locally.

  /            -> the dashboard page (HTML + a little vanilla JS, no libraries)
  /api/data    -> JSON the page polls every few seconds

Credentials are read from environment variables first, then config.json next to
this file as a fallback for local use:

  SEMS_EMAIL, SEMS_PASSWORD, SEMS_STATION_ID   (station id is optional)

All energy figures come straight from the SEMS API (never summed locally).

Run locally:   python web.py            (http://127.0.0.1:8000)
Host:          gunicorn web:app         (see Procfile.web)
"""

import datetime
import json
import os
import re
import threading
import time
import urllib.request

from flask import Flask, jsonify, render_template_string

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, 'config.json')

LOGIN_URL = 'https://hk.semsportal.com/api/v1/Common/CrossLogin'
LOGIN_TOKEN = '{"version":"v2.1.0","client":"ios","language":"en"}'
TOKEN_TTL = 6000    # re-login after ~100 min (SEMS tokens last ~3h)
LIVE_TTL = 8        # cache the live power-flow for this many seconds
STATS_TTL = 300     # cache the energy-panel figures for 5 min


# ── config ──────────────────────────────────────────────────────────────────

def load_config():
    cfg = {}
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        pass
    # environment variables win, so the same file hosts anywhere
    cfg['email'] = os.environ.get('SEMS_EMAIL', cfg.get('email', ''))
    cfg['password'] = os.environ.get('SEMS_PASSWORD', cfg.get('password', ''))
    cfg['station_id'] = os.environ.get('SEMS_STATION_ID', cfg.get('station_id', ''))
    return cfg


CFG = load_config()


# ── SEMS API (synchronous, stdlib only) ─────────────────────────────────────

def _post(url, headers, payload, timeout=12, tries=1):
    last = None
    for _ in range(tries):
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


def sems_login(username, password):
    body = _post(LOGIN_URL,
                 {'Content-Type': 'application/json', 'Token': LOGIN_TOKEN},
                 {'account': username, 'pwd': password}, tries=3)
    if body.get('hasError') or body.get('code') != 0:
        raise ValueError(body.get('msg', 'Login failed. Check credentials.'))
    return body['data'], body.get('api', 'https://hk.semsportal.com/api/')


def _token_header(login_data):
    return {'Content-Type': 'application/json',
            'Token': json.dumps(login_data, separators=(',', ':'))}


def get_station_id(api, login_data):
    body = _post(api + 'PowerStation/GetPowerStationIdByOwner',
                 _token_header(login_data), {}, tries=3)
    sid = body.get('data')
    if not sid:
        raise ValueError('No power station found on this account.')
    return str(sid)


def get_powerflow(api, login_data, sid):
    body = _post(api + 'v2/PowerStation/GetPowerflow',
                 _token_header(login_data), {'PowerStationId': sid}, tries=2)
    return (body.get('data') or {}).get('powerflow') or {}


def get_inverter_detail(api, login_data, sid):
    body = _post(api + 'v1/PowerStation/GetMonitorDetailByPowerstationId',
                 _token_header(login_data), {'powerStationId': sid},
                 timeout=14, tries=2)
    return ((body.get('data') or {}).get('inverter') or [{}])[0]


def get_chart(api, login_data, sid, date_str, rng):
    """Return {line_name: [{'x','y'},...]} merging PVGeneration + Buy series."""
    out = {}
    for cidx in ('3', '8'):
        body = _post(api + 'v2/Charts/GetChartByPlant', _token_header(login_data),
                     {'id': sid, 'date': date_str, 'range': rng,
                      'chartIndexId': cidx, 'isDetailFull': False},
                     timeout=20, tries=3)
        for ln in (body.get('data') or {}).get('lines') or []:
            out[ln.get('name')] = ln.get('xy') or []
    return out


def _line_map(chart, name):
    return {p['x']: p['y'] for p in chart.get(name, [])}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _watts(s):
    if s is None:
        return 0.0
    m = re.search(r'(-?\d+\.?\d*)', str(s))
    return float(m.group(1)) if m else 0.0


def grid_state_from_detail(inv):
    """Return 'on' / 'off' / None from inverter work_mode + grid voltage."""
    d = inv.get('d') or {}
    wm = str(d.get('work_mode', '')).lower()
    if 'off' in wm or 'backup' in wm:
        return 'off'
    if 'on' in wm or 'normal' in wm:
        return 'on'
    gv = _watts(d.get('grid_voltage'))
    if gv:
        return 'on' if gv > 50 else 'off'
    return None


# ── cached session + data (shared across requests, refreshed on demand) ──────

class Session:
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self.login = None
        self.api = None
        self.login_time = 0.0
        self.sid = cfg.get('station_id') or ''
        self.last_grid = None
        # caches
        self._live = None
        self._live_time = 0.0
        self._stats = None
        self._stats_time = 0.0

    def _ensure_login(self):
        if self.login and (time.time() - self.login_time) < TOKEN_TTL:
            return
        self.login, self.api = sems_login(self.cfg['email'], self.cfg['password'])
        self.login_time = time.time()
        if not self.sid:
            self.sid = get_station_id(self.api, self.login)

    def _fetch_live(self):
        self._ensure_login()
        pf = get_powerflow(self.api, self.login, self.sid)
        grid = self.last_grid
        etotal = None
        try:
            inv = get_inverter_detail(self.api, self.login, self.sid)
            grid = grid_state_from_detail(inv)
            self.last_grid = grid
            etotal = _num(inv.get('etotal'))
        except Exception:
            pass  # keep last-known grid state; live flow still updates
        return {'pf': pf, 'grid_state': grid, 'etotal': etotal,
                'updated': datetime.datetime.now().strftime('%H:%M:%S')}

    def _fetch_stats(self):
        today = datetime.date.today()
        yest = today - datetime.timedelta(days=1)
        daily = get_chart(self.api, self.login, self.sid, today.isoformat(), 2)
        monthly = get_chart(self.api, self.login, self.sid, today.isoformat(), 3)
        dpv, dbuy = _line_map(daily, 'PVGeneration'), _line_map(daily, 'Buy')
        mpv, mbuy = _line_map(monthly, 'PVGeneration'), _line_map(monthly, 'Buy')
        tk, yk = today.isoformat(), yest.isoformat()
        tm = today.strftime('%Y-%m')
        lm = (today.replace(day=1) - datetime.timedelta(days=1)).strftime('%Y-%m')
        return {
            'today_pv': dpv.get(tk), 'today_grid': dbuy.get(tk),
            'yest_pv': dpv.get(yk), 'yest_grid': dbuy.get(yk),
            'tmonth_pv': mpv.get(tm), 'tmonth_grid': mbuy.get(tm),
            'lmonth_pv': mpv.get(lm), 'lmonth_grid': mbuy.get(lm),
            'updated': datetime.datetime.now().strftime('%H:%M:%S'),
        }

    def data(self):
        """Return the full payload for the page, using short-lived caches."""
        with self._lock:
            now = time.time()
            if self._live is None or now - self._live_time > LIVE_TTL:
                self._live = self._fetch_live()
                self._live_time = now
            if self._stats is None or now - self._stats_time > STATS_TTL:
                try:
                    self._stats = self._fetch_stats()
                    self._stats_time = now
                except Exception:
                    pass  # keep stale stats rather than failing the whole page
            out = dict(self._live)
            out['stats'] = self._stats
            return out


SESSION = Session(CFG)


# ── web app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0e0e12">
<title>SolarGrowwe</title>
<style>
  :root{ --green:#2fbf5a; --orange:#e0892a; --red:#e0533a; --grey:#9aa0a6;
         --white:#f1f1f1; --line:rgba(170,170,178,.55); --fill:rgba(26,26,30,.92); }
  *{ box-sizing:border-box; }
  html,body{ margin:0; height:100%; }
  body{ background:radial-gradient(120% 120% at 50% 0%, #16161c 0%, #0b0b0f 70%);
        color:var(--white); font-family:'Segoe UI',system-ui,Arial,sans-serif;
        display:flex; flex-direction:column; align-items:center; min-height:100%; }
  header{ width:100%; max-width:540px; display:flex; align-items:center;
          gap:10px; padding:16px 18px 6px; }
  #dot{ width:11px; height:11px; border-radius:50%; background:var(--grey);
        box-shadow:0 0 8px currentColor; color:var(--grey); }
  #state{ font-size:14px; color:var(--grey); }
  #updated{ margin-left:auto; font-size:12px; color:#6a6a72; }
  canvas{ width:min(92vw,440px); height:min(92vw,440px); touch-action:none; }
  .panel{ width:min(92vw,440px); margin:4px 0 26px;
          background:var(--fill); border:1px solid #2a2a31; border-radius:14px;
          padding:14px 16px; }
  table{ width:100%; border-collapse:collapse; font-size:14px; }
  th,td{ text-align:right; padding:6px 4px; }
  th:first-child,td:first-child{ text-align:left; color:var(--grey); font-weight:400; }
  thead th{ font-weight:600; }
  .solar{ color:var(--green); } .grid{ color:var(--orange); }
  tfoot td{ border-top:1px solid #2a2a31; padding-top:9px; color:var(--grey); }
  .err{ color:var(--red); font-size:13px; padding:6px 18px; }
  .hint{ color:#5a5a62; font-size:11px; padding:0 0 18px; }
</style>
</head>
<body>
  <header>
    <span id="dot"></span>
    <span id="state">connecting…</span>
    <span id="updated"></span>
  </header>
  <canvas id="c" width="880" height="880"></canvas>
  <div id="err" class="err" hidden></div>
  <div class="panel">
    <table>
      <thead><tr><th></th><th class="solar">Solar kWh</th><th class="grid">Grid kWh</th></tr></thead>
      <tbody id="rows">
        <tr><td>Today</td><td>—</td><td>—</td></tr>
        <tr><td>Yesterday</td><td>—</td><td>—</td></tr>
        <tr><td>This month</td><td>—</td><td>—</td></tr>
        <tr><td>Last month</td><td>—</td><td>—</td></tr>
      </tbody>
      <tfoot id="foot"></tfoot>
    </table>
  </div>
  <div class="hint">auto-refreshing • figures come straight from SEMS</div>

<script>
const C = document.getElementById('c'), X = C.getContext('2d');
const COL = { green:'#2fbf5a', orange:'#e0892a', red:'#e0533a', grey:'#9aa0a6',
              white:'#f1f1f1', line:'rgba(170,170,178,.55)', fill:'rgba(26,26,30,.95)' };
let DATA = null, phase = 0;

function watts(s){ if(s==null) return 0; const m=String(s).match(/-?\d+\.?\d*/); return m?parseFloat(m[0]):0; }
function fmtKw(w){ let t=(w/1000).toFixed(3).replace(/0+$/,'').replace(/\.$/,''); return (t===''||t==='-0')?'0':t; }
function g(v){ return (v==null)?'—':(+v).toLocaleString(undefined,{maximumFractionDigits:3}); }

function draw(){
  const W=C.width, H=C.height, cx=W/2, cy=H/2, s=Math.min(W,H);
  X.clearRect(0,0,W,H);
  const r=s*0.16, ox=W/2-r-s*0.02, oy=H/2-r-s*0.02;
  const pf=(DATA&&DATA.pf)||{};
  const pvW=watts(pf.pv), gridW=watts(pf.grid), loadW=watts(pf.load), batW=watts(pf.bettery);
  const soc=Math.round(+(pf.soc)||0), gridSt=parseInt(pf.gridStatus)||0, batSt=parseInt(pf.betteryStatus)||0;

  const solar=[cx,cy-oy], grid=[cx-ox,cy], load=[cx+ox,cy], bat=[cx,cy+oy], ctr=[cx,cy];

  flow(solar,ctr,r,pvW>0,true,s);                         // solar -> bus
  flow(load,ctr,r,loadW>0,false,s);                       // bus -> load
  flow(grid,ctr,r,gridSt!==0&&gridW>0,gridSt>0,s);        // import -> bus
  flow(bat,ctr,r,batSt!==0&&batW>0,batSt<0,s);            // charge: bus -> bat

  X.fillStyle=COL.line; X.beginPath(); X.arc(cx,cy,s*0.012,0,7); X.fill();

  node(solar,r,COL.green,'Solar',fmtKw(pvW),s);
  node(grid,r,COL.orange,gridSt<0?'Export':'Grid',fmtKw(gridW),s);
  node(load,r,COL.orange,'Load',fmtKw(loadW),s);
  node(bat,r, batSt>0?COL.green:COL.orange, 'Battery '+soc+'%', fmtKw(batW), s);
}

function flow(node,ctr,r,active,into,s){
  const vx=ctr[0]-node[0], vy=ctr[1]-node[1], len=Math.hypot(vx,vy)||1;
  const ux=vx/len, uy=vy/len, edge=[node[0]+ux*r, node[1]+uy*r];
  X.strokeStyle=COL.line; X.lineWidth=Math.max(1.5,s*0.006);
  X.beginPath(); X.moveTo(edge[0],edge[1]); X.lineTo(ctr[0],ctr[1]); X.stroke();
  if(!active) return;
  const a=into?edge:ctr, b=into?ctr:edge, n=3, rr=Math.max(2,s*0.014);
  for(let k=0;k<n;k++){
    const t=(phase+k/n)%1, x=a[0]+(b[0]-a[0])*t, y=a[1]+(b[1]-a[1])*t;
    const al=Math.max(0,Math.min(1,(70+170*Math.sin(Math.PI*t))/255));
    X.fillStyle='rgba(241,241,241,'+al.toFixed(3)+')';
    X.beginPath(); X.arc(x,y,rr,0,7); X.fill();
  }
}

function node(c,r,ring,label,value,s){
  X.lineWidth=Math.max(2,s*0.012); X.strokeStyle=ring; X.fillStyle=COL.fill;
  X.beginPath(); X.arc(c[0],c[1],r,0,7); X.fill(); X.stroke();
  X.textAlign='center'; X.fillStyle=COL.grey;
  X.font=Math.max(11,s*0.035)+'px Segoe UI,sans-serif';
  X.textBaseline='middle'; X.fillText(label, c[0], c[1]-r*0.42);
  X.fillStyle=COL.white; X.font='bold '+Math.max(14,s*0.06)+'px Segoe UI,sans-serif';
  X.fillText(value, c[0], c[1]+r*0.02);
  X.fillStyle=COL.grey; X.font=Math.max(10,s*0.032)+'px Segoe UI,sans-serif';
  X.fillText('kW', c[0], c[1]+r*0.5);
}

function paintHeader(){
  const dot=document.getElementById('dot'), st=document.getElementById('state');
  let col=COL.grey, txt='Grid status —';
  if(!DATA){ col=COL.red; txt='Offline (no data)'; }
  else if(DATA.grid_state==='off'){ col=COL.orange; txt='Off-grid'; }
  else if(DATA.grid_state==='on'){ col=COL.green; txt='On-grid'; }
  const pf=(DATA&&DATA.pf)||{}; const soc=Math.round(+(pf.soc)||0);
  dot.style.color=col; dot.style.background=col;
  st.textContent = txt + (DATA? '  ·  Battery '+soc+'%' : '');
  document.getElementById('updated').textContent = (DATA&&DATA.updated)? 'updated '+DATA.updated : '';
}

function paintPanel(){
  const s=(DATA&&DATA.stats)||{};
  const rows=[['Today',s.today_pv,s.today_grid],['Yesterday',s.yest_pv,s.yest_grid],
              ['This month',s.tmonth_pv,s.tmonth_grid],['Last month',s.lmonth_pv,s.lmonth_grid]];
  document.getElementById('rows').innerHTML = rows.map(
    r=>`<tr><td>${r[0]}</td><td>${g(r[1])}</td><td>${g(r[2])}</td></tr>`).join('');
  document.getElementById('foot').innerHTML =
    (DATA&&DATA.etotal!=null)? `<tr><td>Total PV</td><td>${g(DATA.etotal)}</td><td></td></tr>` : '';
}

async function refresh(){
  try{
    const res = await fetch('api/data', {cache:'no-store'});
    const j = await res.json();
    const err=document.getElementById('err');
    if(j.error){ err.textContent=j.error; err.hidden=false; }
    else { DATA=j; err.hidden=true; }
  }catch(e){
    const err=document.getElementById('err'); err.textContent='Could not reach the server.'; err.hidden=false;
  }
  paintHeader(); paintPanel();
}

function loop(){ phase=(phase+0.02)%1; draw(); requestAnimationFrame(loop); }

refresh(); setInterval(refresh, 8000); requestAnimationFrame(loop);
</script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(PAGE)


@app.route('/api/data')
def api_data():
    if not CFG.get('email') or not CFG.get('password'):
        return jsonify({'error': 'Server is missing SEMS_EMAIL / SEMS_PASSWORD.'}), 500
    try:
        return jsonify(SESSION.data())
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'.strip()}), 502


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
