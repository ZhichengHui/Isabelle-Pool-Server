"""Configuration for the Isabelle Pool Server LSP-like MCP server (all via env, prefix ISABELLE_MCP_LSP_)."""
from __future__ import annotations

from ..common.config import env_bool, env_float, env_int, env_str

_P = "ISABELLE_MCP_LSP_"


class Config:
    # Where the running Isabelle Pool Server lives.
    GYM_URL: str = env_str(_P, "GYM_URL", "http://localhost:8000")
    DEFAULT_FIELD: str = env_str(_P, "FIELD", "HOL")
    # File bindings and scratch sessions start on the session image the
    # server's header parse suggests for the file's dotted imports
    # (`suggested_field` of POST /parse_theory_header: the largest distribution
    # image named) instead of DEFAULT_FIELD. A file importing
    # "HOL-Computational_Algebra.Computational_Algebra" then starts on that
    # image (seconds) rather than loading its theories from source into the
    # HOL image (minutes; isabelle_open used to time out). An image the server
    # does not have is built on first use, inside the acquire: prebuild the
    # big ones (`deploy/setup.sh --build-heaps`). Opt-in.
    FIELD_FROM_IMPORTS: bool = env_bool(_P, "FIELD_FROM_IMPORTS", False)
    DEFAULT_TASK_GROUP: str = env_str(_P, "TASK_GROUP", "default")

    # httpx timeout for the underlying client (must exceed load/build budgets).
    HTTP_TIMEOUT: float = env_float(_P, "HTTP_TIMEOUT", 600.0)
    # Wall budget (s) for load_document syncs and per-candidate attempt verification.
    LOAD_TIMEOUT: float = env_float(_P, "LOAD_TIMEOUT", 30.0)
    ATTEMPT_TIMEOUT: float = env_float(_P, "ATTEMPT_TIMEOUT", 180.0)

    # Concurrency cap for multi_attempt fan-out (bounded by the server pool).
    MAX_PARALLEL: int = env_int(_P, "MAX_PARALLEL", 4)
    # Warm scratch sessions kept per context (task_group, heap, imports, field);
    # reused across calls — each use is a load_document reset.
    SCRATCH_POOL_SIZE: int = env_int(_P, "SCRATCH_POOL_SIZE", 4)
    # Max seconds a caller waits for a scratch slot once the pool is at its cap
    # (a legitimate wait is one other candidate's verification); expiry raises a
    # clear error instead of hanging forever (Bug 17 / audit MCP-1).
    SCRATCH_WAIT_TIMEOUT: float = env_float(_P, "SCRATCH_WAIT_TIMEOUT", ATTEMPT_TIMEOUT + 60.0)
    # Idle scratch sessions kept ready per context, already loaded with the file's text
    # (scratch_spare.py): min(SCRATCH_SPARE, MAX_PARALLEL), within SCRATCH_POOL_SIZE, prepared
    # in the background after a file is bound and topped up after each multi_attempt, so each
    # candidate of the next multi_attempt (also the first) finds a warm, checked session. Each
    # spare is a held Isabelle session (memory). 0 disables.
    SCRATCH_SPARE: int = env_int(_P, "SCRATCH_SPARE", 4)

    # Every session this MCP holds (open files and warm scratch sessions) has its
    # lease renewed this often while the MCP runs (lease_renewal.py), so the
    # server's abandoned-lease rule (ISABELLE_MAX_LEASE_AGE) closes only sessions
    # of a client that died. Keep it well below that age. 0 disables. Warm
    # scratch sessions are let go on memory pressure (warning/critical reminder)
    # or isabelle_release_idle, not after an idle time.
    LEASE_RENEW_S: float = env_float(_P, "LEASE_RENEW_S", 60.0)
    # A held session counts as "idle" for memory advice and reminders after
    # this many seconds without a tool call.
    ADVICE_IDLE_S: int = env_int(_P, "ADVICE_IDLE_S", 120)

    # When true, isabelle_close destroys the session (immediate teardown,
    # freeing memory) instead of the default warm release back to the pool.
    # The per-call `destroy` argument on isabelle_close overrides this.
    CLOSE_DESTROYS: bool = env_bool(_P, "CLOSE_DESTROYS", False)

    # fill_sorries (plan 2026-10-18 §1.5): per-sorry sledgehammer budget and the
    # job's wall budget (server-side defaults ISABELLE_FILL_TIMEOUT_S / _MAX_TOTAL_S);
    # how long isabelle_fill_sorries waits before returning the partial state with
    # `continuing: true` — keep PATIENCE_S <= the MCP client's tool timeout - 60 —
    # and the poll interval (each poll touches the registry).
    FILL_TIMEOUT_S: int = env_int(_P, "FILL_TIMEOUT_S", 30)
    FILL_MAX_TOTAL_S: float = env_float(_P, "FILL_MAX_TOTAL_S", 600.0)
    FILL_PATIENCE_S: float = env_float(_P, "FILL_PATIENCE_S", 240.0)
    FILL_POLL_S: float = env_float(_P, "FILL_POLL_S", 3.0)

    # Transport: "stdio" (local) or "streamable-http" (remote).
    TRANSPORT: str = env_str(_P, "TRANSPORT", "stdio")
    HOST: str = env_str(_P, "HOST", "127.0.0.1")
    PORT: int = env_int(_P, "PORT", 8849)
