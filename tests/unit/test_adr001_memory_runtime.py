from agent_crew.memory_runtime import MemoryRecord, MemoryScope, SQLiteMemoryStorage, reconstruct_context

def test_four_layers_round_trip_and_scope_isolation(tmp_path):
    store=SQLiteMemoryStorage(str(tmp_path/'memory.sqlite')); scope=MemoryScope(project='p', task_id='t')
    for layer in ('authoritative','checkpoint','procedural','episodic'): store.put(MemoryRecord(layer, layer, {'ref': layer}, scope))
    assert {r.layer for r in store.retrieve(scope)} == {'authoritative','checkpoint','procedural','episodic'}
    assert store.retrieve(MemoryScope(project='other')) == []

def test_exact_and_hybrid_retrieval(tmp_path):
    store=SQLiteMemoryStorage(str(tmp_path/'memory.sqlite')); scope=MemoryScope(project='p')
    store.put(MemoryRecord('procedural','how-to-review',{'text':'review safety'},scope))
    assert store.retrieve(scope, exact_key='how-to-review')[0].key == 'how-to-review'
    assert store.retrieve(scope, query='safety')[0].layer == 'procedural'

def test_flag_off_reconstruction_is_empty(monkeypatch,tmp_path):
    monkeypatch.delenv('AGENT_CREW_ADR001_MEMORY_ENABLED', raising=False)
    assert reconstruct_context(SQLiteMemoryStorage(str(tmp_path/'m.sqlite')), 'reviewer','t',MemoryScope()) == {'enabled':False,'records':[]}
