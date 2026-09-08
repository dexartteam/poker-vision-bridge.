from __future__ import annotations

import argparse
import asyncio
import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import websockets


SAMPLE = Path(__file__).with_name("preflop_observation.json")


async def run(url: str, token: str, source: Path) -> None:
    template = json.loads(source.read_text(encoding="utf-8"))
    boot_id = str(uuid.uuid4())
    async with websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
    ) as websocket:
        for sequence in (1, 2):
            event = copy.deepcopy(template)
            event["event_id"] = str(uuid.uuid4())
            event["source"]["boot_id"] = boot_id
            event["source"]["seq"] = sequence
            event["frame"]["seq"] = sequence
            event["captured_at"] = datetime.now(timezone.utc).isoformat()
            await websocket.send(json.dumps(event, separators=(",", ":")))
            print(await websocket.recv())


def main() -> None:
    parser = argparse.ArgumentParser(description="Send two stable snapshots over WebSocket")
    parser.add_argument("--url", default="ws://127.0.0.1:8000/v1/stream")
    parser.add_argument("--token", required=True)
    parser.add_argument("--input", type=Path, default=SAMPLE)
    args = parser.parse_args()
    asyncio.run(run(args.url, args.token, args.input))


if __name__ == "__main__":
    main()
