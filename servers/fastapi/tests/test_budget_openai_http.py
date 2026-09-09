"""Native SDK boundary tests; all HTTP is handled by an in-memory transport."""
import asyncio
import json
import unittest

import httpx
from openai import AsyncOpenAI

from test_budget_client import fixture
from utils.budget import PresentationBudgetError
from utils.budget_openai_http import PresentationOpenAITransport


def completion():
    return {"id": "chatcmpl-offline", "object": "chat.completion", "created": 1,
            "model": "gpt-5.6-luna", "service_tier": "default",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                      "prompt_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 30}}}


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_metadata_preserves_custom_fields_without_receipts_or_enforce_changes_in_shadow(self):
        for mode in ["shadow", "enforce"]:
            run, calls, requests, client = await self.setup_client(completion(), mode)
            body = {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 50, "metadata": {"tag": "existing", "budget_operation_id": "stale"}}
            await asyncio.gather(*[client.post("https://api.openai.com/v1/chat/completions", json=body) for _ in range(2)])
            sent = [json.loads(request.content) for request in requests]
            self.assertEqual({b["metadata"]["budget_operation_id"] for b in sent},
                             {"presentation:execution:build:0", "presentation:execution:build:1"})
            for native in sent:
                self.assertEqual(native["metadata"]["tag"], "existing")
                self.assertEqual(native["metadata"]["budget_reservation_id"], "reservation00001")
                self.assertNotIn("nbgt1.", json.dumps(native))
                if mode == "shadow":
                    self.assertEqual(native["max_tokens"], 50)
                    self.assertNotIn("service_tier", native)
            self.assertEqual(body["metadata"]["budget_operation_id"], "stale")
            self.assertTrue(all(payload["correlationVersion"] == 1 for _, payload in calls if payload["action"].startswith("reserve")))
            run.ensure_active()

    async def test_metadata_overflow_and_invalid_values_fail_before_reservation(self):
        for metadata in [{str(i): "x" for i in range(15)}, {"k": "x" * 513}, {"x" * 65: "v"}, {"k": 1}, []]:
            _, calls, requests, client = await self.setup_client(completion())
            with self.assertRaises(PresentationBudgetError):
                await client.post("https://api.openai.com/v1/chat/completions", json={
                    "model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}], "metadata": metadata})
            self.assertEqual(calls, [])
            self.assertEqual(requests, [])

    async def test_fourteen_metadata_entries_leave_exactly_two_safe_correlation_slots(self):
        _, _, requests, client = await self.setup_client(completion())
        metadata = {str(i): "x" * 512 for i in range(14)}
        await client.post("https://api.openai.com/v1/chat/completions", json={
            "model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}], "metadata": metadata})
        sent = json.loads(requests[0].content)["metadata"]
        self.assertEqual(len(sent), 16)
        self.assertTrue(all(sent[k] == v for k, v in metadata.items()))

    async def setup_client(self, response, mode="enforce"):
        run, calls = fixture(mode)
        requests = []

        async def native(request):
            requests.append(request)
            return response(request) if callable(response) else httpx.Response(200, json=response)

        inner = httpx.AsyncClient(transport=httpx.MockTransport(native), trust_env=False)
        outer = httpx.AsyncClient(transport=PresentationOpenAITransport(run, native=inner), trust_env=False)
        self.addAsyncCleanup(outer.aclose)
        return run, calls, requests, outer

    async def test_real_sdk_json_settles_native_usage_before_return(self):
        run, calls, requests, client = await self.setup_client(completion())
        sdk = AsyncOpenAI(api_key="offline-only", http_client=client, max_retries=0)
        result = await sdk.chat.completions.create(model="gpt-5.6-luna", messages=[{"role": "user", "content": "hello"}])
        self.assertEqual(result.choices[0].message.content, "ok")
        self.assertEqual([p["action"] for _, p in calls], ["reserve_presenton_openai", "observe", "finalize_presenton_openai"])
        self.assertEqual(calls[-1][1]["usage"]["cacheWriteTokens"], 30)
        body = json.loads(requests[0].content)
        self.assertEqual(body["max_completion_tokens"], 4000)
        self.assertEqual(body["service_tier"], "default")
        self.assertFalse(any(k.startswith("x-nevel-budget-") for k in requests[0].headers))
        run.ensure_active()

    async def test_sdk_sse_requests_usage_and_settles_terminal_chunk(self):
        data = completion()
        data["object"] = "chat.completion.chunk"
        data["choices"] = []
        wire = b"data: " + json.dumps(data).encode() + b"\n\ndata: [DONE]\n\n"
        run, calls, requests, client = await self.setup_client(lambda _: httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"}))
        sdk = AsyncOpenAI(api_key="offline-only", http_client=client, max_retries=0)
        stream = await sdk.chat.completions.create(model="gpt-5.6-luna", messages=[{"role": "user", "content": "hello"}], stream=True)
        chunks = [chunk async for chunk in stream]
        self.assertEqual(len(chunks), 1)
        self.assertEqual(calls[-1][1]["action"], "finalize_presenton_openai")
        self.assertTrue(json.loads(requests[0].content)["stream_options"]["include_usage"])
        run.ensure_active()

    async def test_missing_cache_write_retains_identity_and_blocks_new_work(self):
        data = completion()
        del data["usage"]["prompt_tokens_details"]["cache_write_tokens"]
        run, calls, requests, client = await self.setup_client(data)
        with self.assertRaises(PresentationBudgetError):
            await client.post("https://api.openai.com/v1/chat/completions", json={"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual([p["action"] for _, p in calls], ["reserve_presenton_openai", "observe"])
        with self.assertRaises(PresentationBudgetError):
            run.ensure_active()
        self.assertEqual(len(requests), 1)

    async def test_shadow_missing_usage_preserves_original_json(self):
        data = completion()
        del data["usage"]
        run, calls, _, client = await self.setup_client(data, "shadow")
        response = await client.post("https://api.openai.com/v1/chat/completions", json={"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.json(), data)
        self.assertEqual([p["action"] for _, p in calls], ["reserve_presenton_openai", "observe"])
        run.ensure_active()

    async def test_unsupported_request_rejected_before_reservation_or_http(self):
        for update in [{"model": "other"}, {"n": True}, {"max_tokens": 0}, {"stream": "yes"}, {"service_tier": "priority"},
                       {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/image"}}]}]}]:
            with self.subTest(update=update):
                _, calls, requests, client = await self.setup_client(completion())
                with self.assertRaises(PresentationBudgetError):
                    await client.post("https://api.openai.com/v1/chat/completions", json={"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}], **update})
                self.assertEqual(calls, [])
                self.assertEqual(requests, [])

    async def test_native_error_is_not_released_or_retried(self):
        run, calls, requests, client = await self.setup_client(lambda _: httpx.Response(503, text="private provider detail"))
        with self.assertRaises(PresentationBudgetError) as error:
            await client.post("https://api.openai.com/v1/chat/completions", json={"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}]})
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(len(requests), 1)
        self.assertEqual([p["action"] for _, p in calls], ["reserve_presenton_openai"])
        with self.assertRaises(PresentationBudgetError):
            run.ensure_active()

    async def test_shadow_native_error_preserves_provider_response_without_refund(self):
        run, calls, requests, client = await self.setup_client(lambda _: httpx.Response(429, json={"error": "rate limited"}), "shadow")
        response = await client.post("https://api.openai.com/v1/chat/completions", json={"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"error": "rate limited"})
        self.assertEqual(len(requests), 1)
        self.assertEqual([p["action"] for _, p in calls], ["reserve_presenton_openai"])
        run.ensure_active()

    async def test_hosted_search_settles_separate_native_call_count(self):
        data = {"id": "resp_offline", "model": "gpt-5.6-luna", "service_tier": "default", "status": "completed",
                "output": [{"type": "web_search_call", "status": "completed"}],
                "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                          "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}}
        _, calls, requests, client = await self.setup_client(data)
        response = await client.post("https://api.openai.com/v1/responses", json={"model": "gpt-5.6-luna", "input": "offline query", "tools": [{"type": "web_search_preview"}]})
        self.assertEqual(response.json(), data)
        self.assertEqual(calls[0][1]["operation"], "web_search")
        self.assertEqual(calls[-1][1]["usage"]["searchCalls"], 1)
        self.assertEqual(json.loads(requests[0].content)["max_tool_calls"], 1)
        self.assertEqual(json.loads(requests[0].content)["metadata"], {
            "budget_operation_id": "presentation:execution:build:0", "budget_reservation_id": "reservation00001"})

    async def test_truncated_stream_retains_hold_and_denies_new_work(self):
        data = completion()
        del data["usage"]
        wire = b"data: " + json.dumps(data).encode() + b"\n\n"
        run, calls, _, client = await self.setup_client(lambda _: httpx.Response(200, content=wire))
        with self.assertRaises(PresentationBudgetError):
            await client.post("https://api.openai.com/v1/chat/completions", json={"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}], "stream": True})
        self.assertEqual([p["action"] for _, p in calls], ["reserve_presenton_openai", "observe"])
        with self.assertRaises(PresentationBudgetError):
            run.ensure_active()

    async def test_conflicting_native_usage_is_never_silently_overwritten(self):
        first = completion()
        second = completion()
        second["usage"]["completion_tokens"] = 1
        second["usage"]["total_tokens"] = 101
        wire = b"".join(b"data: " + json.dumps(data).encode() + b"\n\n" for data in [first, second]) + b"data: [DONE]\n\n"
        run, calls, _, client = await self.setup_client(lambda _: httpx.Response(200, content=wire))
        with self.assertRaises(PresentationBudgetError) as error:
            await client.post("https://api.openai.com/v1/chat/completions", json={"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}], "stream": True})
        self.assertEqual(error.exception.code, "budget_idempotency_conflict")
        self.assertEqual([p["action"] for _, p in calls], ["reserve_presenton_openai", "observe"])
        with self.assertRaises(PresentationBudgetError):
            run.ensure_active()

    async def test_cancellation_after_send_keeps_hold_and_closes_native_stream(self):
        entered = asyncio.Event()
        closed = asyncio.Event()

        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                data = completion()
                del data["usage"]
                yield b"data: " + json.dumps(data).encode() + b"\n\n"
                entered.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        run, calls, requests, client = await self.setup_client(lambda _: httpx.Response(200, stream=WaitingStream()))
        task = asyncio.create_task(client.post("https://api.openai.com/v1/chat/completions", json={"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hello"}], "stream": True}))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.assertEqual(len(requests), 1)
        self.assertEqual([p["action"] for _, p in calls], ["reserve_presenton_openai", "observe"])
        with self.assertRaises(PresentationBudgetError):
            run.ensure_active()

    async def test_closed_request_pool_cannot_be_reused(self):
        run, _ = fixture()
        first = run.openai_http_client()
        self.assertIs(first, run.openai_http_client())
        await run.aclose()
        self.assertTrue(first.is_closed)
        with self.assertRaises(PresentationBudgetError):
            run.openai_http_client()
