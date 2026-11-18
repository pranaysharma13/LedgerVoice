"""nessie-tools: a small, typed wrapper around the Capital One Nessie sandbox API.

M0 scope: a thin HTTP client with safe error messages (the API key travels in the
query string, so it must never appear in logs or exceptions). Typed models, LLM tool
schemas and the in-memory fake arrive in M1.
"""

from nessie_tools.client import DEFAULT_BASE_URL, NessieClient, NessieError, redact_key

__all__ = ["DEFAULT_BASE_URL", "NessieClient", "NessieError", "redact_key"]
