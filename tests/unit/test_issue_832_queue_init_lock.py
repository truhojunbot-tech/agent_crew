import logging
import sqlite3

import pytest

from agent_crew.queue import TaskQueue


@pytest.mark.parametrize('always_locked', [False, True])
def test_schema_init_retries_busy_commit_and_preserves_failure(tmp_path, monkeypatch, caplog, always_locked):
    path = str(tmp_path / 'queue.db')
    original = TaskQueue._connect
    attempts = []

    class Connection:
        def __init__(self, real):
            self.real = real
        def execute(self, *args, **kwargs):
            return self.real.execute(*args, **kwargs)
        def commit(self):
            attempts.append(1)
            if always_locked or len(attempts) == 1:
                raise sqlite3.OperationalError('database is locked')
            return self.real.commit()
        def rollback(self):
            return self.real.rollback()
        def close(self):
            return self.real.close()

    def connect(self):
        return Connection(original(self))

    monkeypatch.setattr(TaskQueue, '_connect', connect)
    monkeypatch.setattr('agent_crew.queue.time.sleep', lambda _seconds: None)
    with caplog.at_level(logging.WARNING):
        if always_locked:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                TaskQueue(path)
            assert len(attempts) == 5
        else:
            TaskQueue(path)
            assert len(attempts) == 2
    assert 'schema init locked' in caplog.text
