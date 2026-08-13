#!/usr/bin/env bash
#
# Set up and start notchgen.
#
# By default this builds the Docker image and starts the portal. Pass --local to run
# straight from a uv virtualenv instead, with no Docker involved.
#
# Anything needing root is announced before it runs, and nothing is enabled at boot
# unless you pass --enable-docker.

set -euo pipefail

PORT="${NOTCHGEN_PORT:-8000}"
MODE="docker"
ENABLE_DOCKER=0
DETACH=1
REBUILD=0

readonly REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

step()  { printf '%s==>%s %s\n' "$BLUE$BOLD" "$RESET$BOLD" "$*$RESET"; }
info()  { printf '    %s\n' "$*"; }
note()  { printf '    %s%s%s\n' "$DIM" "$*" "$RESET"; }
warn()  { printf '    %s! %s%s\n' "$YELLOW" "$*" "$RESET"; }
ok()    { printf '    %s* %s%s\n' "$GREEN" "$*" "$RESET"; }
die()   { printf '\n%serror:%s %s\n' "$RED$BOLD" "$RESET" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
${BOLD}notchgen installer${RESET}

  ./install.sh [options]

Options:
  --local            Run from a uv virtualenv instead of Docker.
  --port N           Port to serve on (default 8000, or \$NOTCHGEN_PORT).
  --rebuild          Force a fresh image build, ignoring the Docker cache.
  --foreground       Stay attached and stream logs (Docker mode).
  --enable-docker    Also enable the Docker service at boot.
  -h, --help         Show this message.

Examples:
  ./install.sh                     build the image and start on :8000
  ./install.sh --port 9000         same, on :9000
  ./install.sh --local             no Docker, just uv + uvicorn
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local)         MODE="local"; shift ;;
    --port)          PORT="${2:?--port needs a number}"; shift 2 ;;
    --port=*)        PORT="${1#*=}"; shift ;;
    --rebuild)       REBUILD=1; shift ;;
    --foreground)    DETACH=0; shift ;;
    --enable-docker) ENABLE_DOCKER=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *)               die "unknown option: $1 (try --help)" ;;
  esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT > 0 && PORT < 65536 )) || die "not a valid port: $PORT"

cd "$REPO_DIR"

wait_for_health() {
  local url="http://127.0.0.1:${PORT}/healthz"
  step "Waiting for the portal to answer"
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      ok "healthy at $url"
      return 0
    fi
    sleep 1
  done
  return 1
}

announce_done() {
  printf '\n%s  notchgen is running:%s  %shttp://localhost:%s%s\n\n' \
    "$GREEN$BOLD" "$RESET" "$BOLD" "$PORT" "$RESET"
}

# ---------------------------------------------------------------- local mode

run_local() {
  step "Checking for uv"
  if ! command -v uv >/dev/null 2>&1; then
    die "uv is not installed. Get it with:
       curl -LsSf https://astral.sh/uv/install.sh | sh
     or:
       sudo pacman -S uv"
  fi
  ok "$(uv --version)"

  step "Installing dependencies"
  uv sync --frozen
  ok "virtualenv ready at .venv"

  step "Running the test suite"
  if uv run pytest -q; then
    ok "tests pass"
  else
    warn "tests failed — starting anyway, but treat results with suspicion"
  fi

  step "Starting the portal on port $PORT"
  note "press Ctrl-C to stop"
  printf '\n'
  exec uv run uvicorn notchgen.api:app --host 0.0.0.0 --port "$PORT"
}

[[ "$MODE" == "local" ]] && run_local

# ---------------------------------------------------------------- docker mode

step "Checking for Docker"
command -v docker >/dev/null 2>&1 || die "Docker is not installed.
     Arch/CachyOS:  sudo pacman -S docker docker-compose
     Debian/Ubuntu: sudo apt install docker.io docker-compose-v2
     or see https://docs.docker.com/engine/install/"
ok "$(docker --version)"

# Sudo is only reached for if the daemon is down or the socket is not ours.
SUDO=()
need_sudo() {
  if [[ ${#SUDO[@]} -gt 0 ]]; then return 0; fi
  command -v sudo >/dev/null 2>&1 || die "need root to continue, but sudo is not installed"
  SUDO=(sudo)
}

if ! docker info >/dev/null 2>&1; then
  step "The Docker daemon is not reachable"

  if command -v systemctl >/dev/null 2>&1 && \
     [[ "$(systemctl is-active docker 2>/dev/null || true)" != "active" ]]; then
    warn "the docker service is not running; starting it needs root"
    need_sudo
    "${SUDO[@]}" systemctl start docker
    ok "docker service started"
    if (( ENABLE_DOCKER )); then
      "${SUDO[@]}" systemctl enable docker
      ok "docker service enabled at boot"
    else
      note "it will stop again on reboot; pass --enable-docker to make it permanent"
    fi
    sleep 2
  fi

  if ! docker info >/dev/null 2>&1; then
    # The daemon is up but this user cannot reach the socket.
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
      warn "$USER is not in the 'docker' group, so this run needs sudo"
      need_sudo
      "${SUDO[@]}" usermod -aG docker "$USER"
      ok "added $USER to the 'docker' group"
      note "log out and back in (or run: newgrp docker) to use Docker without sudo"
    else
      need_sudo
    fi
    "${SUDO[@]}" docker info >/dev/null 2>&1 || die "still cannot talk to the Docker daemon"
  fi
fi
[[ ${#SUDO[@]} -eq 0 ]] && ok "daemon reachable as $USER"

step "Checking for Docker Compose"
if "${SUDO[@]}" docker compose version >/dev/null 2>&1; then
  COMPOSE=("${SUDO[@]}" docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=("${SUDO[@]}" docker-compose)
else
  die "Docker Compose is not available.
     Arch/CachyOS:  sudo pacman -S docker-compose
     Debian/Ubuntu: sudo apt install docker-compose-v2"
fi
ok "$("${COMPOSE[@]}" version --short 2>/dev/null || echo 'compose available')"

export NOTCHGEN_PORT="$PORT"

if "${SUDO[@]}" docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ":${PORT}->"; then
  warn "something is already published on port $PORT; replacing it"
  "${COMPOSE[@]}" down --remove-orphans || true
fi

step "Building the image"
note "first build pulls the base image and can take a few minutes"
if (( REBUILD )); then
  "${COMPOSE[@]}" build --no-cache
else
  "${COMPOSE[@]}" build
fi
ok "image built"

step "Starting the service on port $PORT"
if (( DETACH )); then
  "${COMPOSE[@]}" up -d --remove-orphans
  if wait_for_health; then
    announce_done
    info "logs:  ${COMPOSE[*]} logs -f"
    info "stop:  ${COMPOSE[*]} down"
    printf '\n'
  else
    warn "the container started but never became healthy. Recent logs:"
    printf '\n'
    "${COMPOSE[@]}" logs --tail 40
    die "portal did not come up on port $PORT"
  fi
else
  note "press Ctrl-C to stop"
  printf '\n'
  exec "${COMPOSE[@]}" up --remove-orphans
fi
