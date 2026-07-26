#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="nft-forward-panel"
INSTALL_DIR="/opt/$APP_NAME"
DATA_DIR="/var/lib/$APP_NAME"
ENV_FILE="/etc/$APP_NAME.env"
SERVICE_FILE="/etc/systemd/system/$APP_NAME.service"
CADDY_SNIPPET="/etc/caddy/conf.d/$APP_NAME.caddy"
REPOSITORY="${NFP_REPOSITORY:-}"
RELEASE_VERSION="latest"
DOMAIN=""
EMAIL=""
ADMIN_USERNAME="admin"
ADMIN_PASSWORD="${NFP_ADMIN_PASSWORD:-}"
NON_INTERACTIVE=0
SOURCE_DIR=""
TEMP_DIR=""

log() { printf '\033[1;32m[nfp]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[nfp]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[nfp] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
cleanup() { [[ -z "$TEMP_DIR" || ! -d "$TEMP_DIR" ]] || rm -rf -- "$TEMP_DIR"; }
trap cleanup EXIT

usage() {
    cat <<'EOF'
Usage: sudo bash install.sh [options]

  --domain DOMAIN       HTTPS domain, e.g. panel.example.com
  --email EMAIL         ACME certificate notification email
  --admin USERNAME      Initial administrator username (default: admin)
  --repo OWNER/REPO     GitHub repository providing Releases
  --version TAG         Release tag to install (default: latest)
  --non-interactive     Do not prompt; require all values via options/env
  -h, --help            Show this help

For non-interactive first installation, pass the password through the
NFP_ADMIN_PASSWORD environment variable instead of a command-line argument.
EOF
}

while (($#)); do
    case "$1" in
        --domain) [[ $# -ge 2 ]] || die "--domain requires a value"; DOMAIN="$2"; shift 2 ;;
        --email) [[ $# -ge 2 ]] || die "--email requires a value"; EMAIL="$2"; shift 2 ;;
        --admin) [[ $# -ge 2 ]] || die "--admin requires a value"; ADMIN_USERNAME="$2"; shift 2 ;;
        --repo) [[ $# -ge 2 ]] || die "--repo requires OWNER/REPO"; REPOSITORY="$2"; shift 2 ;;
        --version) [[ $# -ge 2 ]] || die "--version requires a tag"; RELEASE_VERSION="$2"; shift 2 ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "run this installer as root"
[[ -r /etc/os-release ]] || die "/etc/os-release is missing"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in debian|ubuntu) ;; *) die "only Debian and Ubuntu are supported" ;; esac
command -v apt-get >/dev/null || die "apt-get is required"

if [[ $NON_INTERACTIVE -eq 0 ]]; then
    [[ -t 0 ]] || die "interactive mode needs a terminal; download the script before running it"
    read -r -e -p "Panel domain (e.g. panel.example.com): " DOMAIN
    read -r -e -p "Certificate email: " EMAIL
    read -r -e -p "Initial administrator username [admin]: " input_admin
    ADMIN_USERNAME="${input_admin:-admin}"
fi

new_install=0
[[ -f "$DATA_DIR/panel.db" ]] || new_install=1
if [[ $new_install -eq 1 && -z "$ADMIN_PASSWORD" && $NON_INTERACTIVE -eq 0 ]]; then
    while :; do
        read -r -s -p "Initial administrator password (12-256 characters): " ADMIN_PASSWORD; printf '\n'
        read -r -s -p "Confirm password: " confirmation; printf '\n'
        [[ "$ADMIN_PASSWORD" == "$confirmation" ]] || { warn "passwords do not match"; continue; }
        ((${#ADMIN_PASSWORD} >= 12 && ${#ADMIN_PASSWORD} <= 256)) || { warn "password must be 12-256 characters"; continue; }
        [[ "$ADMIN_PASSWORD" != *$'\n'* && "$ADMIN_PASSWORD" != *$'\r'* ]] || { warn "password cannot contain a newline"; continue; }
        break
    done
fi

DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%%/*}"
[[ "$DOMAIN" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]] || die "invalid domain: $DOMAIN"
[[ "$EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || die "invalid email: $EMAIL"
[[ "$ADMIN_USERNAME" =~ ^[A-Za-z0-9_.-]{3,32}$ ]] || die "administrator username must be 3-32 letters, numbers, _, - or ."
if [[ $new_install -eq 1 ]]; then
    ((${#ADMIN_PASSWORD} >= 12 && ${#ADMIN_PASSWORD} <= 256)) || die "an initial password of 12-256 characters is required"
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -f "$SCRIPT_DIR/app.py" && -f "$SCRIPT_DIR/requirements.txt" ]]; then
    SOURCE_DIR="$SCRIPT_DIR"
    log "using local source tree: $SOURCE_DIR"
else
    [[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "specify the GitHub repository with --repo OWNER/REPO"
fi

log "installing system dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gpg openssl nftables python3 python3-venv sqlite3
if ! command -v caddy >/dev/null; then
    if ! apt-get install -y caddy; then
        log "enabling the official Caddy package repository"
        apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
        key_tmp=$(mktemp)
        curl -1fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key -o "$key_tmp"
        gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg "$key_tmp"
        rm -f "$key_tmp"
        curl -1fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
            -o /etc/apt/sources.list.d/caddy-stable.list
        chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
        apt-get update
        apt-get install -y caddy
    fi
fi

if [[ -z "$SOURCE_DIR" ]]; then
    TEMP_DIR=$(mktemp -d)
    if [[ "$RELEASE_VERSION" == "latest" ]]; then
        release_url="https://github.com/$REPOSITORY/releases/latest/download"
    else
        release_url="https://github.com/$REPOSITORY/releases/download/$RELEASE_VERSION"
    fi
    log "downloading $REPOSITORY release $RELEASE_VERSION"
    curl -fL --retry 3 -o "$TEMP_DIR/$APP_NAME.tar.gz" "$release_url/$APP_NAME.tar.gz" \
        || die "release archive download failed; confirm the repository is public and has a published GitHub Release"
    if [[ "$RELEASE_VERSION" == "latest" ]]; then
        release_api="https://api.github.com/repos/$REPOSITORY/releases/latest"
    else
        release_api="https://api.github.com/repos/$REPOSITORY/releases/tags/$RELEASE_VERSION"
    fi
    expected_digest=$(curl -fsSL "$release_api" | python3 -c \
        'import json, sys; name=sys.argv[1]; data=json.load(sys.stdin); print(next((a.get("digest", "") for a in data.get("assets", []) if a.get("name") == name), ""))' \
        "$APP_NAME.tar.gz")
    [[ "$expected_digest" =~ ^sha256:[0-9a-fA-F]{64}$ ]] \
        || die "GitHub did not return a SHA-256 digest for the release archive"
    actual_digest="sha256:$(sha256sum "$TEMP_DIR/$APP_NAME.tar.gz" | awk '{print $1}')"
    [[ "$actual_digest" == "$expected_digest" ]] \
        || die "release archive SHA-256 verification failed"
    log "release archive SHA-256 verified"
    tar -xzf "$TEMP_DIR/$APP_NAME.tar.gz" -C "$TEMP_DIR"
    SOURCE_DIR="$TEMP_DIR/$APP_NAME"
    [[ -f "$SOURCE_DIR/app.py" && -f "$SOURCE_DIR/deploy/$APP_NAME.service" ]] || die "release archive has an unexpected layout"
fi

timestamp=$(date +%Y%m%d-%H%M%S)
if [[ -d "$INSTALL_DIR" ]]; then
    cp -a "$INSTALL_DIR" "$INSTALL_DIR.backup-$timestamp"
fi
install -d -m 0755 "$INSTALL_DIR"
tar -C "$SOURCE_DIR" --exclude=.git --exclude=.venv --exclude=.preview-data \
    --exclude=__pycache__ --exclude='*.pyc' --exclude='*.db*' -cf - . | tar -C "$INSTALL_DIR" -xf -
chown -R root:root "$INSTALL_DIR"
chmod 0755 "$INSTALL_DIR/install.sh" "$INSTALL_DIR/nft.sh" "$INSTALL_DIR/nfpctl.py"

log "creating Python environment"
rm -rf "$INSTALL_DIR/.venv"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"
"$INSTALL_DIR/.venv/bin/pip" check

install -d -o root -g root -m 0700 "$DATA_DIR" "$DATA_DIR/avatars"
secret_key=""
if [[ -f "$ENV_FILE" ]]; then
    cp -a "$ENV_FILE" "$ENV_FILE.backup-$timestamp"
    secret_key=$(sed -n 's/^PANEL_SECRET_KEY=//p' "$ENV_FILE" | head -n 1)
fi
secret_key="${secret_key:-$(openssl rand -hex 32)}"
env_escape() { local value=${1//\\/\\\\}; value=${value//\"/\\\"}; printf '"%s"' "$value"; }
umask 077
{
    printf 'PANEL_SECRET_KEY=%s\n' "$secret_key"
    if [[ $new_install -eq 1 ]]; then
        printf 'PANEL_ADMIN_USERNAME='; env_escape "$ADMIN_USERNAME"; printf '\n'
        printf 'PANEL_ADMIN_PASSWORD='; env_escape "$ADMIN_PASSWORD"; printf '\n'
    fi
    cat <<EOF
PANEL_DATA_DIR=$DATA_DIR
PANEL_FORWARD_CONFIG=/etc/nftables.d/port-forward.conf
PANEL_MAIN_CONFIG=/etc/nftables.conf
PANEL_SYSCTL_CONFIG=/etc/sysctl.d/99-nft-forward.conf
PANEL_COOKIE_SECURE=1
PANEL_TRUSTED_PROXY_COUNT=1
PANEL_POLICY_SCHEDULER=1
PANEL_LOGIN_WINDOW_SECONDS=600
PANEL_LOGIN_MAX_ATTEMPTS=20
EOF
} > "$ENV_FILE"
chmod 0600 "$ENV_FILE"

install -d -m 0755 /etc/nftables.d
[[ -f /etc/nftables.conf ]] || die "/etc/nftables.conf is missing after nftables installation"
cp -a /etc/nftables.conf "/etc/nftables.conf.backup-$timestamp"
if ! grep -Eq '^[[:space:]]*include[[:space:]]+"/etc/nftables\.d/\*\.conf"' /etc/nftables.conf; then
    printf '\ninclude "/etc/nftables.d/*.conf"\n' >> /etc/nftables.conf
fi
if ! nft -c -f /etc/nftables.conf; then
    cp -a "/etc/nftables.conf.backup-$timestamp" /etc/nftables.conf
    die "nftables validation failed; original configuration restored"
fi
systemctl enable --now nftables

cp "$INSTALL_DIR/deploy/$APP_NAME.service" "$SERVICE_FILE"
ln -sfn "$INSTALL_DIR/nft.sh" /usr/local/sbin/nfpctl
systemctl daemon-reload
systemctl enable --now "$APP_NAME"
systemctl restart "$APP_NAME"

install -d -o caddy -g caddy -m 0750 /etc/caddy/conf.d /var/log/caddy
[[ -f /etc/caddy/Caddyfile ]] || install -o root -g root -m 0644 /dev/null /etc/caddy/Caddyfile
cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.backup-$timestamp"
if ! grep -Eq '^[[:space:]]*import[[:space:]]+/etc/caddy/conf\.d/\*\.caddy' /etc/caddy/Caddyfile; then
    printf '\nimport /etc/caddy/conf.d/*.caddy\n' >> /etc/caddy/Caddyfile
fi
cat > "$CADDY_SNIPPET" <<EOF
$DOMAIN {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8108
    tls $EMAIL
    log {
        output file /var/log/caddy/$APP_NAME-access.log {
            roll_size 20MiB
            roll_keep 10
            roll_keep_for 720h
        }
        format json
    }
}
EOF
caddy fmt --overwrite /etc/caddy/Caddyfile >/dev/null
caddy fmt --overwrite "$CADDY_SNIPPET" >/dev/null
chown root:caddy /etc/caddy/Caddyfile "$CADDY_SNIPPET"
chmod 0644 /etc/caddy/Caddyfile
chmod 0640 "$CADDY_SNIPPET"
if ! runuser -u caddy -- caddy validate --config /etc/caddy/Caddyfile; then
    cp -a "/etc/caddy/Caddyfile.backup-$timestamp" /etc/caddy/Caddyfile
    rm -f "$CADDY_SNIPPET"
    die "Caddy validation failed; original Caddyfile restored"
fi
systemctl enable --now caddy
systemctl reload caddy

sleep 2
systemctl is-active --quiet "$APP_NAME" || die "panel failed; inspect: journalctl -u $APP_NAME -n 100"
curl -fsS -o /dev/null http://127.0.0.1:8108/ || die "panel did not respond on 127.0.0.1:8108"
if [[ $new_install -eq 1 ]]; then
    sed -i '/^PANEL_ADMIN_PASSWORD=/d' "$ENV_FILE"
fi

printf '\nInstallation completed.\n'
printf 'URL: https://%s\n' "$DOMAIN"
printf 'Administrator: %s\n' "$ADMIN_USERNAME"
printf 'Status: systemctl status %s caddy nftables\n' "$APP_NAME"
printf 'Logs: journalctl -u %s -u caddy -f\n' "$APP_NAME"
printf 'The initial password has been removed from %s after account creation.\n' "$ENV_FILE"
