from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from app.bot.handlers import create_dispatcher, deliver, set_hub, setup_bot_commands
from app.config import get_app_config, get_settings
from app.db.models import Base
from app.db.session import get_engine, get_session_factory
from app.notify.consumer import AlertConsumer
from app.notify.publisher import AlertPublisher, create_redis
from app.notify.telegram_outbox import init_outbox
from app.services.runtime import RuntimeHub
from app.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)


def _make_bot(token: str, timeout: float = 35.0) -> Bot:
    return Bot(token=token, session=AiohttpSession(timeout=timeout))


async def init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    get_session_factory()
    logger.info("Database schema ensured")


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    config = get_app_config()

    await init_db()

    redis = await create_redis()
    publisher = AlertPublisher(redis)
    hub = RuntimeHub(config, publisher)
    set_hub(hub)
    await hub.start()

    tg_timeout = float(config.get("telegram", "request_timeout_sec", default=35))
    bot = _make_bot(settings.bot_token, timeout=tg_timeout)
    try:
        await asyncio.wait_for(setup_bot_commands(bot), timeout=tg_timeout)
    except Exception as exc:
        logger.warning("setup_bot_commands skipped: %s", exc)
    dp = create_dispatcher()

    outbox = init_outbox(
        min_interval_sec=float(config.get("telegram", "min_interval_sec", default=1.5)),
        max_queue=int(config.get("telegram", "max_queue", default=200)),
        send_timeout_sec=tg_timeout,
    )
    outbox.start()

    async def on_alert(alert: dict) -> None:
        await deliver(bot, settings.allowed_chat_id, alert)

    consumer = AlertConsumer(redis, on_alert)
    await consumer.start()
    logger.info("Starting Telegram polling; allowed_chat_id=%s", settings.allowed_chat_id)

    try:
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down…")
        await consumer.stop()
        await outbox.stop()
        await hub.stop()
        await bot.session.close()
        await redis.aclose()
        await get_engine().dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
