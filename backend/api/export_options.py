"""Export encoding options shared by every export endpoint."""
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel

from ..render.export_service import ExportOptions


class ExportOptionsMixin(BaseModel):
    imageFormat: str = "jpeg"      # PDF page images: jpeg | png (lossless)
    quality: int = 85              # JPEG quality 1-100
    zipFormat: str = "png"         # ZIP page files: png | webp | jpeg
    dpi: Optional[float] = None    # render every page to this ppi (overrides scale)
    pdfWidthIn: float = 6.625      # PDF page width in inches

    def export_options(self) -> ExportOptions:
        opts = ExportOptions(image_format=self.imageFormat, quality=self.quality,
                             zip_format=self.zipFormat, dpi=self.dpi,
                             pdf_width_in=self.pdfWidthIn)
        try:
            return opts.validate()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
