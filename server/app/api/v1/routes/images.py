"""POST /api/v1/select_field: the session image to start on so that built theories are reused
(services/image_select.py)."""
import asyncio
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from server.app.core.input_guards import validate_project_path
from server.app.core.logging import get_logger
from server.app.services.image_select import selection

router = APIRouter(tags=["images"])
logger = get_logger(__name__)


class SelectFieldRequest(BaseModel):
    imports: List[str] = Field(default_factory=list)
    field: Optional[str] = None
    session_dirs: List[str] = Field(default_factory=list)

    @field_validator("session_dirs")
    @classmethod
    def _dirs(cls, v):
        return [validate_project_path(d) for d in v[:64]]


class SelectFieldResponse(BaseModel):
    field: Optional[str]
    # qualified imports the chosen image does not contain: the session loads them from source
    from_source: List[str] = Field(default_factory=list)
    # images that contain all of them but are out of date (their sources changed since the build)
    stale: List[str] = Field(default_factory=list)
    # the proposed field is itself out of date: the session cannot start on it
    field_stale: bool = False


@router.post("/api/v1/select_field", response_model=SelectFieldResponse)
async def select_field_endpoint(request: SelectFieldRequest) -> SelectFieldResponse:
    sel = await asyncio.to_thread(
        selection, request.imports, request.field, tuple(request.session_dirs))
    if sel["field"] != request.field or sel["stale"]:
        logger.info("select_field imports=%s proposed=%s chosen=%s stale=%s",
                    request.imports, request.field, sel["field"], sel["stale"])
    return SelectFieldResponse(**sel)
