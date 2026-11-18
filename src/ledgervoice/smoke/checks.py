"""One function per external API. Each returns a CheckResult and never raises.

The checks double as the first measurements of the project: Nessie round-trip time
(sets the filler-phrase threshold in M4), Cartesia's handling of "fifty" vs "fifteen"
(the transfer-safety risk), and Moss query latency (the sub-10 ms claim).
"""

from __future__ import annotations

import asyncio
import io
import re
import statistics
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ledgervoice.config import MissingConfigError, Settings
from ledgervoice.costs import CostLedger

# Contains both "fifty" and "fifteen" on purpose: number confusion is the core STT risk.
SMOKE_PHRASE = "Please move fifty dollars from checking to savings, not fifteen."
MOSS_INDEX = "ledgervoice-smoke"
MOSS_DOCS = [
    {"id": "m-coffee", "text": "Blue Door Coffee, espresso bar on Main Street, downtown"},
    {"id": "m-grocer", "text": "Harvest Market, grocery store in the Westside neighborhood"},
    {"id": "m-gas", "text": "QuickFuel gas station off Highway 9 near the office park"},
    {"id": "m-pizza", "text": "Luigi's Pizzeria, takeout pizza on Elm Avenue"},
    {"id": "m-gym", "text": "IronWorks Gym, monthly membership"},
    {"id": "m-books", "text": "Chapter House Books, independent bookstore downtown"},
]


@dataclass
class CheckResult:
    name: str
    ok: bool
    summary: str
    notes: list[str] = field(default_factory=list)


def ms_since(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def p50(values: list[float]) -> float:
    return statistics.median(values)


def normalize_words(text: str) -> set[str]:
    """Lowercase, drop punctuation and '$', split on whitespace. "$50," -> "50"."""
    return set(re.sub(r"[^a-z0-9\s]", " ", text.lower()).split())


def wav_duration_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / float(w.getframerate())


def cartesia_headers(s: Settings) -> dict[str, str]:
    assert s.cartesia_api_key is not None
    return {
        "Authorization": f"Bearer {s.cartesia_api_key.get_secret_value()}",
        "Cartesia-Version": s.cartesia_version,
    }


# --- Nessie ----------------------------------------------------------------------
def check_nessie(s: Settings, ledger: CostLedger) -> CheckResult:
    from nessie_tools import NessieClient

    s.require("nessie_api_key")
    assert s.nessie_api_key is not None
    latencies: list[float] = []
    with NessieClient(s.nessie_api_key.get_secret_value(), base_url=s.nessie_base_url) as c:
        for _ in range(5):
            t0 = time.perf_counter()
            customers = c.get_customers()
            latencies.append(ms_since(t0))
    ledger.record("nessie", "GET /customers", {"requests": 5}, latency_ms=p50(latencies))
    notes = []
    if s.nessie_base_url.startswith("http://"):
        notes.append("base URL is plain HTTP: the key travels unencrypted (sandbox key only)")
    return CheckResult(
        "nessie",
        True,
        f"{len(customers)} customers visible; GET p50 {p50(latencies):.0f} ms, "
        f"max {max(latencies):.0f} ms over 5 calls",
        notes,
    )


# --- Claude ----------------------------------------------------------------------
def check_claude(s: Settings, ledger: CostLedger) -> CheckResult:
    import anthropic

    s.require("anthropic_api_key")
    assert s.anthropic_api_key is not None
    client = anthropic.Anthropic(api_key=s.anthropic_api_key.get_secret_value())
    tools = [
        {
            "name": "get_account_balance",
            "description": "Get the current balance of one of the user's accounts.",
            "input_schema": {
                "type": "object",
                "properties": {"account": {"type": "string", "enum": ["checking", "savings"]}},
                "required": ["account"],
            },
        }
    ]
    t0 = time.perf_counter()
    resp = client.messages.create(
        model=s.claude_model,
        max_tokens=256,
        tools=tools,
        messages=[{"role": "user", "content": "What's my checking balance?"}],
        # Sent via extra_body so this works whether or not the installed SDK version
        # has a typed `output_config` argument yet.
        extra_body={"output_config": {"effort": s.claude_effort}},
    )
    latency = ms_since(t0)
    u = resp.usage
    ledger.record(
        "anthropic",
        "messages.create",
        {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        },
        latency_ms=latency,
        model=s.claude_model,
    )
    calls = [b for b in resp.content if b.type == "tool_use"]
    ok = (
        resp.stop_reason == "tool_use"
        and len(calls) == 1
        and calls[0].name == "get_account_balance"
        and calls[0].input.get("account") == "checking"
    )
    detail = f"tool_use {calls[0].name}({calls[0].input})" if calls else f"stop={resp.stop_reason}"
    return CheckResult(
        "claude",
        ok,
        f"{s.claude_model}: {detail}; {latency:.0f} ms total (non-streaming, includes network)",
    )


# --- Cartesia TTS ----------------------------------------------------------------
def check_cartesia_tts(s: Settings, ledger: CostLedger) -> CheckResult:
    s.require("cartesia_api_key")
    body = {
        "model_id": s.cartesia_tts_model,
        "transcript": SMOKE_PHRASE,
        "voice": {"id": s.cartesia_voice_id},
        # 16 kHz mono PCM: what STT models expect, and a sensible size for tests.
        "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 16000},
        "language": "en",
    }
    t0 = time.perf_counter()
    resp = httpx.post(
        f"{s.cartesia_base_url}/tts/bytes", headers=cartesia_headers(s), json=body, timeout=30
    )
    latency = ms_since(t0)
    if resp.is_error:
        return CheckResult("cartesia_tts", False, f"HTTP {resp.status_code}: {resp.text[:300]}")
    audio = resp.content
    if audio[:4] != b"RIFF":
        return CheckResult("cartesia_tts", False, f"response is not a WAV file ({audio[:16]!r})")
    duration = wav_duration_seconds(audio)
    out = s.artifacts_dir / "smoke_tts.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio)
    ledger.record(
        "cartesia", "POST /tts/bytes", {"tts_characters": len(SMOKE_PHRASE)}, latency_ms=latency
    )
    return CheckResult(
        "cartesia_tts",
        True,
        f"{duration:.2f} s of audio in {latency:.0f} ms (full file, not time-to-first-byte); "
        f"saved {out}",
    )


# --- Cartesia STT ----------------------------------------------------------------
def check_cartesia_stt(s: Settings, ledger: CostLedger) -> CheckResult:
    s.require("cartesia_api_key")
    wav_path: Path = s.artifacts_dir / "smoke_tts.wav"
    if not wav_path.exists():
        return CheckResult("cartesia_stt", False, f"needs {wav_path}; run the tts check first")
    audio = wav_path.read_bytes()
    t0 = time.perf_counter()
    resp = httpx.post(
        f"{s.cartesia_base_url}/stt",
        headers=cartesia_headers(s),
        files={"file": (wav_path.name, audio, "audio/wav")},
        data={"model": s.cartesia_stt_model, "language": "en"},
        timeout=60,
    )
    latency = ms_since(t0)
    if resp.is_error:
        return CheckResult("cartesia_stt", False, f"HTTP {resp.status_code}: {resp.text[:300]}")
    text = resp.json().get("text", "")
    ledger.record(
        "cartesia",
        "POST /stt",
        {"stt_audio_seconds": round(wav_duration_seconds(audio), 2)},
        latency_ms=latency,
    )
    words = normalize_words(text)
    has_fifty = bool(words & {"fifty", "50"})
    has_fifteen = bool(words & {"fifteen", "15"})
    notes = []
    if words & {"50", "15"}:
        notes.append("transcript uses digits; amount parsing in M1 must accept both forms")
    return CheckResult(
        "cartesia_stt",
        has_fifty and has_fifteen,
        f"{latency:.0f} ms; transcript: {text!r} (fifty={has_fifty}, fifteen={has_fifteen})",
        notes,
    )


# --- Moss ------------------------------------------------------------------------
async def _moss(s: Settings, ledger: CostLedger) -> CheckResult:
    from moss import MossClient, QueryOptions

    assert s.moss_project_key is not None
    client = MossClient(s.moss_project_id, s.moss_project_key.get_secret_value())
    notes: list[str] = []
    try:
        await client.create_index(MOSS_INDEX, MOSS_DOCS)
    except Exception as exc:  # most likely "already exists" on a re-run; verify in M0
        notes.append(f"create_index raised {type(exc).__name__}: {s.scrub(str(exc))[:150]}")
    await client.load_index(MOSS_INDEX)

    query = "that coffee place downtown"
    latencies: list[float] = []
    results = None
    for _ in range(20):
        t0 = time.perf_counter()
        results = await client.query(MOSS_INDEX, query, QueryOptions(top_k=3))
        latencies.append(ms_since(t0))
    assert results is not None
    ledger.record("moss", "query", {"queries": 20}, latency_ms=p50(latencies))

    if not results.docs:
        return CheckResult("moss", False, "query returned no documents", notes)
    top = results.docs[0]
    top_id = getattr(top, "id", None)
    ok = top_id == "m-coffee" or "Blue Door Coffee" in top.text
    server_ms = getattr(results, "time_taken_ms", None)
    return CheckResult(
        "moss",
        ok,
        f"top hit {top.text!r} (score {top.score:.3f}); client-side p50 {p50(latencies):.1f} ms, "
        f"p95 {sorted(latencies)[18]:.1f} ms over 20 queries; server-reported {server_ms} ms",
        notes,
    )


def check_moss(s: Settings, ledger: CostLedger) -> CheckResult:
    s.require("moss_project_id", "moss_project_key")
    return asyncio.run(_moss(s, ledger))


# Order matters: stt reuses the audio file the tts check writes.
CHECKS: dict[str, Callable[[Settings, CostLedger], CheckResult]] = {
    "nessie": check_nessie,
    "claude": check_claude,
    "tts": check_cartesia_tts,
    "stt": check_cartesia_stt,
    "moss": check_moss,
}


def run_check(name: str, s: Settings, ledger: CostLedger) -> CheckResult:
    try:
        return CHECKS[name](s, ledger)
    except MissingConfigError as exc:
        return CheckResult(name, False, f"skipped: {exc}")
    except Exception as exc:  # report, never crash the whole run
        return CheckResult(name, False, s.scrub(f"{type(exc).__name__}: {exc}")[:400])
