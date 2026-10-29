"""The CLI wrapper emits a typed timeout on its own deadline (#87, #927)."""

import inspect
from agent_crew import cli


def test_wrapper_timeout_result_has_typed_kind():
    source = inspect.getsource(cli)
    assert '"error_info": {"kind": "timeout", "reason": "wrapper_timeout"}' in source
