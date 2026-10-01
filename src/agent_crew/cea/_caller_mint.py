"""The producer of :class:`~agent_crew.cea.intent.Caller`, off the public path.

⛔**Hygiene, not a boundary. This module is not a seal and nothing here is an
  authentication mechanism.** Python has no private module: anything running in
  this interpreter can ``import agent_crew.cea._caller_mint``, call
  :func:`mint_caller` directly, or reach the registry below through
  ``is_authenticated_caller.__closure__`` and add an object it built with
  ``object.__new__``. Codex demonstrated both against the previous arrangement
  (re-review of ``f1aee1d``) and both still work. The leading underscore says
  "not API", which is all it has ever been able to say.

What actually contains that attack is elsewhere and is not a secret: a forged
caller **gains nothing**. Every judgement that would differ by principal is
refused while ``caller_identity_status`` is UNVERIFIED — which it
unconditionally is until the O21b broker exists (P2a) — so minting or injecting
a principal yields exactly what an honest unauthenticated caller already had.
See :data:`~agent_crew.cea.intent.IDENTITY_DEPENDENT_WORK_CLASSES` and
:meth:`~agent_crew.cea.engine.AuthorizationEngine._decide`.

The second half of the answer is deployment, not code: in
:data:`~agent_crew.cea.engine.ENFORCE` the engine refuses embedded in-process
authorization altogether and must be reached over the unix socket, where a
credential is validated by a peer that is not the caller
(:mod:`agent_crew.cea.service`). A process boundary plus a credential is a
boundary; a module-private name is a naming convention.
"""
from __future__ import annotations

import weakref

from agent_crew.cea.intent import Caller, CallerProvenance, IdentityStatus


def _build():
    """Close the mint over a registry of what it produced.

    The registry is weak: a Caller stops being recognised exactly when the last
    reference to it is dropped, so a long-lived engine does not accumulate every
    principal that ever called it.

    This answers "did an authenticator produce this object?" and nothing more.
    It is a *hygiene* check — it catches an adapter that built a Caller by hand
    instead of authenticating, which is the mistake that actually happens. It is
    not a defence against code running in this process, and the engine does not
    treat a positive answer as evidence of anything about the principal.
    """
    minted: "weakref.WeakSet[Caller]" = weakref.WeakSet()

    def mint(principal: str, provenance: CallerProvenance, credential_kind: str) -> "Caller":
        if not isinstance(principal, str) or not principal:
            raise ValueError("a minted Caller needs a principal")
        if not isinstance(provenance, CallerProvenance):
            raise TypeError("provenance must be a CallerProvenance, not a string from a peer")
        if not isinstance(credential_kind, str) or not credential_kind:
            raise ValueError("a minted Caller names the credential kind that produced it")
        caller = object.__new__(Caller)
        setattr_ = object.__setattr__
        setattr_(caller, "principal", principal)
        setattr_(caller, "provenance", provenance)
        # ⛔P2a: **derived here, never accepted.** One line, and it is the whole
        #   O21b seam: when a broker that independently verifies a spawn exists,
        #   it mints VERIFIED and nothing else in this file changes. Until then
        #   every caller — every credential kind, every provenance — is
        #   UNVERIFIED, so there is no input that produces VERIFIED anywhere.
        setattr_(caller, "identity_status", IdentityStatus.UNVERIFIED)
        setattr_(caller, "credential_kind", credential_kind)
        minted.add(caller)
        return caller

    def is_minted(obj) -> bool:
        return isinstance(obj, Caller) and obj in minted

    return mint, is_minted


mint_caller, is_authenticated_caller = _build()
is_authenticated_caller.__doc__ = """Was this Caller produced by an authenticator?

The engine's J9 test (§7.1), and a hygiene check rather than a security control:
it catches an adapter that hand-built a Caller instead of authenticating, and it
does not pretend to stop code that is already running in this process. Membership
is by *identity*, so a forgery that copies every field of a real principal still
answers ``False`` — and a forgery that gets itself into the registry still gets
the same decision, because the decision does not turn on the principal while
identity is UNVERIFIED (P2a)."""


__all__ = ["is_authenticated_caller", "mint_caller"]
