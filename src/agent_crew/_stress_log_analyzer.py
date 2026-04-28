"""Stress-run log analyzer (Issue #121 acceptance support).

`scripts/stress_test_50_runs.sh` runs ``crew run`` N times and points
at the server's log + the wrapper-side stdout. After the run, the
operator (or this analyzer programmatically) needs to know how many of
the failure modes #121 cares about actually fired:

  - server-side: ``WATCHDOG TIMEOUT: ...`` / ``watchdog timeout: pane idle``
  - CLI-side:    ``watchdog timeout: CLI detected pane idle ...``
                 ``watchdog timeout: crew run wrapper exited ...``

These are emitted as plain log lines by ``server.py`` and ``cli.py``
respectively. We grep them rather than re-parse structured logs because
the format has been stable for three issue cycles (#85, #87, #92, #103)
and a regex change was always preferred over a schema change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Order matters — first match wins so a single line with multiple
# patterns gets categorized once. The two CLI-side patterns are more
# specific than the generic "watchdog timeout: pane idle" / "WATCHDOG
# TIMEOUT" markers, so they must match first.
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "cli_wrapper_exit",
        re.compile(r"watchdog timeout:\s+crew run wrapper exited", re.IGNORECASE),
    ),
    (
        "cli_pane_idle",
        re.compile(r"watchdog timeout:\s+CLI detected pane idle", re.IGNORECASE),
    ),
    (
        "server_pane_idle",
        re.compile(r"watchdog timeout:\s+pane idle", re.IGNORECASE),
    ),
    # Bare "WATCHDOG TIMEOUT:" header (server.py:499). The other server
    # marker also gets emitted alongside this one, so we keep this last
    # to catch lines that don't carry the more specific suffix.
    ("server_watchdog_timeout", re.compile(r"WATCHDOG TIMEOUT:", re.IGNORECASE)),
)


@dataclass
class StressLogReport:
    total_lines: int
    counts: dict[str, int]
    matching_lines: list[tuple[str, str]]  # (category, line)

    @property
    def total_timeouts(self) -> int:
        return sum(self.counts.values())

    @property
    def passed(self) -> bool:
        """#121 acceptance criterion: zero watchdog timeouts."""
        return self.total_timeouts == 0


def analyze(lines: Iterable[str]) -> StressLogReport:
    """Scan log lines and bucket each match into a category."""
    counts: dict[str, int] = {name: 0 for name, _ in _PATTERNS}
    matching: list[tuple[str, str]] = []
    total = 0
    for raw in lines:
        total += 1
        line = raw.rstrip("\n")
        for name, pat in _PATTERNS:
            if pat.search(line):
                counts[name] += 1
                matching.append((name, line))
                break
    return StressLogReport(
        total_lines=total,
        counts=counts,
        matching_lines=matching,
    )


def analyze_path(path: str) -> StressLogReport:
    with open(path, encoding="utf-8", errors="replace") as f:
        return analyze(f)


def format_report(report: StressLogReport, *, max_examples: int = 3) -> str:
    """Human-readable summary suitable for CI logs / Telegram messages."""
    lines = []
    verdict = "PASS" if report.passed else "FAIL"
    lines.append(
        f"stress log analysis: {verdict} "
        f"({report.total_timeouts} timeout(s) across {report.total_lines} lines)"
    )
    for name, count in report.counts.items():
        lines.append(f"  {name}: {count}")
    if report.matching_lines:
        lines.append("examples:")
        for category, line in report.matching_lines[:max_examples]:
            lines.append(f"  [{category}] {line[:200]}")
    return "\n".join(lines)


def main() -> int:  # pragma: no cover — CLI shim
    import argparse

    p = argparse.ArgumentParser(
        description="Analyze a server.log for #121 watchdog-timeout markers."
    )
    p.add_argument("log_path")
    p.add_argument("--max-examples", type=int, default=5)
    args = p.parse_args()

    report = analyze_path(args.log_path)
    print(format_report(report, max_examples=args.max_examples))
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
