"""Request model of the server-side ``fill_sorries`` job (plan 2026-10-18 §3.1).
Its own module: ``API_models.py`` sits at the source-size gate."""

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from server.app.core.config import Memory
from server.app.core.input_guards import reject_code_execution, validate_master_dir


class FillSorriesRequest(BaseModel):  # pylint: disable=too-few-public-methods
    """Submit a proof with ``sorry``s for the server-side sledgehammer job
    (plan 2026-10-18 §3.1)."""

    text: str = Field(
        description="The proof text; every `sorry` command becomes a target."
    )
    mode: Literal["document", "chunk"] = Field(
        default="document",
        description="document: load the text as the whole document (file-sync clients); "
        "chunk: append it as a chunk (verify_chunk semantics).",
    )
    timeout_s: int = Field(
        default=Memory.FILL_TIMEOUT_S,
        ge=1,
        le=300,
        description="Isabelle sledgehammer budget per subgoal (1–300 s).",
    )
    max_total_s: float = Field(
        default=Memory.FILL_MAX_TOTAL_S,
        ge=1.0,
        le=3600.0,
        description="Wall bound of the whole job, including time parked.",
    )
    load_timeout: float = Field(
        default=120.0, ge=1.0, description="Budget for loading/verifying the text."
    )
    master_dir: Optional[str] = Field(
        default=None,
        description="document mode: absolute directory of the file the text comes from "
        "(bare-name imports resolve against it; see DocumentLoadRequest.master_dir).",
    )
    provers: Optional[str] = Field(
        default=None,
        pattern=r"^[a-z0-9_]+( [a-z0-9_]+)*$",
        description="Optional space-separated prover names (Isabelle default when omitted).",
    )

    check_master_dir = field_validator("master_dir")(
        classmethod(lambda cls, v: validate_master_dir(v)))

    @field_validator("text")
    @classmethod
    def _no_code_execution(cls, v: str) -> str:
        # routes/fill.py hands `text` to the session itself (load_document /
        # verify_chunk), so the validators of the other text models never see
        # it: with the guard on, this route still accepted ML and
        # axiomatization until 2026-10-27 (report recheck 3B.2).
        return reject_code_execution(v, what="fill text")
