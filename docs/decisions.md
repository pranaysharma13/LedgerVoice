# Decisions log

The full log with context lives in the project plan; this file mirrors the decisions.

| # | Decision | Choice | Why | Status |
|---|---|---|---|---|
| D1 | Framework | Pipecat (pinned) for M3+; hand-rolled script for the M2 baseline | Python, no server infra, first-party Cartesia + Moss, built-in metrics; baseline gives a before/after number | Accepted 2026-11-17 |
| D2 | STT / turns | Cartesia Ink-2 Turns; local Smart Turn as ablation | One vendor, provider end-of-turn + eager_end | Accepted |
| D3 | Client | Browser WebRTC; terminal only for M2 | Browser echo cancellation is required for barge-in | Accepted |
| D4 | Language | Python 3.12 (Homebrew) | Ecosystem fit; widest wheel support for voice/ML libs | Accepted (revised 2026-11-18) |
| D5 | Repo | Monorepo, packages/nessie-tools installed editable; split out at Project #2 | Avoid premature packaging | Accepted |
| D6 | Secrets / cost | .env + pydantic-settings + gitleaks; per-call JSONL cost ledger; Nessie key redaction | Never commit keys; cost per conversation becomes a metric | Accepted |
| D7 | LLM | Haiku 5.5 low effort live; Sonnet 5.5 as eval comparison | Latency first | Accepted |
| D8 | Safety | Two-phase transfer (propose → confirm) enforced in code | Prompts can be talked around; code can't | Accepted |
| D9 | Runpod | Not used in #1 | Save credits for #2 | Accepted |
| D10 | M2 baseline | Build the hand-rolled non-streaming baseline | Before/after latency story | Accepted |
| D11 | Cartesia in M0 | Raw REST (httpx), not the SDK | Learn the protocol; avoid SDK-version guesswork | Accepted |
| D12 | License | MIT | Simple, widely recognized for portfolio code | Accepted 2026-11-18 |
| D13 | Package tooling | pip + venv; `requirements.lock` via `make freeze`; `make setup` installs from the lock | Familiar tooling; reproducible results | Accepted 2026-11-18 |
| D14 | Secret-scan exceptions | Inline `# gitleaks:allow` only on fake test keys, with a reason | Keeps gitleaks active for the rest of the file | Accepted 2026-11-18 |
| D15 | Transfer mechanics | Withdrawal (source) + deposit (destination), run as a saga; compensation = reversing deposit; our audit log is the source of truth | Nessie transfers have no destination field | Accepted 2026-11-18 |
| D16 | Non-whole amounts | Reject non-integer amounts before calling Nessie; agent asks for a whole-dollar amount | Nessie silently truncates ($4.75 → $4) | Accepted 2026-11-18 |
| D17 | Process | Deliver fixed code before review when fixes are known or design-level | Review corrected code, not code known to be wrong | Accepted 2026-11-18 |
| D18 | Balance source | Current balance = stored opening balance + completed deposits − withdrawals − purchases | Nessie transactions never change the stored balance | Accepted 2026-11-18 |
| D19 | Review triage rule | Accept safety/secret/measurement issues and cheap fixes; defer what the next milestone rewrites; reject hardening of one-off tools | Effort proportional; nothing deferred gets lost | Accepted 2026-11-18 |
| D20 | Fake placement | `BankBackend` interface at the lowest level; NessieBackend + FakeBackend; contract tests run the same suite on both | Tests exercise the real business logic | Accepted 2026-11-19 |
| D21 | Money representation | Integer dollars inside; user text parsed with Decimal (never float) | Matches Nessie storage; no silent rounding | Accepted 2026-11-19 |
| D22 | Local state | SQLite for audit log + pending transfers | Built-in, atomic, queryable | Accepted 2026-11-19 |
| D23 | Time | Configurable `LEDGERVOICE_TODAY`; evals pin "today" | Reproducible answers to "last week" | Accepted 2026-11-19 |
| D24 | Seed data | 3 personas, 90 days, ~25 merchants, fixed RNG seed, ground truth from generated data, manifest-based reset | Deterministic, realistic, testable | Accepted 2026-11-19 |
| D25 | Tool set | get_accounts, get_transactions (computed totals), get_bills, propose_transfer; no confirm tool | Totals in code; model cannot confirm | Accepted 2026-11-19 |
| D26 | Confirmation ownership | Runtime owns confirmation: exact read-back by code; next user message classified deterministically; whole-message yes → code executes | The model has no ability to move money | Accepted 2026-11-19 |
| D27 | Agent loop | Hand-written loop on the Anthropic SDK, prompt caching, `make chat` | The interception point must be ours | Accepted 2026-11-19 |
| D28 | Text eval | 30 YAML scenarios, programmatic checks, 3 runs each | Rigorous, cheap, reproducible | Accepted 2026-11-19 |
| D29 | Transfer limit | $2,000 per transfer; own accounts only | Bounded blast radius | Accepted 2026-11-19 |
| D30 | Review cadence | 3 review cycles in M1 (after a+b, c+d, e+f) | Enough review without overhead | Accepted 2026-11-19 |
| D31 | Domain code location | Balances, audit, saga, reconciliation live in nessie-tools; no app config imports | Reuse in Projects #2/#3 | Accepted 2026-11-19 |
| D32 | Pre-check failures | Any failure before the first write raises TransferRejected and writes nothing | Honest outcome: nothing happened | Accepted 2026-11-19 |
| D33 | Ambiguous outcomes | Timeout/5xx/2xx-with-error → re-check with back-off (0, 0.5, 1, 2 s); still not found → NEEDS_ATTENTION. Never FAILED, never compensate on a guess | A late write can land after we look; a false "no money moved" invites a duplicate | Accepted 2026-11-19 (review R1-02) |
| D34 | Transfer API | Only `create_pending` + `execute_pending` (+ `cancel_pending`); no direct execute path; unknown ids rejected; funds re-checked at execution | Structurally impossible to skip confirmation | Accepted 2026-11-19 (review R1-01) |
| D35 | Audit evolution | RESOLVED state (from NEEDS_ATTENTION, note required); schema versioned with PRAGMA user_version + ordered migrations; transfer ids must match `LV-TRF-<8 hex>` | Incidents can be closed; M1d can add columns safely; reconcile can always see legs | Accepted 2026-11-19 (review R1-05/R1-09/R1-14) |
| D36 | Referential integrity | Nessie has none (verified live 2026-11-20): deleting an account keeps its transactions; transactions can be created for nonexistent accounts. Backends (incl. the fake) store what they're given; existence is checked explicitly with get_account by the saga (before any write) and by reconcile (account_missing, while still checking orphaned legs) | The fake must copy Nessie's quirks (D20); a deposit to a mistyped id would otherwise vanish | Accepted 2026-11-20 |
| D37 | Concurrency model | Per-account locks are process-wide (shared by every TransferService); the audit log is thread-safe; one process per audit DB (multi-process would need a DB-level lock). Reconcile flags `overdrawn` on non-credit accounts as a safety net | Two service instances could otherwise both pass the funds check and overspend | Accepted 2026-11-20 (review V1-01) |
| D38 | Resolution snapshot | `resolve()` stores the leg ids the human saw; reconcile flags any later leg as `leg_after_resolution`, and flags a tagged leg from a transfer outside the scanned scope as `out_of_scope_leg` | "Resolved" means "checked at that moment", not "ignore forever"; a late write must stay visible | Accepted 2026-11-20 (review V1-02/V1-07) |
| D39 | Rejections at execution | Only the customer who created a transfer can execute/cancel it. A temporary failure while re-checking (network, 5xx) raises `TransferRejected(retryable=True)` and keeps it PENDING; a permanent one (account gone, insufficient funds) cancels it. The destination is re-checked just before leg 2; if gone, leg 1 is compensated. Migration steps are atomic (`BEGIN IMMEDIATE`) | A blip right after "yes" must not discard the request; a stranger's session must not touch it; no deposit into a deleted account | Accepted 2026-11-20 (review V1-03/V1-04/V1-05/V1-06) |
