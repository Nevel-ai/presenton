import asyncio
import unittest

from utils.budget import PresentationBudgetError, current_presentation_budget
from utils.budget_middleware import PresentationBudgetMiddleware
from test_budget_client import capability, fixture


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, app, headers=None, factory=None, path="/api/v1/ppt/presentation/build/presentation", method="GET"):
        output = []

        async def send(message):
            output.append(message)

        async def receive():
            return {"type": "http.request", "body": b""}

        await PresentationBudgetMiddleware(app, factory or (lambda token: fixture()[0]))(
            {"type": "http", "path": path, "method": method, "headers": headers if headers is not None else [(b"x-nevel-budget-capability", capability().encode())]}, receive, send)
        return output

    async def test_scope_survives_response_stream_and_awaited_background_work(self):
        stages = []
        run, _ = fixture()

        async def app(scope, receive, send):
            self.assertEqual(scope["headers"], [])
            stages.append(current_presentation_budget.get())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await asyncio.sleep(0)
            stages.append(current_presentation_budget.get())
            await send({"type": "http.response.body", "body": b"done"})
            await asyncio.sleep(0)  # ASGI background tasks are awaited by app.
            stages.append(current_presentation_budget.get())

        output = await self.invoke(app, factory=lambda token: run)
        self.assertEqual(stages, [run, run, run])
        self.assertIn((b"x-nevel-budget-version", b"1"), output[0]["headers"])
        self.assertIsNone(current_presentation_budget.get())
        with self.assertRaises(PresentationBudgetError):
            run.ensure_active()

    async def test_no_capability_preserves_unscoped_behavior(self):
        async def app(scope, receive, send):
            self.assertIsNone(current_presentation_budget.get())
            await send({"type": "http.response.start", "status": 200, "headers": []})
        output = await self.invoke(app, headers=[], factory=lambda token: self.fail("Unexpected issuance"))
        self.assertEqual(output[0]["headers"], [])

    async def test_invalid_and_duplicate_credentials_never_enter_application(self):
        async def app(*args):
            self.fail("Invalid credential reached application")

        def denied(token):
            raise PresentationBudgetError("budget_invalid_transition")

        for headers in [[(b"x-nevel-budget-capability", b"invalid")], [(b"x-nevel-budget-capability", b"one"), (b"x-nevel-budget-capability", b"two")]]:
            result = await self.invoke(app, headers, denied)
            self.assertEqual(result[0]["status"], 409)
            self.assertIsNone(current_presentation_budget.get())

    async def test_swallowed_denial_cannot_turn_into_success(self):
        async def app(scope, receive, send):
            current_presentation_budget.get().deny("budget_exceeded")
            await send({"type": "http.response.start", "status": 200, "headers": []})
        output = await self.invoke(app)
        self.assertEqual(output[0]["status"], 429)
        self.assertIn((b"x-nevel-budget-error", b"budget_exceeded"), output[0]["headers"])

    async def test_error_after_headers_aborts_stream_without_second_response(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            current_presentation_budget.get().deny("budget_exceeded")
            await send({"type": "http.response.body", "body": b"must not leak"})
        with self.assertRaises(PresentationBudgetError):
            await self.invoke(app)
        self.assertIsNone(current_presentation_budget.get())

    async def test_arbitrary_exception_is_sanitized_and_scope_is_cleaned(self):
        async def app(*args):
            raise RuntimeError("private provider payload")
        output = await self.invoke(app)
        self.assertEqual(output[0]["status"], 503)
        self.assertNotIn(b"private provider", output[1]["body"])
        self.assertIsNone(current_presentation_budget.get())

    async def test_private_capability_cannot_start_an_unowned_async_job(self):
        async def app(*args):
            self.fail("Unowned async job started")
        output = await self.invoke(app, path="/api/v1/ppt/presentation/generate/async", method="POST")
        self.assertEqual(output[0]["status"], 409)

    async def test_cancellation_closes_context_and_prevents_late_native_admission(self):
        run, calls = fixture()
        entered = asyncio.Event()

        async def app(*args):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.invoke(app, factory=lambda token: run))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(current_presentation_budget.get())
        with self.assertRaises(PresentationBudgetError):
            await run.reserve_openai("chat", 1000, 100)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
