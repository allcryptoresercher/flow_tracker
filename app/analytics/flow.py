"""Signed flow imbalance and the BUYING / SELLING / NEUTRAL state machine.

No I/O: callers feed minute-window stats and receive transitions.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

TF_SEC: dict[str, int] = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "12h": 12 * 60 * 60,
    "24h": 24 * 60 * 60,
}

ALERT_TFS: tuple[str, ...] = ("5m", "15m", "1h", "4h")
CONTEXT_TFS: tuple[str, ...] = ("12h", "24h")
ALL_TFS: tuple[str, ...] = ALERT_TFS + CONTEXT_TFS
DEFAULT_TFS: tuple[str, ...] = ("15m", "1h", "4h")
LOUD_TFS: frozenset[str] = frozenset({"1h", "4h", "12h", "24h"})

NEUTRAL = "NEUTRAL"
BUYING = "BUYING"
SELLING = "SELLING"

Venue = tuple[str, str]
StatsFn = Callable[[list[Venue], int, float], "tuple[WindowStats, float]"]


def step_sec(tf: str) -> int:
    return TF_SEC[tf] // 3


def is_alert_tf(tf: str) -> bool:
    return tf in ALERT_TFS


@dataclass(frozen=True)
class FlowParams:
    r_enter: float = 0.25
    r_exit: float = 0.10
    z_min: float = 2.0
    z_exit: float = 1.0
    confirmations: int = 2
    alert_cooldown_sec: float = 900.0


@dataclass(frozen=True)
class WindowStats:
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    sum_sq: float = 0.0
    trade_count: int = 0
    sell_base: float = 0.0

    @property
    def vol_usd(self) -> float:
        return self.buy_usd + self.sell_usd

    @property
    def delta_usd(self) -> float:
        return self.buy_usd - self.sell_usd

    @property
    def empty(self) -> bool:
        return self.trade_count <= 0 or self.vol_usd <= 0 or self.sum_sq <= 0


@dataclass(frozen=True)
class Imbalance:
    r: float
    z: float
    n_eff: float
    delta_usd: float
    vol_usd: float
    trade_count: int


def compute_imbalance(stats: WindowStats) -> Imbalance | None:
    """r = Δ/V, n_eff = V²/Σc², z = |r|√n_eff."""
    if stats.empty:
        return None
    volume = stats.vol_usd
    delta = stats.delta_usd
    r = delta / volume
    n_eff = (volume * volume) / stats.sum_sq
    if n_eff <= 0:
        return None
    z = abs(r) * math.sqrt(n_eff)
    return Imbalance(
        r=r,
        z=z,
        n_eff=n_eff,
        delta_usd=delta,
        vol_usd=volume,
        trade_count=stats.trade_count,
    )


def min_abs_r_for_z(n_eff: float, z_min: float) -> float:
    if n_eff <= 0:
        return 1.0
    return z_min / math.sqrt(n_eff)


def classify_target(current: str, r: float, z: float, params: FlowParams) -> str:
    """Desired state. Returning `current` means the hysteresis band holds."""
    if current in (BUYING, SELLING) and (abs(r) < params.r_exit or z < params.z_exit):
        return NEUTRAL
    if r <= -params.r_enter and z >= params.z_min and current != SELLING:
        return SELLING
    if r >= params.r_enter and z >= params.z_min and current != BUYING:
        return BUYING
    return current


@dataclass
class ScopeState:
    state: str = NEUTRAL
    entered_at: float = 0.0
    r: float = 0.0
    z: float = 0.0
    n_eff: float = 0.0
    delta_usd: float = 0.0
    vol_usd: float = 0.0
    trade_count: int = 0
    confirm_count: int = 0
    pending_state: str | None = None
    marker_trade_ts: float = 0.0
    last_eval_at: float = 0.0
    last_alert_at: float = 0.0
    prev_dir_state: str | None = None
    prev_dir_sec: float = 0.0


@dataclass(frozen=True)
class Transition:
    old: str
    new: str
    notify: bool
    held_sec: float
    prev_dir_state: str | None
    prev_dir_sec: float
    r: float
    z: float
    delta_usd: float
    vol_usd: float
    trade_count: int


def apply_silence(st: ScopeState, now: float) -> None:
    """Drop to NEUTRAL with no notification. Already-neutral state is kept."""
    if st.state in (BUYING, SELLING):
        held = max(0.0, now - st.entered_at) if st.entered_at else 0.0
        st.prev_dir_state = st.state
        st.prev_dir_sec = held
        st.state = NEUTRAL
        st.entered_at = now
    st.confirm_count = 0
    st.pending_state = None


def apply_check(
    st: ScopeState,
    *,
    target: str,
    trade_ts: float,
    now: float,
    imb: Imbalance,
    confirmations: int = 2,
) -> Transition | None:
    """One scheduled check. A repeated candidate counts only if a new trade arrived."""
    st.r = imb.r
    st.z = imb.z
    st.n_eff = imb.n_eff
    st.delta_usd = imb.delta_usd
    st.vol_usd = imb.vol_usd
    st.trade_count = imb.trade_count

    if target == st.state:
        st.confirm_count = 0
        st.pending_state = None
        st.marker_trade_ts = trade_ts
        return None

    if st.pending_state != target:
        st.pending_state = target
        st.confirm_count = 1
        st.marker_trade_ts = trade_ts
        return None

    if trade_ts <= st.marker_trade_ts:
        return None

    st.confirm_count += 1
    st.marker_trade_ts = trade_ts
    if st.confirm_count < confirmations:
        return None

    old = st.state
    held = max(0.0, now - st.entered_at) if st.entered_at else 0.0
    prev_state = st.prev_dir_state
    prev_sec = st.prev_dir_sec
    notify = target != NEUTRAL

    if target == NEUTRAL and old in (BUYING, SELLING):
        st.prev_dir_state = old
        st.prev_dir_sec = held
        prev_state = None
        prev_sec = 0.0
    elif notify:
        st.prev_dir_state = None
        st.prev_dir_sec = 0.0

    st.state = target
    st.entered_at = now
    st.confirm_count = 0
    st.pending_state = None

    return Transition(
        old=old,
        new=target,
        notify=notify,
        held_sec=held,
        prev_dir_state=prev_state if old == NEUTRAL else None,
        prev_dir_sec=prev_sec if old == NEUTRAL else 0.0,
        r=imb.r,
        z=imb.z,
        delta_usd=imb.delta_usd,
        vol_usd=imb.vol_usd,
        trade_count=imb.trade_count,
    )


def is_flip(tr: Transition) -> bool:
    return (
        tr.old in (BUYING, SELLING)
        and tr.new in (BUYING, SELLING)
        and tr.old != tr.new
    )


def cooldown_blocks(last_alert_at: float, now: float, transitions: list[Transition], cooldown: float) -> bool:
    if any(is_flip(t) for t in transitions):
        return False
    if last_alert_at <= 0:
        return False
    return (now - last_alert_at) < cooldown


def evaluate_scope(
    st: ScopeState,
    *,
    now: float,
    window_sec: int,
    stats: WindowStats | None,
    last_trade_ts: float,
    params: FlowParams,
    tracking_started: float,
) -> Transition | None:
    """Evaluate one scope. Empty window keeps the state. Long silence forces NEUTRAL."""
    if now < tracking_started + window_sec:
        return None

    st.last_eval_at = now
    silent = last_trade_ts <= 0 or (now - last_trade_ts) > 2 * window_sec
    if silent:
        apply_silence(st, now)
        return None

    if stats is None or stats.empty:
        return None

    imb = compute_imbalance(stats)
    if imb is None:
        return None
    target = classify_target(st.state, imb.r, imb.z, params)
    return apply_check(
        st,
        target=target,
        trade_ts=last_trade_ts,
        now=now,
        imb=imb,
        confirmations=params.confirmations,
    )


@dataclass
class ScopeSnap:
    changed: bool
    old: str
    new: str
    r: float
    z: float
    delta_usd: float
    vol_usd: float
    trades: int
    held_sec: float
    prev_dir_state: str | None
    prev_dir_sec: float


@dataclass
class NotifyBlock:
    tf: str
    ex: ScopeSnap
    agg: ScopeSnap


def _snap_from_transition(tr: Transition) -> ScopeSnap:
    return ScopeSnap(
        changed=True,
        old=tr.old,
        new=tr.new,
        r=tr.r,
        z=tr.z,
        delta_usd=tr.delta_usd,
        vol_usd=tr.vol_usd,
        trades=tr.trade_count,
        held_sec=tr.held_sec,
        prev_dir_state=tr.prev_dir_state,
        prev_dir_sec=tr.prev_dir_sec,
    )


def _snap_from_state(st: ScopeState) -> ScopeSnap:
    return ScopeSnap(
        changed=False,
        old=st.state,
        new=st.state,
        r=st.r,
        z=st.z,
        delta_usd=st.delta_usd,
        vol_usd=st.vol_usd,
        trades=st.trade_count,
        held_sec=0.0,
        prev_dir_state=None,
        prev_dir_sec=0.0,
    )


class FlowEngine:
    def __init__(self) -> None:
        self.scopes: dict[tuple[int, str, str], ScopeState] = {}

    def scope(self, watch_id: int, tf: str, name: str) -> ScopeState:
        key = (watch_id, tf, name)
        st = self.scopes.get(key)
        if st is None:
            st = ScopeState()
            self.scopes[key] = st
        return st

    def load(self, watch_id: int, tf: str, name: str, st: ScopeState) -> None:
        self.scopes[(watch_id, tf, name)] = st


@dataclass
class WatchEval:
    watch_id: int
    created_ts: float
    timeframes: list[str]
    ex_venues: list[Venue]
    agg_venues: list[Venue]
    muted: bool = False


def tf_due(tf: str, now: float, created_ts: float, last_eval: float) -> bool:
    window = TF_SEC[tf]
    if now < created_ts + window:
        return False
    if last_eval <= 0:
        return True
    return now + 0.5 >= last_eval + step_sec(tf)


def _ordered_tfs(selected: list[str]) -> list[str]:
    wanted = set(selected) | set(CONTEXT_TFS)
    return [tf for tf in ALL_TFS if tf in wanted]


def evaluate_watch(
    engine: FlowEngine,
    watch: WatchEval,
    now: float,
    params: FlowParams,
    stats_fn: StatsFn,
) -> list[NotifyBlock]:
    """Run every due timeframe. Notifying scopes of this tick become one batch."""
    blocks: list[NotifyBlock] = []
    for tf in _ordered_tfs(watch.timeframes):
        st_ex = engine.scope(watch.watch_id, tf, "ex")
        st_agg = engine.scope(watch.watch_id, tf, "agg")
        if not tf_due(tf, now, watch.created_ts, st_ex.last_eval_at):
            continue

        window = TF_SEC[tf]
        ex_stats, ex_last = stats_fn(watch.ex_venues, window, now)
        agg_stats, agg_last = stats_fn(watch.agg_venues, window, now)
        tr_ex = evaluate_scope(
            st_ex,
            now=now,
            window_sec=window,
            stats=ex_stats,
            last_trade_ts=ex_last,
            params=params,
            tracking_started=watch.created_ts,
        )
        tr_agg = evaluate_scope(
            st_agg,
            now=now,
            window_sec=window,
            stats=agg_stats,
            last_trade_ts=agg_last,
            params=params,
            tracking_started=watch.created_ts,
        )

        notifies = [t for t in (tr_ex, tr_agg) if t is not None and t.notify]
        if not notifies or not is_alert_tf(tf) or watch.muted:
            continue
        last_alert = max(st_ex.last_alert_at, st_agg.last_alert_at)
        if cooldown_blocks(last_alert, now, notifies, params.alert_cooldown_sec):
            continue

        st_ex.last_alert_at = now
        st_agg.last_alert_at = now
        ex_snap = _snap_from_transition(tr_ex) if tr_ex is not None and tr_ex.notify else _snap_from_state(st_ex)
        agg_snap = _snap_from_transition(tr_agg) if tr_agg is not None and tr_agg.notify else _snap_from_state(st_agg)
        # A non-notifying transition (into NEUTRAL) still updates the visible state.
        if tr_ex is not None and not tr_ex.notify:
            ex_snap = ScopeSnap(
                changed=False,
                old=tr_ex.new,
                new=tr_ex.new,
                r=tr_ex.r,
                z=tr_ex.z,
                delta_usd=tr_ex.delta_usd,
                vol_usd=tr_ex.vol_usd,
                trades=tr_ex.trade_count,
                held_sec=0.0,
                prev_dir_state=None,
                prev_dir_sec=0.0,
            )
        if tr_agg is not None and not tr_agg.notify:
            agg_snap = ScopeSnap(
                changed=False,
                old=tr_agg.new,
                new=tr_agg.new,
                r=tr_agg.r,
                z=tr_agg.z,
                delta_usd=tr_agg.delta_usd,
                vol_usd=tr_agg.vol_usd,
                trades=tr_agg.trade_count,
                held_sec=0.0,
                prev_dir_state=None,
                prev_dir_sec=0.0,
            )
        blocks.append(NotifyBlock(tf=tf, ex=ex_snap, agg=agg_snap))
    return blocks


def kind_rank(old: str, new: str) -> tuple[int, str] | None:
    if old == BUYING and new == SELLING:
        return (0, "flip_sell")
    if old == NEUTRAL and new == SELLING:
        return (1, "start_sell")
    if old == SELLING and new == BUYING:
        return (2, "flip_buy")
    if old == NEUTRAL and new == BUYING:
        return (3, "start_buy")
    return None


def primary_kind(blocks: list[NotifyBlock]) -> str | None:
    best: tuple[int, str] | None = None
    for block in blocks:
        for snap in (block.ex, block.agg):
            if not snap.changed:
                continue
            ranked = kind_rank(snap.old, snap.new)
            if ranked is None:
                continue
            if best is None or ranked[0] < best[0]:
                best = ranked
    return best[1] if best else None


def enters_selling(blocks: list[NotifyBlock]) -> list[NotifyBlock]:
    out: list[NotifyBlock] = []
    for block in blocks:
        for snap in (block.ex, block.agg):
            if snap.changed and snap.new == SELLING and snap.old != SELLING:
                out.append(block)
                break
    return out


def any_loud(blocks: list[NotifyBlock]) -> bool:
    for block in blocks:
        if block.tf not in LOUD_TFS:
            continue
        for snap in (block.ex, block.agg):
            if snap.changed and snap.new == SELLING and snap.old != SELLING:
                return True
    return False
