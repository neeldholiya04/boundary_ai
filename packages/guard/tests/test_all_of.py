"""`all_of`: a second detector must confirm, unless the first is all but certain; a confirmer that
errors leaves the decision to the first."""

from __future__ import annotations

import pytest

from boundary_guard.core.detector import Detector
from boundary_guard.core.types import CheckContext, Detection
from boundary_guard.detectors.all_of import AllOfDetector


class Fixed(Detector):
    type_name = "fixed"

    def __init__(self, score: float, fires: bool, error: bool = False) -> None:
        self.score, self.fires, self.error = score, fires, error

    async def detect(self, text: str, ctx: CheckContext) -> Detection:
        if self.error:
            raise RuntimeError("model busy")
        return Detection(triggered=self.fires, score=self.score, reasons=[f"p={self.score}"])


@pytest.mark.parametrize(
    ("primary", "confirmer", "alone", "fires"),
    [
        (Fixed(0.9, True), Fixed(0.9, True), None, True),  # both agree
        (Fixed(0.9, True), Fixed(0.1, False), None, False),  # not confirmed
        (Fixed(0.1, False), Fixed(0.9, True), None, False),  # the confirmer alone never fires
        (Fixed(0.9995, True), Fixed(0.1, False), 0.999, True),  # all but certain: decides alone
        (Fixed(0.998, True), Fixed(0.1, False), 0.999, False),
        (Fixed(0.9, True), Fixed(0, False, error=True), None, True),  # confirmer errored: primary decides
        (Fixed(0.1, False), Fixed(0, False, error=True), None, False),
    ],
)
async def test_all_of_verdicts(primary, confirmer, alone, fires):
    detection = await AllOfDetector([primary, confirmer], primary_alone=alone).detect("x", CheckContext())
    assert detection.triggered is fires
    assert detection.score == primary.score


async def test_a_primary_error_follows_the_policy_on_error():
    with pytest.raises(RuntimeError):
        await AllOfDetector([Fixed(0, False, error=True), Fixed(0.9, True)]).detect("x", CheckContext())


def test_all_of_has_no_single_threshold_to_tune():
    assert AllOfDetector([Fixed(0.9, True), Fixed(0.9, True)]).threshold is None
