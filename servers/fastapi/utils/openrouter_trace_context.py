import json
from contextvars import ContextVar, Token
from typing import Any, Optional


NEVEL_OPENROUTER_TRACE_HEADER = "x-nevel-openrouter-trace"

_ALLOWED_TRACE_KEYS = {
    "trace_id",
    "app_event_id",
    "app_event_realm",
    "app_event_type",
    "workspace_id",
    "app",
    "env",
    "api_key_id",
}

_openrouter_trace_context: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "openrouter_trace_context",
    default=None,
)


def _clean_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_trace_fields(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None

    cleaned: dict[str, Any] = {}
    raw_trace = raw.get("trace")
    if isinstance(raw_trace, dict):
        trace = {
            key: value
            for key in _ALLOWED_TRACE_KEYS
            if (value := _clean_string(raw_trace.get(key))) is not None
        }
        if trace:
            cleaned["trace"] = trace

    for key in ("user", "session_id"):
        value = _clean_string(raw.get(key))
        if value is not None:
            cleaned[key] = value

    return cleaned or None


def set_openrouter_trace_from_header(
    raw_header: Optional[str],
) -> Token[Optional[dict[str, Any]]]:
    if not raw_header:
        return _openrouter_trace_context.set(None)

    try:
        parsed = json.loads(raw_header)
    except json.JSONDecodeError:
        return _openrouter_trace_context.set(None)

    return _openrouter_trace_context.set(_clean_trace_fields(parsed))


def reset_openrouter_trace(token: Token[Optional[dict[str, Any]]]) -> None:
    _openrouter_trace_context.reset(token)


def merge_openrouter_extra_body(
    extra_body: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    trace_fields = _openrouter_trace_context.get()
    if not trace_fields:
        return extra_body

    merged = dict(extra_body or {})
    trace = trace_fields.get("trace")
    if isinstance(trace, dict):
        existing_trace = merged.get("trace")
        if not isinstance(existing_trace, dict):
            existing_trace = {}
        merged["trace"] = {**trace, **existing_trace}

    for key in ("user", "session_id"):
        if key not in merged and key in trace_fields:
            merged[key] = trace_fields[key]

    return merged
