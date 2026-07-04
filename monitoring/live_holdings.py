"""
Live Holdings enrichment — read user's actual moomoo positions from
logs/live_holdings_input.json, enrich every line with:
  - current price
  - market value, P&L
  - portfolio weight
  - latest news headline + date
  - last earnings + summary
  - per-position recommendation (ADD / TRIM / HOLD / CLOSE) + reasoning

Output: logs/live_holdings_enriched.json (dashboard reads this)

Sources: yfinance (free, no key) for prices + news + earnings calendar.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import yfinance as yf

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
INPUT_FILE  = LOG_DIR / "live_holdings_input.json"
OUTPUT_FILE = LOG_DIR / "live_holdings_enriched.json"


# ── Recommendation engine ─────────────────────────────────────────

REC_RULES = {
    # ticker → manual override + reasoning (gates the auto-rec)
    "GLD":   {"rec": "CLOSE",  "reason": "Hedge thesis broken — gold has tanked (-17% on the position). Better hedges available."},
    "NOK":   {"rec": "CLOSE",  "reason": "Value trap. -7% unrealized. Telecom dividend story has been broken for years."},
    "GLW":   {"rec": "TRIM",   "reason": "Up 110% YTD, late-stage momentum. Q=40 fails quality bar. Take some profits."},
    "AB":    {"rec": "HOLD",   "reason": "-9.8% but 6%+ dividend yield supports. Asset mgmt benefits from rates."},
    "BE":    {"rec": "TRIM",   "reason": "Fuel cell speculation +3% today. Highly speculative — size it small."},
    "CRCL":  {"rec": "HOLD",   "reason": "Stablecoin thesis intact, but volatile. Size carefully, avoid adding here."},
    "EWY":   {"rec": "HOLD",   "reason": "Korea momentum strong (+4.5% gain). Geographic diversifier."},
    "DRAM":  {"rec": "HOLD",   "reason": "Memory cycle ETF — flat. Cycle-dependent, no near catalyst."},
    "AMZN":  {"rec": "HOLD",   "reason": "Mega-cap quality. AWS deceleration risk, but framework respects the moat."},
    "AVGO":  {"rec": "HOLD",   "reason": "COMPOUNDER. Q=89, AI silicon king. Don't sell on noise — let it compound."},
    "GOOG":  {"rec": "ADD",    "reason": "TPU narrative + Search + Cloud + Waymo. -3% gain/loss is acceptable cost basis."},
    "GOOGM": {"rec": "HOLD",   "reason": "Preferred — fixed-income proxy. Hold for yield, not growth."},
    "MRVL":  {"rec": "HOLD",   "reason": "AI silicon — solid Q, decent fundamentals. Smaller bet than AVGO."},
    "ANET":  {"rec": "HOLD",   "reason": "COMPOUNDER. Q=87, AI networking pure-play. Just entered, give it time."},
    "UVXY":  {"rec": "TRIM",   "reason": "Volatility hedge decays. Hold only as event hedge, trim when no catalyst."},
}


def _classify_auto(pnl_pct: float, ticker: str, market_cap: Optional[float],
                   quality_score: Optional[float]) -> Dict:
    """Fallback recommendation when no manual rule exists."""
    if ticker in REC_RULES:
        return REC_RULES[ticker]
    # Auto-logic:
    if pnl_pct is None:
        return {"rec": "HOLD", "reason": "Insufficient data for recommendation."}
    if pnl_pct < -15:
        return {"rec": "REVIEW", "reason": f"Down {pnl_pct:.1f}% — review thesis vs. stop loss."}
    if pnl_pct > 50:
        return {"rec": "TRIM",   "reason": f"Up {pnl_pct:.1f}% — consider taking some profits."}
    if quality_score is not None and quality_score >= 0.70:
        return {"rec": "HOLD",   "reason": "Quality ≥70 — compounder hold discipline."}
    return {"rec": "HOLD", "reason": "Default — no specific signal."}


# ── yfinance enrichment ───────────────────────────────────────────

def _price_block(ticker: str) -> Dict:
    """Get current price + recent moves."""
    try:
        t = yf.Ticker(ticker)
        h = t.history(period="30d")
        if h is None or len(h) < 2:
            return {"price": None}
        last_px = float(h.iloc[-1]["Close"])
        prev = float(h.iloc[-2]["Close"]) if len(h) >= 2 else last_px
        day_chg = (last_px / prev - 1) * 100
        ytd = None
        try:
            ytd_h = t.history(start="2026-01-02", end="2026-12-31")
            if ytd_h is not None and len(ytd_h) >= 2:
                ytd = (last_px / float(ytd_h.iloc[0]["Close"]) - 1) * 100
        except Exception:
            pass
        return {
            "price": round(last_px, 2),
            "day_chg_pct": round(day_chg, 2),
            "ytd_pct": round(ytd, 1) if ytd is not None else None,
        }
    except Exception as e:
        logger.debug(f"price block failed for {ticker}: {e}")
        return {"price": None}


def _news_block(ticker: str) -> Dict:
    """Latest 3 headlines."""
    try:
        t = yf.Ticker(ticker)
        news = (t.news or [])[:5]
        out = []
        for item in news:
            c = item.get("content", item)
            title = c.get("title", "")
            provider = (c.get("provider") or {}).get("displayName", "")
            pub_ts = item.get("providerPublishTime", 0)
            try:
                if not pub_ts:
                    pub_ts = datetime.fromisoformat(c.get("pubDate","").replace("Z","+00:00")).timestamp()
            except Exception:
                pub_ts = 0
            pub_iso = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()[:16] if pub_ts else "—"
            out.append({"title": title[:140], "provider": provider, "published": pub_iso})
        return {"headlines": out[:3]}
    except Exception as e:
        return {"headlines": []}


def _earnings_block(ticker: str) -> Dict:
    """Last and next earnings."""
    try:
        t = yf.Ticker(ticker)
        cal = getattr(t, "calendar", None)
        next_earn = None
        if cal is not None:
            try:
                if hasattr(cal, "get"):
                    ev = cal.get("Earnings Date")
                    if isinstance(ev, list) and ev:
                        next_earn = str(ev[0])[:10]
                    elif ev:
                        next_earn = str(ev)[:10]
            except Exception:
                pass

        # Last earnings (from quarterly_earnings)
        last_earn = None
        try:
            qe = t.quarterly_earnings if hasattr(t, "quarterly_earnings") else None
            if qe is not None and len(qe) > 0:
                last_earn = {
                    "date": str(qe.index[0])[:10],
                    "eps": float(qe.iloc[0].get("Earnings", 0)),
                    "revenue": float(qe.iloc[0].get("Revenue", 0)) if "Revenue" in qe.columns else None,
                }
        except Exception:
            pass

        return {
            "next_earnings_date": next_earn,
            "last_earnings": last_earn,
        }
    except Exception as e:
        return {"next_earnings_date": None, "last_earnings": None}


def _fundamentals_block(ticker: str) -> Dict:
    """Quick fundamental snapshot."""
    try:
        from data.fundamentals import get_fundamentals, compute_quality_score
        f = get_fundamentals(ticker)
        if not f:
            return {"market_cap": None, "quality_score": None}
        q = compute_quality_score(f)
        return {
            "market_cap":      f.get("market_cap"),
            "forward_pe":      f.get("forward_pe"),
            "rev_growth":      f.get("rev_growth"),
            "op_margin":       f.get("op_margin"),
            "beta":            f.get("beta"),
            "quality_score":   q,
            "analyst_target":  f.get("analyst_price_target"),
        }
    except Exception as e:
        return {"market_cap": None, "quality_score": None}


# ── Build enriched holdings ───────────────────────────────────────

def enrich_position(p: Dict, bucket: str) -> Dict:
    """Enrich one equity/ETF position."""
    ticker = p["ticker"]
    shares = float(p.get("shares") or 0)
    cost   = float(p.get("cost_basis") or 0)

    price_b = _price_block(ticker)
    fund_b = _fundamentals_block(ticker)
    news_b = _news_block(ticker)
    earn_b = _earnings_block(ticker)

    px = price_b.get("price")
    mv = px * shares if px else None
    cost_total = cost * shares
    pnl = (mv - cost_total) if mv is not None else None
    pnl_pct = (pnl / cost_total * 100) if (pnl is not None and cost_total) else None

    rec = _classify_auto(pnl_pct, ticker,
                        fund_b.get("market_cap"),
                        fund_b.get("quality_score"))

    return {
        "ticker":            ticker,
        "company":           p.get("company"),
        "category":          p.get("category"),
        "bucket":            bucket,
        "shares":            shares,
        "cost_basis":        cost,
        "cost_total":        round(cost_total, 2),
        "entry_date":        p.get("entry_date"),
        "current_price":     px,
        "day_chg_pct":       price_b.get("day_chg_pct"),
        "ytd_pct":           price_b.get("ytd_pct"),
        "market_val":        round(mv, 2) if mv is not None else None,
        "unrealized_pnl":    round(pnl, 2) if pnl is not None else None,
        "unrealized_pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        "fundamentals":      fund_b,
        "latest_news":       news_b["headlines"],
        "earnings":          earn_b,
        "recommendation":    rec.get("rec"),
        "reasoning":         rec.get("reason"),
    }


def enrich_option(o: Dict) -> Dict:
    """Enrich one short option position (calculate approximate MV via OCC pricing)."""
    ticker = o["ticker"]
    strike = float(o.get("strike", 0))
    expiry = o.get("expiry")
    typ = o.get("type", "P")
    contracts = float(o.get("contracts", 0))
    premium = float(o.get("premium", 0))

    # Days to expiry
    days_to_exp = None
    if expiry:
        try:
            days_to_exp = (date.fromisoformat(expiry) - date.today()).days
        except Exception:
            pass

    # Approximate current option price (without full options chain query)
    # Use intrinsic value as floor + time value decay heuristic
    price_b = _price_block(ticker)
    underlying = price_b.get("price")
    intrinsic = 0
    if underlying is not None and strike:
        if typ == "P":
            intrinsic = max(0, strike - underlying)
        else:  # C
            intrinsic = max(0, underlying - strike)

    # Estimate current premium: intrinsic + decayed time value
    # crude: time_value = original_extrinsic * sqrt(days_to_exp / days_at_entry)
    est_premium = intrinsic + (premium * 0.5)  # rough mid-life estimate
    mv = -abs(contracts) * est_premium * 100  # short = negative MV

    # P&L: collected premium − current premium to close
    pnl = abs(contracts) * (premium - est_premium) * 100

    return {
        "ticker":        f"{ticker} {expiry} {strike:.0f}{typ}",
        "underlying":    ticker,
        "strike":        strike,
        "expiry":        expiry,
        "type":          typ,
        "contracts":     contracts,
        "premium_at_entry": premium,
        "est_current_premium": round(est_premium, 2),
        "days_to_exp":   days_to_exp,
        "underlying_px": underlying,
        "intrinsic":     round(intrinsic, 2),
        "market_val":    round(mv, 2),
        "unrealized_pnl": round(pnl, 2),
        "recommendation": ("CLOSE" if intrinsic > premium * 1.5
                          else ("HOLD" if days_to_exp and days_to_exp > 14
                                else "ROLL_OR_CLOSE")),
        "reasoning": (
            "Deep ITM — assignment risk." if intrinsic > premium * 1.5
            else f"Days to expiry: {days_to_exp}. Time decay favors holder."
                 if days_to_exp and days_to_exp > 14
            else "Near expiry — manage gamma risk."
        ),
    }


def _china_as_equity_rows() -> List[Dict]:
    """Read china_holdings_enriched.json and reshape into the same
    schema as equities/etfs so they can render in the Live Account tab
    using the existing renderHoldingRow function.

    CNY prices are converted to USD for portfolio-wide consistency
    (using fx_cny_per_usd from the china enriched file).
    """
    ch_path = LOG_DIR / "china_holdings_enriched.json"
    if not ch_path.exists():
        return []
    try:
        ch = json.loads(ch_path.read_text())
    except Exception:
        return []
    fx = ch.get("fx_cny_per_usd", 7.15) or 7.15
    out = []
    for p in ch.get("positions", []):
        px_cny = p.get("current_price_cny")
        px_usd = (px_cny / fx) if px_cny else None
        shares = p.get("shares")
        cost_cny = p.get("cost_basis_cny")
        cost_usd = (cost_cny / fx) if cost_cny else None
        mv_usd = (px_usd * shares) if (px_usd and shares) else None
        pnl_usd = ((px_usd - cost_usd) * shares) if (px_usd and cost_usd and shares) else None
        pnl_pct = p.get("unrealized_pnl_pct")
        fund = p.get("fundamentals") or {}

        # Recommendation from China alerts
        sev = p.get("overall_severity", "GREEN")
        rec = "REVIEW" if sev == "URGENT" else ("WATCH" if sev == "WATCH" else "HOLD")
        alerts_txt = "; ".join(a.get("detail", "")[:100] for a in p.get("alerts", [])[:2])
        reasoning = alerts_txt if alerts_txt else f"China A-share · {p.get('category','')} · sev {sev}"

        out.append({
            "ticker":              p.get("code"),
            "company":             p.get("company"),
            "category":            "🇨🇳 " + (p.get("category") or ""),
            "bucket":              "china_equity",
            "shares":              shares,
            "cost_basis":          cost_usd,   # USD-converted for display parity
            "cost_total":          round(cost_usd * shares, 2) if (cost_usd and shares) else 0,
            "entry_date":          None,
            "current_price":       round(px_usd, 2) if px_usd else None,
            "day_chg_pct":         p.get("day_chg_pct"),
            "ytd_pct":             p.get("ytd_pct"),
            "market_val":          round(mv_usd, 2) if mv_usd else None,
            "unrealized_pnl":      round(pnl_usd, 2) if pnl_usd else None,
            "unrealized_pnl_pct":  pnl_pct,
            "fundamentals": {
                "market_cap":     (fund.get("market_cap_cny") or 0) / fx if fund.get("market_cap_cny") else None,
                "forward_pe":     fund.get("forward_pe"),
                "rev_growth":     fund.get("rev_growth"),
                "op_margin":      fund.get("op_margin"),
                "beta":           fund.get("beta"),
                "quality_score":  None,
                "analyst_target": fund.get("analyst_target"),
            },
            "latest_news":         p.get("latest_news", [])[:3],
            "earnings":            {"next_earnings_date": None},
            "recommendation":      rec,
            "reasoning":           reasoning,
            # Retain original CNY fields for reference on hover / expand
            "price_cny":           px_cny,
            "name_zh":             p.get("name_zh"),
            "china_severity":      sev,
            "china_alerts":        p.get("alerts", []),
        })
    return out


def run() -> Dict:
    if not INPUT_FILE.exists():
        return {"error": "no input file"}
    inp = json.loads(INPUT_FILE.read_text())

    equities = [enrich_position(p, "equity") for p in inp.get("equities", [])]
    etfs     = [enrich_position(p, "etf")    for p in inp.get("etfs", [])]
    options  = [enrich_option(o)             for o in inp.get("options_short_puts", [])]
    china    = _china_as_equity_rows()

    # Hedge (UVXY): pull live and use as portfolio hedge
    hedge_data = None
    h = inp.get("hedge", {})
    if h and h.get("ticker"):
        pb = _price_block(h["ticker"])
        hedge_data = {
            "ticker":   h["ticker"],
            "company":  h.get("company"),
            "shares":   h.get("shares"),
            "current_price": pb.get("price"),
            "day_chg_pct":   pb.get("day_chg_pct"),
            "ytd_pct":       pb.get("ytd_pct"),
            "note":     h.get("note"),
        }

    # Portfolio aggregates (include China as equity)
    total_equity_mv = sum(e["market_val"] or 0 for e in equities)
    total_etf_mv    = sum(e["market_val"] or 0 for e in etfs)
    total_opt_mv    = sum(o["market_val"] for o in options)
    total_china_mv  = sum(c["market_val"] or 0 for c in china)
    total_mv        = total_equity_mv + total_etf_mv + total_opt_mv + total_china_mv

    total_cost = sum(e["cost_total"] for e in equities + etfs + china)
    total_pnl  = (total_equity_mv + total_etf_mv + total_china_mv) - total_cost + sum(o["unrealized_pnl"] for o in options)

    # Weights + day-change dollar impact + portfolio impact (include china)
    if total_mv:
        for e in equities + etfs + china:
            mv = e.get("market_val") or 0
            e["portfolio_weight_pct"] = round(mv / total_mv * 100, 2)
            day_chg_pct = e.get("day_chg_pct")
            if day_chg_pct is not None and mv:
                prev_mv = mv / (1 + day_chg_pct / 100)
                day_pnl_dollar = mv - prev_mv
                e["day_pnl_dollar"] = round(day_pnl_dollar, 2)
                e["day_pnl_portfolio_pct"] = round(day_pnl_dollar / total_mv * 100, 3)
            else:
                e["day_pnl_dollar"] = None
                e["day_pnl_portfolio_pct"] = None

    # Top winners/losers (include china)
    pos_list = [e for e in equities + etfs + china if e.get("unrealized_pnl") is not None]
    winners = sorted(pos_list, key=lambda x: -x["unrealized_pnl"])[:3]
    losers  = sorted(pos_list, key=lambda x: x["unrealized_pnl"])[:3]

    # Risk flags (include china)
    risk_flags = []
    if total_mv:
        for e in equities + etfs + china:
            w = e.get("portfolio_weight_pct", 0)
            if w > 20:
                risk_flags.append(f"⚠ {e['ticker']}: {w}% weight — concentration risk")
        if total_etf_mv / total_mv > 0.4:
            risk_flags.append(f"⚠ ETFs are {total_etf_mv/total_mv*100:.0f}% of book — limited stock-picking alpha")
        gld_pos = next((e for e in etfs if e["ticker"] == "GLD"), None)
        if gld_pos and gld_pos.get("unrealized_pnl_pct", 0) < -10:
            risk_flags.append(f"🔴 GLD: {gld_pos['unrealized_pnl_pct']}% — hedge thesis broken (recommend close)")

    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account_label": inp.get("account_label", "Individual"),
        "total_account_value": round(total_mv, 2),
        "total_cost_basis":    round(total_cost, 2),
        "total_pnl":           round(total_pnl, 2),
        "total_pnl_pct":       round(total_pnl / total_cost * 100, 2) if total_cost else None,
        "equity_mv":  round(total_equity_mv, 2),
        "etf_mv":     round(total_etf_mv, 2),
        "option_mv":  round(total_opt_mv, 2),
        "hedge":      hedge_data,
        "risk_flags": risk_flags,
        "top_winners": [{"ticker": e["ticker"], "pnl": e["unrealized_pnl"], "pnl_pct": e["unrealized_pnl_pct"]} for e in winners],
        "top_losers":  [{"ticker": e["ticker"], "pnl": e["unrealized_pnl"], "pnl_pct": e["unrealized_pnl_pct"]} for e in losers],
        "equities":   equities,
        "etfs":       etfs,
        "options":    options,
        "china":      china,
        "china_mv":   round(total_china_mv, 2),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = run()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nAccount: {out.get('account_label')}")
    print(f"Total value: ${out.get('total_account_value'):,.0f}")
    print(f"Total P&L:   ${out.get('total_pnl'):,.0f}  ({out.get('total_pnl_pct')}%)")
    print(f"Equities:    {len(out.get('equities',[]))}")
    print(f"ETFs:        {len(out.get('etfs',[]))}")
    print(f"Options:     {len(out.get('options',[]))}")
    if out.get('hedge'):
        h = out['hedge']
        print(f"Hedge:       {h['ticker']} @ ${h.get('current_price','?')}")
    if out.get('risk_flags'):
        print(f"\nRisk flags:")
        for f in out['risk_flags']:
            print(f"  {f}")
