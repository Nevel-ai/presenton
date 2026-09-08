"""Offline native image billing tests; no image/provider connections."""
import asyncio
import unittest

import httpx

from test_budget_client import fixture, RECEIPT, admission
from utils.budget import PresentationBudgetError
from utils.budget_ideogram import IDEOGRAM_PROFILE, generate_budgeted_ideogram


IMAGE_URL = "https://ideogram.ai/api/images/ephemeral/offline-image.png?exp=123&sig=private-signature"


class IdeogramTests(unittest.IsolatedAsyncioTestCase):
    def run_fixture(self, mode="enforce", reject=None):
        calls = []

        def callback(token, payload):
            calls.append(payload)
            if reject:
                raise PresentationBudgetError(reject)
            if payload["action"] == "reserve_presenton_ideogram":
                return {"kind": "tracked_presenton_ideogram", "receipt": RECEIPT, "mode": mode, "profile": dict(IDEOGRAM_PROFILE)}
            if payload["action"] == "reserve_presenton_openai":
                return admission(payload, mode)
            return {"kind": "acknowledged"}

        return fixture(mode, callback)[0], calls

    def client(self, handler):
        native = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        self.addAsyncCleanup(native.aclose)
        return native

    async def test_admits_before_http_and_settles_before_returning_download_url(self):
        run, calls = self.run_fixture()
        requests = []

        def native(request):
            self.assertEqual(calls[0]["action"], "reserve_presenton_ideogram")
            requests.append(request)
            self.assertIn("multipart/form-data", request.headers["content-type"])
            self.assertIn(b'name="num_images"', request.content)
            self.assertNotIn(RECEIPT.encode(), request.content)
            self.assertFalse(any(name.startswith("x-nevel-budget-") for name in request.headers))
            return httpx.Response(200, json={"data": [{"url": IMAGE_URL, "is_image_safe": True}]})

        result = await generate_budgeted_ideogram(run, "offline prompt", "offline-key", client=self.client(native))
        self.assertEqual(result, IMAGE_URL)
        self.assertEqual(len(requests), 1)
        self.assertEqual([p["action"] for p in calls], ["reserve_presenton_ideogram", "observe", "finalize_presenton_ideogram"])
        self.assertEqual(calls[-1]["usage"]["generatedImages"], 1)
        self.assertNotIn("private-signature", str(calls))
        run.ensure_active()

    async def test_rejection_does_not_call_native_provider(self):
        run, calls = self.run_fixture(reject="budget_exceeded")

        def native(_):
            self.fail("Provider called without admission")

        with self.assertRaises(PresentationBudgetError) as error:
            await generate_budgeted_ideogram(run, "offline", "offline", client=self.client(native))
        self.assertEqual(error.exception.code, "budget_exceeded")
        self.assertEqual(len(calls), 1)

    async def test_unknown_response_keeps_hold_without_retry_or_release(self):
        for data in [{}, {"data": []}, {"data": [{"url": "https://other.invalid/image.png"}]}, {"data": [None]},
                     {"data": [{"url": IMAGE_URL}, {"url": IMAGE_URL}]}]:
            with self.subTest(data=data):
                run, calls = self.run_fixture()
                requests = []

                def native(request):
                    requests.append(request)
                    return httpx.Response(200, json=data)

                with self.assertRaises(PresentationBudgetError):
                    await generate_budgeted_ideogram(run, "offline", "offline", client=self.client(native))
                self.assertEqual(len(requests), 1)
                self.assertEqual([p["action"] for p in calls], ["reserve_presenton_ideogram"])
                with self.assertRaises(PresentationBudgetError):
                    run.ensure_active()

    async def test_generated_but_unsafe_image_is_charged_without_delivery(self):
        run, calls = self.run_fixture()
        with self.assertRaises(PresentationBudgetError):
            await generate_budgeted_ideogram(run, "offline", "offline", client=self.client(lambda _: httpx.Response(200, json={"data": [{"url": IMAGE_URL, "is_image_safe": False}]})))
        self.assertEqual(calls[-1]["action"], "finalize_presenton_ideogram")

    async def test_model_and_image_admissions_share_unique_steps(self):
        run, calls = self.run_fixture()
        await asyncio.gather(run.reserve_openai("chat", 100, 10), run.reserve_ideogram(), run.reserve_ideogram())
        self.assertEqual(sorted(p["step"] for p in calls), [0, 1, 2])

    async def test_cancelled_native_request_is_never_released(self):
        run, calls = self.run_fixture()
        entered = asyncio.Event()

        async def native(_):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(generate_budgeted_ideogram(run, "offline", "offline", client=self.client(native)))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([p["action"] for p in calls], ["reserve_presenton_ideogram"])
        with self.assertRaises(PresentationBudgetError):
            run.ensure_active()

    async def test_image_receipt_cannot_change_identity(self):
        run, calls = self.run_fixture()
        operation = await run.reserve_ideogram()
        operation.mark_started()
        await operation.finalize_image("ideogram:one")
        await operation.finalize_image("ideogram:one")
        self.assertEqual(sum(p["action"] == "finalize_presenton_ideogram" for p in calls), 1)
        with self.assertRaises(PresentationBudgetError):
            await operation.finalize_image("ideogram:two")

    async def test_image_receipt_cannot_settle_as_openai(self):
        run, calls = self.run_fixture()
        operation = await run.reserve_ideogram()
        operation.mark_started()
        with self.assertRaises(PresentationBudgetError):
            await operation.finalize_openai("ideogram:one", {})
        self.assertEqual(len(calls), 1)

    async def test_shadow_preserves_legacy_url_without_inventing_evidence(self):
        run, calls = self.run_fixture("shadow")
        legacy_url = "https://cdn.example.invalid/legacy-image.png"
        result = await generate_budgeted_ideogram(run, "offline", "offline", client=self.client(lambda _: httpx.Response(200, json={"data": [{"image_url": legacy_url}]})))
        self.assertEqual(result, legacy_url)
        self.assertEqual([p["action"] for p in calls], ["reserve_presenton_ideogram"])
        run.ensure_active()

    async def test_oversized_native_response_is_closed_and_keeps_hold(self):
        run, calls = self.run_fixture()
        closed = asyncio.Event()

        class Oversized(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * 262_145
                raise AssertionError("Must stop reading at limit")

            async def aclose(self):
                closed.set()

        with self.assertRaises(PresentationBudgetError):
            await generate_budgeted_ideogram(run, "offline", "offline", client=self.client(lambda _: httpx.Response(200, stream=Oversized())))
        self.assertTrue(closed.is_set())
        self.assertEqual([p["action"] for p in calls], ["reserve_presenton_ideogram"])
