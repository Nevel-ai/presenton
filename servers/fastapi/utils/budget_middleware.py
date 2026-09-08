"""ASGI lifetime for private budget credentials (including streamed responses)."""
import asyncio
import json
import re

from utils.budget import PresentationBudget, PresentationBudgetError, current_presentation_budget
from utils.budget_profile import assert_budget_profile


class PresentationBudgetMiddleware:
    def __init__(self, app, budget_factory=PresentationBudget):
        self.app = app
        self.budget_factory = budget_factory

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        values = [value for name, value in scope.get("headers", []) if name.lower() == b"x-nevel-budget-capability"]
        if not values:
            return await self.app(scope, receive, send)

        run = None
        token = None
        started = False

        async def scoped_send(message):
            nonlocal started
            run.raise_if_denied()
            if message["type"] == "http.response.start":
                headers = [(name, value) for name, value in message.get("headers", [])
                           if name.lower() not in {b"x-nevel-budget-version", b"x-nevel-budget-error", b"cache-control"}]
                message = {**message, "headers": [*headers, (b"x-nevel-budget-version", b"1"), (b"cache-control", b"no-store")]}
                started = True
            await send(message)

        try:
            if len(values) != 1 or current_presentation_budget.get() is not None:
                raise PresentationBudgetError("budget_invalid_transition")
            # Only current synchronous B2C entry points are budget-enabled.
            # In particular, never acknowledge an unowned /generate/async job.
            path = scope.get("path", "")
            if path.startswith("/api/v1/budgeted-ppt/"):
                path = "/api/v1/ppt/" + path[len("/api/v1/budgeted-ppt/"):]
            method = scope.get("method", "")
            paid_get = method == "GET" and re.fullmatch(r"/api/v1/ppt/(?:outlines/create|presentation/build)/[A-Za-z0-9-]+", path)
            paid_post = method == "POST" and path in {"/api/v1/ppt/presentation/prepare", "/api/v1/ppt/slide/create", "/api/v1/ppt/slide/edit"}
            if not (paid_get or paid_post):
                raise PresentationBudgetError("budget_invalid_transition")
            try:
                credential = values[0].decode("ascii")
            except (UnicodeError, AttributeError):
                raise PresentationBudgetError("budget_invalid_transition") from None
            run = self.budget_factory(credential)
            assert_budget_profile(run)
            token = current_presentation_budget.set(run)
            # Headers are removed before routing/logging/provider parameter handling.
            private_scope = {**scope, "path": path, "raw_path": path.encode("utf-8"), "headers": [(name, value) for name, value in scope.get("headers", [])
                                                  if not name.lower().startswith(b"x-nevel-budget-")]}
            await self.app(private_scope, receive, scoped_send)
            run.raise_if_denied()
        except asyncio.CancelledError:
            if run is not None:
                run.deny()
            raise
        except Exception as error:
            code = error.code if isinstance(error, PresentationBudgetError) else "budget_internal_error"
            if run is not None:
                try:
                    run.raise_if_denied()
                except PresentationBudgetError as denied:
                    code = denied.code
            if started:
                # Cannot replace committed response headers. Abort the stream;
                # the TS client treats incomplete output as nonretryable unknown.
                raise PresentationBudgetError(code) from None
            status = 429 if code == "budget_exceeded" else 409 if code in {"budget_invalid_transition", "budget_idempotency_conflict"} else 503
            body = json.dumps({"code": code, "message": "Presentation budget operation unavailable"}).encode()
            await send({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                (b"x-nevel-budget-version", b"1"), (b"x-nevel-budget-error", code.encode()),
            ]})
            await send({"type": "http.response.body", "body": body})
        finally:
            try:
                if run is not None:
                    await run.aclose()
            finally:
                if token is not None:
                    current_presentation_budget.reset(token)
