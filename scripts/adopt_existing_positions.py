"""
Adopt existing broker positions into the bot's CSV + thesis system.

The bot currently can't manage positions it didn't open. This script
walks every broker position and:
  1. Generates a thesis (signals + fundamentals + qualitative)
  2. Writes to logs/auto_trades.csv as OPEN
  3. Writes to logs/theses_active.json for dashboard display

After this runs, the bot's exit logic + risk controls cover everything,
and the dashboard shows the full thesis behind every position.

Run:  ./venv/bin/python -m scripts.adopt_existing_positions [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from moomoo import OpenUSTradeContext, OpenQuoteContext, TrdEnv, KLType, RET_OK

PROJECT_ROOT = Path(__file__).parent.parent
TRADES_CSV = PROJECT_ROOT / "logs" / "auto_trades.csv"

from data.trade_thesis import build_thesis, log_thesis, get_sector, get_tailwinds
from data.fundamentals import get_fundamentals


# ─── Per-position thesis library ────────────────────────────────────────
# Each entry is the qualitative rationale we want recorded. Quantitative
# data is pulled from yfinance automatically.

THESIS_LIB = {
    # ─── EXISTING LONGS (already on broker) ─────────────────────────────
    "US.TSM": {
        "side": "LONG",
        "thesis_summary": "Sole leading-edge foundry (3nm/2nm). AI compute, Apple, and auto silicon all depend on TSM. Capacity-constrained = pricing power.",
        "moat": ["technology_leadership","capacity_advantage","scale"],
        "tailwinds_narrative": "AI infra capex cycle + auto chips + sovereign AI. 3nm+2nm capacity sold out 2026-2027.",
        "kpis_to_watch": ["3nm utilisation","2nm pull-in","Apple+NVDA wallet share","gross margin >55%"],
        "thesis_break_conditions": ["China-Taiwan escalation","Apple+NVDA insourcing","2nm yield miss"],
        "target_upside_pct": 25,
    },
    "US.AAPL": {
        "side": "LONG",
        "thesis_summary": "iPhone installed-base monetisation via Services + Apple Intelligence on-device AI. Buyback engine.",
        "moat": ["ecosystem","brand","switching_costs","scale"],
        "tailwinds_narrative": "Services margin expansion; AI refresh cycle; Vision platform optionality.",
        "kpis_to_watch": ["Services revenue growth","gross margin >45%","iPhone units recovery"],
        "thesis_break_conditions": ["Services regulation","China demand collapse","App Store ruling"],
        "target_upside_pct": 15,
    },
    "US.GS": {
        "side": "LONG",
        "thesis_summary": "Investment banking + capital markets re-acceleration. Wealth management durable fee stream.",
        "moat": ["brand","talent","client_relationships"],
        "tailwinds_narrative": "Deal pipeline recovering; private credit growth; wealth mgmt fee tailwind.",
        "kpis_to_watch": ["IB backlog","ROE >12%","wealth AUM growth"],
        "thesis_break_conditions": ["Recession + IPO freeze","comp ratio spike"],
        "target_upside_pct": 20,
    },
    "US.GOOGL": {
        "side": "LONG",
        "thesis_summary": "Search moat + Google Cloud at scale + YouTube + AI optionality (Gemini, Waymo). Multiple shots on goal.",
        "moat": ["network_effects","data_moat","scale","brand"],
        "tailwinds_narrative": "AI ad targeting lift, GCP growth, Waymo robotaxi commercial.",
        "kpis_to_watch": ["GCP growth >30%","Search resilience to AI","YouTube CPM"],
        "thesis_break_conditions": ["AI search erosion","DOJ break-up","ad spend collapse"],
        "target_upside_pct": 30,
    },
    "US.JNJ": {
        "side": "LONG",
        "thesis_summary": "Defensive dividend compounder with pipeline. Stelara loss-of-exclusivity priced in; oncology + neuroscience growth.",
        "moat": ["brand","scale","R&D"],
        "tailwinds_narrative": "Pipeline maturation; aging demographic; consistent dividend grower.",
        "kpis_to_watch": ["Oncology revenue","gross margin >65%","dividend coverage"],
        "thesis_break_conditions": ["Pipeline failures","talc litigation escalation"],
        "target_upside_pct": 12,
    },
    "US.JPM": {
        "side": "LONG",
        "thesis_summary": "Best-in-class universal bank. Capital fortress, NIM beneficiary of higher rates, M&A optionality.",
        "moat": ["scale","brand","client_relationships"],
        "tailwinds_narrative": "Rate cycle, market share gains, fee income tailwind.",
        "kpis_to_watch": ["ROTCE >18%","NIM","credit loss provisions"],
        "thesis_break_conditions": ["Recession credit cycle","regulatory cap"],
        "target_upside_pct": 15,
    },
    "HK.00700": {
        "side": "LONG",
        "thesis_summary": "Tencent: gaming recovery + Weixin advertising leverage + ride-share/fintech optionality. China sentiment trough.",
        "moat": ["network_effects","scale","ecosystem"],
        "tailwinds_narrative": "Gaming approvals normalised; ad take-rate expansion; cost discipline.",
        "kpis_to_watch": ["gaming revenue growth","ad rev acceleration","FCF margin"],
        "thesis_break_conditions": ["regulatory tightening","tech crackdown 2.0","ADR delisting"],
        "target_upside_pct": 35,
    },

    # ─── EXISTING SHORTS (hedge sleeve — diversification + bubble hedge) ─
    "US.XLK": {
        "side": "SHORT",
        "thesis_summary": "Concentrated tech ETF hedge. Mag7 weight >50% of XLK creates concentration risk. Hedge against tech-led drawdown.",
        "moat": ["n/a — index short"],
        "tailwinds_narrative": "Bubble valuation in mega-cap tech; rotation risk to other sectors.",
        "kpis_to_watch": ["Mag7 forward P/E vs SPX","XLK/SPY ratio"],
        "thesis_break_conditions": ["earnings accelerate","valuation contracts"],
        "target_upside_pct": 8,
    },
    "US.ORCL": {
        "side": "SHORT",
        "thesis_summary": "Hedge: Oracle parabolic move on AI-cloud narrative. RPO bookings vs realised revenue gap. Valuation stretched to 25x fwd.",
        "moat": ["n/a — short trade"],
        "tailwinds_narrative": "Reversion candidate if AI capex cycle pauses or RPO conversion disappoints.",
        "kpis_to_watch": ["RPO revenue conversion ratio","OCI growth rate","gross margin pressure"],
        "thesis_break_conditions": ["RPO converts at premium margins","NVDA partnership accelerates"],
        "target_upside_pct": 15,
    },
    "US.MSFT": {
        "side": "SHORT",
        "thesis_summary": "Tactical hedge: AI capex spike compressing FCF margins. Copilot monetisation gap vs expectations.",
        "moat": ["n/a — short trade"],
        "tailwinds_narrative": "Margin compression from data-centre capex.",
        "kpis_to_watch": ["Azure AI revenue mix","capex/revenue ratio","Copilot ARR"],
        "thesis_break_conditions": ["Azure reaccelerates >30%","Copilot ARR > $5B run-rate"],
        "target_upside_pct": 10,
    },
    "US.AMZN": {
        "side": "SHORT",
        "thesis_summary": "Hedge: AWS deceleration risk vs. valuation. Retail margin pressure from logistics buildout.",
        "moat": ["n/a — short trade"],
        "tailwinds_narrative": "Reversion if AWS slows or retail margins disappoint.",
        "kpis_to_watch": ["AWS growth rate","retail op margin","capex trajectory"],
        "thesis_break_conditions": ["AWS reaccelerates","retail margin breakout"],
        "target_upside_pct": 10,
    },
    "US.MSTR": {
        "side": "SHORT",
        "thesis_summary": "Hedge: MicroStrategy is a leveraged BTC proxy at premium to NAV. Premium compresses in BTC drawdown.",
        "moat": ["n/a — short trade"],
        "tailwinds_narrative": "Premium-to-NAV decay + BTC reversion.",
        "kpis_to_watch": ["BTC price","premium-to-NAV","convertible debt rollover risk"],
        "thesis_break_conditions": ["BTC sustained above $200K","convertible structure change"],
        "target_upside_pct": 25,
    },
    "US.WMT": {
        "side": "SHORT",
        "thesis_summary": "Hedge: Walmart at all-time-high valuation for low-growth retailer. Mean-reversion candidate.",
        "moat": ["n/a — short trade"],
        "tailwinds_narrative": "Valuation overshoot, e-commerce margin pressure.",
        "kpis_to_watch": ["Comp sales growth","grocery margin","Walmart+ subs"],
        "thesis_break_conditions": ["AI personalisation drives margin lift","comp >5%"],
        "target_upside_pct": 8,
    },
    "US.UNH": {
        "side": "SHORT",
        "thesis_summary": "Hedge: healthcare sector underperformer. MLR pressure + regulatory overhang. Recovery uncertain.",
        "moat": ["n/a — short trade"],
        "tailwinds_narrative": "MLR stays elevated; political risk premium.",
        "kpis_to_watch": ["Medical loss ratio","Optum growth","CMS rate decisions"],
        "thesis_break_conditions": ["MLR drops <85%","Optum reaccelerates"],
        "target_upside_pct": 12,
    },
    "US.MS": {
        "side": "SHORT",
        "thesis_summary": "Pair-trade hedge: short MS vs long JPM/GS. MS wealth-mgmt growth slowdown + IB lag.",
        "moat": ["n/a — short trade"],
        "tailwinds_narrative": "Relative value vs other financials.",
        "kpis_to_watch": ["Wealth flows","IB market share","trading revenue"],
        "thesis_break_conditions": ["Wealth flows reaccelerate","IB share gains"],
        "target_upside_pct": 8,
    },
    "US.BAC": {
        "side": "SHORT",
        "thesis_summary": "Hedge: deposit beta squeeze. NIM less attractive than peers. Asset sensitivity headwind if Fed cuts.",
        "moat": ["n/a — short trade"],
        "tailwinds_narrative": "NIM pressure relative to JPM/GS.",
        "kpis_to_watch": ["NII trajectory","deposit costs","credit losses"],
        "thesis_break_conditions": ["NIM expands","Fed pauses cuts"],
        "target_upside_pct": 6,
    },
    "US.IWM": {
        "side": "SHORT",
        "thesis_summary": "Hedge: small-cap fragility. IWM constituents 40% unprofitable. Beta-1.3 hedge for tech longs.",
        "moat": ["n/a — index short"],
        "tailwinds_narrative": "Credit conditions tightening = small-cap pressure.",
        "kpis_to_watch": ["Russell 2000 EPS growth","HY credit spreads"],
        "thesis_break_conditions": ["Rate cuts trigger small-cap rally"],
        "target_upside_pct": 8,
    },
}


def get_existing_codes_in_csv() -> set:
    if not TRADES_CSV.exists():
        return set()
    with TRADES_CSV.open() as f:
        return {r["code"] for r in csv.DictReader(f)
                if r.get("status") == "OPEN"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write — just print the plan.")
    args = ap.parse_args()

    pwd = os.environ.get("MOOMOO_TRADE_PASSWORD", "")
    us = OpenUSTradeContext(host="127.0.0.1", port=11111)
    us.unlock_trade(pwd)
    ret, pos = us.position_list_query(trd_env=TrdEnv.SIMULATE)
    us.close()

    existing = get_existing_codes_in_csv()
    print(f"CSV already tracks {len(existing)} positions: {sorted(existing)}\n")

    plan = []
    for _, r in pos.iterrows():
        code = r["code"]
        qty = float(r["qty"])
        if qty == 0:
            continue
        # Filter out option codes (long strings with C/P + strike)
        sym = code.split(".", 1)[1] if "." in code else code
        if len(sym) > 10 or any(c.isdigit() for c in sym):
            print(f"  SKIP option: {code}")
            continue
        if code in existing:
            print(f"  SKIP already-tracked: {code}")
            continue
        side = "SHORT" if qty < 0 else "LONG"
        entry = float(r["cost_price"]) if "cost_price" in r else float(r["nominal_price"])
        if code not in THESIS_LIB:
            print(f"  SKIP no thesis defined: {code}")
            continue
        plan.append((code, side, abs(qty), entry))

    if not plan:
        print("Nothing to adopt.")
        return

    print(f"\n{'='*72}\nADOPTION PLAN — {len(plan)} positions to bring under bot management:\n{'='*72}")
    for code, side, qty, entry in plan:
        lib = THESIS_LIB.get(code, {})
        print(f"  {code:10} {side:5} qty={qty:>6}  entry=${entry:>7.2f}")
        print(f"             thesis: {lib.get('thesis_summary','(no thesis)')[:100]}")

    if args.dry_run:
        print("\n[DRY-RUN] No changes made.")
        return

    print("\nProceeding to write CSV + theses...")
    # Append to auto_trades.csv with status=OPEN
    fields = ["timestamp","code","side","qty","entry_price",
              "stop_loss","take_profit","strategy","status","order_id",
              "thesis","score","current_price"]
    write_header = not TRADES_CSV.exists() or TRADES_CSV.stat().st_size == 0
    if not write_header:
        with TRADES_CSV.open() as f:
            first = f.readline().strip()
            if not first:
                write_header = True
            else:
                fields = first.split(",")  # preserve existing schema

    with TRADES_CSV.open("a") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if write_header:
            w.writeheader()
        for code, side, qty, entry in plan:
            lib = THESIS_LIB.get(code, {})
            row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "code": code, "side": side, "qty": int(qty),
                "entry_price": round(entry, 2),
                "stop_loss": round(entry * (1.08 if side == "SHORT" else 0.92), 2),
                "take_profit": round(entry * (0.92 if side == "SHORT" else 1.08), 2),
                "strategy": "adopted",
                "status": "OPEN",
                "order_id": f"adopted-{int(datetime.now().timestamp())}",
                "thesis": lib.get("thesis_summary", "")[:160],
                "score": 0.6,
                "current_price": round(entry, 2),
            }
            w.writerow(row)

    # Write theses
    for code, side, qty, entry in plan:
        lib = THESIS_LIB.get(code, {})
        target_upside = lib.get("target_upside_pct", 10)
        target_price = entry * (1 + target_upside/100) if side == "LONG" else entry * (1 - target_upside/100)
        stop_price = entry * (0.92 if side == "LONG" else 1.08)

        thesis = build_thesis(
            code=code, side=side, qty=qty, entry_price=entry,
            signals=[{"name": "adopted", "detail": "Existing broker position adopted into bot"}],
            qualitative={
                "thesis_summary": lib.get("thesis_summary", ""),
                "moat": lib.get("moat", []),
                "tailwinds_narrative": lib.get("tailwinds_narrative", ""),
                "kpis_to_watch": lib.get("kpis_to_watch", []),
                "thesis_break_conditions": lib.get("thesis_break_conditions", []),
            },
            target_price=round(target_price, 2),
            stop_price=round(stop_price, 2),
            time_horizon_days=90,
        )
        log_thesis(thesis)

    print(f"\n✅ Adopted {len(plan)} positions into bot CSV + theses log.")


if __name__ == "__main__":
    main()
