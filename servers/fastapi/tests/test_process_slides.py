import asyncio
from types import SimpleNamespace

from utils.process_slides import (
    PLACEHOLDER_ICON_URL,
    process_old_and_new_slides_and_fetch_assets,
    process_slide_add_placeholder_assets,
    process_slide_and_fetch_assets,
)


class StubImageGenerationService:
    async def generate_image(self, image_prompt):
        return "/generated/image.png"


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
