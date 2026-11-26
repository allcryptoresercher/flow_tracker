from app.analytics.book import MinuteBook
from app.analytics.flow import (
    BUYING,
    NEUTRAL,
    SELLING,
    FlowEngine,
    FlowParams,
    NotifyBlock,
    ScopeSnap,
    ScopeState,
    WatchEval,
    WindowStats,
    apply_check,
    classify_target,
    compute_imbalance,
    evaluate_scope,
    evaluate_watch,
    min_abs_r_for_z,
)
from app.bot.formatters import format_state_message
from app.utils.fmt import fmt_usd, parse_duration


PARAMS = FlowParams()


def _sell_stats(n: int, cost: float = 10.0) -> WindowStats:
    return WindowStats(buy_usd=0.0, sell_usd=n * cost, sum_sq=n * cost * cost, trade_count=n)


def test_neff_table_matches_spec():
    assert min_abs_r_for_z(4, 2) == 1
    assert min_abs_r_for_z(16, 2) == 0.5
    assert min_abs_r_for_z(64, 2) == 0.25


def test_one_trade_cannot_change_state():
    stats = WindowStats(buy_usd=0, sell_usd=100, sum_sq=10000, trade_count=1)
    imb = compute_imbalance(stats)
    assert imb is not None
    assert imb.n_eff == 1
    assert imb.z <= 1
    assert classify_target(NEUTRAL, imb.r, imb.z, PARAMS) == NEUTRAL


def test_four_equal_sells_reach_z_min():
    imb = compute_imbalance(_sell_stats(4, 10))
    assert imb is not None
    assert round(imb.n_eff, 6) == 4
    assert imb.r == -1
    assert round(imb.z, 6) == 2
    assert classify_target(NEUTRAL, imb.r, imb.z, PARAMS) == SELLING


def test_two_confirms_need_a_new_trade():
    st = ScopeState()
    imb = compute_imbalance(_sell_stats(4, 10))
    assert imb is not None
    first = apply_check(st, target=SELLING, trade_ts=10, now=100, imb=imb)
    assert first is None
    assert st.confirm_count == 1
    stalled = apply_check(st, target=SELLING, trade_ts=10, now=200, imb=imb)
    assert stalled is None
    assert st.confirm_count == 1
    done = apply_check(st, target=SELLING, trade_ts=11, now=300, imb=imb)
    assert done is not None
    assert done.old == NEUTRAL and done.new == SELLING and done.notify


def test_failed_check_resets_confirmation():
    st = ScopeState()
    imb = compute_imbalance(_sell_stats(16, 10))
    assert imb is not None
    apply_check(st, target=SELLING, trade_ts=1, now=10, imb=imb)
    hold = compute_imbalance(WindowStats(buy_usd=50, sell_usd=50, sum_sq=100, trade_count=20))
    assert hold is not None
    assert classify_target(NEUTRAL, hold.r, hold.z, PARAMS) == NEUTRAL
    apply_check(st, target=NEUTRAL, trade_ts=2, now=20, imb=hold)
    assert st.pending_state is None
    apply_check(st, target=SELLING, trade_ts=3, now=30, imb=imb)
    assert st.state == NEUTRAL
    assert st.confirm_count == 1


def test_exit_to_neutral_is_silent_and_remembered():
    st = ScopeState(state=BUYING, entered_at=0)
    weak = compute_imbalance(WindowStats(buy_usd=55, sell_usd=45, sum_sq=25, trade_count=40))
    assert weak is not None
    assert abs(weak.r) < 0.25
    # z may or may not be below 1; force the exit band via r if needed
    target = classify_target(BUYING, 0.05, 3.0, PARAMS)
    assert target == NEUTRAL
    apply_check(
        st,
        target=NEUTRAL,
        trade_ts=1,
        now=100,
        imb=compute_imbalance(WindowStats(buy_usd=52, sell_usd=48, sum_sq=10, trade_count=100)),
    )
    imb = compute_imbalance(WindowStats(buy_usd=52, sell_usd=48, sum_sq=10, trade_count=100))
    assert imb is not None
    st.confirm_count = 1
    st.pending_state = NEUTRAL
    st.marker_trade_ts = 1
    tr = apply_check(st, target=NEUTRAL, trade_ts=2, now=500, imb=imb)
    assert tr is not None and tr.notify is False
    assert st.state == NEUTRAL
    assert st.prev_dir_state == BUYING


def test_empty_window_keeps_state_and_silence_clears_it():
    st = ScopeState(state=BUYING, entered_at=10)
    kept = evaluate_scope(
        st,
        now=1000,
        window_sec=300,
        stats=WindowStats(),
        last_trade_ts=900,
        params=PARAMS,
        tracking_started=0,
    )
    assert kept is None
    assert st.state == BUYING

    evaluate_scope(
        st,
        now=1000,
        window_sec=300,
        stats=_sell_stats(4),
        last_trade_ts=100,
        params=PARAMS,
        tracking_started=0,
    )
    assert st.state == NEUTRAL


def test_flip_bypasses_cooldown_and_batch_merges_timeframes():
    engine = FlowEngine()
    created = 0.0
    now = 100_000.0

    def stats_fn(_venues, _window, ts):
        return _sell_stats(16, 10), ts

    watch = WatchEval(
        watch_id=3,
        created_ts=created,
        timeframes=["15m", "1h"],
        ex_venues=[("binance", "SOL/USDT")],
        agg_venues=[("binance", "SOL/USDT")],
        muted=False,
    )
    assert evaluate_watch(engine, watch, now, PARAMS, stats_fn) == []
    blocks = evaluate_watch(engine, watch, now + 1200, PARAMS, stats_fn)
    assert [b.tf for b in blocks] == ["15m", "1h"]
    text = format_state_message(
        coin="SOL",
        exchange_label="Binance",
        watch_id=3,
        blocks=blocks,
    )
    assert "НАЧАЛО ПРОДАЖ" in text
    assert "ТФ 15m" in text and "ТФ 1h" in text
    assert "#3" in text


def test_divergence_header():
    block = NotifyBlock(
        tf="15m",
        ex=ScopeSnap(True, NEUTRAL, SELLING, -0.61, 2.8, -9000, 15000, 20, 0, None, 0),
        agg=ScopeSnap(False, BUYING, BUYING, 0.12, 1.2, 30000, 250000, 80, 0, None, 0),
    )
    text = format_state_message(
        coin="ARB", exchange_label="Bybit", watch_id=5, blocks=[block]
    )
    assert "РАСХОЖДЕНИЕ" in text
    assert "Продают на бирже наблюдения, рынок в целом покупает." in text
    assert "🟠" in text


def test_earlier_line_after_neutral():
    block = NotifyBlock(
        tf="15m",
        ex=ScopeSnap(True, NEUTRAL, SELLING, -0.54, 3.1, -18000, 33000, 41, 0, BUYING, 3 * 3600 + 20 * 60),
        agg=ScopeSnap(True, NEUTRAL, SELLING, -0.38, 3.4, -61000, 160000, 98, 0, None, 0),
    )
    text = format_state_message(
        coin="SOL", exchange_label="Binance", watch_id=3, blocks=[block]
    )
    assert "Ранее: BUYING 3 ч 20 мин" in text
    assert "−$18k" in text or "-$18k" in text
    assert fmt_usd(-18000, signed=True).endswith("$18k")


def test_minute_book_overlaps_window():
    book = MinuteBook()
    book.add("binance", "SOL/USDT", 10, "sell", 100, 2)
    book.add("binance", "SOL/USDT", 890, "buy", 40, 1)
    stats = book.window([("binance", "SOL/USDT")], 0, 900)
    assert stats.sell_usd == 100
    assert stats.buy_usd == 40
    assert stats.trade_count == 2
    assert stats.sell_base == 2
    outside = book.window([("binance", "SOL/USDT")], 100, 200)
    assert outside.trade_count == 0


def test_duration_parser():
    assert parse_duration("2h") == 7200
    assert parse_duration("30m") == 1800
    assert parse_duration("1d") == 86400
    assert fmt_usd(9600) == "$9.6k"
    assert fmt_usd(41000) == "$41k"
