import json

from ledgervoice.costs import CostLedger, summarize


def test_record_writes_jsonl_with_estimate(tmp_path):
    path = tmp_path / "costs.jsonl"
    pricing = {("anthropic", "input_tokens"): 1e-6, ("anthropic", "output_tokens"): None}
    ledger = CostLedger(path, run_id="t1", pricing=pricing)

    row = ledger.record("anthropic", "messages.create", {"input_tokens": 1000, "output_tokens": 5})

    assert row["est_usd"] == 0.001
    assert row["unpriced_units"] == ["output_tokens"]
    stored = json.loads(path.read_text().strip())
    assert stored["run_id"] == "t1"
    assert stored["units"] == {"input_tokens": 1000, "output_tokens": 5}


def test_zero_amount_of_unpriced_unit_is_not_flagged(tmp_path):
    ledger = CostLedger(tmp_path / "c.jsonl", pricing={})
    row = ledger.record("moss", "query", {"queries": 0})
    assert row["unpriced_units"] == []


def test_summarize_totals_per_service(tmp_path):
    path = tmp_path / "costs.jsonl"
    ledger = CostLedger(path, pricing={("nessie", "requests"): 0.0})
    ledger.record("nessie", "GET /customers", {"requests": 5})
    ledger.record("nessie", "GET /customers", {"requests": 3})

    totals = summarize(path)

    assert totals["nessie"]["calls"] == 2
    assert totals["nessie"]["units"]["requests"] == 8
    assert summarize(tmp_path / "missing.jsonl") == {}
