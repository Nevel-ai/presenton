from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from enums.llm_provider import LLMProvider
from utils.get_env import get_can_change_keys_env
from utils.llm_provider import get_llm_provider
from utils.openrouter_trace_context import (
    NEVEL_OPENROUTER_TRACE_HEADER,
    reset_openrouter_trace,
    set_openrouter_trace_from_header,
)
from utils.user_config import update_env_with_user_config


class OpenRouterTraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # trace/session_id/user are OpenRouter-specific extra_body fields.
        # Only LLMProvider.CUSTOM is ever wired to point at OpenRouter in this
        # codebase — OPENAI/OLLAMA/etc. hit their own fixed endpoints, which
        # reject unrecognized top-level body fields (e.g. real api.openai.com
        # 400s with "Unrecognized request arguments supplied: session_id, trace").
        header_value = request.headers.get(NEVEL_OPENROUTER_TRACE_HEADER)
        if header_value:
            try:
                is_custom_provider = get_llm_provider() == LLMProvider.CUSTOM
            except Exception:
                is_custom_provider = False
            if not is_custom_provider:
                header_value = None

        token = set_openrouter_trace_from_header(header_value)
        try:
            return await call_next(request)
        finally:
            reset_openrouter_trace(token)


class UserConfigEnvUpdateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if get_can_change_keys_env() != "false":
            update_env_with_user_config()
        return await call_next(request)
