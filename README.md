# Poker Vision Bridge

## Browser webcam stage

The isolated webcam console is documented in [docs/vision/README.md](docs/vision/README.md).
It runs as `app.vision.api:app` on port 8001 and does not invoke the bridge's decision
or execution pipeline. Start with explicit mock mode, then configure the server-side
OpenAI key for live recognition. Run `npm run verify` for both new and legacy tests.

A runnable, fail-closed MVP for this bot-only flow:

```text
camera/CV JSON snapshots
        ↓
authenticated HTTP or WebSocket receiver
        ↓
stable state + idempotent turn key
        ↓
PokerAI mixed GTO strategy
        ↓
deterministic frequency sampling + legal-action validation
        ├── Telegram copy
        └── Open Poker official WebSocket action
```

Open Poker has no manual player UI. Its browser table is for spectating, so this project does **not** use a screen clicker for Open Poker. It executes through the platform's documented V2 WebSocket (`hand_id`, `turn_token`, `client_action_id`). Camera state and platform state remain separate; a camera-derived decision is executed only if it matches the authoritative Open Poker turn.

The service is deliberately allowlisted to `openpoker.ai` for automatic execution and starts in dry-run mode.

## What is implemented

- `POST /v1/events` — one observation or a batch of up to 100.
- `WS /v1/stream` — continuous camera/CV JSON ingestion.
- `WS /v1/decision-stream` — completed decisions for a UI or monitor.
- `GET /v1/schema/vision-event` — the receiver's current JSON Schema.
- SQLite append-only event log, latest projections, decision log, duplicate protection, and sequence-gap reporting.
- Two independent projections: `vision` and authoritative `platform`.
- Stability gate: by default, two identical semantic observations before a vision decision.
- Confidence gate and complete ordered action-history requirement.
- Freshness and future-clock-skew gates before a vision solve.
- Raw HTTP adapter for PokerAI (the published SDK packages lag the current API).
- Reproducible HMAC sampling from PokerAI's exact mixed frequencies. A retry produces the same choice.
- Strict validation against the currently advertised legal actions. There is no locally invented fallback.
- Telegram notification.
- Open Poker reconnect/resync path and idempotent action IDs.

## Honest solver boundary

PokerAI is not a universal synchronous `state → action` endpoint:

- **Preflop:** fast `POST /v1/gto/preflop`. This MVP requires a full six-position map and supports the first voluntary Hero decision. PokerAI's documented presolved request cannot faithfully encode a prior voluntary Hero action.
- **Flop:** fast two-player presolved `tree → exact node`. The observation must provide `pokerai.pot_type`, role positions, and the exact node path. An arbitrary opponent sizing may not exist in the tree.
- **Turn/river:** intentionally returns `unsupported`. Those streets require range conversion plus an asynchronous solve that can take seconds to tens of seconds.
- **Multiway postflop:** unsupported by PokerAI's two-range postflop contract.

The presolved chart depth is a hard boundary. The configured preflop version must match about 100 BB (or about 40 BB for `6max_RC_40bb`) within a 1 BB tolerance. Because Open Poker stacks change after hands, many later states will intentionally become `unsupported`. This release proves and safely exercises the integration; it is not yet a full-session autonomous strategy.

Every observation is recorded. Unsupported solver contexts and illegal solver outputs can be sent to Telegram; stale or low-confidence frames stop before a solver attempt and therefore do not create a Telegram decision. None of these states are executed. Open Poker will apply its own timeout behavior; the bridge never silently replaces a missing solver result with check/fold.

## Run locally

Python 3.11+ is required. With `uv`:

```bash
cp .env.example .env
uv sync --extra dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The defaults use a fixed fake response only to test wiring. It is not a poker strategy and automatic execution is off.

Generate separate receiver and sampling secrets, then put them in `.env`:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

In another terminal:

```bash
uv run python examples/send_sample.py \
  --token YOUR_RECEIVER_TOKEN
```

The first event is stored as stability frame `1/2`; the second identical observation produces one decision. Interactive API documentation is at `http://127.0.0.1:8000/docs`.

The WebSocket equivalent is:

```bash
uv run python examples/stream_sample.py --token YOUR_RECEIVER_TOKEN
```

`/v1/stream` applies backpressure: send one snapshot and read its ACK before sending the next. The sample follows that contract.

Docker is also supported:

```bash
cp .env.example .env
docker compose up --build
```

Run only one application worker in this MVP because SQLite and the in-process Open Poker session are single-instance components.

## Enable PokerAI

Set:

```dotenv
SOLVER_MODE=pokerai
POKERAI_API_KEY=gto_...
POKERAI_BASE_URL=https://pokerai.bet
```

The API key stays server-side. The adapter calls the current HTTP contract directly:

- `POST /v1/gto/preflop`
- `POST /v1/gto/flop/tree`
- `POST /v1/gto/flop/node`

PokerAI returns `strategy[]` frequencies, not one recommended action. The bridge samples exactly from those frequencies using `SAMPLING_SECRET` and the stable turn identity.

The adapter and tests use the published contract, but a live PokerAI request is not exercised without your API key.

Official references: [PokerAI documentation](https://pokerai.bet/docs), [PokerAI OpenAPI](https://pokerai.bet/openapi.en.json).

## Telegram

Set:

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:...
TELEGRAM_CHAT_ID=...
```

Each completed solver attempt sends the hand, cards, board, full mixed strategy, selected branch, and execution status. Telegram failure never triggers another solver choice or another table action.

## Open Poker modes

Create a self-hosted bot in the Open Poker dashboard and copy its API key.

### Recommended first run: direct platform JSON, dry-run

This proves the PokerAI mapping without the camera:

```dotenv
DECISION_SOURCE=platform
OPENPOKER_ENABLED=true
OPENPOKER_API_KEY=op_live_...
AUTO_EXECUTE=false
```

### Direct autonomous bot-only execution

After inspecting dry-run decisions:

```dotenv
DECISION_SOURCE=platform
OPENPOKER_ENABLED=true
OPENPOKER_API_KEY=op_live_...
AUTO_EXECUTE=true
BOT_ONLY_ACK=I_UNDERSTAND_BOT_ONLY
```

### Camera as the decision source

Keep `DECISION_SOURCE=vision`, enable Open Poker, and send camera observations whose `table_id` and `hand_id` match the live platform session. Immediately before execution the bridge compares:

- table, hand, street, dealer, actor, Hero seat/position, cards, and board;
- blind ratio, ante, pot, every current stack/bet, and all action amounts in BB;
- the exact ordered action line and PokerAI context;
- the complete legal-action set, including call and raise amounts;
- the selected action against the still-current authoritative `valid_actions`.

Any mismatch blocks the action. The camera never supplies or stores the platform `turn_token`; only the authenticated Open Poker session owns it.

The camera controller can read the current non-secret table and hand identifiers from authenticated `GET /v1/openpoker/status`; the turn token is never exposed there. With camera-driven automatic execution, wait until that endpoint reports `hero_turn_authority=true` before publishing the two actionable stability frames.

Official references: [Open Poker V2 spec](https://docs.openpoker.ai/llms-full.txt), [WebSocket protocol](https://docs.openpoker.ai/api-reference/websocket-protocol/).

## Camera/CV event contract

See [`examples/preflop_observation.json`](examples/preflop_observation.json). Important rules:

- Send **full semantic snapshots**, not JSON patches. They are small and self-healing after reconnects.
- Monetary values use native table chips. JSON strings are recommended for exact decimal transport; JSON numbers are also accepted. The bridge converts them to BB using `blinds.big`.
- `seats[].stack` is the current behind stack and `seats[].bet_to` is the current-street committed amount.
- `action_history.actions[].contribution` is the incremental amount added by that action.
- `raise_to` and legal `min_to/max_to` are absolute bet-to/raise-to amounts.
- Cards use `Ah`, `Td`, `2c`, `Ks` notation.
- `source.seq` is globally monotonic for that source within one `boot_id`, across all tables. Resend the exact same event and `event_id` until acknowledged.
- `frame.seq` strictly increases for each fresh capture. Byte-identical consecutive captures may keep the same `frame.hash`; an exact retransmission keeps both values unchanged.
- `turn_id` must stay identical across repeated frames for the same Hero decision and change for the next decision.
- `action_history.complete=true` means the ordered history is known without gaps. Do not assert it when CV is ambiguous.
- Ante games are rejected because PokerAI's GTO requests have no ante field; presolved stack depth must match the selected 100 BB or 40 BB dataset.
- The bridge independently computes the semantic state hash. `frame.hash` and `state_token` are audit metadata, not trusted decision authority.

HTTP example:

```bash
curl http://127.0.0.1:8000/v1/events \
  -H 'Authorization: Bearer YOUR_RECEIVER_TOKEN' \
  -H 'Content-Type: application/json' \
  --data @examples/preflop_observation.json
```

For a live curl test, replace the example's `captured_at` with the current UTC time. The bundled `send_sample.py` does this automatically.

WebSocket clients can authenticate with `Authorization: Bearer ...`; browser clients may use `/v1/stream?token=...` on a trusted local network.

## Response states

- `accepted` / `duplicate` / `rejected` describe ingestion.
- `solver_pending`, `ready`, `unsupported`, `invalid`, `solver_failed` describe decisions.
- `dry_run`, `confirmed`, `unknown_waiting_for_resync`, `failed`, `not_executed` describe execution.

A sequence gap is accepted because every message is a full snapshot, but the ACK sets `resync_required=true` so the sender can surface the loss.

## Tests

```bash
uv run pytest
```

The suite covers schema and cross-field validation, trusted-frame stability, duplicate/crash recovery, bounded solver retry, deterministic mixed-strategy sampling, PokerAI boundaries, Telegram token redaction, Open Poker reducer/resync behavior, and execution reconciliation.

## Scope and safety

This project is for Open Poker and other explicitly bot-only research environments. It contains no anti-detection, process masking, real-money-room connector, or generic screen clicker. Automatic execution cannot be pointed at another host through configuration.
