"""
Universe audit — evaluate whether the active US_UNIVERSE is too narrow.

Runs the current strategy suite against US_TOP100_TEST (a superset of
US_UNIVERSE), captures which names would have produced signals the active
list misses, and reports a gap analysis.

Writes:  logs/universe_audit.json   — findings consumed by the dashboard
Usage:   ./venv/bin/python -m scripts.eval_universe [--lookback 30]
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from moomoo import RET_OK, KLType, OpenQuoteContext

# Import the live modules so we use the exact same strategy code
import auto_trader as at
from data.signal_log import log_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eval_universe")

PROJECT_ROOT = Path(__file__).parent.parent
OUT = PROJECT_ROOT / "logs" / "universe_audit.json"


def main(lookback_days: int = 30, max_names: int = 100) -> None:
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

    # Candidate list: the expanded top-100 minus the active universe
    active = set(at.US_UNIVERSE + at.ETF_UNIVERSE)
    candidates = [s for s in at.US_TOP100_TEST if s not in active][:max_names]
    log.info(f"Active universe: {len(active)} names")
    log.info(f"Candidates to evaluate (outside active): {len(candidates)}")

    start = (datetime.now() - timedelta(days=lookback_days + 30)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")

    findings = []
    signal_count_by_strategy: dict = {}
    for sym in candidates:
        time.sleep(0.35)  # rate limit
        try:
            result = ctx.request_history_kline(
                sym, start=start, end=end, ktype=KLType.K_DAY, max_count=90,
            )
            ret = result[0] if len(result) else None
            df = result[1] if len(result) >= 2 else None
            if ret != RET_OK or df is None or df.empty:
                continue
            # Enrich like the live scanner does
            df_e = at.get_enriched_data(ctx, sym)
            if df_e is None:
                continue
        except Exception as e:
            log.debug(f"{sym}: fetch failed: {e}")
            continue

        # Run every strategy the scanner runs
        for strat_fn in at.ALL_STRATEGIES:
            try:
                sig = strat_fn(sym, df_e)
                if sig is None:
                    continue
                if sig.get("score", 0) < at.MIN_SIGNAL_SCORE:
                    continue
                # Evaluate the forward return: price today vs. close 3 bars ago
                try:
                    closes = df["close"].tolist()
                    if len(closes) >= 4:
                        fwd3 = (closes[-1] - closes[-4]) / closes[-4] * 100
                    else:
                        fwd3 = None
                except Exception:
                    fwd3 = None
                finding = {
                    "symbol": sym,
                    "strategy": sig["strategy"],
                    "direction": sig["direction"],
                    "score": sig["score"],
                    "fwd_return_3d_pct": round(fwd3, 2) if fwd3 is not None else None,
                    "thesis": sig.get("thesis", ""),
                }
                findings.append(finding)
                signal_count_by_strategy[sig["strategy"]] = \
                    signal_count_by_strategy.get(sig["strategy"], 0) + 1
                # Log as a "missed_candidate" event in the unified log
                log_event(
                    symbol=sym, strategy=sig["strategy"],
                    direction=sig["direction"],
                    stage="missed_candidate", verdict="OUTSIDE_UNIVERSE",
                    detail=f"outside active universe — score {sig['score']:.2f}",
                    score=float(sig["score"]),
                    extra={"fwd_return_3d_pct": finding["fwd_return_3d_pct"]},
                )
            except Exception as e:
                log.debug(f"{sym} {strat_fn.__name__}: {e}")

    # Summary
    with_fwd = [f for f in findings if f["fwd_return_3d_pct"] is not None]
    longs = [f for f in with_fwd if f["direction"] == "LONG"]
    wins = [f for f in longs if f["fwd_return_3d_pct"] > 0]

    summary = {
        "generated_at": datetime.now().isoformat(),
        "lookback_days": lookback_days,
        "active_universe_size": len(active),
        "candidates_evaluated": len(candidates),
        "signals_found": len(findings),
        "signals_by_strategy": signal_count_by_strategy,
        "signals_with_forward_data": len(with_fwd),
        "long_signals": len(longs),
        "long_would_have_profited": len(wins),
        "hit_rate": round(len(wins) / len(longs), 4) if longs else None,
        "avg_fwd_return_pct": round(
            sum(f["fwd_return_3d_pct"] for f in with_fwd) / len(with_fwd), 2
        ) if with_fwd else None,
        "top_10_missed": sorted(
            with_fwd, key=lambda f: -(f["fwd_return_3d_pct"] or 0)
        )[:10],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "summary": summary,
        "findings": findings,
    }, indent=2, default=str))
    log.info(f"Wrote {OUT}")
    log.info(f"Summary: {json.dumps(summary, indent=2, default=str)}")
    try:
        ctx.close()
    except Exception:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=30,
                    help="days of history to use for forward-return calc")
    ap.add_argument("--max-names", type=int, default=100)
    args = ap.parse_args()
    main(lookback_days=args.lookback, max_names=args.max_names)
