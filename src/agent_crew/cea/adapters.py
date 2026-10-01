"""§7 — the ingress adapters. Every way a task can enter, named once.

ADR §7.1: an adapter is thin by definition. It *translates* whatever its
transport speaks into an :class:`~agent_crew.cea.intent.Intent`, it
*authenticates* with its own credential, and it hands both to the engine. It
does not decide. Anything an adapter decides is a decision the receipt cannot
explain, which is the shape every bypass in this system has had.

The credential is per-adapter by design. Today the in-process adapters share the
loopback mint in :mod:`agent_crew.cea.auth` (and that module says at length why
that is not a boundary); when
``AGENT_CREW_CEA_ENGINE_ENDPOINT`` points the engine at another process, the
adapter presents its token from ``AGENT_CREW_CEA_ADAPTER_TOKEN_FILE`` and a
peer that is not the caller checks it. Either way the receipt records
``caller_identity_status: UNVERIFIED`` — under one uid there is nothing else it
could honestly say (P2a).

⛔``provenance`` is an **audit** field (§3), never an admission input. Naming
  the ingress correctly does not buy that ingress anything; it makes the
  receipt able to answer "where did this come from", which is the question
  every post-incident read of this system has actually needed. That is also why
  ``coordinator_managed`` belongs here and not in a suppression branch: what it
  truthfully records is *which adapter enqueued the task*, and the moment it
  starts deciding whether a cascade runs it has become an admission input a
  caller sets in a JSON body.

Each entry below is an ingress that exists today. The static test in
``tests/unit/test_sev0_cea_s2b_adapters.py`` asserts that no enqueue call site
in the product relies on the default provenance — because the default silently
labelled watch ingestion, retries and cascades all as ``direct``, and a receipt
that says ``direct`` about a cascade successor is an audit field that lies.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent_crew.cea.intent import CallerProvenance


@dataclass(frozen=True)
class Ingress:
    """One way in: its id, the provenance its receipts carry, and where it lives."""
    id: str
    provenance: CallerProvenance
    where: str


#: The ingresses that exist in this build — no more, and no aspirational ones.
#:
#: ⛔MCP is deliberately absent. It has no ``enqueue`` of its own: its workers
#:   create tasks by POSTing ``/tasks`` (``http.tasks``) and its cascade helpers
#:   are the same ``pipeline`` functions the HTTP path uses. Listing an
#:   ``mcp.create_task`` that nothing calls would make the registry describe a
#:   boundary that is not there — the exact failure mode §7 is closing.
INGRESSES: tuple[Ingress, ...] = (
    Ingress("http.tasks", CallerProvenance.DIRECT, "server.POST /tasks"),
    Ingress("cli.enqueue", CallerProvenance.MANUAL, "cli — crew run / crew task"),
    Ingress("cli.discuss", CallerProvenance.MANUAL, "discussion.enqueue_discussion"),
    Ingress("loop.implement", CallerProvenance.MANUAL, "loop.enqueue_implement"),
    Ingress("loop.review", CallerProvenance.MANUAL, "loop.enqueue_review"),
    Ingress("loop.test", CallerProvenance.MANUAL, "loop.enqueue_test"),
    Ingress("cascade.review", CallerProvenance.CASCADE, "pipeline.auto_enqueue_review"),
    Ingress("cascade.test", CallerProvenance.CASCADE, "pipeline.auto_enqueue_test"),
    Ingress("cascade.fix", CallerProvenance.CASCADE, "pipeline.auto_enqueue_fix"),
    Ingress("cascade.fallback", CallerProvenance.CASCADE, "pipeline — fallback successor"),
    Ingress("retry.failed_task", CallerProvenance.RETRY, "server._auto_retry_failed_task"),
    Ingress("watchdog.stale_review", CallerProvenance.WATCHDOG,
            "server — re-dispatch a review of a moved head"),
    Ingress("cron.watch", CallerProvenance.CRON, "watch — GitHub issue ingestion"),
    Ingress("cron.triage", CallerProvenance.CRON, "triage — issue triage"),
)

BY_ID = {ingress.id: ingress for ingress in INGRESSES}


def provenance_of(ingress_id: str) -> CallerProvenance:
    """The provenance an adapter stamps, by id.

    ⛔Raises on an unknown id rather than falling back to ``DIRECT``. A silent
      fallback is how every ingress came to be labelled ``direct`` in the first
      place: the default was reachable, so nothing ever had to name itself.
    """
    try:
        return BY_ID[ingress_id].provenance
    except KeyError:
        raise KeyError(
            f"{ingress_id!r} is not a registered §7 ingress; add it to "
            f"agent_crew.cea.adapters.INGRESSES rather than passing a provenance "
            f"inline — the registry is what makes 'every ingress is an adapter' "
            f"checkable") from None


__all__ = ["BY_ID", "INGRESSES", "Ingress", "provenance_of"]
