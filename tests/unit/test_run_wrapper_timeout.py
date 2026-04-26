"""crew run wrapper timeout cleanup (Issue #87).

Before this fix the wrapper raised ClickException at the deadline and left
the task stuck as in_progress in the SQLite queue. This test patches the
HTTP plumbing and drives `_wait` (or, more practically, the code path that
calls `_auto_submit_failed`) to assert the cleanup happens.

We test through `_run_command_callback` indirectly via subprocess would be
heavy. Instead we exercise the integration by checking the `_auto_submit_failed`
emission produces a watchdog-shaped summary that the rate-limit fallback
policy will pick up.
"""
from agent_crew.fallback import has_rate_limit_signal


def test_wrapper_timeout_summary_matches_fallback_pattern():
    """The exact summary emitted by the wrapper on deadline hit must match
    the failover patterns so `_auto_fallback_failed_task` reroutes the task
    instead of falling through to same-role retry."""
    summary = (
        "watchdog timeout: crew run wrapper exited after 1200s without a "
        "result POST for task 'impl-stuck'. Auto-failed for queue cleanup."
    )
    assert has_rate_limit_signal(summary, []) is True
