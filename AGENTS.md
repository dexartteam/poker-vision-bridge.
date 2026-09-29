# Development boundaries

Stage 1 lives in `app/vision/` and the existing recognition modules in `web/`. It must never import the legacy
coordinator, solver, Telegram, or Open Poker modules. Start its own FastAPI app.
The existing bridge and its observation contract remain separate.

Read `docs/vision/ARCHITECTURE.md` before changing module boundaries. Contracts
are versioned. Unknown values are null, never guessed or replaced with zero.
The reducer is pure and uses injected time. Browser detection never calls APIs.
Only the server scheduler may start cloud provider requests. The local browser
controller may start on-device OCR and card recognition, with no image upload.
API keys are server-only. Local observations have a separate versioned contract,
remain partial, and never imply complete table state or readiness to act.
Never silently switch live recognition to mock data. Stage 1 never sets
`decision_ready=true` and never produces robot commands.

Recorded-video solver debugging is a separate consumer in `app/replay/` and
`web/replay/`, served at `replay.html`. It may reuse `app.models` and `app.solver`
for validated game state and PokerAI requests, but never the coordinator,
Telegram, Open Poker, or execution modules. Only recorded input is accepted.
Manual context is explicitly confirmed and bound to the exact source frame.
Partial observations remain unchanged; missing context blocks solver requests.
Replay holds the source frame through recognition and the solver response, then
waits for manual continuation. Seek/source changes invalidate pending responses.
The solver key stays on the server; there is no fake solver fallback.

Run `npm run verify` before a PR. Add behavioral regression tests when changing
detection, scheduling, reconciliation, or authentication. Keep actual results
and unverified real-camera assumptions in `docs/vision/PROGRESS.md`.
