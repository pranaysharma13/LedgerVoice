"""Is this message a clear "yes"? Deterministic, no model involved (D26, D53).

Only a whole message that is one of a fixed list of phrases counts. Normalisation is
deliberately small: lowercase, curly apostrophes made straight, commas dropped, runs of
whitespace collapsed, trailing "." and "!" removed. Nothing else is cleaned up, so
"yes?" (a question), "yes but make it $40", "yesterday", "ok", "sure" and any message
with hidden or unusual characters are not a yes. Being strict costs little: a
cancelled transfer moves no money and the user can simply ask again.
"""

from __future__ import annotations

import re

YES_PHRASES: frozenset[str] = frozenset(
    {
        "yes",
        "yes please",
        "yeah",
        "yep",
        "yup",
        "confirm",
        "confirmed",
        "i confirm",
        "yes confirm",
        "do it",
        "yes do it",
        "please do",
        "go ahead",
        "yes go ahead",
        "go ahead please",
        "that's right",
        "yes that's right",
        "that is right",
        "correct",
        "yes correct",
        "yes that's correct",
        "that's correct",
    }
)
MAX_LENGTH = 40  # longer than any phrase above; anything longer is not a plain yes

_WHITESPACE = re.compile(r"\s+")
_TRAILING = re.compile(r"[.!]+$")


def normalize(text: str) -> str:
    lowered = text.lower().replace("’", "'").replace(",", " ")
    return _TRAILING.sub("", _WHITESPACE.sub(" ", lowered).strip()).strip()


def is_clear_yes(text: str) -> bool:
    if len(text) > MAX_LENGTH:
        return False
    return normalize(text) in YES_PHRASES
