from __future__ import annotations

from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ReplaySettings(BaseSettings):
    """Independent settings: live execution and fake-solver switches are never read."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    receiver_token: SecretStr = SecretStr("change-me-local-only")
    pokerai_api_key: SecretStr | None = None
    pokerai_base_url: str = "https://pokerai.bet"
    pokerai_timeout_seconds: float = Field(default=20, gt=0, le=120)
    preflop_version: str = "6max"
    flop_version: str = "6max"
    replay_allowed_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_network(self) -> "ReplaySettings":
        target = urlparse(self.pokerai_base_url)
        if (
            target.scheme != "https"
            or target.netloc != "pokerai.bet"
            or target.path not in ("", "/")
            or target.query
            or target.fragment
        ):
            raise ValueError("PokerAI API is allowlisted only for https://pokerai.bet")
        if self.pokerai_api_key and (
            len(self.receiver_token.get_secret_value()) < 24
            or self.receiver_token.get_secret_value()
            in {"change-me-local-only", "replace-with-a-long-random-value"}
        ):
            raise ValueError("set a private RECEIVER_TOKEN of at least 24 characters")
        for origin in self.replay_allowed_origins:
            parsed = urlparse(origin)
            local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (
                not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.scheme not in ({"http", "https"} if local else {"https"})
                or "*" in origin
            ):
                raise ValueError("REPLAY_ALLOWED_ORIGINS must contain explicit HTTPS origins")
        return self
