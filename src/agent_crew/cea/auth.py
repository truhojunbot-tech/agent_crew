"""J9 — the caller-authentication boundary (ADR Π P2a, P5, §6.5, §7.1, CXC-4).

⛔**This module is the only place a :class:`~agent_crew.cea.intent.Caller` is
  constructed from anything a peer sent.** Everything else — the unix socket
  decoder, an HTTP adapter, a test helper standing in for one — hands a *bare
  credential* to an authenticator and takes what it gets back, or takes ``None``
  and answers 401.

The defect this closes (codex review of 2a @5831d39, P1 #1): the socket decoder
built ``Caller(principal=…, provenance=…, credential_kind=…, identity_status=…)``
straight out of the request JSON, and :meth:`AuthorizationEngine.authorize` only
rejected ``caller is None``. So ``{"principal": "attacker", "provenance":
"direct", "credential_kind": null}`` was a principal, and an OPS intent under it
was ALLOWed. "The engine authenticates" was never true: nothing had ever checked
a credential, because nothing was ever presented.

"Only place" is a convention this module keeps, **not** a property it enforces
(codex, re-review of ``f1aee1d``). ``Caller(...)`` raises and the intended
producer is :func:`~agent_crew.cea._caller_mint.mint_caller`, which lives off
the public path; but that module is importable and its registry is reachable
through ``is_authenticated_caller.__closure__``, so in-process code can still
manufacture a Caller the engine accepts. Saying otherwise would be the same
mistake in a new place. The engine's J9 test —
:func:`~agent_crew.cea._caller_mint.is_authenticated_caller`, *was this object
produced by an authenticator* — is therefore hygiene: it catches an adapter that
built a Caller by hand, and it is strictly better than the two tests it replaced
(``caller is None``, then ``credential_kind in (...)``, both of which read a
string the caller chose), but it is not a boundary.

The boundary is two other things. **Deployment**: in
:data:`~agent_crew.cea.engine.ENFORCE` the engine refuses embedded in-process
authorization and must be reached over the unix socket, where the credential
below is checked by a peer that is not the caller. **Decision shape**: while
``caller_identity_status`` is UNVERIFIED, no judgement differs by principal
(:data:`~agent_crew.cea.intent.IDENTITY_DEPENDENT_WORK_CLASSES`), so a forged
caller obtains exactly what an honest unauthenticated one obtains.

Under the shared uid a ``0600`` token file is **tamper-evident, not
authentication** (P2a): any process running as this uid can read it. That is
exactly why ``mint_caller`` stamps ``identity_status=UNVERIFIED``
unconditionally, and why :data:`CREDENTIAL_KIND_BROKER` has no producer —
``VERIFIED`` is reserved for the O21b broker's spawn/registration path and is
not something this file can honestly hand out.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import stat
from dataclasses import dataclass
from typing import Optional

from agent_crew.cea._caller_mint import mint_caller
from agent_crew.cea.intent import Caller, CallerProvenance

CREDENTIAL_KIND_ADAPTER_TOKEN = "adapter_token"
"""A per-adapter secret from a ``0600`` file. Tamper-evident under one uid (P2a)."""

CREDENTIAL_KIND_BROKER = "broker_registered"
"""O21b. The only kind that may ever carry ``VERIFIED`` — and nothing issues it yet."""


class AuthenticationError(Exception):
    """The authenticator could not be consulted at all (never "denied")."""


@dataclass(frozen=True)
class AdapterIdentity:
    """What a registered token *is*: a principal and the ingress it speaks for.

    Both come from the token file on the engine's side of the boundary, never
    from the request. That is the whole point — ``caller_provenance`` is an
    audit field (§3), and an audit field a caller may set is decoration.
    """
    principal: str
    provenance: CallerProvenance


class DenyAllAuthenticator:
    """The default: nothing is registered, so nobody authenticates.

    P7's shape applied to J9 — an unconfigured credential boundary refuses,
    it does not wave callers through.
    """

    def authenticate(self, credential: Optional[str]) -> Optional[Caller]:
        return None


class StaticTokenAuthenticator:
    """An in-memory token table. Used by tests and by an embedded adapter."""

    def __init__(self, tokens: dict[str, AdapterIdentity]):
        self._tokens = dict(tokens)

    def authenticate(self, credential: Optional[str]) -> Optional[Caller]:
        return _match(self._tokens, credential)


class TokenFileAuthenticator:
    """Map a presented adapter token to a Caller, from a ``0600``/``0400`` file.

    File format — a JSON object whose ``adapters`` map is ``token -> {principal,
    provenance}``::

        {"adapters": {"<secret>": {"principal": "cron:admitted_trigger",
                                   "provenance": "cron"}}}

    A file readable by group or other is refused rather than used: it is the one
    property that makes the token tamper-*evident*, and a silently-widened mode
    would turn an audited boundary into a decorative one.
    """

    def __init__(self, path: str, *, require_private_mode: bool = True):
        self.path = path
        self.require_private_mode = require_private_mode

    def authenticate(self, credential: Optional[str]) -> Optional[Caller]:
        return _match(self._load(), credential)

    def _load(self) -> dict[str, AdapterIdentity]:
        try:
            st = os.stat(self.path)
        except OSError as exc:
            raise AuthenticationError(
                f"caller token file {self.path!r} is unreadable: {exc}") from exc
        if self.require_private_mode and stat.S_IMODE(st.st_mode) & 0o077:
            raise AuthenticationError(
                f"caller token file {self.path!r} is mode "
                f"{stat.S_IMODE(st.st_mode):04o}; refusing to treat a group/world-readable "
                f"file as a credential store (P2a)")
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                body = json.load(fh)
        except (OSError, ValueError) as exc:
            raise AuthenticationError(
                f"caller token file {self.path!r} is not readable JSON: {exc}") from exc
        return parse_token_table(body)


def parse_token_table(body: dict) -> dict[str, AdapterIdentity]:
    """``{"adapters": {token: {principal, provenance}}}`` → the table, validated."""
    adapters = (body or {}).get("adapters") or {}
    if not isinstance(adapters, dict):
        raise AuthenticationError("caller token table: 'adapters' must be an object")
    table: dict[str, AdapterIdentity] = {}
    for token, entry in adapters.items():
        if not isinstance(token, str) or not token or not isinstance(entry, dict):
            raise AuthenticationError("caller token table: each entry is token -> object")
        principal = entry.get("principal")
        provenance = entry.get("provenance")
        if not isinstance(principal, str) or not principal:
            raise AuthenticationError("caller token table: 'principal' is required")
        try:
            prov = CallerProvenance(provenance)
        except ValueError as exc:
            raise AuthenticationError(
                f"caller token table: {provenance!r} is not a CallerProvenance") from exc
        table[token] = AdapterIdentity(principal=principal, provenance=prov)
    return table


def _match(table: dict[str, AdapterIdentity], credential: Optional[str]) -> Optional[Caller]:
    """Constant-time lookup. The whole table is walked whether or not it matched:
    an early return would leak which prefix was right through timing."""
    if not isinstance(credential, str) or not credential:
        return None
    presented = credential.encode("utf-8")
    found: Optional[AdapterIdentity] = None
    for token, identity in table.items():
        if hmac.compare_digest(token.encode("utf-8"), presented):
            found = identity
    if found is None:
        return None
    # This is the mint. It runs on exactly one condition — a presented secret
    # matched a table entry — and the principal and provenance it stamps come
    # from that entry, never from the request. ``identity_status`` is not an
    # argument at all: :func:`mint_caller` derives it, and under the shared uid
    # it is UNVERIFIED for every kind until the O21b broker exists (P2a).
    return mint_caller(found.principal, found.provenance, CREDENTIAL_KIND_ADAPTER_TOKEN)


def authenticator_from_env(env: Optional[dict] = None):
    """``AGENT_CREW_CEA_CALLER_TOKENS`` → a file authenticator, else deny-all."""
    e = os.environ if env is None else env
    path = (e.get("AGENT_CREW_CEA_CALLER_TOKENS") or "").strip()
    return TokenFileAuthenticator(path) if path else DenyAllAuthenticator()


# ── the in-process adapter (step 2c, temporary) ─────────────────────────────

def _seal_in_process():
    """Mint one loopback credential per ingress provenance, at import.

    ⛔This is **not** a credential boundary and nothing here pretends it is.
      The token is generated in this process and presented to an engine running
      in this same process: it proves that the code holding it is this process,
      which is a tautology, not authentication. It exists for one reason — J9
      refuses a Caller nobody minted, and the step-2c adapters (``queue.enqueue``
      and the legacy ``POST /tasks`` behind it) are in-process callers that have
      no token file yet. Minting here keeps ``authorize`` on its single
      authenticated entry path instead of growing a second, unauthenticated one.

    Two properties make it honest rather than a hole:

    * it grants nothing. Anything that can call :func:`in_process_caller`
      already has the engine object and could call ``authorize`` directly;
    * it does not cross the boundary. With ``AGENT_CREW_CEA_ENGINE_ENDPOINT``
      set, the engine is another process whose token table does not contain
      this secret, so the loopback caller gets 401 and fails closed — which is
      the correct answer, because out of process the claim really is unproven.

    The receipt says so either way: ``caller_identity_status`` is UNVERIFIED
    with ``downgrade_reason SHARED_UID_NO_CREDENTIAL_BOUNDARY`` (P2a), because
    :func:`~agent_crew.cea._caller_mint.mint_caller` derives that and takes no
    argument for it. §7 replaces this with per-adapter tokens in step 2b.
    """
    tokens = {secrets.token_hex(32): AdapterIdentity(
        principal=f"agent_crew.in_process:{prov.value}", provenance=prov)
        for prov in CallerProvenance}
    authenticator = StaticTokenAuthenticator(tokens)
    by_provenance = {identity.provenance: token for token, identity in tokens.items()}

    def caller(provenance: CallerProvenance = CallerProvenance.DIRECT) -> Caller:
        """The minted Caller for an in-process ingress of this provenance."""
        if not isinstance(provenance, CallerProvenance):
            raise AuthenticationError(
                f"{provenance!r} is not a CallerProvenance; the ingress names itself from the "
                f"enum, never from a request field (§3)")
        minted = authenticator.authenticate(by_provenance[provenance])
        if minted is None:                      # pragma: no cover — the table is built here
            raise AuthenticationError("the in-process token table did not authenticate its own token")
        return minted

    return authenticator, caller


in_process_authenticator, in_process_caller = _seal_in_process()


__all__ = ["AdapterIdentity", "AuthenticationError", "CREDENTIAL_KIND_ADAPTER_TOKEN",
           "CREDENTIAL_KIND_BROKER", "DenyAllAuthenticator", "StaticTokenAuthenticator",
           "TokenFileAuthenticator", "authenticator_from_env", "in_process_authenticator",
           "in_process_caller", "parse_token_table"]
