"""One function per external API. Each returns a CheckResult and never raises.

The checks double as the first measurements of the project: Nessie round-trip time
(sets the filler-phrase threshold in M4), Cartesia's handling of "fifty" vs "fifteen"
(the transfer-safety risk), and Moss build + query latency.
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
from typing import Any

import httpx

from ledgervoice.config import MissingConfigError, Settings
from ledgervoice.costs import CostLedger

# Contains both "fifty" and "fifteen" on purpose: number confusion is the core STT risk.
SMOKE_PHRASE = "Please move fifty dollars from checking to savings, not fifteen."
EXPECTED_AMOUNTS = [50, 15]  # the amounts in SMOKE_PHRASE, in order
SAMPLE_RATE = 16_000  # Hz, mono, 16-bit: what STT models expect
RUN_STARTED = time.time()  # files older than this come from an earlier run

# v2: the first index build ("ledgervoice-smoke") was started with plain dicts and never
# became visible; a fresh name avoids depending on that stuck build.
MOSS_INDEX = "ledgervoice-smoke-v2"
MOSS_DOCS = [
    ("m-coffee", "Blue Door Coffee, espresso bar on Main Street, downtown"),
    ("m-grocer", "Harvest Market, grocery store in the Westside neighborhood"),
    ("m-gas", "QuickFuel gas station off Highway 9 near the office park"),
    ("m-pizza", "Luigi's Pizzeria, takeout pizza on Elm Avenue"),
    ("m-gym", "IronWorks Gym, monthly membership"),
    ("m-books", "Chapter House Books, independent bookstore downtown"),
]
MOSS_BUILD_TIMEOUT_S = 180  # index builds are asynchronous
MOSS_POLL_INTERVAL_S = 3
N_QUERIES = 20


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


_ONES = {
    word: n
    for n, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
_TENS = {
    word: 10 * n
    for n, word in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)
}
# Digit groups with thousands commas ("1,200"), plain digits, or a word.
_AMOUNT_TOKEN = re.compile(r"\d{1,3}(?:,\d{3})+|\d+|[a-z]+")


def extract_amounts(text: str) -> list[int]:
    """Every amount in `text`, in order: "$1,200" -> 1200, "fifty-five" -> 55.

    Order matters: "fifteen ... not fifty" must not pass as "fifty ... not fifteen".
    Number words cover 0-99; all other words are ignored.
    """
    tokens = _AMOUNT_TOKEN.findall(text.lower().replace("-", " "))
    amounts: list[int] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token[0].isdigit():
            amounts.append(int(token.replace(",", "")))
        elif token in _TENS:
            value = _TENS[token]
            following = tokens[i + 1] if i + 1 < len(tokens) else ""
            if 1 <= _ONES.get(following, 0) <= 9:  # "fifty five" -> 55
                value += _ONES[following]
                i += 1
            amounts.append(value)
        elif token in _ONES:
            amounts.append(_ONES[token])
        i += 1
    return amounts


def pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV header whose sizes match the actual data.

    Why: a streamed WAV from Cartesia can't know its final length up front, so its header
    carries a placeholder size (~4 GB). Building the header ourselves keeps durations,
    cost-ledger audio seconds, and the saved file correct.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


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
    return CheckResult(
        "nessie",
        True,
        f"{len(customers)} customers visible; GET p50 {p50(latencies):.0f} ms, "
        f"max {max(latencies):.0f} ms over 5 calls",
    )


# --- Claude ----------------------------------------------------------------------
def check_claude(s: Settings, ledger: CostLedger) -> CheckResult:
    import anthropic

    s.require("anthropic_api_key")
    assert s.anthropic_api_key is not None
    client = anthropic.Anthropic(
        api_key=s.anthropic_api_key.get_secret_value(), timeout=30.0, max_retries=0
    )
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
    out = s.artifacts_dir / "smoke_tts.wav"
    out.unlink(missing_ok=True)  # never leave stale audio for the stt check
    s.require("cartesia_api_key")
    body = {
        "model_id": s.cartesia_tts_model,
        "transcript": SMOKE_PHRASE,
        "voice": {"id": s.cartesia_voice_id},
        # Raw PCM, not WAV: we write the WAV header ourselves (see pcm_to_wav).
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
        "language": "en",
    }
    t0 = time.perf_counter()
    resp = httpx.post(
        f"{s.cartesia_base_url}/tts/bytes", headers=cartesia_headers(s), json=body, timeout=30
    )
    latency = ms_since(t0)
    if resp.is_error:
        return CheckResult(
            "cartesia_tts", False, f"HTTP {resp.status_code}: {s.scrub(resp.text)[:300]}"
        )
    pcm = resp.content
    if pcm.startswith(b"{"):
        return CheckResult("cartesia_tts", False, "response looks like JSON, not PCM")
    if not pcm or len(pcm) % 2:
        return CheckResult("cartesia_tts", False, f"unexpected PCM payload ({len(pcm)} bytes)")
    duration = len(pcm) / (2 * SAMPLE_RATE)
    if not 1.0 <= duration <= 15.0:
        return CheckResult("cartesia_tts", False, f"implausible audio duration ({duration:.2f} s)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pcm_to_wav(pcm))
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
    if wav_path.stat().st_mtime < RUN_STARTED:
        return CheckResult(
            "cartesia_stt",
            False,
            "audio is from an earlier run; run the tts and stt checks together",
        )
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
        return CheckResult(
            "cartesia_stt", False, f"HTTP {resp.status_code}: {s.scrub(resp.text)[:300]}"
        )
    text = resp.json().get("text", "")
    ledger.record(
        "cartesia",
        "POST /stt",
        {"stt_audio_seconds": round(wav_duration_seconds(audio), 2)},
        latency_ms=latency,
    )
    amounts = extract_amounts(text)
    notes = []
    if re.search(r"\d", text):
        notes.append("transcript uses digits; amount parsing in M1 must accept both forms")
    return CheckResult(
        "cartesia_stt",
        amounts == EXPECTED_AMOUNTS,
        f"{latency:.0f} ms; transcript: {text!r}; amounts {amounts} "
        f"(expected {EXPECTED_AMOUNTS}, in order)",
        notes,
    )


# --- Moss ------------------------------------------------------------------------
async def _find_index(client: Any, name: str) -> Any:
    for idx in await client.list_indexes():
        if getattr(idx, "name", None) == name:
            return idx
    return None


def _status(idx: Any) -> str:
    return str(getattr(idx, "status", "")) if idx is not None else "not listed yet"


async def _moss(s: Settings, ledger: CostLedger) -> CheckResult:
    from moss import DocumentInfo, MossClient, QueryOptions

    assert s.moss_project_key is not None
    client = MossClient(s.moss_project_id, s.moss_project_key.get_secret_value())
    notes: list[str] = []
    try:
        # 1. Build the index only if it isn't already Ready (builds are asynchronous).
        idx = await _find_index(client, MOSS_INDEX)
        if _status(idx).lower() == "ready":
            notes.append(f"index {MOSS_INDEX} already built; reusing it")
        else:
            t_build = time.perf_counter()
            if idx is None:
                docs = [DocumentInfo(id=doc_id, text=text) for doc_id, text in MOSS_DOCS]
                try:
                    # wait=False: our polling loop below enforces MOSS_BUILD_TIMEOUT_S.
                    await client.create_index(MOSS_INDEX, docs, wait=False)
                except Exception as exc:
                    msg = f"create_index: {type(exc).__name__}: {s.scrub(str(exc))}"
                    if "already running" not in msg.lower():
                        return CheckResult("moss", False, msg[:300], notes)
                    notes.append(msg[:150])  # a build from an earlier run is still going
            deadline = time.monotonic() + MOSS_BUILD_TIMEOUT_S
            while _status(idx).lower() != "ready":
                if _status(idx).lower() in {"failed", "error"}:
                    return CheckResult("moss", False, f"index build ended: {_status(idx)}", notes)
                if time.monotonic() > deadline:
                    return CheckResult(
                        "moss",
                        False,
                        f"index not ready after {MOSS_BUILD_TIMEOUT_S} s "
                        f"(last status: {_status(idx)})",
                        notes,
                    )
                await asyncio.sleep(MOSS_POLL_INTERVAL_S)
                idx = await _find_index(client, MOSS_INDEX)
            notes.append(
                f"index build took ~{ms_since(t_build) / 1000:.0f} s "
                f"(±{MOSS_POLL_INTERVAL_S} s polling granularity)"
            )

        # 2. Load (downloads the index for local querying), then query.
        t_load = time.perf_counter()
        await client.load_index(MOSS_INDEX)
        notes.append(f"load_index took {ms_since(t_load):.0f} ms")

        query = "that coffee place downtown"
        latencies: list[float] = []
        results = None
        for _ in range(N_QUERIES):
            t0 = time.perf_counter()
            results = await client.query(MOSS_INDEX, query, QueryOptions(top_k=3))
            latencies.append(ms_since(t0))
        assert results is not None
        ledger.record("moss", "query", {"queries": N_QUERIES}, latency_ms=p50(latencies))

        if not results.docs:
            return CheckResult("moss", False, "query returned no documents", notes)
        top = results.docs[0]
        ok = getattr(top, "id", None) == "m-coffee" or "Blue Door Coffee" in top.text
        server_ms = getattr(results, "time_taken_ms", None)
        p95 = statistics.quantiles(latencies, n=20, method="inclusive")[-1]
        return CheckResult(
            "moss",
            ok,
            f"top hit {top.text!r} (score {top.score:.3f}); client-side p50 "
            f"{p50(latencies):.1f} ms, p95 {p95:.1f} ms over {N_QUERIES} queries; "
            f"server-reported {server_ms} ms",
            notes,
        )
    except Exception as exc:  # keep the notes gathered so far
        return CheckResult("moss", False, s.scrub(f"{type(exc).__name__}: {exc}")[:300], notes)
    finally:
        if hasattr(client, "close"):
            await client.close()


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
