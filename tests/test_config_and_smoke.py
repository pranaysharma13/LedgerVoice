import pytest
from pydantic import ValidationError

from ledgervoice.config import MissingConfigError, Settings
from ledgervoice.costs import CostLedger
from ledgervoice.smoke.checks import (
    CHECKS,
    EXPECTED_AMOUNTS,
    extract_amounts,
    pcm_to_wav,
    run_check,
    wav_duration_seconds,
)


def bare_settings(**overrides) -> Settings:
    # _env_file=None: tests must not read the developer's real .env
    return Settings(_env_file=None, **overrides)


def test_require_lists_all_missing_keys(monkeypatch):
    for var in ("NESSIE_API_KEY", "MOSS_PROJECT_ID", "MOSS_PROJECT_KEY"):
        monkeypatch.delenv(var, raising=False)
    s = bare_settings()
    with pytest.raises(MissingConfigError) as info:
        s.require("moss_project_id", "moss_project_key")
    assert "MOSS_PROJECT_ID" in str(info.value) and "MOSS_PROJECT_KEY" in str(info.value)


def test_scrub_removes_every_secret():
    s = bare_settings(anthropic_api_key="sk-ant-xyz", nessie_api_key="nes123")
    assert s.scrub("a sk-ant-xyz b nes123 c") == "a *** b *** c"
    assert "sk-ant-xyz" not in repr(s)  # SecretStr hides values in repr


def test_missing_key_is_reported_as_skip_not_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("NESSIE_API_KEY", raising=False)
    s = bare_settings(cost_ledger_path=tmp_path / "c.jsonl")
    result = run_check("nessie", s, CostLedger(s.cost_ledger_path))
    assert not result.ok and result.summary.startswith("skipped")


def test_extract_amounts_digits_in_order():
    assert extract_amounts("Please move $50 from checking to savings, not $15.") == [50, 15]


def test_extract_amounts_words_in_order():
    assert extract_amounts("move fifty dollars, not fifteen") == [50, 15]


def test_extract_amounts_detects_swapped_numbers():
    swapped = extract_amounts("move fifteen dollars, not fifty")
    assert swapped == [15, 50]
    assert swapped != EXPECTED_AMOUNTS


def test_extract_amounts_compounds_and_thousands():
    assert extract_amounts("fifty-five") == [55]
    assert extract_amounts("$1,200") == [1200]


def test_run_check_scrubs_secrets_from_errors(tmp_path, monkeypatch):
    secret = "fake-nessie-secret"
    s = bare_settings(nessie_api_key=secret, cost_ledger_path=tmp_path / "c.jsonl")

    def boom(settings, ledger):
        raise RuntimeError(f"request failed with key {secret}")

    monkeypatch.setitem(CHECKS, "boom", boom)
    result = run_check("boom", s, CostLedger(s.cost_ledger_path))
    assert result.ok is False
    assert secret not in result.summary


def test_settings_rejects_plain_http_nessie_url():
    with pytest.raises(ValidationError, match="https://"):
        bare_settings(nessie_base_url="http://api.nessieisreal.com")


def test_pcm_to_wav_header_matches_data():
    pcm = b"\x00\x00" * 16_000  # 1 second of 16 kHz mono silence
    wav = pcm_to_wav(pcm)
    assert wav[:4] == b"RIFF"
    assert wav_duration_seconds(wav) == 1.0
