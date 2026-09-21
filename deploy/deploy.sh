#!/usr/bin/env bash
# Put the checkout in this directory live, and roll back if it will not serve.
#
# Run by the GitHub Actions self-hosted runner from the root of a fresh checkout. The
# previous release is kept next door until the new one answers /healthz, so a release that
# installs cleanly but cannot start does not take the portal down with it.
#
# The service is stopped around every file swap. It has Restart=always, so replacing the
# directory under a running unit sends systemd into a restart loop against half-present
# files, and it latches at the start limit — after which even a good release will not
# start until the failure is reset. That is what `systemctl reset-failed` below is for.
set -euo pipefail

APP=/opt/notchgen/app
PREV=/opt/notchgen/app.prev
ENV=/opt/notchgen/env
ENV_PREV=/opt/notchgen/env.prev
UV=/opt/notchgen/.local/bin/uv
PORT=8000

# Every verb here has to match /etc/sudoers.d/notchgen argument for argument.
unit() { sudo /usr/bin/systemctl "$1" notchgen; }

healthy() {
  for _ in $(seq 1 40); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

bring_up() {
  unit reset-failed || true
  unit start
  healthy
}

revision=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
version=$(date +%Y.%m.%d)-${revision}
echo "==> deploying ${version}"

mkdir -p "$APP"
rm -rf "$PREV" "$ENV_PREV"
[ -d "$APP/src" ] && cp -a "$APP" "$PREV"
# The stamp is read as an EnvironmentFile, so it has to be written before the unit starts.
# Snapshot it too, or a rollback leaves /api/version advertising the release that just
# failed while the portal is in fact serving the old one.
[ -f "$ENV" ] && cp -a "$ENV" "$ENV_PREV"

unit stop || true

rsync -a --delete \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude '.pytest_cache' \
  ./ "$APP/"

cd "$APP"
"$UV" sync --frozen --no-dev

printf 'NOTCHGEN_VERSION=%s\nNOTCHGEN_REVISION=%s\n' "$version" "$revision" > "$ENV"

if bring_up; then
  echo "==> live: $(curl -fsS "http://127.0.0.1:${PORT}/api/version" || echo '?')"
  rm -rf "$PREV" "$ENV_PREV"
  exit 0
fi

echo "!! ${version} did not answer /healthz" >&2
sudo /usr/bin/journalctl -u notchgen -n 30 --no-pager >&2 || true

if [ ! -d "$PREV/src" ]; then
  echo "!! no previous release to fall back to" >&2
  exit 1
fi

echo "==> rolling back" >&2
unit stop || true
rm -rf "$APP"
mv "$PREV" "$APP"
[ -f "$ENV_PREV" ] && mv "$ENV_PREV" "$ENV"
if bring_up; then
  echo "==> rolled back; the portal is serving the previous release" >&2
else
  echo "!! rollback also failed to serve" >&2
fi
exit 1
