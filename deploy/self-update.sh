#!/usr/bin/env bash
# Self-update helper for nft-forward-panel.
#
# Started by the panel (POST /api/update) as a transient systemd unit, so the
# restart below cannot kill the process that triggered it.  Only releases of
# the configured repository are accepted, and the archive must match the
# SHA-256 digest GitHub reports for that asset.

set -Euo pipefail

APP_NAME="nft-forward-panel"
INSTALL_DIR="/opt/${APP_NAME}"
DATA_DIR="${PANEL_DATA_DIR:-/var/lib/${APP_NAME}}"
REPO="${PANEL_UPDATE_REPO:-CinnieMochi/nft-forward-panel}"
STATUS_FILE="${DATA_DIR}/update.status"
LOCK_DIR="${DATA_DIR}/.update.lock"
HEALTH_URL="http://127.0.0.1:8108/healthz"
WORK=""
TARGET="${1:-latest}"

mkdir -p "$DATA_DIR"

write_status() {
    local phase="$1" message="${2:-}"
    printf '{"phase":"%s","message":"%s","at":"%s"}\n' \
        "$phase" "${message//\"/\\\"}" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" > "$STATUS_FILE"
}

cleanup() {
    [ -z "$WORK" ] || rm -rf "$WORK"
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

fail() {
    write_status failed "$1"
    echo "$1" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail "必须以 root 身份运行"
command -v curl >/dev/null 2>&1 || fail "缺少 curl"
command -v python3 >/dev/null 2>&1 || fail "缺少 python3"
command -v sha256sum >/dev/null 2>&1 || fail "缺少 sha256sum"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    fail "已有更新任务正在运行"
fi

WORK="$(mktemp -d)"
write_status checking "正在查询发布版本"

if [ "$TARGET" = "latest" ]; then
    API="https://api.github.com/repos/${REPO}/releases/latest"
else
    API="https://api.github.com/repos/${REPO}/releases/tags/${TARGET}"
fi

curl -fsSL --retry 3 -m 30 -o "$WORK/release.json" "$API" || fail "无法读取发布信息"

python3 - "$WORK/release.json" "$WORK/meta" <<'PY' || fail "发布信息解析失败"
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
tag = str(payload.get("tag_name") or "").lstrip("v")
assets = {item.get("name"): item for item in payload.get("assets") or []}
archive = assets.get("nft-forward-panel.tar.gz")
if not tag or not archive:
    raise SystemExit("missing tag or archive asset")
lines = [
    f"TAG={tag}",
    f"URL={archive['browser_download_url']}",
    f"DIGEST={archive.get('digest') or ''}",
]
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY

# shellcheck disable=SC1090
. "$WORK/meta"
[ -n "${TAG:-}" ] || fail "无法确定目标版本"

write_status downloading "正在下载 v${TAG}"
curl -fsSL --retry 3 -m 180 -o "$WORK/package.tar.gz" "$URL" || fail "下载失败"

ACTUAL="sha256:$(sha256sum "$WORK/package.tar.gz" | awk '{print $1}')"
if [ -z "${DIGEST:-}" ]; then
    fail "发布资产缺少 SHA-256 摘要，拒绝安装"
fi
[ "$ACTUAL" = "$DIGEST" ] || fail "校验失败：期望 ${DIGEST}，实际 ${ACTUAL}"

write_status installing "正在安装 v${TAG}"
mkdir -p "$WORK/pkg"
tar -xzf "$WORK/package.tar.gz" -C "$WORK/pkg" || fail "解包失败"
SOURCE="$WORK/pkg/nft-forward-panel"
[ -f "$SOURCE/app.py" ] || fail "发布包内缺少 app.py"

TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
BACKUP="${INSTALL_DIR}.backup-${TIMESTAMP}"
cp -a "$INSTALL_DIR" "$BACKUP" || fail "备份当前版本失败"
{ ls -1d "${INSTALL_DIR}".backup-* 2>/dev/null | sort -r | tail -n +3 | xargs -r rm -rf; } || true

restore() {
    rm -rf "$INSTALL_DIR"
    if [ -d "$BACKUP" ]; then
        mv "$BACKUP" "$INSTALL_DIR"
        systemctl restart "$APP_NAME" >/dev/null 2>&1 || true
    fi
    fail "$1"
}

( cd "$SOURCE" && tar -cf - --exclude=.venv . ) | ( cd "$INSTALL_DIR" && tar -xf - ) \
    || restore "写入新版本文件失败"
chmod 0755 "$INSTALL_DIR/install.sh" "$INSTALL_DIR/nft.sh" "$INSTALL_DIR/nfpctl.py" 2>/dev/null || true

PIP="$INSTALL_DIR/.venv/bin/pip"
if [ -x "$PIP" ]; then
    "$PIP" install --quiet --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt" >/dev/null \
        || restore "安装 Python 依赖失败"
    "$PIP" check >/dev/null 2>&1 || restore "Python 依赖校验失败"
fi

write_status restarting "正在重启服务"
systemctl restart "$APP_NAME" || restore "重启服务失败"

for _ in $(seq 1 40); do
    if systemctl is-active --quiet "$APP_NAME" && curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1; then
        write_status done "已更新到 v${TAG}"
        exit 0
    fi
    sleep 1
done

restore "新版本健康检查未通过，已回滚到旧版本"
