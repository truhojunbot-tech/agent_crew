"""The dispatch wait distinguishes activity, silence, and its absolute cap."""
import asyncio
import time

import pytest

from agent_crew import server


class SlowProcess:
    returncode = None

    async def wait(self):
        await asyncio.sleep(1)
        self.returncode = 0
        return 0


@pytest.mark.parametrize("active", [True, False])
def test_idle_timeout_observes_log_growth(tmp_path, monkeypatch, active):
    monkeypatch.setattr(server, "_HEARTBEAT_INTERVAL_S", 0.005)
    log = tmp_path / "dispatch.log"
    log.write_bytes(b"")
    progress = []

    async def run():
        async def write_output():
            for _ in range(6):
                await asyncio.sleep(0.02)
                with log.open("ab") as stream:
                    stream.write(b"progress\n")

        writer = asyncio.create_task(write_output()) if active else None
        try:
            return await server._wait_for_dispatch_activity(
                SlowProcess(), str(log), 0, hard_timeout_s=0.11 if active else 1,
                idle_timeout_s=0.045, on_progress=lambda: progress.append(time.monotonic()),
            )
        finally:
            if writer:
                writer.cancel()

    reason, age = asyncio.run(run())
    assert reason == ("dispatcher_timeout" if active else "dispatcher_idle_timeout")
    assert age < 0.045 if active else age >= 0.045
    assert bool(progress) is active


def test_process_exit_precedes_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_HEARTBEAT_INTERVAL_S", 0.005)
    log = tmp_path / "dispatch.log"
    log.write_bytes(b"")

    class ExitingProcess:
        returncode = None

        async def wait(self):
            await asyncio.sleep(0.01)
            self.returncode = 0

    reason, _ = asyncio.run(server._wait_for_dispatch_activity(
        ExitingProcess(), str(log), 0, hard_timeout_s=0.1,
        idle_timeout_s=0.05, on_progress=lambda: None,
    ))
    assert reason is None


def test_active_output_survives_old_wall_limit(tmp_path, monkeypatch):
    """Scaled 50ms old reviewer wall: work continues past it with output."""
    monkeypatch.setattr(server, "_HEARTBEAT_INTERVAL_S", 0.005)
    log = tmp_path / "dispatch.log"
    log.write_bytes(b"")
    progress = []

    class ActiveProcess:
        returncode = None

        async def wait(self):
            for _ in range(5):
                await asyncio.sleep(0.02)
                with log.open("ab") as stream:
                    stream.write(b"tool call\n")
            self.returncode = 0

    reason, age = asyncio.run(server._wait_for_dispatch_activity(
        ActiveProcess(), str(log), 0, hard_timeout_s=0.2,
        idle_timeout_s=0.045, on_progress=lambda: progress.append(time.monotonic()),
    ))
    assert reason is None
    assert age < 0.045
    assert len(progress) >= 4
