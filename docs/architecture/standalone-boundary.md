# Standalone package boundary (#310)

Agent Crew's portable core owns the CLI, task queue, dispatch and result lifecycle,
project discovery, and local context and memory capture. A wheel must start
`crew --help` with an empty `HOME` and no `AGENT_CREW_*` environment. The
[clean-install smoke test](../../tests/unit/test_issue_310_clean_install_smoke.py)
checks that entry point; it does not exercise a live dispatcher or admission.

Project discovery accepts `AGENT_CREW_PROJECT_ROOTS` as an `os.pathsep`-separated
list of roots. When unset, it retains the legacy home-root search. Memory
capture accepts `AGENT_CREW_MEMORY_PROJECTS_JSON`, an object with `projects`,
`aliases`, and `repo_disambiguation`; unset or invalid configuration retains
the existing catalog. These defaults preserve existing project behavior.

Optional adapters remain in the package but must not become prerequisites for
the standalone CLI:

| Adapter | Existing configuration |
| --- | --- |
| Context Pack / Forge | `AGENT_CREW_CONTEXT_PACK` and `AGENT_CREW_FORGE_PROVIDER` opt in; `AGENT_CREW_FORGE_URL` selects the Forge endpoint. Forge is off by default. |
| CEA policy inputs | `AGENT_CREW_CEA_REGISTRY_PATH`, `AGENT_CREW_CEA_SNAPSHOT_PATH`, `AGENT_CREW_CEA_MEMORY_CMD`, and `AGENT_CREW_CEA_QUOTA_CACHE_DIR` select external registry, snapshot, provider command, and quota cache. |
| CEA service and broker | `AGENT_CREW_CEA_ENGINE_ENDPOINT` and `AGENT_CREW_CEA_ADAPTER_TOKEN_FILE` select the external engine and credential; `AGENT_CREW_CEA_SNAPSHOT_HWM_FILE`, `AGENT_CREW_CEA_RECEIPT_SIGNING_KEY_FILE`, and `AGENT_CREW_AUTHZ_SRC_COMMIT_PATH` configure private state and signing paths. |

The CEA, broker, signing, and `/opt` defaults are existing optional-adapter
boundary debt. This document does not change their behavior. The
[#531 portable-core guard](../../tests/unit/test_issue_310_portable_core_boundary.py)
rejects new private-package imports and private home paths under
`src/agent_crew` while listing the existing exceptions. New council follow-ups
must identify their target as **PORTABLE_CORE** or **OPTIONAL_ADAPTER**.
