"""Server-side telegram notification helper (Issue #79).

Provides non-blocking, exception-safe notification functions for server lifecycle events.
"""
import os
import sys
from typing import Optional

import httpx


def notify_telegram(message: str, chat_id: Optional[str] = None) -> bool:
    """Send a message to Telegram via Bot API.

    Args:
        message: The message text to send.
        chat_id: Telegram chat ID. If None, reads from TELEGRAM_CHAT_ID env var.

    Returns:
        True if message was sent successfully (HTTP 200), False otherwise.
        Never raises exceptions.
    """
    try:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            return False

        resolved_chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        if not resolved_chat_id:
            return False

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        response = httpx.post(
            url,
            json={
                "chat_id": resolved_chat_id,
                "text": message,
            },
            timeout=5,
        )

        return response.status_code == 200
    except Exception:
        return False


def notify_console(message: str) -> bool:
    """Write a message to stderr as a fallback notification channel.

    Args:
        message: The message text to write.

    Returns:
        Always returns True.
    """
    try:
        print(message, file=sys.stderr)
        return True
    except Exception:
        return False
