from typing import Annotated, Optional
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
import uuid

from models.sql.presentation import PresentationModel
from models.sql.slide import SlideModel
from services.database import get_async_session
from services.image_generation_service import ImageGenerationService
from utils.asset_directory_utils import get_images_directory
from utils.llm_calls.edit_slide import get_edited_slide_content
from utils.llm_calls.edit_slide_html import get_edited_slide_html
from utils.llm_calls.select_slide_type_on_edit import get_slide_layout_from_prompt
from utils.process_slides import (
    process_old_and_new_slides_and_fetch_assets,
    process_slide_add_placeholder_assets,
    process_slide_and_fetch_assets,
)
from models.presentation_with_slides import PresentationWithSlides
from models.presentation_outline_model import (
    PresentationOutlineModel,
    SlideOutlineModel,
)
from utils.llm_calls.generate_presentation_structure import (
    generate_presentation_structure,
)
from utils.llm_calls.generate_slide_content import (
    get_slide_content_from_type_and_outline,
)


SLIDE_ROUTER = APIRouter(prefix="/slide", tags=["Slide"])


@SLIDE_ROUTER.post("/edit")
async def edit_slide(
    presentation_id: Annotated[uuid.UUID, Body()],
    slide_index: Annotated[int, Body()],
    prompt: Annotated[str, Body()],
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, presentation_id)
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")

    slide_result = await sql_session.scalars(
        select(SlideModel).where(
            (SlideModel.presentation == presentation_id)
            & (SlideModel.index == slide_index)
        )
    )
    slide = slide_result.first()
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")

    presentation_layout = presentation.get_layout()
    slide_layout = await get_slide_layout_from_prompt(
        prompt, presentation_layout, slide
    )

    edited_slide_content = await get_edited_slide_content(
        prompt, slide, presentation.language, slide_layout
    )

    image_generation_service = ImageGenerationService(get_images_directory())

    # This will mutate edited_slide_content
    new_assets = await process_old_and_new_slides_and_fetch_assets(
        image_generation_service,
        slide,
        edited_slide_content,
    )

    # Always assign a new unique id to the slide
    slide.id = uuid.uuid4()

    sql_session.add(slide)
    slide.content = edited_slide_content
    slide.layout = slide_layout.id
    slide.speaker_note = edited_slide_content.get("__speaker_note__", "")
    sql_session.add_all(new_assets)
    await sql_session.commit()

    return slide


@SLIDE_ROUTER.post("/edit-html", response_model=SlideModel)
async def edit_slide_html(
    id: Annotated[uuid.UUID, Body()],
    prompt: Annotated[str, Body()],
    html: Annotated[Optional[str], Body()] = None,
    sql_session: AsyncSession = Depends(get_async_session),
):
    slide = await sql_session.get(SlideModel, id)
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")

    html_to_edit = html or slide.html_content
    if not html_to_edit:
        raise HTTPException(status_code=400, detail="No HTML to edit")

    edited_slide_html = await get_edited_slide_html(prompt, html_to_edit)

    # Always assign a new unique id to the slide
    # This is to ensure that the nextjs can track slide updates
    slide.id = uuid.uuid4()

    sql_session.add(slide)
    slide.html_content = edited_slide_html
    await sql_session.commit()

    return slide


@SLIDE_ROUTER.post("/delete", response_model=PresentationWithSlides)
async def delete_slide(
    presentation_id: Annotated[uuid.UUID, Body()],
    slide_index: Annotated[int, Body()],
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, presentation_id)
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")

    # Fetch slides count to validate index range
    slides_result = await sql_session.scalars(
        select(SlideModel)
        .where(SlideModel.presentation == presentation_id)
        .order_by(SlideModel.index)
    )
    slides_list = slides_result.all()

    if slide_index < 0 or slide_index >= len(slides_list):
        raise HTTPException(status_code=400, detail="Slide index out of range")

    # Find the specific slide to delete
    slide_result = await sql_session.scalars(
        select(SlideModel).where(
            (SlideModel.presentation == presentation_id)
            & (SlideModel.index == slide_index)
        )
    )
    slide_to_delete = slide_result.first()
    if not slide_to_delete:
        # Shouldn't happen due to range check, but keeping it safe
        raise HTTPException(status_code=404, detail="Slide not found")

    # Delete the slide
    from sqlalchemy import delete as sa_delete

    await sql_session.execute(
        sa_delete(SlideModel).where(SlideModel.id == slide_to_delete.id)
    )

    # Reindex subsequent slides to keep indices consistent
    subsequent_slides_result = await sql_session.scalars(
        select(SlideModel)
        .where(
            (SlideModel.presentation == presentation_id)
            & (SlideModel.index > slide_index)
        )
        .order_by(SlideModel.index)
    )
    subsequent_slides = subsequent_slides_result.all()
    for s in subsequent_slides:
        s.index = s.index - 1
        sql_session.add(s)

    # Update presentation's slide count
    presentation.n_slides = max(0, presentation.n_slides - 1)
    sql_session.add(presentation)

    await sql_session.commit()

    # Return updated presentation with slides like get_presentation
    updated_slides_result = await sql_session.scalars(
        select(SlideModel)
        .where(SlideModel.presentation == presentation_id)
        .order_by(SlideModel.index)
    )
    updated_slides = updated_slides_result.all()

    return PresentationWithSlides(
        **presentation.model_dump(),
        slides=updated_slides,
    )


@SLIDE_ROUTER.post("/create", response_model=PresentationWithSlides)
async def create_slide(
    presentation_id: Annotated[uuid.UUID, Body()],
    slide_index: Annotated[int, Body()],
    content: Annotated[str, Body()],
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, presentation_id)
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")

    # Fetch existing slides ordered to validate and prepare reindexing
    slides_result = await sql_session.scalars(
        select(SlideModel)
        .where(SlideModel.presentation == presentation_id)
        .order_by(SlideModel.index)
    )
    slides_list = slides_result.all()

    if slide_index < 0 or slide_index > len(slides_list):
        raise HTTPException(status_code=400, detail="Slide index out of range")

    # Build a single-slide outline from the provided content (prompt)
    outline_model = PresentationOutlineModel(
        slides=[SlideOutlineModel(content=content)]
    )

    # Select a layout for this new slide based on the presentation's template/layout
    layout_model = presentation.get_layout()
    if layout_model.ordered:
        presentation_structure = layout_model.to_presentation_structure()
    else:
        presentation_structure = await generate_presentation_structure(
            outline_model,
            layout_model,
            presentation.instructions,
            True,
        )

    # Ensure we have one layout index
    presentation_structure.slides = presentation_structure.slides[:1]
    if not presentation_structure.slides:
        raise HTTPException(status_code=500, detail="Failed to select slide layout")

    layout_index = presentation_structure.slides[0]
    if layout_index >= len(layout_model.slides):
        # Fallback to a safe index
        layout_index = 0

    slide_layout = layout_model.slides[layout_index]

    # Generate slide content
    slide_content = await get_slide_content_from_type_and_outline(
        slide_layout,
        outline_model.slides[0],
        presentation.language,
        presentation.tone,
        presentation.verbosity,
        presentation.instructions,
    )

    # Create new slide model at the desired index
    new_slide = SlideModel(
        presentation=presentation_id,
        layout_group=layout_model.name,
        layout=slide_layout.id,
        index=slide_index,
        speaker_note=slide_content.get("__speaker_note__", ""),
        content=slide_content,
    )

    # Reindex subsequent slides (shift indices by +1 from insertion point)
    subsequent_slides_result = await sql_session.scalars(
        select(SlideModel)
        .where(
            (SlideModel.presentation == presentation_id)
            & (SlideModel.index >= slide_index)
        )
        .order_by(SlideModel.index.desc())
    )
    subsequent_slides = subsequent_slides_result.all()
    # Update in descending order to avoid index collisions
    for s in subsequent_slides:
        s.index = s.index + 1
        sql_session.add(s)

    # Fetch and attach assets for the new slide
    image_generation_service = ImageGenerationService(get_images_directory())
    process_slide_add_placeholder_assets(new_slide)
    generated_assets = await process_slide_and_fetch_assets(
        image_generation_service, new_slide
    )

    # Persist changes
    sql_session.add(new_slide)
    if generated_assets:
        sql_session.add_all(generated_assets)

    presentation.n_slides = (presentation.n_slides or len(slides_list)) + 1
    sql_session.add(presentation)

    await sql_session.commit()

    # Return the updated presentation with slides
    updated_slides_result = await sql_session.scalars(
        select(SlideModel)
        .where(SlideModel.presentation == presentation_id)
        .order_by(SlideModel.index)
    )
    updated_slides = updated_slides_result.all()

    return PresentationWithSlides(
        **presentation.model_dump(),
        slides=updated_slides,
    )
