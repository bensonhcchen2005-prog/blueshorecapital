"""
Static dashboard exporter — produces a single self-contained HTML file
suitable for GitHub Pages or any static host.

Pulls live JSON from the running dashboard (default http://localhost:8877),
embeds the snapshot inline, and renders a public read-only view:
  - KPI tiles (total P&L, win-rate, profit factor, drawdown)
  - Equity curve (Chart.js from CDN)
  - Strategy breakdown
  - Open holdings (sanitised — no account ids)
  - Signal-flow funnel
  - "Last updated" timestamp + auto-refresh meta tag

Usage:
    ./venv/bin/python -m scripts.export_static
    ./venv/bin/python -m scripts.export_static --out docs/index.html
    ./venv/bin/python -m scripts.export_static --window 168

Pair with cron + `git push` to keep the GitHub Pages copy live.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_OUT = PROJECT_ROOT / "docs" / "index.html"


def fetch(url: str, timeout: int = 10) -> dict:
    with urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def sanitise_data(d: dict) -> dict:
    """Strip account-identifying fields before publishing."""
    safe = json.loads(json.dumps(d, default=str))  # deep copy

    # Drop raw broker snapshot fields that may leak account ids
    for k in ("broker_snapshot", "account_id", "trd_env_account"):
        safe.pop(k, None)

    # Holdings: keep symbol/qty/pnl, drop anything that looks like an order id
    for h in safe.get("holdings", []) or []:
        for k in list(h.keys()):
            if "order_id" in k.lower() or "trd_id" in k.lower():
                h.pop(k, None)

    return safe


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Moomoo Paper Trader — Public Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#0d1117; --surface:#161b22; --border:#30363d;
    --text:#c9d1d9; --dim:#8b949e;
    --green:#3fb950; --red:#f85149; --blue:#58a6ff; --amber:#d29922;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:var(--bg);color:var(--text);line-height:1.5}
  header{padding:24px 32px;border-bottom:1px solid var(--border);
         display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
  h1{margin:0;font-size:20px}
  .badge{display:inline-block;padding:2px 10px;border-radius:12px;
         background:var(--surface);border:1px solid var(--border);font-size:12px;color:var(--dim)}
  .badge.live{color:var(--green);border-color:var(--green)}
  main{padding:24px 32px;max-width:1280px;margin:0 auto}
  .grid{display:grid;gap:16px}
  .kpis{grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin-bottom:24px}
  .kpi{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px}
  .kpi .label{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
  .kpi .value{font-size:24px;font-weight:600;margin-top:4px}
  .pos{color:var(--green)}.neg{color:var(--red)}.neu{color:var(--blue)}
  section{background:var(--surface);border:1px solid var(--border);
          border-radius:8px;padding:20px;margin-bottom:16px}
  h2{margin:0 0 14px 0;font-size:16px;color:var(--text)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)}
  th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  tr:last-child td{border-bottom:0}
  .funnel{display:flex;gap:8px;align-items:flex-end;height:160px;margin-top:8px}
  .funnel-bar{flex:1;background:var(--blue);border-radius:4px 4px 0 0;
              position:relative;min-height:4px;display:flex;align-items:flex-start;
              justify-content:center;color:white;font-size:11px;font-weight:600;padding-top:4px}
  .funnel-label{font-size:10px;color:var(--dim);text-align:center;margin-top:6px;text-transform:uppercase}
  .funnel-col{flex:1;display:flex;flex-direction:column}
  .two-col{grid-template-columns:1fr 1fr}
  @media(max-width:720px){.two-col{grid-template-columns:1fr}}
  footer{padding:24px 32px;color:var(--dim);font-size:12px;text-align:center;
         border-top:1px solid var(--border);margin-top:32px}
  footer a{color:var(--blue);text-decoration:none}
  .disclaimer{background:rgba(210,153,34,0.08);border:1px solid var(--amber);
              color:var(--amber);padding:10px 14px;border-radius:6px;font-size:12px;margin-bottom:16px}
  .tab-btn{background:transparent;border:none;color:var(--dim);cursor:pointer;
           padding:10px 16px;font-size:13px;border-bottom:2px solid transparent;
           font-family:inherit;transition:all 0.15s}
  .tab-btn:hover{color:var(--text)}
  .tab-btn.active{color:var(--text);border-bottom-color:var(--blue);font-weight:600}
  .trade-card{background:var(--surface);border:1px solid var(--border);
              border-radius:8px;padding:18px;margin-bottom:14px}
  .trade-card-header{display:flex;justify-content:space-between;align-items:center;
                     margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid var(--border)}
  .trade-meta-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;font-size:12px;margin-bottom:14px}
  .trade-meta-cell{}
  .trade-meta-cell .lbl{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
  .trade-meta-cell .val{font-weight:600;font-size:14px}
  .trade-section{margin-top:14px}
  .trade-section h4{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin:0 0 6px 0}
  .signal-pill{display:inline-block;background:rgba(88,166,255,0.1);color:var(--blue);
               padding:3px 9px;border-radius:12px;font-size:11px;margin-right:6px;margin-bottom:4px}
  .risk-pill{display:inline-block;background:rgba(248,81,73,0.1);color:var(--red);
             padding:3px 9px;border-radius:12px;font-size:11px;margin-right:6px;margin-bottom:4px}
  .kpi-pill{display:inline-block;background:rgba(63,185,80,0.1);color:var(--green);
            padding:3px 9px;border-radius:12px;font-size:11px;margin-right:6px;margin-bottom:4px}
  @media(max-width:720px){.trade-meta-grid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<header>
  <div>
    <h1>Moomoo Paper Trader</h1>
    <div style="color:var(--dim);font-size:13px">Public read-only snapshot · auto-refreshes every 15 min</div>
  </div>
  <div>
    <span class="badge live">PAPER · {{MODE}}</span>
    <span class="badge">Updated {{UPDATED}}</span>
  </div>
</header>

<main>
  <div class="disclaimer">
    Paper-trading results. Past performance is not indicative of future returns.
    This page is for transparency only — not investment advice.
  </div>

  <div id="tabs" style="display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:20px;flex-wrap:wrap">
    <button data-tab="overview" class="tab-btn active">Overview</button>
    <button data-tab="baskets"  class="tab-btn">Baskets</button>
    <button data-tab="theses"   class="tab-btn">Active Theses</button>
    <button data-tab="details"  class="tab-btn">Trade Details</button>
    <button data-tab="alerts"   class="tab-btn">Alerts &amp; Macro</button>
    <button data-tab="funnel"   class="tab-btn">Signal Funnel</button>
  </div>

  <div id="tab-overview" class="tab-panel">
  <div style="display:flex;justify-content:flex-end;margin-bottom:14px;align-items:center;gap:10px">
    <span style="font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px">Market</span>
    <select id="marketFilter" style="background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:6px 12px;font-size:13px;cursor:pointer">
      <option value="ALL">All markets (US + HK)</option>
      <option value="US">US only</option>
      <option value="HK">HK only</option>
    </select>
  </div>

  <div class="grid kpis" id="kpis"></div>
  <div id="marketBreakdown" style="margin-bottom:20px"></div>

  <section>
    <h2>Equity curve</h2>
    <canvas id="equityChart" height="80"></canvas>
  </section>

  <div class="grid two-col">
    <section>
      <h2>Strategy breakdown</h2>
      <table id="strategyTable">
        <thead><tr><th>Strategy</th><th>Trades</th><th>Win rate</th><th>P&amp;L</th></tr></thead>
        <tbody></tbody>
      </table>
    </section>

    <section>
      <h2>Open holdings</h2>
      <table id="holdingsTable">
        <thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>P&amp;L %</th></tr></thead>
        <tbody></tbody>
      </table>
    </section>
  </div>
  </div><!-- /tab-overview -->

  <div id="tab-baskets" class="tab-panel" style="display:none">
    <section>
      <h2>Directional baskets</h2>
      <div style="font-size:12px;color:var(--dim);margin-bottom:12px">
        Positions grouped by the directional bet they express. Each basket shows the thematic thesis,
        gross notional, P&amp;L, and member positions.
      </div>
      <div id="basketsSummary" style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap"></div>
      <div id="basketsList"></div>
    </section>
  </div>

  <div id="tab-theses" class="tab-panel" style="display:none">
    <section>
      <h2>Active investment theses</h2>
      <div style="font-size:12px;color:var(--dim);margin-bottom:12px">
        Every open position has a recorded thesis with technical signals + fundamentals + qualitative rationale.
        Click a row to expand.
      </div>
      <div id="thesesList"></div>
    </section>
  </div>

  <div id="tab-details" class="tab-panel" style="display:none">
    <section>
      <h2>Per-trade details</h2>
      <div style="font-size:12px;color:var(--dim);margin-bottom:12px">
        Full record of every position: entry signals, fundamentals at entry, target, stop, real-time P&amp;L, KPIs to monitor, thesis breakers.
      </div>
      <div id="tradeDetailsList"></div>
    </section>
  </div>

  <div id="tab-alerts" class="tab-panel" style="display:none">
    <section>
      <h2>Macro context</h2>
      <div id="macroBox" style="font-size:13px;color:var(--dim)">Loading…</div>
    </section>

    <section>
      <h2>Thesis monitor</h2>
      <div style="font-size:12px;color:var(--dim);margin-bottom:12px">
        Automated checks on every active position: revenue growth decay, margin compression,
        valuation rerate, stop proximity, earnings imminence, thesis-breaker conditions.
      </div>
      <div id="alertsSummary" style="display:flex;gap:8px;margin-bottom:14px"></div>
      <div id="alertsList"></div>
    </section>

    <section>
      <h2>News scanner — last 24h</h2>
      <div style="font-size:12px;color:var(--dim);margin-bottom:12px">
        Headlines per active position scanned against deterioration keywords
        (downgrades, guidance cuts, lawsuits, recalls, sanctions, executive departures).
      </div>
      <div id="newsSummary" style="display:flex;gap:8px;margin-bottom:14px"></div>
      <div id="newsList"></div>
    </section>
  </div>

  <div id="tab-funnel" class="tab-panel" style="display:none">
    <section>
      <h2>Signal funnel — last 7 days</h2>
      <div style="font-size:12px;color:var(--dim);margin-bottom:8px" id="funnelMeta"></div>
      <div class="funnel" id="funnel"></div>
      <div style="display:flex;gap:8px;margin-top:8px" id="funnelLabels"></div>
    </section>
  </div>
</main>

<footer>
  Built with the moomoo-trader open-source dashboard ·
  <a href="https://github.com/{{GH_REPO}}" target="_blank" rel="noopener">View source on GitHub</a>
</footer>

<script>
const SNAPSHOT = {{SNAPSHOT}};
const PIPELINE = {{PIPELINE}};
const THESES   = {{THESES}};
const ALERTS   = {{ALERTS}};
const NEWS     = {{NEWS}};
const BASKETS  = {{BASKETS}};

const fmt = n => n == null ? "—" : (Math.abs(n) >= 1000 ? n.toLocaleString(undefined,{maximumFractionDigits:0}) : n.toFixed(2));
const pct = n => n == null ? "—" : (n>=0?"+":"") + n.toFixed(1) + "%";
const klass = n => n == null ? "neu" : (n>0?"pos":n<0?"neg":"neu");

// KPIs — recomputed per market filter
const s = SNAPSHOT.stats || {};
const sys = SNAPSHOT.system || {};
const balHistRaw = SNAPSHOT.balance_history || [];
const portfolios = SNAPSHOT.portfolios || {};
const PAPER_START = {{PAPER_START}};

function renderKPIs(market) {
  const us = portfolios.US || {};
  const hk = portfolios.HK || {};
  const hkInUSD = hk.total_assets_usd || (hk.total_assets / (hk.fx_hkd_per_usd || 7.78));
  const fx = hk.fx_hkd_per_usd || 7.78;

  let curEq, totalRet, totalPnl, openCount, closedCount, winRate, pf, dd, label, ccy = '$';

  if (market === 'US') {
    curEq = us.total_assets;
    totalRet = us.total_return;  // dollars
    totalPnl = us.total_pnl;
    openCount = us.broker_open_positions ?? us.open_trades ?? 0;
    closedCount = us.closed_trades || 0;
    winRate = us.win_rate_pct || 0;
    pf = s.profit_factor;
    dd = s.max_drawdown;
    label = 'US Portfolio (USD)';
  } else if (market === 'HK') {
    curEq = hk.total_assets;  // in HKD
    totalRet = hk.total_return;
    totalPnl = hk.total_pnl;
    openCount = (SNAPSHOT.holdings || []).filter(h => h.market === 'HK').length;
    closedCount = hk.closed_trades || 0;
    winRate = hk.win_rate_pct || 0;
    pf = s.profit_factor;
    dd = s.max_drawdown;
    label = 'HK Portfolio (HKD)';
    ccy = 'HK$';
  } else {
    // ALL
    curEq = sys.balance ?? (balHistRaw.length ? balHistRaw[balHistRaw.length-1].balance : PAPER_START);
    totalRet = ((curEq - PAPER_START) / PAPER_START * 100);
    totalPnl = s.total_pnl;
    openCount = s.open_trades || 0;
    closedCount = s.closed_trades || 0;
    winRate = s.win_rate || 0;
    pf = s.profit_factor;
    dd = s.max_drawdown;
    label = 'Combined (USD)';
  }

  const retDisplay = (market === 'ALL')
    ? (totalRet>=0?'+':'') + totalRet.toFixed(1) + '%'
    : ccy + fmt(totalRet);

  const kpis = [
    {label:"Portfolio value",     value: ccy + fmt(curEq), klass:"neu"},
    {label:"Total return",        value: retDisplay,       klass:klass(totalRet)},
    {label:"Total P&L",           value: ccy + fmt(totalPnl), klass:klass(totalPnl)},
    {label:"Win rate",            value: (winRate||0).toFixed(1)+"%", klass:"neu"},
    {label:"Profit factor",       value: fmt(pf), klass:klass((pf||1)-1)},
    {label:"Trades closed",       value: closedCount, klass:"neu"},
    {label:"Open positions",      value: openCount, klass:"neu"},
    {label:"Max drawdown",        value: "$"+fmt(dd), klass:"neg"},
  ];
  document.getElementById("kpis").innerHTML = kpis.map(k =>
    `<div class="kpi"><div class="label">${k.label}</div><div class="value ${k.klass}">${k.value}</div></div>`
  ).join("");

  // Per-market breakdown panel when ALL is selected
  if (market === 'ALL') {
    document.getElementById('marketBreakdown').innerHTML = `
      <section style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px">
        <h3 style="margin:0 0 12px 0;font-size:14px">Market breakdown</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
          <div style="background:rgba(88,166,255,0.05);border:1px solid var(--border);border-left:3px solid var(--blue);border-radius:4px;padding:12px">
            <div style="font-size:11px;color:var(--dim);text-transform:uppercase;margin-bottom:4px">🇺🇸 US (USD)</div>
            <div style="font-size:20px;font-weight:600">$${fmt(us.total_assets)}</div>
            <div style="font-size:12px;color:var(--dim);margin-top:6px">
              Cash: $${fmt(us.cash)} · Mkt val: $${fmt(us.market_val)}<br>
              Total return: <span class="${klass(us.total_return)}">$${fmt(us.total_return)}</span>
              · ${us.broker_open_positions || 0} positions
            </div>
          </div>
          <div style="background:rgba(63,185,80,0.05);border:1px solid var(--border);border-left:3px solid var(--green);border-radius:4px;padding:12px">
            <div style="font-size:11px;color:var(--dim);text-transform:uppercase;margin-bottom:4px">🇭🇰 HK (HKD)</div>
            <div style="font-size:20px;font-weight:600">HK$${fmt(hk.total_assets)}</div>
            <div style="font-size:12px;color:var(--dim);margin-top:6px">
              Cash: HK$${fmt(hk.cash)} · Mkt val: HK$${fmt(hk.market_val)}<br>
              ≈ USD $${fmt(hkInUSD)} @ FX ${fx} · ${(SNAPSHOT.holdings||[]).filter(h=>h.market==='HK').length} positions<br>
              Total return: <span class="${klass(hk.total_return)}">HK$${fmt(hk.total_return)}</span>
            </div>
          </div>
        </div>
      </section>`;
  } else {
    document.getElementById('marketBreakdown').innerHTML = '';
  }

  // Filter holdings table by market
  const allH = SNAPSHOT.holdings || [];
  const filteredH = (market === 'ALL') ? allH : allH.filter(h => h.market === market);
  const sym = (market === 'HK') ? 'HK$' : '$';
  const holdRows = filteredH.slice(0,20).map(h => {
    const sideColor = h.side === 'SHORT' ? 'var(--red)' : 'var(--green)';
    const entry = h.entry_price ?? h.entry ?? h.cost_price;
    const useLocal = market !== 'ALL';
    const mvDisp = useLocal && market === 'HK' ? ('HK$' + fmt(h.market_val_local))
                                                : ('$' + fmt(h.market_val_usd));
    return `<tr><td>${h.code} <span style="color:${sideColor};font-size:10px">${h.side||''}</span></td>
            <td>${fmt(h.qty)}</td>
            <td>${entry != null ? '$' + fmt(entry) : '—'}</td>
            <td class="${klass(h.pl_ratio_pct)}">${pct(h.pl_ratio_pct)}</td></tr>`;
  }).join("");
  const tbody = document.querySelector("#holdingsTable tbody");
  if (tbody) tbody.innerHTML = holdRows || "<tr><td colspan=4>No open positions</td></tr>";
}

// Initial render + dropdown wiring
renderKPIs('ALL');
const mf = document.getElementById('marketFilter');
if (mf) mf.addEventListener('change', e => renderKPIs(e.target.value));

// Equity curve — downsample to ~300 evenly-spaced points so the line scales smoothly
function downsample(arr, maxPts) {
  if (arr.length <= maxPts) return arr;
  const step = arr.length / maxPts;
  const out = [];
  for (let i = 0; i < maxPts; i++) out.push(arr[Math.floor(i * step)]);
  out.push(arr[arr.length-1]);  // always include latest
  return out;
}
const balHist = downsample(balHistRaw, 300);
const labels = balHist.map(p => (p.t || p.timestamp || "").replace("T", " ").slice(0, 16));
const values = balHist.map(p => p.balance ?? p.equity ?? 0);
new Chart(document.getElementById("equityChart"), {
  type:"line",
  data:{labels, datasets:[{
    label:"Portfolio value",
    data:values, borderColor:"#58a6ff", backgroundColor:"rgba(88,166,255,0.1)",
    fill:true, tension:0.2, pointRadius:0, pointHoverRadius:5, borderWidth:2
  }]},
  options:{responsive:true, interaction:{mode:"index", intersect:false},
    plugins:{
      legend:{display:false},
      tooltip:{
        backgroundColor:"#161b22", borderColor:"#30363d", borderWidth:1,
        titleColor:"#c9d1d9", bodyColor:"#c9d1d9", padding:10,
        callbacks:{
          title:(items)=>items[0].label,
          label:(item)=>{
            const v = item.parsed.y;
            const pnl = v - PAPER_START;
            const pct = (pnl / PAPER_START * 100).toFixed(2);
            const sign = pnl >= 0 ? "+" : "";
            return [
              `Portfolio: $${v.toLocaleString(undefined,{maximumFractionDigits:0})}`,
              `P&L: ${sign}$${pnl.toLocaleString(undefined,{maximumFractionDigits:0})} (${sign}${pct}%)`
            ];
          }
        }
      }
    },
    scales:{x:{ticks:{color:"#8b949e",maxTicksLimit:8},grid:{color:"#30363d"}},
            y:{ticks:{color:"#8b949e",callback:v=>"$"+v.toLocaleString()},grid:{color:"#30363d"}}}}
});

// Strategies (API returns 'name', not 'strategy')
const stratRows = (SNAPSHOT.strategy_breakdown || [])
  .map(s => `<tr><td>${s.name || s.strategy || '—'}</td><td>${s.trades||0}</td><td>${(s.win_rate||0).toFixed(1)}%</td>
             <td class="${klass(s.pnl)}">$${fmt(s.pnl)}</td></tr>`).join("");
document.querySelector("#strategyTable tbody").innerHTML = stratRows || "<tr><td colspan=4>No data</td></tr>";

// Holdings now rendered by renderKPIs() with market filter — no separate block needed

// Directional baskets
function renderBaskets() {
  const data = BASKETS || {};
  const baskets = data.baskets || [];
  const summary = data.summary || {};

  // Summary tiles
  const tile = (label, val, color) => `<div style="background:rgba(0,0,0,0.3);border:1px solid var(--border);border-left:3px solid ${color};border-radius:6px;padding:10px 14px;flex:1;min-width:140px">
    <div style="font-size:11px;color:var(--dim);text-transform:uppercase">${label}</div>
    <div style="font-size:18px;font-weight:600;color:${color}">${val}</div>
  </div>`;
  const fmtUsd = n => '$' + (n||0).toLocaleString(undefined,{maximumFractionDigits:0});
  document.getElementById('basketsSummary').innerHTML =
    tile('Long gross', fmtUsd(summary.long_gross), 'var(--green)') +
    tile('Short gross', fmtUsd(summary.short_gross), 'var(--red)') +
    tile('Net directional', fmtUsd(summary.net_directional),
         (summary.net_directional||0) >= 0 ? 'var(--green)' : 'var(--red)') +
    tile('Active baskets', summary.n_baskets || 0, 'var(--blue)');

  if (!baskets.length) {
    document.getElementById('basketsList').innerHTML =
      '<div style="color:var(--dim);padding:14px">No active baskets.</div>';
    return;
  }

  document.getElementById('basketsList').innerHTML = baskets.map(b => {
    const dirColor = b.direction === 'LONG' ? 'var(--green)' :
                     b.direction === 'SHORT' ? 'var(--red)' : 'var(--blue)';
    const pnlColor = (b.unrealized_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)';
    return `
      <details style="background:var(--surface);border:1px solid var(--border);border-left:4px solid ${dirColor};border-radius:6px;margin-bottom:10px">
        <summary style="cursor:pointer;list-style:none;padding:14px 16px">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
            <div style="flex:1;min-width:200px">
              <div style="font-weight:600;font-size:14px">${b.name}
                <span style="color:${dirColor};font-size:12px;margin-left:8px">${b.direction}</span>
              </div>
              <div style="font-size:11px;color:var(--dim);margin-top:2px">${b.thesis}</div>
            </div>
            <div style="text-align:right;min-width:200px">
              <div style="font-size:11px;color:var(--dim)">${b.n_positions} positions · gross ${fmtUsd(b.gross_notional)}</div>
              <div style="color:${pnlColor};font-weight:600;font-size:14px">${(b.unrealized_pnl||0)>=0?'+':''}${fmtUsd(b.unrealized_pnl).replace('$','$')}</div>
            </div>
          </div>
        </summary>
        <div style="border-top:1px solid var(--border);padding:14px 16px">
          <table style="width:100%;font-size:12px">
            <thead><tr style="color:var(--dim);text-transform:uppercase;font-size:10px">
              <th style="text-align:left;padding:6px">Code</th>
              <th style="text-align:left">Side</th>
              <th style="text-align:left">Horizon</th>
              <th style="text-align:right">Notional</th>
              <th style="text-align:right">P&amp;L</th>
              <th style="text-align:left;padding-left:14px">Thesis</th>
            </tr></thead>
            <tbody>
              ${b.members.map(m => `<tr style="border-top:1px solid var(--border)">
                <td style="padding:6px;font-weight:600">${m.code}</td>
                <td style="color:${m.side==='LONG'?'var(--green)':'var(--red)'}">${m.side || '—'}</td>
                <td style="font-size:11px;color:var(--dim)">${m.horizon_class || '—'}</td>
                <td style="text-align:right">${fmtUsd(m.market_val_usd)}</td>
                <td style="text-align:right;color:${(m.unrealized_pnl||0)>=0?'var(--green)':'var(--red)'}">${(m.unrealized_pnl||0)>=0?'+':''}${fmtUsd(m.unrealized_pnl)}</td>
                <td style="padding-left:14px;font-size:11px;color:var(--dim)">${(m.thesis_summary||'').slice(0,80)}${(m.thesis_summary||'').length>80?'…':''}</td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </details>`;
  }).join('');
}
renderBaskets();

// News scanner
function renderNews() {
  const data = NEWS || {};
  const counts = data.counts || {RED:0,ORANGE:0,YELLOW:0,POSITIVE:0,NEUTRAL:0};
  const sev = (label, n, color) => `<div style="background:rgba(0,0,0,0.3);border:1px solid var(--border);border-left:3px solid ${color};border-radius:6px;padding:8px 12px;flex:1;min-width:80px">
    <div style="font-size:10px;color:var(--dim);text-transform:uppercase">${label}</div>
    <div style="font-size:18px;font-weight:600;color:${color}">${n}</div>
  </div>`;
  document.getElementById('newsSummary').innerHTML =
    sev('Red', counts.RED, 'var(--red)') +
    sev('Orange', counts.ORANGE, 'var(--amber)') +
    sev('Yellow', counts.YELLOW, '#d29922') +
    sev('Positive', counts.POSITIVE, 'var(--green)') +
    sev('Neutral', counts.NEUTRAL, 'var(--dim)');

  const flagged = [...(data.red_alerts||[]), ...(data.orange_alerts||[])];
  if (!flagged.length) {
    document.getElementById('newsList').innerHTML =
      '<div style="color:var(--green);padding:14px;background:rgba(63,185,80,0.08);border:1px solid var(--green);border-radius:6px">✓ No RED or ORANGE news flags across the book.</div>';
  } else {
    const sevColor = {RED:'var(--red)', ORANGE:'var(--amber)'};
    document.getElementById('newsList').innerHTML = flagged.map(a => `
      <div style="background:var(--surface);border:1px solid var(--border);border-left:4px solid ${sevColor[a.headlines[0]?.severity||'ORANGE']};border-radius:6px;padding:14px;margin-bottom:10px">
        <div style="font-weight:600;font-size:14px;margin-bottom:8px">${a.code}</div>
        ${a.headlines.map(h => `
          <div style="padding:6px 0;border-top:1px solid rgba(255,255,255,0.05)">
            <div style="font-size:12px">${h.url ? '<a href="'+h.url+'" target="_blank" rel="noopener" style="color:var(--text)">' : ''}${h.title}${h.url ? '</a>' : ''}</div>
            <div style="font-size:10px;color:var(--dim);margin-top:3px">
              ${h.source || '?'} · ${h.pub_at ? h.pub_at.slice(0,16) : '?'}
              · keywords: <span style="color:${sevColor[h.severity]}">${(h.keywords||[]).join(', ')}</span>
            </div>
          </div>`).join('')}
      </div>`).join('');
  }

  // Also surface per-code summary (top 5 codes by news volume) below
  const byCode = data.by_code || {};
  const entries = Object.values(byCode).filter(s => s.n_items > 0)
    .sort((a,b) => b.n_items - a.n_items).slice(0, 8);
  if (entries.length) {
    const html = '<div style="font-size:11px;color:var(--dim);text-transform:uppercase;margin-top:18px;margin-bottom:8px">Per-position activity</div>' +
      '<table style="width:100%;font-size:12px"><thead><tr style="color:var(--dim);font-size:10px;text-transform:uppercase">' +
      '<th style="text-align:left;padding:6px">Code</th><th>Headlines</th><th>Max severity</th>' +
      '</tr></thead><tbody>' +
      entries.map(e => `<tr style="border-top:1px solid var(--border)">
        <td style="padding:6px;font-weight:600">${e.code}</td>
        <td>${e.n_items}</td>
        <td style="color:${({'RED':'var(--red)','ORANGE':'var(--amber)','YELLOW':'#d29922','POSITIVE':'var(--green)','NEUTRAL':'var(--dim)'})[e.max_severity] || 'var(--dim)'}">${e.max_severity}</td>
      </tr>`).join('') + '</tbody></table>';
    document.getElementById('newsList').insertAdjacentHTML('beforeend', html);
  }
}
renderNews();

// Alerts + Macro
function renderAlerts() {
  const a = ALERTS || {};
  const macro = a.macro || {};
  const counts = a.counts || {RED:0,ORANGE:0,YELLOW:0,GREEN:0};

  // Macro box
  const tier1Today = macro.tier1_today || [];
  const upcoming = (macro.upcoming_14d || []).slice(0,8);
  const nextT1 = macro.next_tier1_event;
  let macroHtml = '';
  if (tier1Today.length) {
    macroHtml += `<div style="background:rgba(248,81,73,0.1);border:1px solid var(--red);color:var(--red);padding:10px 14px;border-radius:6px;margin-bottom:12px">
      <b>⚠ Today: Tier-1 macro event</b><br>
      ${tier1Today.map(e=>`<div style="margin-top:4px">• ${e.event} — ${e.impact}</div>`).join('')}
    </div>`;
  } else {
    macroHtml += '<div style="color:var(--green);margin-bottom:12px">No Tier-1 macro events today.</div>';
  }
  if (nextT1) {
    macroHtml += `<div style="margin-bottom:8px"><b>Next Tier-1 catalyst:</b> ${nextT1.date} — ${nextT1.event}</div>`;
  }
  if (upcoming.length) {
    macroHtml += '<div style="font-size:11px;color:var(--dim);margin-top:8px">Upcoming 14 days:</div>';
    macroHtml += '<table style="width:100%;font-size:12px;margin-top:6px">';
    upcoming.forEach(e => {
      const color = e.tier===1?'var(--red)':e.tier===2?'var(--amber)':'var(--dim)';
      macroHtml += `<tr><td style="padding:3px 0;color:${color};width:90px">${e.date}</td><td style="color:${color};width:60px">T${e.tier}</td><td>${e.event}</td></tr>`;
    });
    macroHtml += '</table>';
  }
  document.getElementById('macroBox').innerHTML = macroHtml;

  // Counts summary
  const sev = (label, n, color) => `<div style="background:rgba(0,0,0,0.3);border:1px solid var(--border);border-left:3px solid ${color};border-radius:6px;padding:10px 14px;flex:1;min-width:100px">
    <div style="font-size:11px;color:var(--dim);text-transform:uppercase">${label}</div>
    <div style="font-size:22px;font-weight:600;color:${color}">${n}</div>
  </div>`;
  document.getElementById('alertsSummary').innerHTML =
    sev('Red', counts.RED, 'var(--red)') +
    sev('Orange', counts.ORANGE, 'var(--amber)') +
    sev('Yellow', counts.YELLOW, '#d29922') +
    sev('Green', counts.GREEN, 'var(--green)');

  // Alert cards (skip GREEN)
  const alerts = (a.alerts || []).filter(x => x.severity !== 'GREEN')
    .sort((x,y) => ({RED:0,ORANGE:1,YELLOW:2}[x.severity] - {RED:0,ORANGE:1,YELLOW:2}[y.severity]));
  if (!alerts.length) {
    document.getElementById('alertsList').innerHTML =
      '<div style="color:var(--green);padding:14px;background:rgba(63,185,80,0.08);border:1px solid var(--green);border-radius:6px">✓ All theses healthy — no deterioration flagged.</div>';
    return;
  }
  const sevColor = {RED:'var(--red)', ORANGE:'var(--amber)', YELLOW:'#d29922'};
  document.getElementById('alertsList').innerHTML = alerts.map(al => `
    <div style="background:var(--surface);border:1px solid var(--border);border-left:4px solid ${sevColor[al.severity]};border-radius:6px;padding:14px;margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div>
          <span style="font-weight:600;font-size:15px">${al.code}</span>
          <span style="color:var(--dim);margin-left:8px;font-size:12px">${al.side} · ${al.sector}</span>
        </div>
        <div>
          <span style="color:${sevColor[al.severity]};font-weight:600">${al.severity}</span>
          <span style="color:var(--dim);font-size:11px;margin-left:8px">score ${al.deterioration_score}/100</span>
        </div>
      </div>
      <div style="font-size:12px">
        ${(al.rules_triggered || []).map(r => `
          <div style="padding:4px 0;display:flex;gap:10px">
            <span style="color:${sevColor[r.severity]||'var(--dim)'};min-width:80px;font-weight:600;text-transform:uppercase;font-size:10px">${r.rule}</span>
            <span>${r.detail}</span>
          </div>`).join('')}
      </div>
    </div>`).join('');
}
renderAlerts();

// Tab switching
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
    document.getElementById('tab-' + btn.dataset.tab).style.display = 'block';
  });
});

// Per-trade detail cards
function renderTradeDetails() {
  const theses = (THESES && THESES.theses) || [];
  if (!theses.length) {
    document.getElementById('tradeDetailsList').innerHTML =
      '<div style="color:var(--dim);font-style:italic;padding:12px">No active trades yet.</div>';
    return;
  }
  theses.sort((a,b) => (b.unrealized_pnl||0) - (a.unrealized_pnl||0));
  const html = theses.map(t => {
    const q = t.quantitative || {};
    const ql = t.qualitative || {};
    const target = t.target || {};
    const invalid = t.invalidation || {};
    const sideColor = t.side === 'LONG' ? 'var(--green)' : 'var(--red)';
    const pnl = t.unrealized_pnl || 0;
    const pnlColor = pnl >= 0 ? 'var(--green)' : 'var(--red)';
    const pctChg = t.price_change_pct;
    const signals = t.signals || [];
    const opened = (t.opened_at||'').slice(0,16).replace('T',' ');
    const notional = (t.entry_price || 0) * (t.qty || 0);

    return `
      <div class="trade-card">
        <div class="trade-card-header">
          <div>
            <div style="font-size:18px;font-weight:600">${t.code}
              <span style="color:${sideColor};font-size:13px;margin-left:8px">${t.side}</span>
              <span style="color:var(--dim);font-size:12px;margin-left:8px">${t.sector}</span>
            </div>
            <div style="font-size:11px;color:var(--dim);margin-top:2px">Opened ${opened} UTC</div>
          </div>
          <div style="text-align:right">
            <div style="color:${pnlColor};font-size:18px;font-weight:600">
              ${pnl>=0?'+':''}$${Math.abs(pnl).toLocaleString(undefined,{maximumFractionDigits:0})}
              ${pctChg!=null?'<span style="font-size:13px;margin-left:4px">('+(pctChg>=0?'+':'')+pctChg.toFixed(1)+'%)</span>':''}
            </div>
            <div style="font-size:11px;color:var(--dim)">Unrealized P&amp;L</div>
          </div>
        </div>

        <div class="trade-meta-grid">
          <div class="trade-meta-cell">
            <div class="lbl">Quantity</div>
            <div class="val">${(t.qty||0).toLocaleString()}</div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Entry price</div>
            <div class="val">$${(t.entry_price||0).toFixed(2)}</div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Current</div>
            <div class="val">${t.current_price?'$'+t.current_price.toFixed(2):'—'}</div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Notional</div>
            <div class="val">$${notional.toLocaleString(undefined,{maximumFractionDigits:0})}</div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Target</div>
            <div class="val" style="color:var(--green)">$${(target.price||0).toFixed(2)}</div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Upside</div>
            <div class="val" style="color:var(--green)">${target.upside_pct!=null?(target.upside_pct>=0?'+':'')+target.upside_pct+'%':'—'}</div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Stop loss</div>
            <div class="val" style="color:var(--red)">${invalid.stop_price?'$'+invalid.stop_price.toFixed(2):'—'}</div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Horizon class</div>
            <div class="val">${target.horizon_class || '—'} <span style="color:var(--dim);font-weight:normal;font-size:11px">(${target.horizon_days||'?'}d)</span></div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Entry date</div>
            <div class="val">${t.entry_date || '—'}</div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Days held</div>
            <div class="val">${t.days_held != null ? t.days_held.toFixed(1) : '—'}d
              ${t.horizon_utilization_pct != null ? '<span style="color:var(--dim);font-weight:normal;font-size:11px"> ('+t.horizon_utilization_pct.toFixed(0)+'%)</span>' : ''}
            </div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Days remaining</div>
            <div class="val">${t.days_remaining != null ? t.days_remaining.toFixed(0) + 'd' : '—'}</div>
          </div>
          <div class="trade-meta-cell">
            <div class="lbl">Re-eval cadence</div>
            <div class="val" style="font-size:13px">${target.reeval_cadence || '—'}</div>
          </div>
        </div>

        <div class="trade-section">
          <h4>Horizon rationale</h4>
          <div style="font-size:12px;color:var(--text);background:rgba(88,166,255,0.05);border-left:2px solid var(--blue);padding:8px 12px;border-radius:0 4px 4px 0;font-style:italic">
            ${target.horizon_rationale || '—'}
          </div>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px">
          <div>
            <div class="trade-section">
              <h4>Investment thesis</h4>
              <div style="font-size:13px;line-height:1.55">${ql.thesis_summary || '—'}</div>
            </div>
            <div class="trade-section">
              <h4>Entry signals</h4>
              <div>${signals.map(s => `<span class="signal-pill" title="${s.detail||''}">${s.name}</span>`).join('') || '<span style="color:var(--dim)">—</span>'}</div>
              ${signals.length ? '<div style="font-size:11px;color:var(--dim);margin-top:6px">' + signals.map(s=>'• '+s.detail).join('<br>') + '</div>' : ''}
            </div>
            <div class="trade-section">
              <h4>Tailwinds</h4>
              <div style="font-size:12px;line-height:1.5">${ql.tailwinds_narrative || '—'}</div>
            </div>
            <div class="trade-section">
              <h4>Moat</h4>
              <div style="font-size:12px">${(ql.moat||[]).join(' · ') || '—'}</div>
            </div>
          </div>
          <div>
            <div class="trade-section">
              <h4>Fundamentals at entry</h4>
              <table style="width:100%;font-size:12px">
                <tr><td style="color:var(--dim)">Market cap</td><td style="text-align:right">${q.market_cap?'$'+(q.market_cap/1e9).toFixed(0)+'B':'—'}</td></tr>
                <tr><td style="color:var(--dim)">Forward P/E</td><td style="text-align:right">${q.forward_pe?q.forward_pe.toFixed(1):'—'}</td></tr>
                <tr><td style="color:var(--dim)">P/S</td><td style="text-align:right">${q.ps?q.ps.toFixed(1):'—'}</td></tr>
                <tr><td style="color:var(--dim)">EV / EBITDA</td><td style="text-align:right">${q.ev_ebitda?q.ev_ebitda.toFixed(1):'—'}</td></tr>
                <tr><td style="color:var(--dim)">Revenue growth (YoY)</td><td style="text-align:right">${q.rev_growth!=null?(q.rev_growth*100).toFixed(1)+'%':'—'}</td></tr>
                <tr><td style="color:var(--dim)">Gross margin</td><td style="text-align:right">${q.gross_margin!=null?(q.gross_margin*100).toFixed(1)+'%':'—'}</td></tr>
                <tr><td style="color:var(--dim)">Operating margin</td><td style="text-align:right">${q.op_margin!=null?(q.op_margin*100).toFixed(1)+'%':'—'}</td></tr>
                <tr><td style="color:var(--dim)">FCF margin</td><td style="text-align:right">${q.fcf_margin!=null?(q.fcf_margin*100).toFixed(1)+'%':'—'}</td></tr>
                <tr><td style="color:var(--dim)">ROE</td><td style="text-align:right">${q.roe!=null?(q.roe*100).toFixed(1)+'%':'—'}</td></tr>
                <tr><td style="color:var(--dim)">Insider ownership</td><td style="text-align:right">${q.insider_ownership!=null?(q.insider_ownership*100).toFixed(1)+'%':'—'}</td></tr>
                <tr><td style="color:var(--dim)">Beta</td><td style="text-align:right">${q.beta!=null?q.beta.toFixed(2):'—'}</td></tr>
                <tr><td style="color:var(--dim);font-weight:600">Quality score</td><td style="text-align:right;font-weight:600;color:var(--blue)">${q.quality_score!=null?(q.quality_score*100).toFixed(0)+'/100':'—'}</td></tr>
                <tr><td style="color:var(--dim)">Analyst tgt</td><td style="text-align:right">${q.analyst_price_target?'$'+q.analyst_price_target.toFixed(2):'—'}</td></tr>
              </table>
            </div>
            <div class="trade-section">
              <h4>KPIs to monitor</h4>
              <div>${(ql.kpis_to_watch||[]).map(k => `<span class="kpi-pill">${k}</span>`).join('') || '<span style="color:var(--dim)">—</span>'}</div>
            </div>
            <div class="trade-section">
              <h4>Thesis-breaker risks</h4>
              <div>${(ql.thesis_break_conditions||[]).map(k => `<span class="risk-pill">${k}</span>`).join('') || '<span style="color:var(--dim)">—</span>'}</div>
            </div>
          </div>
        </div>
      </div>`;
  }).join('');
  document.getElementById('tradeDetailsList').innerHTML = html;
}
renderTradeDetails();

// Active Theses
function renderTheses() {
  const theses = (THESES && THESES.theses) || [];
  if (!theses.length) {
    document.getElementById("thesesList").innerHTML =
      '<div style="color:var(--dim);font-style:italic;padding:12px">No active theses yet.</div>';
    return;
  }
  theses.sort((a,b) => (b.unrealized_pnl||0) - (a.unrealized_pnl||0));
  const html = theses.map(t => {
    const q = t.quantitative || {};
    const ql = t.qualitative || {};
    const sideColor = t.side === "LONG" ? "var(--green)" : "var(--red)";
    const pnlColor = (t.unrealized_pnl||0) >= 0 ? "var(--green)" : "var(--red)";
    const pnl = t.unrealized_pnl || 0;
    const target = t.target || {};
    const tail = (t.tailwinds || []).join(", ") || "—";
    return `
      <details style="border:1px solid var(--border);border-radius:6px;margin-bottom:8px;padding:0;overflow:hidden">
        <summary style="cursor:pointer;padding:12px 14px;list-style:none;display:flex;align-items:center;gap:14px;background:var(--surface)">
          <span style="font-weight:600;font-size:14px;min-width:90px">${t.code}</span>
          <span style="color:${sideColor};font-weight:600;min-width:50px">${t.side}</span>
          <span style="color:var(--dim);font-size:12px;min-width:90px">${t.sector || "—"}</span>
          <span style="flex:1;color:var(--dim);font-size:12px">${(ql.thesis_summary||"").slice(0,80)}${(ql.thesis_summary||"").length>80?"…":""}</span>
          <span style="color:${pnlColor};font-weight:600;min-width:90px;text-align:right">${pnl>=0?"+":""}$${Math.abs(pnl).toLocaleString(undefined,{maximumFractionDigits:0})}</span>
          <span style="color:var(--dim);font-size:11px;min-width:70px;text-align:right">${(target.upside_pct!=null?(target.upside_pct>=0?"+":"")+target.upside_pct+"%":"—")} tgt</span>
        </summary>
        <div style="padding:16px;border-top:1px solid var(--border);background:rgba(0,0,0,0.2)">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
            <div>
              <div style="font-size:11px;color:var(--dim);text-transform:uppercase;margin-bottom:8px">Thesis</div>
              <div style="font-size:13px;margin-bottom:12px">${ql.thesis_summary||"—"}</div>
              <div style="font-size:11px;color:var(--dim);text-transform:uppercase;margin-bottom:4px">Tailwinds</div>
              <div style="font-size:12px;margin-bottom:10px">${ql.tailwinds_narrative||tail}</div>
              <div style="font-size:11px;color:var(--dim);text-transform:uppercase;margin-bottom:4px">Moat</div>
              <div style="font-size:12px;margin-bottom:10px">${(ql.moat||[]).join(" · ") || "—"}</div>
              <div style="font-size:11px;color:var(--dim);text-transform:uppercase;margin-bottom:4px">KPIs to watch</div>
              <div style="font-size:12px;margin-bottom:10px">${(ql.kpis_to_watch||[]).join(" · ") || "—"}</div>
              <div style="font-size:11px;color:var(--dim);text-transform:uppercase;margin-bottom:4px">Thesis breakers</div>
              <div style="font-size:12px;color:var(--amber)">${(ql.thesis_break_conditions||[]).join(" · ") || "—"}</div>
            </div>
            <div>
              <div style="font-size:11px;color:var(--dim);text-transform:uppercase;margin-bottom:8px">Fundamentals</div>
              <table style="width:100%;font-size:12px">
                <tr><td style="color:var(--dim)">Market cap</td><td style="text-align:right">${q.market_cap?"$"+(q.market_cap/1e9).toFixed(0)+"B":"—"}</td></tr>
                <tr><td style="color:var(--dim)">Forward P/E</td><td style="text-align:right">${q.forward_pe?q.forward_pe.toFixed(1):"—"}</td></tr>
                <tr><td style="color:var(--dim)">P/S</td><td style="text-align:right">${q.ps?q.ps.toFixed(1):"—"}</td></tr>
                <tr><td style="color:var(--dim)">EV/EBITDA</td><td style="text-align:right">${q.ev_ebitda?q.ev_ebitda.toFixed(1):"—"}</td></tr>
                <tr><td style="color:var(--dim)">Revenue growth (YoY)</td><td style="text-align:right">${q.rev_growth!=null?(q.rev_growth*100).toFixed(1)+"%":"—"}</td></tr>
                <tr><td style="color:var(--dim)">Gross margin</td><td style="text-align:right">${q.gross_margin!=null?(q.gross_margin*100).toFixed(1)+"%":"—"}</td></tr>
                <tr><td style="color:var(--dim)">Operating margin</td><td style="text-align:right">${q.op_margin!=null?(q.op_margin*100).toFixed(1)+"%":"—"}</td></tr>
                <tr><td style="color:var(--dim)">FCF margin</td><td style="text-align:right">${q.fcf_margin!=null?(q.fcf_margin*100).toFixed(1)+"%":"—"}</td></tr>
                <tr><td style="color:var(--dim)">Insider ownership</td><td style="text-align:right">${q.insider_ownership!=null?(q.insider_ownership*100).toFixed(1)+"%":"—"}</td></tr>
                <tr><td style="color:var(--dim)">Beta</td><td style="text-align:right">${q.beta!=null?q.beta.toFixed(2):"—"}</td></tr>
                <tr><td style="color:var(--dim)">Quality score</td><td style="text-align:right"><b>${q.quality_score!=null?(q.quality_score*100).toFixed(0)+"/100":"—"}</b></td></tr>
              </table>
              <div style="margin-top:14px;font-size:11px;color:var(--dim);text-transform:uppercase">Position</div>
              <div style="font-size:12px;margin-top:4px">
                Entry $${(t.entry_price||0).toFixed(2)} → Target $${(target.price||0).toFixed(2)} (${(target.upside_pct!=null?(target.upside_pct>=0?"+":"")+target.upside_pct+"%":"—")})
                · Horizon ${target.horizon_days||"?"}d
              </div>
            </div>
          </div>
        </div>
      </details>`;
  }).join("");
  document.getElementById("thesesList").innerHTML = html;
}
renderTheses();

// Funnel
const f = PIPELINE.funnel || {};
const stages = ["originated","passed_score","passed_gate","queued","filled"];
const max = Math.max(...stages.map(k => f[k]||0), 1);
const colors = ["#58a6ff","#3fb950","#d29922","#a371f7","#f85149"];
document.getElementById("funnel").innerHTML = stages.map((k,i) => {
  const v = f[k] || 0;
  const h = Math.max(4, (v/max)*150);
  return `<div class="funnel-col"><div class="funnel-bar" style="height:${h}px;background:${colors[i]}">${v}</div></div>`;
}).join("");
document.getElementById("funnelLabels").innerHTML = stages.map(k =>
  `<div style="flex:1;text-align:center;font-size:10px;color:#8b949e;text-transform:uppercase">${k.replace(/_/g," ")}</div>`
).join("");
const bn = PIPELINE.bottleneck;
if (bn) {
  document.getElementById("funnelMeta").textContent =
    `Biggest drop: ${bn.from} → ${bn.to} (${(bn.rate*100).toFixed(1)}% pass-through, ${bn.dropped} signals lost)`;
}
</script>
</body>
</html>"""


def render(snapshot: dict, pipeline: dict, theses: dict, alerts: dict,
           news: dict, baskets: dict,
           gh_repo: str = "your-username/moomoo-trader",
           paper_start: float = 1_000_000) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (HTML_TEMPLATE
            .replace("{{SNAPSHOT}}", json.dumps(snapshot, default=str))
            .replace("{{PIPELINE}}", json.dumps(pipeline, default=str))
            .replace("{{THESES}}",   json.dumps(theses,   default=str))
            .replace("{{ALERTS}}",   json.dumps(alerts,   default=str))
            .replace("{{NEWS}}",     json.dumps(news,     default=str))
            .replace("{{BASKETS}}",  json.dumps(baskets,  default=str))
            .replace("{{UPDATED}}", now)
            .replace("{{MODE}}", str(snapshot.get("system", {}).get("mode", "PAPER")))
            .replace("{{GH_REPO}}", gh_repo)
            .replace("{{PAPER_START}}", str(int(paper_start))))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8877")
    ap.add_argument("--window", type=int, default=168, help="pipeline window in hours")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--gh-repo", default="your-username/moomoo-trader",
                    help="GitHub repo slug for the source link in footer")
    ap.add_argument("--paper-start", type=float, default=1_000_000,
                    help="Starting paper-account capital used for total-return calc")
    args = ap.parse_args()

    try:
        snap = fetch(f"{args.base}/api/data")
        pipe = fetch(f"{args.base}/api/pipeline?window={args.window}")
        try:
            theses = fetch(f"{args.base}/api/theses")
        except Exception:
            theses = {"theses": [], "count": 0}
        try:
            alerts = fetch(f"{args.base}/api/alerts")
        except Exception:
            alerts = {"alerts": [], "macro": {}, "counts": {}}
        try:
            news = fetch(f"{args.base}/api/news")
        except Exception:
            news = {"by_code": {}, "counts": {}, "red_alerts": [], "orange_alerts": []}
        try:
            baskets = fetch(f"{args.base}/api/baskets")
        except Exception:
            baskets = {"baskets": [], "summary": {}}
    except Exception as e:
        print(f"ERROR: failed to fetch dashboard JSON: {e}", file=sys.stderr)
        print(f"  Is the dashboard running at {args.base}?", file=sys.stderr)
        sys.exit(1)

    snap = sanitise_data(snap)
    html = render(snap, pipe, theses, alerts, news, baskets, args.gh_repo, args.paper_start)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    size_kb = out_path.stat().st_size / 1024
    print(f"Wrote {out_path}  ({size_kb:.1f} KB)")
    print(f"Preview locally:  open {out_path}")


if __name__ == "__main__":
    main()
