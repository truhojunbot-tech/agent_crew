"""#928 part B: free text never supplies issue identity or dependency decisions."""

import json
import subprocess

from agent_crew import context_pack, triage, watch
from agent_crew.protocol import TaskRequest
from agent_crew.queue import task_issue_number


def test_open_pr_uses_closing_references_not_body_or_branch(monkeypatch):
    prs = [
        {"number": 10, "body": "mentions #123", "headRefName": "fix/123",
         "closingIssuesReferences": []},
    ]

    def api(args, **_kwargs):
        assert "closingIssuesReferences" in args[args.index("-f") + 1]
        data = {"data": {"repository": {"pullRequests": {"nodes": [
            {"closingIssuesReferences": {"nodes": pr["closingIssuesReferences"]}}
            for pr in prs]}}}}
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")

    monkeypatch.setattr(watch.subprocess, "run", api)
    gh = watch.GhCli()
    assert not gh.issue_has_open_pr("owner/repo", 123)
    prs[0]["closingIssuesReferences"] = [{"number": 123}]
    assert gh.issue_has_open_pr("owner/repo", 123)


def test_description_never_supplies_issue_identity():
    task = TaskRequest(task_id="impl-928b", task_type="implement",
                       description="Implement #123", context={"risk_tier": 1})
    assert task_issue_number(task) is None
    task.context["issue"] = 123
    assert task_issue_number(task) == 123
    assert watch._issue_of({"description": "Implement #123", "context": {}}) is None


def test_dependency_and_phase_are_declared_not_parsed_from_body():
    issue = {"number": 6, "title": "child", "body": "depends on #5. Phase 2",
             "labels": []}
    parsed = triage.parse_issues([issue])[0]
    assert parsed["parents"] == [] and parsed["phase"] is None
    issue["parent"] = {"number": 5, "state": "OPEN"}
    issue["labels"] = [{"name": "phase:2"}]
    parsed = triage.parse_issues([issue])[0]
    assert parsed["parents"] == [5] and parsed["phase"] == 2
    assert triage.filter_blocked([parsed], set()) == []
    assert watch.unblocked([parsed], [parsed]) == []


def test_dependency_api_failure_is_unknown_and_not_eligible(monkeypatch):
    rows = [{"id": "I_6", "number": 6, "title": "child", "labels": [],
             "body": "depends on #5"}]

    def api(args, **_kwargs):
        if args[:3] == ["gh", "api", "graphql"]:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")

    monkeypatch.setattr(triage.subprocess, "run", api)
    parsed = triage.parse_issues(triage.fetch_issues_from_gh("owner/repo"))
    assert parsed[0]["dependency_unknown"] is True
    assert triage.filter_blocked(parsed, set()) == []
    assert watch.unblocked(parsed, parsed) == []


def test_graphql_parent_and_tracked_links_reach_triage(monkeypatch):
    rows = [{"id": "I_6", "number": 6, "title": "child", "labels": [],
             "body": "no dependency prose"}]

    def api(args, **_kwargs):
        if args[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"nodes": [{
                "id": "I_6", "number": 6, "parent": {"number": 5},
                "trackedInIssues": {"nodes": [{"number": 4}],
                                    "pageInfo": {"hasNextPage": False}},
            }]}}), "")
        return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")

    monkeypatch.setattr(triage.subprocess, "run", api)
    issue = triage.parse_issues(triage.fetch_issues_from_gh("owner/repo"))[0]
    assert issue["parents"] == [5, 4]
    assert issue["dependency_unknown"] is False


def test_issue_body_paths_do_not_create_scope():
    assert context_pack.resolve_task_scope({
        "issue_title": "src/agent_crew/watch.py",
        "issue_body": "Inspect tests/unit/test_triage.py",
    }) == ([], [])
