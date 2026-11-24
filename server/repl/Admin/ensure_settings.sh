#!/usr/bin/env bash
#
# DESCRIPTION: ensure the Isabelle USER settings file carries the two entries
# every Isabelle-launched process of the Isabelle Pool Server needs. Shared by
# the container entrypoint (every start) and deploy/native_setup.sh.
#
#   1. ML heap cap — ML_OPTIONS with --maxheap, so no single poly process
#      (REPL session or `isabelle build` child) can grow until the OOM killer
#      fires; it fails with an ML exception instead. This MUST live in the
#      user settings file: the polyml component's etc/settings forces
#      ML_OPTIONS="", clobbering any environment/.env value, and a non-empty
#      ML_OPTIONS then wins over ML_OPTIONS32/64 (src/Pure/ML/ml_settings.scala).
#      The override is a full replacement, so the platform defaults
#      (--minheap, --enablegcsharing) are restated explicitly.
#   2. JVM GC logging for every Isabelle-launched JVM (the gateway above all).
#      Same trap: env vars (ISABELLE_TOOL_JAVA_OPTIONS, JAVA_TOOL_OPTIONS) are
#      clobbered or filtered by the toolchain's settings evaluation
#      (docs/ISSUES.md Bug 11), so the settings file is the only reliable
#      injection point. %p keeps each JVM's log separate (gateway vs build tools).
#
# Idempotent: an entry already present (including a hand-tuned one) is left
# untouched — operator tuning wins. Knobs:
#   ISABELLE_ML_MAXHEAP_MB    cap in MB (default 16384)
#   ISABELLE_SERVER_LOG_DIR   GC log directory (default logs/; a relative path
#                             is resolved against the repo root)
#   ISABELLE_HOME             Isabelle installation (default /opt/isabelle;
#                             falls back to `isabelle` on PATH)

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$( cd -- "${SCRIPT_DIR}/../../.." &> /dev/null && pwd )"

ISABELLE="${ISABELLE_HOME:-/opt/isabelle}/bin/isabelle"
[[ -x "$ISABELLE" ]] || ISABELLE="$(command -v isabelle || true)"
[[ -n "$ISABELLE" ]] || { echo "ensure_settings: isabelle not found (set ISABELLE_HOME or put it on PATH)" >&2; exit 1; }

MAXHEAP_MB="${ISABELLE_ML_MAXHEAP_MB:-16384}"
LOG_DIR="${ISABELLE_SERVER_LOG_DIR:-logs}"
[[ "$LOG_DIR" = /* ]] || LOG_DIR="${REPO_ROOT}/${LOG_DIR}"

ISABELLE_HOME_USER="$("$ISABELLE" getenv -b ISABELLE_HOME_USER 2>/dev/null || echo "${HOME}/.isabelle")"
SETTINGS="${ISABELLE_HOME_USER}/etc/settings"
mkdir -p "$(dirname "$SETTINGS")" "$LOG_DIR"

if grep -qs -- "--maxheap" "$SETTINGS"; then
  echo "ensure_settings: ML heap cap already present in $SETTINGS, leaving it"
else
  cat >> "$SETTINGS" <<EOT

# Isabelle Pool Server (Admin/ensure_settings.sh): hard per-process ML heap cap.
# One pathological theory must fail gracefully (ML exception -> Build FAILED)
# instead of OOM-killing the host/container. Full replacement of the platform
# default, so --minheap/--enablegcsharing are restated explicitly.
ML_OPTIONS="--minheap 500 --enablegcsharing --maxheap ${MAXHEAP_MB}"
EOT
  echo "ensure_settings: wrote ML heap cap (--maxheap ${MAXHEAP_MB}) to $SETTINGS"
fi

if grep -qs -- "-Xlog:gc" "$SETTINGS"; then
  echo "ensure_settings: JVM GC logging already configured in $SETTINGS, leaving it"
else
  cat >> "$SETTINGS" <<EOT

# Isabelle Pool Server (Admin/ensure_settings.sh): GC logging for every
# Isabelle-launched JVM (gateway, build tools). The JVM's own output is the only
# source of truth for GC storms — the 2026-09-10 slowdown was undiagnosable
# without it.
ISABELLE_TOOL_JAVA_OPTIONS="\$ISABELLE_TOOL_JAVA_OPTIONS -Xlog:gc*:file=${LOG_DIR}/isabelle-jvm-gc-%p.log:time,uptime,level,tags:filecount=3,filesize=10M"
EOT
  echo "ensure_settings: wrote JVM GC logging (-Xlog:gc, ${LOG_DIR}/isabelle-jvm-gc-%p.log) to $SETTINGS"
fi
