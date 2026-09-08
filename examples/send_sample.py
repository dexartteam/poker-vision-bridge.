from __future__ import annotations

import argparse
import asyncio
import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx


async def main() -> None:
    parser = argparse.ArgumentParser(description="Send two stable demo observations.")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="replace-with-a-long-random-value")
    args = parser.parse_args()

    sample_path = Path(__file__).with_name("preflop_observation.json")
    template = json.loads(sample_path.read_text(encoding="utf-8"))
    boot_id = str(uuid.uuid4())
    events = []
    for seq in (1, 2):
        event = copy.deepcopy(template)
        event["event_id"] = str(uuid.uuid4())
        event["source"]["boot_id"] = boot_id
        event["source"]["seq"] = seq
        event["frame"]["seq"] = 100 + seq
        event["frame"]["hash"] = f"sha256:demo-frame-{seq:04d}"
        event["captured_at"] = datetime.now(timezone.utc).isoformat()
        events.append(event)

    async with httpx.AsyncClient(base_url=args.url, timeout=30, trust_env=False) as client:
        response = await client.post(
            "/v1/events",
            headers={"Authorization": f"Bearer {args.token}"},
            json=events,
        )
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
