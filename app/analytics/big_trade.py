"""Large-trade detector. Runs only for coins that have a full-mode watch.

Threshold T = K * mean USD size over the reference (most liquid) venue.
A print is compared with T before same-side prints are glued (up to 1.5 s).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.exchanges.base import NormalizedTrade
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _Pending:
    side: str
    symbol: str
    amount: float = 0.0
    cost: float = 0.0
    price_sum: float = 0.0
    count: int = 0
    last_ts: float = 0.0
    trade_ids: set[str] = field(default_factory=set)
    watch_ids: list[int] = field(default_factory=list)
    threshold: float = 0.0
    avg: float = 0.0
    mode: str = "avg"
    samples: int = 0
    k: float = 15.0
    floor: float = 1000.0


class BigTradeDetector:
    def __init__(
        self,
        config: Any,
        *,
        most_liquid_exchange: Callable[[str], Awaitable[str | None]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self._most_liquid_exchange = most_liquid_exchange
        self._clock = clock or time.time
        self._samples: dict[tuple[str, str], deque[tuple[float, float]]] = defaultdict(deque)
        self._seen: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._pending: dict[tuple[str, str, str], _Pending] = {}
        self._preferred: dict[str, tuple[float, str | None]] = {}
        self._recent: dict[int, deque[tuple[float, str, float]]] = defaultdict(deque)

    def clear_coin(self, coin: str) -> None:
        coin = coin.upper()
        self._preferred.pop(coin, None)
        for key in list(self._samples):
            if key[0] == coin:
                self._samples.pop(key, None)
                self._seen.pop(key, None)
        for key in list(self._pending):
            if key[0] == coin:
                self._pending.pop(key, None)

    def recent_summary(self, watch_id: int, minutes: float = 30.0) -> dict[str, float]:
        now = self._clock()
        cutoff = now - minutes * 60
        rows = self._recent.get(watch_id)
        if not rows:
            return {"sell_n": 0, "sell_usd": 0.0, "buy_n": 0, "buy_usd": 0.0}
        while rows and rows[0][0] < cutoff:
            rows.popleft()
        sell_n = buy_n = 0
        sell_usd = buy_usd = 0.0
        for _, side, cost in rows:
            if side == "sell":
                sell_n += 1
                sell_usd += cost
            else:
                buy_n += 1
                buy_usd += cost
        return {"sell_n": sell_n, "sell_usd": sell_usd, "buy_n": buy_n, "buy_usd": buy_usd}

    def _coin_or(self, coin: str, name: str, default: Any) -> Any:
        override = self.config.coin_get(coin, "big", name, default=None)
        if override is not None:
            return override
        return self.config.get("big", name, default=default)

    def _params(self, coin: str) -> tuple[float, float, float, int, float | None]:
        window_min = float(self._coin_or(coin, "window_minutes", 60))
        k = float(self._coin_or(coin, "k_multiplier", 15))
        floor = float(self._coin_or(coin, "floor_usd", 1000))
        min_samples = int(self._coin_or(coin, "min_samples", 10))
        absolute = self.config.coin_get(coin, "big", "usd", default=None)
        if absolute is not None:
            absolute = float(absolute)
        return window_min, k, floor, min_samples, absolute

    async def _preferred_exchange(self, coin: str) -> str | None:
        coin = coin.upper()
        ttl = float(self.config.get("big", "most_liquid_cache_sec", default=60))
        now = self._clock()
        cached = self._preferred.get(coin)
        if cached and now - cached[0] < ttl:
            return cached[1]
        preferred: str | None = None
        if self._most_liquid_exchange is not None:
            try:
                preferred = await self._most_liquid_exchange(coin)
            except Exception as exc:
                logger.warning("most-liquid lookup failed for %s: %s", coin, exc)
        self._preferred[coin] = (now, preferred)
        return preferred

    async def _threshold(
        self, coin: str, exchange_hint: str, now: float
    ) -> tuple[float, float, str, int, float, float]:
        window_min, k, floor, min_samples, absolute = self._params(coin)
        if absolute is not None:
            return absolute, 0.0, "abs", 0, k, floor
        preferred = await self._preferred_exchange(coin)
        ref = preferred or exchange_hint
        samples = self._samples[(coin, ref)]
        cutoff = now - window_min * 60
        while samples and samples[0][0] < cutoff:
            samples.popleft()
        n = len(samples)
        avg = (sum(v for _, v in samples) / n) if n else 0.0
        if n < min_samples:
            return max(avg * k, floor), avg, "floor", n, k, floor
        return avg * k, avg, "avg", n, k, floor

    async def on_trade(self, trade: NormalizedTrade, watch_ids: list[int]) -> list[dict[str, Any]]:
        """Record the print. Alerts for a finished glue-group are returned on a later flush."""
        coin = trade.symbol.split("/")[0].upper()
        key = (coin, trade.exchange)
        seen = self._seen[key]
        if trade.trade_id in seen:
            return []
        seen.add(trade.trade_id)
        if len(seen) > 5000:
            self._seen[key] = set(list(seen)[-2500:])

        now = self._clock()
        # Threshold uses the window as it was before this print, so one
        # large trade cannot lift the bar it is being measured against.
        threshold, avg, mode, n, k, floor = await self._threshold(coin, trade.exchange, now)
        window_min = float(self._coin_or(coin, "window_minutes", 60))
        samples = self._samples[key]
        samples.append((now, trade.cost))
        cutoff = now - window_min * 60
        while samples and samples[0][0] < cutoff:
            samples.popleft()

        pend_key = (coin, trade.exchange, trade.side)
        pending = self._pending.get(pend_key)
        agg_ms = float(self.config.get("big", "aggregate_ms", default=1500)) / 1000.0
        # A burst keeps the threshold that opened it. Recalculating after the
        # first print would hide the rest of the same aggressor.
        if (
            pending is not None
            and watch_ids
            and now - pending.last_ts <= agg_ms
            and trade.cost >= pending.threshold
        ):
            pending.amount += trade.amount
            pending.cost += trade.cost
            pending.price_sum += trade.price * trade.amount
            pending.count += 1
            pending.last_ts = now
            pending.trade_ids.add(trade.trade_id)
            pending.watch_ids = list(watch_ids)
            return []

        if trade.cost < threshold or not watch_ids:
            return []

        emitted: list[dict[str, Any]] = []
        if pending is not None:
            emitted.extend(self._emit(coin, trade.exchange, pending))
        self._pending[pend_key] = _Pending(
            side=trade.side,
            symbol=trade.symbol,
            amount=trade.amount,
            cost=trade.cost,
            price_sum=trade.price * trade.amount,
            count=1,
            last_ts=now,
            trade_ids={trade.trade_id},
            watch_ids=list(watch_ids),
            threshold=threshold,
            avg=avg,
            mode=mode,
            samples=n,
            k=k,
            floor=floor,
        )
        return emitted

    def flush(self) -> list[dict[str, Any]]:
        now = self._clock()
        agg_ms = float(self.config.get("big", "aggregate_ms", default=1500)) / 1000.0
        emitted: list[dict[str, Any]] = []
        for key, pending in list(self._pending.items()):
            if now - pending.last_ts <= agg_ms:
                continue
            coin, exchange, _side = key
            emitted.extend(self._emit(coin, exchange, pending))
            del self._pending[key]
        return emitted

    def _emit(self, coin: str, exchange: str, pending: _Pending) -> list[dict[str, Any]]:
        if not pending.watch_ids:
            return []
        avg_price = pending.price_sum / pending.amount if pending.amount else 0.0
        now = self._clock()
        anchor = min(pending.trade_ids) if pending.trade_ids else str(int(pending.last_ts))
        alerts: list[dict[str, Any]] = []
        for watch_id in pending.watch_ids:
            self._recent[watch_id].append((now, pending.side, pending.cost))
            alerts.append(
                {
                    "type": "big_trade",
                    "watch_id": watch_id,
                    "coin": coin,
                    "exchange": exchange,
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "amount": pending.amount,
                    "cost_usd": pending.cost,
                    "price": avg_price,
                    "trades": pending.count,
                    "threshold_usd": pending.threshold,
                    "avg_usd": pending.avg,
                    "threshold_mode": pending.mode,
                    "k": pending.k,
                    "floor_usd": pending.floor,
                    "sample_count": pending.samples,
                    "idempotency_key": f"big:{watch_id}:{pending.side}:{anchor}",
                }
            )
        logger.info(
            "BIG %s %s %s usd=%.0f x%s",
            exchange,
            pending.symbol,
            pending.side,
            pending.cost,
            pending.count,
        )
        return alerts
