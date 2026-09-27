from __future__ import annotations

import pytest

from boundary_eval.fakes import GENERATORS, UnknownPlaceholderError, expand, placeholders_in
from boundary_guard import CheckContext


def test_expansion_is_deterministic_per_seed():
    text = "{{fake:github}} and {{fake:github}}"
    first, second = expand(text, "r1"), expand(text, "r1")
    assert first == second
    a, b = first.split(" and ")
    assert a != b  # position matters
    assert expand(text, "r2") != first


def test_placeholders_in():
    assert placeholders_in("x {{fake:ssn}} y {{fake:jwt}}") == ["ssn", "jwt"]


def test_unknown_kind_raises():
    with pytest.raises(UnknownPlaceholderError):
        expand("{{fake:nope}}", "r")


@pytest.mark.parametrize(
    "kind",
    ["github", "aws_key_id", "stripe_live", "private_key", "jwt", "openai", "anthropic", "google", "slack"],
)
async def test_generated_secrets_are_caught_by_shipped_ruleset(kind):
    from pathlib import Path

    from boundary_guard.detectors.regex_rules import RegexRulesDetector

    rules = Path(__file__).resolve().parents[3] / "policies" / "rules" / "secrets.v1.yaml"
    detector = RegexRulesDetector(rules)
    value = expand(f"{{{{fake:{kind}}}}}", "seed")
    assert (await detector.detect(f"value: {value}", CheckContext())).triggered, value


async def test_stripe_test_keys_are_not_secrets():
    from pathlib import Path

    from boundary_guard.detectors.regex_rules import RegexRulesDetector

    rules = Path(__file__).resolve().parents[3] / "policies" / "rules" / "secrets.v1.yaml"
    value = expand("{{fake:stripe_test}}", "seed")
    assert not (await RegexRulesDetector(rules).detect(value, CheckContext())).triggered


def test_every_generator_produces_text():
    for kind in GENERATORS:
        assert expand(f"{{{{fake:{kind}}}}}", "s")


def test_iban_has_valid_check_digits():
    for seed in ("a", "b", "c", "d"):
        iban = expand("{{fake:iban}}", seed).replace(" ", "")
        assert iban.startswith("DE") and len(iban) == 22
        rearranged = iban[4:] + iban[:4]
        numeric = "".join(str(int(ch, 36)) for ch in rearranged)
        assert int(numeric) % 97 == 1
