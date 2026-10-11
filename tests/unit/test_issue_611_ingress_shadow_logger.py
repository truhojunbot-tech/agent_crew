"""Row 5.3: ingress decisions reach the existing redacted shadow logger."""

import json
import subprocess
import pytest

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue, TaskAlreadyExistsError


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


def test_shadow_logger_fail_open_cases(tmp_path, monkeypatch):
    from agent_crew.cea import shadow_decision_log as log
    provider = tmp_path / "admission_inputs.py"
    provider.write_text("")
    assert not log.record_ingress_decision("work", "demo", provider_path=str(provider))
    logger = tmp_path / "contract_registry.py"
    logger.write_text("raise RuntimeError('broken')\n")
    assert not log.record_ingress_decision("work", "demo", provider_path=str(provider))
    logger.write_text("import time\ntime.sleep(3)\n")
    assert not log.record_ingress_decision("work", "demo", provider_path=str(provider))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    assert not log.record_ingress_decision("work", "demo", provider_path=str(provider))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "synthetic fixture")
    assert not log.record_ingress_decision("work", "demo")
