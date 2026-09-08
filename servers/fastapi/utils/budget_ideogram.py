"""One native v3 Default image: reserve, generate once, settle before file IO."""
import asyncio
import hashlib
import json
import logging
import re
from urllib.parse import urlsplit

import httpx

from utils.budget import PresentationBudgetError, PresentationBudgetOperation

IDEOGRAM_PROFILE = {"modelId": "ideogram-v3", "renderingSpeed": "DEFAULT", "aspectRatio": "1x1", "numImages": 1}
IDEOGRAM_URL = "https://api.ideogram.ai/v1/ideogram-v3/generate"


class IdeogramBudgetOperation(PresentationBudgetOperation):
    def __init__(self, run, admission):
        super().__init__(run, {**admission, "operation": "ideogram", "maxOutputTokens": 0})

    async def finalize_image(self, provider_id):
        async with self._lock:
            await self._observe(provider_id)
            usage = {"modelId": "ideogram-v3", "renderingSpeed": "DEFAULT", "generatedImages": 1}
            if self._finalized is not None:
                return
            if await self._send({"action": "finalize_presenton_ideogram", "providerUsageId": provider_id, "usage": usage}):
                self._finalized = usage

    async def finalize_openai(self, provider_id, usage):
        self._fail()


def image_evidence(data):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list) or len(data["data"]) != 1:
        raise PresentationBudgetError()
    item = data["data"][0]
    if not isinstance(item, dict) or not isinstance(item.get("url"), str):
        raise PresentationBudgetError()
    url = urlsplit(item["url"])
    if (url.scheme != "https" or url.hostname != "ideogram.ai" or url.port not in {None, 443}
            or url.username or url.password or url.fragment
            or not re.fullmatch(r"/api/images/ephemeral/[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|webp)", url.path)):
        raise PresentationBudgetError()
    # Stable native image identity, never persist the expiring signed URL/query.
    identity = "ideogram:" + hashlib.sha256(url.path.encode()).hexdigest()
    return identity, item["url"], item.get("is_image_safe")


async def generate_budgeted_ideogram(run, prompt, api_key, *, client=None):
    run.ensure_active()
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 32_000 or not api_key:
        run.deny("budget_invalid_transition")
        raise PresentationBudgetError("budget_invalid_transition")
    operation = await run.reserve_ideogram()
    owned = client is None
    native = None
    response = None
    try:
        native = client if client is not None else httpx.AsyncClient(timeout=120, follow_redirects=False)
        # Multipart is the documented native endpoint contract. No file/image
        # reference fields: those carry different tariffs.
        fields = {"prompt": prompt, "rendering_speed": "DEFAULT", "aspect_ratio": "1x1", "num_images": "1"}
        request = native.build_request("POST", IDEOGRAM_URL, headers={"Api-Key": api_key}, files={k: (None, v) for k, v in fields.items()})
        if operation is not None:
            operation.mark_started()
        response = await native.send(request, stream=True)
        if response.status_code != 200:
            raise PresentationBudgetError()
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > 262_144:
                raise PresentationBudgetError()
        data = json.loads(content)
        try:
            identity, url, safe = image_evidence(data)
            if operation is not None:
                await operation.observe(identity)
                await operation.finalize_image(identity)
        except Exception:
            # Shadow cannot turn incomplete billing evidence into a new product
            # failure. Preserve the original URL fallback, but never claim cost0.
            run.raise_if_denied()
            if run.mode != "shadow":
                raise
            first = data.get("data", [None])[0] if isinstance(data, dict) and data.get("data") else None
            url = (first.get("url") or first.get("image_url") or first.get("imageUrl")) if isinstance(first, dict) else None
            if not isinstance(url, str) or not url:
                raise PresentationBudgetError() from None
            logging.getLogger(__name__).warning("Shadow presentation image evidence unavailable")
            return url
        # A generated image still incurs cost if later blocked from delivery.
        if safe is not True and run.mode == "enforce":
            raise PresentationBudgetError()
        return url
    except BaseException as error:
        if operation is not None and operation._state == "reserved":
            await operation.release_unstarted()
        if run.mode == "enforce":
            run.deny(error.code if isinstance(error, PresentationBudgetError) else None)
        if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
            raise
        logging.getLogger(__name__).warning("Presentation image operation unavailable; no paid retry")
        raise PresentationBudgetError(error.code if isinstance(error, PresentationBudgetError) else None) from None
    finally:
        try:
            if response is not None:
                await response.aclose()
        finally:
            if owned and native is not None:
                await native.aclose()
