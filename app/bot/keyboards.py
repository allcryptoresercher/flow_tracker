from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.analytics.flow import ALERT_TFS


def exchanges_kb(venues: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=v["label"], callback_data=f"tr:ex:{v['exchange']}")]
        for v in venues
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="tr:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deposit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="tr:skip")],
            [InlineKeyboardButton(text="Отмена", callback_data="tr:cancel")],
        ]
    )


def options_kb(pushover: bool, mode: str) -> InlineKeyboardMarkup:
    po = "Pushover: вкл" if pushover else "Pushover: выкл"
    md = "Режим: расширенный" if mode == "full" else "Режим: тихий"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=po, callback_data="tr:po")],
            [InlineKeyboardButton(text=md, callback_data="tr:mode")],
            [
                InlineKeyboardButton(text="Запустить", callback_data="tr:go"),
                InlineKeyboardButton(text="Отмена", callback_data="tr:cancel"),
            ],
        ]
    )


def watch_kb(watch_id: int, pushover: bool, mode: str) -> InlineKeyboardMarkup:
    po = "Pushover: вкл" if pushover else "Pushover: выкл"
    md = "Режим: расширенный" if mode == "full" else "Режим: тихий"
    i = str(watch_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=po, callback_data=f"w:po:{i}"),
                InlineKeyboardButton(text=md, callback_data=f"w:mode:{i}"),
            ],
            [
                InlineKeyboardButton(text="Депозит", callback_data=f"w:dep:{i}"),
                InlineKeyboardButton(text="ТФ", callback_data=f"w:tf:{i}"),
            ],
            [InlineKeyboardButton(text="Удалить", callback_data=f"w:del:{i}")],
        ]
    )


def tf_kb(watch_id: int, selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for tf in ALERT_TFS:
        mark = "✓ " if tf in selected else ""
        rows.append(
            [InlineKeyboardButton(text=f"{mark}{tf}", callback_data=f"tf:{watch_id}:{tf}")]
        )
    rows.append([InlineKeyboardButton(text="Готово", callback_data=f"tf:{watch_id}:ok")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
