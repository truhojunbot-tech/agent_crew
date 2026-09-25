"""Codex quota observations must belong to the current account."""
import base64
import hashlib
import json
import os
import sqlite3
from unittest.mock import patch

import pytest

from agent_crew.cea import store as receipt_store
from agent_crew.cea.input_providers.budget import QuotaBudgetProvider
from agent_crew.cea.receipt import BudgetClass, ProviderBudgetState
from agent_crew.cea.refusal_http import status_for
from agent_crew.cea.schema import validate_receipt
from tests.unit.test_sev0_cea_engine import caller, engine, intent


NOW = 2_000_000_000.0
ACCOUNT = "account-a"
SECRET = "secret-token-must-not-escape"


def fingerprint(account=ACCOUNT):
    return "sha256:" + hashlib.sha256(account.encode()).hexdigest()[:16]


def token(account=ACCOUNT, *, nested=False):
    claims = ({"https://api.openai.com/auth": {"chatgpt_account_id": account}}
              if nested else {"chatgpt_account_id": account})
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def provider(tmp_path, *, auth=True, account=ACCOUNT, claim=ACCOUNT, credit="paid", nested=False):
    auth_path = tmp_path / "auth.json"
    if auth:
        auth_path.write_text(json.dumps({"tokens": {"account_id": account, "id_token": token(claim, nested=nested),
                                                    "access_token": SECRET}}))
    quota = tmp_path / "quota"
    (quota / "codex_monitor").mkdir(parents=True, exist_ok=True)
    return QuotaBudgetProvider(str(quota), credit_class={"codex": credit}, clock=lambda: NOW,
                               env={"AGENT_CREW_CEA_CODEX_AUTH_PATH": str(auth_path)})


def cache(p, **kw):
    data = {"fetched_at": NOW, "account_fingerprint": fingerprint(),
            "five_hour": {"utilization": 0.1}, "seven_day": {"utilization": 0.2}}
    data.update(kw)
    path = p.quota_dir + "/codex_monitor/quota_cache.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


@pytest.mark.parametrize("util, expected", [(0.1, BudgetClass.OK),
                                             (0.95, BudgetClass.CONSTRAINED),
                                             (1.0, BudgetClass.EXHAUSTED)])
def test_matching_account_uses_utilization(tmp_path, util, expected):
    p = provider(tmp_path)
    cache(p, five_hour={"utilization": util})
    assert p.budget("codex").state is expected


@pytest.mark.parametrize("change", ["mismatch", "missing_fingerprint", "unreadable_auth",
                                         "disagreeing_claim", "error", "stale", "missing_cache"])
def test_unobservable_codex_budget_is_unverified(tmp_path, change, caplog):
    p = provider(tmp_path, auth=change != "unreadable_auth",
                 claim="other-account" if change == "disagreeing_claim" else ACCOUNT)
    if change != "missing_cache":
        changes = {}
        if change == "mismatch":
            changes["account_fingerprint"] = fingerprint("other-account")
        elif change == "missing_fingerprint":
            changes["account_fingerprint"] = None
        elif change == "error":
            changes["error"] = "quota fetch failed"
        elif change == "stale":
            changes["fetched_at"] = NOW - 901
        cache(p, **changes)
    observation = p.budget("codex")
    assert observation.state is ProviderBudgetState.UNVERIFIED
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        receipt_store.ensure_schema(conn)
        auth = engine(budgets=p).authorize(conn, intent(task_id=change), caller())
    assert auth.decision == "BLOCK" and auth.code == "BUDGET_UNVERIFIED"
    assert auth.receipt["binding"]["budget_class"] == "EXHAUSTED"
    assert auth.receipt["provider_budget"]["state"] == "UNVERIFIED"
    assert validate_receipt(auth.receipt) == []
    assert SECRET not in json.dumps(auth.receipt)
    assert SECRET not in caplog.text


def test_missing_account_id_and_plan_account_mismatch_are_unverified(tmp_path):
    p = provider(tmp_path, account=None)
    cache(p)
    assert p.budget("codex").state is ProviderBudgetState.UNVERIFIED
    plan = provider(tmp_path, account="different", credit="plan")
    cache(plan)
    assert plan.budget("codex").state is ProviderBudgetState.UNVERIFIED


@pytest.mark.parametrize("change", ["mismatch", "missing_fingerprint", "missing_identity"])
def test_plan_without_current_account_observation_blocks(tmp_path, change):
    p = provider(tmp_path, account=None if change == "missing_identity" else ACCOUNT, credit="plan")
    cache(p, account_fingerprint=(fingerprint("other-account") if change == "mismatch" else
                                  None if change == "missing_fingerprint" else fingerprint()))
    assert p.budget("codex").state is ProviderBudgetState.UNVERIFIED
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        receipt_store.ensure_schema(conn)
        auth = engine(budgets=p).authorize(conn, intent(task_id=change), caller())
    assert auth.decision == "BLOCK" and auth.code == "BUDGET_UNVERIFIED"
    assert auth.receipt["binding"]["budget_class"] == "EXHAUSTED"


@pytest.mark.parametrize("change", ["stale", "error"])
def test_plan_matching_stale_observation_remains_constrained(tmp_path, change):
    p = provider(tmp_path, credit="plan")
    cache(p, **({"fetched_at": NOW - 901} if change == "stale" else
                {"error": "quota fetch failed"}))
    assert p.budget("codex").state is BudgetClass.CONSTRAINED


def test_nested_claim_fixture_matches_account_id(tmp_path):
    p = provider(tmp_path, nested=True)
    cache(p)
    assert p._codex_fingerprint() == fingerprint()
    assert p.budget("codex").state is BudgetClass.OK


def test_writer_reader_fingerprints_match_for_nested_claim(tmp_path, monkeypatch):
    quota_ops_path = os.environ.get("J6_QUOTA_OPS_PATH")
    quota_core_path = os.environ.get("J6_QUOTA_CORE_PATH")
    if not quota_ops_path or not quota_core_path:
        pytest.skip("cross-repo contract test requires J6_QUOTA_OPS_PATH and J6_QUOTA_CORE_PATH")
    monkeypatch.syspath_prepend(quota_core_path)
    monkeypatch.syspath_prepend(quota_ops_path)
    from codex_monitor import codex_monitor

    p = provider(tmp_path, nested=True)
    with patch.object(codex_monitor, "CODEX_AUTH", tmp_path / "auth.json"):
        writer_fingerprint, _ = codex_monitor._current_account_identity()
    assert writer_fingerprint == p._codex_fingerprint() == fingerprint()


@pytest.mark.parametrize("credit", ["paid", "overage", "unrecognized"])
def test_unobserved_costly_credit_classes_are_unverified(tmp_path, credit):
    p = provider(tmp_path, credit=credit)
    assert p.budget("codex").state is ProviderBudgetState.UNVERIFIED


def test_real_cooldown_remains_exhausted(tmp_path):
    p = provider(tmp_path)
    cache(p)
    cooldown = tmp_path / "cooldown.json"
    cooldown.write_text(json.dumps({"codex": NOW + 60}))
    p.cooldown_file = str(cooldown)
    assert p.budget("codex").state is BudgetClass.EXHAUSTED


def test_claude_cache_does_not_need_codex_identity(tmp_path):
    p = provider(tmp_path, auth=False)
    other = tmp_path / "quota" / "claude_monitor"
    other.mkdir()
    (other / "quota_cache.json").write_text(json.dumps({"fetched_at": NOW,
                                                        "five_hour": {"utilization": 0.1}}))
    assert p.budget("claude").state is BudgetClass.OK


def test_unverified_refusal_has_exhausted_http_status():
    assert status_for("BLOCK", "BUDGET_UNVERIFIED") == status_for("BLOCK", "BUDGET_EXHAUSTED") == 423
