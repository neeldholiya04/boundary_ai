"""Deterministic fake credentials and PII for dataset placeholders.

Datasets write `{{fake:<kind>}}` instead of key-shaped strings, so the repo never contains
anything a secret scanner (GitHub push protection, gitleaks) would flag. At load time each
placeholder is expanded into a realistic value of the right format. The value depends only
on the record id and the placeholder's position, so every eval run sees identical text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import string
from collections.abc import Callable

PLACEHOLDER = re.compile(r"\{\{fake:([a-z0-9_]+)\}\}")

_ALNUM = string.ascii_letters + string.digits
_UPPER32 = string.ascii_uppercase + "234567"
_B64 = string.ascii_letters + string.digits + "+/"
_B64URL = string.ascii_letters + string.digits + "-_"
_WORDS = ["blue", "fox", "river", "stone", "maple", "otter", "cloud"]


def _pick(rng: random.Random, alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def _mixed(rng: random.Random, n: int) -> str:
    """Random alphanumerics with at least one upper, lower and digit, like a real issued key."""
    while True:
        value = _pick(rng, _ALNUM, n)
        if (
            any(c.isupper() for c in value)
            and any(c.islower() for c in value)
            and any(c.isdigit() for c in value)
        ):
            return value


def _b64url_json(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode()


def _jwt(rng: random.Random) -> str:
    header = _b64url_json({"alg": "HS256", "typ": "JWT"})
    payload = _b64url_json(
        {"sub": str(rng.randint(10_000, 99_999)), "iat": 1_760_000_000 + rng.randint(0, 10**6)}
    )
    return f"{header}.{payload}.{_pick(rng, _B64URL, 43)}"


def _private_key(rng: random.Random) -> str:
    body = "\n".join(_pick(rng, _B64, 64) for _ in range(8))
    return f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"


def _ssn(rng: random.Random) -> str:
    area = rng.choice([a for a in range(100, 666)])
    return f"{area:03d}-{rng.randint(1, 99):02d}-{rng.randint(1, 9999):04d}"


def _iban(rng: random.Random) -> str:
    """German IBAN with valid ISO 7064 mod-97 check digits (detectors such as Presidio verify them)."""
    bban = _pick(rng, string.digits, 18)  # 8-digit bank code + 10-digit account number
    rearranged = bban + "1314" + "00"  # "DE" -> D=13, E=14, then placeholder check digits
    check = 98 - int(rearranged) % 97
    iban = f"DE{check:02d}{bban}"
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


GENERATORS: dict[str, Callable[[random.Random], str]] = {
    "github": lambda r: "ghp_" + _pick(r, _ALNUM, 36),
    "github_pat": lambda r: "github_pat_" + _pick(r, _ALNUM + "_", 82),
    "aws_key_id": lambda r: "AKIA" + _pick(r, _UPPER32, 16),
    "aws_secret": lambda r: _pick(r, _B64, 40),
    "openai": lambda r: "sk-proj-" + _pick(r, _B64URL, 64),
    # Shapes the v1 secrets ruleset missed: a short project key, the legacy 48-char key, other
    # providers' prefixes, and a token from a vendor no rule names.
    "openai_short": lambda r: "sk-proj-" + _mixed(r, 20 + r.randint(0, 12)),
    "openai_legacy": lambda r: "sk-" + _mixed(r, 48),
    # A key typed by hand or made up: a real prefix, then lowercase letters from a few keys. No digit,
    # no capital, low entropy: it passed every stage before v8, and a leaked typo of a real key looks
    # the same.
    "openai_typed": lambda r: (
        "sk-proj-" + _pick(r, r.choice(["asdfjklg", "qwertyui", "zxcvbnmh"]), 30 + r.randint(0, 8))
    ),
    # The third live leak (v8): `sk-ant-` + 15 typed letters, shorter than any real Anthropic key.
    "anthropic_typed": lambda r: "sk-ant-" + _pick(r, r.choice(["asdfjklg", "qwertyui", "zxcvbnmh"]), 15),
    # Formats only the imported gitleaks rules know (no hand-written rule matches them).
    "digitalocean": lambda r: "dop_v1_" + _pick(r, "0123456789abcdef", 64),
    "doppler": lambda r: "dp.pt." + _pick(r, _ALNUM, 43),
    "pulumi": lambda r: "pul-" + _pick(r, "0123456789abcdef", 40),
    "planetscale": lambda r: "pscale_tkn_" + _pick(r, _ALNUM, 43),
    "postman": lambda r: "PMAK-" + _pick(r, "0123456789abcdef", 24) + "-" + _pick(r, "0123456789abcdef", 34),
    # A plain secret only its variable name gives away (DB_PASSWORD=bluefoxriver): lowercase words
    # and two digits, far below any entropy floor.
    "weak_secret": lambda r: "".join(r.choice(_WORDS) for _ in range(3)) + str(r.randint(10, 99)),
    "groq": lambda r: "gsk_" + _mixed(r, 52),
    "huggingface": lambda r: "hf_" + _mixed(r, 34),
    "xai": lambda r: "xai-" + _mixed(r, 80),
    "gitlab": lambda r: "glpat-" + _mixed(r, 20),
    "npm": lambda r: "npm_" + _mixed(r, 36),
    # Unconstrained on purpose: unlike issued keys above, nothing guarantees a vendor token mixes
    # cases and digits, so the generic rules are measured on what they can and can't see.
    "opaque_token": lambda r: _pick(r, _ALNUM, 40),
    # Evasions: a key hidden as base64 (inside an .env line) or spelled out with spaces.
    # The plaintext is a sentence (it has spaces), so only decoding finds the key: a bare key's base64
    # would itself look like a random token to the high-entropy rules.
    "openai_b64": lambda r: base64.b64encode(
        f"export OPENAI_API_KEY=sk-proj-{_mixed(r, 40)} # prod".encode()
    ).decode(),
    "openai_spaced": lambda r: " ".join("sk-proj-" + _mixed(r, 24)),
    "anthropic": lambda r: "sk-ant-api03-" + _pick(r, _B64URL, 93) + "AA",
    "google": lambda r: "AIza" + _pick(r, _B64URL, 35),
    "slack": lambda r: (
        f"xoxb-{r.randint(10**11, 10**12 - 1)}-{r.randint(10**11, 10**12 - 1)}-{_pick(r, _ALNUM, 24)}"
    ),
    "stripe_live": lambda r: "sk_live_" + _pick(r, _ALNUM, 24),
    "stripe_test": lambda r: "sk_test_" + _pick(r, _ALNUM, 24),
    "jwt": _jwt,
    "private_key": _private_key,
    "password": lambda r: _pick(r, _ALNUM, 12) + r.choice("!#%^*") + _pick(r, _ALNUM, 4),
    "ssn": _ssn,
    "iban": _iban,
    # Card networks' published test numbers: valid format, never real accounts.
    "card_visa": lambda r: "4111 1111 1111 1111",
    "card_mc": lambda r: "5555 5555 5555 4444",
    "card_amex": lambda r: "3782 822463 10005",
}


class UnknownPlaceholderError(ValueError):
    pass


def placeholders_in(text: str) -> list[str]:
    return PLACEHOLDER.findall(text)


def expand(text: str, seed: str) -> str:
    """Replace every `{{fake:kind}}` with a deterministic fake value."""
    counter = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal counter
        kind = match.group(1)
        generator = GENERATORS.get(kind)
        if generator is None:
            raise UnknownPlaceholderError(f"unknown placeholder {{{{fake:{kind}}}}}")
        digest = hashlib.sha256(f"{seed}:{counter}:{kind}".encode()).digest()
        counter += 1
        return generator(random.Random(digest))

    return PLACEHOLDER.sub(replace, text)
