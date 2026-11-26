"""Watches, shared trade streams, minute buckets and the evaluation tick."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.analytics.big_trade import BigTradeDetector
from app.analytics.book import MinuteBook
from app.analytics.flow import (
    ALL_TFS,
    ALERT_TFS,
    CONTEXT_TFS,
    FlowEngine,
    ScopeState,
    TF_SEC,
    WatchEval,
    any_loud,
    compute_imbalance,
    enters_selling,
    evaluate_watch,
    primary_kind,
)
from app.bot.formatters import (
    format_big_summary,
    format_big_trade,
    format_deleted,
    format_deposit_line,
    format_pushover,
    format_restored,
    format_stall,
    format_started,
    format_state_message,
    format_status,
    format_watch_card,
)
from app.collectors.trade_collector import TradeCollector
from app.config import AppConfig
from app.db.models import Bucket, Event, FlowState, Watch
from app.db.session import get_session_factory
from app.exchanges.base import NormalizedTrade
from app.exchanges.ccxt_adapter import CcxtExchangeAdapter
from app.exchanges.names import display_name, ordered_enabled
from app.exchanges.registry import create_adapter
from app.notify.publisher import AlertPublisher
from app.notify.telegram_outbox import PRIO_BIG, PRIO_COMBO
from app.utils.fmt import fmt_clock, fmt_duration, fmt_pct, fmt_r, fmt_tokens, fmt_z
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _ts(dt: datetime | None) -> float:
    dt = _as_utc(dt)
    return dt.timestamp() if dt else 0.0


def _from_ts(ts: float) -> datetime | None:
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@dataclass
class LiveWatch:
    id: int
    coin: str
    exchange: str
    symbol: str
    deposit: float | None
    mode: str
    pushover: bool
    timeframes: list[str]
    venues: list[dict]
    muted_until: datetime | None
    created_at: datetime

    @property
    def created_ts(self) -> float:
        return _ts(self.created_at)

    def pairs(self) -> list[tuple[str, str]]:
        return [(v["exchange"], v["symbol"]) for v in self.venues]

    def ex_pairs(self) -> list[tuple[str, str]]:
        return [(self.exchange, self.symbol)]

    def muted(self, now: float) -> bool:
        until = _as_utc(self.muted_until)
        return bool(until and until.timestamp() > now)


class RuntimeHub:
    def __init__(self, config: AppConfig, publisher: AlertPublisher) -> None:
        self.config = config
        self.publisher = publisher
        self.book = MinuteBook()
        self.engine = FlowEngine()
        self.big = BigTradeDetector(config, most_liquid_exchange=self.most_liquid)
        self.adapters: dict[str, CcxtExchangeAdapter] = {}
        self.streams: dict[tuple[str, str], TradeCollector] = {}
        self.watches: dict[int, LiveWatch] = {}
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._liquidity: dict[str, tuple[float, str | None]] = {}
        self._stall_open: set[tuple[str, str]] = set()
        self._last_pushover: dict[int, float] = {}

    async def start(self) -> None:
        enabled = list(self.config.get("exchanges", "enabled", default=[]))
        for name in enabled:
            try:
                adapter = create_adapter(name)
                await adapter.load_markets()
                self.adapters[name] = adapter
            except Exception as exc:
                logger.error("Failed to init exchange %s: %s", name, exc)
        await self._restore()
        await self._sync_streams()
        self._tasks = [
            asyncio.create_task(self._flush_loop(), name="db-flush"),
            asyncio.create_task(self._eval_loop(), name="flow-eval"),
            asyncio.create_task(self._stall_loop(), name="stall-watch"),
            asyncio.create_task(self._big_flush_loop(), name="big-flush"),
        ]
        logger.info("RuntimeHub started, watches=%s adapters=%s", len(self.watches), len(self.adapters))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        for collector in list(self.streams.values()):
            await collector.stop()
        self.streams.clear()
        for adapter in self.adapters.values():
            await adapter.close()

    def find_venues(self, coin: str) -> list[dict]:
        coin = coin.upper().strip()
        found: list[dict] = []
        for name in ordered_enabled(list(self.adapters)):
            adapter = self.adapters.get(name)
            if adapter is None:
                continue
            symbol = adapter.resolve_symbol(coin)
            if not symbol:
                continue
            found.append(
                {"exchange": name, "symbol": symbol, "label": display_name(name)}
            )
        return found

    async def create_watch(
        self,
        *,
        coin: str,
        exchange: str,
        deposit: float | None,
        mode: str,
        pushover: bool,
        venues: list[dict],
    ) -> str:
        coin = coin.upper().strip()
        exchange = exchange.lower().strip()
        if pushover and not _pushover_ready():
            raise ValueError("Ключи Pushover не заданы")
        chosen = next((v for v in venues if v["exchange"] == exchange), None)
        if chosen is None:
            raise ValueError("Биржа недоступна для этой монеты")
        limit = int(self.config.get("flow", "max_watches", default=50))
        async with self._lock:
            if len(self.watches) >= limit:
                raise ValueError(f"Лимит наблюдений: {limit}")
            if any(w.coin == coin and w.exchange == exchange for w in self.watches.values()):
                raise ValueError(f"{coin} на {display_name(exchange)} уже отслеживается")
            tfs = self.config.default_timeframes()
            row = Watch(
                coin=coin,
                exchange=exchange,
                symbol=chosen["symbol"],
                deposit_tokens=deposit,
                mode=mode if mode in ("quiet", "full") else "quiet",
                pushover=bool(pushover),
                timeframes=tfs,
                venues=[{"exchange": v["exchange"], "symbol": v["symbol"]} for v in venues],
                created_at=_utcnow(),
            )
            factory = get_session_factory()
            async with factory() as session:
                session.add(row)
                await session.commit()
                await session.refresh(row)
            live = _live_from_row(row)
            self.watches[live.id] = live
            await self._sync_streams()
        labels = [display_name(v["exchange"]) for v in live.venues]
        return format_started(
            watch_id=live.id,
            coin=live.coin,
            exchange_label=display_name(live.exchange),
            timeframes=list(live.timeframes),
            mode=live.mode,
            pushover=live.pushover,
            venues=labels,
        )

    def list_cards(self) -> list[tuple[int, str, bool, str]]:
        """Return (id, text, pushover, mode) for the watch list."""
        now = time.time()
        cards: list[tuple[int, str, bool, str]] = []
        for watch in sorted(self.watches.values(), key=lambda w: w.id):
            lines = self._state_lines(watch, now, alert_only=True)
            muted = None
            if watch.muted(now) and watch.muted_until:
                muted = "заглушено до " + fmt_clock(watch.muted_until, _tz())
            text = format_watch_card(
                watch_id=watch.id,
                coin=watch.coin,
                exchange_label=display_name(watch.exchange),
                mode=watch.mode,
                pushover=watch.pushover,
                timeframes=list(watch.timeframes),
                lines=lines,
                muted_txt=muted,
            )
            cards.append((watch.id, text, watch.pushover, watch.mode))
        return cards

    def status_text(self, watch_id: int) -> str:
        watch = self.watches.get(watch_id)
        if watch is None:
            raise ValueError("Наблюдение не найдено")
        now = time.time()
        lines = self._state_lines(watch, now, alert_only=False)
        last = self.book.last_trade_ts(watch.ex_pairs())
        if last <= 0:
            last_txt = "нет сделок"
        else:
            age = fmt_duration(max(0.0, now - last))
            last_txt = f"{fmt_clock(_from_ts(last) or _utcnow(), _tz())} ({age} назад)"
        muted = None
        if watch.muted(now) and watch.muted_until:
            muted = "заглушено до " + fmt_clock(watch.muted_until, _tz())
        realization = None
        if watch.mode == "full" and watch.deposit:
            _delta, sold, _vol = self.book.sum_since(watch.ex_pairs(), watch.created_ts)
            pct = sold / watch.deposit * 100.0
            realization = (
                f"Реализация (оценка): {fmt_pct(pct)} "
                f"({fmt_tokens(sold)} / {fmt_tokens(watch.deposit)} {watch.coin})"
            )
        return format_status(
            watch_id=watch.id,
            coin=watch.coin,
            exchange_label=display_name(watch.exchange),
            mode=watch.mode,
            pushover=watch.pushover,
            timeframes=list(watch.timeframes),
            lines=lines,
            last_txt=last_txt,
            venues=[display_name(v["exchange"]) for v in watch.venues],
            realization=realization,
            muted_txt=muted,
        )

    async def delete_watch(self, watch_id: int) -> str:
        async with self._lock:
            watch = self.watches.get(watch_id)
            if watch is None:
                raise ValueError("Наблюдение не найдено")
            now = time.time()
            changes = await self._count_changes(watch_id)
            delta_ex, _sold_ex, _ = self.book.sum_since(watch.ex_pairs(), watch.created_ts)
            delta_agg, sold_agg_unused, _ = self.book.sum_since(watch.pairs(), watch.created_ts)
            _ = sold_agg_unused
            realization = None
            if watch.mode == "full" and watch.deposit:
                _d, sold, _v = self.book.sum_since(watch.ex_pairs(), watch.created_ts)
                realization = sold / watch.deposit * 100.0
            text = format_deleted(
                watch_id=watch.id,
                coin=watch.coin,
                exchange_label=display_name(watch.exchange),
                worked_sec=max(0.0, now - watch.created_ts),
                changes=changes,
                delta_ex=delta_ex,
                delta_agg=delta_agg,
                realization=realization,
            )
            factory = get_session_factory()
            async with factory() as session:
                row = await session.get(Watch, watch_id)
                if row is not None:
                    await session.delete(row)
                    await session.commit()
            self.watches.pop(watch_id, None)
            self._drop_engine(watch_id)
            self._last_pushover.pop(watch_id, None)
            if not any(w.coin == watch.coin for w in self.watches.values()):
                self.big.clear_coin(watch.coin)
            await self._sync_streams()
            return text

    async def toggle_pushover(self, watch_id: int) -> str:
        async with self._lock:
            watch = self._need(watch_id)
            if not watch.pushover and not _pushover_ready():
                raise ValueError("Ключи Pushover не заданы в .env")
            watch.pushover = not watch.pushover
            await self._save(watch)
            state = "включён" if watch.pushover else "выключен"
            return f"#{watch.id}: Pushover {state}"

    async def toggle_mode(self, watch_id: int) -> str:
        async with self._lock:
            watch = self._need(watch_id)
            watch.mode = "full" if watch.mode == "quiet" else "quiet"
            await self._save(watch)
            label = "расширенный" if watch.mode == "full" else "тихий"
            return f"#{watch.id}: режим {label}"

    async def set_deposit(self, watch_id: int, deposit: float | None) -> str:
        async with self._lock:
            watch = self._need(watch_id)
            watch.deposit = deposit
            await self._save(watch)
            if deposit is None:
                return f"#{watch.id}: депозит сброшен"
            return f"#{watch.id}: депозит {fmt_tokens(deposit)} {watch.coin}"

    async def toggle_timeframe(self, watch_id: int, tf: str) -> list[str]:
        if tf not in ALERT_TFS:
            raise ValueError("Неизвестный таймфрейм")
        async with self._lock:
            watch = self._need(watch_id)
            selected = [item for item in watch.timeframes if item in ALERT_TFS]
            if tf in selected:
                if len(selected) == 1:
                    raise ValueError("Нужен хотя бы один таймфрейм")
                selected.remove(tf)
            else:
                selected.append(tf)
            watch.timeframes = [item for item in ALERT_TFS if item in selected]
            await self._save(watch)
            return list(watch.timeframes)

    def timeframes_of(self, watch_id: int) -> list[str]:
        return list(self._need(watch_id).timeframes)

    async def mute(self, watch_id: int, seconds: int) -> str:
        if seconds <= 0:
            raise ValueError("Укажите длительность, например 2h")
        async with self._lock:
            watch = self._need(watch_id)
            watch.muted_until = _utcnow() + timedelta(seconds=seconds)
            await self._save(watch)
            return (
                f"#{watch.id} заглушено на {fmt_duration(seconds)} "
                f"(до {fmt_clock(watch.muted_until, _tz())})"
            )

    async def on_trade(self, trade: NormalizedTrade) -> None:
        ts = trade.timestamp.timestamp() if trade.timestamp else time.time()
        self.book.add(trade.exchange, trade.symbol, ts, trade.side, trade.cost, trade.amount)
        ids = self._full_watch_ids(trade.exchange, trade.symbol, include_muted=False)
        # Still feed the detector when every full watch is muted, so the
        # threshold window keeps filling. Alerts are addressed only to open watches.
        if ids or self._full_watch_ids(trade.exchange, trade.symbol, include_muted=True):
            alerts = await self.big.on_trade(trade, ids)
            for alert in alerts:
                await self._emit_big(alert)

    async def most_liquid(self, coin: str) -> str | None:
        coin = coin.upper()
        ttl = float(self.config.get("big", "most_liquid_cache_sec", default=60))
        now = time.time()
        cached = self._liquidity.get(coin)
        if cached and now - cached[0] < ttl:
            return cached[1]
        best: str | None = None
        best_vol = -1.0
        for exchange, symbol in self._venues_of_coin(coin):
            adapter = self.adapters.get(exchange)
            if adapter is None:
                continue
            try:
                vol = await adapter.fetch_quote_volume(symbol)
            except Exception as exc:
                logger.warning("quote volume %s %s: %s", exchange, symbol, exc)
                continue
            if vol is not None and vol > best_vol:
                best_vol = vol
                best = exchange
        self._liquidity[coin] = (now, best)
        return best

    async def evaluate(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for watch in list(self.watches.values()):
            try:
                await self._evaluate_one(watch, now)
            except Exception:
                logger.exception("evaluate failed for watch %s", watch.id)
        try:
            await self._persist_states()
        except Exception:
            logger.exception("persist flow state failed")

    async def _evaluate_one(self, watch: LiveWatch, now: float) -> None:
        params = self.config.flow_params(watch.coin)

        def stats_fn(venues, window, ts):
            return self.book.window(venues, ts - window, ts), self.book.last_trade_ts(venues)

        blocks = evaluate_watch(
            self.engine,
            WatchEval(
                watch_id=watch.id,
                created_ts=watch.created_ts,
                timeframes=list(watch.timeframes),
                ex_venues=watch.ex_pairs(),
                agg_venues=watch.pairs(),
                muted=watch.muted(now),
            ),
            now,
            params,
            stats_fn,
        )
        if not blocks:
            return

        deposit_line = None
        big_line = None
        if watch.mode == "full":
            if watch.deposit:
                _delta, sold, _vol = self.book.sum_since(watch.ex_pairs(), watch.created_ts)
                deposit_line = format_deposit_line(
                    deposit=watch.deposit,
                    sold=sold,
                    coin=watch.coin,
                    exchange_label=display_name(watch.exchange),
                )
            window_min = float(self.config.get("big", "summary_minutes", default=30))
            big_line = format_big_summary(self.big.recent_summary(watch.id, window_min))

        text = format_state_message(
            coin=watch.coin,
            exchange_label=display_name(watch.exchange),
            watch_id=watch.id,
            blocks=blocks,
            deposit_line=deposit_line,
            big_line=big_line,
        )
        loud = any_loud(blocks)
        kind = primary_kind(blocks) or "state"
        minute = int(now) // 60
        await self._emit(
            {
                "type": "state",
                "watch_id": watch.id,
                "text": text,
                "silent": not loud,
                "priority": PRIO_COMBO if loud else PRIO_BIG,
                "idempotency_key": f"msg:{watch.id}:{minute}:{kind}",
            }
        )
        for block in blocks:
            for scope, snap in (("ex", block.ex), ("agg", block.agg)):
                if not snap.changed:
                    continue
                await self._insert_event(
                    watch.id,
                    "state_change",
                    f"st:{watch.id}:{block.tf}:{scope}:{snap.old}:{snap.new}:{minute}",
                    {"tf": block.tf, "scope": scope, "old": snap.old, "new": snap.new},
                )

        if watch.pushover and enters_selling(blocks):
            cool = float(self.config.get("flow", "pushover_cooldown_sec", default=3600))
            last = self._last_pushover.get(watch.id, 0.0)
            if now - last >= cool:
                self._last_pushover[watch.id] = now
                block = enters_selling(blocks)[0]
                title, body = format_pushover(watch.coin, display_name(watch.exchange), block)
                await self._emit(
                    {
                        "type": "pushover",
                        "watch_id": watch.id,
                        "title": title,
                        "text": body,
                        "idempotency_key": f"po:{watch.id}:{minute}",
                    }
                )

    def _state_lines(self, watch: LiveWatch, now: float, *, alert_only: bool) -> list[str]:
        selected = list(watch.timeframes)
        tfs = [tf for tf in ALL_TFS if tf in selected or (not alert_only and tf in CONTEXT_TFS)]
        lines: list[str] = []
        for tf in tfs:
            window = TF_SEC[tf]
            tag = " · контекст" if tf in CONTEXT_TFS and tf not in selected else ""
            if now < watch.created_ts + window:
                lines.append(f"{tf}{tag}: прогрев")
                continue
            ex_stats = self.book.window(watch.ex_pairs(), now - window, now)
            agg_stats = self.book.window(watch.pairs(), now - window, now)
            ex_imb = compute_imbalance(ex_stats)
            agg_imb = compute_imbalance(agg_stats)
            ex_state = self.engine.scope(watch.id, tf, "ex")
            agg_state = self.engine.scope(watch.id, tf, "agg")
            lines.append(
                f"{tf}{tag}: биржа {ex_state.state} r {fmt_r(ex_imb.r if ex_imb else ex_state.r)} "
                f"z {fmt_z(ex_imb.z if ex_imb else ex_state.z)} · "
                f"сводно {agg_state.state} r {fmt_r(agg_imb.r if agg_imb else agg_state.r)} "
                f"z {fmt_z(agg_imb.z if agg_imb else agg_state.z)}"
            )
        return lines

    async def _restore(self) -> None:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(select(Watch))
            rows = list(result.scalars().all())
            states = list((await session.execute(select(FlowState))).scalars().all())
            history = int(self.config.get("flow", "history_sec", default=90000))
            since = _utcnow() - timedelta(seconds=history)
            buckets = list(
                (
                    await session.execute(select(Bucket).where(Bucket.minute_start >= since))
                ).scalars().all()
            )
            po = await session.execute(
                select(Event.watch_id, func.max(Event.ts))
                .where(Event.type == "pushover")
                .group_by(Event.watch_id)
            )
            for watch_id, ts in po.all():
                if watch_id is not None and ts is not None:
                    self._last_pushover[int(watch_id)] = _ts(ts)

        for row in rows:
            self.watches[row.id] = _live_from_row(row)
        for row in states:
            self.engine.load(row.watch_id, row.tf, row.scope, _scope_from_row(row))
        for row in buckets:
            self.book.load_acc(
                row.exchange,
                row.symbol,
                int(row.minute_start.timestamp()) // 60 * 60,
                buy_usd=row.buy_usd,
                sell_usd=row.sell_usd,
                sum_sq=row.sum_sq,
                trade_count=row.trade_count,
                sell_base=row.sell_base,
            )
        logger.info("Restored watches=%s buckets=%s", len(rows), len(buckets))

    async def _sync_streams(self) -> None:
        needed: set[tuple[str, str]] = set()
        for watch in self.watches.values():
            for pair in watch.pairs():
                needed.add(pair)
        for exchange, symbol in sorted(needed):
            if (exchange, symbol) in self.streams:
                continue
            adapter = self.adapters.get(exchange)
            if adapter is None:
                logger.warning("No adapter for %s, stream %s skipped", exchange, symbol)
                continue
            collector = TradeCollector(adapter, symbol, self.on_trade, self.config)
            collector.start()
            self.streams[(exchange, symbol)] = collector
        for key in list(self.streams):
            if key not in needed:
                await self.streams[key].stop()
                del self.streams[key]
                self._stall_open.discard(key)

    async def _flush_loop(self) -> None:
        interval = float(self.config.get("flush_interval_sec", default=2))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._flush_buckets()
            except Exception:
                logger.exception("bucket flush failed")

    async def _flush_buckets(self) -> None:
        rows = self.book.pop_dirty()
        if rows:
            values = [
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "minute_start": datetime.fromtimestamp(minute, tz=timezone.utc),
                    "buy_usd": acc.buy_usd,
                    "sell_usd": acc.sell_usd,
                    "sum_sq": acc.sum_sq,
                    "trade_count": acc.trade_count,
                    "sell_base": acc.sell_base,
                }
                for exchange, symbol, minute, acc in rows
            ]
            stmt = pg_insert(Bucket).values(values)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_bucket",
                set_={
                    "buy_usd": stmt.excluded.buy_usd,
                    "sell_usd": stmt.excluded.sell_usd,
                    "sum_sq": stmt.excluded.sum_sq,
                    "trade_count": stmt.excluded.trade_count,
                    "sell_base": stmt.excluded.sell_base,
                },
            )
            factory = get_session_factory()
            async with factory() as session:
                await session.execute(stmt)
                await session.commit()
        keep = float(self.config.get("flow", "history_sec", default=90000))
        self.book.prune(keep, time.time())

    async def _persist_states(self) -> None:
        values = []
        for (watch_id, tf, scope), st in self.engine.scopes.items():
            if watch_id not in self.watches:
                continue
            if st.last_eval_at <= 0 and st.confirm_count == 0 and st.state == "NEUTRAL":
                continue
            values.append(
                {
                    "watch_id": watch_id,
                    "tf": tf,
                    "scope": scope,
                    "state": st.state,
                    "entered_at": _from_ts(st.entered_at),
                    "r": st.r,
                    "z": st.z,
                    "confirm_count": st.confirm_count,
                    "pending_state": st.pending_state,
                    "marker_trade_ts": st.marker_trade_ts,
                    "last_eval_at": _from_ts(st.last_eval_at),
                    "last_alert_at": _from_ts(st.last_alert_at),
                    "prev_dir_state": st.prev_dir_state,
                    "prev_dir_sec": st.prev_dir_sec,
                    "delta_usd": st.delta_usd,
                    "vol_usd": st.vol_usd,
                    "trade_count": st.trade_count,
                }
            )
        if not values:
            return
        stmt = pg_insert(FlowState).values(values)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["watch_id", "tf", "scope"],
            set_={
                "state": excluded.state,
                "entered_at": excluded.entered_at,
                "r": excluded.r,
                "z": excluded.z,
                "confirm_count": excluded.confirm_count,
                "pending_state": excluded.pending_state,
                "marker_trade_ts": excluded.marker_trade_ts,
                "last_eval_at": excluded.last_eval_at,
                "last_alert_at": excluded.last_alert_at,
                "prev_dir_state": excluded.prev_dir_state,
                "prev_dir_sec": excluded.prev_dir_sec,
                "delta_usd": excluded.delta_usd,
                "vol_usd": excluded.vol_usd,
                "trade_count": excluded.trade_count,
            },
        )
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def _eval_loop(self) -> None:
        while True:
            now = time.time()
            delay = 60.0 - (now % 60.0) + 0.2
            await asyncio.sleep(delay)
            await self.evaluate()

    async def _big_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                for alert in self.big.flush():
                    await self._emit_big(alert)
            except Exception:
                logger.exception("big-trade flush failed")

    async def _stall_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_stalls()
            except Exception:
                logger.exception("stall check failed")

    async def _check_stalls(self) -> None:
        stall = float(self.config.get("flow", "data_stall_sec", default=300))
        now = time.time()
        for key, collector in list(self.streams.items()):
            idle = now - collector.last_msg
            exchange, _symbol = key
            ids = self._watch_ids_for_stream(key)
            if not ids:
                continue
            if idle >= stall and key not in self._stall_open:
                self._stall_open.add(key)
                minutes = max(1, int(idle // 60))
                await self._emit(
                    {
                        "type": "stall",
                        "text": format_stall(ids, display_name(exchange), minutes),
                        "silent": False,
                        "priority": PRIO_COMBO,
                        "idempotency_key": f"stall:{exchange}:{_symbol}:{int(collector.last_msg)}",
                    }
                )
            elif idle < 30 and key in self._stall_open:
                self._stall_open.discard(key)
                await self._emit(
                    {
                        "type": "restored",
                        "text": format_restored(ids, display_name(exchange)),
                        "silent": True,
                        "priority": PRIO_BIG,
                        "idempotency_key": f"restored:{exchange}:{_symbol}:{int(now)}",
                    }
                )

    async def _emit_big(self, alert: dict) -> None:
        watch = self.watches.get(int(alert["watch_id"]))
        if watch is None or watch.mode != "full":
            return
        text = format_big_trade(
            side=alert["side"],
            cost=alert["cost_usd"],
            symbol=alert["symbol"],
            exchange_label=display_name(alert["exchange"]),
            watch_id=watch.id,
            price=alert["price"],
            amount=alert["amount"],
            trades=alert["trades"],
            threshold=alert["threshold_usd"],
            avg=alert["avg_usd"],
            mode=alert["threshold_mode"],
            k=alert["k"],
            floor=alert["floor_usd"],
        )
        await self._emit(
            {
                "type": "big_trade",
                "watch_id": watch.id,
                "text": text,
                "silent": True,
                "priority": PRIO_BIG,
                "idempotency_key": alert["idempotency_key"],
            }
        )

    async def _emit(self, alert: dict) -> None:
        key = alert.get("idempotency_key")
        if key:
            saved = await self._insert_event(
                alert.get("watch_id"),
                str(alert.get("type") or "message"),
                key,
                {"text": alert.get("text"), "title": alert.get("title")},
            )
            if not saved:
                return
        await self.publisher.publish(alert)

    async def _insert_event(
        self,
        watch_id: int | None,
        event_type: str,
        key: str,
        payload: dict | None,
    ) -> bool:
        factory = get_session_factory()
        async with factory() as session:
            session.add(
                Event(
                    watch_id=watch_id,
                    type=event_type,
                    ts=_utcnow(),
                    payload=payload,
                    idempotency_key=key[:160],
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def _count_changes(self, watch_id: int) -> int:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(Event)
                .where(Event.watch_id == watch_id, Event.type == "state_change")
            )
            return int(result.scalar_one() or 0)

    async def _save(self, watch: LiveWatch) -> None:
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(Watch, watch.id)
            if row is None:
                return
            row.mode = watch.mode
            row.pushover = watch.pushover
            row.deposit_tokens = watch.deposit
            row.timeframes = list(watch.timeframes)
            row.venues = list(watch.venues)
            row.muted_until = watch.muted_until
            await session.commit()

    def _need(self, watch_id: int) -> LiveWatch:
        watch = self.watches.get(watch_id)
        if watch is None:
            raise ValueError("Наблюдение не найдено")
        return watch

    def _drop_engine(self, watch_id: int) -> None:
        for key in list(self.engine.scopes):
            if key[0] == watch_id:
                self.engine.scopes.pop(key, None)

    def _full_watch_ids(self, exchange: str, symbol: str, *, include_muted: bool) -> list[int]:
        now = time.time()
        ids: list[int] = []
        for watch in self.watches.values():
            if watch.mode != "full":
                continue
            if not include_muted and watch.muted(now):
                continue
            if any(v["exchange"] == exchange and v["symbol"] == symbol for v in watch.venues):
                ids.append(watch.id)
        return ids

    def _watch_ids_for_stream(self, key: tuple[str, str]) -> list[int]:
        exchange, symbol = key
        return [
            w.id
            for w in self.watches.values()
            if any(v["exchange"] == exchange and v["symbol"] == symbol for v in w.venues)
        ]

    def _venues_of_coin(self, coin: str) -> list[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str]] = []
        for watch in self.watches.values():
            if watch.coin != coin:
                continue
            for pair in watch.pairs():
                if pair not in seen:
                    seen.add(pair)
                    out.append(pair)
        return out


def _live_from_row(row: Watch) -> LiveWatch:
    return LiveWatch(
        id=row.id,
        coin=row.coin,
        exchange=row.exchange,
        symbol=row.symbol,
        deposit=row.deposit_tokens,
        mode=row.mode,
        pushover=row.pushover,
        timeframes=list(row.timeframes or []),
        venues=list(row.venues or []),
        muted_until=row.muted_until,
        created_at=_as_utc(row.created_at) or _utcnow(),
    )


def _scope_from_row(row: FlowState) -> ScopeState:
    return ScopeState(
        state=row.state,
        entered_at=_ts(row.entered_at),
        r=row.r,
        z=row.z,
        delta_usd=row.delta_usd,
        vol_usd=row.vol_usd,
        trade_count=row.trade_count,
        confirm_count=row.confirm_count,
        pending_state=row.pending_state,
        marker_trade_ts=row.marker_trade_ts,
        last_eval_at=_ts(row.last_eval_at),
        last_alert_at=_ts(row.last_alert_at),
        prev_dir_state=row.prev_dir_state,
        prev_dir_sec=row.prev_dir_sec,
    )


def _tz() -> str:
    try:
        from app.config import get_settings

        return get_settings().tz
    except Exception:
        return "Europe/Vienna"


def _pushover_ready() -> bool:
    try:
        from app.config import get_settings

        return get_settings().pushover_configured
    except Exception:
        return False
