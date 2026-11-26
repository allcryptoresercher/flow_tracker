from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from app.exchanges.base import NormalizedTrade
from app.exchanges.ccxt_adapter import CcxtExchangeAdapter
from app.utils.logging import get_logger
from app.utils.reconnect import PermanentStreamError, TransientStreamPause, with_exponential_backoff

logger = get_logger(__name__)

OnTrade = Callable[[NormalizedTrade], Awaitable[None]]


class TradeCollector:
    """One public trade stream for an exchange symbol, shared by every watch of that coin."""

    def __init__(
        self,
        adapter: CcxtExchangeAdapter,
        symbol: str,
        on_trade: OnTrade,
        config,
    ) -> None:
        self.adapter = adapter
        self.symbol = symbol
        self.on_trade = on_trade
        self.config = config
        self.last_msg = time.time()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._seen: set[str] = set()

    def start(self) -> None:
        self._stop.clear()
        self.last_msg = time.time()
        self._task = asyncio.create_task(
            self._run(), name=f"trades-{self.adapter.name}-{self.symbol}"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        logger.info("Starting trade stream %s %s", self.adapter.name, self.symbol)
        base_delay = float(self.config.get("reconnect", "base_delay", default=2.0))
        max_delay = float(self.config.get("reconnect", "max_delay", default=300.0))
        max_attempts = int(self.config.get("reconnect", "max_attempts_before_pause", default=30))
        resume_after = float(self.config.get("reconnect", "resume_after_sec", default=300))

        async def _loop() -> None:
            async for trades in self.adapter.watch_trades(self.symbol):
                if self._stop.is_set():
                    return
                self.last_msg = time.time()
                for trade in trades:
                    if trade.trade_id in self._seen:
                        continue
                    self._seen.add(trade.trade_id)
                    if len(self._seen) > 10000:
                        self._seen = set(list(self._seen)[-5000:])
                    try:
                        await self.on_trade(trade)
                    except Exception:
                        logger.exception(
                            "Trade handler failed %s %s", self.adapter.name, self.symbol
                        )

        while not self._stop.is_set():
            try:
                await with_exponential_backoff(
                    _loop,
                    name=f"ws:{self.adapter.name}:{self.symbol}",
                    base_delay=base_delay,
                    max_delay=max_delay,
                    max_attempts=max_attempts,
                )
            except asyncio.CancelledError:
                raise
            except TransientStreamPause as exc:
                logger.error(
                    "Cooling down trade stream %s %s: %s",
                    self.adapter.name,
                    self.symbol,
                    exc,
                )
                await asyncio.sleep(resume_after)
            except PermanentStreamError as exc:
                logger.error(
                    "Permanently stopping trade stream %s %s: %s",
                    self.adapter.name,
                    self.symbol,
                    exc,
                )
                return
            except Exception:
                logger.exception("Trade collector fatal %s %s", self.adapter.name, self.symbol)
                await asyncio.sleep(5)
