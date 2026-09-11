"""Content-free process observations, shared by console and telemetry export."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

LOGGER_NAME = "fantasy_cards_generator.telemetry.rotation"
EVENT_NAME = "secret.rotation_observation"
SCHEMA_VERSION = 1
SECRET_ALIASES = {"APP_SESSION_SECRET_KEY": "session", "ENTRA_CLIENT_SECRET": "entra"}
ERROR_CATEGORIES = {
    "none",
    "unknown",
    "timeout",
    "not_found",
    "access_denied",
    "network_error",
    "unavailable",
    "binding_error",
}
_ENUMS = {
    "event": {EVENT_NAME},
    "logical_secret": {"session", "entra"},
    "source": {"azure", "env", "unknown"},
    "stage": {"provider", "entra_binding"},
    "result": {"adopted", "unchanged", "stale", "failed"},
    "error_category": ERROR_CATEGORIES,
}
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", re.ASCII)
_UUID_PATTERN = re.compile(r"[0-9a-f]{32}", re.ASCII)
_HASH_PATTERN = re.compile(r"[0-9a-f]{12}", re.ASCII)
_TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", re.ASCII)
_INTEGER_LIMITS = {"schema_version": 1, "pid": 2**31 - 1, "sequence": 2**63 - 1}
_logger = logging.getLogger(LOGGER_NAME)
_lock = threading.RLock()
_pid: int | None = None
_incarnation: str | None = None
_sequence = 0


def _after_fork() -> None:
    global _lock, _pid, _incarnation, _sequence
    # Neither an inherited locked mutex nor the parent's identity may survive fork.
    _lock = threading.RLock()
    _pid = None
    _incarnation = None
    _sequence = 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def version_hash(version: str | None) -> str:
    """Hash only the provider version identifier, never a secret value."""
    return hashlib.sha256(version.encode("utf-8")).hexdigest()[:12] if version else "unknown"


def safe_platform_id(value: Any) -> str:
    return value if isinstance(value, str) and _ID_PATTERN.fullmatch(value) else "unknown"


def validate_envelope(values: Mapping[str, Any]) -> dict[str, Any] | None:
    """Accept the complete v1 contract only; discard all non-contract attributes."""
    safe: dict[str, Any] = {}
    for key, allowed in _ENUMS.items():
        value = values.get(key)
        if not isinstance(value, str) or value not in allowed:
            return None
        safe[key] = value
    for key, maximum in _INTEGER_LIMITS.items():
        value = values.get(key)
        if type(value) is not int or not 1 <= value <= maximum:
            return None
        safe[key] = value
    for key, pattern in (
        ("incarnation", _UUID_PATTERN),
        ("observed_at", _TIME_PATTERN),
        ("revision", _ID_PATTERN),
        ("replica", _ID_PATTERN),
    ):
        value = values.get(key)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            return None
        safe[key] = value
    try:
        datetime.fromisoformat(safe["observed_at"])
    except ValueError:
        return None
    version = values.get("version_hash")
    if not isinstance(version, str) or (
        version != "unknown" and not _HASH_PATTERN.fullmatch(version)
    ):
        return None
    safe["version_hash"] = version
    if safe["stage"] == "entra_binding" and safe["logical_secret"] != "entra":
        return None
    if (safe["result"] in {"adopted", "unchanged"}) != (safe["error_category"] == "none"):
        return None
    return safe


def envelope_json(envelope: Mapping[str, Any]) -> str:
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sanitize_log_body(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, str) or len(body) > 4096:
        return None
    try:
        values = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return validate_envelope(values) if isinstance(values, dict) else None


class _ConsoleHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        # Resolve stderr per emission (including after fork); no framework prefix.
        self.stream = sys.stderr
        envelope = sanitize_log_body(record.msg) if not record.args else None
        sanitized = logging.LogRecord(
            LOGGER_NAME,
            logging.INFO,
            "",
            0,
            envelope_json(envelope) if envelope else "rotation.observation_rejected",
            (),
            None,
        )
        super().emit(sanitized)

    def handleError(self, record: logging.LogRecord) -> None:
        # Standard logging's traceback/record dump could expose an I/O exception.
        sys.stderr.write('{"event":"rotation.observability_delivery_failed"}\n')


def configure_rotation_logging(export_handler: logging.Handler | None = None) -> None:
    with _lock:
        _logger.setLevel(logging.INFO)
        _logger.propagate = False
        if not any(isinstance(handler, _ConsoleHandler) for handler in _logger.handlers):
            handler = _ConsoleHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            _logger.addHandler(handler)
        if export_handler is not None:
            _logger.addHandler(export_handler)


def emit_observation(
    *,
    name: str,
    source: str,
    stage: Literal["provider", "entra_binding"],
    result: Literal["adopted", "unchanged", "stale", "failed"],
    version: str | None,
    error_category: str = "none",
) -> None:
    """Emit only the two application rotation targets, not arbitrary vault secrets."""
    global _pid, _incarnation, _sequence
    if name not in SECRET_ALIASES:
        return
    with _lock:
        configure_rotation_logging()
        pid = os.getpid()
        if _pid != pid:
            _pid, _incarnation, _sequence = pid, uuid4().hex, 0
        _sequence += 1
        envelope = validate_envelope(
            {
                "event": EVENT_NAME,
                "schema_version": SCHEMA_VERSION,
                "observed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "sequence": _sequence,
                "pid": _pid,
                "incarnation": _incarnation,
                "revision": safe_platform_id(os.getenv("CONTAINER_APP_REVISION")),
                "replica": safe_platform_id(os.getenv("CONTAINER_APP_REPLICA_NAME")),
                "logical_secret": SECRET_ALIASES[name],
                "source": source if source in {"azure", "env"} else "unknown",
                "stage": stage,
                "result": result,
                "version_hash": version_hash(version),
                "error_category": (
                    error_category if error_category in ERROR_CATEGORIES else "unknown"
                ),
            }
        )
        if envelope is None:
            raise ValueError("Invalid rotation observation contract.")
        _logger.info(envelope_json(envelope))
