from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from server.app.core.config import Timeouts
from server.app.core.diagnostic_guard import validate_diagnostic_command
from server.app.core.input_guards import (
    reject_code_execution,
    validate_master_dir,
    validate_import_name,
    validate_import_names,
    validate_project_path,
    validate_safe_name,
)


def _opt_safe_name(what: str):
    """Field validator factory: optional path-segment-safe identifier."""
    def _v(cls, v):
        return None if v is None else validate_safe_name(v, what)
    return _v


def _opt_project(cls, v):
    return None if v is None else validate_project_path(v)


def _opt_imports(cls, v):
    return None if v is None else validate_import_names(v)


def _opt_theory_name(cls, v):
    """A single theory name that gets spliced into a generated header /
    ROOT — same character policy as import names (no quotes, whitespace)."""
    return None if v is None else validate_import_name(v)


class SessionCreateRequest(BaseModel):
    theories: List[str] | None = None
    field: str | None = None
    label: str | None = Field(
        default=None,
        description="Free-form observability label (e.g. the file path a "
                    "file-synced client is mirroring). Echoed in session info; "
                    "no pooling behavior change.",
    )
    task_group: str | None = Field(
        default=None,
        description="Task group for heap-pool tenancy (default: 'default'). Sessions "
                    "may only use heaps of their own group.",
    )
    heap_session: str | None = Field(
        default=None,
        description="Name of a ready heap-pool session to start on (resolves within "
                    "task_group). The wrapper then states the heap's theories.",
    )
    project: str | None = Field(
        default=None,
        description="Project dir of a heap-pool entry (alternative to heap_session).",
    )

    check_theories = field_validator("theories")(classmethod(_opt_imports))
    check_task_group = field_validator("task_group")(classmethod(_opt_safe_name("task_group")))
    check_heap_session = field_validator("heap_session")(classmethod(_opt_safe_name("heap_session")))
    check_project = field_validator("project")(classmethod(_opt_project))


class SessionAcquireRequest(BaseModel):
    theories: List[str] = Field(default_factory=list)
    field: str | None = None
    reuse_dirty: bool = Field(
        default=True,
        description="If True, reuse sessions that already have commands executed. "
                    "If False, only match sessions with an empty command history.",
    )
    task_group: str | None = Field(
        default=None,
        description="Task group for heap-pool tenancy (default: 'default').",
    )
    heap_session: str | None = Field(
        default=None,
        description="Name of a ready heap-pool session to start on (within task_group).",
    )
    project: str | None = Field(
        default=None,
        description="Project dir of a heap-pool entry (alternative to heap_session).",
    )
    label: str | None = Field(
        default=None,
        description="Human-readable label shown in the admin console; applied on "
                    "EVERY acquire (fresh or reused) so it follows the current "
                    "holder rather than the original creator.",
    )

    check_theories = field_validator("theories")(classmethod(_opt_imports))
    check_task_group = field_validator("task_group")(classmethod(_opt_safe_name("task_group")))
    check_heap_session = field_validator("heap_session")(classmethod(_opt_safe_name("heap_session")))
    check_project = field_validator("project")(classmethod(_opt_project))


class SessionResponse(BaseModel):
    session_id: str
    created_at: float
    theories: List[str]
    status: str
    lease_id: str = Field(description="Exclusive lease identifier required for session-specific endpoints.")
    label: str | None = None
    task_group: str | None = None


class CommandRequest(BaseModel):
    command: str
    timeout: Optional[float] = Timeouts.COMMAND_DEFAULT

    @field_validator("command")
    @classmethod
    def _no_code_execution(cls, v: str) -> str:
        # ML / setup / file-IO commands are rejected (422) unless
        # ISABELLE_ALLOW_ML_COMMANDS=true — see core.input_guards.
        return reject_code_execution(v, what="command")


class EnterTheoryRequest(BaseModel):
    imports: Optional[List[str]] = Field(
        default=None,
        description="If given, the server begins the theory with a correctly-quoted "
                    "'theory <name> imports ... begin' header. If omitted, the caller must "
                    "supply the header itself (e.g. a corpus .thy file).",
    )

    check_imports = field_validator("imports")(classmethod(_opt_imports))


class DocumentLoadRequest(BaseModel):
    text: str = Field(
        min_length=1,
        description="Document body after 'begin' when imports are given, else a "
                    "full .thy source including its own 'theory ... imports ... begin' header.",
    )
    thy_name: Optional[str] = Field(
        default=None,
        description="Theory node name. Required when imports are given; otherwise "
                    "defaults to the name in text's theory header.",
    )
    imports: Optional[List[str]] = Field(
        default=None,
        description="If given, the server builds the theory header (same convention "
                    "as enter_theory) and text is the body after 'begin'. If omitted, "
                    "text must contain the header itself (the file-sync case).",
    )
    timeout: Optional[float] = Timeouts.COMMAND_DEFAULT
    report: bool = Field(
        default=False,
        description="If True, produce a per-command status report (same shape as "
                    "verify_chunk's) stored as the session's last_chunk_report, "
                    "WITHOUT rolling back ordinary failures (LSP-style: broken "
                    "state stays for inspection). On budget timeout the edit is "
                    "still discarded to cancel runaway commands.",
    )

    master_dir: Optional[str] = Field(
        default=None,
        description="Absolute directory of the file the text comes from: its bare-name "
                    "imports resolve against it, as in `isabelle build` (read from the "
                    "server's disk). Omitted: they resolve in the server's own directory.",
    )
    keep_on_timeout: bool = Field(
        default=False,
        description="With report: keep the edit when the first load outlives its timeout "
                    "(commands go on running, the next load is incremental) instead of "
                    "discarding it, which restarted the load on every call.",
    )

    check_imports = field_validator("imports")(classmethod(_opt_imports))
    check_thy_name = field_validator("thy_name")(classmethod(_opt_theory_name))

    check_master_dir = field_validator("master_dir")(
        classmethod(lambda cls, v: validate_master_dir(v)))

    @field_validator("text")
    @classmethod
    def _no_code_execution(cls, v: str) -> str:
        return reject_code_execution(v, what="document text")

    @model_validator(mode="after")
    def _name_required_with_imports(self):
        if self.imports and not self.thy_name:
            raise ValueError("thy_name is required when imports are given")
        return self


class DocumentLoadResponse(BaseModel):
    success: bool
    theory: str
    output: str | None = None
    error: str | None = None
    error_kind: str | None = Field(
        default=None,
        description="'memory' | 'prover_died' | None (see CommandResponse).",
    )
    execution_time: float
    report: Optional[Dict[str, Any]] = None


class CommandResponse(BaseModel):
    success: bool
    output: str | None = None
    error: str | None = None
    error_kind: str | None = Field(
        default=None,
        description="Why the command failed when it was not a proof error: 'memory' (the ML "
        "runtime ran out of memory) or 'prover_died' (the session's ML process is "
        "gone; the session was discarded — open a new one). None otherwise.",
    )
    subgoal_error: str | None = None
    subgoals: List[str]
    execution_time: float
    mode: str | None = None
    theory_verified: bool = False


class ProofStateResponse(BaseModel):
    subgoals: List[str]
    proof_finished: bool
    pending_qed: bool = False
    current_theory: str


class FactsResponse(BaseModel):
    facts: List[str]
    count: int


class StateCheckpoint(BaseModel):
    checkpoint_id: int
    timestamp: float


class ProofAttemptResponse(BaseModel):
    proof_id: str
    status: str
    message: str


class ProofStatusResponse(BaseModel):
    proof_id: str
    status: str
    progress: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None


class SessionAcquireResponse(BaseModel):
    session_id: str
    created_at: float
    theories: List[str]
    status: str
    reused: bool = Field(description="True if an existing session was returned, False if a new one was created.")
    lease_id: str = Field(description="Exclusive lease identifier. Pass to /release to return the session to the pool.")
    task_group: str | None = None


class BigStepTheoryRequest(BaseModel):
    theory_name: str
    dependencies: List[str] = Field(default_factory=list)
    field: str | None = None
    theory: str
    timeout: float = Timeouts.BIGSTEP_DEFAULT

    check_dependencies = field_validator("dependencies")(classmethod(_opt_imports))
    check_theory_name = field_validator("theory_name")(classmethod(_opt_theory_name))

    @field_validator("theory")
    @classmethod
    def _no_code_execution(cls, v: str) -> str:
        # bigstep is lease-free and runs `isabelle build` on this text, so it
        # is the most exposed ML-execution path — same guard as the others.
        return reject_code_execution(v, what="theory text")


class SledgehammerRequest(BaseModel):
    timeout_s: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Isabelle sledgehammer timeout in seconds (1–300).",
    )


class SledgehammerResponse(BaseModel):
    success: bool = Field(
        description="True only when a proof was found (a 'Try this:' line)."
    )
    suggestions: List[str]
    raw_output: str
    execution_time: float
    outcome: Optional[str] = Field(
        default=None, description="proved | none | timeout | error"
    )
    method: Optional[str] = Field(
        default=None, description="The first 'Try this:' method text, timing cut."
    )


class DiagnosticRequest(BaseModel):
    command: str = Field(
        description="A single READ-ONLY Isabelle diagnostic command: thm, term, prop, typ, "
        "prf, find_theorems, find_consts, or any print_*/find_* inspector. The command runs "
        "transiently (it does NOT alter the proof script). Code-executing / IO commands (ML, "
        "setup, *_file, ...) are rejected with HTTP 422.",
    )
    timeout: Optional[float] = Timeouts.COMMAND_DEFAULT

    @field_validator("command")
    @classmethod
    def _gatekeep_command(cls, v: str) -> str:
        # Raises ValueError -> FastAPI returns 422 with the reason. Keeps the
        # allowlist/denylist policy in one place (core.diagnostic_guard).
        return validate_diagnostic_command(v)


class DiagnosticResponse(BaseModel):
    success: bool = Field(description="False if the command produced an error message.")
    output: str | None = Field(
        default=None,
        description="The diagnostic's writeln/state output, e.g. the theorem statement for "
        "`thm`, the matches for `find_theorems`, the printed term for `term`.",
    )
    error: str | None = None
    execution_time: float


class CommandMessage(BaseModel):
    sev: str = Field(description="Message severity: 'error' or 'warning'.")
    text: str


class Position(BaseModel):
    line: int = Field(description="1-based line.")
    col: int = Field(description="1-based column (UTF-16 units, i.e. LSP columns).")


class CommandRange(BaseModel):
    start: Position
    end: Position


class CommandStatus(BaseModel):
    index: int = Field(description="Command position in the node (source order).")
    line: int = Field(description="1-based start line of the command WITHIN the submitted chunk.")
    node_line: Optional[int] = Field(
        default=None, description="Absolute 1-based line in the accumulated theory node (debug).")
    kind: str = Field(description="Command keyword, e.g. 'have', 'lemma', 'by'.")
    status: str = Field(description="One of: ok | failed | running | unprocessed.")
    range: Optional[CommandRange] = Field(
        default=None,
        description="1-based line/column extent of this command (node-absolute). "
                    "Message-level offsets do not exist for DRAFT nodes, so diagnostics "
                    "carry their owning command's range (jEdit granularity).")
    messages: List[CommandMessage] = Field(default_factory=list)


class LocatedCommand(BaseModel):
    """A command located by a line-based read-only query (jEdit cursor semantics:
    comment/blank lines resolve to the nearest preceding non-ignored command)."""
    kind: str = Field(description="Command keyword, e.g. 'have', 'lemma', 'by'.")
    source: str = Field(description="Source text of the command.")
    range: Optional[CommandRange] = Field(
        default=None, description="1-based line/column extent (node-absolute).")


class CommandAtLineResponse(BaseModel):
    """Read-only query: the command containing a 1-based line of the current node.
    Snapshot-based (no edits, no ML probes); consumed by the LSP-like file-sync mode."""
    found: bool
    kind: Optional[str] = None
    source: Optional[str] = None
    range: Optional[CommandRange] = None
    error: Optional[str] = None


class GoalsResponse(BaseModel):
    """Read-only query: rendered goal state before/after the command containing a
    1-based line. State messages only exist with show_states on
    (ISABELLE_SHOW_STATES, default true); with it off the goal lists are empty and `error` says so.
    `goals_after` is the command's last state message as one raw text element (no
    subgoal splitting); `goals_before` likewise for the previous non-ignored command."""
    found: bool
    command: Optional[LocatedCommand] = None
    goals_before: List[str] = Field(default_factory=list)
    goals_after: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class HoverResponse(BaseModel):
    """Hover info at a 1-based line/col (UTF-16 columns). Snapshot + Rendering —
    no evaluation. `contents` are the tooltip entries (entity kind, type, docs)."""
    found: bool
    range: Optional[CommandRange] = None
    contents: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class DefinitionTarget(BaseModel):
    """One go-to-definition target. kind=file → file/line/col(/end_*); kind=node →
    node + start_line/end_line (entity defined in the entry document itself);
    kind=path → file only (loaded-file references); kind=command_id → unresolved."""
    kind: str
    file: Optional[str] = None
    line: Optional[int] = None
    col: Optional[int] = None
    end_line: Optional[int] = None
    end_col: Optional[int] = None
    node: Optional[str] = None
    start_line: Optional[int] = None
    id: Optional[str] = None


class DefinitionResponse(BaseModel):
    found: bool
    targets: List[DefinitionTarget] = Field(default_factory=list)
    error: Optional[str] = None


class SledgehammerAtRequest(BaseModel):
    line: int = Field(ge=1, description="1-based line whose open goal to attack "
                                        "(jEdit cursor semantics: the command containing the line).")
    subgoal: int = Field(default=1, ge=1, description="1-based subgoal index.")
    timeout_s: int = Field(default=30, ge=1, le=300,
                           description="Isabelle sledgehammer timeout in seconds (1–300).")


class SledgehammerAtResponse(BaseModel):
    found: bool = Field(
        description="True only when a proof was found (a 'Try this:' line)."
    )
    results: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    execution_time: float = 0.0
    outcome: Optional[str] = Field(
        default=None, description="proved | none | timeout | error"
    )
    method: Optional[str] = Field(
        default=None, description="The first 'Try this:' method text, timing cut."
    )


class ChunkVerifyRequest(BaseModel):
    chunk: str = Field(description="A whole proof chunk (one or more Isar commands).")
    timeout: float = Field(
        default=Timeouts.COMMAND_DEFAULT,
        description="Single overall wall budget (seconds) for checking the chunk. "
                    "On expiry the report is partial; no per-command timeouts are raised.",
    )

    @field_validator("chunk")
    @classmethod
    def _non_empty_chunk(cls, v: str) -> str:
        # An empty/whitespace chunk would otherwise return success=False with
        # zero commands and no error — indistinguishable from a real failure.
        if not v or not v.strip():
            raise ValueError("chunk must contain at least one Isar command")
        return reject_code_execution(v, what="chunk")


class ChunkVerifyResponse(BaseModel):
    success: bool = Field(description="True iff every command is 'ok' and not timed out. "
                                      "NOTE: this means 'no command errors', NOT 'theorem proved' "
                                      "— check `proof_open` for that.")
    proof_open: bool = Field(
        default=False,
        description="True if the chunk left an UNCLOSED proof block (Isabelle `Toplevel.is_proof`) "
                    "— either subgoals remain, or the goal is discharged but `qed` is still "
                    "pending (see `pending_qed`; batch builds reject that state with "
                    "'Goal present in this block'). The chunk is still kept (so you "
                    "can sledgehammer the open goal), but the theorem is NOT proved; close it "
                    "or rollback before starting a new theorem/lemma. A fully proved chunk has "
                    "success=True and proof_open=False.",
    )
    pending_qed: bool = Field(
        default=False,
        description="True if the proof block is open but NO subgoals remain — the goal is "
                    "discharged and only the closing `qed` is missing. Submit a bare `qed` "
                    "chunk to finish; do NOT start new proof work.",
    )
    used_sorry: bool = Field(
        default=False,
        description="True if the chunk contains a `sorry` or `oops` command (detected on the "
                    "PARSED commands, so occurrences in comments/strings don't count). Such a "
                    "theorem is NOT actually proved. A genuinely proved chunk has success=True, "
                    "proof_open=False, and used_sorry=False.",
    )
    timed_out: bool = Field(description="True if the overall wall budget elapsed.")
    stuck_line: int | None = Field(
        default=None,
        description="On timeout, the line still 'running' (the likely loop), if any.",
    )
    commands: List[CommandStatus]
    execution_time: float
    error: str | None = Field(
        default=None,
        description="Backend-level error when nothing could be checked (e.g. "
                    "'theory not begun'). None when commands were processed.",
    )
    error_kind: str | None = Field(
        default=None,
        description="'memory' when a command failed because the ML runtime ran out of "
        "memory, 'prover_died' when the session's ML process is gone (session "
        "discarded), None for ordinary proof errors / success.",
    )

# ---------------------------------------------------------------------------
# Heap pool (Stage 3): verified per-project heaps + task-group tenancy
# ---------------------------------------------------------------------------


class HeapBuildRequest(BaseModel):
    task_group: str = Field(description="Owning task group (required; namespace isolation).")
    project: str = Field(description="Absolute path of the project dir (theories = top-level .thy files).")
    session_name: Optional[str] = Field(
        default=None,
        description="Isabelle session name for the heap. Default: parsed from a "
                    "user-provided ROOT, else derived from the project dir name.",
    )

    check_task_group = field_validator("task_group")(classmethod(_opt_safe_name("task_group")))
    check_session_name = field_validator("session_name")(classmethod(_opt_safe_name("session_name")))

    @field_validator("project")
    @classmethod
    def _project_under_allowed_roots(cls, v: str) -> str:
        return validate_project_path(v)


class HeapTheoryFile(BaseModel):
    path: str
    sha256: str
    mtime: float


class HeapEntryResponse(BaseModel):
    task_group: str
    project: str
    session_name: str
    root_dir: str
    fingerprint: str
    status: str = Field(description="One of: building | ready | stale | failed.")
    built_at: Optional[float] = None
    built_by: Optional[str] = None
    build_log_tail: str = ""


class HeapListResponse(BaseModel):
    heaps: List[HeapEntryResponse]


class HeapManifestResponse(HeapEntryResponse):
    """The full inspection record: what the heap was built from."""
    root_text: str
    theory_files: List[HeapTheoryFile]


class HeapGroupInfo(BaseModel):
    task_group: str
    heap_count: int
    ready: int


class AvailableHeap(BaseModel):
    """One heap image on disk (base session image or pool-built)."""
    session: str
    platform: str
    size_mb: float
    modified: str
    origin: str = Field(description="One of: pool | user | distribution.")
    path: str


class AvailableHeapsResponse(BaseModel):
    heaps: List[AvailableHeap]


class ParseTheoryHeaderRequest(BaseModel):
    text: str


class ParseTheoryHeaderResponse(BaseModel):
    """The canonical theory-header parse (comment-stripped, header-anchored)."""
    theory_name: Optional[str]
    imports: List[str]
    suggested_field: Optional[str] = Field(
        description="Session image the dotted imports imply: the largest "
        "distribution image named (HOL-Probability > HOL-Analysis > "
        "HOL-Number_Theory > HOL-Algebra > HOL-Computational_Algebra > "
        "HOL-Combinatorics > HOL-Library), else the first dotted import's "
        "session (HOL-Cardinals.Cardinals -> HOL-Cardinals); path imports never "
        "count. A session started on it builds the image if the server lacks it."
    )


class HeapGroupsResponse(BaseModel):
    groups: List[HeapGroupInfo]
