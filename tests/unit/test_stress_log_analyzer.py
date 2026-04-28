"""Tests for the #121 stress log analyzer.

The analyzer reads a server.log slice and reports whether any watchdog
timeout markers fired during the stress window. The bash harness in
`scripts/stress_test_50_runs.sh` shells out to it; these tests pin the
parsing rules so a future log-format tweak surfaces here, not in
production after a 50-run sample wasted on a regex drift.
"""
from agent_crew._stress_log_analyzer import (
    StressLogReport,
    analyze,
    analyze_path,
    format_report,
)


# ---------------------------------------------------------------------------
# analyze() — pattern matching
# ---------------------------------------------------------------------------


class TestAnalyzeMatches:
    def test_clean_log_passes_with_zero_timeouts(self):
        log = [
            "[2026-04-28 03:00:00] INFO: POST /tasks: enqueued task_id=t1",
            "[2026-04-28 03:00:01] INFO: PUSH SUCCESS: task_id=t1",
            "[2026-04-28 03:00:02] INFO: POST /tasks/t1/result: status=completed",
        ]
        report = analyze(log)
        assert report.passed is True
        assert report.total_timeouts == 0

    def test_server_watchdog_timeout_caught(self):
        log = [
            "[2026-04-28 03:00:00] INFO: ok",
            "[2026-04-28 03:00:01] WARNING: WATCHDOG TIMEOUT: task_id=t-bad marked failed",
        ]
        report = analyze(log)
        assert report.passed is False
        assert report.counts["server_watchdog_timeout"] == 1

    def test_server_pane_idle_caught(self):
        log = [
            "watchdog timeout: pane idle 600s without progress on t-stuck",
        ]
        report = analyze(log)
        assert report.counts["server_pane_idle"] == 1

    def test_cli_pane_idle_caught(self):
        log = [
            "Warning: watchdog timeout: CLI detected pane idle for 320s on task 't1'",
        ]
        report = analyze(log)
        assert report.counts["cli_pane_idle"] == 1

    def test_cli_wrapper_exit_caught(self):
        log = [
            "Warning: watchdog timeout: crew run wrapper exited after 600s",
        ]
        report = analyze(log)
        assert report.counts["cli_wrapper_exit"] == 1

    def test_each_line_counted_once(self):
        # A line that matches more than one pattern (e.g. the literal
        # "WATCHDOG TIMEOUT" alongside "watchdog timeout: pane idle")
        # only contributes to the first matching bucket — keeps the
        # totals from double-counting.
        log = [
            "WARNING: WATCHDOG TIMEOUT: task_id=t-x; watchdog timeout: pane idle 600s",
        ]
        report = analyze(log)
        assert report.total_timeouts == 1

    def test_case_insensitive(self):
        log = ["WaTcHdOg TiMeOuT: pAnE iDlE for ages"]
        report = analyze(log)
        assert report.counts["server_pane_idle"] == 1

    def test_total_lines_includes_non_matches(self):
        log = ["normal", "more normal", "WATCHDOG TIMEOUT: bad"]
        report = analyze(log)
        assert report.total_lines == 3
        assert report.total_timeouts == 1


# ---------------------------------------------------------------------------
# analyze_path / format_report — round trip
# ---------------------------------------------------------------------------


class TestAnalyzePath:
    def test_reads_file_and_returns_report(self, tmp_path):
        log_path = tmp_path / "server.log"
        log_path.write_text(
            "INFO: enqueue ok\n"
            "WARNING: WATCHDOG TIMEOUT: task t-1 marked failed\n"
            "INFO: completed t-2\n"
        )
        report = analyze_path(str(log_path))
        assert report.passed is False
        assert report.total_timeouts == 1


class TestFormatReport:
    def test_pass_report_says_pass(self):
        report = StressLogReport(
            total_lines=42,
            counts={
                "server_watchdog_timeout": 0,
                "server_pane_idle": 0,
                "cli_pane_idle": 0,
                "cli_wrapper_exit": 0,
            },
            matching_lines=[],
        )
        out = format_report(report)
        assert "PASS" in out
        assert "0 timeout(s)" in out

    def test_fail_report_lists_examples(self):
        report = StressLogReport(
            total_lines=10,
            counts={
                "server_watchdog_timeout": 1,
                "server_pane_idle": 0,
                "cli_pane_idle": 0,
                "cli_wrapper_exit": 0,
            },
            matching_lines=[
                ("server_watchdog_timeout", "WATCHDOG TIMEOUT: task t-x"),
            ],
        )
        out = format_report(report)
        assert "FAIL" in out
        assert "1 timeout(s)" in out
        assert "task t-x" in out

    def test_examples_truncated_to_max(self):
        matching = [
            ("server_watchdog_timeout", f"WATCHDOG TIMEOUT: task t-{i}")
            for i in range(10)
        ]
        report = StressLogReport(
            total_lines=10,
            counts={
                "server_watchdog_timeout": 10,
                "server_pane_idle": 0,
                "cli_pane_idle": 0,
                "cli_wrapper_exit": 0,
            },
            matching_lines=matching,
        )
        out = format_report(report, max_examples=2)
        assert out.count("WATCHDOG TIMEOUT") == 2
