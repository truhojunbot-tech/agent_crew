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

def test_task_scope_inherits_project_and_fleet_memory(tmp_path):
    store=SQLiteMemoryStorage(str(tmp_path/'m.sqlite'))
    store.put(MemoryRecord('procedural','fleet-rule',{},MemoryScope(fleet='f')))
    store.put(MemoryRecord('procedural','project-rule',{},MemoryScope(fleet='f',project='p')))
    task=MemoryScope(fleet='f',project='p',worktree='w',task_id='t')
    assert [r.key for r in store.retrieve(task)] == ['project-rule','fleet-rule']

def test_reconstruction_keeps_authoritative_and_checkpoint(monkeypatch,tmp_path):
    monkeypatch.setenv('AGENT_CREW_ADR001_MEMORY_ENABLED','1'); store=SQLiteMemoryStorage(str(tmp_path/'m.sqlite')); s=MemoryScope(project='p',task_id='t')
    store.put(MemoryRecord('authoritative','git-ref',{},s)); store.put(MemoryRecord('checkpoint','state',{},s))
    assert {r['layer'] for r in reconstruct_context(store,'reviewer','t',s)['records']} >= {'authoritative','checkpoint'}

def test_newer_memory_version_cannot_regress(tmp_path):
    store=SQLiteMemoryStorage(str(tmp_path/'m.sqlite')); s=MemoryScope(project='p')
    store.put(MemoryRecord('authoritative','spec',{'v':2},s,version=2)); store.put(MemoryRecord('authoritative','spec',{'v':1},s,version=1))
    assert store.retrieve(s,exact_key='spec')[0].value == {'v':2}

def test_sibling_task_memory_does_not_leak_and_null_ancestor_inherits(tmp_path):
    store=SQLiteMemoryStorage(str(tmp_path/'m.sqlite')); project=MemoryScope(fleet='f',project='p')
    store.put(MemoryRecord('procedural','project',{},project))
    store.put(MemoryRecord('episodic','only-a',{},MemoryScope(fleet='f',project='p',task_id='a')))
    got=store.retrieve(MemoryScope(fleet='f',project='p',task_id='b'))
    assert [r.key for r in got] == ['project']

def test_equal_version_never_overwrites_content(tmp_path):
    store=SQLiteMemoryStorage(str(tmp_path/'m.sqlite')); s=MemoryScope(project='p')
    store.put(MemoryRecord('authoritative','spec',{'v':'first'},s,version=2)); store.put(MemoryRecord('authoritative','spec',{'v':'second'},s,version=2))
    assert store.retrieve(s,exact_key='spec')[0].value == {'v':'first'}
