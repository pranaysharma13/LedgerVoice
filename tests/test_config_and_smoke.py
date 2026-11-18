import pytest

from ledgervoice.config import MissingConfigError, Settings
from ledgervoice.costs import CostLedger
from ledgervoice.smoke.checks import normalize_words, pcm_to_wav, run_check, wav_duration_seconds


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


def test_normalize_words_handles_digits_and_punctuation():
    assert {"50", "fifteen"} <= normalize_words("Move $50, not fifteen.")


def test_pcm_to_wav_header_matches_data():
    pcm = b"\x00\x00" * 16_000  # 1 second of 16 kHz mono silence
    wav = pcm_to_wav(pcm)
    assert wav[:4] == b"RIFF"
    assert wav_duration_seconds(wav) == 1.0
