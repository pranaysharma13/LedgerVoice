# Decisions log

| # | Decision | Choice | Why | Status |
|---|---|---|---|---|
| D1 | Framework | Pipecat (pinned) for M3+; hand-rolled script for the M2 baseline | Python, no server infra, first-party Cartesia + Moss, built-in metrics; baseline gives a before/after number | Accepted 2026-11-17 |
| D2 | STT / turns | Cartesia Ink-2 via CartesiaTurnsSTTService; local Smart Turn as ablation | One vendor, provider end-of-turn + eager_end | Accepted |
| D3 | Client | Browser WebRTC; terminal only for M2 | Browser echo cancellation is required for barge-in | Accepted |
| D4 | Language | Python 3.12 + uv | Ecosystem fit | Accepted |
| D5 | Repo | Single repo, uv workspace, packages/nessie-tools (+ fake in M1); split out at Project #2 | Avoid premature packaging; fake makes tests/evals fast, free, resettable | Accepted |
| D6 | Secrets / cost | .env + pydantic-settings + gitleaks; per-call JSONL cost ledger; Nessie key redaction | Never commit keys; cost per conversation becomes a metric | Accepted |
| D7 | LLM | Haiku 5.5 low effort live; Sonnet 5.5 as eval comparison | Latency first | Accepted |
| D8 | Safety | Two-phase transfer (propose → confirm) enforced in code | Prompts can be talked around; code can't | Accepted |
| D9 | Runpod | Not used in #1 | Save credits for #2 | Accepted |
| D10 | M2 baseline | Build the hand-rolled non-streaming baseline | Before/after latency story | Accepted |
| D11 | Cartesia in M0 | Raw REST (httpx), not the SDK | Learn the protocol; avoid SDK-version guesswork | Accepted |
| D12 | License | MIT | Simple, widely recognized for portfolio code | Accepted |
