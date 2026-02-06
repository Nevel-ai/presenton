import base64
import json
import os
import aiohttp
from typing import Literal
import uuid
from fastapi import HTTPException
from pathvalidate import sanitize_filename

from models.pptx_models import PptxPresentationModel
from models.presentation_and_path import PresentationAndPath
from models.sql.presentation import PresentationModel
from services.database import async_session_maker
from services.pptx_presentation_creator import PptxPresentationCreator
from services.temp_file_service import TEMP_FILE_SERVICE
from utils.asset_directory_utils import get_exports_directory
import uuid


async def _save_presentation_thumbnail(
    pptx_model: PptxPresentationModel, presentation_id: uuid.UUID
) -> None:
    """
    Read the first slide's screenshot, encode it as base64, and store it
    directly on the PresentationModel. Clean up all screenshot temp files.
    """
    thumbnail_base64 = None

    for slide in pptx_model.slides:
        if not slide.screenshot_src or not os.path.exists(slide.screenshot_src):
            continue

        # Encode the first available slide as the presentation thumbnail
        if thumbnail_base64 is None:
            with open(slide.screenshot_src, "rb") as f:
                thumbnail_base64 = base64.b64encode(f.read()).decode("utf-8")

        # Clean up all screenshot temp files
        try:
            os.remove(slide.screenshot_src)
        except OSError:
            pass

    if thumbnail_base64:
        async with async_session_maker() as sql_session:
            presentation = await sql_session.get(PresentationModel, presentation_id)
            if presentation:
                presentation.thumbnail_base64 = thumbnail_base64
                sql_session.add(presentation)
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

        # Encode first slide screenshot as base64 and save as presentation thumbnail
        await _save_presentation_thumbnail(pptx_model, presentation_id)

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
