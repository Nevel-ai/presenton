"""Real FastAPI/Starlette middleware lifetime; no database or native providers."""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from api.middlewares import OpenRouterTraceMiddleware, UserConfigEnvUpdateMiddleware
from test_budget_client import capability, fixture
from utils.budget import PresentationBudgetError, current_presentation_budget
from utils.budget_middleware import PresentationBudgetMiddleware


class StackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"CAN_CHANGE_KEYS": "false", "LLM": "openai", "OPENAI_MODEL": "gpt-5.6-luna", "IMAGE_PROVIDER": "ideogram"})
        env.start()
        self.addCleanup(env.stop)

    def app(self, run):
        app = FastAPI()
        app.add_middleware(UserConfigEnvUpdateMiddleware)
        app.add_middleware(OpenRouterTraceMiddleware)
        app.add_middleware(PresentationBudgetMiddleware, budget_factory=lambda token: run)
        return app

    async def test_stream_and_background_keep_scope_until_request_closes(self):
        run, _ = fixture()
        stages = []
        app = self.app(run)

        @app.get("/api/v1/ppt/presentation/build/offline")
        async def route(request: Request):
            self.assertNotIn("x-nevel-budget-capability", request.headers)
            stages.append(current_presentation_budget.get())

            async def stream():
                await asyncio.sleep(0)
                stages.append(current_presentation_budget.get())
                yield b"done"

            async def background():
                stages.append(current_presentation_budget.get())
                current_presentation_budget.get().ensure_active()

            return StreamingResponse(stream(), background=BackgroundTask(background))

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://offline") as client:
            response = await client.get("/api/v1/ppt/presentation/build/offline", headers={"x-nevel-budget-capability": capability()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "done")
        self.assertEqual(response.headers["x-nevel-budget-version"], "1")
        self.assertEqual(stages, [run, run, run])
        self.assertIsNone(current_presentation_budget.get())
        with self.assertRaises(PresentationBudgetError):
            run.ensure_active()

    async def test_config_mutation_is_not_entered_on_enforce_drift(self):
        run, _ = fixture()
        app = self.app(run)
        with patch.dict("os.environ", {"CAN_CHANGE_KEYS": "true"}), patch("api.middlewares.update_env_with_user_config") as update:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://offline") as client:
                response = await client.get("/api/v1/ppt/presentation/build/offline", headers={"x-nevel-budget-capability": capability()})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.headers["x-nevel-budget-error"], "budget_invalid_transition")
            update.assert_not_called()
