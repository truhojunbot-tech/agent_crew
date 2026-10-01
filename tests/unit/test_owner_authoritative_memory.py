import hashlib

import pytest

from agent_crew.memory_runtime import (
    MemoryScope, SQLiteMemoryStorage, capture_owner_statement,
    effective_owner_statements, owner_statement_history, owner_statement_key,
)


def proof(mid="7", chat="42", text="Monday"):
    return {"status": "VERIFIED", "message_id": mid, "user_id": chat,
            "chat_id": chat, "ts": "2026-09-26T00:00:00Z",
            "text_sha256": hashlib.sha256(text.encode()).hexdigest()}


def test_correction_is_effective_but_original_remains_immutable_and_auditable(tmp_path):
    store = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    first = capture_owner_statement(store, project="alfred", target_project="alfred",
                                    proof=proof("7", text="Friday"), text="Friday",
                                    source_ref="session.jsonl")
    second = capture_owner_statement(store, project="alfred", target_project="alfred",
                                     proof=proof("8", text="Monday"), text="Monday",
                                     source_ref="session.jsonl", supersedes=[first.key])
    assert [r.key for r in effective_owner_statements(store, "alfred")] == [second.key]
    assert [r.key for r in owner_statement_history(store, "alfred", first.key)] == [first.key, second.key]
    # A repeated capture is idempotent; a higher version cannot rewrite source truth.
    capture_owner_statement(store, project="alfred", target_project="alfred",
                            proof=proof("7", text="Friday"), text="Friday",
                            source_ref="session.jsonl")
    with pytest.raises(ValueError):
        store.put(type(first)(first.layer, first.key, {"text": "changed"}, first.scope, version=2))


@pytest.mark.parametrize("change", [
    {"project": "halla", "target_project": "alfred"},
    {"project": "alfred", "target_project": "halla"},
    {"proof": {**proof(), "status": "TEXT_MISMATCH"}},
    {"proof": {**proof(), "chat_id": "99"}},
    {"proof": {**proof(), "text_sha256": "0" * 64}},
])
def test_wrong_bot_chat_or_verification_never_writes(tmp_path, change):
    store = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    args = dict(project="alfred", target_project="alfred", proof=proof(),
                text="Monday", source_ref="session.jsonl")
    args.update(change)
    with pytest.raises(ValueError):
        capture_owner_statement(store, **args)
    assert store.retrieve(MemoryScope(project="alfred")) == []


def test_no_cross_bot_or_chat_collision_and_invalid_supersedes_fails_closed(tmp_path):
    store = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    a = capture_owner_statement(store, project="alfred", target_project="alfred",
                                proof=proof(), text="Monday", source_ref="a")
    h = capture_owner_statement(store, project="halla", target_project="halla",
                                proof=proof(), text="Monday", source_ref="h")
    other_chat = capture_owner_statement(store, project="alfred", target_project="alfred",
                                         proof=proof(chat="99"), text="Monday", source_ref="a")
    assert a.key != h.key
    assert a.key != other_chat.key
    assert [r.key for r in effective_owner_statements(store, "halla")] == [h.key]
    with pytest.raises(ValueError):
        capture_owner_statement(store, project="alfred", target_project="alfred",
                                proof=proof("9", text="Tuesday"), text="Tuesday",
                                source_ref="a", supersedes=[h.key])
    assert owner_statement_key("alfred", "telegram", "42", "7") == a.key


def test_malformed_correction_link_never_yields_effective_owner_text(tmp_path):
    from agent_crew.memory_runtime import MemoryRecord
    store = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    store.put(MemoryRecord("authoritative", "owner:alfred:telegram:42:7",
                           {"kind": "owner_statement", "text": "stale",
                            "supersedes": "bad-link"}, MemoryScope(project="alfred")))
    with pytest.raises(ValueError):
        effective_owner_statements(store, "alfred")


def test_broad_or_wrong_project_owner_record_never_leaks(tmp_path):
    from agent_crew.memory_runtime import MemoryRecord
    store = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    store.put(MemoryRecord("authoritative", "owner:alfred:telegram:42:7",
                           {"kind": "owner_statement", "text": "private"},
                           MemoryScope()))
    assert effective_owner_statements(store, "halla") == []
    assert effective_owner_statements(store, "alfred") == []
