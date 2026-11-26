"""Numbers, durations and timestamps for Telegram texts."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _compact(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    return f"{value:.1f}"


def fmt_usd(value: float, *, signed: bool = False) -> str:
    sign = ""
    if signed:
        if value > 0:
            sign = "+"
        elif value < 0:
            sign = "−"
    av = abs(value)
    if av >= 1_000_000:
        body = f"${_compact(av / 1_000_000)}M"
    elif av >= 1_000:
        body = f"${_compact(av / 1_000)}k"
    elif av >= 100:
        body = f"${av:.0f}"
    elif av >= 10:
        body = f"${av:.1f}"
    else:
        body = f"${av:.2f}"
    return sign + body


def fmt_r(value: float) -> str:
    if value > 0:
        return f"+{value:.2f}"
    if value < 0:
        return f"−{abs(value):.2f}"
    return "0.00"


def fmt_z(value: float) -> str:
    return f"{value:.1f}"


def fmt_price(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def fmt_tokens(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value)):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ")


def fmt_pct(value: float) -> str:
    return f"{value:.1f}".replace(".", ",") + "%"


def plural_trades(count: int) -> str:
    n = abs(int(count))
    n10 = n % 10
    n100 = n % 100
    if n10 == 1 and n100 != 11:
        word = "сделка"
    elif 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        word = "сделки"
    else:
        word = "сделок"
    return f"{n} {word}"


def _days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def fmt_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{_days(days)} {hours} ч" if hours else _days(days)
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    return f"{minutes} мин"


def display_tz(name: str = "Europe/Vienna") -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/Vienna")


def fmt_clock(dt: datetime, tz_name: str = "Europe/Vienna") -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(display_tz(tz_name)).strftime("%H:%M")


def parse_amount(text: str) -> float:
    cleaned = text.strip().replace(" ", "").replace(",", ".")
    value = float(cleaned)
    if value < 0:
        raise ValueError("negative")
    return value


def parse_duration(text: str) -> int | None:
    """Return seconds for '2h', '30m', '90min', '1d'. None if the text is not a duration."""
    raw = text.strip().lower().replace(" ", "")
    units = {
        "m": 60,
        "min": 60,
        "h": 3600,
        "hr": 3600,
        "d": 86400,
        "м": 60,
        "мин": 60,
        "ч": 3600,
        "д": 86400,
    }
    for suffix, mult in sorted(units.items(), key=lambda item: -len(item[0])):
        if raw.endswith(suffix) and raw[: -len(suffix)].isdigit():
            return int(raw[: -len(suffix)]) * mult
    return None
