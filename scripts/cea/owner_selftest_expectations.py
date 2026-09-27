"""Shared receipt and integrity expectations for installed broker self-tests."""

from agent_crew.cea.broker import BROKER_TREE_USER_WRITABLE, DISPATCHER_REGISTRATION_UNAUTHENTICATED


def assert_installed_selftest(evidence: dict, *, privileged: bool,
                              launcher_check: str | None = None) -> None:
    required = ("pass", "authenticated", "schema_valid", "signed_receipt_valid",
                "tampered_receipt_rejected", "snapshot_rollback_refused")
    missing = [name for name in required if evidence.get(name) is not True]
    expected = (DISPATCHER_REGISTRATION_UNAUTHENTICATED if privileged
                else BROKER_TREE_USER_WRITABLE)
    if evidence.get("downgrade_reason") != expected:
        missing.append(f"receipt downgrade_reason={expected}")
    if privileged:
        if evidence.get("broker_status_downgrade_reason") is not None:
            missing.append("broker status downgrade_reason=None")
        if evidence.get("broker_preflight_downgrade_reason") is not None:
            missing.append("broker preflight downgrade_reason=None")
        if launcher_check is None or "downgrade_reason=none" not in launcher_check.splitlines():
            missing.append("launcher --check downgrade_reason=none")
    elif evidence.get("broker_status_downgrade_reason") != BROKER_TREE_USER_WRITABLE:
        missing.append("broker status downgrade_reason=BROKER_TREE_USER_WRITABLE")
    if missing:
        raise RuntimeError(f"installed broker self-test failed ({', '.join(missing)}): {evidence}")
