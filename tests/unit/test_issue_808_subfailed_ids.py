"""#808: pytest subtest failures keep their parent test ID."""

from agent_crew import script_tester


PARENT = (
    "tests/test_lemmalog_hooks.py::LemmalogHookTests::"
    "test_recovered_alpha_writer_to_hook_overcap_sessions"
)


def test_two_subfailures_deduplicate_to_parent():
    output = (
        f"SUBFAILED(case2) {PARENT} - AssertionError: second case\n"
        f"SUBFAILED(case3) {PARENT} - AssertionError: third case\n"
        "================ 2 failed, 804 passed in 12.3s ================\n"
    )
    counts, ids = script_tester.parse_test_output(output)
    assert counts == {"failed": 2, "passed": 804}
    assert ids == [PARENT]


def test_both_subfailed_forms_keep_parent_with_nested_delimiters():
    output = (
        f"SUBFAILED(case [2] - (retry)) {PARENT} - AssertionError: bad\n"
        f"SUBFAILED[case [3] - (retry)] {PARENT} - AssertionError: bad\n"
    )
    _, ids = script_tester.parse_test_output(output)
    assert ids == [PARENT]


def test_subfailed_message_and_params_keep_parent():
    output = (
        "SUBFAILED[m (a)] (i=1) test_p.py::test_fx - assert 1 == 0\n"
        "SUBFAILED[msg] (k=1) test_s.py::T::test_msg - AssertionError\n"
        "SUBFAILED[msg [nested] - text] (k=(1, 2)) "
        f"{PARENT} - AssertionError: bad\n"
    )
    _, ids = script_tester.parse_test_output(output)
    assert ids == ["test_p.py::test_fx", "test_s.py::T::test_msg", PARENT]


def test_subfailed_parent_becomes_new_failure_and_finding(tmp_path, monkeypatch):
    class Checkout:
        def __init__(self, repo, base_sha, head_sha=""):
            self.head_sha = head_sha

        def __enter__(self):
            return str(tmp_path)

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(script_tester, "Checkout", Checkout)
    calls = []

    def runner(command, cwd, timeout_s, *, env):
        calls.append(command)
        output = (
            f"SUBFAILED(case2) {PARENT} - AssertionError: bad\n"
            "================ 1 failed in 0.01s ================\n"
        ) if len(calls) == 1 else "================ 1 passed in 0.01s ================\n"
        counts, ids = script_tester.parse_test_output(output)
        return script_tester.RunOutcome(exit_code=1 if len(calls) == 1 else 0,
                                        counts=counts, failing_ids=ids)

    record = script_tester.run_script_test(
        str(tmp_path), base_sha="base", head_sha="head",
        state={"test_command": "make test"}, cache_dir=str(tmp_path / "cache"),
        runner=runner,
    )
    result = script_tester.record_to_result("test-808", record)
    assert calls == ["make test", "make test"]
    assert record["outcome"] == script_tester.FAILED
    assert record["new_failures"] == [PARENT]
    assert result.status == "failed"
    assert result.findings == [f"new failure: {PARENT}"]


def test_failed_and_error_summary_ids_still_parse():
    output = (
        "FAILED tests/test_x.py::test_case[a - b] - AssertionError: bad\n"
        "ERROR tests/test_y.py::test_setup - RuntimeError: broken\n"
    )
    _, ids = script_tester.parse_test_output(output)
    assert ids == ["tests/test_x.py::test_case[a - b]", "tests/test_y.py::test_setup"]
