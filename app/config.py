from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.analytics.flow import FlowParams


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(alias="BOT_TOKEN")
    allowed_chat_id: int = Field(alias="ALLOWED_CHAT_ID")
    pushover_api_token: str | None = Field(default=None, alias="PUSHOVER_API_TOKEN")
    pushover_user_key: str | None = Field(default=None, alias="PUSHOVER_USER_KEY")
    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    config_path: str = Field(default="config.yaml", alias="CONFIG_PATH")
    tz: str = Field(default="Europe/Vienna", alias="TZ")

    @property
    def pushover_configured(self) -> bool:
        return bool(self.pushover_api_token and self.pushover_user_key)


def load_yaml_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


class AppConfig:
    """YAML config. Mount the file into the container and restart to apply."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data = load_yaml_config(self.path)

    def reload(self) -> None:
        self._data = load_yaml_config(self.path)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self._data
        for key in keys:
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return cur

    def coin_get(self, coin: str, *keys: str, default: Any = None) -> Any:
        cur: Any = self.get("coins", coin.upper())
        if not isinstance(cur, dict):
            return default
        for key in keys:
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return cur

    def _coin_or_global(self, coin: str, section: str, name: str, default: Any) -> Any:
        override = self.coin_get(coin, name, default=None)
        if override is None:
            override = self.coin_get(coin, section, name, default=None)
        if override is not None:
            return override
        return self.get(section, name, default=default)

    def flow_params(self, coin: str) -> FlowParams:
        return FlowParams(
            r_enter=float(self._coin_or_global(coin, "flow", "r_enter", 0.25)),
            r_exit=float(self._coin_or_global(coin, "flow", "r_exit", 0.10)),
            z_min=float(self._coin_or_global(coin, "flow", "z_min", 2.0)),
            z_exit=float(self._coin_or_global(coin, "flow", "z_exit", 1.0)),
            confirmations=int(self._coin_or_global(coin, "flow", "confirmations", 2)),
            alert_cooldown_sec=float(self._coin_or_global(coin, "flow", "alert_cooldown_sec", 900)),
        )

    def default_timeframes(self) -> list[str]:
        raw = self.get("flow", "default_timeframes", default=["15m", "1h", "4h"])
        return [str(x) for x in raw]


_app_config: AppConfig | None = None


def get_app_config() -> AppConfig:
    global _app_config
    if _app_config is None:
        settings = get_settings()
        _app_config = AppConfig(settings.config_path)
    return _app_config
