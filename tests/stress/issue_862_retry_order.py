"""Explicit #862 stress run; the filename keeps it out of routine collection."""

import os
import random

import pytest

from tests.unit import test_bugs_152_167_159 as retry_tests

TARGET = ("test", None, "0", True)
CASES = [TARGET] * 200
if os.environ.get("AGENT_CREW_862_RANDOM_ORDER") == "1":
    # Mix in the other policy variants so the target runs after different
    # preceding cases, not merely in a shuffled list of identical cases.
    CASES += [
        ("implement", None, None, True),
        ("implement", None, "1", True),
        ("implement", None, "0", False),
        ("implement", {"reason": "infrastructure_failure"}, "0", True),
        ("review", None, "0", True),
    ] * 4
    random.Random(862).shuffle(CASES)


@pytest.mark.parametrize("case", CASES)
def test_retry_policy_stress(tmp_db, tmp_path, monkeypatch, case):
    retry_tests.test_auto_retry_skips_agent_reported_implement_failure(
        tmp_db, tmp_path, monkeypatch, *case)
