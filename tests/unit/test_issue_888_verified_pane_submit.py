"""#888: every text injection into a pane is a verified submit.

A fake tmux keeps an input line per pane. Text lands in it; Enter submits it
only when the fake is told to honour that Enter, so a dropped Enter leaves the
text parked exactly as the real TUI did in #74.
"""
import threading

import pytest

from agent_crew import pane_submit, server, session, setup


class FakeTmux:
    def __init__(self, drop_enters=0, prompt="❯ "):
        self.drop_enters = drop_enters
        self.prompt = prompt
        self.input = ""
        self.history = ""
        self.buffer = ""
        self.calls = []

    def screen(self):
        if self.prompt.startswith("$"):  # a shell: nothing below the cursor line
            return f"{self.history}\n{self.prompt}{self.input}\n"
        return f"{self.history}\n{self.prompt}{self.input}\n────────\n  footer"

    def run(self, cmd, *a, **kw):
        self.calls.append(cmd)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        r = R()
        if cmd[:2] == ["tmux", "load-buffer"]:
            self.buffer = kw.get("input", "")
        elif cmd[:2] == ["tmux", "paste-buffer"]:
            self.input += self.buffer
        elif cmd[:3] == ["tmux", "send-keys", "-l"]:
            self.input += cmd[-1]
        elif cmd[:2] == ["tmux", "send-keys"] and cmd[-1] == "Enter":
            if self.drop_enters:
                self.drop_enters -= 1
            else:
                self.history += f"\n> {self.input}\nok"
                self.input = ""
        elif cmd[:2] == ["tmux", "capture-pane"]:
            r.stdout = self.screen()
        return r

    def enters(self):
        return [c for c in self.calls if c[:2] == ["tmux", "send-keys"] and c[-1] == "Enter"]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    def make(**kw):
        t = FakeTmux(**kw)
        monkeypatch.setattr(pane_submit.subprocess, "run", t.run)
        return t
    monkeypatch.setattr(pane_submit.time, "sleep", lambda *_a: None)
    monkeypatch.setenv("AGENT_CREW_PANE_LOCK_DIR", str(tmp_path / "locks"))
    return make


def test_an_unsubmitted_paste_is_detected_and_retried(fake):
    t = fake(drop_enters=2)
    assert pane_submit.submit("%1", "hello world task") == "submitted"
    assert len(t.enters()) == 3
    assert t.input == ""


def test_a_paste_that_never_submits_is_reported_not_claimed(fake):
    t = fake(drop_enters=99)
    assert pane_submit.submit("%1", "hello", delays=(0, 0, 0)) == "unsubmitted"
    assert len(t.enters()) == 3


def test_submitted_text_echoed_in_history_is_not_mistaken_for_pending(fake):
    """The transcript echo of the text is above the prompt, not in the box."""
    t = fake()
    assert pane_submit.submit("%1", "same text") == "submitted"
    assert "same text" in t.screen()
    assert len(t.enters()) == 1


def test_a_hard_wrapped_input_is_still_recognised_as_pending():
    screen = "❯ Start your task loop no\nw: call get_next_task\n───\n"
    assert pane_submit.text_pending("Start your task loop now: call get_next_task")(screen)


def test_collapsed_paste_placeholder_is_pending():
    assert pane_submit.text_pending("x" * 500)("❯ [Pasted text #2 +42 lines]\n───")


def test_tmux_refusing_the_text_is_send_failed(fake):
    t = fake()
    real = t.run

    def failing(cmd, *a, **kw):
        r = real(cmd, *a, **kw)
        if cmd[:3] == ["tmux", "send-keys", "-l"]:
            r.returncode = 1
        return r
    pane_submit.subprocess.run = failing
    assert pane_submit.submit("%1", "/clear", paste=False) == "send_failed"
    assert t.enters() == []


def test_a_busy_pane_lock_sends_nothing(fake, tmp_path):
    import fcntl
    import os
    t = fake()
    lock_dir = tmp_path / "locks"
    os.makedirs(lock_dir, exist_ok=True)
    fh = open(lock_dir / "_1.lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        done = []
        th = threading.Thread(target=lambda: done.append(pane_submit.submit("%1", "x")))
        th.start()
        th.join(5)
        assert done == ["send_failed"]
        assert t.calls == []
    finally:
        fh.close()


def test_shell_command_pending_while_screen_ends_with_it():
    cmd = "cd /wt && claude --dangerously-skip-permissions"
    pending = pane_submit.shell_line_pending(cmd)
    assert pending(f"user@h:~$ {cmd}\n")
    assert not pending(f"user@h:~$ {cmd}\nWelcome to Claude\n")


def test_default_push_retries_dropped_enter_via_verified_submit(fake):
    t = fake(drop_enters=1)
    server._default_push("%1", "=== AGENT_CREW TASK ===\ntask_id: t-1\nbody")
    assert len(t.enters()) == 2
    assert t.input == ""


def test_clear_context_retries_and_reports_unsubmitted(fake, monkeypatch):
    monkeypatch.setattr(server.time, "sleep", lambda *_a: None)
    t = fake(drop_enters=1)
    assert server._pane_clear_context("%1") == "attempted"
    assert len(t.enters()) == 2
    t = fake(drop_enters=99)
    assert server._pane_clear_context("%1") == "unsubmitted"


def test_setup_kickoff_and_launch_are_verified(fake, monkeypatch):
    t = fake(drop_enters=1, prompt="$ ")
    monkeypatch.setattr(setup.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(setup, "_CLI_READY_MARKERS", {})
    setup.start_agents_in_panes("s", ["gemini"], pane_targets=["%5"])
    assert t.input == ""
    # launch: 1 dropped + 1 honoured; kickoff: 1 honoured (+ the q Enter)
    assert len(t.enters()) == 4


def test_log_viewer_launch_is_verified(fake, monkeypatch, tmp_path):
    t = fake(drop_enters=1, prompt="$ ")
    monkeypatch.setattr(setup.time, "sleep", lambda *_a: None)
    setup.start_log_viewers_in_panes(["claude"], ["%6"], str(tmp_path))
    assert t.input == ""
    assert len(t.enters()) == 2


def test_dead_refresh_pane_is_gone():
    assert not hasattr(session, "refresh_pane")


class MultiPaneTmux:
    """tmux buffers are server-global; inputs are per pane. Two concurrent
    load-buffers both land before either paste (the barrier), which is the
    interleaving per-pane locks do not prevent."""

    def __init__(self):
        self.buffers = {}
        self.inputs = {}
        self.barrier = threading.Barrier(2, timeout=5)

    @staticmethod
    def _opt(cmd, flag):
        return cmd[cmd.index(flag) + 1] if flag in cmd else None

    def run(self, cmd, *a, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        r = R()
        if cmd[:2] == ["tmux", "load-buffer"]:
            # Without -b tmux creates a new top-of-stack buffer; paste without
            # -b takes the top one — i.e. whichever load ran last.
            self.buffers[self._opt(cmd, "-b") or "__top__"] = kw.get("input", "")
            self.barrier.wait()
        elif cmd[:2] == ["tmux", "paste-buffer"]:
            name = self._opt(cmd, "-b") or "__top__"
            pane = self._opt(cmd, "-t")
            self.inputs[pane] = self.inputs.get(pane, "") + self.buffers.get(name, "")
            if "-d" in cmd:
                self.buffers.pop(name, None)
        elif cmd[:2] == ["tmux", "send-keys"] and cmd[-1] == "Enter":
            self.inputs[self._opt(cmd, "-t")] = ""
        elif cmd[:2] == ["tmux", "capture-pane"]:
            r.stdout = f"❯ {self.inputs.get(self._opt(cmd, '-t'), '')}\n────────\n"
        return r


def test_concurrent_submits_to_different_panes_never_cross_paste(monkeypatch, tmp_path):
    t = MultiPaneTmux()
    pasted = {}
    real = t.run

    def spy(cmd, *a, **kw):
        if cmd[:2] == ["tmux", "paste-buffer"]:
            pane = MultiPaneTmux._opt(cmd, "-t")
            name = MultiPaneTmux._opt(cmd, "-b") or "__top__"
            pasted[pane] = t.buffers.get(name)
        return real(cmd, *a, **kw)
    monkeypatch.setattr(pane_submit.subprocess, "run", spy)
    monkeypatch.setattr(pane_submit.time, "sleep", lambda *_a: None)
    monkeypatch.setenv("AGENT_CREW_PANE_LOCK_DIR", str(tmp_path / "locks"))

    out = {}
    threads = [threading.Thread(target=lambda p=p, s=s: out.__setitem__(
        p, pane_submit.submit(p, s))) for p, s in (("%1", "task for one"), ("%2", "task for two"))]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert pasted == {"%1": "task for one", "%2": "task for two"}
    assert out == {"%1": "submitted", "%2": "submitted"}
    assert t.buffers == {}  # every named buffer was deleted by its paste


def test_unopenable_lock_file_sends_nothing(fake, tmp_path, monkeypatch):
    t = fake()
    not_a_dir = tmp_path / "locks-is-a-file"
    not_a_dir.write_text("")
    monkeypatch.setenv("AGENT_CREW_PANE_LOCK_DIR", str(not_a_dir))
    assert pane_submit.submit("%1", "x") == "send_failed"
    assert t.calls == []


def test_failed_paste_deletes_its_named_buffer(fake):
    t = fake()
    real = t.run

    def failing(cmd, *a, **kw):
        r = real(cmd, *a, **kw)
        if cmd[:2] == ["tmux", "paste-buffer"]:
            r.returncode = 1
        return r
    pane_submit.subprocess.run = failing
    assert pane_submit.submit("%1", "hello") == "send_failed"
    load = next(c for c in t.calls if c[:2] == ["tmux", "load-buffer"])
    assert ["tmux", "delete-buffer", "-b", load[3]] in t.calls
    assert t.enters() == []
