# Installation by hand

The README's [Setup](../README.md#setup) section installs the Isabelle Pool Server with one
command per path (`deploy/setup.sh` for Docker, `deploy/native_setup.sh` for a Linux host).
This document is what those scripts do, step by step, for when you want to do it by hand,
adapt it to another platform, or understand what went wrong. Every section maps to one
script step; the scripts are the authoritative version.

Sizing for both paths: ~30 GB disk (Isabelle + heaps), 16 GB+ RAM. Each Isabelle session
is 1.5–4 GB resident; `ISABELLE_POOL_SIZE × ~4 GB` plus one `isabelle build` must fit.
Isabelle version: `Isabelle2026` (the 2026 release candidates also build; `Isabelle2025-2`
and earlier do not, the PIDE header API changed in 2026-RC1). Heaps are version-locked —
never share a user-data directory or volume
between two Isabelle versions.

---

## A. Docker path (`deploy/setup.sh`)

Works on x86-64 and ARM64 Linux, and on Docker Desktop (macOS/Windows) for development.
The build picks the Isabelle tarball for the host architecture. Nothing but Docker is
installed on the host.

### A1. Install Docker (`--install-docker`)

```bash
docker --version && docker compose version   # both present? skip this step
```

Ubuntu/Debian (what `--install-docker` runs):

```bash
sudo apt-get install -y git curl ca-certificates gnupg
sudo install -m 0755 -d /etc/apt/keyrings
. /etc/os-release
curl -fsSL "https://download.docker.com/linux/$ID/gpg" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"   # then log out and back in
```

Other platforms: <https://docs.docker.com/engine/install/>. The legacy `docker-compose` v1
binary also works — substitute it in every command.

### A2. Configure `.env`

```bash
cp .env.example .env
python3 -c 'import secrets; print("ISABELLE_ADMIN_TOKEN=" + secrets.token_hex(16))'   # paste into .env
```

`.env` is loaded into the container via `env_file`; `.env.example` documents every knob.
The ones to check first:

```bash
ISABELLE_POOL_SIZE=3              # max concurrent Isabelle sessions (each ~1.5-4 GB)
ISABELLE_INITIAL_SESSIONS=0       # sessions pre-warmed at startup (0 = fast startup)
ISABELLE_MEMORY_PRESSURE_THRESHOLD=80.0   # refuse new sessions above this used-%
ISABELLE_ML_MAXHEAP_MB=16384      # hard per-process ML heap cap (Poly/ML --maxheap; x86_64_32 allows 32 GiB)
ISABELLE_ADMIN_TOKEN=...          # admin console / admin endpoints credential
```

Changing `.env` later requires recreating the container, not just restarting it:
`docker compose up -d --force-recreate isabelle-pool-server`.

### A3. Build and start

```bash
docker compose build isabelle-pool-server    # Isabelle download + backend build, 10-30 min first time
docker compose up -d isabelle-pool-server    # entrypoint: component registration -> ML heap cap -> server
curl http://localhost:8000/healthz           # {"status":"alive"}  (allow 1-2 min for the gateway JVM)
curl http://localhost:8000/                  # full health: version, gateway_alive, pool, memory
```

What the image build does (`deploy/Dockerfile`): `python:3.12-slim` + the packages in
`deploy/apt_packages.txt` (fontconfig is required by the Isabelle JVM even headless) →
Isabelle tarball (`ISABELLE_VERSION` from `.env`) for the build architecture, trying the
mirrors in `deploy/isabelle_mirrors.txt` in order and abandoning any under 1 MB/s → Python deps from
`pyproject.toml` (runtime + `mcp` extra) → `server/repl/Admin/init` (registers the REPL
component, fetches the py4j/spliff contribs) → `isabelle scala_build` (compiles the Scala
backend into `server/repl/lib/repl.jar`; Isabelle does this itself, no Gradle/JDK needed).

What the container entrypoint does on every start (`server/repl/Admin/container_entrypoint.sh`):
re-runs `init` (a no-op when the `isabelle_user_data` volume is already registered —
[ISSUES.md](ISSUES.md) Bug 7), runs `ensure_settings.sh` (ML heap cap + JVM GC logging in
the Isabelle user settings), then `exec python -m server.app.main`.

The version comes from `ISABELLE_VERSION` in `.env` (else `.env.example`); `./deploy/setup.sh
--isabelle-version <V>` writes it there before building. The script refuses `--no-build` when
the image was built for another version (the image carries it as a label), and after the
start it asks the container's launcher (`isabelle version`) and stops if the answer differs.
By hand, another version is one build arg away:

```bash
docker build -f deploy/Dockerfile --build-arg ISABELLE_VERSION=Isabelle2026-RC2 \
  -t isabelle-pool-server:2026rc2 .
```

### A4. Prebuild heaps (`--build-heaps "..."`)

With `ISABELLE_INITIAL_SESSIONS=0` the first session request pays session creation (~1 min).
If your theories import heavy sessions (`HOL-Library`, `HOL-Analysis`,
`HOL-Computational_Algebra`, …), build their heaps once so sessions and big-step
verification start from a cached image. Heaps persist in the `isabelle_user_data` volume.

```bash
docker compose exec isabelle-pool-server isabelle build -b HOL-Library
# HOL-Analysis-scale heaps: build with the server idle (they share the 14 GB cgroup cap)
curl http://localhost:8000/api/v1/heaps/available
```

### A5. Operating notes

- **Logs:** `docker compose logs -f isabelle-pool-server` (server stdout); the rotating file
  log (10 MB × 5) is also at `logs/server.log` on the host (the repo is mounted at `/app`);
  gateway JVM output in `logs/gateway-jvm.log`; setup output in `logs/setup.log`.
- **Memory limit:** `mem_limit: 14g` in `docker-compose.yml` — must stay **below** the Docker
  VM's own memory or the cgroup-aware admission gate goes blind. On smaller machines lower it
  *and* `ISABELLE_POOL_SIZE`; the gate refuses new sessions (HTTP 503) near the limit instead
  of letting the OOM killer take the JVM.
- **ML heap cap:** the entrypoint writes `ML_OPTIONS="--minheap 500 --enablegcsharing
  --maxheap $ISABELLE_ML_MAXHEAP_MB"` into the Isabelle user settings on the volume (default
  9 GB) so a single runaway poly process fails cleanly instead of OOM-killing the container.
- **Unit tests in the container:** the image ships the runtime and `mcp` dependencies only, so
  `docker compose exec isabelle-pool-server pip install pytest` once per container, then
  `docker compose exec isabelle-pool-server python -m pytest -q`.
- **Upgrading Isabelle:** set `ISABELLE_VERSION` in `.env` (or pass `--isabelle-version`) and
  run `./deploy/setup.sh`: the image is rebuilt (the Isabelle layer is downloaded again), the
  container recreated, and the entrypoint registers the components under the new
  `~/.isabelle/<version>/` on the volume (it fetches the py4j and spliff contribs once, so the
  first start needs the network). Heaps are per version: the old ones stay on the volume
  unused; rebuild what you need with `--build-heaps`.
- **After an image rebuild**, if the server fails with `Not found: py4j`: the pre-existing
  volume shadows the component registration. The entrypoint re-registers on every start; the
  manual fix is `docker compose exec isabelle-pool-server ./server/repl/Admin/init`.
- **Monitoring:** `docker compose up -d prometheus grafana cadvisor` — Grafana on `:3000`
  (admin/admin, "Isabelle Pool Server" dashboard), Prometheus on `:9090`, raw metrics at
  `/metrics`.
- **Windows checkouts:** the container runs the scripts straight from the bind-mounted
  checkout, so they must stay LF — `.gitattributes` pins that; if you see
  `$'\r': command not found`, run `git add --renormalize .`.

### A6. Turnkey image (no build, heaps included)

For reproducing published results, or handing the service to someone without the repo: a
self-contained image with the heaps and Isabelle settings of a running deployment baked in.

```bash
# maintainer — with the server up and the heaps you want shipped prebuilt (A4):
./deploy/export_turnkey.sh isabelle-pool-server:turnkey --save   # -> isabelle-pool-server-turnkey.tar.gz

# recipient — Docker only (~35 GB disk, 16 GB+ RAM):
docker load < isabelle-pool-server-turnkey.tar.gz
docker run -d --name isabelle-pool-server -p 8000:8000 --memory 14g \
  -e ISABELLE_ADMIN_TOKEN=$(openssl rand -hex 16) isabelle-pool-server:turnkey
curl http://localhost:8000/healthz                 # 1-2 min for the gateway JVM
curl http://localhost:8000/api/v1/heaps/available  # the baked-in heaps
```

The image label `org.opencontainers.image.revision` carries the source commit
(`docker image inspect <tag> --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'`);
cite it in reproduction reports. `deploy/Dockerfile.export` is the recipe (`FROM` the regular
image plus the exported `~/.isabelle` subtree); the historical RC0 hand-assembly is kept in
`archive/rc0-image/`.

---

## B. Native Linux path (`deploy/native_setup.sh`)

Tested shape: Ubuntu 22.04/24.04, a regular user with `sudo`, repo at
`~/Isabelle-Pool-Server`, Isabelle at `/opt/isabelle`. Adjust paths as you like — everything
is driven by `ISABELLE_HOME` and `.env`.

### B1. System packages (`--system`)

```bash
sudo apt-get update
sudo apt-get install -y $(sed -e 's/#.*//' deploy/apt_packages.txt) python3 python3-venv python3-pip
```

`deploy/apt_packages.txt` is the same list the Docker image installs (the host additionally
needs Python itself).

- Python **3.10+** (22.04 ships 3.10, 24.04 ships 3.12).
- No JDK: Isabelle bundles its own and compiles the Scala backend itself. It fails with
  `Unknown JAVA_HOME` if a foreign `JAVA_HOME` is exported globally — keep it out of the
  server's environment.
- `fontconfig` + one font family are **required even though nothing is displayed**:
  Isabelle 2026 initialises the font subsystem on every session start and the JVM dies
  with `Fontconfig head is null` without them.

### B2. Install Isabelle (`--system`, `--isabelle-version`)

```bash
VERSION=$(grep ^ISABELLE_VERSION= .env.example | cut -d= -f2)   # Isabelle2026, or a 2026 release candidate
case "$(uname -m)" in aarch64|arm64) T="${VERSION}_linux_arm.tar.gz";; *) T="${VERSION}_linux.tar.gz";; esac
# release candidates live under website-<version>/dist/, final releases under dist/
curl -fLO "https://isabelle.in.tum.de/website-${VERSION}/dist/${T}" \
  || curl -fLO "https://isabelle.in.tum.de/dist/${T}" \
  || curl -fLO "https://isabelle.sketis.net/website-${VERSION}/dist/${T}"
sudo mkdir -p /opt && sudo tar -xf "$T" -C /opt      # -> /opt/<version>
sudo chown -R "$USER" "/opt/${VERSION}"
sudo ln -s "/opt/${VERSION}" /opt/isabelle              # the one in use; switch by re-pointing it
rm "$T"
```

This is the layout `--system` produces: every version under `/opt/<version>`, `/opt/isabelle` a
symlink to the one in use. A version an older run installed as a plain `/opt/isabelle`
directory is moved to `/opt/<its name>` the first time the script switches away from it.

Make it permanent for your user (the server, the gateway JVM and the heap pool all resolve
the launcher from `ISABELLE_HOME`):

```bash
cat >> ~/.profile <<'EOF'
export ISABELLE_HOME=/opt/isabelle
export PATH="$ISABELLE_HOME/bin:$PATH"
EOF
source ~/.profile
isabelle version          # sanity check
```

The full mirror order (and the 1 MB/s floor rule) is `deploy/isabelle_mirrors.txt`, shared
with the Docker build.

### B3. Python, component, backend, settings

```bash
cd ~/Isabelle-Pool-Server
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[mcp]" -e ./client      # server deps + MCP SDK + async client (pyproject.toml extras)

./server/repl/Admin/init                 # register the REPL component, fetch the py4j/spliff contribs
isabelle scala_build                     # compile the Scala backend (server/repl/lib/repl.jar)
./server/repl/Admin/ensure_settings.sh   # ML heap cap + JVM GC logging in the Isabelle user settings
```

`isabelle scala_build` also runs automatically whenever `isabelle scala` launches the
gateway, so a stale jar after a `git pull` is rebuilt on the next server start; running it
here just surfaces compile errors at setup time. `server/repl/build.gradle` is for IDE
import only and is not part of the install.

The ML heap cap matters: without it one pathological theory can grow a Poly/ML process
until the kernel OOM killer takes down the whole server. It **must** live in the Isabelle
user settings file (`$(isabelle getenv -b ISABELLE_HOME_USER)/etc/settings`), not in the
environment — the polyml component's own `etc/settings` forces `ML_OPTIONS=""`. The script
writes `ML_OPTIONS="--minheap 500 --enablegcsharing --maxheap $ISABELLE_ML_MAXHEAP_MB"`
(default 16384) and leaves an existing `--maxheap` untouched, so hand-tuning wins.

### B4. `.env`

```bash
cp .env.example .env
python3 -c 'import secrets; print("ISABELLE_ADMIN_TOKEN=" + secrets.token_hex(16))'   # paste into .env
```

The same file serves both paths: the path-like defaults (heap-pool dir, allowed heap roots,
log dir) are computed by the server from `HOME` and the checkout, so nothing needs
overriding on a host. Nothing loads `.env` automatically outside Docker — `source` it
(B5) or let systemd's `EnvironmentFile` do it (B6). Value lines must not carry inline
`# comments`; the integer knobs are parsed with `int()`.

### B5. Run

```bash
cd ~/Isabelle-Pool-Server
set -a; source .env; set +a
source .venv/bin/activate
python -m server.app.main              # foreground; Ctrl-C stops it
```

In another shell:

```bash
curl http://localhost:8000/healthz     # {"status":"alive"}   (1-2 min for the gateway JVM)
curl http://localhost:8000/            # gateway_alive: true, pool, memory
```

Memory note: the admission gate reads the cgroup when it runs in a container; on a bare
host it falls back to the machine's `MemTotal`, so `ISABELLE_MEMORY_PRESSURE_THRESHOLD` is a
percentage of **all** host RAM. Keep `ISABELLE_POOL_SIZE × 4 GB + 9 GB` (one capped build)
under the memory you are willing to give it.

### B6. systemd service (`--systemd`)

The unit is the template `deploy/isabelle-pool-server.service`; fill its placeholders and
install it:

```bash
sed -e "s|@USER@|$USER|" -e "s|@REPO@|$PWD|" -e "s|@ISABELLE_HOME@|$ISABELLE_HOME|" \
    -e "s|@PYTHON_DIR@|$PWD/.venv/bin|" -e "s|@PYTHON@|$PWD/.venv/bin/python|" \
    deploy/isabelle-pool-server.service \
  | sudo tee /etc/systemd/system/isabelle-pool-server.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now isabelle-pool-server
systemctl status isabelle-pool-server
journalctl -u isabelle-pool-server -f           # live log (also logs/server.log)
```

`KillMode=control-group` matters: a stop must take the gateway JVM and every `poly`
process with it. `TimeoutStopSec=120` gives the server time to close sessions cleanly.

### B7. Prebuild heaps (optional)

Same reasoning as A4. Build with the server **stopped or idle** — a `HOL-Analysis` build
needs several GB on its own:

```bash
isabelle build -b HOL-Library
isabelle build -b -j 2 HOL-Analysis
curl http://localhost:8000/api/v1/heaps/available
```

### B8. Operating notes

- **Logs:** `logs/server.log` (rotating, 10 MB × 5), `logs/gateway-jvm.log` (JVM
  stdout/stderr), `journalctl -u isabelle-pool-server`, setup output in `logs/native_setup.log`.
- **Upgrading the code:** `git pull`, then `./deploy/native_setup.sh` (re-installs deps,
  recompiles the backend; add `--systemd` to restart the service) — or by hand
  `pip install -e ".[mcp]"`, `isabelle scala_build`, `sudo systemctl restart isabelle-pool-server`.
- **Upgrading Isabelle:** `./deploy/native_setup.sh --system --isabelle-version <V>` (add
  `--systemd` to restart the service): the new version goes to `/opt/<V>`, `/opt/isabelle` is
  re-pointed, the old version stays at `/opt/<old>`, and the script registers the component,
  recompiles the backend and writes the settings under `~/.isabelle/<V>/`, which are
  per-version. Heaps are per version too: rebuild what the service uses
  (`isabelle build -b HOL-Library`); the old ones stay under `~/.isabelle/<old>/`. To go back,
  run the same command with the old version.
- **Stuck processes:** `pgrep -af "poly|isabelle"` — after `systemctl stop isabelle-pool-server`
  nothing should remain; if it does, the gateway did not get the signal (check `KillMode`).
- **Gateway fails to start:** run `./server/repl/Admin/init` again and check
  `isabelle components -l`; compile errors show up in `isabelle scala_build`.
