"""Is this message a clear "yes"? Deterministic, no model involved (D26, D53).

Only a whole message that is one of a fixed list of phrases counts. A message may
contain only ASCII letters, spaces, tabs, line breaks, straight or curly apostrophes,
commas, "." and "!"; any other character (control or format characters, non-breaking
or other Unicode spaces, look-alike letters, digits, "?") means it is not a yes
(R2-13). Normalisation is deliberately small: lowercase, curly apostrophes made
straight, commas dropped, runs of spaces/tabs/line breaks collapsed, trailing "." and
"!" removed. So "yes?" (a question), "yes but make it $40", "yesterday", "ok" and
"sure" are not a yes. Being strict costs little: a cancelled transfer moves no money
and the user can simply ask again.
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

_ALLOWED = re.compile("[A-Za-z' \t\r\n,.!\u2019]*")  # checked on the raw text
_SPACES = re.compile("[ \t\r\n]+")
_TRAILING = re.compile("[.!]+$")


def normalize(text: str) -> str:
    lowered = text.lower().replace("\u2019", "'").replace(",", " ")
    return _TRAILING.sub("", _SPACES.sub(" ", lowered).strip(" ")).strip(" ")


def is_clear_yes(text: object) -> bool:
    if not isinstance(text, str) or len(text) > MAX_LENGTH:
        return False
    if not _ALLOWED.fullmatch(text):
        return False
    return normalize(text) in YES_PHRASES
