from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis

from app.notify.publisher import CHANNEL_ALERTS
from app.utils.logging import get_logger

logger = get_logger(__name__)

AlertHandler = Callable[[dict[str, Any]], Awaitable[None]]


class AlertConsumer:
    def __init__(self, redis_client: redis.Redis, handler: AlertHandler) -> None:
        self._redis = redis_client
        self._handler = handler
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="alert-consumer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(CHANNEL_ALERTS)
        logger.info("Subscribed to Redis channel %s", CHANNEL_ALERTS)
        try:
            async for message in pubsub.listen():
                if message is None or message.get("type") != "message":
                    continue
                raw = message.get("data")
                try:
                    alert = json.loads(raw)
                except Exception:
                    logger.exception("Bad alert payload")
                    continue
                key = alert.get("idempotency_key")
                if key:
                    try:
                        fresh = await self._redis.set(
                            f"flow:sent:{key}", "1", nx=True, ex=7 * 24 * 3600
                        )
                    except Exception:
                        logger.exception("Idempotency check failed")
                        fresh = True
                    if not fresh:
                        logger.info("Skip duplicate alert %s", key)
                        continue
                try:
                    await self._handler(alert)
                except Exception:
                    logger.exception("Failed to handle alert")
        finally:
            await pubsub.unsubscribe(CHANNEL_ALERTS)
            await pubsub.aclose()
