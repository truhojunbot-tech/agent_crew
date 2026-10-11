"""Row 5.3: ingress decisions reach the existing redacted shadow logger."""

import json
import subprocess
import pytest

from agent_crew.protocol import TaskRequest
from agent_crew.queue import (
    TaskQueue, TaskAlreadyExistsError, _CEA_SYSTEM_SUCCESSOR_PROVENANCE,
)


def test_shadow_logger_uses_stdin_and_existing_contract(tmp_path, monkeypatch):
    from agent_crew.cea.shadow_decision_log import record_ingress_decision

    provider = tmp_path / "admission_inputs.py"
    provider.write_text("")
    logger = tmp_path / "contract_registry.py"
    logger.write_text(
        "import json, os\n"
        "def admission_decision(text, project, *, mode):\n"
        "    assert mode == 'shadow'\n"
        "    with open(os.environ['CAPABILITY_DECISION_LOG'], 'a') as f:\n"
        "        f.write(json.dumps({'project': project, 'mode': mode, 'text_len': len(text)})+'\\n')\n"
    )
    log = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("CAPABILITY_DECISION_LOG", str(log))
    description = "private work description"
    assert record_ingress_decision(description, "demo", provider_path=str(provider))
    assert json.loads(log.read_text()) == {
        "project": "demo", "mode": "shadow", "text_len": len(description)}


def test_only_admitted_natural_ingress_calls_logger_and_failure_does_not_block(tmp_path, monkeypatch):
    from agent_crew.cea import shadow_decision_log

    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "shadow")
    calls = []

    def failing_logger(description, project):
        calls.append((description, project))
        raise subprocess.TimeoutExpired("logger", 2)

    monkeypatch.setattr(shadow_decision_log, "record_ingress_decision", failing_logger)
    q = TaskQueue(str(tmp_path / "tasks.db"))
    for ingress in ("http.tasks", "cli.enqueue", "cascade.review", "retry.failed_task"):
        task = TaskRequest(task_id="task-" + ingress.replace(".", "-"),
                           task_type="discuss", description="review PR " + ingress, project="demo",
                           branch="main")
        assert q.enqueue(task, ingress=ingress) == task.task_id
    assert calls == [("review PR http.tasks", "demo"),
                     ("review PR cli.enqueue", "demo")]


def test_lineage_and_refused_admissions_do_not_reach_promotion_log(tmp_path, monkeypatch):
    from agent_crew.cea import shadow_decision_log
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "shadow")
    calls = []
    monkeypatch.setattr(shadow_decision_log, "record_ingress_decision",
                        lambda description, project: calls.append((description, project)))
    q = TaskQueue(str(tmp_path / "tasks.db"))
    q._declared_project = "demo"
    root = TaskRequest("natural", "discuss", "natural work", project="demo")
    q.enqueue(root, ingress="http.tasks")
    child = TaskRequest("child", "discuss", "child work", project="demo",
                        context={"prev_task_id": "natural"})
    q.enqueue(child, ingress="http.tasks")
    with pytest.raises(TaskAlreadyExistsError):
        q.enqueue(root, ingress="http.tasks")
    foreign = TaskRequest("foreign", "discuss", "foreign work", project="foreign")
    try:
        q.enqueue(foreign, ingress="http.tasks")
    except Exception:
        pass
    assert calls == [("natural work", "demo")]


def test_natural_ingress_excludes_system_and_retry_context_and_watchdog(tmp_path, monkeypatch):
    from agent_crew.cea import shadow_decision_log

    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "shadow")
    calls = []
    monkeypatch.setattr(shadow_decision_log, "record_ingress_decision",
                        lambda description, project: calls.append((description, project)))
    q = TaskQueue(str(tmp_path / "tasks.db"))

    # The private capability is sufficient to make a task a system successor
    # even when it has no parent field to trigger the other eligibility checks.
    system = TaskRequest("system", "discuss", "system work", project="demo")
    assert q.enqueue(system, ingress="http.tasks",
                     _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE) == "system"

    # Isolate the original_task_id clause from the capability clause. Ordinary
    # callers lose lineage keys at the trust boundary; this fixture supplies
    # an already trusted context to exercise the promotion decision itself.
    original_context = q._trusted_enqueue_context
    def context_with_original(task, *, successor_provenance):
        context = original_context(task, successor_provenance=successor_provenance)
        if task.task_id == "retry-context":
            context["original_task_id"] = "external-parent"
        return context
    monkeypatch.setattr(q, "_trusted_enqueue_context", context_with_original)
    retry = TaskRequest("retry-context", "discuss", "retry context work", project="demo")
    assert q.enqueue(retry, ingress="http.tasks") == "retry-context"

    watchdog = TaskRequest("watchdog", "discuss", "watchdog work", project="demo")
    assert q.enqueue(watchdog, ingress="watchdog.stale_review") == "watchdog"
    for ingress in ("cron.watch", "cron.triage"):
        task = TaskRequest(ingress, "discuss", ingress + " work", project="demo")
        assert q.enqueue(task, ingress=ingress) == ingress
    assert calls == [("cron.watch work", "demo"), ("cron.triage work", "demo")]


def test_shadow_logger_fail_open_cases(tmp_path, monkeypatch):
    from agent_crew.cea import shadow_decision_log as log
    provider = tmp_path / "admission_inputs.py"
    provider.write_text("")
    assert not log.record_ingress_decision("work", "demo", provider_path=str(provider))
    logger = tmp_path / "contract_registry.py"
    logger.write_text("raise RuntimeError('broken')\n")
    assert not log.record_ingress_decision("work", "demo", provider_path=str(provider))
    log_path = tmp_path / "late-decision.jsonl"
    monkeypatch.setenv("CAPABILITY_DECISION_LOG", str(log_path))
    logger.write_text(
        "import os, time\n"
        "time.sleep(3)\n"
        "def admission_decision(text, project, *, mode):\n"
        "    with open(os.environ['CAPABILITY_DECISION_LOG'], 'w') as f:\n"
        "        f.write(project)\n"
    )
    assert not log.record_ingress_decision("work", "demo", provider_path=str(provider))
    assert not log_path.exists()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    assert not log.record_ingress_decision("work", "demo", provider_path=str(provider))


def test_pytest_guard_skips_subprocess_but_provider_override_bypasses_it(tmp_path, monkeypatch):
    from agent_crew.cea import shadow_decision_log as log

    provider = tmp_path / "admission_inputs.py"
    provider.write_text("")
    (tmp_path / "contract_registry.py").write_text("def admission_decision(*a, **k): pass\n")
    calls = []
    def recording_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0)
    monkeypatch.setattr(subprocess, "run", recording_run)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "synthetic fixture")
    assert not log.record_ingress_decision("work", "demo")
    assert calls == []
    assert log.record_ingress_decision("work", "demo", provider_path=str(provider))
    assert len(calls) == 1
