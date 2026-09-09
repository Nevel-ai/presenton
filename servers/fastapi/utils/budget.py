"""Private, receipt-scoped billing. No paid retries or guessed zero costs."""
from __future__ import annotations

import asyncio
import base64
from contextvars import ContextVar
import hashlib
import hmac
import json
import logging
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class PresentationBudgetError(RuntimeError):
    def __init__(self, code="budget_internal_error"):
        self.code = code if isinstance(code, str) and code in {
            "budget_exceeded", "budget_internal_error", "budget_invalid_transition",
            "budget_idempotency_conflict", "budget_reservation_not_found",
        } else "budget_internal_error"
        self.retryable = False
        super().__init__("Presentation budget unavailable; do not repeat paid work")


def credential(value):
    return isinstance(value, str) and len(value) <= 2048 and re.fullmatch(r"nbgt1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}", value) is not None


def verify_capability(token, secret, now_ms=None):
    try:
        if not credential(token) or not isinstance(secret, str) or len(secret) < 32:
            raise ValueError()
        envelope, payload, signature = token.split(".")
        key = hmac.new(secret.encode(), b"nevel/budget-bridge/v1", hashlib.sha256).digest()
        expected = base64.urlsafe_b64encode(hmac.new(key, f"{envelope}.{payload}".encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if not isinstance(claims, dict) or set(claims) != {"version", "kind", "tool", "subject", "runId", "mode", "issuedAt", "expiresAt"}:
            raise ValueError()
        if (type(claims["version"]) is not int or claims["version"] != 1 or claims["kind"] != "run"
                or claims["tool"] != "presentation" or claims["mode"] not in {"shadow", "enforce"}):
            raise ValueError()
        subject = claims["subject"]
        if (not isinstance(subject, dict) or set(subject) != {"userId", "workspaceId"}
                or any(not isinstance(v, str) or len(v) != 16 for v in subject.values())
                or not isinstance(claims["runId"], str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", claims["runId"])):
            raise ValueError()
        issued, expires = claims["issuedAt"], claims["expiresAt"]
        now = time.time() * 1000 if now_ms is None else now_ms
        if (type(issued) is not int or type(expires) is not int or issued < 0 or expires > 2**53 - 1
                or not 0 < expires - issued <= 3_600_000 or issued > now + 60_000 or now >= expires):
            raise ValueError()
        return claims
    except Exception:
        raise PresentationBudgetError("budget_invalid_transition") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BudgetCallback:
    def __init__(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/api/internal/budget/operations"
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                    and not parsed.hostname.endswith(".svc.cluster.local"))):
            raise PresentationBudgetError()
        self._url = url
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def __call__(self, token, payload):
        request = Request(self._url, method="POST", data=json.dumps(payload).encode(), headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        })
        try:
            with self._opener.open(request, timeout=10) as response:
                data = response.read(16_385)
                if len(data) > 16_384:
                    raise ValueError()
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except HTTPError as error:
            status = error.code
            error.close()
            code = "budget_exceeded" if status == 429 else "budget_invalid_transition" if status in {401, 403, 409} else "budget_internal_error"
            raise PresentationBudgetError(code) from None
        except Exception:
            raise PresentationBudgetError() from None


def _unavailable_callback(token, payload):
    raise PresentationBudgetError()


async def _drain(task):
    """Cancellation must not abandon an admission callback running in a thread."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class PresentationBudget:
    def __init__(self, token, *, callback=None, secret=None, now_ms=None):
        claims = verify_capability(token, os.environ.get("JWT_SECRET", "") if secret is None else secret, now_ms)
        self.mode = claims["mode"]
        self._token = token
        try:
            self._callback = callback if callback is not None else BudgetCallback(os.environ.get("NEVEL_BUDGET_URL", ""))
        except Exception:
            if self.mode != "shadow":
                raise PresentationBudgetError() from None
            # Mode was authenticated before this fallback. Never lose the run
            # context or accidentally turn malformed credentials into shadow.
            logging.getLogger(__name__).warning("Shadow presentation callback configuration unavailable")
            self._callback = _unavailable_callback
        self._step = 0
        self._closed = False
        self._failure = None
        self._http_client = None
        self._http_proxy = None

    def __repr__(self):
        return f"PresentationBudget(mode={self.mode!r})"

    def deny(self, code=None):
        if self._failure is None:
            self._failure = PresentationBudgetError(code).code

    def ensure_active(self):
        if self._closed or self._failure:
            raise PresentationBudgetError(self._failure or "budget_invalid_transition")

    def raise_if_denied(self):
        if self._failure:
            raise PresentationBudgetError(self._failure)

    async def aclose(self):
        self._closed = True
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def openai_http_client(self, proxy=None):
        self.ensure_active()
        if self._http_client is None:
            import httpx
            from utils.budget_openai_http import PresentationOpenAITransport
            self._http_client = httpx.AsyncClient(transport=PresentationOpenAITransport(self, proxy=proxy), trust_env=False)
            self._http_proxy = proxy
        elif proxy != self._http_proxy:
            self.deny("budget_invalid_transition")
            raise PresentationBudgetError("budget_invalid_transition")
        return self._http_client

    def _operation(self, result, operation, output):
        if result == {"kind": "bypass"}:
            return None
        if (not isinstance(result, dict) or set(result) != {"kind", "receipt", "mode", "modelId", "operation", "maxOutputTokens", "serviceTier", "maxToolCalls", "correlation"}
                or result["kind"] != "tracked_presenton_openai" or not credential(result["receipt"])
                or result["mode"] != self.mode or result["modelId"] != "gpt-5.6-luna" or result["operation"] != operation
                or result["serviceTier"] != "default" or type(result["maxToolCalls"]) is not int or result["maxToolCalls"] != 1
                or type(result["maxOutputTokens"]) is not int or not 1 <= result["maxOutputTokens"] <= output):
            raise PresentationBudgetError("budget_invalid_transition")
        return PresentationBudgetOperation(self, result)

    async def reserve_openai(self, operation, input_bytes, max_output_tokens):
        self.ensure_active()
        if (operation not in {"chat", "web_search"} or type(input_bytes) is not int or not 1 <= input_bytes <= 922_000
                or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 32_000 or self._step > 511):
            self.deny("budget_invalid_transition")
            raise PresentationBudgetError("budget_invalid_transition")
        return await self._reserve({
            "action": "reserve_presenton_openai", "modelId": "gpt-5.6-luna",
            "operation": operation, "inputBytes": input_bytes, "maxOutputTokens": max_output_tokens,
        }, lambda result: self._operation(result, operation, max_output_tokens))

    async def reserve_ideogram(self):
        from utils.budget_ideogram import IDEOGRAM_PROFILE, IdeogramBudgetOperation

        def parse(result):
            if result == {"kind": "bypass"}:
                return None
            if (not isinstance(result, dict) or set(result) != {"kind", "receipt", "mode", "profile", "correlation"}
                    or result["kind"] != "tracked_presenton_ideogram" or not credential(result["receipt"])
                    or result["mode"] != self.mode or result["profile"] != IDEOGRAM_PROFILE
                    or type(result["profile"].get("numImages")) is not int):
                raise PresentationBudgetError("budget_invalid_transition")
            return IdeogramBudgetOperation(self, result)

        return await self._reserve({"action": "reserve_presenton_ideogram", "profile": dict(IDEOGRAM_PROFILE)}, parse)

    async def _reserve(self, payload, parse):
        self.ensure_active()
        if self._step > 511:
            self.deny("budget_invalid_transition")
            raise PresentationBudgetError("budget_invalid_transition")
        step = self._step
        self._step += 1  # Shared event-loop counter across model/image calls.
        task = asyncio.create_task(asyncio.to_thread(self._callback, self._token, {**payload, "step": step, "correlationVersion": 1}))
        try:
            admitted = parse(await asyncio.shield(task))
            try:
                self.ensure_active()
            except PresentationBudgetError:
                if admitted is not None:
                    await admitted.release_unstarted()
                raise
            return admitted
        except asyncio.CancelledError:
            self.deny()
            try:
                admitted = parse(await _drain(task))
                if admitted is not None:
                    await _drain(asyncio.create_task(admitted.release_unstarted()))
            except Exception:
                logging.getLogger(__name__).error("Cancelled presentation admission remains pending reconciliation")
            raise
        except Exception as error:
            code = error.code if isinstance(error, PresentationBudgetError) else "budget_internal_error"
            if self.mode == "shadow" and code == "budget_internal_error":
                logging.getLogger(__name__).warning("Shadow presentation admission unavailable")
                return None
            self.deny(code)
            raise PresentationBudgetError(code) from None


class PresentationBudgetOperation:
    def __init__(self, run, admission):
        pair = admission.get("correlation")
        if (not isinstance(pair, dict) or set(pair) != {"budget_operation_id", "budget_reservation_id"}
                or not isinstance(pair.get("budget_operation_id"), str) or len(pair["budget_operation_id"]) > 256
                or re.fullmatch(r"presentation:[A-Za-z0-9_:-]+:[0-9]+", pair["budget_operation_id"]) is None
                or not isinstance(pair.get("budget_reservation_id"), str)
                or re.fullmatch(r"[A-Za-z0-9_-]{16}", pair["budget_reservation_id"]) is None):
            raise PresentationBudgetError("budget_invalid_transition")
        self.correlation = dict(pair)
        self._run = run
        self._receipt = admission["receipt"]
        self.operation = admission["operation"]
        self.max_output_tokens = admission["maxOutputTokens"]
        self._state = "reserved"
        self._provider_id = None
        self._observed = False
        self._finalized = None
        self._lock = asyncio.Lock()

    def __repr__(self):
        return f"PresentationBudgetOperation(operation={self.operation!r})"

    def _fail(self, code="budget_invalid_transition"):
        self._run.deny(code)
        raise PresentationBudgetError(code)

    def mark_started(self):
        self._run.ensure_active()
        if self._state != "reserved":
            self._fail()
        self._state = "started"

    async def _send(self, payload):
        task = asyncio.create_task(asyncio.to_thread(self._run._callback, self._receipt, payload))
        try:
            if await asyncio.shield(task) != {"kind": "acknowledged"}:
                raise PresentationBudgetError()
            return True
        except asyncio.CancelledError:
            self._run.deny()
            try:
                await _drain(task)
            except Exception:
                logging.getLogger(__name__).error("Cancelled presentation settlement remains pending reconciliation")
            raise
        except Exception as error:
            code = error.code if isinstance(error, PresentationBudgetError) else "budget_internal_error"
            if self._run.mode == "shadow" and code == "budget_internal_error":
                logging.getLogger(__name__).warning("Shadow presentation settlement unavailable")
                return False
            self._run.deny(code)
            raise PresentationBudgetError(code) from None

    async def release_unstarted(self):
        async with self._lock:
            if self._state == "released":
                return
            if self._state != "reserved" or self._provider_id is not None or self._finalized is not None:
                self._fail()
            self._state = "releasing"
            if await self._send({"action": "release", "reason": "provider_not_called"}):
                self._state = "released"

    async def observe(self, provider_id):
        async with self._lock:
            await self._observe(provider_id)

    async def _observe(self, provider_id):
        prefix = "chatcmpl-" if self.operation == "chat" else "ideogram:" if self.operation == "ideogram" else "resp_"
        if (self._state != "started" or not isinstance(provider_id, str) or len(provider_id) > 256
                or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+", provider_id)):
            self._fail()
        if self._provider_id is not None and self._provider_id != provider_id:
            self._fail("budget_idempotency_conflict")
        self._provider_id = provider_id  # Retain even if callback fails.
        if not self._observed and await self._send({"action": "observe", "providerUsageId": provider_id}):
            self._observed = True

    async def finalize_openai(self, provider_id, usage):
        async with self._lock:
            await self._observe(provider_id)
            expected = {"modelId", "serviceTier", "inputTokens", "cachedTokens", "cacheWriteTokens", "outputTokens", "searchCalls"}
            if (not isinstance(usage, dict) or set(usage) != expected or usage["modelId"] != "gpt-5.6-luna" or usage["serviceTier"] != "default"
                    or any(type(usage[key]) is not int or not 0 <= usage[key] <= maximum for key, maximum in {
                        "inputTokens": 922_000, "cachedTokens": 922_000, "cacheWriteTokens": 922_000, "outputTokens": 128_000, "searchCalls": 1,
                    }.items()) or usage["cachedTokens"] + usage["cacheWriteTokens"] > usage["inputTokens"]
                    or (self.operation == "chat" and usage["searchCalls"] != 0)):
                self._run.deny("budget_invalid_transition")
                raise PresentationBudgetError("budget_invalid_transition")
            if self._finalized is not None:
                if self._finalized != usage:
                    self._fail("budget_idempotency_conflict")
                return
            if await self._send({"action": "finalize_presenton_openai", "providerUsageId": provider_id, "usage": usage}):
                self._finalized = dict(usage)


current_presentation_budget: ContextVar[PresentationBudget | None] = ContextVar("presentation_budget", default=None)
