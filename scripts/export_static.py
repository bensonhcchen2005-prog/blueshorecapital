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

  <div class="grid kpis" id="kpis"></div>

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

  <section>
    <h2>Active investment theses</h2>
    <div style="font-size:12px;color:var(--dim);margin-bottom:12px">
      Every open position has a recorded thesis with technical signals + fundamentals + qualitative rationale.
      Click a row to expand.
    </div>
    <div id="thesesList"></div>
  </section>

  <section>
    <h2>Signal funnel — last 7 days</h2>
    <div style="font-size:12px;color:var(--dim);margin-bottom:8px" id="funnelMeta"></div>
    <div class="funnel" id="funnel"></div>
    <div style="display:flex;gap:8px;margin-top:8px" id="funnelLabels"></div>
  </section>
</main>

<footer>
  Built with the moomoo-trader open-source dashboard ·
  <a href="https://github.com/{{GH_REPO}}" target="_blank" rel="noopener">View source on GitHub</a>
</footer>

<script>
const SNAPSHOT = {{SNAPSHOT}};
const PIPELINE = {{PIPELINE}};
const THESES   = {{THESES}};

const fmt = n => n == null ? "—" : (Math.abs(n) >= 1000 ? n.toLocaleString(undefined,{maximumFractionDigits:0}) : n.toFixed(2));
const pct = n => n == null ? "—" : (n>=0?"+":"") + n.toFixed(1) + "%";
const klass = n => n == null ? "neu" : (n>0?"pos":n<0?"neg":"neu");

// KPIs
const s = SNAPSHOT.stats || {};
const sys = SNAPSHOT.system || {};
const balHistRaw = SNAPSHOT.balance_history || [];
// Use the configured paper-account starting capital ($1M default), not stage.starting_equity
// (stage.starting_equity is the Stage-1 risk envelope of $5K, not real starting capital).
const PAPER_START = {{PAPER_START}};
const curEq = sys.balance ?? (balHistRaw.length ? balHistRaw[balHistRaw.length-1].balance : PAPER_START);
const totalRet = (curEq - PAPER_START) / PAPER_START * 100;
const kpis = [
  {label:"Portfolio value", value:"$"+fmt(curEq), klass:"neu"},
  {label:"Total return", value:pct(totalRet), klass:klass(totalRet)},
  {label:"Total P&L ($)", value:"$"+fmt(s.total_pnl), klass:klass(s.total_pnl)},
  {label:"Win rate", value:(s.win_rate||0).toFixed(1)+"%", klass:"neu"},
  {label:"Profit factor", value:fmt(s.profit_factor), klass:klass((s.profit_factor||1)-1)},
  {label:"Trades closed", value:s.closed_trades||0, klass:"neu"},
  {label:"Open positions", value:s.open_trades||0, klass:"neu"},
  {label:"Max drawdown", value:"$"+fmt(s.max_drawdown), klass:"neg"},
];
document.getElementById("kpis").innerHTML = kpis.map(k =>
  `<div class="kpi"><div class="label">${k.label}</div><div class="value ${k.klass}">${k.value}</div></div>`
).join("");

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

// Strategies
const stratRows = (SNAPSHOT.strategy_breakdown || [])
  .map(s => `<tr><td>${s.strategy}</td><td>${s.trades||0}</td><td>${(s.win_rate||0).toFixed(1)}%</td>
             <td class="${klass(s.pnl)}">$${fmt(s.pnl)}</td></tr>`).join("");
document.querySelector("#strategyTable tbody").innerHTML = stratRows || "<tr><td colspan=4>No data</td></tr>";

// Holdings
const holdRows = (SNAPSHOT.holdings || []).slice(0,15)
  .map(h => `<tr><td>${h.symbol||""}</td><td>${fmt(h.qty)}</td>
             <td>$${fmt(h.entry||h.cost_price)}</td>
             <td class="${klass(h.pnl_pct)}">${pct(h.pnl_pct)}</td></tr>`).join("");
document.querySelector("#holdingsTable tbody").innerHTML = holdRows || "<tr><td colspan=4>No open positions</td></tr>";

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


def render(snapshot: dict, pipeline: dict, theses: dict,
           gh_repo: str = "your-username/moomoo-trader",
           paper_start: float = 1_000_000) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (HTML_TEMPLATE
            .replace("{{SNAPSHOT}}", json.dumps(snapshot, default=str))
            .replace("{{PIPELINE}}", json.dumps(pipeline, default=str))
            .replace("{{THESES}}",   json.dumps(theses,   default=str))
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
    except Exception as e:
        print(f"ERROR: failed to fetch dashboard JSON: {e}", file=sys.stderr)
        print(f"  Is the dashboard running at {args.base}?", file=sys.stderr)
        sys.exit(1)

    snap = sanitise_data(snap)
    html = render(snap, pipe, theses, args.gh_repo, args.paper_start)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    size_kb = out_path.stat().st_size / 1024
    print(f"Wrote {out_path}  ({size_kb:.1f} KB)")
    print(f"Preview locally:  open {out_path}")


if __name__ == "__main__":
    main()
