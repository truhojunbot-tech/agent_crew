"""#239 — task-scoped Context Pack retrieval over project memory.

Agent Crew preserves long provider conversations and ships a task prompt, but
it had no layer that assembles *only* the project context relevant to the
claimed issue. That forced a bad trade: keep full history (token/quota growth,
stale material — the #232/#236 failure mode) or compact hard (lose acceptance
criteria, prior decisions, rejected approaches).

This module takes the other route. Provider chat history stops being the
project memory; a bounded pack is built from durable sources at dispatch.

## The three memory classes, kept separate on purpose

- **Authoritative** — current git code/config, ADR/spec, the issue body and its
  acceptance criteria, linked PRs/reviews, tests. Source of truth.
- **Episodic** — what previous attempts actually did: terminal outcomes, review
  rejections, approaches abandoned, validation results.
- **Working** — the live provider conversation. Not our business here.

⛔Any index is a retrieval *aid*. Every item in a pack carries a `uri` and a
  `revision` that resolve back to a durable git/GitHub/telemetry artifact, so a
  reader can always go check. Nothing here is allowed to become the truth.

## Determinism first

A lexical baseline ships with no embeddings, so semantic retrieval can later be
*measured* against it rather than assumed better. `RetrievalProvider` is a
versioned protocol: a semantic or hybrid backend is added by registering
another provider, with no change to the dispatcher contract.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

#: Bump only for breaking changes to the pack/artifact field contract.
CONTEXT_PACK_SCHEMA_VERSION = 1

# ── artifact taxonomy ─────────────────────────────────────────────────
# `issue` and `acceptance_criteria` are MANDATORY: the planner never drops
# them, even when the budget is blown. A pack without the AC is worse than
# no pack, because it looks authoritative while omitting the requirement.
TYPE_ISSUE = "issue"
TYPE_AC = "acceptance_criteria"
TYPE_ADR = "adr"
TYPE_SPEC = "spec"
TYPE_CODE = "code"
TYPE_TEST = "test"
TYPE_REVIEW = "pr_review"
TYPE_EPISODE = "episode"
#: #240 derived governance. Ranked below every authoritative type on
#: purpose: a procedure may require a check, never overrule current code
#: or the acceptance criteria.
TYPE_PROCEDURE = "procedure"
TYPE_EVIDENCE = "evidence"

MANDATORY_TYPES = frozenset({TYPE_ISSUE, TYPE_AC})

FRESH, STALE, UNKNOWN = "fresh", "stale", "unknown"

MODE_LEXICAL, MODE_SEMANTIC, MODE_HYBRID = "lexical", "semantic", "hybrid"

#: Default budgets per role. Authoritative material outranks recollection, so
#: a reviewer gets the original AC plus implementation context and only a thin
#: episodic tail; a tester leans on tests and prior failure evidence.
DEFAULT_ROLE_BUDGETS: dict = {
    "implementer": {"max_tokens": 6000, "max_items": 24,
                    "type_caps": {TYPE_EPISODE: 4, TYPE_CODE: 8, TYPE_TEST: 4}},
    "reviewer":    {"max_tokens": 5000, "max_items": 20,
                    "type_caps": {TYPE_EPISODE: 2, TYPE_CODE: 8, TYPE_REVIEW: 6}},
    "tester":      {"max_tokens": 4000, "max_items": 16,
                    "type_caps": {TYPE_EPISODE: 3, TYPE_TEST: 8, TYPE_CODE: 4}},
}
DEFAULT_BUDGET = {"max_tokens": 4000, "max_items": 16, "type_caps": {}}

#: Rank order when scores tie. Authoritative beats episodic by construction —
#: "relevant ADR/spec decisions outrank provider conversation recollection".
_TYPE_RANK = {
    TYPE_ISSUE: 0, TYPE_AC: 1, TYPE_ADR: 2, TYPE_SPEC: 3,
    TYPE_TEST: 4, TYPE_CODE: 5, TYPE_REVIEW: 6,
    TYPE_EVIDENCE: 7, TYPE_PROCEDURE: 8, TYPE_EPISODE: 9,
}


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate (~4 chars/token).

    ⛔Deliberately not a real tokenizer: the budget must be reproducible on any
      host without pulling a model-specific dependency, and every consumer must
      compute the same number for the same pack.
    """
    return max(1, (len(text or "") + 3) // 4)


@dataclass
class Artifact:
    """One retrieved item, always resolvable back to a durable source."""

    artifact_id: str
    uri: str
    artifact_type: str
    revision: str = ""
    score: float = 0.0
    score_components: dict = field(default_factory=dict)
    provenance: str = ""
    freshness: str = UNKNOWN
    stale_reason: str = ""
    subject_key: str = ""
    excerpt: str = ""
    est_tokens: int = 0

    def __post_init__(self):
        if not self.est_tokens:
            self.est_tokens = estimate_tokens(self.excerpt)
        if not self.subject_key:
            self.subject_key = self.uri or self.artifact_id

    @property
    def mandatory(self) -> bool:
        return self.artifact_type in MANDATORY_TYPES


@dataclass
class RetrievalQuery:
    task_id: str = ""
    task_type: str = ""
    role: str = ""
    repo: str = ""
    repo_path: str = ""
    issue_number: Optional[int] = None
    issue_title: str = ""
    issue_body: str = ""
    branch: str = ""
    keywords: list = field(default_factory=list)
    limit: int = 40
    retry_of: str = ""


@dataclass
class ContextPack:
    """A bounded, provenance-linked set of artifacts for one task."""

    task_id: str
    role: str
    mode: str
    items: list = field(default_factory=list)
    schema_version: int = CONTEXT_PACK_SCHEMA_VERSION
    budget: dict = field(default_factory=dict)
    candidate_count: int = 0
    stale_count: int = 0
    conflicts: list = field(default_factory=list)
    latency_ms: float = 0.0
    degraded: bool = False
    degraded_reason: str = ""
    provider_errors: list = field(default_factory=list)

    @property
    def selected_count(self) -> int:
        return len(self.items)

    @property
    def total_tokens(self) -> int:
        return sum(a.est_tokens for a in self.items)

    def tokens_by_category(self) -> dict:
        out: dict = {}
        for a in self.items:
            key = "mandatory" if a.mandatory else (
                "procedural" if a.artifact_type == TYPE_PROCEDURE else
                "episodic" if a.artifact_type == TYPE_EPISODE else "authoritative")
            out[key] = out.get(key, 0) + a.est_tokens
        return out

    @property
    def pack_hash(self) -> str:
        """Content hash over the *identity* of what was included, in order.

        Excerpt text is deliberately excluded so the hash is stable when only
        formatting changes, and so it can be logged without leaking content.
        """
        basis = json.dumps(
            [[a.artifact_id, a.artifact_type, a.revision] for a in self.items],
            sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(basis.encode()).hexdigest()[:16]

    @property
    def pack_id(self) -> str:
        return f"cp{self.schema_version}-{self.pack_hash}"

    def telemetry(self) -> dict:
        """Privacy-safe telemetry. Identifiers and counts only — no content.

        ⛔Agent Crew emits neutral numbers here and does not import, or reason
          about, any external economics system (#239 non-goal).
        """
        return {
            "context_pack_id": self.pack_id,
            "context_pack_hash": self.pack_hash,
            "context_pack_schema_version": self.schema_version,
            "mode": self.mode,
            "role": self.role,
            "candidate_count": self.candidate_count,
            "selected_count": self.selected_count,
            "total_tokens": self.total_tokens,
            "tokens_by_category": self.tokens_by_category(),
            "stale_count": self.stale_count,
            "conflict_count": len(self.conflicts),
            "latency_ms": round(self.latency_ms, 1),
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "budget": dict(self.budget or {}),
        }

    def to_prompt_block(self) -> str:
        """Render the pack for the task prompt.

        Every item states its type, provenance and revision so the agent can
        weigh it — and conflicts are printed as conflicts rather than being
        silently resolved in favour of whichever scored higher.
        """
        if not self.items and not self.degraded:
            return ""
        lines = [f"=== CONTEXT PACK {self.pack_id} (mode={self.mode}, "
                 f"{self.selected_count} items, ~{self.total_tokens} tok) ==="]
        if self.degraded:
            lines.append(
                f"!! DEGRADED: {self.degraded_reason} — this pack is incomplete. "
                f"Treat absence of an artifact as unknown, not as absence of fact.")
        for a in self.items:
            head = f"[{a.artifact_type}] {a.uri}"
            if a.revision:
                head += f" @{a.revision}"
            if a.freshness == STALE:
                head += f"  (STALE: {a.stale_reason or 'older than current head'})"
            lines.append(f"\n--- {head}\n    why: {a.provenance}")
            if a.excerpt:
                lines.append(a.excerpt.rstrip())
        if self.conflicts:
            lines.append("\n!! CONFLICTING ARTIFACTS — resolve explicitly, "
                         "do not average them:")
            for c in self.conflicts:
                lines.append(f"  - {c}")
        lines.append("=== END CONTEXT PACK ===")
        return "\n".join(lines)


# ── retrieval providers ───────────────────────────────────────────────


class RetrievalProvider:
    """Versioned retrieval contract (#239 scope 1).

    A backend implements `retrieve()` and declares its `mode`. Adding a
    semantic or hybrid provider therefore requires no dispatcher change — the
    planner consumes providers, not implementations.
    """

    name = "base"
    version = 1
    mode = MODE_LEXICAL

    def retrieve(self, query: RetrievalQuery) -> list:  # pragma: no cover
        raise NotImplementedError


class IssueProvider(RetrievalProvider):
    """The mandatory half: the issue body and its acceptance criteria.

    Split into two artifacts on purpose — the AC is what the work is judged
    against, so it must survive budget pressure independently of a long issue
    body.
    """

    name = "issue"
    version = 1

    _AC_RE = re.compile(
        r"^#{1,4}\s*acceptance\s+criteria\s*$(.*?)(?=^#{1,4}\s|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL)

    def retrieve(self, query: RetrievalQuery) -> list:
        if not query.issue_body and not query.issue_title:
            return []
        num = query.issue_number
        uri = (f"https://github.com/{query.repo}/issues/{num}"
               if query.repo and num else f"issue://{num or query.task_id}")
        body = query.issue_body or ""
        out = [Artifact(
            artifact_id=f"issue-{num or query.task_id}", uri=uri,
            artifact_type=TYPE_ISSUE, revision="",
            score=1.0, score_components={"mandatory": 1.0},
            provenance="the claimed issue itself — mandatory",
            freshness=FRESH,
            excerpt=f"# {query.issue_title}\n\n{self._strip_ac(body)}".strip(),
        )]
        ac = self.extract_ac(body)
        if ac:
            out.append(Artifact(
                artifact_id=f"issue-{num or query.task_id}-ac", uri=uri + "#acceptance-criteria",
                artifact_type=TYPE_AC, revision="",
                score=1.0, score_components={"mandatory": 1.0},
                provenance="acceptance criteria — the bar this task is judged against",
                freshness=FRESH, excerpt=ac,
            ))
        return out

    @classmethod
    def extract_ac(cls, body: str) -> str:
        m = cls._AC_RE.search(body or "")
        return m.group(1).strip() if m else ""

    @classmethod
    def _strip_ac(cls, body: str) -> str:
        return cls._AC_RE.sub("", body or "").strip()


class LexicalRepoProvider(RetrievalProvider):
    """Deterministic code/doc retrieval over the working tree (#239 scope 3).

    Uses git's own grep so results are reproducible and scoped to tracked
    files at the current revision — no index to go stale, no embeddings, and
    the revision is the commit the agent will actually be working on.
    """

    name = "lexical_repo"
    version = 1
    mode = MODE_LEXICAL

    _DOC_DIRS = ("docs/", "adr/", "doc/", "spec/")

    def __init__(self, runner=None, max_files: int = 40, excerpt_lines: int = 12):
        self._run = runner or self._git_grep
        self._max_files = max_files
        self._excerpt_lines = excerpt_lines

    @staticmethod
    def _git_grep(repo_path: str, term: str) -> list:
        try:
            r = subprocess.run(
                ["git", "-C", repo_path, "grep", "-l", "-i", "-F", term],
                capture_output=True, text=True, timeout=10)
            if r.returncode not in (0, 1):
                return []
            return [l for l in r.stdout.splitlines() if l.strip()]
        except Exception:  # noqa: BLE001 — retrieval must never break dispatch
            return []

    @staticmethod
    def _revision(repo_path: str) -> str:
        try:
            r = subprocess.run(["git", "-C", repo_path, "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            return ""

    def _classify(self, path: str) -> str:
        low = path.lower()
        if low.startswith("tests/") or "/test_" in low or low.endswith("_test.py"):
            return TYPE_TEST
        if any(low.startswith(d) for d in self._DOC_DIRS):
            return TYPE_ADR if "adr" in low else TYPE_SPEC
        return TYPE_CODE

    def retrieve(self, query: RetrievalQuery) -> list:
        if not query.repo_path or not query.keywords:
            return []
        rev = self._revision(query.repo_path)
        hits: dict = {}
        for kw in query.keywords:
            for path in self._run(query.repo_path, kw)[: self._max_files]:
                entry = hits.setdefault(path, {"terms": set()})
                entry["terms"].add(kw)
        out = []
        for path, meta in hits.items():
            atype = self._classify(path)
            n = len(meta["terms"])
            # Score is a transparent sum of named components, never a magic
            # number — #239 requires score_components.
            comps = {"term_matches": float(n),
                     "type_weight": 1.5 if atype in (TYPE_ADR, TYPE_SPEC) else 1.0}
            out.append(Artifact(
                artifact_id=f"repo:{path}", uri=path, artifact_type=atype,
                revision=rev, score=comps["term_matches"] * comps["type_weight"],
                score_components=comps,
                provenance=f"lexical match on {sorted(meta['terms'])} at {rev or 'HEAD'}",
                freshness=FRESH,
                excerpt=self._excerpt(query.repo_path, path),
            ))
        return out

    def _excerpt(self, repo_path: str, path: str) -> str:
        try:
            full = os.path.join(repo_path, path)
            with open(full, errors="replace") as f:
                head = [next(f, "") for _ in range(self._excerpt_lines)]
            return "".join(head).rstrip()
        except Exception:  # noqa: BLE001
            return ""


class EpisodicProvider(RetrievalProvider):
    """Prior attempts on this issue, from durable terminal task evidence.

    ⛔Episodes are evidence *about* work, never a substitute for the code. They
      rank below every authoritative type, and a retry gets its own prior
      failure surfaced first because that is the single most useful thing a
      second attempt can know.
    """

    name = "episodic"
    version = 1

    def __init__(self, episodes: Optional[list] = None):
        self._episodes = episodes or []

    def retrieve(self, query: RetrievalQuery) -> list:
        out = []
        for ep in self._episodes:
            if query.issue_number and ep.get("issue") != query.issue_number:
                continue
            is_prior_attempt = bool(query.retry_of) and ep.get("task_id") == query.retry_of
            comps = {"same_issue": 1.0, "prior_attempt": 2.0 if is_prior_attempt else 0.0}
            out.append(Artifact(
                artifact_id=f"episode:{ep.get('task_id')}",
                uri=f"episode://{ep.get('task_id')}",
                artifact_type=TYPE_EPISODE,
                revision=str(ep.get("completed_at") or ""),
                score=sum(comps.values()), score_components=comps,
                provenance=("prior attempt on this task — its exact failure"
                            if is_prior_attempt else "prior work on this issue"),
                freshness=FRESH,
                subject_key=f"episode:{ep.get('issue')}",
                excerpt=format_episode(ep),
            ))
        return out


# ── episodic summaries ────────────────────────────────────────────────


def build_episode(attribution: dict, result: Optional[dict] = None,
                  issue: Optional[int] = None) -> dict:
    """Compact, privacy-safe episode from durable terminal evidence (#239 §4).

    ⛔References and metadata only — no raw prompts or source content. What a
      later task needs is *what happened and where to look*, not a transcript.
    """
    result = result or {}
    return {
        "schema_version": CONTEXT_PACK_SCHEMA_VERSION,
        "task_id": attribution.get("task_id", ""),
        "issue": issue,
        "context_id": attribution.get("context_id", ""),
        "context_generation": attribution.get("context_generation", 0),
        "role": attribution.get("role", ""),
        "agent": attribution.get("agent", ""),
        "branch": attribution.get("git_branch", ""),
        "outcome": attribution.get("outcome", ""),
        "started_at": attribution.get("started_at", 0),
        "completed_at": attribution.get("completed_at", 0),
        "summary": (result.get("summary") or "")[:600],
        "findings": [str(f)[:300] for f in (result.get("findings") or [])[:8]],
        "pr_number": result.get("pr_number"),
        "retry_of": attribution.get("retry_of", ""),
        "fallback_of": attribution.get("fallback_of", ""),
    }


def format_episode(ep: dict) -> str:
    bits = [f"task {ep.get('task_id')} ({ep.get('role')}/{ep.get('agent')}) "
            f"-> {ep.get('outcome') or 'unknown'}"]
    if ep.get("branch"):
        bits.append(f"branch: {ep['branch']}")
    if ep.get("pr_number"):
        bits.append(f"PR #{ep['pr_number']}")
    if ep.get("summary"):
        bits.append(f"summary: {ep['summary']}")
    for f in ep.get("findings") or []:
        bits.append(f"finding: {f}")
    return "\n".join(bits)


def append_episode(path: str, episode: dict) -> None:
    """Append one episode line. Never raises — telemetry must not break a task."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(episode, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        logger.warning("context_pack: could not append episode to %s", path)


def load_episodes(path: str, limit: int = 200) -> list:
    try:
        if not os.path.exists(path):
            return []
        with open(path, errors="replace") as f:
            lines = f.readlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception:  # noqa: BLE001
        return []


# ── planner ───────────────────────────────────────────────────────────


def detect_conflicts(items: list) -> list:
    """Artifacts describing the same subject at different revisions.

    ⛔Surfaced, never merged. Silently preferring the higher-scoring one is how
      a stale document quietly overrides current code.
    """
    by_subject: dict = {}
    for a in items:
        by_subject.setdefault(a.subject_key, []).append(a)
    conflicts = []
    for subject, group in sorted(by_subject.items()):
        revs = {a.revision for a in group if a.revision}
        if len(revs) > 1:
            conflicts.append(
                f"{subject}: {len(group)} artifacts at differing revisions "
                f"{sorted(revs)} — confirm which is current before relying on either")
    return conflicts


def budget_for(role: str, overrides: Optional[dict] = None) -> dict:
    b = dict(DEFAULT_ROLE_BUDGETS.get(role, DEFAULT_BUDGET))
    b.setdefault("type_caps", {})
    if overrides:
        b.update(overrides)
    return b


def _sort_key(a: Artifact):
    # Deterministic total order: mandatory, then type rank, then score desc,
    # then id — so the same candidates always produce the same pack.
    return (0 if a.mandatory else 1, _TYPE_RANK.get(a.artifact_type, 99),
            -a.score, a.artifact_id)


def plan_pack(query: RetrievalQuery, providers: list, *,
              budget: Optional[dict] = None, mode: str = MODE_LEXICAL,
              timeout_s: float = 10.0) -> ContextPack:
    """Assemble a bounded, deterministic pack (#239 scope 2).

    Rules, in force order:
      1. issue + AC are mandatory and are never dropped for budget;
      2. authoritative types outrank episodic recollection;
      3. per-type caps stop one category crowding out the rest;
      4. conflicts are recorded, not resolved;
      5. a provider failing is *degradation*, reported as such — an empty pack
         must never be able to masquerade as a successful one.
    """
    started = time.time()
    pack = ContextPack(task_id=query.task_id, role=query.role, mode=mode,
                       budget=budget or budget_for(query.role))
    candidates: list = []
    for p in providers:
        if time.time() - started > timeout_s:
            pack.degraded = True
            pack.degraded_reason = f"retrieval timeout after {timeout_s:.0f}s"
            break
        try:
            candidates.extend(p.retrieve(query) or [])
        except Exception as exc:  # noqa: BLE001
            pack.degraded = True
            pack.provider_errors.append(f"{getattr(p, 'name', '?')}: {exc}")
            pack.degraded_reason = "; ".join(pack.provider_errors)
            logger.warning("context_pack: provider %s failed: %s",
                           getattr(p, "name", "?"), exc)
    pack.candidate_count = len(candidates)

    b = pack.budget
    max_tokens = int(b.get("max_tokens", DEFAULT_BUDGET["max_tokens"]))
    max_items = int(b.get("max_items", DEFAULT_BUDGET["max_items"]))
    caps = dict(b.get("type_caps") or {})

    selected, used, per_type, seen = [], 0, {}, set()
    for a in sorted(candidates, key=_sort_key):
        if a.artifact_id in seen:
            continue
        if a.mandatory:
            # Never budget-dropped. A pack that omits the AC while looking
            # complete is worse than no pack at all.
            seen.add(a.artifact_id)
            selected.append(a)
            used += a.est_tokens
            per_type[a.artifact_type] = per_type.get(a.artifact_type, 0) + 1
            continue
        if len(selected) >= max_items or used + a.est_tokens > max_tokens:
            continue
        if per_type.get(a.artifact_type, 0) >= caps.get(a.artifact_type, max_items):
            continue
        seen.add(a.artifact_id)
        selected.append(a)
        used += a.est_tokens
        per_type[a.artifact_type] = per_type.get(a.artifact_type, 0) + 1

    pack.items = selected
    pack.stale_count = sum(1 for a in selected if a.freshness == STALE)
    pack.conflicts = detect_conflicts(selected)
    pack.latency_ms = (time.time() - started) * 1000.0
    return pack


def keywords_from(query_title: str, body: str = "", extra: Optional[list] = None,
                  limit: int = 8) -> list:
    """Deterministic keyword extraction for the lexical baseline.

    Identifier-shaped tokens (`snake_case`, `dotted.path`, `#123`) are what
    actually locate code, so they are preferred over prose. Stable ordering
    keeps the whole pipeline reproducible.
    """
    text = f"{query_title}\n{body}"
    ident = re.findall(r"[A-Za-z_][A-Za-z0-9_]{4,}(?:\.[A-Za-z0-9_]+)*", text)
    stop = {"issue", "should", "which", "there", "these", "those", "where",
            "would", "could", "about", "agent", "crew", "context", "because"}
    seen, out = set(), list(extra or [])
    for tok in ident:
        low = tok.lower()
        if low in stop or low in seen:
            continue
        seen.add(low)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


# ── dispatcher-facing builder ─────────────────────────────────────────

#: Opt-in. Off by default so existing prompt composition is untouched until
#: an operator turns it on and the benchmark says it helps (#239 asks for a
#: measured baseline, not an assumed improvement).
def enabled() -> bool:
    return os.getenv("AGENT_CREW_CONTEXT_PACK", "").lower() in ("1", "true", "yes", "on")


#: Bounded `gh` lookup for an issue body the ingest path did not persist.
ISSUE_BODY_FETCH_TIMEOUT_S = 15.0


def fetch_issue_body(repo: str, issue: Optional[int],
                     timeout_s: float = ISSUE_BODY_FETCH_TIMEOUT_S) -> str:
    """Fetch an issue body via `gh`. Returns "" on any failure.

    Only used when the body was not persisted at ingest — `crew run` and
    manual enqueues never went through the watcher, so they have no stored
    body. Bounded by `timeout_s`; the caller treats "" as a degradation when
    an issue number exists, never as "the issue has no criteria".
    """
    if not repo or not issue:
        return ""
    try:
        r = subprocess.run(
            ["gh", "issue", "view", str(issue), "--repo", repo, "--json", "body"],
            capture_output=True, text=True, timeout=timeout_s)
        if r.returncode != 0:
            return ""
        return (json.loads(r.stdout or "{}").get("body") or "")
    except Exception:  # noqa: BLE001 — never break a dispatch on a lookup
        return ""


def resolve_issue_body(ctx: dict, *, issue_body_fn=None) -> tuple:
    """``(body, source)`` for this task's issue.

    Order matters and is deliberate:
      1. `ctx["issue_body"]` — persisted by the watcher at discovery, so the
         common path costs no network call and survives a GitHub outage;
      2. an injected `issue_body_fn` (tests, or a caller with its own source);
      3. a bounded `gh` lookup, for tasks that never went through ingest.

    ⛔Returns the source so the caller can tell "no issue" from "issue whose
      body we could not read". Those must not look the same: the second one
      means the acceptance criteria are missing and the pack has to say so.
    """
    ctx = ctx or {}
    stored = (ctx.get("issue_body") or "").strip()
    if stored:
        return stored, "ingest"
    issue = ctx.get("issue") if isinstance(ctx.get("issue"), int) else None
    if issue_body_fn is not None:
        try:
            body = (issue_body_fn(ctx.get("repo", ""), issue) or "").strip()
        except Exception:  # noqa: BLE001
            return "", "lookup_failed"
        return (body, "injected") if body else ("", "lookup_failed")
    if issue:
        body = fetch_issue_body(ctx.get("repo", ""), issue).strip()
        return (body, "github") if body else ("", "lookup_failed")
    return "", "no_issue"


def _procedure_triggers(episodes: list, issue: Optional[int]) -> tuple:
    """`(outcome_signature, findings)` from prior work on this issue.

    These are exactly the signals #240's procedures trigger on: a recurring
    terminal outcome, and review findings that keep coming back. Taking them
    from persisted episodes means a procedure fires on evidence rather than on
    a guess about what this task might do.
    """
    sig, findings = "", []
    for ep in episodes or []:
        if issue and ep.get("issue") != issue:
            continue
        if not sig and (ep.get("outcome") or "").startswith("failed"):
            sig = ep["outcome"]
        findings.extend(str(f) for f in (ep.get("findings") or []))
    return sig, findings


#: A path-looking token inside issue text: at least one directory segment and
#: a short extension. Deliberately strict — a loose pattern would manufacture
#: scope out of prose, and a wrong module is worse than no module.
_PATH_TOKEN_RE = re.compile(r"(?<![\w/.])((?:[\w.-]+/)+[\w-]+\.[A-Za-z0-9]{1,5})\b")

#: Bound on how much scope one task may claim, so a pathological issue body
#: cannot turn into thousands of comparisons.
MAX_RESOLVED_SCOPE = 40


def resolve_task_scope(ctx: dict) -> tuple:
    """``(paths, modules)`` this task is about, best-effort and bounded.

    Three sources, most authoritative first:

      1. `ctx["modules"]` / `ctx["paths"]` / `ctx["files"]` — whatever the
         producer knew explicitly;
      2. file paths named in the issue title and body, which for an ordinary
         bug report is the only pre-work signal of where the change goes;
      3. nothing — and that is a real, common answer.

    ⛔Case 3 is the load-bearing one. It resolves to an empty list, and an empty
      list makes `Scope.matches` REJECT every module- or path-scoped rule. An
      unresolvable task must not be treated as matching everything: that is
      precisely the defect this function was added to close (review of PR #242,
      round 2), where a module-scoped rule reached every task in the repo.
    """
    from agent_crew.procedural_memory import normalise_module

    ctx = ctx or {}

    def _tokens(value) -> list:
        if isinstance(value, str):
            return [t.strip() for t in value.replace(",", " ").split() if t.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(t).strip() for t in value if str(t).strip()]
        return []

    paths = _tokens(ctx.get("paths")) + _tokens(ctx.get("files"))
    text = " ".join(str(ctx.get(k) or "") for k in ("issue_title", "issue_body"))
    paths.extend(m.group(1) for m in _PATH_TOKEN_RE.finditer(text))

    modules = [normalise_module(m) for m in _tokens(ctx.get("modules"))]
    modules.extend(normalise_module(p) for p in paths)

    return (_dedupe(paths)[:MAX_RESOLVED_SCOPE],
            _dedupe(m for m in modules if m)[:MAX_RESOLVED_SCOPE])


def _dedupe(items) -> list:
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def load_matching_procedures(procedures_path: str, *, repo: str, task_type: str,
                             role: str, episodes: Optional[list] = None,
                             issue: Optional[int] = None,
                             paths: Optional[list] = None,
                             modules: Optional[list] = None) -> list:
    """Active, in-scope, triggered procedures for this task (#240 §4).

    ⛔Imported lazily and wrapped: procedural memory is derived governance and
      must never be able to break a dispatch. A failure here yields no
      procedures, not an exception.
    """
    if not procedures_path or not os.path.exists(procedures_path):
        return []
    try:
        from agent_crew import procedural_memory as _pm

        sig, findings = _procedure_triggers(episodes or [], issue)
        return _pm.match_procedures(
            _pm.load_procedures(procedures_path),
            repo=repo, task_type=task_type, role=role,
            paths=paths or [""], modules=modules or [],
            outcome_signature=sig, findings=findings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("context_pack: procedure matching failed: %s", exc)
        return []


def _record_shadow(shadow_path: str, matched: list, task_id: str) -> None:
    """Append one shadow record per matched procedure. Never raises.

    ⛔Recorded, never applied. Hard enforcement is only justifiable once these
      show an acceptable false-positive rate (#240 §5).
    """
    try:
        from agent_crew import procedural_memory as _pm

        os.makedirs(os.path.dirname(shadow_path) or ".", exist_ok=True)
        with open(shadow_path, "a") as f:
            for proc, reason in matched:
                rec = _pm.shadow_record(
                    proc, task_id=task_id, triggered=True,
                    would=proc.required_action or proc.prohibited_action
                    or proc.rule)
                rec["reason"] = reason
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("context_pack: shadow record failed: %s", exc)


def build_pack_for_task(task_context: dict, *, task_id: str, task_type: str,
                        role: str, repo_path: str = "", branch: str = "",
                        episodes_path: str = "", procedures_path: str = "",
                        shadow_path: str = "", issue_body_fn=None,
                        budget: Optional[dict] = None,
                        extra_providers: Optional[list] = None,
                        mode: str = MODE_LEXICAL,
                        timeout_s: float = 10.0) -> ContextPack:
    """Assemble the pack for one dispatch. Never raises.

    ⛔Fail-soft by contract: any failure yields a *degraded* pack that says so,
      never a silent empty one. A caller must be able to tell "nothing was
      relevant" from "retrieval broke".
    """
    ctx = task_context or {}
    issue = ctx.get("issue") if isinstance(ctx.get("issue"), int) else None
    body, body_source = resolve_issue_body(ctx, issue_body_fn=issue_body_fn)
    title = ctx.get("issue_title", "") or ""
    query = RetrievalQuery(
        task_id=task_id, task_type=task_type, role=role,
        repo=ctx.get("repo", "") or "", repo_path=repo_path,
        issue_number=issue, issue_title=title, issue_body=body, branch=branch,
        keywords=keywords_from(title, body),
        retry_of=str(ctx.get("retry_of") or ""),
    )
    providers = [IssueProvider(), LexicalRepoProvider()]
    episodes = load_episodes(episodes_path) if episodes_path else []
    if episodes_path:
        providers.append(EpisodicProvider(episodes))
    # #240 (review-2016dcf3): actually load persisted procedures. Without this
    # the feature existed only in tests that instantiated the provider by
    # hand — no active procedure could reach a real dispatch and no shadow
    # telemetry ever accrued.
    # #240 (review-1d2e7467): resolve what this task is ABOUT before matching.
    # `modules` is a declared scope dimension; leaving it unresolved meant a
    # module-scoped rule was in scope for every task in the repo. Resolving
    # `paths` from the same signals fixes the mirror-image defect on that
    # dimension, which was dead rather than over-broad.
    _scope_paths, _scope_modules = resolve_task_scope(ctx)
    matched = load_matching_procedures(
        procedures_path, repo=ctx.get("repo", "") or "", task_type=task_type,
        role=role, episodes=episodes, issue=issue,
        paths=_scope_paths, modules=_scope_modules)
    if matched:
        providers.append(ProcedureProvider(matched))
    providers.extend(extra_providers or [])
    try:
        pack = plan_pack(query, providers,
                         budget=budget or budget_for(role), mode=mode,
                         timeout_s=timeout_s)
        # ⛔The acceptance criteria are the one thing #239 promises never to
        #   drop. If this task HAS an issue but we could not read its body, the
        #   AC is missing and the pack must say so — a silently AC-less pack
        #   that reports itself healthy is exactly the failure this guard
        #   exists to prevent (review of PR #241).
        if issue and body_source == "lookup_failed":
            pack.degraded = True
            pack.degraded_reason = "; ".join(filter(None, [
                pack.degraded_reason,
                f"issue #{issue} body unavailable — acceptance criteria are "
                f"NOT in this pack; read the issue before relying on it",
            ]))
        elif issue and body and not any(a.artifact_type == TYPE_AC
                                        for a in pack.items):
            # Body present but no AC heading found. Not a failure — many
            # issues have none — so it is stated, not flagged as degraded.
            logger.info("context_pack: issue #%s has no acceptance-criteria "
                        "section", issue)
        if matched and shadow_path:
            _record_shadow(shadow_path, matched, task_id)
        return pack
    except Exception as exc:  # noqa: BLE001
        pack = ContextPack(task_id=task_id, role=role, mode=mode,
                           budget=budget or budget_for(role))
        pack.degraded = True
        pack.degraded_reason = f"pack build failed: {exc}"
        logger.warning("context_pack: build failed for %s: %s", task_id, exc)
        return pack


class ProcedureProvider(RetrievalProvider):
    """Active procedures from #240, matched to this task.

    Two separate mechanisms keep a rule from overruling the truth, and they
    are worth not confusing:
      - the issue and its AC are **mandatory**, and mandatory items sort ahead
        of everything else regardless of type rank;
      - `_TYPE_RANK` is what puts procedures below the *non-mandatory*
        authoritative types (ADR, spec, code, test).
    Together they deliver #240's non-goal: derived governance never outranks
    current code or the acceptance criteria. Each item states why it matched;
    an inclusion without a stated reason is not allowed in.
    """

    name = "procedural"
    version = 1

    def __init__(self, matched: Optional[list] = None):
        # (procedure, reason) pairs, already scoped and triggered by
        # procedural_memory.match_procedures().
        self._matched = matched or []

    def retrieve(self, query: RetrievalQuery) -> list:
        out = []
        for proc, reason in self._matched:
            hard = proc.enforcement == "hard"
            out.append(Artifact(
                artifact_id=f"procedure:{proc.key}",
                uri=f"procedure://{proc.procedure_id}/v{proc.version}",
                artifact_type=TYPE_PROCEDURE,
                revision=str(proc.version),
                score=1.0 + (1.0 if hard else 0.0),
                score_components={"matched": 1.0, "hard": 1.0 if hard else 0.0},
                provenance=f"procedure {proc.key}: {reason}",
                freshness=FRESH,
                subject_key=f"procedure:{proc.procedure_id}",
                excerpt=proc.render(),
            ))
        return out
