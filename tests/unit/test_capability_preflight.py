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


ALLOW = {"schema": "capability-lookup/v1", "decision": "ALLOW", "reason": "NO_MATCH", "matches": [],
         "registry_status": "OK", "registry_source": "governance/capability_registry.json"}
STOP = {"schema": "capability-lookup/v1", "decision": "STOP_AND_REVIEW", "reason": "OWNER_CONFLICT",
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
        out = dict(ALLOW, decision="STOP_AND_REVIEW", reason="ABSENCE_UNPROVEN", registry_status="DEGRADED")
        d = cp.check_admission("x", "halla", command="lookup", runner=fake(10, out))
        self.assertEqual((d.allow, d.reason, d.registry_status), (False, "ABSENCE_UNPROVEN", "DEGRADED"))

    def test_every_failure_mode_fails_closed(self):
        cases = [fake(raise_=subprocess.TimeoutExpired("x", 1)), fake(raise_=FileNotFoundError("no")),
                 fake(0, "not json"), fake(0, dict(ALLOW, schema="capability-lookup/v0")),
                 fake(10, ALLOW), fake(0, STOP), fake(1, ALLOW), fake(0, [1, 2])]
        for run in cases:
            d = cp.check_admission("x", "halla", command="lookup", runner=run)
            self.assertFalse(d.allow, run)
            self.assertEqual(d.decision, "STOP_AND_REVIEW")

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

    def cmd(self):
        return f"{sys.executable} {os.environ['ALFRED_CAPABILITY_CLI']}"

    def test_incident_spec_is_blocked(self):
        d = cp.check_admission("Implement Council #39 tokenomics enforcement: classify tasks into Tier 0-3 "
                               "(risk-tier cascade depth)", "agent_crew", command=self.cmd())
        self.assertFalse(d.allow)
        self.assertEqual(d.reason, "OWNER_CONFLICT")
        self.assertIn("quota-core.tokenomics-shadow-policy-v1", d.matches)

    def test_unrelated_work_is_allowed(self):
        d = cp.check_admission("Fix the pagination of the Halla order-book viewer widget", "halla",
                               command=self.cmd())
        self.assertTrue(d.allow, d)


if __name__ == "__main__":
    unittest.main()
