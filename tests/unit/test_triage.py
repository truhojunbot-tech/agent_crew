from agent_crew.triage import build_prompt, filter_processed, parse_issues, parse_response


def make_issue(number=1, title="Fix bug", labels=None):
    label_objs = [{"name": l} for l in (labels or [])]
    return {"number": number, "title": title, "labels": label_objs, "body": "details"}


# U-T01: Parse GitHub issues JSON — number, title, labels correctly extracted
def test_u_t01_parse_issues():
    raw = [
        make_issue(1, "Add feature", ["enhancement", "agent_crew:todo"]),
        make_issue(2, "Fix bug", ["bug"]),
    ]
    result = parse_issues(raw)
    assert len(result) == 2
    assert result[0]["number"] == 1
    assert result[0]["title"] == "Add feature"
    assert result[0]["labels"] == ["enhancement", "agent_crew:todo"]
    assert result[1]["number"] == 2
    assert result[1]["labels"] == ["bug"]


# U-T02: Filter already-processed issues — "agent_crew:done" excluded
def test_u_t02_filter_processed():
    issues = [
        {"number": 1, "title": "Open", "labels": ["enhancement"]},
        {"number": 2, "title": "Done", "labels": ["agent_crew:done"]},
        {"number": 3, "title": "Also open", "labels": []},
    ]
    result = filter_processed(issues)
    assert len(result) == 2
    assert all(i["number"] != 2 for i in result)


# U-T03: Build triage prompt — issues and merge history present
def test_u_t03_build_prompt():
    issues = [
        {"number": 1, "title": "Add queue", "labels": ["enhancement"]},
        {"number": 2, "title": "Fix crash", "labels": ["bug"]},
    ]
    history = "Merged PR #10: implement protocol.py"
    prompt = build_prompt(issues, history)
    assert "Add queue" in prompt
    assert "Fix crash" in prompt
    assert history in prompt
    assert len(prompt) > 0


# U-T04: Parse triage agent response — issue number + description extracted
def test_u_t04_parse_response():
    text = "ISSUE: 3\nDESCRIPTION: Implement session manager with health check"
    result = parse_response(text)
    assert result is not None
    assert result["issue"] == 3
    assert "session" in result["description"].lower()


# U-T05: No open issues → build_prompt returns None
def test_u_t05_empty_issues():
    assert build_prompt([], "some history") is None


# U-T04b: parse_response ignores ISSUE:/DESCRIPTION: embedded in prose
def test_u_t04b_parse_response_anchored():
    # inline prose — should NOT match because not at line start
    prose = "We might tackle ISSUE: 99 later. The DESCRIPTION: unclear."
    result = parse_response(prose)
    assert result is None


# U-T04c: parse_response handles malformed response → None
def test_u_t04c_parse_response_missing_fields():
    assert parse_response("just some text with no markers") is None
    assert parse_response("ISSUE: 5\n(no description line)") is None
