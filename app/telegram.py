from __future__ import annotations

import html
from typing import Any

import httpx

from app.config import Settings
from app.models import DecisionView, Observation


class TelegramNotifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=10, trust_env=False)

    async def send(self, decision: DecisionView, observation: Observation) -> None:
        if not self.settings.telegram_enabled:
            return
        token = self.settings.telegram_bot_token
        if token is None or self.settings.telegram_chat_id is None:
            return
        url = f"https://api.telegram.org/bot{token.get_secret_value()}/sendMessage"
        try:
            response = await self.client.post(
                url,
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": self._render(decision, observation),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # httpx exceptions include the request URL; Telegram embeds the bot token in it.
            raise RuntimeError(f"Telegram API returned HTTP {exc.response.status_code}") from None
        except httpx.RequestError as exc:
            raise RuntimeError(f"Telegram request failed ({type(exc).__name__})") from None

    @staticmethod
    def _render(decision: DecisionView, observation: Observation) -> str:
        cards = " ".join(observation.hero.cards) or "—"
        board = " ".join(observation.board) or "—"
        lines = [
            "<b>Poker decision</b>",
            f"Table: <code>{html.escape(decision.table_id)}</code>",
            f"Hand: <code>{html.escape(decision.hand_id)}</code>",
            f"Street: {html.escape(decision.street.value)}",
            f"Hero: <code>{html.escape(cards)}</code> · board: <code>{html.escape(board)}</code>",
        ]
        if decision.solver_result:
            mix = []
            for option in decision.solver_result.strategy:
                label = option.action
                if option.amount_bb is not None:
                    label += f" {option.amount_bb}bb"
                mix.append(f"{label} {option.frequency:.1%}")
            lines.append("Mix: " + html.escape(" / ".join(mix)))
        if decision.selected:
            selected = decision.selected.provider_action.action
            if decision.selected.amount_bb is not None:
                selected += f" {decision.selected.amount_bb}bb"
            lines.append(f"Selected: <b>{html.escape(selected.upper())}</b>")
        lines.append(f"Status: {html.escape(decision.status)}")
        if decision.execution_status:
            lines.append(f"Execution: {html.escape(decision.execution_status)}")
        if decision.error:
            lines.append(f"Note: {html.escape(decision.error[:500])}")
        return "\n".join(lines)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class NullNotifier(TelegramNotifier):
    async def send(self, decision: DecisionView, observation: Observation) -> None:
        return None


class CapturingNotifier:
    """Test helper implementing the notifier protocol."""

    def __init__(self) -> None:
        self.messages: list[tuple[DecisionView, Observation]] = []

    async def send(self, decision: DecisionView, observation: Observation) -> None:
        self.messages.append((decision, observation))

    async def close(self) -> None:
        return None
