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

"Only place" is now enforced rather than asserted (codex P1 #4, re-review of
``cb01d49``). :class:`Caller` is sealed — ``Caller(...)`` raises — and the sole
producer is :func:`~agent_crew.cea.intent._mint_caller`, which this module
holds. The engine's J9 test is
:func:`~agent_crew.cea.intent.is_authenticated_caller`, i.e. *was this object
minted*, not *what does its ``credential_kind`` field say*. The previous test
read a string the caller chose, so an in-process
``Caller(..., credential_kind="broker_registered", identity_status=VERIFIED)``
was ALLOWed for an OPS intent although nothing here issues that kind.

Under the shared uid a ``0600`` token file is **tamper-evident, not
authentication** (P2a): any process running as this uid can read it. That is
exactly why ``_mint_caller`` stamps ``identity_status=UNVERIFIED``
unconditionally, and why :data:`CREDENTIAL_KIND_BROKER` has no producer —
``VERIFIED`` is reserved for the O21b broker's spawn/registration path and is
not something this file can honestly hand out.
"""
from __future__ import annotations

import hmac
import json
import os
import stat
from dataclasses import dataclass
from typing import Optional

from agent_crew.cea.intent import Caller, CallerProvenance, _mint_caller

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
    # argument at all: :func:`_mint_caller` derives it, and under the shared uid
    # it is UNVERIFIED for every kind until the O21b broker exists (P2a).
    return _mint_caller(found.principal, found.provenance, CREDENTIAL_KIND_ADAPTER_TOKEN)


def authenticator_from_env(env: Optional[dict] = None):
    """``AGENT_CREW_CEA_CALLER_TOKENS`` → a file authenticator, else deny-all."""
    e = os.environ if env is None else env
    path = (e.get("AGENT_CREW_CEA_CALLER_TOKENS") or "").strip()
    return TokenFileAuthenticator(path) if path else DenyAllAuthenticator()


__all__ = ["AdapterIdentity", "AuthenticationError", "CREDENTIAL_KIND_ADAPTER_TOKEN",
           "CREDENTIAL_KIND_BROKER", "DenyAllAuthenticator", "StaticTokenAuthenticator",
           "TokenFileAuthenticator", "authenticator_from_env", "parse_token_table"]
