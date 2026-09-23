"""The engine's service boundary — a unix socket in front of :mod:`agent_crew.cea.engine`.

P1 says one component decides. It does not say that component must live in this
process, and the ADR's ``crew-authz`` deployment moves it out. The point of this
module is that moving it is a **config change**: adapters call
:func:`agent_crew.cea.engine.get_engine` and get either the in-process engine or
:class:`UnixSocketEngineClient`, with the same ``authorize(conn, intent, caller)``
signature, depending on ``AGENT_CREW_CEA_ENGINE_ENDPOINT``.

A unix socket, not TCP: file permissions are the only access control available
under one uid, and ``0600`` on the socket path is at least a boundary a
misconfigured neighbour trips over. It is still not the credential boundary P2a
wants — that needs distinct uids — and the receipts keep saying so.
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
from dataclasses import asdict
from typing import Optional

from agent_crew.cea.engine import (
    Authorization, AuthorizationEngine, EngineConfig, EngineError)
from agent_crew.cea.intent import (
    Caller, CallerProvenance, IdentityStatus, Intent, IntentIdentity, Target, WorkClass)

_MAX_FRAME = 1 << 20


# ── wire format ─────────────────────────────────────────────────────────────
# One JSON object per connection, length-prefixed by a trailing newline. Small
# enough to read by eye in a tcpdump, which matters for an audit boundary.

def encode_intent(intent: Intent, caller: Caller, *, retry: bool = False) -> dict:
    return {
        "intent": {
            "identity": {
                "project": intent.identity.project,
                "work_class": getattr(intent.identity.work_class, "value",
                                      intent.identity.work_class),
                "target": {
                    "repo": intent.identity.target.repo,
                    "base_ref": intent.identity.target.base_ref,
                    "scope_anchors": list(intent.identity.target.scope_anchors),
                },
                "capability_id": intent.identity.capability_id,
                "authority_decision_ids": list(intent.identity.authority_decision_ids),
            },
            "task_id": intent.task_id,
            "task_type": intent.task_type,
            "description": intent.description,
            "coordinator_id": intent.coordinator_id,
            "shadow": intent.shadow,
            "idempotency_key": intent.idempotency_key,
            "parent_receipt_id": intent.parent_receipt_id,
            "extra": dict(intent.extra or {}),
        },
        "caller": {
            "principal": caller.principal,
            "provenance": getattr(caller.provenance, "value", caller.provenance),
            "identity_status": getattr(caller.identity_status, "value", caller.identity_status),
            "credential_kind": caller.credential_kind,
        },
        "retry": bool(retry),
    }


def decode_intent(payload: dict) -> tuple[Intent, Caller, bool]:
    i = payload["intent"]
    t = i["identity"]["target"]
    identity = IntentIdentity(
        project=i["identity"]["project"],
        work_class=WorkClass(i["identity"]["work_class"]),
        target=Target(repo=t["repo"], base_ref=t["base_ref"],
                      scope_anchors=tuple(t.get("scope_anchors") or ())),
        capability_id=i["identity"].get("capability_id"),
        authority_decision_ids=tuple(i["identity"].get("authority_decision_ids") or ()),
    )
    intent = Intent(
        identity=identity, task_id=i["task_id"], task_type=i.get("task_type") or "",
        description=i.get("description") or "", coordinator_id=i.get("coordinator_id"),
        shadow=bool(i.get("shadow")), idempotency_key=i.get("idempotency_key"),
        parent_receipt_id=i.get("parent_receipt_id"), extra=dict(i.get("extra") or {}))
    c = payload["caller"]
    caller = Caller(principal=c["principal"],
                    provenance=CallerProvenance(c["provenance"]),
                    identity_status=IdentityStatus(c.get("identity_status") or "UNVERIFIED"),
                    credential_kind=c.get("credential_kind"))
    return intent, caller, bool(payload.get("retry"))


# ── client ──────────────────────────────────────────────────────────────────

class UnixSocketEngineClient:
    """Drop-in for :class:`~agent_crew.cea.engine.AuthorizationEngine` over a socket.

    ``conn`` is accepted and ignored: when the engine is out of process it owns
    its own receipt store. That is the one real behavioural difference between
    the two deployments, and it is why an adapter must treat the receipt it gets
    back as the record — not assume it can read it from the local DB.
    """

    def __init__(self, config: EngineConfig):
        self.config = config
        self.endpoint = config.endpoint

    def authorize(self, conn, intent: Intent, caller: Caller, *,
                  retry: bool = False) -> Authorization:
        reply = self._call({"op": "authorize", **encode_intent(intent, caller, retry=retry)})
        if "error" in reply:
            raise EngineError(str(reply["error"]))
        return Authorization(receipt=reply["receipt"], http_status=int(reply["http_status"]),
                             code=str(reply["code"]), reused=bool(reply.get("reused")),
                             existing_receipt_id=reply.get("existing_receipt_id"))

    def _call(self, request: dict) -> dict:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.endpoint)
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            chunks, total = [], 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_FRAME:
                    raise EngineError("engine reply exceeded the frame limit")
                chunks.append(chunk)
        except OSError as exc:
            # P7: an unreachable engine is an unavailable input, and the adapter
            # must fail closed. It is not this client's job to invent a verdict.
            raise EngineError(f"engine endpoint {self.endpoint!r} unreachable: {exc}") from exc
        finally:
            sock.close()
        return json.loads(b"".join(chunks).decode("utf-8") or "{}")


# ── server ──────────────────────────────────────────────────────────────────

class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.read(_MAX_FRAME)
        try:
            request = json.loads(raw.decode("utf-8"))
            intent, caller, retry = decode_intent(request)
            conn = self.server.connect()          # type: ignore[attr-defined]
            try:
                auth = self.server.engine.authorize(conn, intent, caller, retry=retry)  # type: ignore[attr-defined]
                conn.commit()
            finally:
                conn.close()
            reply = {"receipt": auth.receipt, "http_status": auth.http_status,
                     "code": auth.code, "reused": auth.reused,
                     "existing_receipt_id": auth.existing_receipt_id}
        except Exception as exc:                  # noqa: BLE001 — the wire needs a reply, not a traceback
            reply = {"error": f"{type(exc).__name__}: {exc}"}
        self.wfile.write(json.dumps(reply).encode("utf-8"))


class EngineService(socketserver.ThreadingUnixStreamServer):
    """Serve one :class:`AuthorizationEngine` on a unix socket. Used by ``crew-authz``."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, engine: AuthorizationEngine, connect):
        if os.path.exists(path):
            os.unlink(path)
        super().__init__(path, _Handler)
        os.chmod(path, 0o600)
        self.engine = engine
        self.connect = connect
        self.path = path

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread

    def shutdown_and_close(self) -> None:
        self.shutdown()
        self.server_close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


__all__ = ["EngineService", "UnixSocketEngineClient", "decode_intent", "encode_intent"]
