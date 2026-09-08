"""Admission and native usage capture below the OpenAI SDK response parser."""
import asyncio
import json
import logging

import httpx

from utils.budget import PresentationBudgetError


def _invalid():
    raise PresentationBudgetError("budget_invalid_transition")


def prepare_request(request, raw):
    if (request.method != "POST" or request.url.scheme != "https" or request.url.host != "api.openai.com"
            or request.url.port not in {None, 443} or request.url.query or request.url.fragment
            or request.url.username or request.url.password or len(raw) + 512 > 922_000
            or any(name.lower().startswith("x-nevel-budget-") for name in request.headers)):
        _invalid()
    if request.url.path == "/v1/chat/completions":
        operation = "chat"
    elif request.url.path == "/v1/responses":
        operation = "web_search"
    else:
        _invalid()
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get("model") != "gpt-5.6-luna":
        _invalid()
    common = {"model", "user", "metadata", "service_tier", "temperature", "top_p", "stream", "tools", "tool_choice",
              "parallel_tool_calls", "store", "prompt_cache_key", "prompt_cache_options", "prompt_cache_retention"}
    allowed = common | ({"messages", "max_tokens", "max_completion_tokens", "stream_options", "response_format", "reasoning_effort",
                         "verbosity", "presence_penalty", "frequency_penalty", "stop", "seed", "n", "logprobs", "top_logprobs"}
                        if operation == "chat" else {"input", "max_output_tokens", "max_tool_calls", "reasoning", "text"})
    if set(body) - allowed or body.get("service_tier") not in {None, "auto", "default"}:
        _invalid()
    if operation == "chat":
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
            _invalid()
        for message in messages:
            if not isinstance(message, dict):
                _invalid()
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                if not isinstance(content, list) or any(not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str) for part in content):
                    _invalid()
        tools = body.get("tools") or []
        if not isinstance(tools, list) or any(not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools):
            _invalid()
        requested = body.get("max_completion_tokens")
        if requested is None:
            requested = body.get("max_tokens")
    else:
        if (not isinstance(body.get("input"), str) or not body["input"] or body.get("stream")
                or body.get("tools") != [{"type": "web_search_preview"}]):
            _invalid()
        requested = body.get("max_output_tokens")
    if requested is None:
        requested = 4000
    if body.get("stream") is not None and type(body["stream"]) is not bool:
        _invalid()
    if type(requested) is not int or requested <= 0:
        _invalid()
    return operation, body, min(requested, 32_000)


class NativeUsage:
    def __init__(self, operation):
        self.operation = operation
        self.identity = None
        self.usage = None
        self.done = False

    async def value(self, data):
        if not isinstance(data, dict):
            _invalid()
        identity = data.get("id")
        if isinstance(identity, str):
            await self.operation.observe(identity)
            self.identity = identity
        usage = data.get("usage")
        if usage is None:
            return
        if not isinstance(usage, dict):
            _invalid()
        chat = self.operation.operation == "chat"
        prefix = "prompt" if chat else "input"
        input_tokens = usage.get(f"{prefix}_tokens")
        output_tokens = usage.get("completion_tokens" if chat else "output_tokens")
        if (type(input_tokens) is not int or type(output_tokens) is not int or type(usage.get("total_tokens")) is not int
                or input_tokens + output_tokens != usage["total_tokens"]):
            _invalid()
        details = usage.get(f"{prefix}_tokens_details")
        if not isinstance(details, dict):
            _invalid()
        searches = 0
        if not chat:
            if data.get("status") not in {"completed", "incomplete", "failed", "cancelled"} or not isinstance(data.get("output"), list):
                _invalid()
            for item in data["output"]:
                if not isinstance(item, dict) or item.get("type") not in {"message", "reasoning", "web_search_call"}:
                    _invalid()
                if item["type"] == "web_search_call":
                    if item.get("status") != "completed":
                        _invalid()
                    searches += 1
        if (data.get("model") != "gpt-5.6-luna" or data.get("service_tier") != "default"
                or any(type(details.get(field)) is not int or details[field] < 0 for field in ["cached_tokens", "cache_write_tokens"])
                or details["cached_tokens"] + details["cache_write_tokens"] > input_tokens
                or not 0 <= input_tokens <= 922_000 or not 0 <= output_tokens <= 128_000 or searches > 1):
            _invalid()
        normalized = {"modelId": data.get("model"), "serviceTier": data.get("service_tier"), "inputTokens": input_tokens,
                      "cachedTokens": details.get("cached_tokens"), "cacheWriteTokens": details.get("cache_write_tokens"),
                      "outputTokens": output_tokens, "searchCalls": searches}
        if self.usage is not None and self.usage != normalized:
            self.operation._run.deny("budget_idempotency_conflict")
            raise PresentationBudgetError("budget_idempotency_conflict")
        self.usage = normalized

    async def finish(self):
        if self.identity is None or self.usage is None:
            _invalid()
        await self.operation.finalize_openai(self.identity, self.usage)
        self.done = True


class UsageStream(httpx.AsyncByteStream):
    def __init__(self, response, operation, run, sse):
        self.response = response
        self.evidence = NativeUsage(operation)
        self.run = run
        self.sse = sse

    async def __aiter__(self):
        buffer = b""
        parsing = True
        received = False
        try:
            async for chunk in self.response.aiter_bytes():
                if parsing:
                    try:
                        buffer += chunk
                        if len(buffer) > 4_194_304:
                            _invalid()
                        if self.sse:
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                line = line.rstrip(b"\r")
                                if line.startswith(b"data:"):
                                    value = line[5:].strip()
                                    if value == b"[DONE]":
                                        await self.evidence.finish()
                                    else:
                                        await self.evidence.value(json.loads(value))
                    except Exception:
                        if self.run.mode == "enforce":
                            raise
                        parsing = False
                        pending = buffer
                        buffer = b""
                        logging.getLogger(__name__).warning("Shadow presentation native usage unavailable")
                        if not self.sse:
                            yield pending
                            continue
                if self.sse or not parsing:
                    yield chunk
            received = True
            if parsing:
                if self.sse:
                    if not self.evidence.done:
                        if self.run.mode == "shadow":
                            logging.getLogger(__name__).warning("Shadow presentation stream has no terminal usage")
                            return
                        _invalid()
                else:
                    await self.evidence.value(json.loads(buffer))
                    await self.evidence.finish()
                    yield buffer
        except BaseException as error:
            if self.run.mode == "enforce":
                self.run.deny(error.code if isinstance(error, PresentationBudgetError) else None)
            if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                raise
            if self.run.mode == "shadow":
                # For buffered JSON, preserve the original response after a
                # settlement failure; do not invent a successful ledger write.
                if received and not self.sse and buffer:
                    logging.getLogger(__name__).warning("Shadow presentation settlement unavailable")
                    yield buffer
                    return
            raise PresentationBudgetError(error.code if isinstance(error, PresentationBudgetError) else None) from None
        finally:
            await self.response.aclose()

    async def aclose(self):
        if not self.evidence.done and self.run.mode == "enforce":
            self.run.deny()
        await self.response.aclose()


class PresentationOpenAITransport(httpx.AsyncBaseTransport):
    def __init__(self, run, proxy=None, native=None):
        self.run = run
        # Environment/proxy routing belongs to the inner client; the SDK-facing
        # client must have trust_env=False so it cannot bypass this transport.
        self.native = native if native is not None else httpx.AsyncClient(
            proxy=proxy, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"), follow_redirects=False,
        )

    async def handle_async_request(self, request):
        self.run.ensure_active()
        try:
            raw = await request.aread()
            operation, body, output = prepare_request(request, raw)
        except Exception:
            if self.run.mode == "shadow":
                logging.getLogger(__name__).warning("Shadow presentation native request is outside metered profile")
                return await self.native.send(request, stream=True)
            self.run.deny("budget_invalid_transition")
            raise PresentationBudgetError("budget_invalid_transition") from None
        admitted = await self.run.reserve_openai(operation, len(raw) + 512, output)
        if admitted is None:
            return await self.native.send(request, stream=True)
        response = None
        try:
            if self.run.mode == "enforce":
                body["service_tier"] = "default"
                if operation == "chat":
                    body.pop("max_tokens", None)
                    body["max_completion_tokens"] = admitted.max_output_tokens
                    body["n"] = 1
                else:
                    body["max_output_tokens"] = admitted.max_output_tokens
                    body["max_tool_calls"] = 1
            sse = operation == "chat" and body.get("stream") is True
            if sse:
                body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
            headers = dict(request.headers)
            headers.pop("content-length", None)
            native_request = httpx.Request(request.method, request.url, headers=headers, content=json.dumps(body).encode(), extensions=request.extensions)
            # No await between this ownership transition and native send.
            admitted.mark_started()
            response = await self.native.send(native_request, stream=True)
            if response.status_code >= 300:
                if self.run.mode == "shadow":
                    # Preserve the provider's status/body for the existing SDK
                    # policy. The started reservation stays unresolved.
                    return response
                raise PresentationBudgetError()
            headers = [(name, value) for name, value in response.headers.multi_items() if name.lower() not in {"content-encoding", "content-length"}]
            return httpx.Response(response.status_code, headers=headers, stream=UsageStream(response, admitted, self.run, sse), extensions=response.extensions)
        except BaseException as error:
            if response is not None:
                await response.aclose()
            # release_unstarted checks state; never release after native send.
            if admitted._state == "reserved":
                await admitted.release_unstarted()
            if self.run.mode == "enforce":
                self.run.deny(error.code if isinstance(error, PresentationBudgetError) else None)
            raise

    async def aclose(self):
        await self.native.aclose()
