"""Shared ADR-004 conformance gate for CLI and server auto-merge."""

import json
import os
import re
import subprocess

import click

from agent_crew.cea.wiring import capability_registry_path


_PRIVATE_FLEET_IMPORT = re.compile(r"^\s*(?:from|import)\s+alfred(?:\.|\s|$)")
# Split the project name so an added line containing this detector is not evidence.
_PRIVATE_HOME_LITERAL = re.compile(
    r"""(['"])[^'"\n]*?/home/[^/'"\s]+/""" + "alf" + r"""red/[^'"\n]*?\1""")


def _registry_capability_for_paths(project: str, paths: set[str], registry_path: str) -> str:
    """Find the one active, project-owned capability best supported by changed paths."""
    if not paths or not registry_path:
        return ""
    try:
        with open(registry_path, encoding="utf-8") as fh:
            records = json.load(fh)["records"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if not isinstance(records, list):
        return ""

    def locations(value: str) -> list[str]:
        if not isinstance(value, str):
            return []
        expanded = [value]
        while any("{" in item for item in expanded):
            next_items = []
            for item in expanded:
                group = re.search(r"\{([^{}]+)\}", item)
                if group:
                    next_items.extend(item[:group.start()] + part.strip() + item[group.end():]
                                      for part in group.group(1).split(","))
                else:
                    next_items.append(item)
            if next_items == expanded:
                break
            expanded = next_items
        return [part.strip().replace("\\", "/").removeprefix("./")
                for item in expanded for part in re.split(r"[,;]", item) if part.strip()]

    def matches(changed: str, registered: str) -> bool:
        changed = changed.replace("\\", "/").removeprefix("./")
        return bool(registered and (changed == registered or
                    changed.endswith("/" + registered)))

    norm_project = project.lower().replace("-", "_")
    scored = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("owning_project", "")).lower().replace("-", "_") != norm_project:
            continue
        if str(record.get("lifecycle_state", "")).lower() in {
                "withdrawn", "deprecated", "superseded"}:
            continue
        cap_id = record.get("capability_id")
        if not isinstance(cap_id, str) or not cap_id:
            continue
        registered_paths = set(locations(record.get("implementation_location")))
        for evidence in record.get("evidence") or []:
            if isinstance(evidence, dict):
                registered_paths.update(locations(evidence.get("path")))
        score = sum(any(matches(path, registered) for registered in registered_paths)
                    for path in paths)
        if score:
            scored.append((score, cap_id))
    if not scored:
        return ""
    scored.sort(reverse=True)
    return scored[0][1] if len(scored) == 1 or scored[0][0] > scored[1][0] else ""


def _conformance_gate_allows_merge(queue, task_id: str, *, project: str,
                                   pr_number: int, title: str, task_desc: str,
                                   issue: int | None, receipt_dir: str,
                                   repo: str, fail_closed: bool = False) -> bool:
    """#588: run the ADR-004 conformance gate (``--enforce``) before auto-merge.

    Off unless ``AGENT_CREW_CONFORMANCE_GATE_CMD`` names the gate command
    prefix; the gate itself lives in the alfred repo and is only called here.
    The verdict, receipt path and sha are recorded on ``task_id``:

    * ALLOW  — merge.
    * REVIEW — merge, and post the receipt as a PR comment for the reviewer.
      Any gate error, timeout, non-zero exit other than 10, or
      EVIDENCE_UNAVAILABLE counts as REVIEW.
    * BLOCK (exit 10) — do not merge; the task goes to ``needs_human``.
    """
    prefix = os.environ.get("AGENT_CREW_CONFORMANCE_GATE_CMD", "").strip()
    if not prefix:
        if fail_closed:
            reason = "conformance gate not configured"
            queue.mark_needs_human(task_id, reason,
                                   {"conformance_gate": {"error": reason,
                                                         "merge_allowed": False,
                                                         "pr_number": pr_number}})
            return False
        return True
    import hashlib
    import shlex
    from agent_crew import github
    ctx: dict = {}
    try:
        ctx = queue.get_task_context(task_id) or {}
    except Exception:
        pass
    head_sha = github.pr_head_sha(pr_number, repo=repo, timeout=5)
    prior = ctx.get("conformance_gate") if isinstance(ctx, dict) else None
    if (head_sha and isinstance(prior, dict)
            and prior.get("pr_number") == pr_number
            and prior.get("head_sha") == head_sha
            and prior.get("verdict") in ("ALLOW", "REVIEW", "BLOCK")):
        receipt_path = prior.get("receipt_path")
        try:
            with open(receipt_path, "rb") as fh:
                receipt_matches = (hashlib.sha256(fh.read()).hexdigest()
                                   == prior.get("receipt_sha256"))
        except (OSError, TypeError, ValueError):
            receipt_matches = False
        if receipt_matches and (not fail_closed or not prior.get("error")):
            return bool(prior.get("merge_allowed", prior["verdict"] != "BLOCK"))
    change = {
        "stage": "PRE_MERGE",
        "project": project,
        "text": "\n".join([title, *task_desc.splitlines()[:5]]).strip(),
        "change_type": ctx.get("change_type") if ctx.get("change_type") in ("create", "modify") else "modify",
        "owner_evidence": {"project": project,
                           "ref": f"issue#{issue}" if issue else f"pr#{pr_number}"},
        "evidence_source": {},
    }
    if isinstance(ctx.get("capability_id"), str) and ctx["capability_id"].strip():
        change["capability_id"] = ctx["capability_id"].strip()
        change["evidence_source"]["capability_id"] = "context"
    else:
        change["capability_id"] = (project.replace("_", "-") +
                                   (f".issue-{issue}" if issue else f".pr-{pr_number}"))
        change["evidence_source"]["capability_id"] = "derived"
        if issue:
            try:
                issue_result = subprocess.run(
                    ["gh", "issue", "view", str(issue), "--repo", repo,
                     "--json", "body", "--jq", ".body"],
                    capture_output=True, text=True, timeout=5)
                if issue_result.returncode == 0:
                    declared = re.search(
                        r"^capability(?:_id)?:\s*([A-Za-z0-9][A-Za-z0-9_.:-]*)",
                        issue_result.stdout or "", re.IGNORECASE | re.MULTILINE)
                    if declared:
                        change["capability_id"] = declared.group(1)
                        change["evidence_source"]["capability_id"] = "issue"
            except Exception:
                pass
    if isinstance(ctx.get("role"), str) and ctx["role"].strip():
        change["role"] = ctx["role"].strip()
        change["evidence_source"]["role"] = "context"
    else:
        change["role"] = "implementer"
        change["evidence_source"]["role"] = "derived"
    if isinstance(ctx.get("portable_core"), bool):
        change["portable_core"] = ctx["portable_core"]
        change["evidence_source"]["portable_core"] = "context"
    if isinstance(ctx.get("dependencies"), list):
        change["dependencies"] = ctx["dependencies"]
        change["evidence_source"]["dependencies"] = "context"
    try:
        diff = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", repo, "--patch"],
            capture_output=True, text=True, timeout=30)
        if diff.returncode == 0:
            paths, private_files = set(), set()
            path = ""
            for line in diff.stdout.splitlines():
                if line.startswith("diff --git "):
                    parts = shlex.split(line[len("diff --git "):])
                    path = parts[1][2:] if len(parts) == 2 and parts[1].startswith("b/") else ""
                    if path:
                        paths.add(path)
                elif line.startswith("+++ b/"):
                    path = line[6:]
                    paths.add(path)
                elif line.startswith("+") and not line.startswith("+++") and path.startswith("src/agent_crew/"):
                    added = line[1:]
                    if not added.lstrip().startswith("#") and (
                            _PRIVATE_FLEET_IMPORT.search(added) or
                            _PRIVATE_HOME_LITERAL.search(added)):
                        private_files.add(path)
            if paths:
                change["changed_paths"] = sorted(paths)
                change["text"] += "\n" + "\n".join(sorted(paths)[:20])
            if change["evidence_source"]["capability_id"] == "derived":
                cap_id = _registry_capability_for_paths(
                    project, paths, capability_registry_path(os.environ))
                if cap_id:
                    change["capability_id"] = cap_id
                    change["evidence_source"]["capability_id"] = "registry_path"
            if "portable_core" not in change:
                change["portable_core"] = any(p.startswith("src/agent_crew/") for p in paths)
                change["evidence_source"]["portable_core"] = "derived"
            if "dependencies" not in change:
                change["dependencies"] = [
                    {"kind": "private_fleet", "project": "alfred", "file": p}
                    for p in sorted(private_files)]
                change["evidence_source"]["dependencies"] = "derived"
    except Exception:
        # No diff is evidence unavailable; the checker keeps REVIEW.
        pass
    stem = os.path.join(receipt_dir, f"{task_id}-pr{pr_number}")
    change_path, receipt_path = f"{stem}.change.json", f"{stem}.receipt.json"
    verdict, error, exit_code = "REVIEW", "", None
    try:
        os.makedirs(receipt_dir, exist_ok=True)
        with open(change_path, "w") as fh:
            json.dump(change, fh, indent=2)
        if os.path.exists(receipt_path):
            os.remove(receipt_path)
        cmd = shlex.split(prefix) + ["capability-conformance-check", "--input", change_path,
                                     "--receipt", receipt_path, "--enforce"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        exit_code = r.returncode
        if exit_code == 10:
            verdict = "BLOCK"
        elif exit_code != 0:
            error = f"gate exit {exit_code}: {(r.stderr or r.stdout).strip()[-500:]}"
        else:
            with open(receipt_path) as fh:
                raw = json.load(fh)
            reported = str(raw.get("verdict") or raw.get("decision") or "").upper()
            if reported in ("ALLOW", "REVIEW"):
                verdict = reported
            else:
                # EVIDENCE_UNAVAILABLE, or BLOCK without exit 10, is REVIEW.
                error = f"gate verdict {reported!r} with exit 0"
    except subprocess.TimeoutExpired:
        error = "gate timeout after 30s"
    except Exception as exc:
        error = f"gate error: {exc}"
    receipt_error = False
    try:
        if error or not os.path.exists(receipt_path):
            with open(receipt_path, "w") as fh:
                json.dump({"verdict": verdict, "error": error, "source": "agent_crew"}, fh, indent=2)
        with open(receipt_path, "rb") as fh:
            receipt_bytes = fh.read()
    except OSError as exc:
        # A broken receipt directory is a gate failure, not a merge veto.
        # Keep the REVIEW/BLOCK evidence at a usable fallback path.
        import tempfile
        error = f"{error}; receipt error: {exc}".strip("; ")
        receipt_error = True
        fd, receipt_path = tempfile.mkstemp(prefix="agent_crew_conformance_", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"verdict": verdict, "error": error, "source": "agent_crew"}, fh, indent=2)
        with open(receipt_path, "rb") as fh:
            receipt_bytes = fh.read()
    merge_allowed = verdict != "BLOCK" and not (fail_closed and (error or receipt_error))
    record = {"verdict": verdict, "receipt_path": receipt_path,
              "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
              "exit_code": exit_code, "pr_number": pr_number,
              "merge_allowed": merge_allowed}
    if head_sha:
        record["head_sha"] = head_sha
    if error:
        record["error"] = error
    click.echo(f"  Conformance gate: {verdict} — receipt {receipt_path}")
    try:
        if not merge_allowed:
            queue.mark_needs_human(
                task_id, f"conformance gate {error or verdict} on PR #{pr_number}; receipt {receipt_path}",
                {"conformance_gate": record})
        else:
            queue.patch_context(task_id, {"conformance_gate": record})
    except Exception as exc:
        click.echo(f"Warning: could not record conformance gate receipt: {exc}")
    if verdict == "REVIEW" and merge_allowed:
        body = (f"**Conformance gate (ADR-004, PRE_MERGE): REVIEW** — merging, "
                f"reviewer must address this receipt.\n\n"
                f"task `{task_id}` · receipt `{receipt_path}` · sha256 `{record['receipt_sha256']}`"
                + (f"\n\nerror: {error}" if error else "")
                + f"\n\n```json\n{receipt_bytes.decode('utf-8', 'replace')[:6000]}\n```")
        if not github.post_pr_comment(pr_number, body, repo=repo):
            click.echo("Warning: could not post conformance gate receipt to the PR")
    return merge_allowed
