# M1e checklist: 10 questions with known answers

The M1e definition of done is "the conversation works; 10 manual questions answered
correctly". These questions have answers computed from the seed's ground truth
(`GroundTruth` for seed 42 dated 2026-11-24, persona Priya), so "correct" can be
checked, not judged by feel.

Run the chat against the in-memory demo bank with that date:

```bash
make chat ARGS="--anchor 2026-11-24 --timing"
```

Ask the questions in order (question 10 changes the checking balance).

| # | Ask | Correct answer |
|---|-----|----------------|
| 1 | What's my checking balance? | $1,496 |
| 2 | How much do I owe on my credit card? | $235 |
| 3 | How much do I have in savings? | $1,300 |
| 4 | How much did I spend in the last 30 days? | $1,245 across 42 purchases (Oct 25 to Nov 23) |
| 5 | How much did I spend on dining in the last 30 days? | $129 (8 purchases) |
| 6 | What did I spend the most on last month? | Housing, $750 (October) |
| 7 | How much money came in last month? | $1,490 (October) |
| 8 | Which bills are coming up? | Parkside Apartments $750 on Dec 1, Pulse Mobile $35 on Dec 12, Brightline Electric $40 on Dec 18 |
| 9 | How much did I spend at Daily Grind in the last 90 days? | $120 (21 purchases) |
| 10 | Move $50 from checking to savings. Then answer `yes`, then ask: What's my checking balance now? | The app (not the model) shows "You're moving $50 from checking ending in … to savings ending in …"; after `yes`: "Done. I moved $50 …"; then $1,446 |

Date rules the agent is given (system prompt): "the last N days" is the N days
before today, not counting today; "last month" is the whole previous calendar month.
If the agent picks a different range, it should say which one it used. A reply that
states a different range and a correct total for that range is a prompt issue to
note, not a wrong number.

Safety checks (not counted in the 10, but worth trying):
- "Move $5,000 to savings" is refused (over the $2,000 limit).
- After a read-back, answer "yes but make it 60": the transfer is cancelled and
  nothing moves; the agent can propose $60 as a new transfer.
- "Pay $300 to my credit card" is refused: the card only owes $235.
- Typing "yes" when nothing is pending does nothing special; it goes to the model.

With `--timing`, each turn ends with its reason, the number of model calls and the
time to first output. The cost of every call is in `.ledger/costs.jsonl`
(`make costs` prints totals).
