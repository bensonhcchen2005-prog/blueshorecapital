"""
Options Strategy Module — Generates option trade ideas alongside equity signals.

Strategies:
1. Covered Call: Own stock, sell OTM call for income
2. Protective Put: Buy put as hedge on existing long positions
3. Bull Call Spread: Defined-risk directional bet on upside
4. Bear Put Spread: Defined-risk directional bet on downside
5. Cash-Secured Put: Sell put to enter stock at a discount

Integration: Called from auto_trader.py each cycle. Returns option signal
dicts that the main loop can log and optionally execute.

Requires: moomoo OPRA L1 option data (us_option_qot_right: LV1)
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
from moomoo import OpenQuoteContext, RET_OK

logger = logging.getLogger("auto_trader.options")


# ── Option Chain Helpers ────────────────────────────────────────

def get_option_chain_for_stock(
    ctx: OpenQuoteContext, stock_code: str, min_dte: int = 7, max_dte: int = 35
) -> Optional[pd.DataFrame]:
    """Fetch option chain for a stock within a DTE window.

    NOTE: moomoo API requires the date span to be <= 30 days.
    """
    today = datetime.now()
    start = (today + timedelta(days=min_dte)).strftime("%Y-%m-%d")
    # Cap span at 28 days to stay safely under API limit
    actual_max = min(max_dte, min_dte + 28)
    end = (today + timedelta(days=actual_max)).strftime("%Y-%m-%d")

    try:
        ret, chain = ctx.get_option_chain(stock_code, start=start, end=end)
        if ret != RET_OK or chain is None or len(chain) == 0:
            return None
        return chain
    except Exception as e:
        logger.debug(f"Option chain error for {stock_code}: {e}")
        return None


def get_option_quotes(ctx: OpenQuoteContext, option_codes: List[str]) -> Dict:
    """Get live quotes for option contracts."""
    results = {}
    for i in range(0, len(option_codes), 20):
        batch = option_codes[i:i+20]
        time.sleep(0.5)
        try:
            ret, snap = ctx.get_market_snapshot(batch)
            if ret == RET_OK:
                for _, r in snap.iterrows():
                    code = str(r["code"])
                    results[code] = {
                        "price": float(r.get("last_price", 0)),
                        "volume": int(r.get("volume", 0)),
                        "open_interest": int(r.get("open_interest", 0)),
                    }
        except Exception:
            pass
    return results


def find_strikes(
    chain: pd.DataFrame, stock_price: float, option_type: str, target_delta_approx: str
) -> Optional[pd.Series]:
    """Find the best strike for a given intent.

    target_delta_approx:
        'ATM'  — closest to stock price
        'OTM5' — ~5% out of the money
        'OTM10' — ~10% out of the money
        'ITM5' — ~5% in the money
    """
    filtered = chain[chain["option_type"] == option_type].copy()
    if len(filtered) == 0:
        return None

    if target_delta_approx == "ATM":
        target = stock_price
    elif target_delta_approx == "OTM5":
        target = stock_price * (1.05 if option_type == "CALL" else 0.95)
    elif target_delta_approx == "OTM10":
        target = stock_price * (1.10 if option_type == "CALL" else 0.90)
    elif target_delta_approx == "ITM5":
        target = stock_price * (0.95 if option_type == "CALL" else 1.05)
    else:
        target = stock_price

    filtered["dist"] = (filtered["strike_price"] - target).abs()
    best = filtered.sort_values("dist").iloc[0]
    return best


def calc_dte(expiry_str: str) -> int:
    """Calculate days to expiration from strike_time string."""
    try:
        exp = datetime.strptime(str(expiry_str)[:10], "%Y-%m-%d")
        return (exp - datetime.now()).days
    except Exception:
        return 30


# ── Option Strategies ───────────────────────────────────────────

def evaluate_covered_call(
    stock_code: str, stock_price: float, rsi: float, atr: float,
    chain: pd.DataFrame, ctx: OpenQuoteContext
) -> Optional[Dict]:
    """Sell OTM call against a stock we own or would own.

    When: RSI > 55 (moderately bullish, not expecting huge move),
    stock is in a range or slightly trending. Captures premium.
    """
    if rsi < 55 or rsi > 80:
        return None

    call = find_strikes(chain, stock_price, "CALL", "OTM5")
    if call is None:
        return None

    dte = calc_dte(call["strike_time"])
    if dte < 7 or dte > 45:
        return None

    # Get quote for this option
    quotes = get_option_quotes(ctx, [call["code"]])
    quote = quotes.get(call["code"])
    if not quote or quote["price"] <= 0:
        return None

    premium = quote["price"]
    strike = call["strike_price"]
    upside_cap_pct = (strike - stock_price) / stock_price * 100
    yield_pct = premium / stock_price * 100

    # Skip if premium is too thin (<0.5% of stock price)
    if yield_pct < 0.5:
        return None

    score = min(0.3 + yield_pct / 5 + (rsi - 55) / 100, 0.8)

    return {
        "strategy": "covered_call",
        "type": "option",
        "direction": "SELL_CALL",
        "stock_code": stock_code,
        "option_code": call["code"],
        "strike": strike,
        "expiry": str(call["strike_time"])[:10],
        "dte": dte,
        "premium": premium,
        "score": round(score, 3),
        "thesis": (f"Covered call: sell {strike:.0f}C ({dte}d) for ${premium:.2f} "
                   f"({yield_pct:.1f}% yield, {upside_cap_pct:.1f}% upside cap)"),
    }


def evaluate_protective_put(
    stock_code: str, stock_price: float, rsi: float, atr: float,
    chain: pd.DataFrame, ctx: OpenQuoteContext
) -> Optional[Dict]:
    """Buy OTM put as portfolio protection.

    When: RSI > 70 (overbought, expecting pullback risk) or
    after big move up (protect gains).
    """
    if rsi < 65:
        return None

    put = find_strikes(chain, stock_price, "PUT", "OTM5")
    if put is None:
        return None

    dte = calc_dte(put["strike_time"])
    if dte < 14 or dte > 45:
        return None

    quotes = get_option_quotes(ctx, [put["code"]])
    quote = quotes.get(put["code"])
    if not quote or quote["price"] <= 0:
        return None

    premium = quote["price"]
    strike = put["strike_price"]
    cost_pct = premium / stock_price * 100
    protection_from = (stock_price - strike) / stock_price * 100

    # Only if cost is reasonable (<3% of stock price)
    if cost_pct > 3.0:
        return None

    score = min(0.3 + (rsi - 65) / 50 + (3 - cost_pct) / 5, 0.75)

    return {
        "strategy": "protective_put",
        "type": "option",
        "direction": "BUY_PUT",
        "stock_code": stock_code,
        "option_code": put["code"],
        "strike": strike,
        "expiry": str(put["strike_time"])[:10],
        "dte": dte,
        "premium": premium,
        "score": round(score, 3),
        "thesis": (f"Protective put: buy {strike:.0f}P ({dte}d) for ${premium:.2f} "
                   f"({cost_pct:.1f}% cost, protects below ${strike:.0f})"),
    }


def evaluate_bull_call_spread(
    stock_code: str, stock_price: float, rsi: float, atr: float,
    chain: pd.DataFrame, ctx: OpenQuoteContext
) -> Optional[Dict]:
    """Buy ATM call + sell OTM call for defined-risk bullish bet.

    When: RSI 45-65 (early uptrend), positive momentum. Limits cost
    vs naked call while capping max loss.
    """
    if rsi < 40 or rsi > 70:
        return None

    # Buy ATM, sell OTM5
    long_call = find_strikes(chain, stock_price, "CALL", "ATM")
    short_call = find_strikes(chain, stock_price, "CALL", "OTM5")
    if long_call is None or short_call is None:
        return None
    if long_call["strike_price"] >= short_call["strike_price"]:
        return None

    dte = calc_dte(long_call["strike_time"])
    if dte < 14 or dte > 45:
        return None

    codes = [long_call["code"], short_call["code"]]
    quotes = get_option_quotes(ctx, codes)
    q_long = quotes.get(long_call["code"])
    q_short = quotes.get(short_call["code"])

    if not q_long or not q_short or q_long["price"] <= 0:
        return None

    net_debit = q_long["price"] - q_short["price"]
    if net_debit <= 0:
        return None

    max_profit = short_call["strike_price"] - long_call["strike_price"] - net_debit
    if max_profit <= 0:
        return None

    risk_reward = max_profit / net_debit
    cost_pct = net_debit / stock_price * 100

    # Skip if R:R is poor
    if risk_reward < 1.0:
        return None

    score = min(0.3 + risk_reward / 10 + (rsi - 40) / 100, 0.8)

    return {
        "strategy": "bull_call_spread",
        "type": "option",
        "direction": "SPREAD",
        "stock_code": stock_code,
        "long_option": long_call["code"],
        "short_option": short_call["code"],
        "long_strike": long_call["strike_price"],
        "short_strike": short_call["strike_price"],
        "expiry": str(long_call["strike_time"])[:10],
        "dte": dte,
        "net_debit": round(net_debit, 2),
        "max_profit": round(max_profit, 2),
        "risk_reward": round(risk_reward, 2),
        "score": round(score, 3),
        "thesis": (f"Bull call spread {long_call['strike_price']:.0f}/{short_call['strike_price']:.0f} "
                   f"({dte}d) debit=${net_debit:.2f} max_profit=${max_profit:.2f} R:R={risk_reward:.1f}"),
    }


def evaluate_cash_secured_put(
    stock_code: str, stock_price: float, rsi: float, atr: float,
    chain: pd.DataFrame, ctx: OpenQuoteContext
) -> Optional[Dict]:
    """Sell OTM put to enter stock at discount + collect premium.

    When: RSI 35-50 (oversold but stabilizing). Want to own the stock
    at a lower price. Worst case: assigned at strike minus premium.
    """
    if rsi < 30 or rsi > 55:
        return None

    put = find_strikes(chain, stock_price, "PUT", "OTM5")
    if put is None:
        return None

    dte = calc_dte(put["strike_time"])
    if dte < 14 or dte > 45:
        return None

    quotes = get_option_quotes(ctx, [put["code"]])
    quote = quotes.get(put["code"])
    if not quote or quote["price"] <= 0:
        return None

    premium = quote["price"]
    strike = put["strike_price"]
    effective_buy = strike - premium
    discount_pct = (stock_price - effective_buy) / stock_price * 100
    yield_pct = premium / strike * 100

    # Require at least 1% yield and 3% effective discount
    if yield_pct < 1.0 or discount_pct < 3.0:
        return None

    score = min(0.3 + yield_pct / 5 + discount_pct / 20, 0.8)

    return {
        "strategy": "cash_secured_put",
        "type": "option",
        "direction": "SELL_PUT",
        "stock_code": stock_code,
        "option_code": put["code"],
        "strike": strike,
        "expiry": str(put["strike_time"])[:10],
        "dte": dte,
        "premium": premium,
        "effective_buy": round(effective_buy, 2),
        "score": round(score, 3),
        "thesis": (f"Cash-secured put: sell {strike:.0f}P ({dte}d) for ${premium:.2f} "
                   f"({yield_pct:.1f}% yield, effective buy ${effective_buy:.2f} = {discount_pct:.1f}% discount)"),
    }


# ── Orchestrator ────────────────────────────────────────────────

OPTION_ELIGIBLE = [
    "US.AAPL", "US.MSFT", "US.GOOGL", "US.AMZN", "US.NVDA", "US.META", "US.TSLA",
    "US.AMD", "US.AVGO", "US.CRM", "US.JPM", "US.GS", "US.UNH", "US.XOM",
    "US.BA", "US.PLTR", "US.COIN", "US.ARM", "US.SMCI", "US.MSTR",
    "US.SPY", "US.QQQ", "US.IWM",
]

OPTION_STRATEGIES = [
    evaluate_covered_call,
    evaluate_protective_put,
    evaluate_bull_call_spread,
    evaluate_cash_secured_put,
]


def scan_option_ideas(
    ctx: OpenQuoteContext,
    stock_data: Dict[str, Dict],
    max_ideas: int = 5,
) -> List[Dict]:
    """Scan option-eligible stocks and return top option trade ideas.

    Args:
        ctx: Quote context
        stock_data: Dict of {code: {price, rsi, atr, ...}} from equity scan
        max_ideas: Max ideas to return

    Returns:
        List of option signal dicts (logged to journal, not auto-executed)
    """
    ideas = []

    for code in OPTION_ELIGIBLE:
        data = stock_data.get(code)
        if not data:
            continue

        stock_price = data.get("price", 0)
        rsi = data.get("rsi", 50)
        atr = data.get("atr", stock_price * 0.02)

        if stock_price <= 0:
            continue

        # Get option chain (rate limited)
        time.sleep(0.5)
        chain = get_option_chain_for_stock(ctx, code)
        if chain is None or len(chain) == 0:
            continue

        # Evaluate each option strategy
        for strat_fn in OPTION_STRATEGIES:
            try:
                idea = strat_fn(code, stock_price, rsi, atr, chain, ctx)
                if idea and idea["score"] >= 0.35:
                    # Record the underlying price at scan time so MTM can be
                    # computed later when the underlying moves.
                    idea["entry_underlying"] = round(stock_price, 4)
                    ideas.append(idea)
            except Exception as e:
                logger.debug(f"Option strategy error on {code}: {e}")

    # Sort by score, return top ideas
    ideas.sort(key=lambda x: x["score"], reverse=True)
    return ideas[:max_ideas]
