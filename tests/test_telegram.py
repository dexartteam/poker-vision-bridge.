from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.models import DecisionView, utc_now
from app.telegram import TelegramNotifier


async def test_telegram_http_error_never_exposes_bot_token(event_factory, tmp_path):
    token = "123456:super-secret-token"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert token in str(request.url)
        return httpx.Response(500, request=request, json={"ok": False})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        database_path=tmp_path / "bridge.db",
        receiver_token=SecretStr("private-receiver-token"),
        telegram_enabled=True,
        telegram_bot_token=SecretStr(token),
        telegram_chat_id="42",
    )
    notifier = TelegramNotifier(settings, client=client)
    now = utc_now()
    decision = DecisionView(
        decision_id="decision-1",
        action_key="action-key-1",
        status="unsupported",
        source_kind="vision",
        table_id="table-1",
        hand_id="hand-1",
        street="preflop",
        state_hash="sha256:test",
        execution_status="not_executed",
        error="test",
        created_at=now,
        updated_at=now,
    )

    try:
        with pytest.raises(RuntimeError) as raised:
            await notifier.send(decision, event_factory().observation)
        assert token not in str(raised.value)
        assert "HTTP 500" in str(raised.value)
    finally:
        await client.aclose()
