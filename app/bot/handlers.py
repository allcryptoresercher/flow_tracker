from __future__ import annotations

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, CallbackQuery, Message, TelegramObject
from aiogram import BaseMiddleware

from app.bot.formatters import HELP
from app.bot.keyboards import deposit_kb, exchanges_kb, options_kb, tf_kb, watch_kb
from app.bot.states import EditSG, TrackSG
from app.config import get_settings
from app.notify.pushover import send_pushover
from app.notify.telegram_outbox import get_outbox
from app.utils.fmt import parse_amount, parse_duration
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = Router()
_hub = None


def set_hub(hub) -> None:
    global _hub
    _hub = hub


def get_hub():
    if _hub is None:
        raise RuntimeError("Runtime hub is not ready")
    return _hub


class AllowMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        settings = get_settings()
        chat = getattr(event, "chat", None)
        if chat is None:
            message = getattr(event, "message", None)
            chat = getattr(message, "chat", None)
        if chat is None or chat.id != settings.allowed_chat_id:
            if isinstance(event, CallbackQuery):
                await event.answer()
            return None
        return await handler(event, data)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(AllowMiddleware())
    dp.callback_query.middleware(AllowMiddleware())
    dp.include_router(router)
    return dp


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="track", description="Добавить наблюдение"),
            BotCommand(command="watches", description="Список наблюдений"),
            BotCommand(command="status", description="Состояние наблюдения"),
            BotCommand(command="untrack", description="Удалить наблюдение"),
            BotCommand(command="tf", description="Таймфреймы наблюдения"),
            BotCommand(command="mute", description="Заглушить наблюдение"),
            BotCommand(command="help", description="Справка"),
        ]
    )


async def deliver(bot: Bot, chat_id: int, alert: dict) -> None:
    if alert.get("type") == "pushover":
        await send_pushover(alert.get("title") or "", alert.get("text") or "", priority=1)
        return
    text = alert.get("text") or ""
    if not text:
        return
    await get_outbox().send(
        bot,
        chat_id,
        text,
        priority=int(alert.get("priority", 1)),
        disable_notification=bool(alert.get("silent", True)),
        drop_if_blocked=False,
    )


def _id_arg(text: str | None) -> int:
    if not text or not text.strip().lstrip("#").isdigit():
        raise ValueError("Укажите номер наблюдения, например /status 3")
    return int(text.strip().lstrip("#").split()[0])


@router.message(Command("start"))
@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Диалог отменён.")


@router.message(Command("track"))
async def cmd_track(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(TrackSG.coin)
    await message.answer("Тикер монеты, например SOL.\n/cancel — прервать.")


@router.message(TrackSG.coin, F.text)
async def on_coin(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().upper()
    if not raw or raw.startswith("/"):
        return
    venues = get_hub().find_venues(raw)
    if not venues:
        await message.answer("Спотовая пара не найдена на включённых биржах. Другой тикер?")
        return
    await state.update_data(coin=raw, venues=venues, deposit=None, pushover=False, mode="quiet")
    await state.set_state(TrackSG.exchange)
    names = ", ".join(v["label"] for v in venues)
    await message.answer(
        f"{raw}: выберите биржу наблюдения.\nОстальные пойдут в сводный поток: {names}",
        reply_markup=exchanges_kb(venues),
    )


@router.callback_query(F.data == "tr:cancel")
async def cb_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    if query.message:
        await query.message.answer("Диалог отменён.")


@router.callback_query(TrackSG.exchange, F.data.startswith("tr:ex:"))
async def cb_exchange(query: CallbackQuery, state: FSMContext) -> None:
    exchange = (query.data or "").split(":", 2)[2]
    data = await state.get_data()
    venues = data.get("venues") or []
    if not any(v["exchange"] == exchange for v in venues):
        await query.answer("Биржа не из списка", show_alert=True)
        return
    await state.update_data(exchange=exchange)
    await state.set_state(TrackSG.deposit)
    await query.answer()
    if query.message:
        await query.message.answer(
            "Размер депозита в токенах (необязательно). Нужен для оценки реализации в расширенном режиме.",
            reply_markup=deposit_kb(),
        )


@router.callback_query(TrackSG.deposit, F.data == "tr:skip")
async def cb_skip_deposit(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(deposit=None)
    await state.set_state(TrackSG.options)
    await query.answer()
    await _show_options(query, state)


@router.message(TrackSG.deposit, F.text)
async def on_deposit(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        return
    try:
        value = parse_amount(raw)
    except ValueError:
        await message.answer("Нужно число токенов, или нажмите «Пропустить».")
        return
    if value == 0:
        value = None
    await state.update_data(deposit=value)
    await state.set_state(TrackSG.options)
    data = await state.get_data()
    await message.answer(_options_text(data), reply_markup=options_kb(False, "quiet"))


def _options_text(data: dict) -> str:
    dep = data.get("deposit")
    dep_txt = "не задан" if not dep else str(dep)
    return (
        f"{data.get('coin')} · {data.get('exchange')}\n"
        f"Депозит: {dep_txt}\n"
        "Pushover — алерт на устройство при начале продаж.\n"
        "Тихий режим следит за сменой потока. Расширенный добавляет крупные сделки и реализацию."
    )


async def _show_options(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if query.message:
        await query.message.answer(
            _options_text(data),
            reply_markup=options_kb(bool(data.get("pushover")), data.get("mode") or "quiet"),
        )


@router.callback_query(TrackSG.options, F.data == "tr:po")
async def cb_po(query: CallbackQuery, state: FSMContext) -> None:
    settings = get_settings()
    data = await state.get_data()
    if not data.get("pushover") and not settings.pushover_configured:
        await query.answer("Ключи Pushover не заданы в .env", show_alert=True)
        return
    data["pushover"] = not bool(data.get("pushover"))
    await state.update_data(pushover=data["pushover"])
    await query.answer()
    if query.message:
        await query.message.edit_reply_markup(
            reply_markup=options_kb(data["pushover"], data.get("mode") or "quiet")
        )


@router.callback_query(TrackSG.options, F.data == "tr:mode")
async def cb_mode(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    mode = "full" if data.get("mode") != "full" else "quiet"
    await state.update_data(mode=mode)
    await query.answer()
    if query.message:
        await query.message.edit_reply_markup(
            reply_markup=options_kb(bool(data.get("pushover")), mode)
        )


@router.callback_query(TrackSG.options, F.data == "tr:go")
async def cb_go(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await query.answer()
    try:
        text = await get_hub().create_watch(
            coin=data["coin"],
            exchange=data["exchange"],
            deposit=data.get("deposit"),
            mode=data.get("mode") or "quiet",
            pushover=bool(data.get("pushover")),
            venues=data.get("venues") or [],
        )
    except Exception as exc:
        if query.message:
            await query.message.answer(str(exc))
        return
    await state.clear()
    if query.message:
        await query.message.answer(text)


@router.message(Command("watches"))
async def cmd_watches(message: Message, state: FSMContext) -> None:
    await state.clear()
    cards = get_hub().list_cards()
    if not cards:
        await message.answer("Наблюдений нет. Добавьте командой /track")
        return
    for watch_id, text, pushover, mode in cards:
        await message.answer(text, reply_markup=watch_kb(watch_id, pushover, mode))


@router.message(Command("status"))
async def cmd_status(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    try:
        watch_id = _id_arg(command.args)
        text = get_hub().status_text(watch_id)
    except Exception as exc:
        await message.answer(str(exc))
        return
    await message.answer(text)


@router.message(Command("untrack"))
async def cmd_untrack(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    try:
        watch_id = _id_arg(command.args)
        text = await get_hub().delete_watch(watch_id)
    except Exception as exc:
        await message.answer(str(exc))
        return
    await message.answer(text)


@router.message(Command("tf"))
async def cmd_tf(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    try:
        watch_id = _id_arg(command.args)
        selected = get_hub().timeframes_of(watch_id)
    except Exception as exc:
        await message.answer(str(exc))
        return
    await message.answer(
        f"Таймфреймы #{watch_id}. 12h и 24h всегда только в /status.",
        reply_markup=tf_kb(watch_id, selected),
    )


@router.message(Command("mute"))
async def cmd_mute(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    parts = (command.args or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /mute <id> <время>, например /mute 3 2h")
        return
    try:
        watch_id = _id_arg(parts[0])
        seconds = parse_duration(parts[1])
        if not seconds:
            raise ValueError("Не понял длительность. Пример: 30m, 2h, 1d")
        text = await get_hub().mute(watch_id, seconds)
    except Exception as exc:
        await message.answer(str(exc))
        return
    await message.answer(text)


@router.callback_query(F.data.startswith("w:po:"))
async def cb_watch_po(query: CallbackQuery) -> None:
    watch_id = int((query.data or "").split(":")[2])
    try:
        text = await get_hub().toggle_pushover(watch_id)
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer(text)
    await _refresh_watch_kb(query, watch_id)


@router.callback_query(F.data.startswith("w:mode:"))
async def cb_watch_mode(query: CallbackQuery) -> None:
    watch_id = int((query.data or "").split(":")[2])
    try:
        text = await get_hub().toggle_mode(watch_id)
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer(text)
    await _refresh_watch_kb(query, watch_id)


@router.callback_query(F.data.startswith("w:dep:"))
async def cb_watch_dep(query: CallbackQuery, state: FSMContext) -> None:
    watch_id = int((query.data or "").split(":")[2])
    await state.set_state(EditSG.deposit)
    await state.update_data(watch_id=watch_id)
    await query.answer()
    if query.message:
        await query.message.answer(
            f"Депозит для #{watch_id}: число токенов или «сброс»."
        )


@router.message(EditSG.deposit, F.text)
async def on_edit_deposit(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        return
    data = await state.get_data()
    watch_id = int(data["watch_id"])
    if raw.lower() in {"сброс", "нет", "-", "очистить"}:
        value = None
    else:
        try:
            value = parse_amount(raw)
        except ValueError:
            await message.answer("Нужно число или «сброс».")
            return
        if value == 0:
            value = None
    try:
        text = await get_hub().set_deposit(watch_id, value)
    except Exception as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer(text)


@router.callback_query(F.data.startswith("w:tf:"))
async def cb_watch_tf(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    watch_id = int((query.data or "").split(":")[2])
    try:
        selected = get_hub().timeframes_of(watch_id)
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer()
    if query.message:
        await query.message.answer(
            f"Таймфреймы #{watch_id}",
            reply_markup=tf_kb(watch_id, selected),
        )


@router.callback_query(F.data.startswith("w:del:"))
async def cb_watch_del(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    watch_id = int((query.data or "").split(":")[2])
    try:
        text = await get_hub().delete_watch(watch_id)
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer("Удалено")
    if query.message:
        await query.message.answer(text)


@router.callback_query(F.data.startswith("tf:"))
async def cb_tf(query: CallbackQuery) -> None:
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer()
        return
    watch_id = int(parts[1])
    action = parts[2]
    if action == "ok":
        await query.answer("Сохранено")
        return
    try:
        selected = await get_hub().toggle_timeframe(watch_id, action)
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer()
    if query.message:
        await query.message.edit_reply_markup(reply_markup=tf_kb(watch_id, selected))


async def _refresh_watch_kb(query: CallbackQuery, watch_id: int) -> None:
    watch = get_hub().watches.get(watch_id)
    if watch is None or query.message is None:
        return
    try:
        await query.message.edit_reply_markup(
            reply_markup=watch_kb(watch.id, watch.pushover, watch.mode)
        )
    except Exception:
        logger.debug("keyboard refresh skipped")
