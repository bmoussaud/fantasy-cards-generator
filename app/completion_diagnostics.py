"""Closed, content-free completion diagnostics shared by HOSTED and WEB."""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

Checker = Literal[
    "unexpected_agent_response", "missing_raw_response", "non_stop_finish", "non_text_content"
]
FinishReason = Literal["stop", "length", "content_filter", "tool_calls", "function_call", "other"]
IncompleteReason = Literal["max_output_tokens", "content_filter", "other"]
TokenCount = Annotated[int, Field(strict=True, ge=0, le=2147483647)]

COMPLETION_ATTRIBUTE_KEYS = frozenset(
    {
        "fcg.completion_reason",
        "fcg.provider_finish_reason",
        "fcg.provider_incomplete_reason",
        "fcg.usage.input_tokens",
        "fcg.usage.output_tokens",
        "fcg.usage.total_tokens",
    }
)


def _reason(value: object, allowed: tuple[str, ...]) -> str | None:
    if type(value) is not str:
        return None
    return value if value in allowed else "other"


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2147483647 else None


def completion_attribute(key: str, value: object) -> str | int | None:
    if key == "fcg.completion_reason":
        return value if type(value) is str and value in get_args(Checker) else None
    if key == "fcg.provider_finish_reason":
        return _reason(value, get_args(FinishReason))
    if key == "fcg.provider_incomplete_reason":
        return _reason(value, get_args(IncompleteReason))
    if key in COMPLETION_ATTRIBUTE_KEYS:
        return _count(value)
    return None


class CompletionUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    inputTokens: TokenCount | None = None
    outputTokens: TokenCount | None = None
    totalTokens: TokenCount | None = None


class CompletionDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    stage: Literal["concept", "lore", "art_direction"]
    checker: Checker
    finishReason: FinishReason | None = None
    incompleteReason: IncompleteReason | None = None
    usage: CompletionUsage | None = None

    @classmethod
    def parse(cls, value: object) -> CompletionDiagnostics | None:
        if type(value) is not dict:
            return None
        stage, checker = value.get("stage"), value.get("checker")
        if (
            type(stage) is not str
            or stage not in ("concept", "lore", "art_direction")
            or type(checker) is not str
            or checker not in get_args(Checker)
        ):
            return None
        projected: dict[str, Any] = {"stage": stage, "checker": checker}
        for key, allowed in (
            ("finishReason", get_args(FinishReason)),
            ("incompleteReason", get_args(IncompleteReason)),
        ):
            reason = _reason(value.get(key), allowed)
            if reason is not None:
                projected[key] = reason
        usage = value.get("usage")
        if type(usage) is dict:
            counts = {}
            for key in ("inputTokens", "outputTokens", "totalTokens"):
                count = _count(usage.get(key))
                if count is not None:
                    counts[key] = count
            if counts:
                projected["usage"] = counts
        return cls.model_validate(projected)

    def attributes(self) -> dict[str, str | int]:
        attributes: dict[str, str | int] = {
            "fcg.stage": self.stage,
            "fcg.completion_reason": self.checker,
        }
        if self.finishReason is not None:
            attributes["fcg.provider_finish_reason"] = self.finishReason
        if self.incompleteReason is not None:
            attributes["fcg.provider_incomplete_reason"] = self.incompleteReason
        if self.usage is not None:
            for field, key in (
                ("inputTokens", "fcg.usage.input_tokens"),
                ("outputTokens", "fcg.usage.output_tokens"),
                ("totalTokens", "fcg.usage.total_tokens"),
            ):
                count = getattr(self.usage, field)
                if count is not None:
                    attributes[key] = count
        return attributes
