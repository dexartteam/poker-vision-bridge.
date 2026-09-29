from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from app.solver import PokerAIRequestPlanner, PreparedRequest, SolverUnsupported

from .contracts import FrameRef, Issue, ReplayRequest, ReplayResponse, ReplayState, SolverInfo


def frame_ref(request: ReplayRequest) -> FrameRef:
    source = request.observation.source
    return FrameRef(
        source_epoch=source.epoch,
        frame_seq=source.frame_seq,
        revision=source.revision,
        media_time=source.media_time,
    )


def _card_values(cards, expected: int | None = None):
    if expected is not None:
        return (
            [c.value if c.status == "visible" else None for c in cards]
            if len(cards) == expected
            else None
        )
    if len(cards) != 5 or any(c.status == "unreadable" for c in cards):
        return None
    visible = [c.value for c in cards if c.status == "visible"]
    if any(c.status != ("visible" if i < len(visible) else "empty") for i, c in enumerate(cards)):
        return None
    return visible


def draft_observation(request: ReplayRequest) -> dict[str, Any]:
    raw = request.observation
    heroes = [seat.seat_index for seat in raw.seats if seat.is_hero]
    hero_seat = heroes[0] if len(heroes) == 1 else None
    legal = []
    for visible in raw.available_actions:
        action = "raise" if visible == "bet" else visible
        item: dict[str, Any] = {"action": action}
        if action == "call":
            item["call_amount"] = None
        if action == "raise":
            item.update(min_to=None, max_to=None)
        if action == "all_in":
            item["max_to"] = None
        legal.append(item)
    return {
        "hand_id": str(raw.hand_number) if raw.hand_number is not None else None,
        "street": None if raw.ui_street == "unknown" else raw.ui_street,
        "dealer_seat": raw.dealer_seat,
        "acting_seat": hero_seat if raw.hero_turn else None,
        "blinds": {"small": None, "big": None, "ante": None},
        "hero": {"seat": hero_seat, "position": None, "cards": _card_values(raw.hero_cards, 2)},
        "board": _card_values(raw.board),
        # The OCR display may exclude the current bets; it is not the semantic total pot.
        "pot": None,
        "seats": [
            {
                "seat": seat.seat_index,
                "position": None,
                "name": seat.name,
                "stack": seat.stack,
                "bet_to": None,
                "status": None,
                "in_hand": None,
                "folded": True if seat.status == "folded" else None,
            }
            for seat in raw.seats
        ],
        "action_history": {"complete": False, "actions": []},
        "legal_actions": legal,
        "hero_turn": raw.hero_turn,
        "turn_id": None,
        "pokerai": {},
    }


def _merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        return {key: _merge(base.get(key), value) for key, value in (base | patch).items()}
    return deepcopy(patch)


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float, Decimal)) and not isinstance(left, bool):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except (InvalidOperation, ValueError):
            return False
    return left == right


def _leaves(value: Any, prefix="") -> list[str]:
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _leaves(v, f"{prefix}.{k}".strip("."))]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _leaves(v, f"{prefix}.{i}")]
    return [prefix]


def _known_conflicts(base: dict, draft: dict, add) -> None:
    def compare(left, right, path):
        if left is None:
            return
        if isinstance(left, dict):
            if not isinstance(right, dict):
                add(
                    "recognized_conflict",
                    path,
                    "Ручное значение противоречит прочитанному полю.",
                    "invalid",
                )
            else:
                for key, value in left.items():
                    compare(value, right.get(key), f"{path}.{key}".strip("."))
        elif isinstance(left, list):
            if not isinstance(right, list) or len(left) != len(right):
                add(
                    "recognized_conflict",
                    path,
                    "Количество элементов не совпадает с кадром.",
                    "invalid",
                )
            else:
                for i, value in enumerate(left):
                    compare(value, right[i], f"{path}.{i}")
        elif not _same(left, right):
            add(
                "recognized_conflict",
                path,
                "Ручное значение противоречит прочитанному полю.",
                "invalid",
            )

    for key in ("hand_id", "street", "dealer_seat", "hero", "board", "hero_turn"):
        compare(base[key], draft.get(key), key)
    if not isinstance(draft.get("seats"), list):
        return
    by_id = {
        s["seat"]: s
        for s in draft["seats"]
        if isinstance(s, dict) and isinstance(s.get("seat"), int)
    }
    for seat in base["seats"]:
        compare(seat, by_id.get(seat["seat"]), f"seats.{seat['seat']}")
    legal = draft.get("legal_actions")
    legal_names = (
        {a["action"] for a in legal if isinstance(a, dict) and isinstance(a.get("action"), str)}
        if isinstance(legal, list)
        else set()
    )
    if any(a["action"] not in legal_names for a in base["legal_actions"]):
        add(
            "recognized_conflict",
            "legal_actions",
            "В списке отсутствует распознанная активная кнопка.",
            "invalid",
        )


def diagnose(
    request: ReplayRequest,
    planner: PokerAIRequestPlanner,
    *,
    configured: bool,
) -> tuple[ReplayResponse, ReplayState | None, PreparedRequest | None]:
    issues: list[Issue] = []

    def add(code, field, message, kind="missing"):
        if not any(i.code == code and i.field == field for i in issues):
            issues.append(Issue(code=code, field=field, message=message, kind=kind))

    raw = request.observation
    frame = frame_ref(request)
    draft = draft_observation(request)
    original = deepcopy(draft)
    manual_fields = []
    context = request.confirmed_context
    if context:
        if (
            context.source_epoch != frame.source_epoch
            or context.frame_seq != frame.frame_seq
            or context.revision != frame.revision
            or context.media_time != frame.media_time
        ):
            add(
                "context_frame_mismatch",
                "confirmed_context",
                "Подтверждение относится к другому кадру; проверьте поля заново.",
                "invalid",
            )
        else:
            draft = _merge(draft, context.values)
            manual_fields = sorted(_leaves(context.values))
            _known_conflicts(original, draft, add)
    if raw.source.kind != "recording":
        add(
            "recording_required",
            "source.kind",
            "Этот режим принимает только записанное видео.",
            "invalid",
        )
    if raw.source.media_time is None:
        add("media_time_missing", "source.media_time", "Нет временной отметки кадра в видеозаписи.")
    if raw.status == "stale":
        add(
            "stale_frame",
            "status",
            "Нужен новый снимок остановленного видео; это чтение уже устарело.",
            "invalid",
        )
    if not raw.stable:
        add("unstable_frame", "stable", "Снимите кадр после окончания анимации.")

    def require(path, message):
        value: Any = draft
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value is None or value == "":
            add("missing_field", path, message)

    for path, message in {
        "hand_id": "Укажите идентификатор раздачи и подтвердите её начало.",
        "street": "Не определена стадия раздачи.",
        "dealer_seat": "Не определено место дилера.",
        "acting_seat": "Не определён игрок, который должен ходить.",
        "blinds.small": "Не известен малый блайнд.",
        "blinds.big": "Не известен большой блайнд.",
        "blinds.ante": "Подтвердите наличие и размер анте; отсутствие — 0.",
        "hero.seat": "Не определено место героя.",
        "hero.position": "Не определена позиция героя относительно дилера.",
        "hero.cards": "Не прочитаны обе карты героя.",
        "board": "Не полностью прочитан борд; до флопа подтвердите пустой список.",
        "pot": "Укажите общий банк с текущими ставками: состав суммы pot_display пока неизвестен.",
        "hero_turn": "Нет подтверждения очереди героя; флажки предварительного выбора не подходят.",
        "turn_id": "Укажите идентификатор именно этого решения героя.",
    }.items():
        require(path, message)
    hero = draft.get("hero")
    if isinstance(hero, dict) and isinstance(hero.get("cards"), list):
        if len(hero["cards"]) != 2 or any(c is None for c in hero["cards"]):
            add("missing_field", "hero.cards", "Нужны две достоверно прочитанные карты героя.")
    if draft.get("hero_turn") is False:
        add("not_hero_turn", "hero_turn", "В этом кадре нет решения героя.", "invalid")
    seats = draft.get("seats")
    if not isinstance(seats, list) or len(seats) < 2:
        add("missing_field", "seats", "Нужны все места, позиции, статусы, стеки и текущие ставки.")
    else:
        for index, seat in enumerate(seats):
            if not isinstance(seat, dict):
                continue
            for key in ("seat", "position", "stack", "bet_to", "status", "in_hand", "folded"):
                if seat.get(key) is None and not (
                    key == "position" and seat.get("status") == "empty"
                ):
                    add(
                        "missing_field",
                        f"seats.{index}.{key}",
                        {
                            "bet_to": "Сумма chip_display может быть выплатой: подтвердите ставку на текущей улице.",
                            "position": "Не известна позиция этого места относительно дилера.",
                            "stack": "Не прочитан остаток стека игрока.",
                        }.get(key, "Не подтверждено состояние места игрока."),
                    )
    history = draft.get("action_history")
    if (
        not isinstance(history, dict)
        or history.get("complete") is not True
        or not history.get("actions")
    ):
        add(
            "history_incomplete",
            "action_history",
            "Нужна полная последовательность действий с блайндов, включая пас, чек и суммы добавленных фишек.",
        )
    if not context or not context.history_confirmed:
        add(
            "history_unconfirmed",
            "confirmed_context.history_confirmed",
            "Подтвердите вручную, что история проверена с начала раздачи и не имеет пропусков.",
        )
    legal = draft.get("legal_actions")
    if not isinstance(legal, list) or not legal:
        add(
            "missing_field",
            "legal_actions",
            "Нужны допустимые действия и точные суммы call/min_to/max_to.",
        )
    else:
        for index, item in enumerate(legal):
            if not isinstance(item, dict):
                continue
            action = item.get("action")
            amount_fields = (
                {"call": ["call_amount"], "raise": ["min_to", "max_to"], "all_in": ["max_to"]}.get(
                    action, []
                )
                if isinstance(action, str)
                else []
            )
            for name in amount_fields:
                if item.get(name) is None:
                    add(
                        "missing_field",
                        f"legal_actions.{index}.{name}",
                        "Не известна сумма или граница допустимого действия.",
                    )
    if draft.get("street") == "flop":
        for path, message in {
            "pokerai.pot_type": "Нужен тип префлоп-банка: SRP, 3BET, 4BET или LIMP.",
            "pokerai.node_id": "Нужен точный путь решения в дереве PokerAI.",
        }.items():
            require(path, message)
        ctx = draft.get("pokerai") or {}
        roles = ctx.get("roles") or {} if isinstance(ctx, dict) else {}
        pot_type = ctx.get("pot_type") if isinstance(ctx, dict) else None
        required = (
            {
                "SRP": ("hero", "raiser", "caller"),
                "3BET": ("hero", "raiser", "three_bettor"),
                "4BET": ("hero", "raiser", "three_bettor"),
                "LIMP": ("hero", "limper"),
            }.get(pot_type, ("hero",))
            if isinstance(pot_type, str)
            else ("hero",)
        )
        for role in required:
            if not isinstance(roles, dict) or not roles.get(role):
                add(
                    "missing_field",
                    f"pokerai.roles.{role}",
                    "Не подтверждены позиции участников префлоп-линии.",
                )
    if draft.get("street") in ("turn", "river", "showdown"):
        add(
            "street_unsupported",
            "street",
            "Текущий адаптер поддерживает префлоп и готовые деревья флопа; тёрн/ривер требуют диапазонов и асинхронного расчёта.",
            "unsupported",
        )

    state = None
    try:
        state = ReplayState.model_validate(draft)
    except ValidationError as exc:
        for error in exc.errors():
            path = ".".join(map(str, error["loc"])) or "observation"
            if any(
                i.kind == "missing" and (i.field == path or i.field.startswith(path + "."))
                for i in issues
            ):
                continue
            kind = (
                "missing" if error.get("input") is None or error["type"] == "missing" else "invalid"
            )
            add("invalid_state" if kind == "invalid" else "missing_field", path, error["msg"], kind)
    prepared = None
    if state is not None:
        _check_consistency(state, add)
        if not any(i.kind in {"missing", "invalid"} for i in issues):
            try:
                prepared = planner.prepare(state)
            except SolverUnsupported as exc:
                add("solver_unsupported", "solver", str(exc), "unsupported")
    if not configured:
        add(
            "pokerai_not_configured",
            "solver",
            "На сервере не задан POKERAI_API_KEY. Диагностика доступна, реальный запрос не отправлен.",
            "configuration",
        )
    status = "ready"
    for kind, result in (
        ("invalid", "invalid"),
        ("missing", "missing_data"),
        ("unsupported", "unsupported"),
        ("configuration", "not_configured"),
    ):
        if any(i.kind == kind for i in issues):
            status = result
            break
    return (
        ReplayResponse(
            session_id=request.session_id,
            request_id=request.request_id,
            frame=frame,
            status=status,
            issues=issues,
            solver=SolverInfo(configured=configured),
            draft_observation=draft,
            normalized_observation=state.model_dump(mode="json") if state else None,
            solver_request=prepared.as_log_dict() if prepared else None,
            manual_fields=manual_fields,
        ),
        state,
        prepared,
    )


def _check_consistency(state: ReplayState, add) -> None:
    actions = state.action_history.actions
    if not state.action_history.complete:
        return
    if len(actions) < 2 or [a.action for a in actions[:2]] != ["small_blind", "big_blind"]:
        add(
            "blind_history_missing",
            "action_history.actions",
            "Полная история начинается с малого и большого блайндов.",
            "invalid",
        )
    else:
        for action, position, amount in zip(
            actions[:2], ("SB", "BB"), (state.blinds.small, state.blinds.big), strict=True
        ):
            if (
                action.position != position
                or action.contribution != amount
                or action.street != "preflop"
            ):
                add(
                    "blind_history_mismatch",
                    "action_history.actions",
                    "Позиции или суммы внесённых блайндов не совпадают с параметрами раздачи.",
                    "invalid",
                )
    committed_so_far: dict[tuple, Decimal] = {}
    folded_seats = set()
    for action in actions:
        key = (action.street, action.seat)
        previous = committed_so_far.get(key, Decimal(0))
        if action.seat in folded_seats:
            add(
                "action_after_fold",
                "action_history.actions",
                "В истории есть действие игрока после его паса.",
                "invalid",
            )
        if action.action in {"check", "fold"} and action.contribution != 0:
            add(
                "nonzero_passive_contribution",
                "action_history.actions",
                "Пас и чек не добавляют фишки в банк.",
                "invalid",
            )
        if action.action == "all_in" and action.contribution <= 0:
            add(
                "allin_contribution_missing",
                "action_history.actions",
                "Для all-in нужен положительный взнос и базовое действие call/raise.",
                "invalid",
            )
        if action.raise_to is not None and action.raise_to != previous + action.contribution:
            add(
                "raise_to_mismatch",
                "action_history.actions",
                "raise_to не совпадает с ранее внесённой на улице суммой плюс новым взносом.",
                "invalid",
            )
        committed_so_far[key] = previous + action.contribution
        if action.action == "fold":
            folded_seats.add(action.seat)
    if sum((a.contribution for a in actions), Decimal(0)) != state.pot:
        add(
            "pot_history_mismatch",
            "pot",
            "Общий банк не совпадает с суммой взносов истории. Проверьте пропущенные действия, возврат ставки и состав банка.",
            "invalid",
        )
    for seat in state.seats:
        if seat.folded != (seat.seat in folded_seats):
            add(
                "fold_history_mismatch",
                f"seats.{seat.seat}.folded",
                "Статус паса не совпадает с полной историей действий.",
                "invalid",
            )
        committed = sum(
            (a.contribution for a in actions if a.seat == seat.seat and a.street == state.street),
            Decimal(0),
        )
        if committed != seat.bet_to:
            add(
                "bet_history_mismatch",
                f"seats.{seat.seat}.bet_to",
                "Ставка на улице не совпадает с суммой взносов этого игрока в истории.",
                "invalid",
            )
    by_seat = {s.seat: s for s in state.seats}
    if by_seat[state.dealer_seat].position != "BTN":
        add(
            "dealer_position_mismatch",
            "dealer_seat",
            "Место дилера должно иметь позицию BTN.",
            "invalid",
        )
    if state.hero_turn:
        hero = by_seat[state.hero.seat]
        owed = min(max(s.bet_to for s in state.seats) - hero.bet_to, hero.stack)
        for legal in state.legal_actions:
            if legal.action == "call" and legal.call_amount != owed:
                add(
                    "call_amount_mismatch",
                    "legal_actions",
                    "Сумма call не совпадает с разницей ставок и остатком стека героя.",
                    "invalid",
                )
            if legal.action == "check" and owed > 0:
                add(
                    "check_facing_bet",
                    "legal_actions",
                    "Check указан при ненулевой сумме для уравнивания.",
                    "invalid",
                )
            if (
                legal.action in {"raise", "all_in"}
                and legal.max_to is not None
                and legal.max_to > hero.stack + hero.bet_to
            ):
                add(
                    "raise_exceeds_stack",
                    "legal_actions",
                    "Граница raise/all-in превышает доступный стек героя.",
                    "invalid",
                )
    if state.street == "flop":
        if state.pokerai.roles.get("hero") != state.hero.position:
            add(
                "hero_role_mismatch",
                "pokerai.roles.hero",
                "Позиция hero в контексте солвера не совпадает с местом героя.",
                "invalid",
            )
        remaining = {
            s.position for s in state.seats if s.in_hand and not s.folded and s.status != "empty"
        }
        if any(position not in remaining for position in state.pokerai.roles.values()):
            add(
                "flop_roles_mismatch",
                "pokerai.roles",
                "Позиции ролей не совпадают с двумя оставшимися в раздаче игроками.",
                "invalid",
            )
        flop_actions = [a for a in actions if a.street == "flop"]
        if any(not a.node_label for a in flop_actions):
            add(
                "flop_node_labels_missing",
                "action_history.actions",
                "Для действий флопа нужны проверенные node_label из дерева PokerAI: это не подписи OCR.",
            )
        elif state.pokerai.node_id != "/".join(["root"] + [a.node_label for a in flop_actions]):
            add(
                "flop_path_mismatch",
                "pokerai.node_id",
                "Путь узла не совпадает с порядком действий флопа.",
                "invalid",
            )
