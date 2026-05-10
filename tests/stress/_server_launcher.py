"""Test server launcher for stress_test_50runs.sh.

Usage: python3 _server_launcher.py <port> <log_file> [<db_file>]

Starts a uvicorn server with:
  - pane_map=None (MCP-pull mode, no tmux panes needed)
  - watchdog enabled with fast intervals so spurious fires show up
  - anomaly detection disabled (no GitHub dependency)
  - AGENT_CREW_DELIVERY=both so GET /tasks/next works
"""
import sys
import tempfile
import logging
import os

import uvicorn

# Resolve package — handle running from repo root or from the stress dir.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent_crew.server import create_app  # noqa: E402

def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <port> <log_file> [<db_file>]", file=sys.stderr)
        sys.exit(64)

    port = int(sys.argv[1])
    log_file = sys.argv[2]
    if len(sys.argv) > 3:
        db_file = sys.argv[3]
    else:
        fd, db_file = tempfile.mkstemp(suffix=".stress.db")
        os.close(fd)

    # Route server logger to the log file so the bash harness can grep it.
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler.setFormatter(fmt)
    logging.getLogger("agent_crew").addHandler(file_handler)
    logging.getLogger("agent_crew").setLevel(logging.DEBUG)

    app = create_app(
        db_file,
        pane_map=None,           # MCP-pull mode — no tmux panes
        watchdog_disabled=False, # Keep watchdog ON; it must NOT fire
        watchdog_interval=1.0,   # Tick every second during the test
        reminder_seconds=5.0,
        timeout_seconds=10.0,    # Short timeout — would catch any leak quickly
        anomaly_disabled=True,
    )

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
