#!/usr/bin/env python3
"""Human-readable viewer for dispatch_{role}.log JSONL files.

Usage:
    python -m agent_crew.log_viewer /path/to/dispatch_implementer.log
    # or: crew-log-viewer <path>  (entry point)
"""
import json
import sys
import time
from typing import Optional

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_MAGENTA = "\033[35m"
_BLUE = "\033[34m"


def _trunc(s: str, n: int = 120) -> str:
    s = s.replace("\n", " ").strip()
    return s[:n] + "…" if len(s) > n else s


def _format_tool_input(name: str, inp: dict) -> str:
    """Return a brief one-line summary of a tool call input."""
    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        return desc if desc else _trunc(cmd, 100)
    if name in ("Read", "Write", "Edit"):
        return inp.get("file_path", "?")
    if name == "Agent":
        return inp.get("description", _trunc(str(inp), 80))
    # Generic: show first string value
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return _trunc(v, 80)
    return _trunc(str(inp), 80)


def _format_tool_result(result_payload) -> str:
    """Extract a brief summary from a tool result."""
    if isinstance(result_payload, dict):
        stdout = result_payload.get("stdout", "")
        stderr = result_payload.get("stderr", "")
        interrupted = result_payload.get("interrupted", False)
        if stdout:
            lines = stdout.strip().splitlines()
            summary = f"{len(lines)}L: {_trunc(lines[-1], 80)}" if lines else ""
        elif stderr:
            summary = _trunc(stderr, 80)
        else:
            summary = str(result_payload)
        if interrupted:
            summary += " [interrupted]"
        return summary
    if isinstance(result_payload, str):
        return _trunc(result_payload, 100)
    return _trunc(str(result_payload), 100)


def _process_line(raw: str) -> Optional[str]:
    """Parse one JSONL line and return a formatted string, or None to skip."""
    raw = raw.strip()
    if not raw:
        return None

    # Non-JSON separator lines (task headers)
    if raw.startswith("=") or raw.startswith("TASK ") or raw.startswith("Reading "):
        return f"{_BOLD}{_CYAN}{raw}{_RESET}"

    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        # Codex outputs plain text (no --json flag); show as normal agent text.
        return f"{_GREEN}  {raw[:160]}{_RESET}"

    t = ev.get("type", "")

    if t == "assistant":
        msg = ev.get("message", {})
        parts = []
        for block in msg.get("content", []):
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(f"{_GREEN}  {_trunc(text, 120)}{_RESET}")
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                summary = _format_tool_input(name, inp)
                parts.append(f"{_YELLOW}  [{name}] {summary}{_RESET}")
        return "\n".join(parts) if parts else None

    if t == "user":
        res = ev.get("tool_use_result")
        if res is None:
            return None
        if isinstance(res, dict):
            stdout = res.get("stdout", "")
            stderr = res.get("stderr", "")
            if stdout or stderr:
                summary = _format_tool_result(res)
                return f"{_DIM}    → {summary}{_RESET}"
        return None

    if t == "result":
        subtype = ev.get("subtype", "")
        result_text = ev.get("result", "")
        cost = ev.get("total_cost_usd", 0)
        duration_ms = ev.get("duration_ms", 0)
        turns = ev.get("num_turns", 0)
        duration_s = duration_ms / 1000
        color = _GREEN if subtype == "success" else _RED
        cost_str = f"${cost:.3f}" if cost else ""
        summary = _trunc(result_text or subtype, 120)
        return (
            f"{_BOLD}{color}✓ DONE{_RESET} "
            f"{_DIM}({turns} turns, {duration_s:.0f}s{', ' + cost_str if cost_str else ''}){_RESET}\n"
            f"{color}  {summary}{_RESET}"
        )

    if t == "rate_limit_event":
        info = ev.get("rate_limit_info", {})
        return f"{_MAGENTA}  [rate-limit] {_trunc(str(info), 80)}{_RESET}"

    if t == "system":
        subtype = ev.get("subtype", "")
        if subtype in ("init",):
            model = ev.get("model", "")
            cwd = ev.get("cwd", "")
            return f"{_BLUE}  [session] model={model} cwd={cwd}{_RESET}" if model else None
        return None  # skip hook events etc.

    return None


def tail_and_format(path: str) -> None:
    """Open file, emit existing content formatted, then follow new lines."""
    try:
        f = open(path, "r", errors="replace")
    except FileNotFoundError:
        print(f"Waiting for {path}...", flush=True)
        while True:
            try:
                f = open(path, "r", errors="replace")
                break
            except FileNotFoundError:
                time.sleep(0.5)

    try:
        while True:
            line = f.readline()
            if line:
                formatted = _process_line(line)
                if formatted is not None:
                    print(formatted, flush=True)
            else:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        f.close()


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: crew-log-viewer <dispatch_log_path>", file=sys.stderr)
        sys.exit(1)
    tail_and_format(sys.argv[1])


if __name__ == "__main__":
    main()
