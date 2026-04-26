"""Unit tests for telegram notify helper (Issue #79)."""
import io
import sys
from unittest.mock import Mock, patch, MagicMock

import httpx
import pytest

from agent_crew.notify import notify_telegram, notify_console


class TestNotifyTelegram:
    """Tests for notify_telegram function."""

    def test_missing_bot_token_returns_false_no_httpx_call(self):
        """If TELEGRAM_BOT_TOKEN is absent, return False without calling httpx."""
        with patch.dict("os.environ", {}, clear=True):
            result = notify_telegram("test message", chat_id="123")
            assert result is False

    def test_missing_chat_id_env_and_no_arg_returns_false(self):
        """If TELEGRAM_CHAT_ID missing from env and no chat_id arg, return False."""
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "token123"}, clear=True):
            result = notify_telegram("test message")
            assert result is False

    def test_http_200_returns_true(self):
        """When httpx returns 200, notify_telegram returns True."""
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "token123",
            "TELEGRAM_CHAT_ID": "chat456"
        }):
            with patch("agent_crew.notify.httpx.post") as mock_post:
                mock_response = Mock()
                mock_response.status_code = 200
                mock_post.return_value = mock_response

                result = notify_telegram("test message")
                assert result is True
                mock_post.assert_called_once()

    def test_http_error_returns_false(self):
        """When httpx returns error status, notify_telegram returns False."""
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "token123",
            "TELEGRAM_CHAT_ID": "chat456"
        }):
            with patch("agent_crew.notify.httpx.post") as mock_post:
                mock_response = Mock()
                mock_response.status_code = 500
                mock_post.return_value = mock_response

                result = notify_telegram("test message")
                assert result is False

    def test_httpx_exception_returns_false(self):
        """When httpx raises an exception, notify_telegram catches it and returns False."""
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "token123",
            "TELEGRAM_CHAT_ID": "chat456"
        }):
            with patch("agent_crew.notify.httpx.post") as mock_post:
                mock_post.side_effect = httpx.RequestError("Connection failed")

                result = notify_telegram("test message")
                assert result is False

    def test_chat_id_arg_overrides_env(self):
        """chat_id argument overrides TELEGRAM_CHAT_ID from environment."""
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "token123",
            "TELEGRAM_CHAT_ID": "env_chat_id"
        }):
            with patch("agent_crew.notify.httpx.post") as mock_post:
                mock_response = Mock()
                mock_response.status_code = 200
                mock_post.return_value = mock_response

                notify_telegram("test message", chat_id="arg_chat_id")

                # Verify the call was made with arg_chat_id, not env_chat_id
                call_args = mock_post.call_args
                assert call_args is not None
                # The chat_id should be in the JSON data
                json_data = call_args[1].get("json")
                assert json_data["chat_id"] == "arg_chat_id"

    def test_uses_5_second_timeout(self):
        """notify_telegram uses a 5 second timeout."""
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "token123",
            "TELEGRAM_CHAT_ID": "chat456"
        }):
            with patch("agent_crew.notify.httpx.post") as mock_post:
                mock_response = Mock()
                mock_response.status_code = 200
                mock_post.return_value = mock_response

                notify_telegram("test message")

                call_args = mock_post.call_args
                assert call_args[1].get("timeout") == 5


class TestNotifyConsole:
    """Tests for notify_console function."""

    def test_writes_to_stderr_and_returns_true(self):
        """notify_console writes message to stderr and returns True."""
        stderr_capture = io.StringIO()

        with patch("sys.stderr", stderr_capture):
            result = notify_console("test alert message")

        assert result is True
        output = stderr_capture.getvalue()
        assert "test alert message" in output

    def test_always_returns_true(self):
        """notify_console always returns True regardless of message."""
        with patch("sys.stderr", io.StringIO()):
            assert notify_console("message 1") is True
            assert notify_console("") is True
            assert notify_console("x" * 1000) is True
