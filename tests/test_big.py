from datetime import datetime, timezone

from app.analytics.big_trade import BigTradeDetector
from app.bot.formatters import format_big_trade
from app.exchanges.base import NormalizedTrade


class Cfg:
    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, *keys, default=None):
        cur = self._data
        for key in keys:
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return cur

    def coin_get(self, coin: str, *keys, default=None):
        return self.get("coins", coin.upper(), *keys, default=default)


def _cfg(**coins) -> Cfg:
    return Cfg(
        {
            "big": {
                "window_minutes": 60,
                "k_multiplier": 15,
                "aggregate_ms": 1500,
                "floor_usd": 1000,
                "min_samples": 10,
                "most_liquid_cache_sec": 60,
            },
            "coins": coins,
        }
    )


def _trade(i: int, cost: float, ts: float, side: str = "sell") -> NormalizedTrade:
    return NormalizedTrade(
        exchange="binance",
        symbol="SOL/USDT",
        trade_id=str(i),
        price=100.0,
        amount=cost / 100.0,
        cost=cost,
        side=side,  # type: ignore[arg-type]
        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
    )


async def _noop(_coin: str):
    return "binance"


def test_threshold_glue_and_floor():
    now = {"t": 1_000.0}

    async def run():
        det = BigTradeDetector(_cfg(), most_liquid_exchange=_noop, clock=lambda: now["t"])
        for i in range(10):
            now["t"] = 1000 + i
            await det.on_trade(_trade(i, 100, now["t"]), [3])
        now["t"] = 2000
        assert await det.on_trade(_trade(20, 500, now["t"]), [3]) == []
        now["t"] = 2001
        assert await det.on_trade(_trade(21, 5000, now["t"]), [3]) == []
        now["t"] = 2001.4
        assert await det.on_trade(_trade(22, 5000, now["t"], side="sell"), [3]) == []
        now["t"] = 2004
        alerts = det.flush()
        assert len(alerts) == 1
        assert alerts[0]["trades"] == 2
        assert alerts[0]["cost_usd"] == 10000
        assert alerts[0]["watch_id"] == 3
        text = format_big_trade(
            side=alerts[0]["side"],
            cost=alerts[0]["cost_usd"],
            symbol=alerts[0]["symbol"],
            exchange_label="Binance",
            watch_id=3,
            price=alerts[0]["price"],
            amount=alerts[0]["amount"],
            trades=alerts[0]["trades"],
            threshold=alerts[0]["threshold_usd"],
            avg=alerts[0]["avg_usd"],
            mode=alerts[0]["threshold_mode"],
            k=alerts[0]["k"],
            floor=alerts[0]["floor_usd"],
        )
        assert "SELL" in text and "#3" in text
        assert "склеено 2 сделки" in text
        summary = det.recent_summary(3, 30)
        assert summary["sell_n"] == 1
        assert summary["sell_usd"] == 10000

    import asyncio

    asyncio.run(run())


def test_absolute_threshold_replaces_formula():
    now = {"t": 5_000.0}

    async def run():
        det = BigTradeDetector(
            _cfg(SOL={"big": {"usd": 8000}}),
            most_liquid_exchange=_noop,
            clock=lambda: now["t"],
        )
        await det.on_trade(_trade(1, 5000, now["t"]), [7])
        now["t"] = 5002
        assert det.flush() == []
        now["t"] = 5010
        await det.on_trade(_trade(2, 9000, now["t"]), [7])
        now["t"] = 5012
        alerts = det.flush()
        assert len(alerts) == 1
        assert alerts[0]["threshold_mode"] == "abs"
        assert alerts[0]["cost_usd"] == 9000

    import asyncio

    asyncio.run(run())


def test_floor_when_few_samples():
    now = {"t": 8_000.0}

    async def run():
        det = BigTradeDetector(_cfg(), most_liquid_exchange=_noop, clock=lambda: now["t"])
        for i in range(3):
            now["t"] = 8000 + i
            await det.on_trade(_trade(i, 10, now["t"]), [1])
        now["t"] = 8100
        await det.on_trade(_trade(9, 500, now["t"]), [1])
        now["t"] = 8102
        assert det.flush() == []
        now["t"] = 8200
        await det.on_trade(_trade(10, 2000, now["t"]), [1])
        now["t"] = 8202
        alerts = det.flush()
        assert len(alerts) == 1
        assert alerts[0]["threshold_mode"] == "floor"

    import asyncio

    asyncio.run(run())
