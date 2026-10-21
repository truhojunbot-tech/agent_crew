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
| J5 ownership / reuse | read from intent scope and verified snapshot decisions only; `caller.principal` is not an input. A cross-project match requires a signed, unexpired decision scoped to `reuse:<capability_id>` and the intent project (and matching `intent_hash` when supplied). Otherwise it is `REUSE` → `HUMAN_GATE` / `OWNER_CONFLICT`. |

When `OWNER_CONFLICT` is enforced, the task stays in `needs_human` with a HELD
receipt; HTTP enqueue returns 423 with its task and receipt IDs. The optional
`AGENT_CREW_CEA_OWNER_REQUEST_CMD` receives one JSON request per intent hash on
stdin. Without it, the request is logged and counted. `/health` reports parked
tasks and owner request counts. A later verified snapshot generation triggers
one re-admission attempt; an approved intent runs as a new lineage task, keeping
the original task and receipt in the audit trail.
| J7 review/test contract, J8 human gate | identity-dependent → `REVIEW` with the named reviewer, or `HUMAN_GATE` |

A forged, minted or registry-injected caller therefore obtains **exactly** what
an honest unauthenticated `UNVERIFIED` caller obtains. The validator refuses the
same set (`validator._identity_dependent`), sharing the predicate with the
engine rather than duplicating it.

**2. The boundary is the process plus the credential, not the class.**

| mode | operational? | may `authorize()` run embedded? | does a non-PROCEED answer stop work? |
|------|--------------|-------------------------------|--------------------------------------|
| `off` | yes — the default and the fallback | yes — it computes nothing and stops nothing | no (no verdict is computed) |
| `enforce` | yes | **no** | yes |
| `test` | **no** — pytest harness only | yes — inside the harness | yes, inside the harness |

**There is no operational shadow mode** (owner directive 2026-10-21, PR #818).
The historical value `AGENT_CREW_CEA_MODE=shadow` — compute and record a verdict,
never stop on it — is still spelled by old environments and resolves to `off`,
as does an unset variable or an unrecognised value. Nothing resolves to
something *stricter* than what was written: the fallback is `off`, never
`enforce`, because the engine has known historical false-block classes and a
typo must not refuse real work. `EngineConfig(mode="shadow")` constructed
directly is also `off`. Consequence: every mode that computes a verdict also
enforces it; there is no longer any mode that records without enforcing.

`test` is honoured only when `agent_crew.cea.engine.test_harness_active()` holds
for the interpreter resolving it: the test runner's own `pytest_configure` hook in
`tests/conftest.py` has registered the running session's `Config` with the engine
(`register_test_harness`), and that session is still open. The proof is state only
the runner's session hook sets — never the configuration mapping, never an
environment variable, and **not** `"pytest" in sys.modules`: a standalone process
that imports pytest and exports `AGENT_CREW_CEA_MODE=test` is not a harness
(review of PR #818, HIGH), nor is a child process that merely inherits
`PYTEST_CURRENT_TEST`. Anywhere else, `test` resolves to `off` with a warning.
`tests/unit/test_no_shadow_runtime.py` spawns fresh interpreters — with and
without `import pytest` — and pins this.

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

⛔`AGENT_CREW_CEA_MODE=test` in a real deployment used to get enforcement
*without* a credential boundary. Since the review of PR #818 it gets nothing:
outside a verifiable pytest harness `test` is `off` (see the mode table above),
so the only way to enforce in a live process is `enforce`, across the socket.

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
