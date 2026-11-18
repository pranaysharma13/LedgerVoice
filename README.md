# LedgerVoice

Talk to your bank naturally: "What did I spend on food last week?" "Move fifty bucks to savings."
A real-time voice banking agent built with Claude, Cartesia, Moss and the Capital One Nessie sandbox.

> Status: **M0 — setup & API checks.** Architecture diagram, results and demo arrive with later milestones.

## Quick start
    cp .env.example .env      # add your keys
    make setup
    make test                 # offline
    make smoke                # one real call per API

Run all commands from the repo root; .env, .ledger/ and .artifacts/ are resolved relative to it.

## Layout
    packages/nessie-tools/    typed Nessie client (fake backend arrives in M1)
    src/ledgervoice/          app code: config, cost ledger, smoke checks
    tests/                    unit tests (no network)
    docs/decisions.md         decisions log

## Safety
Nessie is a mock bank; no real money moves. Money-moving actions require explicit,
code-enforced confirmation (see docs/decisions.md, D8).

## License
MIT
