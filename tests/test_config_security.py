from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.config import BOT_ONLY_CONFIRMATION, Settings


def test_example_receiver_placeholder_is_rejected_for_external_integrations(tmp_path):
    with pytest.raises(ValidationError, match="private RECEIVER_TOKEN"):
        Settings(
            database_path=tmp_path / "bridge.db",
            telegram_enabled=True,
            telegram_bot_token=SecretStr("123:test"),
            telegram_chat_id="1",
            receiver_token=SecretStr("replace-with-a-long-random-value"),
        )


def test_example_sampling_placeholder_is_rejected_for_pokerai(tmp_path):
    with pytest.raises(ValidationError, match="private SAMPLING_SECRET"):
        Settings(
            database_path=tmp_path / "bridge.db",
            receiver_token=SecretStr("a-private-receiver-token"),
            solver_mode="pokerai",
            pokerai_api_key=SecretStr("gto_test"),
            sampling_secret=SecretStr("replace-with-another-long-random-value"),
        )


def test_auto_execute_requires_explicit_bot_only_ack(tmp_path):
    with pytest.raises(ValidationError, match="BOT_ONLY_ACK"):
        Settings(
            database_path=tmp_path / "bridge.db",
            receiver_token=SecretStr("a-private-receiver-token"),
            sampling_secret=SecretStr("a-private-sampling-secret"),
            solver_mode="pokerai",
            pokerai_api_key=SecretStr("gto_test"),
            openpoker_enabled=True,
            openpoker_api_key=SecretStr("op_test"),
            auto_execute=True,
        )


def test_auto_execute_accepts_exact_bot_only_ack(tmp_path):
    settings = Settings(
        database_path=tmp_path / "bridge.db",
        receiver_token=SecretStr("a-private-receiver-token"),
        sampling_secret=SecretStr("a-private-sampling-secret"),
        solver_mode="pokerai",
        pokerai_api_key=SecretStr("gto_test"),
        openpoker_enabled=True,
        openpoker_api_key=SecretStr("op_test"),
        auto_execute=True,
        bot_only_ack=BOT_ONLY_CONFIRMATION,
    )
    assert settings.auto_execute is True
