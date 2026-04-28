"""
Market data ingestion from moomoo API.
Handles snapshots, historical klines, and real-time subscriptions.
"""

import logging
import time
from typing import Optional

import pandas as pd
from moomoo import (
    RET_OK,
    SubType,
    KLType,
    AuType,
)
from connection.gateway import Gateway


logger = logging.getLogger(__name__)

# Map string timeframe names to moomoo KLType
KLINE_MAP = {
    "K_1M": KLType.K_1M,
    "K_5M": KLType.K_5M,
    "K_15M": KLType.K_15M,
    "K_30M": KLType.K_30M,
    "K_60M": KLType.K_60M,
    "K_DAY": KLType.K_DAY,
    "K_WEEK": KLType.K_WEEK,
}

# Rate limiting: max 60 snapshot requests per 30 seconds
_last_snapshot_time = 0.0
_SNAPSHOT_MIN_INTERVAL = 0.5  # seconds between snapshot calls


class MarketData:
    """Fetches market data from moomoo via the OpenD gateway."""

    def __init__(self, gateway: Gateway):
        self.gw = gateway

    def get_snapshot(self, codes: "list[str]") -> Optional[pd.DataFrame]:
        """
        Get real-time snapshot for a list of tickers.
        Returns DataFrame with price, volume, bid/ask, market cap, etc.
        """
        global _last_snapshot_time
        elapsed = time.time() - _last_snapshot_time
        if elapsed < _SNAPSHOT_MIN_INTERVAL:
            time.sleep(_SNAPSHOT_MIN_INTERVAL - elapsed)

        ret, data = self.gw.quote_ctx.get_market_snapshot(codes)
        _last_snapshot_time = time.time()

        if ret != RET_OK:
            logger.error(f"Snapshot failed for {codes}: {data}")
            return None

        logger.debug(f"Snapshot fetched for {len(codes)} tickers")
        return data

    def get_historical_klines(
        self,
        code: str,
        ktype: str = "K_DAY",
        start: str = "",
        end: str = "",
        max_count: int = 200,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch historical candlestick data.

        Args:
            code: Ticker code (e.g., "US.AAPL")
            ktype: Candle timeframe key (e.g., "K_DAY", "K_5M")
            start: Start date "YYYY-MM-DD" (empty = auto)
            end: End date "YYYY-MM-DD" (empty = today)
            max_count: Max number of candles to return

        Returns:
            DataFrame with columns: code, time_key, open, close, high, low, volume, turnover
        """
        kl = KLINE_MAP.get(ktype, KLType.K_DAY)

        ret, data, page_req_key = self.gw.quote_ctx.request_history_kline(
            code,
            start=start,
            end=end,
            ktype=kl,
            autype=AuType.QFQ,
            max_count=max_count,
        )

        if ret != RET_OK:
            logger.error(f"Historical kline failed for {code}: {data}")
            return None

        # Fetch remaining pages if needed
        all_data = [data]
        while page_req_key is not None:
            time.sleep(0.5)  # respect rate limits
            ret, data, page_req_key = self.gw.quote_ctx.request_history_kline(
                code,
                start=start,
                end=end,
                ktype=kl,
                autype=AuType.QFQ,
                max_count=max_count,
                page_req_key=page_req_key,
            )
            if ret != RET_OK:
                logger.error(f"Historical kline page failed for {code}: {data}")
                break
            all_data.append(data)

        result = pd.concat(all_data, ignore_index=True) if len(all_data) > 1 else data
        logger.info(f"Fetched {len(result)} candles for {code} ({ktype})")
        return result

    def get_realtime_klines(
        self, code: str, ktype: str = "K_1M", num: int = 50
    ) -> Optional[pd.DataFrame]:
        """Get current real-time kline data (requires active subscription)."""
        kl = KLINE_MAP.get(ktype, KLType.K_1M)
        ret, data = self.gw.quote_ctx.get_cur_kline(code, num=num, ktype=kl)

        if ret != RET_OK:
            logger.error(f"Realtime kline failed for {code}: {data}")
            return None
        return data

    def subscribe(
        self,
        codes: "list[str]",
        sub_types: "Optional[list[str]]" = None,
    ) -> bool:
        """
        Subscribe to real-time data for given tickers.
        Each code x sub_type pair costs 1 subscription quota.

        Args:
            codes: List of ticker codes
            sub_types: List of subscription types. Defaults to QUOTE + K_LINE.
        """
        type_map = {
            "QUOTE": SubType.QUOTE,
            "TICKER": SubType.TICKER,
            "ORDER_BOOK": SubType.ORDER_BOOK,
            "K_LINE": SubType.K_LINE,
            "RT_DATA": SubType.RT_DATA,
        }

        if sub_types is None:
            subs = [SubType.QUOTE, SubType.K_LINE]
        else:
            subs = [type_map[s] for s in sub_types if s in type_map]

        ret, msg = self.gw.quote_ctx.subscribe(
            codes, subs, subscribe_push=True, is_first_push=True
        )

        if ret != RET_OK:
            logger.error(f"Subscribe failed: {msg}")
            return False

        quota_used = len(codes) * len(subs)
        logger.info(
            f"Subscribed to {len(codes)} tickers x {len(subs)} types "
            f"({quota_used} quota slots used)"
        )
        return True

    def unsubscribe(self, codes: "list[str]", sub_types: "Optional[list[str]]" = None) -> bool:
        """Unsubscribe to free up quota slots."""
        type_map = {
            "QUOTE": SubType.QUOTE,
            "TICKER": SubType.TICKER,
            "ORDER_BOOK": SubType.ORDER_BOOK,
            "K_LINE": SubType.K_LINE,
        }
        if sub_types is None:
            subs = [SubType.QUOTE, SubType.K_LINE]
        else:
            subs = [type_map[s] for s in sub_types if s in type_map]

        ret, msg = self.gw.quote_ctx.unsubscribe(codes, subs)
        if ret != RET_OK:
            logger.error(f"Unsubscribe failed: {msg}")
            return False
        return True
