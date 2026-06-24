from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from utils.get_env import get_can_change_keys_env
from utils.openrouter_trace_context import (
    NEVEL_OPENROUTER_TRACE_HEADER,
    reset_openrouter_trace,
    set_openrouter_trace_from_header,
)
from utils.user_config import update_env_with_user_config


class OpenRouterTraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = set_openrouter_trace_from_header(
            request.headers.get(NEVEL_OPENROUTER_TRACE_HEADER)
        )
        try:
            return await call_next(request)
        finally:
            reset_openrouter_trace(token)


class UserConfigEnvUpdateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if get_can_change_keys_env() != "false":
            update_env_with_user_config()
        return await call_next(request)
