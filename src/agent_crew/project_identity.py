"""Fail-closed verification that a local crew port serves this project (#362)."""

import json
import logging
import urllib.error
import urllib.request


logger = logging.getLogger(__name__)


class ProjectIdentityError(RuntimeError):
    """The endpoint did not prove that it belongs to the expected project."""


def verify_server_identity(base_url: str, expected_project: str, timeout: float = 5.0) -> dict:
    """Return `/health` only when it identifies the expected project.

    An unavailable endpoint, invalid response, missing identity, or a different
    project are all attachment failures.  Callers must not continue with a
    task dequeue or result submission after this exception.
    """
    url = f"{base_url.rstrip('/')}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
        actual_project = payload.get("project")
    except Exception as exc:  # urllib and malformed JSON are equally unproven.
        logger.error(
            "#362 identity verification failed: expected project=%r at %s; "
            "health response unavailable or invalid: %s",
            expected_project, url, exc,
        )
        raise ProjectIdentityError(
            f"identity verification failed: expected project={expected_project!r}; "
            f"health endpoint unavailable or invalid at {url}"
        ) from exc

    if not isinstance(actual_project, str) or actual_project != expected_project:
        logger.error(
            "#362 identity verification failed: expected project=%r, server project=%r at %s",
            expected_project, actual_project, url,
        )
        raise ProjectIdentityError(
            f"identity verification failed: expected project={expected_project!r}, "
            f"server project={actual_project!r}"
        )
    return payload
