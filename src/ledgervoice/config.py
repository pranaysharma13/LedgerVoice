"""Central settings, loaded from environment variables and `.env`.

Why pydantic-settings: one typed object instead of `os.environ[...]` scattered around
the code; secrets are `SecretStr`, so printing the settings object never leaks a key.
All keys are optional here so each smoke check can report exactly which key is missing
instead of the whole program crashing at import time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MissingConfigError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Anthropic
    anthropic_api_key: SecretStr | None = None
    claude_model: str = "claude-haiku-5-5"
    claude_effort: str = "low"

    # Cartesia
    cartesia_api_key: SecretStr | None = None
    cartesia_base_url: str = "https://api.cartesia.ai"
    # Cartesia requires a dated version header. The TTS bytes reference lists 2026-08-14.
    # If an endpoint rejects it, the error says which values it accepts; change it in .env.
    cartesia_version: str = "2026-08-14"
    cartesia_tts_model: str = "sonic-3.6"
    cartesia_stt_model: str = "ink-whisper"  # batch /stt only accepts the ink-whisper family
    cartesia_voice_id: str = "47c38ca4-5f35-497b-b1a3-415245fb35e1"  # "Daniel", en-US

    # Nessie
    nessie_api_key: SecretStr | None = None
    nessie_base_url: str = "https://api.nessieisreal.com"  # plain HTTP is refused

    # Moss
    moss_project_id: str | None = None
    moss_project_key: SecretStr | None = None

    # Local paths
    cost_ledger_path: Path = Path(".ledger/costs.jsonl")
    artifacts_dir: Path = Path(".artifacts")

    def require(self, *names: str) -> None:
        """Raise a readable error listing every missing setting (by env var name)."""
        missing = [n.upper() for n in names if not getattr(self, n)]
        if missing:
            raise MissingConfigError(f"missing {', '.join(missing)} (set in .env)")

    def secret_values(self) -> list[str]:
        """Every configured secret, so error messages can be scrubbed before printing."""
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                values.append(value.get_secret_value())
        return values

    def scrub(self, text: str) -> str:
        for secret in self.secret_values():
            text = text.replace(secret, "***")
        return text


@lru_cache
def get_settings() -> Settings:
    return Settings()
