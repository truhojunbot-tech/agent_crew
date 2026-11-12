"""#1016: only ``verdict:`` lines are verdict lines -- prose that merely starts
with the word "Verdict" is review text, not a second verdict."""

from agent_crew import chatgpt_review as chatgpt

SHA = "a" * 40
TASK = "review-impl-1016-r0"
PROSE = "Verdict covers this exact SHA and nothing after it."


def _parse(*lines):
    body = "\n".join((chatgpt.reply_header(TASK, SHA),) + lines)
    return chatgpt.parse_reply(body, task_id=TASK, sha=SHA)


def test_prose_line_starting_with_verdict_is_text_not_a_verdict_line():
    parsed = _parse("VERDICT: approve", PROSE)
    assert parsed.kind == "accepted", parsed
    assert parsed.verdict == "approve"
    assert PROSE in parsed.text


def test_prose_before_the_verdict_line_is_kept_too():
    parsed = _parse("Verdict is below.", "VERDICT: request_changes", "fix x")
    assert parsed.kind == "accepted", parsed
    assert parsed.verdict == "request_changes"
    assert "Verdict is below." in parsed.text and "fix x" in parsed.text


def test_two_verdict_lines_are_rejected():
    parsed = _parse("VERDICT: approve", "VERDICT: request_changes", "fix x")
    assert parsed.kind == "rejected"
    assert parsed.reason == "2 VERDICT lines"


def test_spaced_colon_still_counts_as_a_second_verdict_line():
    parsed = _parse("VERDICT: approve", "  verdict : request_changes")
    assert parsed.kind == "rejected"
    assert parsed.reason == "2 VERDICT lines"


def test_lowercase_or_capitalised_verdict_value_is_garbled():
    parsed = _parse("verdict: Approve")
    assert parsed.kind == "rejected"
    assert parsed.reason.startswith("garbled VERDICT line")


def test_no_verdict_line_is_missing():
    parsed = _parse(PROSE)
    assert parsed.kind == "rejected"
    assert parsed.reason == "missing VERDICT line"
