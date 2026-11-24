"""Request/response models of ``POST /api/v1/sessions/{id}/attempt`` (candidate proofs
run in the file's own session, efficiency plan stage C). Its own module:
``API_models.py`` sits at the source-size gate."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from server.app.core.input_guards import reject_code_execution


class AttemptRequest(BaseModel):  # pylint: disable=too-few-public-methods
    """Candidates to try at a line of the session's current document."""

    line: int = Field(
        ge=1,
        description="1-based first line of the document text a candidate REPLACES: it "
        "continues from the state after the last command that ends before this line.",
    )
    candidates: List[str] = Field(min_length=1, max_length=64)
    timeout_s: Optional[float] = Field(
        default=None,
        description="Time limit of each candidate in seconds (default 30). Larger values "
        "than the server cap (300) run at the cap; the answer says so.",
    )

    @field_validator("candidates")
    @classmethod
    def _no_code_execution(cls, v: List[str]) -> List[str]:
        return [reject_code_execution(c, what="candidate") for c in v]


class AttemptCandidate(BaseModel):  # pylint: disable=too-few-public-methods
    """One candidate: its report (the shape of a document report of "text before line
    + candidate"), or the reason it has to be checked elsewhere (``fallback``)."""

    candidate: str
    fallback: Optional[str] = None
    command: Optional[str] = Field(
        default=None, description="With fallback=candidate_command: the command.")
    report: Optional[Dict[str, Any]] = None
    elapsed_s: Optional[float] = Field(
        default=None, description="The candidate's own running time.")
    cancelled: Optional[str] = Field(
        default=None, description="Why the candidate was stopped early (e.g. memory).")


class AttemptResponse(BaseModel):  # pylint: disable=too-few-public-methods
    """``fallback`` set: no candidate ran in the session (multiline_command, no_host,
    host_end); the caller checks them on a scratch session."""

    fallback: Optional[str] = None
    host_line: Optional[int] = None
    timeout_s: int
    timeout_capped: bool = False
    note: Optional[str] = None
    candidates: List[AttemptCandidate] = Field(default_factory=list)
    execution_time: float = 0.0
