import asyncio
import os
from typing import List, Mapping, TYPE_CHECKING
from urllib.parse import quote
from models.image_prompt import ImagePrompt
from models.sql.image_asset import ImageAsset
from models.sql.slide import SlideModel
from services.icon_finder_service import ICON_FINDER_SERVICE
from utils.dict_utils import get_dict_at_path, get_dict_paths_with_key, set_dict_at_path

if TYPE_CHECKING:
    from services.image_generation_service import ImageGenerationService

PLACEHOLDER_IMAGE_URL = "/static/images/placeholder.jpg"
PLACEHOLDER_ICON_URL = "/static/icons/placeholder.svg"


def _get_s3_proxy_url(s3_key: str) -> str:
    preview_s3_key = quote(s3_key, safe="")
    return (
        "/api/v1/ppt/presentation/slide-screenshot"
        f"?preview_s3_key={preview_s3_key}"
    )


def _get_image_url_for_rendering(image_asset: ImageAsset) -> str:
    if image_asset.s3_url:
        return _get_s3_proxy_url(image_asset.s3_url)
    return image_asset.path


def normalize_local_image_urls(
    content: dict,
    image_s3_keys_by_path: Mapping[str, str],
) -> bool:
    """
    Replace legacy local image URLs with S3 proxy URLs before rendering.

    Older slides can persist /app_data/images/... paths in JSON content. Those
    files are pod-local and may disappear after restart or render on a different
    instance, producing blank slide screenshots. ImageAsset keeps the durable S3
    key, so use it when available and fall back to a placeholder for missing
    local files.
    """
    changed = False

    def visit(value):
        nonlocal changed

        if isinstance(value, dict):
            image_url = value.get("__image_url__")
            if isinstance(image_url, str) and image_url.startswith("/app_data/images/"):
                s3_key = image_s3_keys_by_path.get(image_url)
                if s3_key:
                    value["__image_url__"] = _get_s3_proxy_url(s3_key)
                    changed = True
                elif not os.path.exists(image_url):
                    value["__image_url__"] = PLACEHOLDER_IMAGE_URL
                    changed = True

            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(content)
    return changed


def _get_icon_url_or_placeholder(icon_result) -> str:
    if isinstance(icon_result, (list, tuple)) and icon_result:
        icon_url = icon_result[0]
        if isinstance(icon_url, str) and icon_url:
            return icon_url
    return PLACEHOLDER_ICON_URL


async def process_slide_and_fetch_assets(
    image_generation_service: "ImageGenerationService",
    slide: SlideModel,
) -> List[ImageAsset]:

    async_tasks = []

    image_paths = get_dict_paths_with_key(slide.content, "__image_prompt__")
    icon_paths = get_dict_paths_with_key(slide.content, "__icon_query__")

    for image_path in image_paths:
        __image_prompt__parent = get_dict_at_path(slide.content, image_path)
        async_tasks.append(
            image_generation_service.generate_image(
                ImagePrompt(
                    prompt=__image_prompt__parent["__image_prompt__"],
                    # Pass presentation ID so S3 keys can be grouped per presentation
                    presentation_id=str(slide.presentation),
                )
            )
        )

    for icon_path in icon_paths:
        __icon_query__parent = get_dict_at_path(slide.content, icon_path)
        async_tasks.append(
            ICON_FINDER_SERVICE.search_icons(__icon_query__parent["__icon_query__"])
        )

    results = await asyncio.gather(*async_tasks)
    results.reverse()

    return_assets = []
    for image_path in image_paths:
        image_dict = get_dict_at_path(slide.content, image_path)
        result = results.pop()
        if isinstance(result, ImageAsset):
            return_assets.append(result)
            image_dict["__image_url__"] = _get_image_url_for_rendering(result)
        else:
            image_dict["__image_url__"] = result
        set_dict_at_path(slide.content, image_path, image_dict)

    for icon_path in icon_paths:
        icon_dict = get_dict_at_path(slide.content, icon_path)
        icon_dict["__icon_url__"] = _get_icon_url_or_placeholder(results.pop())
        set_dict_at_path(slide.content, icon_path, icon_dict)

    return return_assets


async def process_old_and_new_slides_and_fetch_assets(
    image_generation_service: "ImageGenerationService",
    slide: SlideModel,
    new_slide_content: dict,
) -> List[ImageAsset]:
    """
    Compare the old and new slide contents, reusing assets where possible and
    generating new ones where prompts/queries changed.
    """
    old_slide_content = slide.content

    # Finds all old images
    old_image_dict_paths = get_dict_paths_with_key(
        old_slide_content, "__image_prompt__"
    )
    old_image_dicts = [
        get_dict_at_path(old_slide_content, path) for path in old_image_dict_paths
    ]
    old_image_prompts = [
        old_image_dict["__image_prompt__"] for old_image_dict in old_image_dicts
    ]

    # Finds all old icons
    old_icon_dict_paths = get_dict_paths_with_key(old_slide_content, "__icon_query__")
    old_icon_dicts = [
        get_dict_at_path(old_slide_content, path) for path in old_icon_dict_paths
    ]
    old_icon_queries = [
        old_icon_dict["__icon_query__"] for old_icon_dict in old_icon_dicts
    ]

    # Finds all new images
    new_image_dict_paths = get_dict_paths_with_key(
        new_slide_content, "__image_prompt__"
    )
    new_image_dicts = [
        get_dict_at_path(new_slide_content, path) for path in new_image_dict_paths
    ]

    # Finds all new icons
    new_icon_dict_paths = get_dict_paths_with_key(new_slide_content, "__icon_query__")
    new_icon_dicts = [
        get_dict_at_path(new_slide_content, path) for path in new_icon_dict_paths
    ]

    # Creates async tasks for fetching new images
    async_image_fetch_tasks = []
    new_images_fetch_status = []

    # Creates async tasks for fetching new icons
    async_icon_fetch_tasks = []
    new_icons_fetch_status = []

    # Creates async tasks for fetching new images
    # Use old image url if prompt is same
    for new_image in new_image_dicts:
        if new_image["__image_prompt__"] in old_image_prompts:
            old_image_url = old_image_dicts[
                old_image_prompts.index(new_image["__image_prompt__"])
            ].get("__image_url__", PLACEHOLDER_IMAGE_URL)
            new_image["__image_url__"] = old_image_url
            new_images_fetch_status.append(False)
            continue

        async_image_fetch_tasks.append(
            image_generation_service.generate_image(
                ImagePrompt(
                    prompt=new_image["__image_prompt__"],
                    # Keep grouping by the same presentation in S3
                    presentation_id=str(slide.presentation),
                )
            )
        )
        new_images_fetch_status.append(True)

    # Creates async tasks for fetching new icons
    # Use old icon url if query is same
    for new_icon in new_icon_dicts:
        if new_icon["__icon_query__"] in old_icon_queries:
            old_icon_url = old_icon_dicts[
                old_icon_queries.index(new_icon["__icon_query__"])
            ].get("__icon_url__", PLACEHOLDER_ICON_URL)
            new_icon["__icon_url__"] = old_icon_url
            new_icons_fetch_status.append(False)
            continue

        async_icon_fetch_tasks.append(
            ICON_FINDER_SERVICE.search_icons(new_icon["__icon_query__"])
        )
        new_icons_fetch_status.append(True)

    new_images = await asyncio.gather(*async_image_fetch_tasks)
    new_icons = await asyncio.gather(*async_icon_fetch_tasks)

    # list of new assets
    new_assets = []

    # Sets new image and icon urls for assets that were fetched
    fetched_image_index = 0
    for i, new_image_dict in enumerate(new_image_dicts):
        if new_images_fetch_status[i]:
            fetched_image = new_images[fetched_image_index]
            fetched_image_index += 1
            if isinstance(fetched_image, ImageAsset):
                new_assets.append(fetched_image)
                image_url = _get_image_url_for_rendering(fetched_image)
            else:
                image_url = fetched_image
            new_image_dict["__image_url__"] = image_url

    fetched_icon_index = 0
    for i, new_icon_dict in enumerate(new_icon_dicts):
        if new_icons_fetch_status[i]:
            new_icon_dict["__icon_url__"] = _get_icon_url_or_placeholder(
                new_icons[fetched_icon_index]
            )
            fetched_icon_index += 1

    for i, new_image_dict in enumerate(new_image_dicts):
        set_dict_at_path(new_slide_content, new_image_dict_paths[i], new_image_dict)

    for i, new_icon_dict in enumerate(new_icon_dicts):
        set_dict_at_path(new_slide_content, new_icon_dict_paths[i], new_icon_dict)

    return new_assets


def process_slide_add_placeholder_assets(slide: SlideModel):

    image_paths = get_dict_paths_with_key(slide.content, "__image_prompt__")
    icon_paths = get_dict_paths_with_key(slide.content, "__icon_query__")

    for image_path in image_paths:
        image_dict = get_dict_at_path(slide.content, image_path)
        image_dict["__image_url__"] = PLACEHOLDER_IMAGE_URL
        set_dict_at_path(slide.content, image_path, image_dict)

    for icon_path in icon_paths:
        icon_dict = get_dict_at_path(slide.content, icon_path)
        icon_dict["__icon_url__"] = PLACEHOLDER_ICON_URL
        set_dict_at_path(slide.content, icon_path, icon_dict)
