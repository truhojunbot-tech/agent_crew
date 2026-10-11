"""A lost POST response must not create a second crew-run task (#658)."""

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from agent_crew.loop import _post_task_http
from agent_crew.protocol import TaskRequest


def _req():
    return TaskRequest("impl-658", "implement", "work", project="agent_crew")


def _response(task_id="impl-658"):
    return io.BytesIO(json.dumps({"task_id": task_id}).encode())


def _not_found(request):
    return urllib.error.HTTPError(request.full_url, 404, "missing", {}, io.BytesIO())


def _conflict(request, *, task_id="impl-658"):
    body = {"detail": {"error": "task_id already exists", "task_id": task_id}}
    return urllib.error.HTTPError(request.full_url, 409, "exists", {},
                                 io.BytesIO(json.dumps(body).encode()))


def test_lost_response_after_insert_uses_project_scoped_get_without_second_post():
    calls = []

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        assert request.get_header("X-agent-crew-project") == "agent_crew"
        if request.get_method() == "POST":
            raise TimeoutError("response read timed out after insert")
        return _response()

    with patch("agent_crew.loop.urllib.request.urlopen", side_effect=urlopen):
        assert _post_task_http(8105, _req()) == "impl-658"
    assert [request.get_method() for request, _ in calls] == ["POST", "GET"]
    assert calls[1][0].full_url.endswith("/tasks/impl-658")
    assert calls[0][1] > 5


def test_timeout_before_insert_retries_post_once_with_longer_timeout():
    calls = []

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            raise TimeoutError("connect/read timed out before insert")
        if request.get_method() == "GET":
            raise _not_found(request)
        return _response()

    with patch("agent_crew.loop.urllib.request.urlopen", side_effect=urlopen):
        assert _post_task_http(8105, _req()) == "impl-658"
    assert [request.get_method() for request, _ in calls] == ["POST", "GET", "POST"]
    assert calls[2][1] > calls[0][1]
    assert calls[2][0].data == calls[0][0].data


def test_retry_409_is_accepted_only_if_same_task_id_exists():
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise TimeoutError("response timed out")
        if request.get_method() == "GET" and len(calls) == 2:
            raise _not_found(request)
        if request.get_method() == "POST":
            raise _conflict(request)
        return _response()

    with patch("agent_crew.loop.urllib.request.urlopen", side_effect=urlopen):
        assert _post_task_http(8105, _req()) == "impl-658"
    assert [request.get_method() for request in calls] == ["POST", "GET", "POST", "GET"]


def test_retry_409_for_another_task_is_not_accepted():
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise TimeoutError("response timed out")
        if request.get_method() == "GET":
            raise _not_found(request)
        raise _conflict(request, task_id="other-impl")

    with patch("agent_crew.loop.urllib.request.urlopen", side_effect=urlopen):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_task_http(8105, _req())
    assert exc.value.code == 409
    assert [request.get_method() for request in calls] == ["POST", "GET", "POST", "GET"]
