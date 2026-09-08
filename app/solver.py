from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.models import (
    LegalActionType,
    Observation,
    ObservedActionType,
    Position,
    SelectedAction,
    SolverResult,
    StrategyAction,
    Street,
)


class SolverError(RuntimeError):
    pass


class SolverUnsupported(SolverError):
    pass


@dataclass(slots=True)
class PreparedRequest:
    kind: str
    payload: dict[str, Any]

    def as_log_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.payload}


class Solver(Protocol):
    def prepare(self, observation: Observation) -> PreparedRequest: ...

    async def solve(self, request: PreparedRequest) -> SolverResult: ...

    async def close(self) -> None: ...


def _to_bb(value: Decimal, big_blind: Decimal) -> Decimal:
    return value / big_blind


def _number(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


class PokerAISolver:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=settings.pokerai_base_url.rstrip("/"),
            timeout=settings.pokerai_timeout_seconds,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {settings.pokerai_api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "User-Agent": "poker-vision-bridge/0.1.0",
            },
        )
        self._flop_tree_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._flop_tree_lock = asyncio.Lock()

    def prepare(self, observation: Observation) -> PreparedRequest:
        if observation.street == Street.PREFLOP:
            return PreparedRequest("preflop", {"request": self._prepare_preflop(observation)})
        if observation.street == Street.FLOP:
            return PreparedRequest("flop", self._prepare_flop(observation))
        raise SolverUnsupported(
            f"PokerAI live MVP supports preflop and presolved flop; {observation.street.value} "
            "needs an asynchronous range/solver pipeline"
        )

    def _prepare_preflop(self, observation: Observation) -> dict[str, Any]:
        if not observation.action_history.complete:
            raise SolverUnsupported("preflop action history is incomplete")
        if observation.blinds.ante > 0:
            raise SolverUnsupported("PokerAI presolved strategy has no ante input")
        occupied_positions = {
            seat.position
            for seat in observation.seats
            if seat.status != "empty" and seat.position is not None
        }
        if occupied_positions != set(Position):
            raise SolverUnsupported("PokerAI presolved preflop currently requires a full 6-max position map")

        actions: list[dict[str, Any]] = []
        for item in observation.action_history.actions:
            if item.street != Street.PREFLOP:
                continue
            is_blind = item.action in {
                ObservedActionType.SMALL_BLIND,
                ObservedActionType.BIG_BLIND,
            }
            if item.seat == observation.hero.seat and not is_blind:
                raise SolverUnsupported(
                    "PokerAI presolved preflop request does not represent a prior voluntary Hero action"
                )

            action_name: str
            if item.action == ObservedActionType.SMALL_BLIND:
                action_name = "small blind"
            elif item.action == ObservedActionType.BIG_BLIND:
                action_name = "big blind"
            elif item.action == ObservedActionType.BET:
                action_name = "raise"
            elif item.action == ObservedActionType.ALL_IN:
                action_name = item.base_action or "raise"
            elif item.action in {
                ObservedActionType.FOLD,
                ObservedActionType.CALL,
                ObservedActionType.RAISE,
            }:
                action_name = item.action.value
            elif item.action == ObservedActionType.CHECK:
                raise SolverUnsupported("unexpected preflop check before Hero")
            else:  # pragma: no cover - enum makes this defensive
                raise SolverUnsupported(f"unsupported preflop action: {item.action}")

            mapped: dict[str, Any] = {
                "position": item.position.value,
                "action": action_name,
            }
            if action_name != "fold":
                mapped["amount"] = _number(_to_bb(item.contribution, observation.blinds.big))
            if item.all_in or item.action == ObservedActionType.ALL_IN:
                mapped["allin"] = True
            actions.append(mapped)

        if len(actions) < 2:
            raise SolverUnsupported("preflop history must start with small blind and big blind posts")
        if actions[0]["action"] != "small blind" or actions[1]["action"] != "big blind":
            raise SolverUnsupported("preflop history must start with small blind then big blind")

        version = observation.pokerai.preflop_version or self.settings.preflop_version
        contributions: dict[int, Decimal] = {}
        for item in observation.action_history.actions:
            if item.street == Street.PREFLOP:
                contributions[item.seat] = contributions.get(item.seat, Decimal("0")) + item.contribution
        effective_starts = [
            (seat.stack + contributions.get(seat.seat, Decimal("0"))) / observation.blinds.big
            for seat in observation.seats
            if seat.status != "empty" and seat.in_hand and not seat.folded
        ]
        target_stack = Decimal("40") if "40bb" in version.lower() else Decimal("100")
        if not effective_starts or abs(min(effective_starts) - target_stack) > Decimal("1"):
            raise SolverUnsupported(
                f"presolved preflop version {version} expects about {target_stack}bb effective"
            )

        return {
            "table_size": "6max",
            "hole_cards": "".join(observation.hero.cards),
            "positions": {"hero": observation.hero.position.value},
            "preflop_version": version,
            "preflop_actions": actions,
        }

    def _prepare_flop(self, observation: Observation) -> dict[str, Any]:
        if not observation.action_history.complete:
            raise SolverUnsupported("flop action history is incomplete")
        if observation.blinds.ante > 0:
            raise SolverUnsupported("PokerAI presolved strategy has no ante input")
        if len(observation.board) != 3:
            raise SolverUnsupported("presolved flop requires exactly three board cards")
        context = observation.pokerai
        if not context.pot_type or not context.roles or not context.node_id:
            raise SolverUnsupported(
                "presolved flop requires pokerai.pot_type, pokerai.roles, and exact pokerai.node_id"
            )
        required = {
            "SRP": {"hero", "raiser", "caller"},
            "3BET": {"hero", "raiser", "three_bettor"},
            "4BET": {"hero", "raiser", "three_bettor"},
            "LIMP": {"hero", "limper"},
        }[context.pot_type]
        if not required.issubset(context.roles):
            missing = ", ".join(sorted(required - set(context.roles)))
            raise SolverUnsupported(f"missing PokerAI flop roles: {missing}")
        tree_request = {
            "board": "".join(observation.board),
            "pot_type": context.pot_type,
            "positions": {key: value.value for key, value in context.roles.items()},
            "flop_version": context.flop_version or self.settings.flop_version,
        }
        preflop_pot = sum(
            (item.contribution for item in observation.action_history.actions if item.street == Street.PREFLOP),
            Decimal("0"),
        )
        active_stacks = [
            seat.stack + seat.bet_to
            for seat in observation.seats
            if seat.in_hand and not seat.folded and seat.status != "empty"
        ]
        if len(active_stacks) != 2:
            raise SolverUnsupported("PokerAI presolved flop supports exactly two remaining players")
        return {
            "tree_request": tree_request,
            "node_id": context.node_id,
            "hole_cards": "".join(observation.hero.cards),
            "expected_tree_pot_bb": _number(preflop_pot / observation.blinds.big),
            "expected_tree_effective_stack_bb": _number(
                min(active_stacks) / observation.blinds.big
            ),
        }

    async def solve(self, request: PreparedRequest) -> SolverResult:
        if request.kind == "preflop":
            response = await self._post("/v1/gto/preflop", request.payload["request"])
            return self._parse_result(response, situation=response.get("situation"))
        if request.kind == "flop":
            tree_request = request.payload["tree_request"]
            cache_key = hashlib.sha256(
                json.dumps(tree_request, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            async with self._flop_tree_lock:
                tree = self._flop_tree_cache.get(cache_key)
                if tree is None:
                    tree = await self._post("/v1/gto/flop/tree", tree_request)
                    self._flop_tree_cache[cache_key] = tree
                    while len(self._flop_tree_cache) > 128:
                        self._flop_tree_cache.popitem(last=False)
                else:
                    self._flop_tree_cache.move_to_end(cache_key)
            expected_pot = float(request.payload["expected_tree_pot_bb"])
            expected_stack = float(request.payload["expected_tree_effective_stack_bb"])
            if abs(float(tree.get("pot", -1)) - expected_pot) > 0.1:
                raise SolverUnsupported(
                    f"presolved flop pot mismatch: tree={tree.get('pot')}bb actual={expected_pot}bb"
                )
            if abs(float(tree.get("effective_stack", -1)) - expected_stack) > 0.1:
                raise SolverUnsupported(
                    "presolved flop effective-stack mismatch: "
                    f"tree={tree.get('effective_stack')}bb actual={expected_stack}bb"
                )
            wanted = request.payload["node_id"]
            node = next((item for item in tree.get("nodes", []) if item.get("node") == wanted), None)
            if node is None:
                raise SolverUnsupported(f"exact PokerAI flop node is unavailable: {wanted}")
            if not node.get("is_hero"):
                raise SolverUnsupported(f"PokerAI flop node is not a Hero decision: {wanted}")
            response = await self._post(
                "/v1/gto/flop/node",
                {"node": node["token"], "hole_cards": request.payload["hole_cards"]},
            )
            return self._parse_result(response, situation=response.get("node"))
        raise SolverUnsupported(f"unknown prepared request kind: {request.kind}")

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise SolverError(f"PokerAI timeout on {path}") from exc
        except httpx.HTTPError as exc:
            raise SolverError(f"PokerAI transport error on {path}: {exc}") from exc
        if response.status_code >= 400:
            detail: Any
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:500]
            if response.status_code in {404, 422}:
                raise SolverUnsupported(f"PokerAI has no solution: {detail}")
            raise SolverError(f"PokerAI HTTP {response.status_code}: {detail}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SolverError("PokerAI returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise SolverError("PokerAI returned a non-object response")
        return data

    @staticmethod
    def _parse_result(data: dict[str, Any], situation: str | None) -> SolverResult:
        raw_strategy = data.get("strategy")
        if not isinstance(raw_strategy, list) or not raw_strategy:
            raise SolverError("PokerAI response contains no strategy")
        try:
            strategy = [StrategyAction.model_validate(item) for item in raw_strategy]
        except Exception as exc:
            raise SolverError(f"PokerAI strategy shape is invalid: {exc}") from exc
        return SolverResult(
            provider="pokerai.bet",
            provider_request_id=data.get("request_id") or data.get("task_id"),
            situation=situation,
            strategy=strategy,
            quota=data.get("quota"),
            raw=data,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class FakeSolver:
    """Fixed local responses for wiring tests. Never use this as a poker strategy."""

    def prepare(self, observation: Observation) -> PreparedRequest:
        if observation.street not in {Street.PREFLOP, Street.FLOP}:
            raise SolverUnsupported("fake solver only exercises the fast MVP paths")
        return PreparedRequest(
            "fake",
            {
                "street": observation.street.value,
                "hole_cards": "".join(observation.hero.cards),
            },
        )

    async def solve(self, request: PreparedRequest) -> SolverResult:
        return SolverResult(
            provider="fake-wiring-test",
            situation=request.payload.get("street"),
            strategy=[
                StrategyAction(action="fold", frequency=0.25),
                StrategyAction(action="call", frequency=0.25),
                StrategyAction(action="raise", frequency=0.5, amount_bb=Decimal("3")),
            ],
            raw={"warning": "not a poker strategy"},
        )

    async def close(self) -> None:
        return None


def select_mixed_action(
    result: SolverResult,
    *,
    action_key: str,
    secret: str,
) -> StrategyAction:
    total = sum(item.frequency for item in result.strategy)
    if total <= 0:
        raise SolverError("strategy frequencies sum to zero")
    digest = hmac.new(secret.encode(), action_key.encode(), hashlib.sha256).digest()
    unit = int.from_bytes(digest[:8], "big") / 2**64
    target = unit * total
    cursor = 0.0
    for item in result.strategy:
        cursor += item.frequency
        if target < cursor:
            return item
    return result.strategy[-1]


def validate_selected_action(
    provider_action: StrategyAction,
    observation: Observation,
) -> SelectedAction:
    legal = {item.action: item for item in observation.legal_actions}
    action = provider_action.action

    if provider_action.allin and provider_action.action in {"bet", "raise"}:
        if LegalActionType.ALL_IN in legal:
            advertised = legal[LegalActionType.ALL_IN]
            amount_bb = (
                advertised.max_to / observation.blinds.big
                if advertised.max_to is not None
                else provider_action.amount_bb
            )
            return SelectedAction(
                provider_action=provider_action,
                platform_action=LegalActionType.ALL_IN,
                amount_bb=amount_bb,
                valid=True,
            )

    simple_map = {
        "fold": LegalActionType.FOLD,
        "check": LegalActionType.CHECK,
        "call": LegalActionType.CALL,
        "bet": LegalActionType.RAISE,
        "raise": LegalActionType.RAISE,
    }
    platform_action = simple_map[action]
    advertised = legal.get(platform_action)
    if advertised is None:
        return SelectedAction(
            provider_action=provider_action,
            valid=False,
            invalid_reason=f"provider action {action} is absent from observed legal_actions",
        )
    if platform_action == LegalActionType.RAISE:
        if provider_action.amount_bb is None:
            return SelectedAction(
                provider_action=provider_action,
                valid=False,
                invalid_reason="PokerAI raise/bet has no amount_bb",
            )
        amount_native = provider_action.amount_bb * observation.blinds.big
        if advertised.min_to is None or advertised.max_to is None:
            return SelectedAction(
                provider_action=provider_action,
                valid=False,
                invalid_reason="observed raise has no min_to/max_to",
            )
        if not advertised.min_to <= amount_native <= advertised.max_to:
            return SelectedAction(
                provider_action=provider_action,
                valid=False,
                invalid_reason=(
                    f"PokerAI raise-to {amount_native} is outside observed "
                    f"[{advertised.min_to}, {advertised.max_to}]"
                ),
            )
        return SelectedAction(
            provider_action=provider_action,
            platform_action=platform_action,
            amount_bb=provider_action.amount_bb,
            valid=True,
        )
    return SelectedAction(
        provider_action=provider_action,
        platform_action=platform_action,
        valid=True,
    )


def build_solver(settings: Settings, client: httpx.AsyncClient | None = None) -> Solver:
    if settings.solver_mode == "pokerai":
        return PokerAISolver(settings, client=client)
    return FakeSolver()
