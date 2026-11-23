"""The gateway JVM gets its heap and GC log from -J arguments of `isabelle scala`.

The launcher default (-J-Xmx4g from ISABELLE_SCALAC_OPTIONS) ran out of heap with
34-64 live sessions; the user settings file does not reach this JVM.
"""
from pathlib import Path

from server.app.core.config import Memory, Repl
from server.repl.src.python.repl_backend_gateway import gateway_jvm_args


def test_heap_options_follow_config(tmp_path: Path) -> None:
    args = gateway_jvm_args(tmp_path)
    assert f"-J-Xmx{Repl.GATEWAY_JVM_HEAP_MB}m" in args
    assert f"-J-Xms{Repl.GATEWAY_JVM_MIN_HEAP_MB}m" in args
    assert all(a.startswith("-J") for a in args)


def test_gc_log_is_absolute_and_per_process(tmp_path: Path) -> None:
    (gc,) = [a for a in gateway_jvm_args(tmp_path) if a.startswith("-J-Xlog:gc")]
    path = gc.split(":file=", 1)[1].split(":", 1)[0]
    assert Path(path).is_absolute() and path.endswith("gateway-gc-%p.log")


def test_capacity_check_uses_the_same_heap() -> None:
    assert Memory.JVM_HEAP_MB == Repl.GATEWAY_JVM_HEAP_MB
