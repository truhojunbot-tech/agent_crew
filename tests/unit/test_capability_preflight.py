"""alfred#51 E4 adapter stub: agent_crew -> fleet capability registry CLI.

stdlib unittest (pytest also collects it). No network, no alfred import.
The optional live-contract test runs only when ALFRED_CAPABILITY_CLI points at
an alfred checkout's ``tools/contract_registry.py``.
"""
import json
import os
import subprocess
import sys
import unittest

from agent_crew import capability_preflight as cp


def fake(rc=0, out=None, raise_=None, record=None):
    def run(argv, **kw):
        if record is not None:
            record.append((argv, kw))
        if raise_:
            raise raise_
        return subprocess.CompletedProcess(argv, rc, stdout=out if isinstance(out, str) else json.dumps(out), stderr="")
    return run


ALLOW = {"schema": "capability-admission/v2", "decision": "ALLOW", "blocked": False, "warn": False,
         "mode": "enforce", "raw_decision": "ALLOW", "reason": "NO_MATCH", "matches": [],
         "registry_status": "OK", "registry_source": "governance/capability_registry.json"}
STOP = {"schema": "capability-admission/v2", "decision": "STOP_AND_REVIEW", "blocked": True, "warn": True,
        "mode": "enforce", "raw_decision": "STOP_AND_REVIEW", "reason": "OWNER_CONFLICT",
        "registry_status": "OK", "registry_source": "x",
        "matches": [{"capability_id": "quota-core.tokenomics-shadow-policy-v1", "kind": "OWNER_CONFLICT"}]}


class CheckAdmission(unittest.TestCase):
    def test_not_configured_fails_closed(self):
        d = cp.check_admission("x", "agent_crew", env={})
        self.assertEqual((d.allow, d.reason), (False, "LOOKUP_NOT_CONFIGURED"))

    def test_allow(self):
        d = cp.check_admission("x", "halla", command="lookup", runner=fake(0, ALLOW))
        self.assertTrue(d.allow)
        self.assertEqual(d.registry_status, "OK")

    def test_owner_conflict_blocks_with_matches(self):
        d = cp.check_admission("x", "agent_crew", command="lookup", runner=fake(10, STOP))
        self.assertEqual((d.allow, d.decision, d.reason), (False, "STOP_AND_REVIEW", "OWNER_CONFLICT"))
        self.assertEqual(d.matches, ("quota-core.tokenomics-shadow-policy-v1",))

    def test_degraded_registry_decision_is_passed_through(self):
        out = dict(ALLOW, decision="STOP_AND_REVIEW", blocked=True, reason="ABSENCE_UNPROVEN", registry_status="DEGRADED")
        d = cp.check_admission("x", "halla", command="lookup", runner=fake(10, out))
        self.assertEqual((d.allow, d.reason, d.registry_status), (False, "ABSENCE_UNPROVEN", "DEGRADED"))

    def test_every_failure_mode_fails_closed(self):
        cases = [fake(raise_=subprocess.TimeoutExpired("x", 1)), fake(raise_=FileNotFoundError("no")),
                 fake(0, "not json"), fake(0, dict(ALLOW, schema="capability-lookup/v1")),
                 fake(0, dict(ALLOW, blocked=True)),
                 fake(10, ALLOW), fake(0, STOP), fake(1, ALLOW), fake(0, [1, 2])]
        for run in cases:
            d = cp.check_admission("x", "halla", command="lookup", runner=run)
            self.assertFalse(d.allow, run)
            self.assertEqual(d.decision, "STOP_AND_REVIEW")

    def test_shadow_mode_allows_but_surfaces_the_raw_stop(self):
        out = dict(ALLOW, mode="shadow", raw_decision="STOP_AND_REVIEW", reason="OWNER_CONFLICT",
                   matches=[{"capability_id": "quota-core.tokenomics-shadow-policy-v1"}])
        d = cp.check_admission("x", "agent_crew", command="lookup", runner=fake(0, out))
        self.assertEqual((d.allow, d.mode, d.raw_decision), (True, "shadow", "STOP_AND_REVIEW"))
        self.assertEqual(d.matches, ("quota-core.tokenomics-shadow-policy-v1",))

    def test_warn_mode_allows_with_warn_flag(self):
        out = dict(ALLOW, mode="warn", warn=True, raw_decision="STOP_AND_REVIEW", reason="OWNER_CONFLICT")
        d = cp.check_admission("x", "agent_crew", command="lookup", runner=fake(0, out))
        self.assertEqual((d.allow, d.warn), (True, True))

    def test_argv_is_a_list_and_description_is_one_argument(self):
        calls = []
        evil = 'x"; rm -rf / #'
        cp.check_admission(evil, "agent_crew", command="python3 /opt/alfred/tools/contract_registry.py",
                           runner=fake(0, ALLOW, record=calls))
        argv, kw = calls[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:2], ["python3", "/opt/alfred/tools/contract_registry.py"])
        self.assertEqual(argv[argv.index("--text") + 1], evil)
        self.assertNotIn("shell", kw)

    def test_env_configures_the_command(self):
        calls = []
        cp.check_admission("x", "p", env={cp.ENV_COMMAND: "lookup --flag"}, runner=fake(0, ALLOW, record=calls))
        self.assertEqual(calls[0][0][:3], ["lookup", "--flag", "capability-lookup"])


@unittest.skipUnless(os.environ.get("ALFRED_CAPABILITY_CLI"), "set ALFRED_CAPABILITY_CLI to run the live contract test")
class LiveContract(unittest.TestCase):
    """Runs the real alfred CLI: proves the adapter and the single implementation agree."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("CAPABILITY_DECISION_LOG")
        os.environ["CAPABILITY_DECISION_LOG"] = os.path.join(self._tmp.name, "decisions.jsonl")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CAPABILITY_DECISION_LOG", None)
        else:
            os.environ["CAPABILITY_DECISION_LOG"] = self._old
        self._tmp.cleanup()

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("CAPABILITY_DECISION_LOG")
        os.environ["CAPABILITY_DECISION_LOG"] = os.path.join(self._tmp.name, "decisions.jsonl")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CAPABILITY_DECISION_LOG", None)
        else:
            os.environ["CAPABILITY_DECISION_LOG"] = self._old
        self._tmp.cleanup()

    def cmd(self):
        return f"{sys.executable} {os.environ['ALFRED_CAPABILITY_CLI']}"

    def test_every_live_call_is_logged(self):
        cp.check_admission("add a risk tier", "agent_crew", command=self.cmd())
        with open(os.environ["CAPABILITY_DECISION_LOG"]) as f:
            row = json.loads(f.readlines()[-1])
        self.assertEqual((row["mode"], row["project"]), ("shadow", "agent_crew"))
        self.assertNotIn("risk tier", json.dumps(row))                     # hash only, no text

    def test_every_live_call_is_logged(self):
        cp.check_admission("add a risk tier", "agent_crew", command=self.cmd())
        with open(os.environ["CAPABILITY_DECISION_LOG"]) as f:
            row = json.loads(f.readlines()[-1])
        self.assertEqual((row["mode"], row["project"]), ("shadow", "agent_crew"))
        self.assertNotIn("risk tier", json.dumps(row))                     # hash only, no text

    def test_incident_spec_is_flagged_in_default_shadow_mode(self):
        d = cp.check_admission("Implement Council #39 tokenomics enforcement: classify tasks into Tier 0-3 "
                               "(risk-tier cascade depth)", "agent_crew", command=self.cmd())
        self.assertEqual((d.allow, d.mode, d.raw_decision, d.reason), (True, "shadow", "STOP_AND_REVIEW", "OWNER_CONFLICT"))
        self.assertIn("quota-core.tokenomics-shadow-policy-v1", d.matches)

    def test_unrelated_work_is_allowed(self):
        d = cp.check_admission("Fix the pagination of the order-book viewer widget", "agent_crew",
                               command=self.cmd())
        self.assertTrue(d.allow, d)
        self.assertEqual(d.raw_decision, "ALLOW")

    def test_uncovered_project_is_never_blocked(self):
        d = cp.check_admission("Fix the pagination of the order-book viewer widget", "halla",
                               command=self.cmd())
        self.assertEqual((d.allow, d.reason), (True, "PROJECT_NOT_COVERED"))


if __name__ == "__main__":
    unittest.main()
