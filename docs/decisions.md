# Decisions log

| # | Decision | Choice | Why | Status |
|---|---|---|---|---|
| D1 | Framework | Pipecat (pinned) for M3+; hand-rolled script for the M2 baseline | Python, no server infra, first-party Cartesia + Moss, built-in metrics; baseline gives a before/after number | Accepted 2026-11-17 |
| D2 | STT / turns | Cartesia Ink-2 Turns; local Smart Turn as ablation | One vendor, provider end-of-turn + eager_end | Accepted |
| D3 | Client | Browser WebRTC; terminal only for M2 | Browser echo cancellation is required for barge-in | Accepted |
| D4 | Language | Python 3.12 (Homebrew) | Ecosystem fit; widest wheel support for voice/ML libs | Accepted (revised 2026-11-18) |
| D5 | Repo | Monorepo, packages/nessie-tools installed editable (+ fake in M1); split out at Project #2 | Avoid premature packaging; fake makes tests/evals fast, free, resettable | Accepted |
| D6 | Secrets / cost | .env + pydantic-settings + gitleaks; per-call JSONL cost ledger; Nessie key redaction | Never commit keys; cost per conversation becomes a metric | Accepted |
| D7 | LLM | Haiku 5.5 low effort live; Sonnet 5.5 as eval comparison | Latency first | Accepted |
| D8 | Safety | Two-phase transfer (propose → confirm) enforced in code | Prompts can be talked around; code can't | Accepted |
| D9 | Runpod | Not used in #1 | Save credits for #2 | Accepted |
| D10 | M2 baseline | Build the hand-rolled non-streaming baseline | Before/after latency story | Accepted |
| D11 | Cartesia in M0 | Raw REST (httpx), not the SDK | Learn the protocol; avoid SDK-version guesswork | Accepted |
| D12 | License | MIT | Simple, widely recognized for portfolio code | Accepted 2026-11-18 |
| D13 | Package tooling | pip + venv, `requirements.lock` via `make freeze` | Familiar, standard tooling; lock file keeps results reproducible | Accepted 2026-11-18 |
| D14 | Secret-scan exceptions | Inline `# gitleaks:allow` only on fake test keys, with a reason | Keeps gitleaks active for the rest of the file | Accepted 2026-11-18 |
| D15 | Transfer mechanics | Withdrawal (source) + deposit (destination) behind one `transfer()`, run as a saga with a compensating action; our audit log is the source of truth | Nessie transfers have no destination field | Accepted 2026-11-18 |
| D16 | Non-whole amounts | Reject non-integer amounts before calling Nessie; agent asks for a whole-dollar amount | Nessie stores integers and may silently truncate | Accepted 2026-11-18 |
| D17 | Process | Deliver M0 v2 (known fixes + probe v2) before the Claude Code review | Review corrected code, not code already known to be wrong | Accepted 2026-11-18 |
| D18 | Balance source | Current balance = Nessie stored balance (opening balance at seed time) + deposits - withdrawals - completed purchases, computed in nessie-tools | Nessie transactions never change the stored balance | Accepted 2026-11-18 |
| D19 | Review triage rule | Accept safety/secret/measurement issues and cheap fixes in code M1 builds on; defer what M1 rewrites; reject hardening of one-off tools | Keeps effort proportional; nothing deferred gets lost | Accepted 2026-11-18 |
