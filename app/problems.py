from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Request


@dataclass(slots=True)
class ProblemDetails(Exception):
    status_code: int
    title: str
    detail: str
    type: str
    error_code: str
    headers: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, request: Request) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "instance": str(request.url.path),
            "errorCode": self.error_code,
            "requestId": getattr(request.state, "request_id", None),
        }
        payload.update(self.extra)
        return payload
