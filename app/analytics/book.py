"""Minute aggregates. Raw trades are not stored."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

from app.analytics.flow import WindowStats


@dataclass
class Acc:
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    sum_sq: float = 0.0
    trade_count: int = 0
    sell_base: float = 0.0


class MinuteBook:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict[int, Acc]] = defaultdict(dict)
        self.dirty: set[tuple[str, str, int]] = set()
        self.last_trade: dict[tuple[str, str], float] = {}

    def add(
        self,
        exchange: str,
        symbol: str,
        ts: float,
        side: str,
        cost: float,
        amount: float,
    ) -> None:
        minute = int(ts) // 60 * 60
        key = (exchange, symbol)
        acc = self.data[key].get(minute)
        if acc is None:
            acc = Acc()
            self.data[key][minute] = acc
        if side == "sell":
            acc.sell_usd += cost
            acc.sell_base += amount
        else:
            acc.buy_usd += cost
        acc.sum_sq += cost * cost
        acc.trade_count += 1
        self.dirty.add((exchange, symbol, minute))
        if ts > self.last_trade.get(key, 0.0):
            self.last_trade[key] = ts

    def load_acc(
        self,
        exchange: str,
        symbol: str,
        minute: int,
        *,
        buy_usd: float,
        sell_usd: float,
        sum_sq: float,
        trade_count: int,
        sell_base: float,
    ) -> None:
        key = (exchange, symbol)
        self.data[key][minute] = Acc(
            buy_usd=buy_usd,
            sell_usd=sell_usd,
            sum_sq=sum_sq,
            trade_count=trade_count,
            sell_base=sell_base,
        )
        if trade_count:
            guess = float(minute + 59)
            now = time.time()
            if guess > now:
                guess = now
            if guess > self.last_trade.get(key, 0.0):
                self.last_trade[key] = guess

    def window(self, venues: list[tuple[str, str]], start: float, end: float) -> WindowStats:
        """Sum minute buckets that cover (start, end]. Resolution is one minute."""
        if end <= start:
            return WindowStats()
        buy = sell = sq = base = 0.0
        trades = 0
        for venue in venues:
            for minute, acc in self.data.get(venue, {}).items():
                # Minute M covers [M, M+60). Keep it when it overlaps (start, end].
                if minute < end and (minute + 60) > start:
                    buy += acc.buy_usd
                    sell += acc.sell_usd
                    sq += acc.sum_sq
                    base += acc.sell_base
                    trades += acc.trade_count
        return WindowStats(
            buy_usd=buy,
            sell_usd=sell,
            sum_sq=sq,
            trade_count=trades,
            sell_base=base,
        )

    def last_trade_ts(self, venues: list[tuple[str, str]]) -> float:
        ts = 0.0
        for venue in venues:
            ts = max(ts, self.last_trade.get(venue, 0.0))
        return ts

    def sum_since(
        self, venues: list[tuple[str, str]], since_ts: float
    ) -> tuple[float, float, float]:
        """Return (delta_usd, sell_base, vol_usd) from `since_ts`."""
        since_m = int(since_ts) // 60 * 60
        buy = sell = base = 0.0
        for venue in venues:
            for minute, acc in self.data.get(venue, {}).items():
                if minute >= since_m:
                    buy += acc.buy_usd
                    sell += acc.sell_usd
                    base += acc.sell_base
        return buy - sell, base, buy + sell

    def pop_dirty(self) -> list[tuple[str, str, int, Acc]]:
        rows: list[tuple[str, str, int, Acc]] = []
        for exchange, symbol, minute in list(self.dirty):
            acc = self.data.get((exchange, symbol), {}).get(minute)
            if acc is not None:
                rows.append((exchange, symbol, minute, acc))
        self.dirty.clear()
        return rows

    def prune(self, keep_sec: float, now: float) -> None:
        cutoff = int(now - keep_sec) // 60 * 60
        for buckets in self.data.values():
            for minute in list(buckets):
                if minute < cutoff:
                    del buckets[minute]
