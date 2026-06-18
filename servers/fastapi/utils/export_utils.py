import os
import shutil
import aiohttp
from copy import deepcopy
from typing import List, Literal, Optional, Set
import uuid
from fastapi import HTTPException
from pathvalidate import sanitize_filename
from sqlmodel import select

from models.pptx_models import PptxPresentationModel
from models.presentation_and_path import PresentationAndPath
from models.sql.image_asset import ImageAsset
from models.sql.slide import SlideModel
from services.database import async_session_maker
from services.pptx_presentation_creator import PptxPresentationCreator
from services.temp_file_service import TEMP_FILE_SERVICE
from utils.asset_directory_utils import get_exports_directory
from utils.process_slides import normalize_local_image_urls
from utils.s3_utils import upload_file_to_s3


async def _save_slide_previews(
    pptx_model: PptxPresentationModel, presentation_id: uuid.UUID
) -> None:
    """
    Upload each slide's screenshot to S3 and store the S3 object key on the
    corresponding SlideModel (preview_s3_key).

    Note: this function does NOT delete the local screenshot files. Element
    screenshots referenced by the same per-request directory are still needed
    by PptxPresentationCreator. The whole per-request directory is removed by
    the caller (export_presentation) once PPTX assembly finishes.
    """
    s3_keys: List[Optional[str]] = []

    for slide in pptx_model.slides:
        if not slide.screenshot_src or not os.path.exists(slide.screenshot_src):
            s3_keys.append(None)
            continue

        s3_key = await upload_file_to_s3(
            slide.screenshot_src,
            postfix=f"preview/{presentation_id}",
        )
        s3_keys.append(s3_key)

    async with async_session_maker() as sql_session:
        slides = (
            await sql_session.scalars(
                select(SlideModel)
                .where(SlideModel.presentation == presentation_id)
                .order_by(SlideModel.index)
            )
        ).all()

        updated = False
        for slide_model in slides:
            if slide_model.index < len(s3_keys) and s3_keys[slide_model.index]:
                slide_model.preview_s3_key = s3_keys[slide_model.index]
                sql_session.add(slide_model)
                updated = True

        if updated:
            await sql_session.commit()


def _collect_screenshot_dirs(pptx_model: PptxPresentationModel) -> Set[str]:
    """
    Return the set of parent directories that hold per-request screenshot
    files produced by the Next.js converter. With the per-request subdir
    layout, all of a single export's screenshots share one directory, but we
    return a set defensively in case of future changes.
    """
    dirs: Set[str] = set()
    for slide in pptx_model.slides:
        if slide.screenshot_src:
            dirs.add(os.path.dirname(slide.screenshot_src))
    return dirs


def _collect_local_image_urls(content: dict) -> Set[str]:
    urls: Set[str] = set()

    def visit(value):
        if isinstance(value, dict):
            image_url = value.get("__image_url__")
            if isinstance(image_url, str) and image_url.startswith("/app_data/images/"):
                urls.add(image_url)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(content)
    return urls


async def _normalize_persisted_slide_image_urls(presentation_id: uuid.UUID) -> None:
    async with async_session_maker() as sql_session:
        slides = (
            await sql_session.scalars(
                select(SlideModel).where(SlideModel.presentation == presentation_id)
            )
        ).all()
        local_image_urls = set()
        for slide in slides:
            local_image_urls.update(_collect_local_image_urls(slide.content))

        if not local_image_urls:
            return

        image_assets = (
            await sql_session.scalars(
                select(ImageAsset).where(
                    ImageAsset.path.in_(list(local_image_urls)),
                    ImageAsset.s3_url.is_not(None),
                )
            )
        ).all()
        image_s3_keys_by_path = {
            asset.path: asset.s3_url
            for asset in image_assets
            if asset.path and asset.s3_url
        }

        updated = False
        for slide in slides:
            content = deepcopy(slide.content)
            if normalize_local_image_urls(content, image_s3_keys_by_path):
                slide.content = content
                sql_session.add(slide)
                updated = True

        if updated:
            await sql_session.commit()


async def export_presentation(
    presentation_id: uuid.UUID, title: str, export_as: Literal["pptx", "pdf"]
) -> PresentationAndPath:
    await _normalize_persisted_slide_image_urls(presentation_id)

    if export_as == "pptx":

        # Get the converted PPTX model from the Next.js service
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://localhost/api/presentation_to_pptx_model?id={presentation_id}"
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    print(f"Failed to get PPTX model: {error_text}")
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to convert presentation to PPTX model",
                    )
                pptx_model_data = await response.json()

        # Create PPTX file using the converted model
        pptx_model = PptxPresentationModel(**pptx_model_data)

        # Per-request screenshot directories created by the Next.js side.
        # Tracked here so we can guarantee removal in finally even on failure.
        screenshot_dirs = _collect_screenshot_dirs(pptx_model)

        try:
            # Upload slide screenshots to S3 and store keys on SlideModel
            await _save_slide_previews(pptx_model, presentation_id)

            temp_dir = TEMP_FILE_SERVICE.create_temp_dir()
            pptx_creator = PptxPresentationCreator(pptx_model, temp_dir)
            await pptx_creator.create_ppt()

            export_directory = get_exports_directory()
            pptx_path = os.path.join(
                export_directory,
                f"{sanitize_filename(title or str(uuid.uuid4()))}.pptx",
            )
            pptx_creator.save(pptx_path)

            return PresentationAndPath(
                presentation_id=presentation_id,
                path=pptx_path,
            )
        finally:
            for screenshot_dir in screenshot_dirs:
                shutil.rmtree(screenshot_dir, ignore_errors=True)
    else:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://localhost/api/export-as-pdf",
                json={
                    "id": str(presentation_id),
                    "title": sanitize_filename(title or str(uuid.uuid4())),
                },
            ) as response:
                response_json = await response.json()

        return PresentationAndPath(
            presentation_id=presentation_id,
            path=response_json["path"],
        )
