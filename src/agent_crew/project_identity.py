"""Verify that a local server port still belongs to the expected project."""

import json
import urllib.request


class ProjectIdentityError(RuntimeError):
    """The server did not prove its project identity."""


def verify_server_identity(base_url: str, expected_project: str, timeout: float = 5.0) -> dict:
    url = f"{base_url.rstrip('/')}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
        actual = payload.get("project") if isinstance(payload, dict) else None
    except Exception as exc:
        raise ProjectIdentityError(
            f"identity verification failed: expected project={expected_project!r}; "
            f"health endpoint unavailable or invalid at {url}") from exc
    if not expected_project or actual != expected_project:
        raise ProjectIdentityError(
            f"identity verification failed: expected project={expected_project!r}, "
            f"server project={actual!r}")
    return payload
