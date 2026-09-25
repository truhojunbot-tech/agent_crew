"""The one place production constructs the CEA inputs (ADR Π P1, P6, P7, §5, O9).

⛔Why this module exists at all. Until it landed, ``TaskQueue`` defaulted to *no*
  providers, which is honest but inert: every input reported unavailable, every
  receipt was ``BLOCK`` with ``…_UNAVAILABLE``, and ``set_default_runtime_authority``
  had no production call site at all — only tests — so an ACTIVE restoration was
  always refused no matter what the signed snapshot said. A signed snapshot file
  sitting on disk cannot make a proof valid through a server that never reads it
  (codex plan review of ``CEA-LIVE-DEPLOY-PLAN.md``, P1). Wiring is therefore a
  *build step with a name*, not something each call site improvises.

What it does **not** do, deliberately:

* it never decides anything. Every branch here answers one question — "is this
  input readable?" — and the answer becomes a provider that reports data, or the
  engine's own ``Unavailable*`` stub that reports absence. P7 turns absence into
  BLOCK, in the engine, where it is recorded on a receipt.
* it never raises into the caller. A misconfigured path, an unreadable key, a
  provider whose constructor throws: each degrades that one slot to
  ``UNAVAILABLE`` with the reason, because in ``mode=shadow`` an exception out of
  here would take down organic dispatch to enforce a policy that is not yet
  enforcing. The whole point of shadow is that it cannot hurt.

Environment (all read-only; nothing here writes):

=======================================  ===================================================
env                                      meaning
=======================================  ===================================================
``AGENT_CREW_CEA_REGISTRY_PATH``         E4 capability registry JSON (alfred governance/)
``AGENT_CREW_CEA_SNAPSHOT_PATH``         §5.3 canonical policy snapshot JSON
``AGENT_CREW_CEA_SNAPSHOT_KEY_FILE``     HMAC key the snapshot signature is checked with;
                                         absent ⇒ snapshot UNKEYED/UNSIGNED ⇒ unverified
                                         input ⇒ BLOCK, exactly as before this module
``AGENT_CREW_CEA_MEMORY_CMD``            L2/L3 provider command (``python3 …/admission_inputs.py``)
``AGENT_CREW_CEA_QUOTA_CACHE_DIR``       Qouta ``<dir>/<provider>_monitor/quota_cache.json``
``AGENT_CREW_CEA_CODEX_AUTH_PATH``       Codex account identity JSON (default ``~/.codex/auth.json``)
``AGENT_CREW_CEA_COOLDOWN_FILE``         #308 cooldown JSON ``{provider: until_epoch}``
``AGENT_CREW_CEA_INCIDENT_MEMORY``       L3 incident memory JSON (passed to the L3 command)
``AGENT_CREW_CEA_CREDIT_CLASS``          JSON ``{provider: "plan"|"paid"|"overage"}`` (O9)
=======================================  ===================================================

The step-3 spellings (``AGENT_CREW_CEA_POLICY_SNAPSHOT``,
``AGENT_CREW_CEA_CAPABILITY_REGISTRY``, ``AGENT_CREW_CEA_ADMISSION_INPUTS``) are
accepted as aliases so an already-configured runtime does not silently lose its
providers on upgrade; the names above win when both are set.

``AGENT_CREW_CEA_ENGINE_ENDPOINT`` / ``AGENT_CREW_CEA_ADAPTER_TOKEN_FILE`` are
left unset in this deployment, so :func:`~agent_crew.cea.engine.get_engine`
returns the **in-process** engine and these providers are the ones it reads. When
an endpoint is configured the engine lives in ``crew-authz`` and wires its own
inputs there; the providers built here would then belong to a process that does
not decide, so :func:`build_wiring` reports that and wires nothing.
"""
from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from typing import Optional

WIRED = "WIRED"
UNAVAILABLE = "UNAVAILABLE"

DEFAULT_REGISTRY_PATH = "/home/truhojun/alfred/governance/capability_registry.json"
DEFAULT_SNAPSHOT_PATH = "/home/truhojun/alfred/governance/control_policy_snapshot.json"
DEFAULT_MEMORY_CMD = "python3 /home/truhojun/alfred/tools/admission_inputs.py"

#: Provider slots, in the order the startup log prints them. These are exactly the
#: keyword names :class:`~agent_crew.cea.engine.AuthorizationEngine` takes, so
#: ``AuthorizationEngine(**wiring.providers)`` is the whole contract.
SLOTS = ("capabilities", "snapshots", "runtime", "budgets", "gates")


@dataclass(frozen=True)
class ProviderStatus:
    """One slot's outcome. ``reason`` is the operator-facing half — an
    ``UNAVAILABLE`` with no reason is the thing that made this whole area
    un-debuggable, so it is not representable here."""
    name: str
    wired: bool
    reason: str

    @property
    def state(self) -> str:
        return WIRED if self.wired else UNAVAILABLE

    def line(self) -> str:
        return f"{self.name}={self.state} ({self.reason})"


@dataclass(frozen=True)
class Wiring:
    """What startup built, and the evidence for it."""
    providers: dict = field(default_factory=dict)
    statuses: tuple[ProviderStatus, ...] = ()
    authority: object = None
    """A :class:`~agent_crew.queue.SnapshotLooseningAuthority`, or ``None`` ⇒ the
    fail-closed :class:`~agent_crew.queue.RefuseAllLoosening` stays in place."""
    authority_reason: str = ""
    mode: str = ""
    endpoint: Optional[str] = None

    def status(self, name: str) -> Optional[ProviderStatus]:
        return next((s for s in self.statuses if s.name == name), None)

    @property
    def wired_slots(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.statuses if s.wired)

    def log_line(self) -> str:
        """The single startup line. One line, because an operator reads it in a
        log tail next to ``build provenance:`` and needs both to fit."""
        where = f"endpoint={self.endpoint}" if self.endpoint else "engine=in-process"
        return ("CEA wiring: mode=%s %s | %s | loosening_authority=%s"
                % (self.mode, where, " ".join(s.line() for s in self.statuses),
                   self.authority_reason))


# ── env reading ─────────────────────────────────────────────────────────────

def _first(env: dict, *names: str) -> str:
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def _readable_file(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.R_OK)


def _split_command(command: str) -> tuple[Optional[str], Optional[str]]:
    """``"python3 /path/to/x.py"`` → ``(interpreter, script)``; a bare path → ``(None, path)``.

    The L3 provider is configured as a *command* because that is how the alfred
    contract documents it (``python3 <path> < request > answer``), and because a
    venv interpreter is part of what makes it runnable.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return None, None
    if not parts:
        return None, None
    if len(parts) == 1:
        return None, parts[0]
    return parts[0], parts[1]


def _credit_class(env: dict) -> dict:
    raw = _first(env, "AGENT_CREW_CEA_CREDIT_CLASS")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


# ── the factory ─────────────────────────────────────────────────────────────

def build_wiring(env: Optional[dict] = None, *, db_path: Optional[str] = None,
                 project: Optional[str] = None) -> Wiring:
    """Build every P1 input provider this runtime can actually read.

    ``db_path`` is the queue database the P6 runtime row lives in; ``None`` (or a
    database that does not exist yet) leaves the runtime slot UNAVAILABLE, which
    the engine treats as STOPPED under P7.

    Never raises.
    """
    from agent_crew.cea.engine import EngineConfig

    e: dict = dict(os.environ) if env is None else dict(env)
    try:
        config = EngineConfig.from_env(e, project=project)
        mode, endpoint = config.mode, config.endpoint
    except Exception as exc:                     # noqa: BLE001 — startup logs it, dispatch survives
        mode, endpoint = "unknown", None
        return Wiring(providers={}, statuses=(ProviderStatus("config", False, repr(exc)),),
                      authority_reason="not installed (engine config unreadable)",
                      mode=mode, endpoint=endpoint)

    if endpoint:
        # The deciding process is elsewhere; providers built here would be read by
        # nobody. Saying so beats building five objects and implying they matter.
        return Wiring(
            providers={},
            statuses=tuple(ProviderStatus(slot, False, "engine is remote; crew-authz wires its own")
                           for slot in SLOTS),
            authority_reason="not installed (engine endpoint configured)",
            mode=mode, endpoint=endpoint)

    providers: dict = {}
    statuses: list[ProviderStatus] = []

    snapshot_reader, snapshot_status, snapshot_verified = _snapshot(e)
    statuses.append(snapshot_status)
    if snapshot_reader is not None:
        providers["snapshots"] = snapshot_reader

    client, memory_status = _memory_client(e)
    e4, cap_status = _capabilities(e, client)
    statuses.append(cap_status)
    if e4 is not None:
        providers["capabilities"] = e4

    gate, gate_status = _gates(client, snapshot_status.wired)
    statuses.append(gate_status)
    if gate is not None:
        providers["gates"] = gate

    runtime, runtime_status = _runtime(db_path)
    statuses.append(runtime_status)
    if runtime is not None:
        providers["runtime"] = runtime

    budgets, budget_status = _budgets(e)
    statuses.append(budget_status)
    if budgets is not None:
        providers["budgets"] = budgets

    statuses.append(memory_status)

    authority, authority_reason = _authority(snapshot_reader, snapshot_verified, project)
    return Wiring(providers=providers, statuses=tuple(statuses), authority=authority,
                  authority_reason=authority_reason, mode=mode, endpoint=None)


def _snapshot(env: dict):
    """(reader | None, status, verifier_configured)."""
    from agent_crew.cea.input_providers.snapshot import (
        CanonicalPolicySnapshotReader, hmac_sha256_verifier)

    path = _first(env, "AGENT_CREW_CEA_SNAPSHOT_PATH",
                  "AGENT_CREW_CEA_POLICY_SNAPSHOT") or DEFAULT_SNAPSHOT_PATH
    if not _readable_file(path):
        return None, ProviderStatus("snapshots", False, f"no readable snapshot at {path}"), False

    key_path = _first(env, "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE")
    verifier, note = None, ""
    if not key_path:
        note = "unkeyed — AGENT_CREW_CEA_SNAPSHOT_KEY_FILE unset, so the snapshot is an " \
               "unverified input and admission BLOCKs (P7)"
    else:
        try:
            with open(key_path, "rb") as fh:
                key = fh.read().strip()
        except OSError as exc:
            key = b""
            note = f"key file {key_path} unreadable ({exc.strerror}); snapshot stays unverified"
        if key:
            verifier = hmac_sha256_verifier(key)
            note = f"hmac-sha256 verified against {key_path}"
        elif not note:
            note = f"key file {key_path} is empty; snapshot stays unverified"

    try:
        reader = CanonicalPolicySnapshotReader(path, verifier=verifier, env=env)
    except Exception as exc:                     # noqa: BLE001
        return None, ProviderStatus("snapshots", False, f"reader construction failed: {exc!r}"), False
    return reader, ProviderStatus("snapshots", True, f"{path}; {note}"), verifier is not None


def _memory_client(env: dict):
    """The shared L2/L3 client — one per wiring, because E4 and incident memory
    are two blocks of the *same* answer and re-spawning the script per block
    would let them disagree about one intent."""
    from agent_crew.cea.input_providers.admission_inputs import AdmissionInputsClient

    command = _first(env, "AGENT_CREW_CEA_MEMORY_CMD") or (
        _first(env, "AGENT_CREW_CEA_ADMISSION_INPUTS") or DEFAULT_MEMORY_CMD)
    python, script = _split_command(command)
    if not script or not _readable_file(script):
        return None, ProviderStatus("l3_memory", False,
                                    f"no readable L2/L3 command script at {script or command!r}")
    registry = _first(env, "AGENT_CREW_CEA_REGISTRY_PATH",
                      "AGENT_CREW_CEA_CAPABILITY_REGISTRY") or DEFAULT_REGISTRY_PATH
    child = {"AGENT_CREW_CEA_CAPABILITY_REGISTRY": registry}
    incident = _first(env, "AGENT_CREW_CEA_INCIDENT_MEMORY")
    if incident:
        child["AGENT_CREW_CEA_INCIDENT_MEMORY"] = incident
    try:
        client = AdmissionInputsClient(script, python=python, env=env, child_env=child)
    except Exception as exc:                     # noqa: BLE001
        return None, ProviderStatus("l3_memory", False, f"client construction failed: {exc!r}")
    return client, ProviderStatus("l3_memory", True, f"{command}; registry={registry}")


def _capabilities(env: dict, client):
    from agent_crew.cea.memory import MemoryCapabilities
    from agent_crew.cea.input_providers.capability import E4CapabilityProvider

    registry = _first(env, "AGENT_CREW_CEA_REGISTRY_PATH",
                      "AGENT_CREW_CEA_CAPABILITY_REGISTRY") or DEFAULT_REGISTRY_PATH
    if client is None:
        # E4 is reached *through* the L2/L3 command (it is the only matcher, §5.1);
        # a registry file we can see but not query is not a matcher.
        return None, ProviderStatus("capabilities", False,
                                    "L2/L3 command unavailable, so E4 cannot be queried")
    if not _readable_file(registry):
        return None, ProviderStatus("capabilities", False, f"no readable E4 registry at {registry}")
    try:
        return (MemoryCapabilities(E4CapabilityProvider(client), client),
                ProviderStatus("capabilities", True, f"E4 via L2/L3 command; registry={registry}"))
    except Exception as exc:                     # noqa: BLE001
        return None, ProviderStatus("capabilities", False, f"construction failed: {exc!r}")


def _gates(client, snapshot_wired: bool):
    from agent_crew.cea.input_providers.gate import SnapshotRecordGate
    from agent_crew.cea.memory import MemoryGate

    try:
        gate = SnapshotRecordGate()
    except Exception as exc:                     # noqa: BLE001
        return None, ProviderStatus("gates", False, f"construction failed: {exc!r}")
    if client is None:
        if not snapshot_wired:
            # With no snapshot and no L3, this gate can only ever answer
            # NOT_REQUIRED — which is what the engine's own default already
            # answers. Installing it would dress absence up as configuration.
            return None, ProviderStatus("gates", False,
                                        "no snapshot and no L3 memory: nothing to gate on")
        return gate, ProviderStatus("gates", True,
                                    "snapshot record gate (J8); no L3 memory tightening")
    return (MemoryGate(gate, client),
            ProviderStatus("gates", True, "snapshot record gate (J8) tightened by L3 memory"))


def _runtime(db_path: Optional[str]):
    from agent_crew.cea.input_providers.runtime import QueueRuntimeStateProvider

    if not db_path:
        return None, ProviderStatus("runtime", False, "no queue database path given")
    if not os.path.exists(db_path):
        return None, ProviderStatus("runtime", False, f"queue database {db_path} does not exist yet")
    try:
        return (QueueRuntimeStateProvider(db_path),
                ProviderStatus("runtime", True, f"P6 row in {db_path}"))
    except Exception as exc:                     # noqa: BLE001
        return None, ProviderStatus("runtime", False, f"construction failed: {exc!r}")


def _budgets(env: dict):
    from agent_crew.cea.input_providers.budget import DEFAULT_QUOTA_DIR, QuotaBudgetProvider

    quota_dir = _first(env, "AGENT_CREW_CEA_QUOTA_CACHE_DIR") or DEFAULT_QUOTA_DIR
    if not os.path.isdir(quota_dir):
        return None, ProviderStatus("budgets", False, f"no Qouta cache directory at {quota_dir}")
    cooldown = _first(env, "AGENT_CREW_CEA_COOLDOWN_FILE") or None
    try:
        provider = QuotaBudgetProvider(quota_dir, cooldown_file=cooldown,
                                       credit_class=_credit_class(env), env=env)
    except Exception as exc:                     # noqa: BLE001
        return None, ProviderStatus("budgets", False, f"construction failed: {exc!r}")
    return provider, ProviderStatus("budgets", True,
                                    f"{quota_dir}; cooldown={cooldown or 'none'}")


def _authority(snapshot_reader, verifier_configured: bool, project: Optional[str]):
    """P6: who may loosen this runtime out of QUARANTINED / STOPPED.

    Installed **only** when the snapshot is both readable and verifiable. With no
    verifier no snapshot can ever be ``VALID``, so
    :class:`~agent_crew.queue.SnapshotLooseningAuthority` would refuse every
    request anyway — installing it there would swap one fail-closed object for
    another while implying a verification path exists. Leaving
    :class:`~agent_crew.queue.RefuseAllLoosening` says the true thing, and its
    refusal message names the missing verifier.
    """
    if snapshot_reader is None:
        return None, "RefuseAllLoosening (no readable policy snapshot)"
    if not verifier_configured:
        return None, ("RefuseAllLoosening (snapshot unverifiable — "
                      "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE unset)")
    try:
        from agent_crew.queue import SnapshotLooseningAuthority
        authority = SnapshotLooseningAuthority(snapshot_reader, runtime=project or "")
    except Exception as exc:                     # noqa: BLE001
        return None, f"RefuseAllLoosening (authority construction failed: {exc!r})"
    return authority, "SnapshotLooseningAuthority over the verified snapshot"


def build_engine_from_env(env: Optional[dict] = None, *, db_path: Optional[str] = None,
                          project: Optional[str] = None):
    """``(engine, wiring)`` — the engine this runtime should use, and why.

    The engine is built with an explicit config, so it is *this* runtime's engine
    rather than the process-wide cached one: two projects on one box may sit at
    different rollout modes (step 4c) and must not share a mode by accident.
    """
    from agent_crew.cea.engine import EngineConfig, get_engine

    wiring = build_wiring(env, db_path=db_path, project=project)
    try:
        config = EngineConfig.from_env(dict(os.environ) if env is None else dict(env),
                                       project=project)
        engine = get_engine(config=config, **wiring.providers)
    except Exception as exc:                     # noqa: BLE001 — shadow never breaks dispatch
        return None, Wiring(providers=wiring.providers,
                            statuses=wiring.statuses + (ProviderStatus("engine", False, repr(exc)),),
                            authority=wiring.authority, authority_reason=wiring.authority_reason,
                            mode=wiring.mode, endpoint=wiring.endpoint)
    return engine, wiring


def install_from_env(env: Optional[dict] = None, *, db_path: Optional[str] = None,
                     project: Optional[str] = None, logger=None) -> Wiring:
    """Build the wiring, install the P6 loosening authority, log one line.

    ⛔Installs the process-wide authority only when there *is* one. Calling
      :func:`~agent_crew.queue.set_default_runtime_authority` with ``None`` would
      not be a no-op: it resets whatever a surrounding process (a harness, an
      embedding caller) had installed, so "we found nothing" would become "we
      removed yours". Absence is expressed by leaving the fail-closed default
      alone, which is what it already is.

    Never raises.
    """
    try:
        wiring = build_wiring(env, db_path=db_path, project=project)
    except Exception as exc:                     # noqa: BLE001
        wiring = Wiring(statuses=(ProviderStatus("wiring", False, repr(exc)),),
                        authority_reason="not installed (wiring failed)", mode="unknown")
    if wiring.authority is not None:
        try:
            from agent_crew.queue import set_default_runtime_authority
            set_default_runtime_authority(wiring.authority)
        except Exception as exc:                 # noqa: BLE001
            wiring = Wiring(providers=wiring.providers, statuses=wiring.statuses,
                            authority=None,
                            authority_reason=f"RefuseAllLoosening (install failed: {exc!r})",
                            mode=wiring.mode, endpoint=wiring.endpoint)
    if logger is not None:
        try:
            logger.info("%s", wiring.log_line())
        except Exception:                        # noqa: BLE001 — a log line is never fatal
            pass
    return wiring


__all__ = ["DEFAULT_MEMORY_CMD", "DEFAULT_REGISTRY_PATH", "DEFAULT_SNAPSHOT_PATH", "SLOTS",
           "UNAVAILABLE", "WIRED", "ProviderStatus", "Wiring", "build_engine_from_env",
           "build_wiring", "install_from_env"]
