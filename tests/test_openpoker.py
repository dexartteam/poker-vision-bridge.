from __future__ import annotations

from app.openpoker import OpenPokerRunner, position_map
from app.models import Position


def test_full_ring_position_map():
    mapped = position_map(5, [0, 1, 2, 3, 4, 5])
    assert mapped == {
        5: Position.BTN,
        0: Position.SB,
        1: Position.BB,
        2: Position.UTG,
        3: Position.MP,
        4: Position.CO,
    }


def test_reconcile_accepts_identical_state(event_factory):
    vision = event_factory().observation
    assert OpenPokerRunner._reconcile(vision, vision) is None


def test_reconcile_rejects_changed_board(event_factory):
    vision = event_factory().observation
    platform = vision.model_copy(update={"board": ["2c", "3d", "4h"]})
    assert OpenPokerRunner._reconcile(vision, platform) == "board"
