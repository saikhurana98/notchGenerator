#!/usr/bin/env bash
# Provision a Debian host to run the notchgen portal. Idempotent: safe to re-run.
#
#   ssh root@<host> 'bash -s' < deploy/bootstrap.sh
#
# Installs uv, the systemd unit, the nginx site, and the narrow sudo rule the deploy
# script needs. It does not fetch the application — deploy.sh does that, either from the
# CI runner or by hand.
set -euo pipefail

USER_NAME=notchgen
HOME_DIR=/opt/notchgen
DATA_DIR=/srv/notchgen-data
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl rsync nginx ca-certificates sudo >/dev/null

echo "==> service account"
id "$USER_NAME" >/dev/null 2>&1 ||
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /bin/bash "$USER_NAME"
install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR" "$HOME_DIR/app" "$DATA_DIR"

echo "==> uv"
if [ ! -x "$HOME_DIR/.local/bin/uv" ]; then
  sudo -u "$USER_NAME" env HOME="$HOME_DIR" sh -c \
    'curl -LsSf https://astral.sh/uv/install.sh | sh' >/dev/null
fi
"$HOME_DIR/.local/bin/uv" --version

echo "==> systemd unit"
install -m 644 "$HERE/notchgen.service" /etc/systemd/system/notchgen.service
systemctl daemon-reload
systemctl enable notchgen >/dev/null

# The deploy runs as notchgen and needs exactly one privileged verb: restarting the unit.
echo "==> sudo rule"
cat > /etc/sudoers.d/notchgen <<EOF
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl restart notchgen, /usr/bin/systemctl status notchgen
EOF
chmod 440 /etc/sudoers.d/notchgen
visudo -cf /etc/sudoers.d/notchgen >/dev/null

echo "==> nginx"
install -m 644 "$HERE/nginx.conf" /etc/nginx/sites-available/notchgen
ln -sfn /etc/nginx/sites-available/notchgen /etc/nginx/sites-enabled/notchgen
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==> done. deploy.sh can now publish a release."
