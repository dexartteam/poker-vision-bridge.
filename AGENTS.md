# Development boundaries

Stage 1 lives in `app/vision/` and `web/`. It must never import the legacy
coordinator, solver, Telegram, or Open Poker modules. Start its own FastAPI app.
The existing bridge and its observation contract remain separate.

Read `docs/vision/ARCHITECTURE.md` before changing module boundaries. Contracts
are versioned. Unknown values are null, never guessed or replaced with zero.
The reducer is pure and uses injected time. Browser detection never calls APIs.
Only the server scheduler may start provider requests. API keys are server-only.
Never silently switch live recognition to mock data. Stage 1 never sets
`decision_ready=true` and never produces robot commands.

Run `npm run verify` before a PR. Add behavioral regression tests when changing
detection, scheduling, reconciliation, or authentication. Keep actual results
and unverified real-camera assumptions in `docs/vision/PROGRESS.md`.
