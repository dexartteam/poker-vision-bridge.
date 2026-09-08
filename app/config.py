from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BOT_ONLY_CONFIRMATION = "I_UNDERSTAND_BOT_ONLY"
INSECURE_RECEIVER_TOKENS = {
    "change-me-local-only",
    "replace-with-a-long-random-value",
}
INSECURE_SAMPLING_SECRETS = {
    "change-me-sampling-secret",
    "replace-with-another-long-random-value",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    receiver_token: SecretStr = SecretStr("change-me-local-only")
    database_path: Path = Path("./data/bridge.db")
    log_level: str = "INFO"
    min_vision_confidence: float = Field(default=0.95, ge=0, le=1)
    min_stable_frames: int = Field(default=2, ge=1, le=20)
    max_observation_age_seconds: float = Field(default=5, gt=0, le=300)
    max_future_clock_skew_seconds: float = Field(default=2, ge=0, le=60)
    decision_source: Literal["vision", "platform"] = "vision"

    solver_mode: Literal["fake", "pokerai"] = "fake"
    pokerai_api_key: SecretStr | None = None
    pokerai_base_url: str = "https://pokerai.bet"
    pokerai_timeout_seconds: float = Field(default=10, gt=0, le=120)
    preflop_version: str = "6max"
    flop_version: str = "6max"
    sampling_secret: SecretStr = SecretStr("change-me-sampling-secret")

    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    openpoker_enabled: bool = False
    openpoker_api_key: SecretStr | None = None
    openpoker_ws_url: str = "wss://openpoker.ai/ws"
    openpoker_rest_url: str = "https://api.openpoker.ai/api"
    openpoker_buy_in: int = Field(default=2000, ge=1000, le=5000)
    openpoker_auto_rebuy: bool = True
    auto_execute: bool = False
    bot_only_ack: str | None = None

    @model_validator(mode="after")
    def validate_integrations(self) -> "Settings":
        if self.solver_mode == "pokerai" and not self.pokerai_api_key:
            raise ValueError("POKERAI_API_KEY is required when SOLVER_MODE=pokerai")
        if (
            self.solver_mode == "pokerai"
            and self.sampling_secret.get_secret_value() in INSECURE_SAMPLING_SECRETS
        ):
            raise ValueError("set a private SAMPLING_SECRET before using PokerAI")
        if self.solver_mode == "pokerai":
            pokerai = urlparse(self.pokerai_base_url)
            if pokerai.scheme != "https" or pokerai.hostname != "pokerai.bet":
                raise ValueError("PokerAI API is allowlisted only for https://pokerai.bet")
        if self.telegram_enabled and (not self.telegram_bot_token or not self.telegram_chat_id):
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
        if self.openpoker_enabled and not self.openpoker_api_key:
            raise ValueError("OPENPOKER_API_KEY is required when OPENPOKER_ENABLED=true")
        if self.openpoker_enabled:
            self._require_openpoker_hosts()
        if (
            self.solver_mode == "pokerai" or self.telegram_enabled or self.openpoker_enabled
        ) and self.receiver_token.get_secret_value() in INSECURE_RECEIVER_TOKENS:
            raise ValueError("set a private RECEIVER_TOKEN before enabling external integrations")
        if self.auto_execute:
            if self.solver_mode != "pokerai":
                raise ValueError("AUTO_EXECUTE requires SOLVER_MODE=pokerai")
            if not self.openpoker_enabled:
                raise ValueError("AUTO_EXECUTE requires OPENPOKER_ENABLED=true")
            if self.bot_only_ack != BOT_ONLY_CONFIRMATION:
                raise ValueError(
                    f"AUTO_EXECUTE requires BOT_ONLY_ACK={BOT_ONLY_CONFIRMATION}"
                )
        return self

    def _require_openpoker_hosts(self) -> None:
        ws = urlparse(self.openpoker_ws_url)
        rest = urlparse(self.openpoker_rest_url)
        if ws.scheme != "wss" or ws.hostname != "openpoker.ai":
            raise ValueError("Open Poker WebSocket is allowlisted only for wss://openpoker.ai")
        if rest.scheme != "https" or rest.hostname != "api.openpoker.ai":
            raise ValueError("Open Poker REST URL must use https://api.openpoker.ai")

    def ensure_directories(self) -> None:
        self.database_path.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
