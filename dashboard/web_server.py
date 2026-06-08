"""
Web dashboard server for monitoring trades and P&L.
Serves a single-page HTML dashboard that auto-refreshes.
Reads trade data from logs/trades.csv and system state from a shared JSON file.

Usage:
    python -m dashboard.web_server          # standalone
    or import and call start_dashboard()    # from main.py
"""

import csv
import json
import logging
import os
import sys
import threading
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = LOG_DIR / "dashboard_state.json"
TRADES_CSV = LOG_DIR / "trades.csv"
AUTO_TRADES_CSV = LOG_DIR / "auto_trades.csv"
OPTION_IDEAS_FILE = LOG_DIR / "option_ideas.json"
BALANCE_HISTORY_FILE = LOG_DIR / "balance_history.json"
LATEST_PRICES_FILE = LOG_DIR / "latest_prices.json"
OPTION_MARKS_FILE = LOG_DIR / "option_marks.json"
BROKER_SNAPSHOT_FILE = LOG_DIR / "broker_snapshot.json"
SLEEVE_ASSIGNMENTS_FILE = LOG_DIR / "sleeve_assignments.json"
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8877"))


def read_broker_snapshot() -> Dict:
    """Read the broker-authoritative account/position snapshot."""
    if BROKER_SNAPSHOT_FILE.exists():
        try:
            with open(BROKER_SNAPSHOT_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def read_trades() -> List[Dict]:
    """Read all trades from the CSV journal."""
    trades = []
    if not TRADES_CSV.exists():
        return trades
    with open(TRADES_CSV, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)
    return trades


def compute_stats(trades: List[Dict]) -> Dict:
    """Compute performance statistics from trade records."""
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    open_trades = [t for t in trades if t.get("status") == "OPEN"]

    if not closed:
        return {
            "total_trades": len(trades),
            "open_trades": len(open_trades),
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "avg_pnl": 0,
            "best_trade": 0,
            "worst_trade": 0,
            "profit_factor": 0,
            "max_drawdown": 0,
        }

    pnls = [float(t.get("pnl") or 0) for t in closed]
    # Extract close dates for each trade (for the equity curve x-axis).
    # Prefer exit_time (from auto_trades.csv) over timestamp (entry time).
    close_dates = []
    for t in closed:
        ts = t.get("exit_time") or t.get("exit_timestamp") or t.get("timestamp") or ""
        close_dates.append(ts[:10] if len(ts) >= 10 else "")

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)

    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    # Max drawdown
    cumulative = []
    running = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        running += p
        cumulative.append(running)
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(closed), 2) if closed else 0,
        "best_trade": round(max(pnls), 2) if pnls else 0,
        "worst_trade": round(min(pnls), 2) if pnls else 0,
        "profit_factor": profit_factor,
        "max_drawdown": round(max_dd, 2),
        "cumulative_pnl": [round(c, 2) for c in cumulative],
        "cumulative_dates": close_dates,
    }


def get_strategy_breakdown(trades: List[Dict]) -> List[Dict]:
    """Break down performance by strategy."""
    strategies = {}
    for t in trades:
        if t.get("status") != "CLOSED":
            continue
        name = t.get("strategy", "unknown")
        if name not in strategies:
            strategies[name] = {"name": name, "trades": 0, "wins": 0, "pnl": 0.0}
        pnl = float(t.get("pnl") or 0)
        strategies[name]["trades"] += 1
        strategies[name]["pnl"] += pnl
        if pnl > 0:
            strategies[name]["wins"] += 1

    result = []
    for s in strategies.values():
        s["pnl"] = round(s["pnl"], 2)
        s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0
        result.append(s)

    return sorted(result, key=lambda x: x["pnl"], reverse=True)


def get_daily_pnl(trades: List[Dict]) -> List[Dict]:
    """Aggregate P&L by day."""
    daily = {}
    for t in trades:
        if t.get("status") != "CLOSED":
            continue
        ts = t.get("timestamp", "")
        day = ts[:10] if len(ts) >= 10 else "unknown"
        if day not in daily:
            daily[day] = {"date": day, "pnl": 0.0, "trades": 0}
        daily[day]["pnl"] += float(t.get("pnl") or 0)
        daily[day]["trades"] += 1

    result = []
    for d in sorted(daily.values(), key=lambda x: x["date"]):
        d["pnl"] = round(d["pnl"], 2)
        result.append(d)
    return result


def read_latest_prices() -> Dict:
    """Load the latest snapshot price + unrealized P&L per open ticker."""
    if not LATEST_PRICES_FILE.exists():
        return {}
    try:
        return json.loads(LATEST_PRICES_FILE.read_text())
    except Exception:
        return {}


def read_detailed_trades() -> List[Dict]:
    """Read the full journal from auto_trades.csv with thesis, stops, etc.

    Open trades are augmented with live market fields from latest_prices.json:
      - current_price       (native currency)
      - unrealized_pnl      (USD, already FX-normalized)
      - unrealized_pnl_pct  (percent move on entry, currency-invariant)
    """
    trades = []
    if not AUTO_TRADES_CSV.exists():
        return trades
    try:
        with open(AUTO_TRADES_CSV, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
    except Exception:
        pass

    prices = read_latest_prices()
    if prices:
        for t in trades:
            if t.get("status") != "OPEN":
                continue
            lp = prices.get(t.get("code", ""))
            if not lp:
                continue
            t["current_price"] = lp.get("price")
            t["unrealized_pnl"] = lp.get("unrealized_pnl")
            t["unrealized_pnl_pct"] = lp.get("unrealized_pnl_pct")
            t["price_ts"] = lp.get("ts")

    # ── Merge broker-only positions ──────────────────────────────
    # Any broker position with qty>0 that is NOT already in the CSV
    # as OPEN gets injected as a synthetic OPEN trade so the Positions
    # tab shows the full book. These carry the "broker_only" flag so
    # the UI can distinguish them.
    broker = read_broker_snapshot()
    csv_open_codes = set(
        t.get("code", "") for t in trades if t.get("status") == "OPEN"
    )
    # Fallback ticker→name map for well-known tickers when broker
    # doesn't return stock_name (common in paper/simulate mode).
    _TICKER_NAMES = {
        "US.AAPL": "Apple", "US.AMZN": "Amazon", "US.GOOG": "Alphabet",
        "US.GOOGL": "Alphabet", "US.MSFT": "Microsoft", "US.META": "Meta",
        "US.TSLA": "Tesla", "US.NVDA": "NVIDIA", "US.AVGO": "Broadcom",
        "US.JPM": "JPMorgan", "US.GS": "Goldman Sachs", "US.BA": "Boeing",
        "US.COIN": "Coinbase", "US.MSTR": "MicroStrategy", "US.PLTR": "Palantir",
        "US.UNH": "UnitedHealth", "US.QQQ": "Invesco QQQ Trust",
        "US.XLK": "Technology Select SPDR", "US.IWM": "iShares Russell 2000",
        "US.SPY": "SPDR S&P 500", "US.DIA": "SPDR Dow Jones",
        "HK.00700": "Tencent", "HK.09988": "Alibaba-W", "HK.00005": "HSBC",
        "HK.09618": "JD-SW", "HK.03690": "Meituan-W", "HK.01810": "Xiaomi-W",
        "HK.02318": "Ping An", "HK.00941": "China Mobile",
    }
    HKD_PER_USD = float(broker.get("hk", {}).get("fx_hkd_per_usd", 7.78) or 7.78)
    # Build map: code → most recent closed trade's strategy + open timestamp.
    # Broker-only positions inherit the last known strategy for that ticker,
    # and we use the first-seen broker snapshot time (persisted) as entry date.
    _last_strategy = {}
    for t in trades:
        code_t = t.get("code", "")
        if t.get("strategy") and t.get("status") == "CLOSED":
            _last_strategy[code_t] = t.get("strategy")
    # Load persisted first-seen timestamps for broker positions
    _first_seen_path = LOG_DIR / "broker_first_seen.json"
    _first_seen = {}
    if _first_seen_path.exists():
        try:
            _first_seen = json.loads(_first_seen_path.read_text())
        except Exception:
            _first_seen = {}
    _first_seen_changed = False
    for p in broker.get("positions") or []:
        code = p.get("code", "")
        qty = float(p.get("qty") or 0)
        # Persist first-seen timestamp for ALL positions (options too)
        if code not in _first_seen and qty != 0:
            _first_seen[code] = broker.get("ts", "")
            _first_seen_changed = True
        if qty <= 0 or code in csv_open_codes:
            continue
        # Infer entry from (market_val - unrealized) / qty
        mv_local = float(p.get("market_val_local") or 0)
        pl_usd = float(p.get("pl_val_usd") or 0)
        market = p.get("market", "US")
        fx = HKD_PER_USD if market == "HK" else 1.0
        pl_local = pl_usd * fx
        cost_total = mv_local - pl_local
        entry_price = round(cost_total / qty, 2) if qty > 0 else 0
        current_price = float(p.get("nominal_price") or 0)
        unrealized_pnl = pl_usd
        unrealized_pnl_pct = float(p.get("pl_ratio_pct") or 0)
        # Resolve name: prefer broker stock_name, then fallback map
        stock_name = (p.get("stock_name") or "").strip()
        if not stock_name:
            stock_name = _TICKER_NAMES.get(code, "")
        # Resolve strategy: inherit from most recent closed trade for this ticker
        inherited_strategy = _last_strategy.get(code, "inherited")
        trades.append({
            "code": code,
            "name": stock_name,
            "side": "LONG",
            "qty": str(int(qty)),
            "entry_price": str(entry_price),
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "stop_loss": "0",
            "take_profit": "0",
            "strategy": inherited_strategy,
            "signal_score": "0",
            "order_id": f"broker_{code}",
            "status": "OPEN",
            "pod": "",
            "thesis": "Pre-existing broker position (not opened by bot)",
            "timestamp": _first_seen.get(code, broker.get("ts", "")),
            "broker_only": True,
        })
    # Persist first-seen timestamps
    if _first_seen_changed:
        try:
            _first_seen_path.write_text(json.dumps(_first_seen))
        except Exception:
            pass
    return trades


def read_balance_history() -> List[Dict]:
    """Load the account balance timeseries written each cycle by auto_trader."""
    if not BALANCE_HISTORY_FILE.exists():
        return []
    try:
        return json.loads(BALANCE_HISTORY_FILE.read_text())
    except Exception:
        return []


def read_option_marks() -> Dict:
    """Load the live option mid-price feed written by auto_trader each cycle."""
    if not OPTION_MARKS_FILE.exists():
        return {"updated_at": None, "quotes": {}}
    try:
        return json.loads(OPTION_MARKS_FILE.read_text())
    except Exception:
        return {"updated_at": None, "quotes": {}}


def compute_option_pnl(idea: Dict, current_underlying: float, option_quotes: Dict = None) -> Dict:
    """Compute unrealized P&L for a single option idea.

    Preferred path: live option mid price from `option_marks.json` (real MTM,
    includes time value). Fallback: intrinsic payoff-at-expiry using the
    current underlying — this is a theoretical max, not a current mark, and
    the `mark_source` field on the return dict tells the UI which path ran so
    it can render the right label.

    All P&L is per-contract × 100 multiplier (standard US equity option).
    """
    option_quotes = option_quotes or {}
    direction = idea.get("direction", "")
    entry_u = float(idea.get("entry_underlying") or 0)
    MULT = 100  # US equity options multiplier
    pnl = None
    basis = None
    mark_source = "none"
    current_mark = None   # per-contract $ price of the whole position

    try:
        if direction == "SELL_CALL":
            entry_premium = float(idea.get("premium") or 0)
            basis = entry_premium * MULT
            oc = idea.get("option_code")
            live = option_quotes.get(oc) if oc else None
            if live and live > 0:
                # Short premium: P&L = (entry_premium - current_premium) * 100
                current_mark = live
                pnl = (entry_premium - live) * MULT
                mark_source = "live"
            elif current_underlying > 0:
                strike = float(idea.get("strike") or 0)
                intrinsic = max(current_underlying - strike, 0)
                pnl = (entry_premium - intrinsic) * MULT
                mark_source = "intrinsic"
        elif direction == "SELL_PUT":
            entry_premium = float(idea.get("premium") or 0)
            basis = entry_premium * MULT
            oc = idea.get("option_code")
            live = option_quotes.get(oc) if oc else None
            if live and live > 0:
                current_mark = live
                pnl = (entry_premium - live) * MULT
                mark_source = "live"
            elif current_underlying > 0:
                strike = float(idea.get("strike") or 0)
                intrinsic = max(strike - current_underlying, 0)
                pnl = (entry_premium - intrinsic) * MULT
                mark_source = "intrinsic"
        elif direction == "BUY_PUT":
            entry_premium = float(idea.get("premium") or 0)
            basis = entry_premium * MULT
            oc = idea.get("option_code")
            live = option_quotes.get(oc) if oc else None
            if live and live > 0:
                current_mark = live
                # Long premium: P&L = (current - entry) * 100
                pnl = (live - entry_premium) * MULT
                mark_source = "live"
            elif current_underlying > 0:
                strike = float(idea.get("strike") or 0)
                intrinsic = max(strike - current_underlying, 0)
                pnl = (intrinsic - entry_premium) * MULT
                mark_source = "intrinsic"
        elif direction == "SPREAD":
            # Bull call spread — long ATM, short OTM, net debit paid
            debit = float(idea.get("net_debit") or 0)
            basis = debit * MULT
            lc = idea.get("long_option")
            sc = idea.get("short_option")
            live_long = option_quotes.get(lc) if lc else None
            live_short = option_quotes.get(sc) if sc else None
            if live_long and live_short and live_long > 0 and live_short > 0:
                current_spread = live_long - live_short
                current_mark = current_spread
                pnl = (current_spread - debit) * MULT
                mark_source = "live"
            elif current_underlying > 0:
                ls = float(idea.get("long_strike") or 0)
                ss = float(idea.get("short_strike") or 0)
                spread_width = max(ss - ls, 0)
                intrinsic = max(min(current_underlying - ls, spread_width), 0)
                pnl = (intrinsic - debit) * MULT
                mark_source = "intrinsic"
    except Exception:
        pnl = None

    out = {
        "entry_underlying": round(entry_u, 2) if entry_u else None,
        "current_underlying": round(current_underlying, 2) if current_underlying else None,
        "current_mark": round(current_mark, 4) if current_mark is not None else None,
        "mark_source": mark_source,
    }
    if pnl is None:
        out["unrealized_pnl"] = None
        out["unrealized_pnl_pct"] = None
    else:
        out["unrealized_pnl"] = round(pnl, 2)
        out["unrealized_pnl_pct"] = round((pnl / basis * 100), 2) if basis and basis > 0 else None
    return out


def read_option_ideas() -> List[Dict]:
    """Read latest option ideas, deduplicated by (strategy, stock_code)."""
    if not OPTION_IDEAS_FILE.exists():
        return []
    try:
        with open(OPTION_IDEAS_FILE, "r") as f:
            ideas = json.load(f)
        # Deduplicate: keep latest per (strategy, stock_code)
        seen = {}
        for idea in ideas:
            key = (idea.get("strategy"), idea.get("stock_code"))
            seen[key] = idea
        result = sorted(seen.values(), key=lambda x: x.get("score", 0), reverse=True)
        return result
    except Exception:
        return []


def read_option_positions() -> List[Dict]:
    """Extract actual option positions from broker snapshot.

    Option codes follow the pattern: US.<TICKER>YYMMDD[C|P]<STRIKE>
    They have non-zero qty (negative for short/written options).
    """
    import re
    broker = read_broker_snapshot()
    first_seen_path = LOG_DIR / "broker_first_seen.json"
    first_seen = {}
    if first_seen_path.exists():
        try:
            first_seen = json.loads(first_seen_path.read_text())
        except Exception:
            pass

    # Pattern: US.<TICKER><6-digit date><C or P><strike>
    opt_re = re.compile(r'^(US\.\w+?)(\d{6})([CP])(\d+)$')
    positions = []
    for p in broker.get("positions") or []:
        code = p.get("code", "")
        qty = float(p.get("qty") or 0)
        if qty == 0:
            continue
        m = opt_re.match(code)
        if not m:
            continue
        underlying_code = m.group(1)  # e.g., US.AMZN
        date_str = m.group(2)         # e.g., 260501
        opt_type = "CALL" if m.group(3) == "C" else "PUT"
        strike_raw = int(m.group(4))
        # Strike encoding: last 3 digits are decimal (e.g., 250000 = $250.000)
        strike = strike_raw / 1000.0

        # Parse expiry date (YYMMDD)
        try:
            expiry = f"20{date_str[:2]}-{date_str[2:4]}-{date_str[4:6]}"
        except Exception:
            expiry = date_str

        # Calculate DTE
        from datetime import datetime as dt_cls
        try:
            exp_date = dt_cls.strptime(expiry, "%Y-%m-%d")
            dte = (exp_date - dt_cls.now()).days
        except Exception:
            dte = 0

        direction = "SHORT" if qty < 0 else "LONG"
        abs_qty = abs(int(qty))
        current_price = float(p.get("nominal_price") or 0)
        market_val = float(p.get("market_val_usd") or 0)
        unrealized_pnl = float(p.get("pl_val_usd") or 0)
        cost_price = float(p.get("cost_price_local") or 0)

        positions.append({
            "code": code,
            "underlying": underlying_code,
            "underlying_ticker": underlying_code.replace("US.", ""),
            "option_type": opt_type,
            "strike": strike,
            "expiry": expiry,
            "dte": max(dte, 0),
            "direction": direction,
            "qty": abs_qty,
            "current_price": round(current_price, 2),
            "market_val": round(market_val, 2),
            "cost_price": round(cost_price, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "stock_name": p.get("stock_name", ""),
            "entry_timestamp": first_seen.get(code, broker.get("ts", "")),
        })

    return positions


def read_system_state() -> Dict:
    """Read the current system state from the shared JSON file."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "mode": "PAPER",
        "connected": False,
        "kill_switch": False,
        "daily_pnl": 0,
        "open_trade_count": 0,
        "max_open_trades": 5,
        "consecutive_losses": 0,
        "last_cycle": "",
    }


def write_system_state(state: Dict) -> None:
    """Write system state for the dashboard to read."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


class DashboardHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves the dashboard and API endpoints."""

    def log_message(self, format, *args):
        pass  # suppress default logging

    def do_OPTIONS(self):
        """Handle CORS preflight for cross-origin requests (e.g. from Preview)."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_dashboard()
        elif self.path == "/api/data":
            self._serve_api_data()
        elif self.path.startswith("/api/backtest/latest"):
            self._serve_backtest_latest()
        elif self.path.startswith("/api/backtest"):
            self._serve_backtest()
        elif self.path == "/api/theses":
            self._serve_theses()
        elif self.path == "/api/sleeves":
            self._serve_sleeves()
        elif self.path.startswith("/api/pipeline/missed"):
            self._serve_pipeline_missed()
        elif self.path.startswith("/api/pipeline"):
            self._serve_pipeline()
        elif self.path == "/strategy_brief.html":
            self._serve_static_html("strategy_brief.html")
        else:
            self.send_error(404)

    def do_POST(self):
        # Stage 1 live-gate endpoints for per-order approval. Always available;
        # only meaningful when live_mode=True. In shadow mode approve/reject
        # just return a no-op because live_gate.queue_order never writes
        # pending rows in that mode.
        if self.path == "/api/stage/approve":
            return self._handle_stage_action("approve")
        if self.path == "/api/stage/reject":
            return self._handle_stage_action("reject")
        if self.path == "/api/stage/clear_halt":
            return self._handle_stage_clear_halt()
        self.send_error(404)

    def _read_post_json(self):
        import json as _j
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return _j.loads(raw or b"{}")
        except Exception:
            return {}

    def _handle_stage_action(self, action: str):
        payload = self._read_post_json()
        trade_id = payload.get("trade_id", "")
        reviewer = payload.get("reviewer", "dashboard")
        try:
            from execution.live_gate import approve_order, reject_order
            if action == "approve":
                ok, row = approve_order(trade_id, reviewer)
                result = {"ok": ok, "row": row}
            else:
                ok = reject_order(trade_id, reviewer)
                result = {"ok": ok}
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        self.send_response(200 if result.get("ok") else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result, default=str).encode())

    def _handle_stage_clear_halt(self):
        payload = self._read_post_json()
        reviewer = payload.get("reviewer", "dashboard")
        try:
            from risk.stage_controller import get_controller
            ok = get_controller().clear_halt(reviewer)
            result = {"ok": ok}
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result, default=str).encode())

    def _serve_api_data(self):
        """JSON endpoint with all dashboard data."""
        trades = read_trades()
        stats = compute_stats(trades)
        # Fold in unrealized P&L from open positions so the headline Total P&L
        # reflects real-time market moves, not just closed trades.
        prices = read_latest_prices()
        unrealized = 0.0
        live_positions = 0
        for lp in prices.values():
            try:
                unrealized += float(lp.get("unrealized_pnl", 0) or 0)
                live_positions += 1
            except Exception:
                pass
        realized = float(stats.get("total_pnl", 0) or 0)
        # Prefer broker snapshot for unrealized P&L — latest_prices.json may
        # contain stale data from already-closed tactical trades.  The broker's
        # holdings_pnl covers all currently-held positions (core + tactical).
        broker = read_broker_snapshot()
        if broker.get("holdings_pnl") is not None:
            broker_unrealized = float(broker["holdings_pnl"])
            # Use the larger of broker holdings P&L or latest_prices unrealized
            # to avoid double-counting, but broker is authoritative for open
            # positions the bot doesn't track (core sleeve, manual positions).
            unrealized = broker_unrealized
            live_positions = len(broker.get("positions", []))
        stats["realized_pnl"] = round(realized, 2)
        stats["unrealized_pnl"] = round(unrealized, 2)
        stats["total_pnl"] = round(realized + unrealized, 2)
        stats["live_positions"] = live_positions

        # Enrich option ideas with real-time P&L using live option mids (if
        # available) or underlying intrinsic as a fallback.
        option_ideas = read_option_ideas()
        marks_blob = read_option_marks()
        option_quotes = marks_blob.get("quotes", {}) or {}
        option_unrealized_total = 0.0
        option_live_count = 0
        option_intrinsic_count = 0
        for idea in option_ideas:
            code = idea.get("stock_code", "")
            current_u = None
            lp = prices.get(code)
            if lp:
                try:
                    current_u = float(lp.get("price") or 0)
                except Exception:
                    current_u = None
            if current_u is None:
                current_u = idea.get("entry_underlying")
            pnl_info = compute_option_pnl(idea, float(current_u or 0), option_quotes)
            idea.update(pnl_info)
            if pnl_info.get("mark_source") == "live":
                option_live_count += 1
            elif pnl_info.get("mark_source") == "intrinsic":
                option_intrinsic_count += 1
            if pnl_info.get("unrealized_pnl") is not None:
                option_unrealized_total += pnl_info["unrealized_pnl"]
        stats["option_unrealized_pnl"] = round(option_unrealized_total, 2)
        stats["option_marks_updated_at"] = marks_blob.get("updated_at")
        stats["option_live_marks"] = option_live_count
        stats["option_intrinsic_marks"] = option_intrinsic_count

        # Capital deployed / available. "Deployed" = absolute notional of all
        # open equity positions (in USD, FX-normalized). "Available" = balance
        # reported by the broker minus deployed long notional (short proceeds
        # free cash but are treated conservatively here). Falls back to the
        # system_state balance if no detailed_trades.
        sys_state = read_system_state()
        broker = read_broker_snapshot()
        # Prefer broker snapshot when available — single source of truth.
        if broker.get("balance_total"):
            balance = float(broker["balance_total"])
        else:
            balance = float(sys_state.get("balance", 0) or 0)
        detailed = read_detailed_trades()
        HKD_PER_USD = 7.78
        MAX_GROSS_EXPOSURE = 1.5  # mirrors auto_trader.py default cap
        deployed_long = 0.0
        deployed_short = 0.0
        long_count = 0
        short_count = 0
        hk_value = 0.0
        us_value = 0.0
        holdings_pnl = 0.0
        # If we have a broker snapshot, prefer the broker's per-position list
        # over Moomoo's `us.market_val` aggregate (which goes negative when
        # shorts dominate, making the dashboard look like one giant short).
        # We walk positions ourselves and split long/short by qty sign.
        broker_used = bool(broker.get("positions") is not None)
        if broker_used:
            holdings_pnl = float(broker.get("holdings_pnl", 0) or 0)
            for p in broker.get("positions", []):
                try:
                    q = float(p.get("qty") or 0)
                    mv = abs(float(p.get("market_val_usd") or 0))  # absolute notional
                    if q == 0 or mv == 0:
                        continue
                    if (p.get("market") or "").upper() == "HK":
                        hk_value += mv
                    else:
                        us_value += mv
                    if q > 0:
                        deployed_long += mv
                        long_count += 1
                    else:
                        deployed_short += mv
                        short_count += 1
                except (TypeError, ValueError):
                    continue
        else:
            # No broker snapshot — fall back to bot's CSV (no shorts expected here).
            for t in detailed:
                if t.get("status") != "OPEN":
                    continue
                try:
                    code = t.get("code", "")
                    qty = int(t.get("qty") or 0)
                    px = t.get("current_price") or t.get("entry_price") or 0
                    px = float(px)
                    notional = abs(qty * px)
                    if code.startswith("HK."):
                        notional /= HKD_PER_USD
                        hk_value += notional
                    else:
                        us_value += notional
                    side = (t.get("side") or "").upper()
                    if side == "LONG":
                        deployed_long += notional
                        long_count += 1
                    else:
                        deployed_short += notional
                        short_count += 1
                    upnl = t.get("unrealized_pnl")
                    if upnl is not None:
                        holdings_pnl += float(upnl)
                except Exception:
                    continue
        deployed_total = round(deployed_long + deployed_short, 2)
        gross_exposure = deployed_total
        # 现金 = broker cash from snapshot (preferred), else derived from balance
        if broker_used:
            cash = float(broker.get("cash_total", balance - deployed_long + deployed_short))
        else:
            cash = balance - deployed_long + deployed_short
        available = max(cash, 0)
        # 今日盈亏 = broker today_pnl when available; otherwise daily_pnl + unreal
        today_realized = float(sys_state.get("daily_pnl", 0) or 0)
        if broker_used and "today_pnl_broker" in broker:
            # Broker today_pl_val already includes unrealized intraday move on
            # held positions. Add bot's realized closes for the day.
            today_pnl = float(broker.get("today_pnl_broker", 0) or 0) + today_realized
        else:
            today_pnl = today_realized + holdings_pnl
        start_balance = float(sys_state.get("day_start_balance") or (balance - today_pnl) or balance)
        today_pnl_pct = (today_pnl / start_balance * 100) if start_balance > 0 else 0
        # 剩余流动性 = remaining headroom under gross exposure cap
        gross_cap = MAX_GROSS_EXPOSURE * balance
        remaining_liquidity = max(gross_cap - gross_exposure, 0)
        capital = {
            "balance": round(balance, 2),
            "deployed_long": round(deployed_long, 2),
            "deployed_short": round(deployed_short, 2),
            "deployed_total": round(deployed_total, 2),
            "deployed_pct": round((deployed_total / balance * 100) if balance > 0 else 0, 1),
            "available": round(available, 2),
            "available_pct": round((available / balance * 100) if balance > 0 else 0, 1),
            "long_count": long_count,
            "short_count": short_count,
            "net_exposure": round(deployed_long - deployed_short, 2),
            "gross_exposure": round(gross_exposure, 2),
            # New Chinese-labeled fields
            "holdings_market": {       # 持仓市场
                "HK": round(hk_value, 2),
                "US": round(us_value, 2),
                "HK_pct": round((hk_value / deployed_total * 100) if deployed_total > 0 else 0, 1),
                "US_pct": round((us_value / deployed_total * 100) if deployed_total > 0 else 0, 1),
            },
            "holdings_pnl": round(holdings_pnl, 2),                    # 持仓盈亏
            "today_pnl": round(today_pnl, 2),                          # 今日盈亏
            "today_pnl_pct": round(today_pnl_pct, 2),                  # 今日盈亏比例
            "today_realized": round(today_realized, 2),
            "cash": round(cash, 2),                                    # 现金
            "remaining_liquidity": round(remaining_liquidity, 2),      # 剩余流动性
            "gross_cap": round(gross_cap, 2),
            "liquidity_used_pct": round((gross_exposure / gross_cap * 100) if gross_cap > 0 else 0, 1),
            "source": "broker" if broker_used else "csv",
            "broker_ts": broker.get("ts") if broker_used else None,
        }

        # Per-market portfolio split. Account/cash/MV come from broker; trade
        # stats (trades, wins, realized P&L) come from the bot's CSV journal
        # since the broker doesn't tell us which closed trades belonged to
        # which market historically.
        def _market_stats(prefix: str) -> dict:
            ts = [t for t in trades if (t.get("code", "") or "").startswith(prefix)]
            closed = [t for t in ts if t.get("status") == "CLOSED"]
            wins = [t for t in closed if float(t.get("pnl") or 0) > 0]
            realized = sum(float(t.get("pnl") or 0) for t in closed)
            open_ts = [t for t in ts if t.get("status") == "OPEN"]
            return {
                "trades": len(ts),
                "closed": len(closed),
                "open": len(open_ts),
                "wins": len(wins),
                "win_rate_pct": round((len(wins) / len(closed) * 100) if closed else 0, 1),
                "realized_pnl": round(realized, 2),
            }

        us_stats = _market_stats("US.")
        hk_stats = _market_stats("HK.")
        # Live unrealized per market from broker snapshot positions
        us_unreal = sum(float(p.get("pl_val_usd", 0) or 0)
                        for p in (broker.get("positions") or []) if p.get("market") == "US")
        hk_unreal = sum(float(p.get("pl_val_usd", 0) or 0)
                        for p in (broker.get("positions") or []) if p.get("market") == "HK")

        # ── US Portfolio accounting ────────────────────────────────
        # Broker-authoritative: total_assets = cash + market_val.
        # Total return = total_assets - starting_capital (paper = $1M).
        # total_return = realized + unrealized + implied_fees
        # The bot's CSV realized_pnl does not include moomoo's commissions
        # (SEC, TAF, platform fee) — these are deducted from broker cash but
        # invisible to the bot. We derive the residual so numbers reconcile.
        STARTING_CAPITAL_US = 1_000_000.0
        us_total_assets = float(broker.get("us", {}).get("total_assets", 0) or 0)
        us_total_return = round(us_total_assets - STARTING_CAPITAL_US, 2)
        us_csv_realized = us_stats["realized_pnl"]
        # implied_fees = total_return - (csv_realized + broker_unrealized)
        # A positive value means the CSV overstates P&L vs what the broker sees.
        us_implied_fees = round(us_csv_realized + us_unreal - us_total_return, 2)
        us_broker_open = len([
            p for p in (broker.get("positions") or [])
            if p.get("market") == "US" and float(p.get("qty") or 0) > 0
        ])

        # HK accounting (same approach, in HKD)
        fx_hkd = float(broker.get("hk", {}).get("fx_hkd_per_usd", 7.78) or 7.78)
        STARTING_CAPITAL_HK_HKD = 1_000_000.0  # paper starting capital in HKD
        hk_total_assets_hkd = float(broker.get("hk", {}).get("total_assets_hkd", 0) or 0)
        hk_total_return_hkd = round(hk_total_assets_hkd - STARTING_CAPITAL_HK_HKD, 2)
        hk_csv_realized_hkd = round(hk_stats["realized_pnl"] * fx_hkd, 2)
        hk_unreal_hkd = round(hk_unreal * fx_hkd, 2)
        hk_implied_fees = round(hk_csv_realized_hkd + hk_unreal_hkd - hk_total_return_hkd, 2)

        portfolios = {
            "US": {
                "label": "US Portfolio",
                "currency": "USD",
                "starting_capital": STARTING_CAPITAL_US,
                "total_assets": us_total_assets,
                "cash": float(broker.get("us", {}).get("cash", 0) or 0),
                "market_val": float(broker.get("us", {}).get("market_val", 0) or 0),
                "frozen_cash": float(broker.get("us", {}).get("frozen_cash", 0) or 0),
                "power": float(broker.get("us", {}).get("power", 0) or 0),
                "unrealized_pnl": round(us_unreal, 2),
                "realized_pnl": us_csv_realized,
                "total_return": us_total_return,
                "total_pnl": round(us_csv_realized + us_unreal, 2),
                "implied_fees": us_implied_fees,
                "trades": us_stats["trades"],
                "closed_trades": us_stats["closed"],
                "open_trades": us_stats["open"],
                "wins": us_stats["wins"],
                "win_rate_pct": us_stats["win_rate_pct"],
                "broker_open_positions": us_broker_open,
            },
            "HK": {
                "label": "HK Portfolio",
                "currency": "HKD",
                "currency_symbol": "HK$",
                "fx_hkd_per_usd": fx_hkd,
                "starting_capital": round(STARTING_CAPITAL_HK_HKD, 2),
                "total_assets": float(broker.get("hk", {}).get("total_assets_hkd", 0) or 0),
                "cash": float(broker.get("hk", {}).get("cash_hkd", 0) or 0),
                "market_val": float(broker.get("hk", {}).get("market_val_hkd", 0) or 0),
                "frozen_cash": float(broker.get("hk", {}).get("frozen_cash_hkd", 0) or 0),
                "power": float(broker.get("hk", {}).get("power_hkd", 0) or 0),
                "total_assets_usd": float(broker.get("hk", {}).get("total_assets_usd", 0) or 0),
                "unrealized_pnl": round(hk_unreal * fx_hkd, 2),
                "realized_pnl": round(hk_stats["realized_pnl"] * fx_hkd, 2),
                "total_return": hk_total_return_hkd,
                "total_pnl": round((hk_stats["realized_pnl"] + hk_unreal) * fx_hkd, 2),
                "implied_fees": hk_implied_fees,
                "trades": hk_stats["trades"],
                "closed_trades": hk_stats["closed"],
                "open_trades": hk_stats["open"],
                "wins": hk_stats["wins"],
                "win_rate_pct": hk_stats["win_rate_pct"],
                # Open positions count from broker (includes manual/untracked)
                "broker_open_positions": len([
                    p for p in (broker.get("positions") or [])
                    if p.get("market") == "HK" and float(p.get("qty") or 0) > 0
                ]),
            },
        }

        # Augment cumulative_dates with exit dates from detailed trades (has exit_time)
        closed_detailed = [t for t in detailed if t.get("status") == "CLOSED"]
        if closed_detailed and "cumulative_dates" in stats:
            exit_dates = []
            for t in closed_detailed:
                ts = t.get("exit_time") or t.get("timestamp") or ""
                exit_dates.append(ts[:10] if len(ts) >= 10 else "")
            # Only override if counts match (both come from same trade set)
            if len(exit_dates) == len(stats.get("cumulative_dates", [])):
                stats["cumulative_dates"] = exit_dates

        # Per-position holdings for pie charts (broker-authoritative).
        # Includes BOTH long (qty>0) and short (qty<0) positions, with side
        # marked explicitly so the UI can render them correctly.
        holdings = []
        for p in (broker.get("positions") or []):
            try:
                qty = float(p.get("qty") or 0)
                mv_usd = float(p.get("market_val_usd") or 0)
                if qty == 0:
                    continue  # only skip flat positions
                holdings.append({
                    "code": p.get("code"),
                    "name": p.get("stock_name") or p.get("code"),
                    "market": p.get("market"),
                    "qty": qty,
                    "side": "SHORT" if qty < 0 else "LONG",
                    "market_val_usd": round(abs(mv_usd), 2),  # absolute notional
                    "market_val_local": round(abs(float(p.get("market_val_local") or mv_usd)), 2),
                    "pl_val_usd": round(float(p.get("pl_val_usd") or 0), 2),
                    "pl_ratio_pct": round(float(p.get("pl_ratio_pct") or 0), 2),
                })
            except (TypeError, ValueError):
                continue

        # Stage 1/2/3 live deployment snapshot (HWM, floors, gates, pending
        # approvals). Always shadow-safe: this is read-only state, the gate
        # module never places orders itself.
        stage_snapshot = None
        stage_pending = []
        stage_shadow_recent = []
        try:
            from risk.stage_controller import get_controller as _gsc
            from execution.live_gate import list_pending as _lp
            stage_snapshot = _gsc().snapshot()
            stage_pending = _lp()
            # Tail of shadow log for the dashboard
            shadow_path = PROJECT_ROOT / "logs" / "live_shadow.jsonl"
            if shadow_path.exists():
                with shadow_path.open() as f:
                    lines = f.readlines()[-20:]
                for ln in lines:
                    try:
                        stage_shadow_recent.append(json.loads(ln))
                    except Exception:
                        pass
        except Exception:
            pass

        data = {
            "stats": stats,
            "trades": trades[-50:],  # last 50 trades
            "detailed_trades": detailed,
            "strategy_breakdown": get_strategy_breakdown(trades),
            "daily_pnl": get_daily_pnl(trades),
            "option_positions": read_option_positions(),
            "option_ideas": option_ideas,
            "balance_history": read_balance_history(),
            "system": sys_state,
            "capital": capital,
            "portfolios": portfolios,
            "holdings": holdings,
            "stage": stage_snapshot,
            "stage_pending": stage_pending,
            "stage_shadow_recent": stage_shadow_recent,
            "updated_at": datetime.now().isoformat(),
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _serve_backtest(self):
        """Run a backtest via the backtest engine and return JSON results."""
        from urllib.parse import urlparse, parse_qs
        import subprocess
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        start = params.get("start", ["2025-01-01"])[0]
        end = params.get("end", [datetime.now().strftime("%Y-%m-%d")])[0]
        universe = params.get("universe", ["us"])[0]
        interval = params.get("interval", ["1d"])[0]
        regime = params.get("regime", ["0"])[0] == "1"
        hybrid = params.get("hybrid", ["0"])[0] == "1"
        pit_hybrid = params.get("pit_hybrid", ["0"])[0] == "1"

        # Build the command
        cmd = [
            sys.executable, "backtest.py",
            "--start", start, "--end", end,
            "--universe", universe, "--interval", interval,
        ]
        if pit_hybrid:
            cmd.append("--pit-hybrid")
        elif hybrid:
            cmd.append("--hybrid")
        elif regime:
            cmd.append("--regime")

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
                cwd=str(Path(__file__).parent.parent),
            )
            # Find the output file
            suffix = f"_{universe}_{interval}"
            if pit_hybrid:
                suffix += "_pit_hybrid"
            elif hybrid:
                suffix += "_hybrid"
            elif regime:
                suffix += "_regime"
            out_path = LOG_DIR / f"backtest_{start}_{end}{suffix}.json"
            if out_path.exists():
                data = json.loads(out_path.read_text())
            else:
                data = {"error": "Backtest completed but output file not found",
                        "stdout": proc.stdout[-2000:] if proc.stdout else "",
                        "stderr": proc.stderr[-2000:] if proc.stderr else ""}
        except subprocess.TimeoutExpired:
            data = {"error": "Backtest timed out (10 min limit)"}
        except Exception as e:
            data = {"error": str(e)}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _serve_backtest_latest(self):
        """Return the most recent backtest result file."""
        import glob
        bt_files = sorted(
            LOG_DIR.glob("backtest_*.json"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        data = {}
        if bt_files:
            try:
                data = json.loads(bt_files[0].read_text())
            except Exception:
                data = {"error": "Failed to parse latest backtest file"}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _serve_sleeves(self):
        """Return sleeve assignment data from logs/sleeve_assignments.json."""
        data = {}
        if SLEEVE_ASSIGNMENTS_FILE.exists():
            try:
                data = json.loads(SLEEVE_ASSIGNMENTS_FILE.read_text())
            except Exception:
                data = {"error": "Failed to parse sleeve assignments file"}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _serve_pipeline(self):
        """
        Signal-flow pipeline — reads the unified lifecycle log at
        logs/signal_events.jsonl and produces a correct funnel by grouping
        events by signal_id.  Supports query params:

            ?window=24         hours of history to include (default 24)
            ?market=US|HK|all  filter by market (default all)
            ?strategy=NAME     filter by strategy name (default all)
        """
        from collections import defaultdict as _dd, Counter as _Counter
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from urllib.parse import urlparse as _urlparse, parse_qs as _qs

        qs = _qs(_urlparse(self.path).query)
        try:
            window_h = int((qs.get("window") or ["24"])[0])
        except Exception:
            window_h = 24
        market_filter = (qs.get("market") or ["all"])[0].upper()
        strategy_filter = (qs.get("strategy") or ["all"])[0]

        now = _dt.now(_tz.utc)
        cutoff = now - _td(hours=window_h)

        # Read the unified signal events log
        events = []
        ev_path = LOG_DIR / "signal_events.jsonl"
        if ev_path.exists():
            try:
                raw_lines = ev_path.read_text().splitlines()
                # Scan from the tail; stop once we cross the window
                for line in reversed(raw_lines[-20000:]):
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    ts_str = r.get("ts", "")
                    try:
                        t = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=_tz.utc)
                    except Exception:
                        continue
                    if t < cutoff:
                        break  # reversed — older from here
                    if market_filter != "ALL" and r.get("market", "") != market_filter:
                        continue
                    if strategy_filter != "all" and r.get("strategy", "") != strategy_filter:
                        continue
                    events.append(r)
            except Exception:
                pass

        # Events are newest-first after the reverse scan; reverse again for chrono groups
        events_chrono = list(reversed(events))

        # ── Group by signal_id to reconstruct each idea's path ─────────
        by_sid = _dd(list)
        for e in events_chrono:
            sid = e.get("signal_id") or f"anon:{e.get('symbol','')}:{e.get('strategy','')}"
            by_sid[sid].append(e)

        # For each signal, determine the furthest stage it reached + final verdict
        STAGE_ORDER = ["originated", "scored", "denied", "allowed",
                       "queued", "filled"]
        # Canonical ordered funnel stages (what we surface in the chart)
        FUNNEL_STAGES = [
            ("originated",  "Originated"),
            ("passed_score","Scored ≥ min"),
            ("passed_gate", "Gate ✓"),
            ("queued",      "Queued"),
            ("filled",      "Filled"),
        ]
        funnel = {k: 0 for k, _ in FUNNEL_STAGES}
        deny_reason_counts = _Counter()
        per_strategy_funnel = _dd(lambda: {k: 0 for k, _ in FUNNEL_STAGES})
        per_market_funnel   = _dd(lambda: {k: 0 for k, _ in FUNNEL_STAGES})
        per_strategy_denies = _dd(_Counter)

        lifecycles = []
        for sid, evs in by_sid.items():
            stages_seen = {e.get("stage") for e in evs}
            verdicts    = [e.get("verdict") for e in evs]
            strat = evs[0].get("strategy", "")
            market = evs[0].get("market", "")
            symbol = evs[0].get("symbol", "")

            # Did the signal originate?  (scored_fail also counts as a candidate)
            had_origination = "originated" in stages_seen
            had_scored_fail = any(e.get("stage") == "scored" and e.get("verdict") == "FAIL"
                                  for e in evs)
            candidate = had_origination or had_scored_fail

            # Binary flags for each funnel stage (one signal counts once per stage)
            passed_score = had_origination  # originated means score >= MIN_SIGNAL_SCORE
            passed_gate  = "allowed" in stages_seen or "queued" in stages_seen or "filled" in stages_seen
            was_queued   = "queued" in stages_seen
            was_filled   = "filled" in stages_seen

            if candidate:
                funnel["originated"] += 1
                per_strategy_funnel[strat]["originated"] += 1
                per_market_funnel[market]["originated"] += 1
            if passed_score:
                funnel["passed_score"] += 1
                per_strategy_funnel[strat]["passed_score"] += 1
                per_market_funnel[market]["passed_score"] += 1
            if passed_gate:
                funnel["passed_gate"] += 1
                per_strategy_funnel[strat]["passed_gate"] += 1
                per_market_funnel[market]["passed_gate"] += 1
            if was_queued:
                funnel["queued"] += 1
                per_strategy_funnel[strat]["queued"] += 1
                per_market_funnel[market]["queued"] += 1
            if was_filled:
                funnel["filled"] += 1
                per_strategy_funnel[strat]["filled"] += 1
                per_market_funnel[market]["filled"] += 1

            # Deny reason accounting (only if the signal never passed all gates)
            if not passed_gate:
                for e in evs:
                    dr = e.get("deny_reason")
                    if dr:
                        deny_reason_counts[dr] += 1
                        per_strategy_denies[strat][dr] += 1
                        break  # first DENY per signal only

            # Assemble lifecycle
            final = evs[-1]
            lifecycles.append({
                "signal_id": sid,
                "symbol": symbol,
                "strategy": strat,
                "market": market,
                "direction": final.get("direction", ""),
                "score": evs[0].get("score"),
                "count": len(evs),
                "final_stage": final.get("stage", ""),
                "final_verdict": final.get("verdict", ""),
                "first_ts": evs[0].get("ts", ""),
                "last_ts": final.get("ts", ""),
                "events": evs[-10:],
                "was_filled": was_filled,
                "was_denied": (not passed_gate) and candidate,
            })

        # Conversion rates between consecutive stages
        conversions = []
        stage_keys = [k for k, _ in FUNNEL_STAGES]
        for i in range(1, len(stage_keys)):
            prev_k = stage_keys[i-1]
            cur_k  = stage_keys[i]
            prev_v = funnel[prev_k]
            cur_v  = funnel[cur_k]
            rate = (cur_v / prev_v) if prev_v > 0 else 0.0
            conversions.append({
                "from": prev_k, "to": cur_k,
                "from_count": prev_v, "to_count": cur_v,
                "rate": round(rate, 4),
                "dropped": prev_v - cur_v,
            })

        # Identify biggest bottleneck (largest absolute drop where rate < 0.8)
        bottleneck = None
        max_drop = 0
        for c in conversions:
            if c["from_count"] > 0 and c["dropped"] > max_drop:
                max_drop = c["dropped"]
                bottleneck = c

        # Recent events tail for the live stream (newest first, cap 200)
        events_tail = events[:200]

        # Top per-strategy and per-market rollups (sorted by originated desc)
        def _rollup(d):
            out = []
            for k, v in d.items():
                orig = v.get("originated", 0)
                filled = v.get("filled", 0)
                fill_rate = (filled / orig) if orig > 0 else 0.0
                out.append({"key": k or "(none)", **v, "fill_rate": round(fill_rate, 4)})
            return sorted(out, key=lambda x: -x["originated"])

        # Lifecycles sorted: filled first, then denied (most-recent denied), then others
        lifecycles.sort(key=lambda x: (
            0 if x["was_filled"] else (1 if x["was_denied"] else 2),
            -((_dt.fromisoformat(x["last_ts"].replace("Z","+00:00"))
               if x["last_ts"] else now).timestamp())
        ))

        payload = {
            "generated_at": now.isoformat(),
            "window_hours": window_h,
            "market_filter": market_filter,
            "strategy_filter": strategy_filter,
            "funnel": funnel,
            "funnel_stages": [{"key":k,"label":lbl} for k,lbl in FUNNEL_STAGES],
            "conversions": conversions,
            "bottleneck": bottleneck,
            "deny_reasons": [{"reason": r, "count": c}
                             for r, c in deny_reason_counts.most_common()],
            "per_strategy": _rollup(per_strategy_funnel),
            "per_market": _rollup(per_market_funnel),
            "per_strategy_denies": {
                s: [{"reason": r, "count": c} for r, c in ctr.most_common()]
                for s, ctr in per_strategy_denies.items()
            },
            "events": events_tail,
            "lifecycles": lifecycles[:100],
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, default=str).encode())

    def _serve_pipeline_missed(self):
        """
        Missed-opportunity analysis: for each DENY in the window, fetch
        recent-history kline data and compute forward return N bars after
        the denial.  Reveals whether filters are rejecting winners.

        Query params:
            ?window=168    hours of history (default 168 = 7 days)
            ?horizon=3     trading days forward to measure return
            ?reason=REASON restrict to a specific deny reason
        """
        from collections import defaultdict as _dd
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from urllib.parse import urlparse as _urlparse, parse_qs as _qs

        qs = _qs(_urlparse(self.path).query)
        try:
            window_h = int((qs.get("window") or ["168"])[0])
            horizon  = int((qs.get("horizon") or ["3"])[0])
        except Exception:
            window_h, horizon = 168, 3
        reason_filter = (qs.get("reason") or ["all"])[0]

        now = _dt.now(_tz.utc)
        cutoff = now - _td(hours=window_h)

        ev_path = LOG_DIR / "signal_events.jsonl"
        if not ev_path.exists():
            self._send_json({"rows": [], "summary": {"count": 0}})
            return

        # Collect unique (symbol, strategy, signal_id) with the first DENY
        first_deny = {}
        try:
            for line in ev_path.read_text().splitlines()[-20000:]:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("verdict") != "DENY":
                    continue
                if reason_filter != "all" and r.get("deny_reason") != reason_filter:
                    continue
                ts_str = r.get("ts", "")
                try:
                    t = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=_tz.utc)
                except Exception:
                    continue
                if t < cutoff:
                    continue
                sid = r.get("signal_id") or f"{r.get('symbol')}:{r.get('strategy')}:{ts_str}"
                if sid not in first_deny:
                    first_deny[sid] = (r, t)
        except Exception:
            pass

        # Try to look up forward returns from latest_prices or cached klines.
        # We use logs/latest_prices.json as a cheap price reference; for a
        # proper post-denial return we need historical daily klines.  To
        # avoid pulling fresh data on every request, we read a cached
        # snapshot written by auto_trader's kline helper if available.
        price_lookup = {}
        try:
            lp_path = LOG_DIR / "latest_prices.json"
            if lp_path.exists():
                price_lookup = json.loads(lp_path.read_text())
        except Exception:
            pass

        # Read moomoo kline cache if it exists (written by pipeline tools)
        kline_cache = {}
        cache_path = LOG_DIR / "kline_cache.json"
        if cache_path.exists():
            try:
                kline_cache = json.loads(cache_path.read_text())
            except Exception:
                pass

        rows = []
        for sid, (r, t) in first_deny.items():
            sym = r.get("symbol", "")
            # Forward return: look up current price vs. the price implied at
            # denial time (we don't have that precisely — but if the denial
            # is fresh, we estimate).
            cur_px = (price_lookup.get(sym) or {}).get("price")
            fwd = None
            if cur_px and sym in kline_cache:
                kl = kline_cache[sym]
                # The kline closest to the denial time
                try:
                    closes = kl.get("closes", [])
                    if closes:
                        base = float(closes[-(horizon+1)]) if len(closes) > horizon else float(closes[0])
                        fwd = (float(cur_px) - base) / base if base else None
                except Exception:
                    pass
            rows.append({
                "signal_id": sid,
                "ts": r.get("ts"),
                "symbol": sym,
                "strategy": r.get("strategy", ""),
                "direction": r.get("direction", ""),
                "score": r.get("score"),
                "reason": r.get("deny_reason", "other"),
                "detail": r.get("detail", ""),
                "current_price": cur_px,
                "forward_return_pct": round(fwd * 100, 2) if fwd is not None else None,
            })

        # Summary: how many denied signals moved favourably afterward?
        valid = [r for r in rows if r["forward_return_pct"] is not None]
        wins = [r for r in valid
                if (r["direction"] == "LONG"  and r["forward_return_pct"] > 0)
                or (r["direction"] == "SHORT" and r["forward_return_pct"] < 0)]
        summary = {
            "count": len(rows),
            "with_forward_data": len(valid),
            "would_have_profited": len(wins),
            "hit_rate": round(len(wins) / len(valid), 4) if valid else None,
            "avg_forward_return_pct": round(
                sum(r["forward_return_pct"] for r in valid) / len(valid), 2
            ) if valid else None,
        }
        # Sort: biggest move first (absolute)
        rows.sort(key=lambda x: abs(x["forward_return_pct"] or 0), reverse=True)
        self._send_json({"rows": rows[:200], "summary": summary,
                         "window_hours": window_h, "horizon_days": horizon})

    def _send_json(self, payload):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, default=str).encode())

    def _serve_theses(self):
        """Return active trade theses (signals + fundamentals + qualitative)."""
        try:
            from data.trade_thesis import get_active_theses
            theses = get_active_theses()
            # Enrich with current price + live P&L from broker snapshot
            broker = read_broker_snapshot()
            price_by_code = {p["code"]: float(p.get("market_val_usd",0))/abs(float(p.get("qty") or 1))
                             for p in (broker.get("positions") or []) if p.get("qty")}
            pnl_by_code = {p["code"]: float(p.get("pl_val_usd",0))
                           for p in (broker.get("positions") or [])}
            for code, t in theses.items():
                t["current_price"] = round(price_by_code.get(code, 0), 2) or None
                t["unrealized_pnl"] = round(pnl_by_code.get(code, 0), 2)
                if t.get("current_price") and t.get("entry_price"):
                    raw_chg = (t["current_price"]/t["entry_price"] - 1) * 100
                    t["price_change_pct"] = round(raw_chg if t["side"] == "LONG" else -raw_chg, 2)
            self._send_json({
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "count": len(theses),
                "theses": list(theses.values()),
            })
        except Exception as e:
            self._send_json({"error": str(e), "theses": [], "count": 0})

    def _serve_static_html(self, filename: str):
        """Serve a static HTML file from the project root."""
        html_file = PROJECT_ROOT / filename
        if html_file.exists():
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html_file.read_bytes())
        else:
            self.send_error(404, f"{filename} not found")

    def _serve_dashboard(self):
        """Serve the main HTML dashboard."""
        html_file = Path(__file__).parent / "dashboard.html"
        if html_file.exists():
            html = html_file.read_text()
        else:
            html = DASHBOARD_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())


def start_dashboard(port: int = DASHBOARD_PORT, background: bool = True):
    """Start the web dashboard server."""
    import socket
    try:
        server = HTTPServer(("0.0.0.0", port), DashboardHandler)
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError as e:
        if e.errno == 48:  # Address already in use
            logger.info(f"Dashboard port {port} already in use — assuming external instance is running")
            return
        raise
    if background:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Dashboard running at http://localhost:{port}")
    else:
        logger.info(f"Dashboard running at http://localhost:{port}")
        server.serve_forever()


# ── HTML Dashboard ──────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Moomoo Trading Dashboard</title>
<style>
:root {
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --text-dim: #8b949e;
    --green: #3fb950;
    --green-bg: rgba(63,185,80,0.1);
    --red: #f85149;
    --red-bg: rgba(248,81,73,0.1);
    --blue: #58a6ff;
    --blue-bg: rgba(88,166,255,0.1);
    --yellow: #d29922;
    --yellow-bg: rgba(210,153,34,0.1);
    --purple: #bc8cff;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
}
.header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.header h1 { font-size: 18px; font-weight: 600; }
.header .mode {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
}
.mode-paper { background: var(--blue-bg); color: var(--blue); border: 1px solid var(--blue); }
.mode-live { background: var(--red-bg); color: var(--red); border: 1px solid var(--red); }
.status-bar {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 8px 24px;
    display: flex;
    gap: 24px;
    font-size: 12px;
    color: var(--text-dim);
}
.status-bar .dot {
    width: 8px; height: 8px; border-radius: 50%;
    display: inline-block; margin-right: 6px;
}
.dot-green { background: var(--green); }
.dot-red { background: var(--red); }
.dot-yellow { background: var(--yellow); }
.container { padding: 20px 24px; max-width: 1400px; margin: 0 auto; }
.grid { display: grid; gap: 16px; margin-bottom: 20px; }
.grid-4 { grid-template-columns: repeat(4, 1fr); }
.grid-3 { grid-template-columns: repeat(3, 1fr); }
.grid-2 { grid-template-columns: repeat(2, 1fr); }
@media (max-width: 900px) {
    .grid-4, .grid-3 { grid-template-columns: repeat(2, 1fr); }
    .grid-2 { grid-template-columns: 1fr; }
}
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
}
.card-header {
    font-size: 12px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 8px;
}
.card-value {
    font-size: 28px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
}
.card-sub {
    font-size: 12px;
    color: var(--text-dim);
    margin-top: 4px;
}
.positive { color: var(--green); }
.negative { color: var(--red); }
.neutral { color: var(--text-dim); }
.section-title {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 12px;
    color: var(--text);
}
table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}
th {
    text-align: left;
    padding: 8px 12px;
    background: var(--bg);
    color: var(--text-dim);
    font-weight: 500;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border);
}
td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    font-variant-numeric: tabular-nums;
}
tr:hover td { background: rgba(255,255,255,0.02); }
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
}
.badge-buy { background: var(--green-bg); color: var(--green); }
.badge-sell { background: var(--red-bg); color: var(--red); }
.badge-open { background: var(--yellow-bg); color: var(--yellow); }
.badge-closed { background: var(--blue-bg); color: var(--blue); }
.chart-container {
    width: 100%;
    height: 200px;
    position: relative;
}
canvas { width: 100% !important; height: 100% !important; }
.bar-chart { display: flex; align-items: flex-end; gap: 4px; height: 120px; padding-top: 10px; }
.bar {
    flex: 1;
    min-width: 20px;
    max-width: 60px;
    border-radius: 3px 3px 0 0;
    position: relative;
    transition: opacity 0.2s;
    cursor: default;
}
.bar:hover { opacity: 0.8; }
.bar-label {
    position: absolute;
    bottom: -20px;
    left: 50%;
    transform: translateX(-50%);
    font-size: 10px;
    color: var(--text-dim);
    white-space: nowrap;
}
.bar-value {
    position: absolute;
    top: -18px;
    left: 50%;
    transform: translateX(-50%);
    font-size: 10px;
    font-weight: 600;
    white-space: nowrap;
}
.equity-curve {
    width: 100%;
    height: 180px;
}
.equity-curve svg { width: 100%; height: 100%; }
.refresh-info {
    text-align: center;
    font-size: 11px;
    color: var(--text-dim);
    padding: 12px;
}
.kill-switch-warning {
    background: var(--red-bg);
    border: 1px solid var(--red);
    color: var(--red);
    padding: 12px 24px;
    text-align: center;
    font-weight: 700;
    font-size: 14px;
}
.empty-state {
    text-align: center;
    color: var(--text-dim);
    padding: 40px;
    font-size: 14px;
}
</style>
</head>
<body>

<div class="header">
    <h1>Moomoo Trading System</h1>
    <div>
        <span class="mode mode-paper" id="trading-mode">PAPER</span>
    </div>
</div>

<div id="kill-switch-banner" class="kill-switch-warning" style="display:none;">
    KILL SWITCH ACTIVE — ALL TRADING HALTED
</div>

<div class="status-bar" id="status-bar">
    <span><span class="dot dot-green" id="conn-dot"></span>
    <span id="conn-status">Connecting...</span></span>
    <span>Last update: <span id="last-update">—</span></span>
    <span>Auto-refresh: 10s</span>
</div>

<div class="container">
    <!-- KPI Cards -->
    <div class="grid grid-4" id="kpi-cards">
        <div class="card">
            <div class="card-header">Total P&L</div>
            <div class="card-value" id="total-pnl">$0.00</div>
            <div class="card-sub" id="avg-pnl">Avg: $0.00 / trade</div>
        </div>
        <div class="card">
            <div class="card-header">Win Rate</div>
            <div class="card-value" id="win-rate">0.0%</div>
            <div class="card-sub" id="win-loss">0W / 0L</div>
        </div>
        <div class="card">
            <div class="card-header">Open / Total Trades</div>
            <div class="card-value" id="trade-count">0 / 0</div>
            <div class="card-sub" id="trade-limit">Limit: 5</div>
        </div>
        <div class="card">
            <div class="card-header">Profit Factor</div>
            <div class="card-value" id="profit-factor">—</div>
            <div class="card-sub" id="max-dd">Max DD: $0.00</div>
        </div>
    </div>

    <!-- Charts Row -->
    <div class="grid grid-2">
        <div class="card">
            <div class="section-title">Cumulative P&L</div>
            <div class="equity-curve" id="equity-chart"></div>
        </div>
        <div class="card">
            <div class="section-title">Daily P&L</div>
            <div class="bar-chart" id="daily-chart" style="margin-bottom:24px;"></div>
        </div>
    </div>

    <!-- Strategy Breakdown -->
    <div class="card" style="margin-bottom:16px;">
        <div class="section-title">Strategy Performance</div>
        <table id="strategy-table">
            <thead><tr>
                <th>Strategy</th><th>Trades</th><th>Wins</th><th>Win Rate</th><th>P&L</th>
            </tr></thead>
            <tbody id="strategy-body"></tbody>
        </table>
    </div>

    <!-- Option Positions (actual broker positions) -->
    <div class="card" style="margin-bottom:16px;">
        <div class="section-title">Option Positions <span style="font-size:11px;color:var(--text-dim);font-weight:400;">(live broker positions)</span></div>
        <div style="overflow-x:auto;">
        <table id="option-pos-table">
            <thead><tr>
                <th>Ticker</th><th>Type</th><th>Direction</th><th>Strike</th>
                <th>Expiry</th><th>DTE</th><th>Qty</th><th>Price</th><th>Mkt Val</th><th>P&L</th><th>Entry Time</th>
            </tr></thead>
            <tbody id="option-pos-body"></tbody>
        </table>
        </div>
        <div class="empty-state" id="no-option-pos">No option positions</div>
    </div>

    <!-- Option Ideas -->
    <div class="card" style="margin-bottom:16px;">
        <div class="section-title">Option Ideas <span style="font-size:11px;color:var(--text-dim);font-weight:400;">(scan results — not auto-executed)</span></div>
        <div style="overflow-x:auto;">
        <table id="option-table">
            <thead><tr>
                <th>Strategy</th><th>Ticker</th><th>Direction</th><th>Strike</th>
                <th>Expiry</th><th>DTE</th><th>Premium/Debit</th><th>Score</th><th>Scan Time</th><th>Thesis</th>
            </tr></thead>
            <tbody id="option-body"></tbody>
        </table>
        </div>
        <div class="empty-state" id="no-options">No option ideas yet</div>
    </div>

    <!-- Trade Log -->
    <div class="card">
        <div class="section-title">Recent Trades</div>
        <div style="overflow-x:auto;">
        <table id="trade-table">
            <thead><tr>
                <th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th>
                <th>Entry</th><th>Exit</th><th>P&L</th><th>Strategy</th><th>Status</th>
            </tr></thead>
            <tbody id="trade-body"></tbody>
        </table>
        </div>
        <div class="empty-state" id="no-trades">No trades yet. Waiting for signals...</div>
    </div>
</div>

<div class="refresh-info">Auto-refreshes every 10 seconds</div>

<script>
const API_URL = '/api/data';

function formatMoney(v) {
    const n = parseFloat(v) || 0;
    const sign = n >= 0 ? '' : '-';
    return sign + '$' + Math.abs(n).toFixed(2);
}

function pnlClass(v) {
    const n = parseFloat(v) || 0;
    if (n > 0) return 'positive';
    if (n < 0) return 'negative';
    return 'neutral';
}

function renderEquityCurve(container, data) {
    if (!data || data.length === 0) {
        container.innerHTML = '<div class="empty-state">No closed trades</div>';
        return;
    }
    const W = container.clientWidth || 400;
    const H = 170;
    const pad = { top: 20, right: 10, bottom: 20, left: 50 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    const maxV = Math.max(...data, 0);
    const minV = Math.min(...data, 0);
    const range = maxV - minV || 1;

    const points = data.map((v, i) => {
        const x = pad.left + (i / Math.max(data.length - 1, 1)) * plotW;
        const y = pad.top + plotH - ((v - minV) / range) * plotH;
        return `${x},${y}`;
    });

    const zeroY = pad.top + plotH - ((0 - minV) / range) * plotH;
    const last = data[data.length - 1];
    const color = last >= 0 ? '#3fb950' : '#f85149';

    const fillPoints = [
        `${pad.left},${zeroY}`,
        ...points,
        `${pad.left + plotW},${zeroY}`
    ].join(' ');

    container.innerHTML = `
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
            <line x1="${pad.left}" y1="${zeroY}" x2="${W-pad.right}" y2="${zeroY}"
                  stroke="#30363d" stroke-width="1" stroke-dasharray="4,4"/>
            <polygon points="${fillPoints}" fill="${color}" opacity="0.1"/>
            <polyline points="${points.join(' ')}" fill="none" stroke="${color}" stroke-width="2"/>
            <circle cx="${points[points.length-1].split(',')[0]}" cy="${points[points.length-1].split(',')[1]}"
                    r="4" fill="${color}"/>
            <text x="${pad.left - 4}" y="${zeroY + 4}" text-anchor="end"
                  fill="#8b949e" font-size="10">$0</text>
            <text x="${pad.left - 4}" y="${pad.top + 4}" text-anchor="end"
                  fill="#8b949e" font-size="10">${formatMoney(maxV)}</text>
            <text x="${W - pad.right}" y="${parseFloat(points[points.length-1].split(',')[1]) - 8}"
                  text-anchor="end" fill="${color}" font-size="11" font-weight="600">
                ${formatMoney(last)}</text>
        </svg>
    `;
}

function renderDailyChart(container, data) {
    if (!data || data.length === 0) {
        container.innerHTML = '<div class="empty-state">No daily data</div>';
        return;
    }
    const maxAbs = Math.max(...data.map(d => Math.abs(d.pnl)), 1);
    container.innerHTML = '';

    data.slice(-14).forEach(d => {
        const pct = Math.abs(d.pnl) / maxAbs * 100;
        const bar = document.createElement('div');
        bar.className = 'bar';
        bar.style.height = Math.max(pct, 4) + '%';
        bar.style.background = d.pnl >= 0 ? 'var(--green)' : 'var(--red)';

        bar.innerHTML = `
            <span class="bar-value ${pnlClass(d.pnl)}">${formatMoney(d.pnl)}</span>
            <span class="bar-label">${d.date.slice(5)}</span>
        `;
        container.appendChild(bar);
    });
}

function renderStrategyTable(tbody, data) {
    if (!data || data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No strategy data</td></tr>';
        return;
    }
    tbody.innerHTML = data.map(s => `
        <tr>
            <td><strong>${s.name}</strong></td>
            <td>${s.trades}</td>
            <td>${s.wins}</td>
            <td>${s.win_rate}%</td>
            <td class="${pnlClass(s.pnl)}">${formatMoney(s.pnl)}</td>
        </tr>
    `).join('');
}

function renderTradeTable(tbody, trades, emptyEl) {
    if (!trades || trades.length === 0) {
        tbody.innerHTML = '';
        emptyEl.style.display = 'block';
        return;
    }
    emptyEl.style.display = 'none';

    // Show newest first
    const sorted = [...trades].reverse();
    tbody.innerHTML = sorted.map(t => {
        const side = (t.side || '').toUpperCase();
        const status = (t.status || '').toUpperCase();
        return `
        <tr>
            <td>${(t.timestamp || '').slice(0, 19)}</td>
            <td><strong>${t.code || ''}</strong></td>
            <td><span class="badge badge-${side === 'LONG' ? 'buy' : 'sell'}">${side}</span></td>
            <td>${t.quantity || ''}</td>
            <td>$${parseFloat(t.entry_price || 0).toFixed(2)}</td>
            <td>${t.exit_price && parseFloat(t.exit_price) > 0 ? '$' + parseFloat(t.exit_price).toFixed(2) : '—'}</td>
            <td class="${pnlClass(t.pnl)}">${t.pnl && parseFloat(t.pnl) !== 0 ? formatMoney(t.pnl) : '—'}</td>
            <td>${t.strategy || ''}</td>
            <td><span class="badge badge-${status === 'OPEN' ? 'open' : 'closed'}">${status}</span></td>
        </tr>`;
    }).join('');
}

function renderOptionPositions(tbody, positions, emptyEl) {
    if (!positions || positions.length === 0) {
        tbody.innerHTML = '';
        emptyEl.style.display = 'block';
        return;
    }
    emptyEl.style.display = 'none';

    tbody.innerHTML = positions.map(p => {
        const dirBadge = p.direction === 'SHORT'
            ? '<span class="badge badge-sell">SHORT</span>'
            : '<span class="badge badge-buy">LONG</span>';
        const typeBadge = p.option_type === 'CALL'
            ? '<span class="badge" style="background:#2563eb;color:#fff;">CALL</span>'
            : '<span class="badge" style="background:#9333ea;color:#fff;">PUT</span>';
        const pnl = p.unrealized_pnl || 0;
        const pnlColor = pnl >= 0 ? 'var(--green)' : 'var(--red)';
        const pnlStr = pnl >= 0 ? `+$${pnl.toFixed(0)}` : `-$${Math.abs(pnl).toFixed(0)}`;
        const entryTs = p.entry_timestamp || '';
        const entryDisplay = entryTs ? new Date(entryTs).toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
        const dteBadge = p.dte <= 7
            ? `<span style="color:var(--red);font-weight:600;">${p.dte}d</span>`
            : `${p.dte}d`;
        return `<tr>
            <td><strong>${p.underlying_ticker}</strong></td>
            <td>${typeBadge}</td>
            <td>${dirBadge}</td>
            <td>$${p.strike.toFixed(0)}</td>
            <td>${p.expiry}</td>
            <td>${dteBadge}</td>
            <td>${p.qty}</td>
            <td>$${p.current_price.toFixed(2)}</td>
            <td>$${p.market_val.toFixed(0)}</td>
            <td style="color:${pnlColor};font-weight:600;">${pnlStr}</td>
            <td style="font-size:11px;color:var(--text-dim);">${entryDisplay}</td>
        </tr>`;
    }).join('');
}

function renderOptionIdeas(tbody, ideas, emptyEl) {
    if (!ideas || ideas.length === 0) {
        tbody.innerHTML = '';
        emptyEl.style.display = 'block';
        return;
    }
    emptyEl.style.display = 'none';

    const dirBadge = d => {
        if (d === 'SELL_CALL') return '<span class="badge badge-sell">SELL CALL</span>';
        if (d === 'SELL_PUT') return '<span class="badge badge-sell">SELL PUT</span>';
        if (d === 'BUY_PUT') return '<span class="badge badge-buy">BUY PUT</span>';
        if (d === 'SPREAD') return '<span class="badge" style="background:var(--purple);color:#fff;">SPREAD</span>';
        return d;
    };

    const stratName = s => s.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

    tbody.innerHTML = ideas.map(o => {
        const strike = o.long_strike
            ? `$${o.long_strike}/$${o.short_strike}`
            : `$${o.strike}`;
        const cost = o.net_debit
            ? `$${o.net_debit} debit`
            : `$${(o.premium || 0).toFixed(2)}`;
        const thesis = (o.thesis || '').length > 60
            ? o.thesis.substring(0, 57) + '...'
            : o.thesis || '';
        const scanTs = o.scan_time || '';
        const scanDisplay = scanTs ? new Date(scanTs).toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
        return `<tr>
            <td>${stratName(o.strategy)}</td>
            <td><strong>${(o.stock_code || '').replace('US.', '')}</strong></td>
            <td>${dirBadge(o.direction)}</td>
            <td>${strike}</td>
            <td>${o.expiry || ''}</td>
            <td>${o.dte || ''}d</td>
            <td>${cost}</td>
            <td><strong>${(o.score || 0).toFixed(2)}</strong></td>
            <td style="font-size:11px;color:var(--text-dim);">${scanDisplay}</td>
            <td style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${o.thesis || ''}">${thesis}</td>
        </tr>`;
    }).join('');
}

async function refresh() {
    try {
        const resp = await fetch(API_URL);
        const data = await resp.json();
        const s = data.stats;
        const sys = data.system;

        // Mode
        const modeEl = document.getElementById('trading-mode');
        if (sys.mode === 'LIVE') {
            modeEl.textContent = 'LIVE';
            modeEl.className = 'mode mode-live';
        } else {
            modeEl.textContent = 'PAPER';
            modeEl.className = 'mode mode-paper';
        }

        // Kill switch
        document.getElementById('kill-switch-banner').style.display =
            sys.kill_switch ? 'block' : 'none';

        // Connection
        const dot = document.getElementById('conn-dot');
        const connText = document.getElementById('conn-status');
        if (sys.connected) {
            dot.className = 'dot dot-green';
            connText.textContent = 'Connected to OpenD';
        } else {
            dot.className = 'dot dot-yellow';
            connText.textContent = 'Disconnected';
        }

        // KPIs
        const pnlEl = document.getElementById('total-pnl');
        pnlEl.textContent = formatMoney(s.total_pnl);
        pnlEl.className = 'card-value ' + pnlClass(s.total_pnl);

        document.getElementById('avg-pnl').textContent = `Avg: ${formatMoney(s.avg_pnl)} / trade`;
        document.getElementById('win-rate').textContent = s.win_rate + '%';
        document.getElementById('win-loss').textContent = `${s.wins}W / ${s.losses}L`;
        document.getElementById('trade-count').textContent = `${s.open_trades} / ${s.total_trades}`;
        document.getElementById('trade-limit').textContent = `Limit: ${sys.max_open_trades || 5}`;
        document.getElementById('profit-factor').textContent =
            s.profit_factor === Infinity ? '∞' : s.profit_factor || '—';
        document.getElementById('max-dd').textContent = `Max DD: ${formatMoney(s.max_drawdown)}`;

        // Charts
        renderEquityCurve(document.getElementById('equity-chart'), s.cumulative_pnl || []);
        renderDailyChart(document.getElementById('daily-chart'), data.daily_pnl || []);

        // Tables
        renderStrategyTable(document.getElementById('strategy-body'), data.strategy_breakdown);
        renderOptionPositions(
            document.getElementById('option-pos-body'),
            data.option_positions,
            document.getElementById('no-option-pos')
        );
        renderOptionIdeas(
            document.getElementById('option-body'),
            data.option_ideas,
            document.getElementById('no-options')
        );
        renderTradeTable(
            document.getElementById('trade-body'),
            data.trades,
            document.getElementById('no-trades')
        );

        // Timestamp
        document.getElementById('last-update').textContent =
            new Date(data.updated_at).toLocaleTimeString();

    } catch (e) {
        document.getElementById('conn-dot').className = 'dot dot-red';
        document.getElementById('conn-status').textContent = 'Dashboard error: ' + e.message;
    }
}

// Initial load + auto-refresh
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    logging.basicConfig(level=logging.INFO)
    print(f"Starting dashboard at http://localhost:{DASHBOARD_PORT}")
    start_dashboard(background=False)
