#!/usr/bin/env bash
# Put the checkout in this directory live, and roll back if it will not serve.
#
# Run by the GitHub Actions self-hosted runner from the root of a fresh checkout. The
# previous release is kept next door until the new one answers /healthz, so a deploy that
# builds fine but refuses to start does not take the portal down with it.
set -euo pipefail

APP=/opt/notchgen/app
PREV=/opt/notchgen/app.prev
UV=/opt/notchgen/.local/bin/uv
PORT=8000

revision=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
version=$(date +%Y.%m.%d)-${revision}
echo "==> deploying ${version}"

mkdir -p "$APP"
rm -rf "$PREV"
if [ -d "$APP/src" ]; then
  cp -a "$APP" "$PREV"
fi

rsync -a --delete \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude '.pytest_cache' \
  ./ "$APP/"

cd "$APP"
"$UV" sync --frozen --no-dev

printf 'NOTCHGEN_VERSION=%s\nNOTCHGEN_REVISION=%s\n' "$version" "$revision" > /opt/notchgen/env

restart_and_check() {
  sudo /usr/bin/systemctl restart notchgen
  for _ in $(seq 1 40); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

if restart_and_check; then
  served=$(curl -fsS "http://127.0.0.1:${PORT}/api/version" || echo '{}')
  echo "==> live: ${served}"
  rm -rf "$PREV"
  exit 0
fi

echo "!! new release did not answer /healthz" >&2
sudo /usr/bin/systemctl status notchgen --no-pager --lines 30 >&2 || true

if [ -d "$PREV/src" ]; then
  echo "==> rolling back to the previous release" >&2
  rm -rf "$APP"
  mv "$PREV" "$APP"
  if restart_and_check; then
    echo "==> rolled back; the portal is serving the previous release" >&2
  else
    echo "!! rollback also failed to serve" >&2
  fi
fi
exit 1
