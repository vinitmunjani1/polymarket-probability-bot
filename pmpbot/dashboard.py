from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .config import Settings
from .dashboard_state import DashboardState
from .engine import BotEngine


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Polymarket Probability Bot Dashboard</title>
  <style>
    :root { color-scheme: dark; --bg:#080b12; --panel:#111827; --muted:#94a3b8; --text:#e5e7eb; --green:#22c55e; --yellow:#f59e0b; --red:#ef4444; --blue:#38bdf8; }
    body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }
    header { padding:18px 22px; border-bottom:1px solid #1f2937; display:flex; gap:16px; align-items:center; justify-content:space-between; }
    h1 { font-size:20px; margin:0; }
    .grid { display:grid; gap:14px; padding:16px; }
    .top { grid-template-columns: repeat(6, minmax(0, 1fr)); }
    .markets { grid-template-columns: repeat(4, minmax(260px, 1fr)); }
    .two { grid-template-columns: 1.2fr .8fr; }
    .card { background:var(--panel); border:1px solid #1f2937; border-radius:14px; padding:14px; box-shadow:0 8px 30px #0003; }
    .metric .label { color:var(--muted); font-size:12px; }
    .metric .value { font-size:20px; font-weight:700; margin-top:4px; }
    .asset { display:flex; justify-content:space-between; align-items:flex-start; }
    .asset h2 { margin:0 0 8px; font-size:22px; }
    .badge { padding:4px 8px; border-radius:999px; font-size:12px; font-weight:700; background:#334155; }
    .trade { background:#052e16; color:#86efac; } .skip { background:#422006; color:#fcd34d; } .bad { background:#450a0a; color:#fca5a5; }
    .rows { display:grid; gap:7px; font-size:13px; }
    .row { display:flex; justify-content:space-between; gap:12px; border-bottom:1px dashed #263244; padding-bottom:5px; }
    .muted { color:var(--muted); }
    .green { color:var(--green); } .yellow { color:var(--yellow); } .red { color:var(--red); } .blue { color:var(--blue); }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th, td { text-align:left; padding:8px 6px; border-bottom:1px solid #1f2937; vertical-align:top; }
    th { color:var(--muted); font-weight:600; }
    pre { white-space:pre-wrap; word-break:break-word; margin:0; font-size:12px; color:#cbd5e1; }
    @media (max-width: 1200px) { .top, .markets, .two { grid-template-columns:1fr 1fr; } }
    @media (max-width: 700px) { .top, .markets, .two { grid-template-columns:1fr; } header { display:block; } }
  </style>
</head>
<body>
  <header>
    <div><h1>Polymarket Probability Bot</h1><div class="muted">Real-time 5-minute crypto market cockpit</div></div>
    <div id="conn" class="badge bad">DISCONNECTED</div>
  </header>

  <section class="grid top" id="health"></section>
  <section class="grid markets" id="markets"></section>
  <section class="grid two">
    <div class="card"><h3>Decision / Event Log</h3><table><thead><tr><th>Time</th><th>Asset</th><th>Event</th><th>Side</th><th>Edge</th><th>Reason</th><th>Latency</th></tr></thead><tbody id="events"></tbody></table></div>
    <div class="card"><h3>Configuration</h3><pre id="config"></pre></div>
  </section>

<script>
let state = {health:{}, markets:{}, positions:{}, events:[], config:{}};
const fmt = (x, d=2) => (x === undefined || x === null || Number.isNaN(x)) ? '—' : Number(x).toFixed(d);
const cents = x => (x === undefined || x === null) ? '—' : (Number(x)*100).toFixed(1) + '¢';
const price = x => (x === undefined || x === null) ? '—' : Number(x).toFixed(3);
function metric(label, value, cls='') { return `<div class="card metric"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`; }
function recomputePnl() {
  const assets = ['BTC','ETH','SOL','XRP'];
  const pnl = {overall_pnl_usd:0, overall_charges_usd:0, assets:{}};
  let overallUsed = 0;
  for (const asset of assets) {
    const m = (state.markets || {})[asset] || {};
    const positions = Object.values(state.positions || {}).filter(p => p.asset === asset);
    const pos = positions[positions.length - 1] || m.position || {};
    const used = positions.filter(p => ['open','pending'].includes(p.status || 'open')).reduce((a,p)=>a+Number(p.notional_usd||0),0);
    overallUsed += used;
    const capital = Number(m.dry_capital_usd ?? ((state.config||{}).dry_capital_per_asset_usd) ?? 10);
    const p = positions.length ? positions.reduce((a,x)=>a+Number(x.pnl_usd||0),0) : Number(pos.pnl_usd ?? m.position_pnl_usd ?? 0);
    const c = positions.length ? positions.reduce((a,x)=>a+Number(x.charges_usd||0),0) : Number(pos.charges_usd ?? m.position_charges_usd ?? 0);
    pnl.assets[asset] = {capital_usd:capital, used_capital_usd:used, available_capital_usd:Math.max(0, capital-used), pnl_usd:p, charges_usd:c, positions, position: Object.keys(pos).length ? pos : null};
    pnl.overall_pnl_usd += p; pnl.overall_charges_usd += c;
  }
  const totalCapital = Number(((state.config||{}).dry_capital_per_asset_usd) || 10) * assets.length;
  const previousAssets = ((state.pnl || {}).assets || {});
  state.pnl = {
    overall_pnl_usd: 0,
    overall_charges_usd: 0,
    overall_starting_capital_usd: totalCapital,
    overall_equity_usd: totalCapital,
    overall_used_capital_usd: overallUsed,
    overall_available_capital_usd: Math.max(0, totalCapital - overallUsed),
    // Preserve existing assets only when there is no fresh market event;
    // fresh computed values must overwrite old cached PnL.
    assets: {...previousAssets, ...pnl.assets},
  };
  state.pnl.overall_pnl_usd = Object.values(state.pnl.assets).reduce((a,x)=>a+Number(x.pnl_usd||0),0);
  state.pnl.overall_charges_usd = Object.values(state.pnl.assets).reduce((a,x)=>a+Number(x.charges_usd||0),0);
  state.pnl.overall_used_capital_usd = Object.values(state.pnl.assets).reduce((a,x)=>a+Number(x.used_capital_usd||0),0);
  state.pnl.overall_starting_capital_usd = totalCapital;
  state.pnl.overall_available_capital_usd = Math.max(0, totalCapital - state.pnl.overall_used_capital_usd);
  state.pnl.overall_equity_usd = totalCapital + state.pnl.overall_pnl_usd;
}
function mergeMarketEvent(asset, event) {
  const previous = (state.markets || {})[asset];
  if (!previous) return event;
  const sparseEvents = new Set(['skip_empty_books', 'skip_no_market', 'skip_time_gate']);
  const richKeys = ['bid', 'ask', 'fair', 'edge', 'features', 'position', 'price_to_beat'];
  const isSparse = sparseEvents.has(event.event) && !richKeys.some(k => event[k] !== undefined);
  if (!isSparse) return event;
  return {...previous, ...event, stale_market_data: true, stale_reason: event.event, last_good_ts: previous.ts};
}
function render() {
  const h = state.health || {};
  document.getElementById('health').innerHTML = [
    metric('Mode', (h.mode || '—').toUpperCase(), h.mode === 'live' ? 'red' : 'green'),
    metric('Status', (h.status || '—').toUpperCase(), h.status === 'running' ? 'green' : 'yellow'),
    metric('Loop Latency', h.loop_latency_ms == null ? '—' : h.loop_latency_ms + 'ms', h.loop_latency_ms > 1000 ? 'red' : 'green'),
    metric('Last Update', h.last_update ? h.last_update.split('T')[1].slice(0,8) + ' UTC' : '—'),
    metric('Max Order', '$' + fmt(h.max_order_usd, 2)),
    metric('Overall Capital', '$' + fmt((state.pnl||{}).overall_equity_usd, 2), ((state.pnl||{}).overall_equity_usd||0) >= ((state.pnl||{}).overall_starting_capital_usd||0) ? 'green' : 'red'),
    metric('Capital Used', '$' + fmt((state.pnl||{}).overall_used_capital_usd, 2)),
    metric('Overall PnL', '$' + fmt((state.pnl||{}).overall_pnl_usd, 2), ((state.pnl||{}).overall_pnl_usd||0) >= 0 ? 'green' : 'red'),
    metric('Charges', '$' + fmt((state.pnl||{}).overall_charges_usd, 4), 'yellow'),
  ].join('');

  const assets = ['BTC','ETH','SOL','XRP'];
  document.getElementById('markets').innerHTML = assets.map(asset => {
    const m = (state.markets || {})[asset] || {};
    const f = m.features || {};
    const pa = (((state.pnl || {}).assets || {})[asset]) || {};
    const pos = pa.position || m.position || {};
    const eventClass = m.event === 'order_intent' ? 'trade' : (String(m.event||'').startsWith('skip') ? 'skip' : 'bad');
    const distance = f.spot && f.open ? ((f.spot / f.open - 1) * 100).toFixed(3) + '%' : '—';
    return `<div class="card">
      <div class="asset"><h2>${asset}</h2><span class="badge ${eventClass}">${m.event || 'WAITING'}</span></div>
      <div class="rows">
        <div class="row"><span class="muted">Decision</span><b>${m.side || '—'} ${m.event === 'order_intent' ? 'INTENT' : ''}</b></div>
        <div class="row"><span class="muted">Reason</span><span>${m.reason || (m.dry_run ? 'dry-run' : '—')}</span></div>
        ${m.stale_market_data ? `<div class="row"><span class="muted">Data Status</span><span class="yellow">stale: ${m.stale_reason || 'book unavailable'}</span></div>` : ''}
        <div class="row"><span class="muted">Bid / Ask</span><span>${price(m.bid)} / ${price(m.ask)}</span></div>
        <div class="row"><span class="muted">Fair Prob</span><span class="blue">${price(m.fair)}</span></div>
        <div class="row"><span class="muted">Edge</span><span class="${m.edge >= 0.03 ? 'green' : 'yellow'}">${cents(m.edge)}</span></div>
        <div class="row"><span class="muted">Spread</span><span>${cents(m.spread)}</span></div>
        <div class="row"><span class="muted">Spot / Price to Beat</span><span>${fmt(f.spot, asset==='XRP'?4:2)} / ${fmt(f.open, asset==='XRP'?4:2)}</span></div>
        <div class="row"><span class="muted">Distance</span><span>${distance}</span></div>
        <div class="row"><span class="muted">Dry Capital</span><span>$${fmt(pa.used_capital_usd,2)} / $${fmt(pa.capital_usd,2)}</span></div>
        <div class="row"><span class="muted">Position</span><span>${pos.side || '—'} @ ${price(pos.entry_price)} | $${fmt(pos.notional_usd,2)}</span></div>
        <div class="row"><span class="muted">Order Limit / Status</span><span>${price(m.order_limit_price || pos.entry_price)} / ${pos.status || m.dry_order_status || '—'}</span></div>
        <div class="row"><span class="muted">Mark / PnL</span><span>${price(pos.mark_price)} / <b class="${(pa.pnl_usd||0) >= 0 ? 'green':'red'}">$${fmt(pa.pnl_usd,2)}</b></span></div>
        <div class="row"><span class="muted">Charges</span><span>$${fmt(pa.charges_usd,4)}</span></div>
        <div class="row"><span class="muted">Latency</span><span>${m.latency_ms ?? '—'}ms</span></div>
      </div>
    </div>`;
  }).join('');

  document.getElementById('events').innerHTML = (state.events || []).slice(0,60).map(e => `
    <tr><td>${e.ts ? e.ts.split('T')[1].slice(0,8) : '—'}</td><td>${e.asset || '—'}</td><td>${e.event || '—'}</td><td>${e.side || '—'}</td><td>${cents(e.edge)}</td><td>${e.reason || '—'}</td><td>${e.latency_ms ?? '—'}ms</td></tr>
  `).join('');
  document.getElementById('config').textContent = JSON.stringify(state.config || {}, null, 2);
}
async function boot() {
  state = await fetch('/api/snapshot').then(r => r.json());
  recomputePnl();
  render();
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { document.getElementById('conn').className='badge trade'; document.getElementById('conn').textContent='CONNECTED'; };
  ws.onclose = () => { document.getElementById('conn').className='badge bad'; document.getElementById('conn').textContent='DISCONNECTED'; setTimeout(boot, 2000); };
  ws.onmessage = msg => {
    const event = JSON.parse(msg.data);
    state.events = [event, ...(state.events || [])].slice(0,100);
    if (event.asset) {
      state.markets[event.asset] = mergeMarketEvent(event.asset, event);
      const pos = state.markets[event.asset].position;
      if (pos) state.positions[`${pos.asset || event.asset}:${pos.window || event.window || 'unknown'}`] = pos;
    }
    recomputePnl();
    state.health.last_update = event.ts;
    state.health.status = 'running';
    if (event.latency_ms !== undefined) state.health.loop_latency_ms = event.latency_ms;
    render();
  };
}
boot();
</script>
</body>
</html>
"""


def create_app() -> FastAPI:
    settings = Settings.load()
    dash_state = DashboardState(settings)
    engine = BotEngine(settings, event_sink=dash_state.publish)
    dash_state.load_positions(engine.state.data.get("positions", {}))
    task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal task
        task = asyncio.create_task(engine.run_forever())
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await engine.close()

    app = FastAPI(title="Polymarket Probability Bot Dashboard", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return HTML

    @app.get("/api/snapshot")
    async def snapshot() -> dict:
        return dash_state.snapshot()

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        queue = dash_state.subscribe()
        try:
            await ws.send_text(json.dumps({"type": "snapshot", **dash_state.snapshot()}, default=str))
            while True:
                event = await queue.get()
                await ws.send_text(json.dumps(event, default=str))
        except WebSocketDisconnect:
            pass
        finally:
            dash_state.unsubscribe(queue)

    return app


app = create_app()
