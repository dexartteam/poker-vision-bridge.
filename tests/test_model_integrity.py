from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import LegalAction, Observation


def observation_payload(event_factory) -> dict:
    return event_factory().observation.model_dump(mode="json")


def test_valid_fixture_preserves_cross_field_integrity(event_factory):
    observation = Observation.model_validate(observation_payload(event_factory))
    assert observation.hero.position.value == "CO"


@pytest.mark.parametrize("position", [None, "BTN"])
def test_in_hand_positions_are_present_and_unique(event_factory, position):
    payload = observation_payload(event_factory)
    payload["seats"][4]["position"] = position

    with pytest.raises(ValidationError, match="position"):
        Observation.model_validate(payload)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"status": "away"}, "active or disconnected"),
        ({"in_hand": False}, "in_hand"),
        ({"folded": True}, "not to be folded"),
    ],
)
def test_hero_turn_requires_consistent_hero_seat(event_factory, updates, message):
    payload = observation_payload(event_factory)
    payload["seats"][4].update(updates)

    with pytest.raises(ValidationError, match=message):
        Observation.model_validate(payload)


def test_hero_position_must_match_hero_seat(event_factory):
    payload = observation_payload(event_factory)
    payload["seats"][4]["position"] = "BTN"
    payload["seats"][5]["position"] = "CO"

    with pytest.raises(ValidationError, match="hero position"):
        Observation.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dealer_seat", 9, "dealer seat"),
        ("acting_seat", 9, "acting seat"),
    ],
)
def test_dealer_and_actor_must_reference_existing_seats(
    event_factory,
    field,
    value,
    message,
):
    payload = observation_payload(event_factory)
    payload[field] = value
    if field == "acting_seat":
        payload["hero_turn"] = False

    with pytest.raises(ValidationError, match=message):
        Observation.model_validate(payload)


def test_action_must_reference_an_existing_seat(event_factory):
    payload = observation_payload(event_factory)
    payload["action_history"]["actions"][2]["seat"] = 9

    with pytest.raises(ValidationError, match="action seat 9 is missing"):
        Observation.model_validate(payload)


def test_action_position_must_match_its_seat(event_factory):
    payload = observation_payload(event_factory)
    payload["action_history"]["actions"][2]["position"] = "BTN"

    with pytest.raises(ValidationError, match="action position BTN does not match"):
        Observation.model_validate(payload)


def test_legal_action_types_are_unique(event_factory):
    payload = observation_payload(event_factory)
    payload["legal_actions"].append({"action": "fold"})

    with pytest.raises(ValidationError, match="duplicate legal action type"):
        Observation.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"action": "call"}, "call requires call_amount"),
        ({"action": "all_in"}, "all_in requires max_to"),
        (
            {"action": "raise", "min_to": "40", "max_to": "100", "call_amount": "20"},
            "raise cannot include call_amount",
        ),
        (
            {"action": "call", "call_amount": "20", "max_to": "100"},
            "call cannot include min_to or max_to",
        ),
        (
            {"action": "all_in", "max_to": "100", "min_to": "40"},
            "all_in cannot include call_amount or min_to",
        ),
        ({"action": "fold", "call_amount": "20"}, "fold cannot include amount fields"),
        ({"action": "check", "max_to": "100"}, "check cannot include amount fields"),
    ],
)
def test_legal_action_amount_fields_are_action_specific(payload, message):
    with pytest.raises(ValidationError, match=message):
        LegalAction.model_validate(payload)
