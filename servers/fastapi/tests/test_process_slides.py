import asyncio
from types import SimpleNamespace

from models.sql.image_asset import ImageAsset
from utils.process_slides import (
    PLACEHOLDER_ICON_URL,
    PLACEHOLDER_IMAGE_URL,
    normalize_local_image_urls,
    process_old_and_new_slides_and_fetch_assets,
    process_slide_add_placeholder_assets,
    process_slide_and_fetch_assets,
)


class StubImageGenerationService:
    async def generate_image(self, image_prompt):
        return "/generated/image.png"


class UploadedImageGenerationService:
    async def generate_image(self, image_prompt):
        return ImageAsset(
            path="/app_data/images/generated.png",
            is_uploaded=True,
            s3_url="api/presentation/images/presentation-id/generated image.png",
        )


def make_slide(content):
    return SimpleNamespace(content=content, presentation="presentation-id")


def test_process_slide_and_fetch_assets_keeps_placeholder_when_icon_search_is_empty(monkeypatch):
    slide = make_slide(
        {
            "bulletPoints": [
                {
                    "title": "Care",
                    "icon": {"__icon_query__": "care icon"},
                }
            ]
        }
    )

    process_slide_add_placeholder_assets(slide)

    async def empty_icon_search(query):
        return []

    monkeypatch.setattr(
        "utils.process_slides.ICON_FINDER_SERVICE.search_icons",
        empty_icon_search,
    )

    asyncio.run(process_slide_and_fetch_assets(StubImageGenerationService(), slide))

    assert (
        slide.content["bulletPoints"][0]["icon"]["__icon_url__"]
        == PLACEHOLDER_ICON_URL
    )


def test_process_old_and_new_slides_reuses_placeholder_when_old_icon_has_no_url():
    old_slide = make_slide(
        {
            "bulletPoints": [
                {
                    "title": "Care",
                    "icon": {"__icon_query__": "care icon"},
                }
            ]
        }
    )
    new_content = {
        "bulletPoints": [
            {
                "title": "Care updated",
                "icon": {"__icon_query__": "care icon"},
            }
        ]
    }

    asyncio.run(
        process_old_and_new_slides_and_fetch_assets(
            StubImageGenerationService(),
            old_slide,
            new_content,
        )
    )

    assert (
        new_content["bulletPoints"][0]["icon"]["__icon_url__"]
        == PLACEHOLDER_ICON_URL
    )


def test_process_old_and_new_slides_applies_fetched_icon_after_reused_icon(monkeypatch):
    old_slide = make_slide(
        {
            "bulletPoints": [
                {
                    "title": "Care",
                    "icon": {
                        "__icon_query__": "care icon",
                        "__icon_url__": "/old/care.svg",
                    },
                }
            ]
        }
    )
    new_content = {
        "bulletPoints": [
            {
                "title": "Care updated",
                "icon": {"__icon_query__": "care icon"},
            },
            {
                "title": "Food",
                "icon": {"__icon_query__": "food icon"},
            },
        ]
    }

    async def icon_search(query):
        return [f"/icons/{query}.svg"]

    monkeypatch.setattr(
        "utils.process_slides.ICON_FINDER_SERVICE.search_icons",
        icon_search,
    )

    asyncio.run(
        process_old_and_new_slides_and_fetch_assets(
            StubImageGenerationService(),
            old_slide,
            new_content,
        )
    )

    assert new_content["bulletPoints"][0]["icon"]["__icon_url__"] == "/old/care.svg"
    assert (
        new_content["bulletPoints"][1]["icon"]["__icon_url__"]
        == "/icons/food icon.svg"
    )


def test_process_slide_and_fetch_assets_uses_s3_proxy_url_for_uploaded_images():
    slide = make_slide(
        {
            "image": {
                "__image_prompt__": "generated image",
                "__image_url__": "/static/images/placeholder.jpg",
            }
        }
    )

    asyncio.run(process_slide_and_fetch_assets(UploadedImageGenerationService(), slide))

    assert (
        slide.content["image"]["__image_url__"]
        == "/api/v1/ppt/presentation/slide-screenshot?preview_s3_key="
        "api%2Fpresentation%2Fimages%2Fpresentation-id%2Fgenerated%20image.png"
    )


def test_process_old_and_new_slides_uses_s3_proxy_url_for_new_uploaded_images():
    old_slide = make_slide(
        {
            "image": {
                "__image_prompt__": "old image",
                "__image_url__": "/old/image.png",
            }
        }
    )
    new_content = {
        "image": {
            "__image_prompt__": "generated image",
            "__image_url__": "/static/images/placeholder.jpg",
        }
    }

    asyncio.run(
        process_old_and_new_slides_and_fetch_assets(
            UploadedImageGenerationService(),
            old_slide,
            new_content,
        )
    )

    assert (
        new_content["image"]["__image_url__"]
        == "/api/v1/ppt/presentation/slide-screenshot?preview_s3_key="
        "api%2Fpresentation%2Fimages%2Fpresentation-id%2Fgenerated%20image.png"
    )


def test_normalize_local_image_urls_rewrites_legacy_uploaded_paths():
    content = {
        "image": {
            "__image_prompt__": "legacy generated image",
            "__image_url__": "/app_data/images/generated image.png",
        },
        "nested": [
            {
                "image": {
                    "__image_prompt__": "missing generated image",
                    "__image_url__": "/app_data/images/missing.png",
                }
            }
        ],
    }

    changed = normalize_local_image_urls(
        content,
        {
            "/app_data/images/generated image.png": (
                "api/presentation/images/presentation-id/generated image.png"
            )
        },
    )

    assert changed is True
    assert (
        content["image"]["__image_url__"]
        == "/api/v1/ppt/presentation/slide-screenshot?preview_s3_key="
        "api%2Fpresentation%2Fimages%2Fpresentation-id%2Fgenerated%20image.png"
    )
    assert content["nested"][0]["image"]["__image_url__"] == PLACEHOLDER_IMAGE_URL
