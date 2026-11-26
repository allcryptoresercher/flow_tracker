"""Telegram texts. One event is one message, kept short on purpose."""

from __future__ import annotations

from app.analytics.flow import (
    BUYING,
    NEUTRAL,
    SELLING,
    NotifyBlock,
    ScopeSnap,
    primary_kind,
)
from app.utils.fmt import (
    fmt_duration,
    fmt_pct,
    fmt_price,
    fmt_r,
    fmt_tokens,
    fmt_usd,
    fmt_z,
    plural_trades,
)

_TITLES = {
    "start_sell": "НАЧАЛО ПРОДАЖ",
    "flip_sell": "РАЗВОРОТ: ПРОДАЖИ",
    "start_buy": "НАЧАЛО ПОКУПОК",
    "flip_buy": "РАЗВОРОТ: ПОКУПКИ",
}
_EMOJI = {
    "start_sell": "🔴",
    "flip_sell": "🔴",
    "start_buy": "🟢",
    "flip_buy": "🟢",
}
_DIR_NOUN = {BUYING: "покупки", SELLING: "продажи"}


def _diverged(block: NotifyBlock) -> bool:
    return block.ex.new != block.agg.new


def _flow_line(label: str, snap: ScopeSnap, *, with_trades: bool) -> str:
    parts = [
        f"r = {fmt_r(snap.r)}",
        f"z = {fmt_z(snap.z)}",
        f"Δ {fmt_usd(snap.delta_usd, signed=True)}",
        f"V {fmt_usd(snap.vol_usd)}",
    ]
    if with_trades:
        parts.append(plural_trades(snap.trades))
    return f"  {label}: " + " · ".join(parts)


def _arrow(snap: ScopeSnap) -> str:
    if snap.changed and snap.old != snap.new:
        text = f"{snap.old} → {snap.new}"
        if snap.old in (BUYING, SELLING) and snap.new in (BUYING, SELLING):
            noun = _DIR_NOUN.get(snap.old, "")
            if noun and snap.held_sec > 0:
                text += f" ({noun} шли {fmt_duration(snap.held_sec)})"
        return text
    return snap.new


def _divergence_sentence(ex_state: str, agg_state: str) -> str:
    if ex_state == SELLING and agg_state == BUYING:
        return "Продают на бирже наблюдения, рынок в целом покупает."
    if ex_state == BUYING and agg_state == SELLING:
        return "Покупают на бирже наблюдения, рынок в целом продаёт."
    if ex_state == SELLING:
        return "Продают на бирже наблюдения, сводный поток в другом состоянии."
    if ex_state == BUYING:
        return "Покупают на бирже наблюдения, сводный поток в другом состоянии."
    return "Состояние биржи наблюдения и сводного потока различается."


def _earlier_line(blocks: list[NotifyBlock]) -> str | None:
    for block in blocks:
        for snap in (block.ex, block.agg):
            if snap.changed and snap.old == NEUTRAL and snap.prev_dir_state in (BUYING, SELLING):
                return f"Ранее: {snap.prev_dir_state} {fmt_duration(snap.prev_dir_sec)}"
    return None


def format_state_message(
    *,
    coin: str,
    exchange_label: str,
    watch_id: int,
    blocks: list[NotifyBlock],
    deposit_line: str | None = None,
    big_line: str | None = None,
) -> str:
    kind = primary_kind(blocks) or "start_sell"
    diverged = any(_diverged(b) for b in blocks)
    emoji = "🟠" if diverged else _EMOJI[kind]
    title = _TITLES[kind]
    head = f"{emoji} {title} · {coin} · {exchange_label} · #{watch_id}"
    if diverged:
        head += " · РАСХОЖДЕНИЕ"
    lines = [head, ""]

    for block in blocks:
        if _diverged(block):
            lines.append(f"ТФ {block.tf}:")
            lines.append(
                f"  {exchange_label} (наблюдение): {_arrow(block.ex)} · "
                f"r = {fmt_r(block.ex.r)} · z = {fmt_z(block.ex.z)} · "
                f"Δ {fmt_usd(block.ex.delta_usd, signed=True)}"
            )
            lines.append(
                f"  Сводно:             {_arrow(block.agg)} · "
                f"r = {fmt_r(block.agg.r)} · "
                f"Δ {fmt_usd(block.agg.delta_usd, signed=True)}"
            )
            lines.append(_divergence_sentence(block.ex.new, block.agg.new))
        else:
            driver = block.ex if block.ex.changed else block.agg
            lines.append(f"ТФ {block.tf}: {_arrow(driver)}")
            lines.append(_flow_line(exchange_label, block.ex, with_trades=True))
            lines.append(_flow_line("Сводно", block.agg, with_trades=True))

    earlier = _earlier_line(blocks)
    if earlier:
        lines.append(earlier)
    if deposit_line:
        lines.append(deposit_line)
    if big_line:
        lines.append(big_line)
    return "\n".join(lines).rstrip()


def format_deposit_line(
    *,
    deposit: float,
    sold: float,
    coin: str,
    exchange_label: str,
) -> str:
    pct = (sold / deposit * 100.0) if deposit else 0.0
    return (
        f"Депозит: {fmt_tokens(deposit)} {coin} · "
        f"продано на {exchange_label} с момента добавления: "
        f"{fmt_tokens(sold)} {coin} ({fmt_pct(pct)}, оценка)"
    )


def format_big_summary(summary: dict[str, float]) -> str | None:
    sell_n = int(summary.get("sell_n", 0))
    buy_n = int(summary.get("buy_n", 0))
    if sell_n == 0 and buy_n == 0:
        return None
    return (
        f"Крупные за 30 мин: {sell_n} sell на {fmt_usd(summary.get('sell_usd', 0))} · "
        f"{buy_n} buy на {fmt_usd(summary.get('buy_usd', 0))}"
    )


def format_big_trade(
    *,
    side: str,
    cost: float,
    symbol: str,
    exchange_label: str,
    watch_id: int,
    price: float,
    amount: float,
    trades: int,
    threshold: float,
    avg: float,
    mode: str,
    k: float,
    floor: float,
) -> str:
    emoji = "🔴" if side == "sell" else "🟢"
    side_txt = "SELL" if side == "sell" else "BUY"
    if mode == "abs":
        how = "задано"
    elif mode == "floor":
        how = f"пол {fmt_usd(floor)}, сделок меньше 10"
    else:
        how = f"среднее {fmt_usd(avg)} × {k:g}"
    return (
        f"{emoji} {side_txt} {fmt_usd(cost)} · {symbol} · {exchange_label} · #{watch_id}\n"
        f"Цена {fmt_price(price)} · {fmt_tokens(amount)} {symbol.split('/')[0]} · "
        f"склеено {plural_trades(trades)} за 1.5 с\n"
        f"Порог: {fmt_usd(threshold)} ({how})"
    )


def format_pushover(coin: str, exchange_label: str, block: NotifyBlock) -> tuple[str, str]:
    snap = block.ex if block.ex.changed and block.ex.new == "SELLING" else block.agg
    title = f"{coin} · {exchange_label} · НАЧАЛО ПРОДАЖ"
    text = (
        f"{block.tf} {snap.old} → {snap.new} · r {fmt_r(snap.r)} · "
        f"Δ {fmt_usd(block.ex.delta_usd, signed=True)} "
        f"(сводно {fmt_usd(block.agg.delta_usd, signed=True)})"
    )
    return title, text


def format_started(
    *,
    watch_id: int,
    coin: str,
    exchange_label: str,
    timeframes: list[str],
    mode: str,
    pushover: bool,
    venues: list[str],
) -> str:
    mode_txt = "расширенный" if mode == "full" else "тихий"
    po = "вкл" if pushover else "выкл"
    tfs = " ".join(timeframes)
    lines = [
        f"▶ Наблюдение #{watch_id} запущено · {coin} · {exchange_label} · "
        f"ТФ {tfs} · режим {mode_txt} · Pushover {po}"
    ]
    if venues:
        lines.append("Сводно: " + ", ".join(venues))
    return "\n".join(lines)


def format_deleted(
    *,
    watch_id: int,
    coin: str,
    exchange_label: str,
    worked_sec: float,
    changes: int,
    delta_ex: float,
    delta_agg: float,
    realization: float | None,
) -> str:
    lines = [
        f"⏹ НАБЛЮДЕНИЕ #{watch_id} УДАЛЕНО · {coin} · {exchange_label}",
        f"Работало {fmt_duration(worked_sec)} · смен состояния: {changes}",
        f"Дельта с момента добавления: {exchange_label} {fmt_usd(delta_ex, signed=True)} · "
        f"сводно {fmt_usd(delta_agg, signed=True)}",
    ]
    if realization is not None:
        lines.append(f"Реализация (оценка): {fmt_pct(realization)} депозита")
    return "\n".join(lines)


def format_stall(watch_ids: list[int], exchange_label: str, minutes: int) -> str:
    ids = " ".join(f"#{i}" for i in watch_ids)
    return f"⚠ {ids}: нет данных {exchange_label} {minutes} мин, состояние не обновляется"


def format_restored(watch_ids: list[int], exchange_label: str) -> str:
    ids = " ".join(f"#{i}" for i in watch_ids)
    return f"✅ {ids}: данные {exchange_label} восстановлены"


def format_watch_card(
    *,
    watch_id: int,
    coin: str,
    exchange_label: str,
    mode: str,
    pushover: bool,
    timeframes: list[str],
    lines: list[str],
    muted_txt: str | None = None,
) -> str:
    mode_txt = "расширенный" if mode == "full" else "тихий"
    po = "вкл" if pushover else "выкл"
    head = (
        f"#{watch_id} · {coin} · {exchange_label} · {mode_txt} · Pushover {po}\n"
        f"ТФ {' '.join(timeframes)}"
    )
    if muted_txt:
        head += f" · {muted_txt}"
    body = "\n".join(lines)
    return f"{head}\n{body}" if body else head


def format_status(
    *,
    watch_id: int,
    coin: str,
    exchange_label: str,
    mode: str,
    pushover: bool,
    timeframes: list[str],
    lines: list[str],
    last_txt: str,
    venues: list[str],
    realization: str | None,
    muted_txt: str | None = None,
) -> str:
    card = format_watch_card(
        watch_id=watch_id,
        coin=coin,
        exchange_label=exchange_label,
        mode=mode,
        pushover=pushover,
        timeframes=timeframes,
        lines=lines,
        muted_txt=muted_txt,
    )
    extra = [f"Последняя сделка: {last_txt}"]
    if venues:
        extra.append("Сводно: " + ", ".join(venues))
    if realization:
        extra.append(realization)
    return card + "\n" + "\n".join(extra)


HELP = """\
Flow Tracker следит за спотовым потоком и пишет, когда режим меняется: начались продажи или покупки.

/track — добавить монету и биржу
/watches — список и кнопки (Pushover, режим, депозит, ТФ, удалить)
/status <id> — r, z, состояния, последняя сделка
/untrack <id> — удалить и получить сводку
/tf <id> — набор таймфреймов (по умолчанию 15m 1h 4h)
/mute <id> <время> — заглушить, например /mute 3 2h
/help — эта справка

Тихий режим смотрит только смену потока. Расширенный добавляет крупные сделки и, если задан депозит, оценку реализации.

Дельта видит только агрессивные сделки: продажа лимитным ордером или айсбергом проявляется с задержкой. Для долгой раздачи оставляйте 4h. На тонких монетах окна 5m и 15m часто пустые — надёжнее 1h и 4h.

Реализация — это taker-sell на бирже наблюдения с момента добавления, делённый на размер депозита. Это оценка, не факт продажи именно этого депозита.\
"""
