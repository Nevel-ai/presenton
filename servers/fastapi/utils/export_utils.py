import os
import aiohttp
from typing import List, Literal, Optional
import uuid
from fastapi import HTTPException
from pathvalidate import sanitize_filename
from sqlmodel import select

from models.pptx_models import PptxPresentationModel
from models.presentation_and_path import PresentationAndPath
from models.sql.slide import SlideModel
from services.database import async_session_maker
from services.pptx_presentation_creator import PptxPresentationCreator
from services.temp_file_service import TEMP_FILE_SERVICE
from utils.asset_directory_utils import get_exports_directory
from utils.s3_utils import upload_file_to_s3


async def _save_slide_previews(
    pptx_model: PptxPresentationModel, presentation_id: uuid.UUID
) -> None:
    """
    Upload each slide's screenshot to S3, store the S3 object key on the
    corresponding SlideModel (preview_s3_key), and clean up temp files.
    """
    # Collect S3 keys per slide index
    s3_keys: List[Optional[str]] = []

    for idx, slide in enumerate(pptx_model.slides):
        if not slide.screenshot_src or not os.path.exists(slide.screenshot_src):
            s3_keys.append(None)
            continue

        # Upload screenshot to S3
        s3_key = await upload_file_to_s3(
            slide.screenshot_src,
            postfix=f"preview/{presentation_id}",
        )
        s3_keys.append(s3_key)

        # Clean up temp screenshot file
        try:
            os.remove(slide.screenshot_src)
        except OSError:
            pass

    # Persist S3 keys on the corresponding SlideModel rows
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


async def export_presentation(
    presentation_id: uuid.UUID, title: str, export_as: Literal["pptx", "pdf"]
) -> PresentationAndPath:
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
