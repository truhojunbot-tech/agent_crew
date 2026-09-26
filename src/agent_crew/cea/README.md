# CEA — what this engine can and cannot prove about a caller

Contract: alfred `sev0/e11-adr-draft` `evidence/sev0-p0/E11-ADR-DRAFT.md`
@ `6cbce565` (Π P1–P7, §3, §5, §6, §7, §11). Receipt schema: the byte-identical
copy at `tests/cea_contract/receipt.schema.json`.

## Same-uid peers are untrusted, and identity is UNVERIFIED by contract

Every agent_crew process runs as one OS user. Under one uid there is no
credential boundary between peers: a `0600` token file is readable by anything
running as that user, and a process can read another's memory, so a secret is
**tamper-evident, not secret** (P2a).

So `caller_identity_status` and `executor_binding_status` are **unconditionally
`UNVERIFIED`**, with `downgrade_reason: SHARED_UID_NO_CREDENTIAL_BOUNDARY`, on
every receipt this build issues. There is no input anywhere — no credential
kind, no provenance, no config, no signing key — that produces `VERIFIED`.
`VERIFIED` is reserved for the O21b broker's spawn/registration path, which does
not exist yet; `CREDENTIAL_KIND_BROKER` is a reserved name with no producer.

Receipt signing is independent of all of this. The engine key proves the
*engine* wrote the receipt. It says nothing about who asked.

## The in-process `Caller` is not an authentication boundary

Codex established this against two earlier attempts (reviews of `cb01d49` and
`f1aee1d`), and this repo now states it rather than arguing with it. Both of
these still work and are reproduced as tests in
`tests/unit/test_sev0_cea_s2a_fix_r3.py`:

```python
from agent_crew.cea._caller_mint import mint_caller
forged = mint_caller("attacker", CallerProvenance.DIRECT, "adapter_token")
```

```python
obj = object.__new__(Caller)                    # past the raising constructor
object.__setattr__(obj, "principal", "attacker")   # ...and the rest
registry = next(c.cell_contents for c in is_authenticated_caller.__closure__
                if isinstance(c.cell_contents, weakref.WeakSet))
registry.add(obj)                                # now `is_authenticated_caller(obj)` is True
```

In-process Python cannot keep a secret from in-process Python. The constructor
guard, the weak mint registry and the `_caller_mint` module name are **hygiene**:
they catch an adapter that builds a `Caller` by hand instead of authenticating,
which is the mistake that actually happens. None of them is a security control
and none is described as one.

## What does contain it

**1. Identity confers nothing while it is unverified.** The decision function is
principal-invariant whenever `caller_identity_status` is `UNVERIFIED`. Any
judgement that would differ by principal or credential kind is refused rather
than answered:

| judgement | while UNVERIFIED |
|-----------|------------------|
| J9 who may do OPS (`IDENTITY_DEPENDENT_WORK_CLASSES`) | `HUMAN_GATE` / `IDENTITY_UNVERIFIED_WHO_MAY_ACT` — never `ALLOW`, and not `REVIEW` either: a reviewer checks the work, not the entitlement |
| J5 ownership / reuse | read from intent scope only; `caller.principal` is not an input. An unowned-by-this-project match is `REUSE` → `HUMAN_GATE` / `OWNER_CONFLICT` for everyone |
| J7 review/test contract, J8 human gate | identity-dependent → `REVIEW` with the named reviewer, or `HUMAN_GATE` |

A forged, minted or registry-injected caller therefore obtains **exactly** what
an honest unauthenticated `UNVERIFIED` caller obtains. The validator refuses the
same set (`validator._identity_dependent`), sharing the predicate with the
engine rather than duplicating it.

**2. The boundary is the process plus the credential, not the class.**

| mode | may `authorize()` run embedded? | does a non-PROCEED answer stop work? |
|------|-------------------------------|--------------------------------------|
| `shadow` | yes — it stops nothing, it measures | no |
| `test` | yes — a harness, and it says so in its name | yes |
| `enforce` | **no** | yes |

For staged enforcement, set `AGENT_CREW_CEA_ENFORCE_CODES_<PROJECT>` to a
comma-separated set of reason codes, for example
`AGENT_CREW_CEA_ENFORCE_CODES_ALFRED=RUNTIME_STATE_FORBIDS`. Project names use
the same uppercase, non-alphanumeric-to-underscore normalization as
`AGENT_CREW_CEA_MODE__<PROJECT>`. The process-wide fallback is
`AGENT_CREW_CEA_ENFORCE_CODES`. A project value takes precedence when present.
In `enforce` or `test`, only listed codes stop work; other non-PROCEED answers
remain recorded as advisory and require review. Without either variable, all
codes enforce as before. Unknown codes are ignored with a warning. This setting
changes only whether a verdict stops work; the receipt keeps its actual decision
and reason code. It does not change the separate operator STOP gate.

In `enforce`, embedded in-process `authorize()` returns a P7 fail-closed receipt
(`BLOCK` / `CREDENTIAL_BOUNDARY_UNAVAILABLE`, with `caller_credential_boundary`
in `provenance.unavailable_inputs`) and the engine must be reached over the unix
socket in `service.py`, where the credential is validated by a peer that is not
the caller — `AGENT_CREW_CEA_ENGINE_ENDPOINT` plus
`AGENT_CREW_CEA_ADAPTER_TOKEN_FILE`.

⛔`AGENT_CREW_CEA_MODE=test` in a real deployment gets enforcement *without* a
credential boundary. It is offered as a named, explicit choice rather than a
silent default; that is the only honest way to offer it at all.

A process boundary with a validated credential is still not a *verified
identity* under one uid — the receipts keep saying `UNVERIFIED` across the socket
too. It is a boundary a misconfigured neighbour trips over, and it is the seam
O21b replaces.

**2a. How `enforce` refuses, and exactly what that is not.**

The refusal above used to be conditional: the engine carried a public
`attach_credential_boundary(name)` method writing a `_credential_boundary`
string, and `enforce` authorized whenever that string was non-`None`. Codex
(`review-sev0-cea-lineage-s2a-fix-r3-x`, P1) reproduced the obvious consequence
— `eng.attach_credential_boundary('/not-a-socket')`, then a forged
`mint_caller('attacker', DIRECT, 'x')`, then a REVIEW intent: **ALLOW / OK**,
`caller_identity=attacker`, with no socket in existence and no credential ever
checked. A mutable declaration is not proof of process topology.

There is now no such attribute. Instead there are two methods:

| method | who calls it | `enforce` behaviour |
|--------|--------------|---------------------|
| `authorize()` — public | adapters, anything in-process | **unconditionally** `BLOCK` / `CREDENTIAL_BOUNDARY_UNAVAILABLE` |
| `_authorize_authenticated()` — boundary-internal | `service.EngineService._Handler`, only after `authenticate()` matched the presented credential with `hmac.compare_digest` | decides |

⛔This is **not** an in-process access control, and is not claimed as one.
  Python has no way to stop code in the same interpreter from writing
  `eng._authorize_authenticated(...)`, exactly as it can import
  `_caller_mint.mint_caller`. That residue is the P2a same-uid limitation (§P2a,
  O21b) and nothing here repairs it. What changed is narrower and real: there is
  no longer a *public, supported* enforce path, and no declaration a caller can
  make that turns one on. The regressions are
  `tests/unit/test_sev0_cea_s2a_fix_r3.py::test_no_public_declaration_can_turn_enforce_authorization_on`
  and `::test_the_service_is_the_credential_boundary_enforce_requires`, the
  latter asserted end-to-end through `UnixSocketEngineClient` rather than by
  calling the engine in-process after a service object happened to exist — which
  is what the superseded test did, and is how it encoded the bypass.
