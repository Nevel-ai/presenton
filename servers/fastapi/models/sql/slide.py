from typing import Optional
import uuid
from sqlalchemy import ForeignKey, String
from sqlmodel import Field, Column, JSON, SQLModel


class SlideModel(SQLModel, table=True):
    __tablename__ = "slides"

    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
    presentation: uuid.UUID = Field(
        sa_column=Column(ForeignKey("presentations.id", ondelete="CASCADE"), index=True)
    )
    layout_group: str
    layout: str
    index: int
    content: dict = Field(sa_column=Column(JSON))
    html_content: Optional[str]
    speaker_note: Optional[str] = None
    properties: Optional[dict] = Field(sa_column=Column(JSON))
    preview_s3_key: Optional[str] = Field(sa_column=Column(String), default=None)

    def get_new_slide(self, presentation: uuid.UUID, content: Optional[dict] = None):
        # Only carry the preview_s3_key when the new row belongs to the same
        # presentation (in-place edit). When deriving a new presentation we must
        # not point at the source presentation's S3 object; the next export will
        # populate a fresh preview_s3_key under the new presentation's prefix.
        same_presentation = presentation == self.presentation
        return SlideModel(
            id=uuid.uuid4(),
            presentation=presentation,
            layout_group=self.layout_group,
            layout=self.layout,
            index=self.index,
            speaker_note=self.speaker_note,
            content=content or self.content,
            properties=self.properties,
            preview_s3_key=self.preview_s3_key if same_presentation else None,
        )
