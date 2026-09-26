import hashlib
import json

import pytest
from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.memory_runtime import MemoryRecord, MemoryScope, SQLiteMemoryStorage
from agent_crew.owner_statements import ingest_owner_transcripts, owner_key


def _transcript(path, message_id, text, *, sender='42', chat='42'):
    row = {'type': 'user', 'origin': {'kind': 'channel', 'server': 'plugin:telegram:telegram'},
           'timestamp': '2026-09-26T00:00:00Z', 'message': {'content':
           f'<channel source="plugin:telegram:telegram" user_id="{sender}" chat_id="{chat}" '
           f'message_id="{message_id}" ts="2026-09-26T00:00:00Z">{text}</channel>'}}
    with path.open('a') as stream:
        stream.write(json.dumps(row) + '\n')


def _verify(message_id, *, text_sha256, transcripts_glob, access_path):
    return {'status': 'VERIFIED', 'message_id': str(message_id), 'user_id': '42',
            'chat_id': '42', 'text_sha256': text_sha256}


def test_idempotent_ingest_immutability_and_bot_isolation(tmp_path):
    path = tmp_path / 'bot.jsonl'
    _transcript(path, '10', 'Keep this exactly')
    store = SQLiteMemoryStorage(str(tmp_path / 'mem.db'))
    args = dict(bot='alpha', transcripts_glob=str(path), access_path=str(tmp_path / 'access.json'), verifier=_verify)
    assert ingest_owner_transcripts(store, **args)['inserted'] == 1
    assert ingest_owner_transcripts(store, **args)['existing'] == 1
    key = owner_key('alpha', '42', '10')
    original = store.audit(MemoryScope(project='alpha'), key)[0]
    assert original.value['text'] == 'Keep this exactly'
    assert original.value['text_sha256'] == hashlib.sha256(b'Keep this exactly').hexdigest()
    assert store.retrieve(MemoryScope(project='beta')) == []
    assert store.audit(MemoryScope(project='beta'), key) == []
    with pytest.raises(ValueError, match='immutable'):
        store.put(MemoryRecord('authoritative', key, {**original.value, 'timestamp': 'changed'},
                               original.scope))
    with pytest.raises(ValueError, match='invalid'):
        store.put(MemoryRecord('authoritative', key, original.value, original.scope, version=2))
    assert store.audit(MemoryScope(project='alpha'), key)[0] == original
    earlier = tmp_path / 'a-bot.jsonl'
    _transcript(earlier, '10', 'Keep this exactly')
    args['transcripts_glob'] = str(tmp_path / '*.jsonl')
    assert ingest_owner_transcripts(store, **args)['existing'] == 1
    assert store.audit(MemoryScope(project='alpha'), key)[0] == original


def test_correction_is_distinct_and_effective_read_excludes_original(tmp_path):
    path = tmp_path / 'bot.jsonl'
    _transcript(path, '10', 'Use red')
    _transcript(path, '11', 'Correction: use blue')
    store = SQLiteMemoryStorage(str(tmp_path / 'mem.db'))
    report = ingest_owner_transcripts(store, bot='alpha', transcripts_glob=str(path), access_path='unused',
                                      supersedes={'11': '10'}, verifier=_verify)
    assert report['inserted'] == 2
    old = owner_key('alpha', '42', '10')
    new = owner_key('alpha', '42', '11')
    assert [r.key for r in store.retrieve(MemoryScope(project='alpha'))] == [new]
    assert [r.key for r in store.audit(MemoryScope(project='alpha'), old)] == [old, new]
    assert store.audit(MemoryScope(project='alpha'), old)[1].value['supersedes'] == old
    assert store.audit(MemoryScope(project='alpha'), new)[0].key == new


def test_verification_failure_and_ambiguous_message_are_gaps(tmp_path):
    path = tmp_path / 'bot.jsonl'
    _transcript(path, '10', 'Forbidden')
    _transcript(path, '11', 'First')
    _transcript(path, '11', 'Different')
    store = SQLiteMemoryStorage(str(tmp_path / 'mem.db'))
    def rejected(message_id, **kwargs):
        return {'status': 'SENDER_NOT_ALLOWED'}
    report = ingest_owner_transcripts(store, bot='alpha', transcripts_glob=str(path),
                                      access_path='unused', verifier=rejected)
    assert report['inserted'] == 0
    assert {gap['reason'] for gap in report['gaps']} == {'VERIFICATION_FAILED', 'AMBIGUOUS_MESSAGE'}
    assert store.retrieve(MemoryScope(project='alpha')) == []


def test_missing_correction_target_is_gap(tmp_path):
    path = tmp_path / 'bot.jsonl'
    _transcript(path, '11', 'Correction')
    store = SQLiteMemoryStorage(str(tmp_path / 'mem.db'))
    report = ingest_owner_transcripts(store, bot='alpha', transcripts_glob=str(path),
                                      access_path='unused', verifier=_verify, supersedes={'11': '10'})
    assert report['inserted'] == 0
    assert report['gaps'][0]['reason'] == 'WRITE_REJECTED'


def test_cli_reports_missing_transcript_without_activation(tmp_path):
    result = CliRunner().invoke(crew, ['ingest-owner-transcripts', '--bot', 'alpha',
                                       '--transcripts-glob', str(tmp_path / '*.jsonl'),
                                       '--access-path', str(tmp_path / 'access.json'),
                                       '--db', str(tmp_path / 'mem.db')])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['gaps'][0]['reason'] == 'MISSING_TRANSCRIPT'


def test_cli_verifies_owner_channel_evidence(tmp_path):
    path = tmp_path / 'bot.jsonl'
    _transcript(path, '20', 'Verbatim owner text')
    access = tmp_path / 'access.json'
    access.write_text(json.dumps({'allowFrom': ['42']}))
    db_path = tmp_path / 'mem.db'
    command = ['ingest-owner-transcripts', '--bot', 'alpha', '--transcripts-glob', str(path),
               '--access-path', str(access), '--db', str(db_path)]
    first = CliRunner().invoke(crew, command)
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)['inserted'] == 1
    assert json.loads(CliRunner().invoke(crew, command).output)['existing'] == 1
    stored = SQLiteMemoryStorage(str(db_path)).retrieve(MemoryScope(project='alpha'))[0]
    assert stored.value['verification']['status'] == 'VERIFIED'
    access.write_text(json.dumps({'allowFrom': ['other']}))
    assert json.loads(CliRunner().invoke(crew, command).output)['gaps'][0]['reason'] == 'VERIFICATION_FAILED'
