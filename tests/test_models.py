from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_duplicate_cards_are_rejected(event_factory):
    event = event_factory()
    with pytest.raises(ValidationError):
        event.observation.model_copy(
            update={"board": ["Ah", "2c", "3d"], "street": "flop"}
        ).__class__.model_validate(
            {
                **event.observation.model_dump(mode="json"),
                "street": "flop",
                "board": ["Ah", "2c", "3d"],
            }
        )

