"""Open the live Nessie bank from settings (shared by the seed CLI and the chat)."""

from __future__ import annotations

from collections.abc import Callable

from nessie_tools import BankBackend


def open_live_bank() -> tuple[BankBackend, str, Callable[[], None]]:
    """(backend, server url, close). Needs NESSIE_API_KEY in .env."""
    from nessie_tools import NessieBackend, NessieClient

    from ledgervoice.config import get_settings

    settings = get_settings()
    settings.require("nessie_api_key")
    assert settings.nessie_api_key is not None
    client = NessieClient(
        settings.nessie_api_key.get_secret_value(), base_url=settings.nessie_base_url
    )
    return NessieBackend(client), settings.nessie_base_url, client.close
