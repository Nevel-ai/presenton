"""Offline-only tests: no provider, database, or HTTP connections."""
import asyncio
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from utils.budget import BudgetCallback, PresentationBudget, PresentationBudgetError, verify_capability


SIGNING_KEY = Ed25519PrivateKey.generate()
SECRET = SIGNING_KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
OTHER_PUBLIC_KEY = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
NOW = 1_000_000


def capability(**overrides):
    claims = dict(version=1, kind="run", tool="presentation", mode="enforce",
                  subject={"userId": "user000000000001", "workspaceId": "workspace0000001"},
                  runId="execution:build", issuedAt=NOW, expiresAt=NOW + 3_600_000)
    claims.update(overrides)
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(SIGNING_KEY.sign(f"nbgt2.{payload}".encode())).decode().rstrip("=")
    return f"nbgt2.{payload}.{signature}"


RECEIPT = "nbgt2.offline." + "s" * 86
USAGE = dict(modelId="gpt-5.6-luna", serviceTier="default", inputTokens=1000, cachedTokens=100, cacheWriteTokens=200, outputTokens=100, searchCalls=0)


def admission(payload, mode="enforce"):
    return dict(kind="tracked_presenton_openai", receipt=RECEIPT, mode=mode, modelId="gpt-5.6-luna",
                operation=payload["operation"], maxOutputTokens=payload["maxOutputTokens"], serviceTier="default", maxToolCalls=1,
                correlation={"budget_operation_id": f"presentation:execution:build:{payload.get('step', 0)}", "budget_reservation_id": "reservation00001"})


def fixture(mode="enforce", callback=None):
    calls = []

    def respond(token, payload):
        calls.append((token, payload))
        return admission(payload, mode) if payload["action"] == "reserve_presenton_openai" else {"kind": "acknowledged"}

    return PresentationBudget(capability(mode=mode), callback=callback or respond, secret=SECRET, now_ms=NOW), calls


class CapabilityTests(unittest.TestCase):
    def test_old_envelopes_private_keys_and_jwt_fallback_are_rejected(self):
        from cryptography.hazmat.primitives.serialization import PrivateFormat, NoEncryption
        private = SIGNING_KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        for token, key in [(capability().replace("nbgt2.", "nbgt1."), SECRET),
                           (capability(), private), (capability(), "offline-jwt-secret")]:
            with self.assertRaises(PresentationBudgetError):
                verify_capability(token, key, NOW)
        with patch.dict("os.environ", {"JWT_SECRET": SECRET, "NEVEL_BUDGET_PUBLIC_KEY": ""}):
            with self.assertRaises(PresentationBudgetError):
                PresentationBudget(capability(), callback=Mock(), now_ms=NOW)

    def test_validates_signature_tool_subject_time_and_mode(self):
        self.assertEqual(verify_capability(capability(), SECRET, NOW)["tool"], "presentation")
        for values in [{"tool": "deep_research"}, {"mode": "off"}, {"version": True}, {"kind": "receipt"},
                       {"subject": {}}, {"runId": "../other"}, {"issuedAt": NOW + 60_001}, {"expiresAt": NOW},
                       {"expiresAt": NOW + 3_600_001}, {"admin": True}]:
            with self.subTest(values=values), self.assertRaises(PresentationBudgetError):
                verify_capability(capability(**values), SECRET, NOW)
        with self.assertRaises(PresentationBudgetError):
            verify_capability(capability(), OTHER_PUBLIC_KEY, NOW)

    def test_callback_url_is_private_and_exact(self):
        BudgetCallback("http://ui.default.svc.cluster.local/api/internal/budget/operations")
        for url in ["http://public.example/api/internal/budget/operations", "https://user:secret@example.com/api/internal/budget/operations",
                    "https://example.com/other", "https://example.com/api/internal/budget/operations?token=secret"]:
            with self.subTest(url=url), self.assertRaises(PresentationBudgetError):
                BudgetCallback(url)

    def test_callback_is_bounded_sanitized_and_does_not_follow_redirects(self):
        callback = BudgetCallback("https://billing.example/api/internal/budget/operations")
        callback._opener = Mock()
        callback._opener.open.return_value = io.BytesIO(b'{"kind":"acknowledged"}')
        self.assertEqual(callback(RECEIPT, {"action": "observe"}), {"kind": "acknowledged"})
        request = callback._opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer " + RECEIPT)
        self.assertNotIn(RECEIPT.encode(), request.data)
        self.assertEqual(callback._opener.open.call_args.kwargs["timeout"], 10)
        for data in [b"x" * 16_385, b"[]", b"private malformed response"]:
            callback._opener.open.return_value = io.BytesIO(data)
            with self.assertRaises(PresentationBudgetError) as failure:
                callback(RECEIPT, {})
            self.assertNotIn("private", str(failure.exception))
        for status, code in [(302, "budget_internal_error"), (401, "budget_invalid_transition"), (403, "budget_invalid_transition"),
                             (409, "budget_invalid_transition"), (429, "budget_exceeded"), (500, "budget_internal_error")]:
            callback._opener.open.side_effect = HTTPError("https://billing.example", status, "private detail", {}, io.BytesIO(b"private body"))
            with self.assertRaises(PresentationBudgetError) as failure:
                callback(RECEIPT, {})
            self.assertEqual(failure.exception.code, code)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_callback_configuration_only_falls_back_after_verified_shadow_mode(self):
        with patch.dict("os.environ", {"NEVEL_BUDGET_URL": ""}):
            run = PresentationBudget(capability(mode="shadow"), secret=SECRET, now_ms=NOW)
            self.assertIsNone(await run.reserve_openai("chat", 1000, 100))
            with self.assertRaises(PresentationBudgetError):
                PresentationBudget(capability(), secret=SECRET, now_ms=NOW)
            with self.assertRaises(PresentationBudgetError):
                PresentationBudget(capability(mode="shadow"), secret=OTHER_PUBLIC_KEY, now_ms=NOW)

    async def test_reserves_unique_steps_then_observes_and_finalizes_exactly_once(self):
        run, calls = fixture()
        first, second = await asyncio.gather(run.reserve_openai("chat", 1000, 100), run.reserve_openai("web_search", 10, 100))
        self.assertEqual(sorted(p["step"] for _, p in calls), [0, 1])
        first.mark_started()
        await first.finalize_openai("chatcmpl-native", USAGE)
        await first.finalize_openai("chatcmpl-native", USAGE)
        self.assertEqual([p["action"] for _, p in calls], ["reserve_presenton_openai", "reserve_presenton_openai", "observe", "finalize_presenton_openai"])
        self.assertTrue(all(token == RECEIPT for token, _ in calls[2:]))
        self.assertNotIn(capability(), repr(run))
        self.assertNotIn(RECEIPT, repr(first))
        await second.release_unstarted()
        await second.release_unstarted()
        with self.assertRaises(PresentationBudgetError):
            second.mark_started()

    async def test_missing_usage_preserves_native_identity_without_refund_or_new_work(self):
        run, calls = fixture()
        op = await run.reserve_openai("chat", 1000, 100)
        op.mark_started()
        bad = dict(USAGE)
        del bad["cacheWriteTokens"]
        with self.assertRaises(PresentationBudgetError):
            await op.finalize_openai("chatcmpl-native", bad)
        self.assertEqual(calls[-1][1], {"action": "observe", "providerUsageId": "chatcmpl-native"})
        with self.assertRaises(PresentationBudgetError):
            await run.reserve_openai("chat", 1000, 100)
        with self.assertRaises(PresentationBudgetError):
            await op.release_unstarted()

    async def test_native_identity_and_finalized_amount_are_immutable(self):
        run, _ = fixture()
        op = await run.reserve_openai("chat", 1000, 100)
        op.mark_started()
        await op.finalize_openai("chatcmpl-native", USAGE)
        for identity, usage in [("resp_wrong", USAGE), ("chatcmpl-other", USAGE), ("chatcmpl-native", {**USAGE, "outputTokens": 200})]:
            with self.assertRaises(PresentationBudgetError):
                await op.finalize_openai(identity, usage)

    async def test_native_zero_is_explicit_not_missing(self):
        run, calls = fixture()
        op = await run.reserve_openai("chat", 1000, 100)
        op.mark_started()
        zero = {key: 0 if type(value) is int else value for key, value in USAGE.items()}
        await op.finalize_openai("chatcmpl-native", zero)
        self.assertEqual(calls[-1][1]["usage"], zero)

    async def test_shadow_availability_fallback_does_not_bypass_signed_admission_denials(self):
        callback = Mock(side_effect=PresentationBudgetError())
        run, _ = fixture("shadow", callback)
        self.assertIsNone(await run.reserve_openai("chat", 1000, 100))
        for code in ["budget_exceeded", "budget_invalid_transition", "budget_idempotency_conflict"]:
            callback.side_effect = PresentationBudgetError(code)
            run, _ = fixture("shadow", callback)
            with self.assertRaises(PresentationBudgetError):
                await run.reserve_openai("chat", 1000, 100)
            with self.assertRaises(PresentationBudgetError):
                run.ensure_active()

    async def test_cancellation_drains_late_admission_and_releases_only_unstarted_work(self):
        entered, finish = threading.Event(), threading.Event()
        calls = []

        def callback(token, payload):
            calls.append(payload)
            if payload["action"] == "reserve_presenton_openai":
                entered.set()
                if not finish.wait(2):
                    raise AssertionError("test callback not released")
                return admission(payload)
            return {"kind": "acknowledged"}

        run, _ = fixture(callback=callback)
        task = asyncio.create_task(run.reserve_openai("chat", 1000, 100))
        try:
            await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual([p["action"] for p in calls], ["reserve_presenton_openai", "release"])
        finally:
            finish.set()

    async def test_closed_run_rejects_new_calls_but_keeps_receipt_settlement(self):
        run, calls = fixture()
        op = await run.reserve_openai("chat", 1000, 100)
        op.mark_started()
        await run.aclose()
        await op.finalize_openai("chatcmpl-native", USAGE)
        with self.assertRaises(PresentationBudgetError):
            await run.reserve_openai("chat", 1000, 100)
        self.assertEqual(len(calls), 3)

    async def test_closure_during_admission_releases_late_unstarted_receipt(self):
        entered, finish = threading.Event(), threading.Event()
        calls = []

        def callback(token, payload):
            calls.append(payload)
            if payload["action"] == "reserve_presenton_openai":
                entered.set()
                if not finish.wait(2):
                    raise AssertionError("test callback not released")
                return admission(payload)
            return {"kind": "acknowledged"}

        run, _ = fixture(callback=callback)
        task = asyncio.create_task(run.reserve_openai("chat", 1000, 100))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await run.aclose()
            finish.set()
            with self.assertRaises(PresentationBudgetError):
                await task
            self.assertEqual([p["action"] for p in calls], ["reserve_presenton_openai", "release"])
        finally:
            finish.set()

    async def test_cancellation_after_native_start_never_releases_or_repeats_paid_work(self):
        entered, finish = threading.Event(), threading.Event()
        calls = []

        def callback(token, payload):
            calls.append(payload)
            if payload["action"] == "reserve_presenton_openai":
                return admission(payload)
            if payload["action"] == "finalize_presenton_openai":
                entered.set()
                if not finish.wait(2):
                    raise AssertionError("test callback not released")
            return {"kind": "acknowledged"}

        run, _ = fixture(callback=callback)
        op = await run.reserve_openai("chat", 1000, 100)
        op.mark_started()
        task = asyncio.create_task(op.finalize_openai("chatcmpl-native", USAGE))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual([p["action"] for p in calls], ["reserve_presenton_openai", "observe", "finalize_presenton_openai"])
            with self.assertRaises(PresentationBudgetError):
                await op.release_unstarted()
            with self.assertRaises(PresentationBudgetError):
                run.ensure_active()
        finally:
            finish.set()

    async def test_invalid_controls_are_rejected_before_callback(self):
        callback = Mock()
        for operation, size, output in [("other", 10, 100), ("chat", True, 100), ("chat", 922001, 100), ("chat", 10, 32001)]:
            run, _ = fixture(callback=callback)
            with self.assertRaises(PresentationBudgetError):
                await run.reserve_openai(operation, size, output)
        callback.assert_not_called()

    async def test_mode_or_endpoint_drift_in_admission_is_rejected(self):
        for changes in [{"mode": "shadow"}, {"operation": "web_search"}, {"maxToolCalls": True}, {"maxOutputTokens": 101}, {"modelId": "other"}]:
            def callback(token, payload):
                return {**admission(payload), **changes}
            run, _ = fixture(callback=callback)
            with self.assertRaises(PresentationBudgetError):
                await run.reserve_openai("chat", 1000, 100)


if __name__ == "__main__":
    unittest.main()
