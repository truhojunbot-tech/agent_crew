"""Broker integrity and identity downgrades remain separate claims."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

from agent_crew.cea.broker import BROKER_TREE_USER_WRITABLE, DISPATCHER_REGISTRATION_UNAUTHENTICATED
from agent_crew.cea.engine import AuthorizationEngine, EngineConfig


_FIXTURES = Path(__file__).with_name("test_sev0_cea_engine.py")
spec = importlib.util.spec_from_file_location("cea_engine_downgrade_fixtures", _FIXTURES)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


@pytest.mark.parametrize("config,expected", [
    (EngineConfig(mode="test", out_of_process_broker=True), DISPATCHER_REGISTRATION_UNAUTHENTICATED),
    (EngineConfig(mode="test"), "SHARED_UID_NO_CREDENTIAL_BOUNDARY"),
    (EngineConfig(mode="test", out_of_process_broker=True,
                  fallback_reason=BROKER_TREE_USER_WRITABLE), BROKER_TREE_USER_WRITABLE),
])
def test_full_and_refusal_receipts_use_same_identity_downgrade(config, expected):
    caller = fixtures.caller()
    results = []
    for kind in ("full", "unavailable", "refusal"):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            intent = fixtures.intent(kind)
            if kind == "full":
                auth = fixtures.engine(config=config).authorize(conn, intent, caller)
            elif kind == "unavailable":
                auth = AuthorizationEngine(config=config).authorize(conn, intent, caller)
            else:
                auth = fixtures.engine(config=config).refuse(
                    conn, intent, caller, code="INPUTS_UNAVAILABLE",
                    text="sandbox input unavailable")
            results.append(auth)
        finally:
            conn.close()
    assert results[1].code == "INPUTS_UNAVAILABLE"
    for auth in results:
        assert auth.receipt["caller_identity_status"] == "UNVERIFIED"
        assert auth.receipt["executor_binding_status"] == "UNVERIFIED"
        assert auth.receipt["downgrade_reason"] == expected


def test_shared_installed_expectation_checks_integrity_and_receipt():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/cea"))
    from owner_selftest_expectations import assert_installed_selftest
    common = dict.fromkeys(("pass", "authenticated", "schema_valid", "signed_receipt_valid",
                            "tampered_receipt_rejected", "snapshot_rollback_refused"), True)
    privileged = {**common, "downgrade_reason": DISPATCHER_REGISTRATION_UNAUTHENTICATED,
                  "broker_status_downgrade_reason": None,
                  "broker_preflight_downgrade_reason": None}
    assert_installed_selftest(privileged, privileged=True, launcher_check="downgrade_reason=none\n")
    with pytest.raises(RuntimeError, match="receipt downgrade_reason"):
        assert_installed_selftest({**privileged, "downgrade_reason": "SHARED_UID_NO_CREDENTIAL_BOUNDARY"},
                                  privileged=True, launcher_check="downgrade_reason=none\n")
    with pytest.raises(RuntimeError, match="launcher"):
        assert_installed_selftest(privileged, privileged=True,
                                  launcher_check="downgrade_reason=BROKER_TREE_USER_WRITABLE\n")
    writable = {**common, "downgrade_reason": BROKER_TREE_USER_WRITABLE,
                "broker_status_downgrade_reason": BROKER_TREE_USER_WRITABLE}
    assert_installed_selftest(writable, privileged=False)
