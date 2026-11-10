#!/usr/bin/env bash
# nft-forward-panel installer.
#
#   install / upgrade, pick an access mode (domain+HTTPS or IP+HTTP), switch
#   between modes later, and uninstall.  All user-facing text is available in
#   Chinese and English via --lang.

set -Eeuo pipefail

APP_NAME="nft-forward-panel"
INSTALL_DIR="/opt/$APP_NAME"
DATA_DIR="/var/lib/$APP_NAME"
ENV_FILE="/etc/$APP_NAME.env"
SERVICE_FILE="/etc/systemd/system/$APP_NAME.service"
CADDY_SNIPPET="/etc/caddy/conf.d/$APP_NAME.caddy"
CADDY_MAIN="/etc/caddy/Caddyfile"
DEFAULT_REPOSITORY="CinnieMochi/nft-forward-panel"
DEFAULT_HTTP_PORT=8108
LOCAL_BIND="127.0.0.1:8108"

LANG_CODE="zh"
ACTION="install"
ACCESS_MODE=""
DOMAIN=""
EMAIL=""
HTTP_PORT="$DEFAULT_HTTP_PORT"
ADMIN_USERNAME="admin"
ADMIN_PASSWORD="${NFP_ADMIN_PASSWORD:-}"
REPOSITORY="${NFP_REPOSITORY:-$DEFAULT_REPOSITORY}"
RELEASE_VERSION="latest"
LANG_EXPLICIT=0
NON_INTERACTIVE=0
ASSUME_YES=0
PURGE_DATA=0
KEEP_RULES=0
SOURCE_DIR=""
TEMP_DIR=""

declare -A MSG_ZH MSG_EN

load_messages() {
    MSG_ZH=(
        [need_root]="必须以 root 身份运行：sudo bash install.sh"
        [need_os]="仅支持 Debian / Ubuntu"
        [need_apt]="缺少 apt-get"
        [need_tty]="交互模式需要终端；请先下载脚本再执行"
        [lang_prompt]="选择语言 / Choose language [1=中文 2=English] (默认 1): "
        [lang_invalid]="请输入 1 或 2"
        [access_prompt]="访问方式 / Access mode [1=域名+HTTPS 2=IP+端口 HTTP] (默认 1): "
        [access_invalid]="请输入 1 或 2"
        [domain_prompt]="面板域名（例如 panel.example.com）: "
        [email_prompt]="证书通知邮箱: "
        [port_prompt]="面板监听端口 (默认 %s): "
        [admin_prompt]="管理员用户名 (默认 admin): "
        [password_prompt]="管理员密码（12-256 字符）: "
        [password_confirm]="再次输入密码: "
        [password_mismatch]="两次输入不一致"
        [password_len]="密码长度必须为 12-256，且不能包含换行"
        [invalid_domain]="域名格式不正确: %s"
        [invalid_email]="邮箱格式不正确: %s"
        [invalid_user]="用户名需为 3-32 位字母、数字、_、- 或 ."
        [invalid_port]="端口需为 1-65535"
        [http_warn]="注意：HTTP 模式下账号密码与流量为明文传输，请仅在可信网络或已有额外防护时使用。"
        [http_port_open]="已在本机防火墙放行 %s/tcp"
        [http_port_closed]="已关闭本机防火墙的 %s/tcp"
        [firewall_none]="未检测到本机防火墙；请记得在云安全组放行 %s/tcp"
        [existing_found]="检测到已安装（访问方式：%s），将保留数据库并更新程序"
        [switching]="访问方式将从 %s 切换为 %s"
        [deps]="安装系统依赖"
        [download]="下载 %s 版本 %s"
        [download_failed]="下载发布包失败；请确认仓库公开且存在 Release"
        [digest_missing]="GitHub 未返回发布包的 SHA-256 摘要"
        [digest_bad]="发布包 SHA-256 校验失败"
        [digest_ok]="发布包 SHA-256 校验通过"
        [bad_archive]="发布包结构不符合预期"
        [backup]="备份现有安装到 %s"
        [venv]="创建 Python 虚拟环境"
        [env_written]="已写入 %s"
        [nft_ok]="nftables 主配置校验通过"
        [nft_bad]="nftables 校验失败，已还原主配置"
        [unit_done]="已安装 systemd 服务"
        [caddy_done]="已配置 Caddy 站点 %s"
        [caddy_bad]="Caddy 配置校验失败，已还原"
        [ready_ok]="面板已在 127.0.0.1:%s 应答"
        [ready_bad]="面板未在 127.0.0.1:%s 应答 /healthz，请查看 journalctl -u %s -n 100"
        [summary_url]="访问地址: %s"
        [summary_user]="管理员: %s"
        [summary_status]="状态: systemctl status %s caddy nftables"
        [summary_logs]="日志: journalctl -u %s -u caddy -f"
        [switch_hint]="以后想改成域名 HTTPS，重新运行本脚本并选择 1 即可。"
        [uninstall_title]="即将卸载 nft-forward-panel"
        [uninstall_confirm]="确认卸载？数据库默认保留。输入 yes 继续: "
        [uninstall_cancelled]="已取消"
        [uninstall_stopped]="已停止并禁用服务"
        [uninstall_service]="已移除 systemd 服务与 nfpctl 链接"
        [uninstall_caddy]="已移除 Caddy 站点"
        [uninstall_env]="环境文件已备份到 %s 并移除"
        [uninstall_rules]="已删除 nftables 表 ip port_forward（转发停止）"
        [uninstall_rules_kept]="已保留 nftables 表 ip port_forward"
        [uninstall_data_kept]="已保留数据目录 %s（含数据库与头像）"
        [uninstall_data_removed]="已删除数据目录 %s"
        [uninstall_done]="卸载完成"
        [not_installed]="未检测到已安装的 %s"
    )
    MSG_EN=(
        [need_root]="run this installer as root: sudo bash install.sh"
        [need_os]="only Debian and Ubuntu are supported"
        [need_apt]="apt-get is required"
        [need_tty]="interactive mode needs a terminal; download the script before running it"
        [lang_prompt]="Choose language / 选择语言 [1=中文 2=English] (default 1): "
        [lang_invalid]="please answer 1 or 2"
        [access_prompt]="Access mode / 访问方式 [1=domain+HTTPS 2=IP+port HTTP] (default 1): "
        [access_invalid]="please answer 1 or 2"
        [domain_prompt]="Panel domain (e.g. panel.example.com): "
        [email_prompt]="Certificate email: "
        [port_prompt]="Panel port (default %s): "
        [admin_prompt]="Administrator username (default admin): "
        [password_prompt]="Administrator password (12-256 characters): "
        [password_confirm]="Confirm password: "
        [password_mismatch]="passwords do not match"
        [password_len]="password must be 12-256 characters without a newline"
        [invalid_domain]="invalid domain: %s"
        [invalid_email]="invalid email: %s"
        [invalid_user]="username must be 3-32 letters, numbers, _, - or ."
        [invalid_port]="port must be 1-65535"
        [http_warn]="Warning: in HTTP mode credentials and traffic are sent in clear text. Use it only on a trusted network or behind extra protection."
        [http_port_open]="opened %s/tcp in the local firewall"
        [http_port_closed]="closed %s/tcp in the local firewall"
        [firewall_none]="no local firewall detected; remember to allow %s/tcp in your cloud security group"
        [existing_found]="existing install detected (access mode: %s); the database is kept and the program is updated"
        [switching]="switching access mode from %s to %s"
        [deps]="installing system dependencies"
        [download]="downloading %s release %s"
        [download_failed]="release archive download failed; confirm the repository is public and has a published GitHub Release"
        [digest_missing]="GitHub did not return a SHA-256 digest for the release archive"
        [digest_bad]="release archive SHA-256 verification failed"
        [digest_ok]="release archive SHA-256 verified"
        [bad_archive]="release archive has an unexpected layout"
        [backup]="backing up the current install to %s"
        [venv]="creating the Python virtualenv"
        [env_written]="wrote %s"
        [nft_ok]="nftables main configuration validated"
        [nft_bad]="nftables validation failed; original configuration restored"
        [unit_done]="systemd service installed"
        [caddy_done]="Caddy site configured: %s"
        [caddy_bad]="Caddy validation failed; original configuration restored"
        [ready_ok]="panel answered on 127.0.0.1:%s"
        [ready_bad]="panel did not answer /healthz on 127.0.0.1:%s; inspect: journalctl -u %s -n 100"
        [summary_url]="URL: %s"
        [summary_user]="Administrator: %s"
        [summary_status]="Status: systemctl status %s caddy nftables"
        [summary_logs]="Logs: journalctl -u %s -u caddy -f"
        [switch_hint]="To move to domain+HTTPS later, run this script again and choose 1."
        [uninstall_title]="about to uninstall nft-forward-panel"
        [uninstall_confirm]="Proceed? The database is kept by default. Type yes to continue: "
        [uninstall_cancelled]="cancelled"
        [uninstall_stopped]="service stopped and disabled"
        [uninstall_service]="systemd service and the nfpctl symlink removed"
        [uninstall_caddy]="Caddy site removed"
        [uninstall_env]="environment file backed up to %s and removed"
        [uninstall_rules]="deleted nftables table ip port_forward (forwarding stopped)"
        [uninstall_rules_kept]="kept nftables table ip port_forward"
        [uninstall_data_kept]="kept the data directory %s (database and avatars)"
        [uninstall_data_removed]="removed the data directory %s"
        [uninstall_done]="uninstall complete"
        [not_installed]="no %s installation found"
    )
}

t() {
    local key="$1"; shift
    local text
    if [[ "$LANG_CODE" == "en" ]]; then text="${MSG_EN[$key]-$key}"; else text="${MSG_ZH[$key]-$key}"; fi
    if (($#)); then printf "$text" "$@"; else printf '%s\n' "$text"; fi
}
log() { printf '\033[1;32m[nfp]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[nfp]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[nfp] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
cleanup() { [[ -z "$TEMP_DIR" || ! -d "$TEMP_DIR" ]] || rm -rf -- "$TEMP_DIR"; }
valid_port() { [[ "${1:-}" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 )); }
valid_username() { [[ "${1:-}" =~ ^[A-Za-z0-9_.-]{3,32}$ ]]; }
valid_domain() { [[ "${1:-}" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]; }
valid_email() { [[ "${1:-}" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; }
valid_password() {
    local value="${1:-}" len
    len=$(printf '%s' "$value" | wc -c)
    (( len >= 12 && len <= 256 )) || return 1
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || return 1
    return 0
}

env_value() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | head -n 1; }

existing_access_mode() {
    [[ -f "$ENV_FILE" ]] || { printf ''; return; }
    local bind
    bind="$(env_value PANEL_BIND)"
    case "$bind" in
        0.0.0.0:*) printf 'http'; return ;;
    esac
    printf 'https'
}

firewall_open_port() {
    local port="$1"
    if systemctl is-active --quiet firewalld 2>/dev/null; then
        firewall-cmd --add-port="$port/tcp" >/dev/null 2>&1 || true
        firewall-cmd --permanent --add-port="$port/tcp" >/dev/null 2>&1 || true
        t http_port_open "$port"
    elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^Status: active'; then
        ufw allow "$port/tcp" >/dev/null 2>&1 || true
        t http_port_open "$port"
    elif command -v iptables >/dev/null 2>&1 && iptables -S >/dev/null 2>&1; then
        iptables -C INPUT -p tcp --dport "$port" -j ACCEPT >/dev/null 2>&1 \
            || iptables -I INPUT -p tcp --dport "$port" -j ACCEPT >/dev/null 2>&1 || true
        t http_port_open "$port"
    else
        warn "$(t firewall_none "$port")"
    fi
}

firewall_close_port() {
    local port="$1"
    if systemctl is-active --quiet firewalld 2>/dev/null; then
        firewall-cmd --remove-port="$port/tcp" >/dev/null 2>&1 || true
        firewall-cmd --permanent --remove-port="$port/tcp" >/dev/null 2>&1 || true
        t http_port_closed "$port"
    elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^Status: active'; then
        ufw --force delete allow "$port/tcp" >/dev/null 2>&1 || true
        t http_port_closed "$port"
    elif command -v iptables >/dev/null 2>&1 && iptables -S >/dev/null 2>&1; then
        iptables -D INPUT -p tcp --dport "$port" -j ACCEPT >/dev/null 2>&1 || true
        t http_port_closed "$port"
    fi
}

ask_language() {
    # The answer decides the language of everything that follows, so the prompt
    # itself has to be bilingual.
    [[ "$LANG_EXPLICIT" -eq 1 ]] && return 0
    local answer=""
    read -r -p "选择语言 / Choose language [1=中文 2=English] (默认/默认 1): " answer || answer="1"
    case "${answer:-1}" in
        2) LANG_CODE="en" ;;
        *) LANG_CODE="zh" ;;
    esac
    load_messages
}

# --------------------------------------------------------------------------
# Library mode: source this file to unit-test the catalog and pure helpers.
# --------------------------------------------------------------------------
load_messages
if [[ "${NFP_INSTALLER_LIB:-0}" == "1" ]]; then
    return 0 2>/dev/null || exit 0
fi

usage() {
    if [[ "$LANG_CODE" == "en" ]]; then
        cat <<'EOF'
Usage: sudo bash install.sh [options]

Access mode
  --access https         Domain + HTTPS through Caddy (default)
  --access http          Plain HTTP on a public port, no domain
  --domain DOMAIN        Domain for HTTPS mode, e.g. panel.example.com
  --email EMAIL          ACME certificate notification email
  --port PORT            Public port for HTTP mode (default 8108)

Account and source
  --admin USERNAME       Initial administrator username (default: admin)
  --repo OWNER/REPO      GitHub repository providing Releases
  --version TAG          Release tag to install (default: latest)

Behaviour
  --lang zh|en           Interface language (default: zh)
  --non-interactive      Do not prompt; supply everything via options/env
  --uninstall            Stop and remove the panel
  --purge                With --uninstall: also delete the database and avatars
  --keep-rules           With --uninstall: leave the nftables table in place
  --yes                  With --uninstall: skip the confirmation prompt
  -h, --help             Show this help

For non-interactive installation pass the password through NFP_ADMIN_PASSWORD.
EOF
    else
        cat <<'EOF'
用法: sudo bash install.sh [选项]

访问方式
  --access https         域名 + Caddy 自动 HTTPS（默认）
  --access http          纯 IP + 端口 HTTP，不使用域名
  --domain DOMAIN        HTTPS 模式的面板域名，例如 panel.example.com
  --email EMAIL          证书通知邮箱
  --port PORT            HTTP 模式的公网端口（默认 8108）

账号与来源
  --admin USERNAME       初始管理员用户名（默认 admin）
  --repo OWNER/REPO      提供 Release 的 GitHub 仓库
  --version TAG          要安装的版本标签（默认 latest）

行为
  --lang zh|en           界面语言（默认 zh）
  --non-interactive      不提示，全部通过参数与环境变量提供
  --uninstall            停止并卸载面板
  --purge                与 --uninstall 合用：连数据库与头像一起删除
  --keep-rules           与 --uninstall 合用：保留 nftables 转发表
  --yes                  与 --uninstall 合用：跳过确认
  -h, --help             显示帮助

非交互安装请通过 NFP_ADMIN_PASSWORD 传入管理员密码。
EOF
    fi
}

while (($#)); do
    case "$1" in
        --lang) [[ $# -ge 2 ]] || die "--lang requires a value"; LANG_CODE="$2"; LANG_EXPLICIT=1; shift 2 ;;
        --access) [[ $# -ge 2 ]] || die "--access requires https|http"; ACCESS_MODE="$2"; shift 2 ;;
        --domain) [[ $# -ge 2 ]] || die "--domain requires a value"; DOMAIN="$2"; shift 2 ;;
        --email) [[ $# -ge 2 ]] || die "--email requires a value"; EMAIL="$2"; shift 2 ;;
        --port) [[ $# -ge 2 ]] || die "--port requires a value"; HTTP_PORT="$2"; shift 2 ;;
        --admin) [[ $# -ge 2 ]] || die "--admin requires a value"; ADMIN_USERNAME="$2"; shift 2 ;;
        --repo) [[ $# -ge 2 ]] || die "--repo requires OWNER/REPO"; REPOSITORY="$2"; shift 2 ;;
        --version) [[ $# -ge 2 ]] || die "--version requires a tag"; RELEASE_VERSION="$2"; shift 2 ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --purge) PURGE_DATA=1; shift ;;
        --keep-rules) KEEP_RULES=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

case "$LANG_CODE" in zh|en) ;; *) LANG_CODE="zh" ;; esac
load_messages

[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "$(t need_root)"
[[ -r /etc/os-release ]] || die "$(t need_os)"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in debian|ubuntu) ;; *) die "$(t need_os)" ;; esac
command -v apt-get >/dev/null || die "$(t need_apt)"

trap cleanup EXIT

if [[ $NON_INTERACTIVE -eq 0 ]]; then
    ask_language
fi

do_uninstall() {
    if [[ ! -d "$INSTALL_DIR" && ! -f "$SERVICE_FILE" ]]; then
        warn "$(t not_installed "$APP_NAME")"
        return 0
    fi
    log "$(t uninstall_title)"
    if [[ $ASSUME_YES -eq 0 ]]; then
        local answer
        read -r -p "$(t uninstall_confirm)" answer
        [[ "$answer" == "yes" ]] || { warn "$(t uninstall_cancelled)"; return 1; }
    fi
    systemctl stop "$APP_NAME" >/dev/null 2>&1 || true
    systemctl disable "$APP_NAME" >/dev/null 2>&1 || true
    log "$(t uninstall_stopped)"

    rm -f "$SERVICE_FILE"
    rm -f /usr/local/sbin/nfpctl
    systemctl daemon-reload || true
    log "$(t uninstall_service)"

    if [[ -f "$CADDY_SNIPPET" ]]; then
        rm -f "$CADDY_SNIPPET"
        if command -v caddy >/dev/null 2>&1 && systemctl is-active --quiet caddy 2>/dev/null; then
            systemctl reload caddy >/dev/null 2>&1 || true
        fi
        log "$(t uninstall_caddy)"
    fi

    local mode port
    mode="$(existing_access_mode)"
    port="$(env_value PANEL_BIND | sed -n 's/.*:\([0-9]*\)$/\1/p')"
    if [[ "$mode" == "http" && -n "${port:-}" ]]; then
        firewall_close_port "$port"
    fi

    if command -v nft >/dev/null 2>&1 && nft list table ip port_forward >/dev/null 2>&1; then
        if [[ $KEEP_RULES -eq 1 ]]; then
            log "$(t uninstall_rules_kept)"
        else
            nft delete table ip port_forward >/dev/null 2>&1 || true
            log "$(t uninstall_rules)"
        fi
    fi

    if [[ -f "$ENV_FILE" ]]; then
        local env_backup="/root/$APP_NAME.env.uninstalled-$(date +%Y%m%d-%H%M%S)"
        cp -a "$ENV_FILE" "$env_backup"
        rm -f "$ENV_FILE"
        log "$(t uninstall_env "$env_backup")"
    fi

    rm -rf "$INSTALL_DIR" 2>/dev/null || true
    rm -rf "$INSTALL_DIR".backup-* 2>/dev/null || true
    if [[ $PURGE_DATA -eq 1 ]]; then
        rm -rf "$DATA_DIR"
        log "$(t uninstall_data_removed "$DATA_DIR")"
    else
        log "$(t uninstall_data_kept "$DATA_DIR")"
    fi
    log "$(t uninstall_done)"
}

if [[ "$ACTION" == "uninstall" ]]; then
    do_uninstall
    exit 0
fi

new_install=0
[[ -f "$DATA_DIR/panel.db" ]] || new_install=1
previous_mode=""
previous_port=""
if [[ $new_install -eq 0 ]]; then
    previous_mode="$(existing_access_mode)"
    previous_port="$(env_value PANEL_BIND | sed -n 's/.*:\([0-9]*\)$/\1/p')"
fi

if [[ $NON_INTERACTIVE -eq 0 ]]; then
    [[ -t 0 ]] || die "$(t need_tty)"
    if [[ -z "$ACCESS_MODE" ]]; then
        choice=""
        read -r -p "$(t access_prompt)" choice
        case "${choice:-1}" in 1) ACCESS_MODE="https" ;; 2) ACCESS_MODE="http" ;; *) die "$(t access_invalid)" ;; esac
    fi
    if [[ -z "$previous_mode" ]]; then
        previous_mode="$ACCESS_MODE"
    fi
    if [[ "$ACCESS_MODE" == "https" ]]; then
        read -r -e -p "$(t domain_prompt)" DOMAIN
        read -r -e -p "$(t email_prompt)" EMAIL
        if [[ $new_install -eq 1 ]]; then
            admin_input=""
            read -r -e -p "$(t admin_prompt)" admin_input
            ADMIN_USERNAME="${admin_input:-admin}"
        fi
    else
        port_input=""
        read -r -e -p "$(t port_prompt "$HTTP_PORT")" port_input
        HTTP_PORT="${port_input:-$HTTP_PORT}"
    fi
    if [[ $new_install -eq 1 && -z "$ADMIN_PASSWORD" ]]; then
        while :; do
            read -r -s -p "$(t password_prompt)" ADMIN_PASSWORD; printf '\n'
            read -r -s -p "$(t password_confirm)" confirmation; printf '\n'
            if [[ "$ADMIN_PASSWORD" != "$confirmation" ]]; then warn "$(t password_mismatch)"; continue; fi
            valid_password "$ADMIN_PASSWORD" || { warn "$(t password_len)"; continue; }
            break
        done
    fi
else
    [[ -n "$ACCESS_MODE" ]] || die "--access is required with --non-interactive"
fi

case "$ACCESS_MODE" in
    https)
        valid_domain "$DOMAIN" || die "$(t invalid_domain "$DOMAIN")"
        valid_email "$EMAIL" || die "$(t invalid_email "$EMAIL")"
        ;;
    http)
        valid_port "$HTTP_PORT" || die "$(t invalid_port)"
        ;;
    *) die "$(t access_invalid)" ;;
esac
valid_username "$ADMIN_USERNAME" || die "$(t invalid_user)"
if [[ $new_install -eq 1 ]]; then
    valid_password "$ADMIN_PASSWORD" || die "$(t password_len)"
fi

if [[ $new_install -eq 0 && -n "$previous_mode" && "$previous_mode" != "$ACCESS_MODE" ]]; then
    log "$(t switching "$previous_mode" "$ACCESS_MODE")"
elif [[ $new_install -eq 0 ]]; then
    log "$(t existing_found "$previous_mode")"
fi
if [[ "$ACCESS_MODE" == "http" ]]; then
    warn "$(t http_warn)"
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -f "$SCRIPT_DIR/app.py" && -f "$SCRIPT_DIR/requirements.txt" ]]; then
    SOURCE_DIR="$SCRIPT_DIR"
    log "using local source tree: $SOURCE_DIR"
else
    [[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "specify --repo OWNER/REPO"
fi

log "$(t deps)"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates conntrack curl gpg openssl nftables python3 python3-venv sqlite3
if [[ "$ACCESS_MODE" == "https" ]] && ! command -v caddy >/dev/null; then
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
        release_api="https://api.github.com/repos/$REPOSITORY/releases/latest"
    else
        release_url="https://github.com/$REPOSITORY/releases/download/$RELEASE_VERSION"
        release_api="https://api.github.com/repos/$REPOSITORY/releases/tags/$RELEASE_VERSION"
    fi
    log "$(t download "$REPOSITORY" "$RELEASE_VERSION")"
    curl -fL --retry 3 -o "$TEMP_DIR/$APP_NAME.tar.gz" "$release_url/$APP_NAME.tar.gz" || die "$(t download_failed)"
    expected_digest=$(curl -fsSL "$release_api" | python3 -c \
        'import json, sys; name=sys.argv[1]; data=json.load(sys.stdin); print(next((a.get("digest", "") for a in data.get("assets", []) if a.get("name") == name), ""))' \
        "$APP_NAME.tar.gz")
    [[ "$expected_digest" =~ ^sha256:[0-9a-fA-F]{64}$ ]] || die "$(t digest_missing)"
    actual_digest="sha256:$(sha256sum "$TEMP_DIR/$APP_NAME.tar.gz" | awk '{print $1}')"
    [[ "$actual_digest" == "$expected_digest" ]] || die "$(t digest_bad)"
    log "$(t digest_ok)"
    tar -xzf "$TEMP_DIR/$APP_NAME.tar.gz" -C "$TEMP_DIR"
    SOURCE_DIR="$TEMP_DIR/$APP_NAME"
    [[ -f "$SOURCE_DIR/app.py" && -f "$SOURCE_DIR/deploy/$APP_NAME.service" && -f "$SOURCE_DIR/deploy/serve.sh" ]] || die "$(t bad_archive)"
fi

timestamp=$(date +%Y%m%d-%H%M%S)
if [[ -d "$INSTALL_DIR" ]]; then
    log "$(t backup "$INSTALL_DIR.backup-$timestamp")"
    cp -a "$INSTALL_DIR" "$INSTALL_DIR.backup-$timestamp"
    chown -R root:root "$INSTALL_DIR.backup-$timestamp" 2>/dev/null || true
fi
install -d -m 0755 "$INSTALL_DIR"
tar -C "$SOURCE_DIR" --exclude=.git --exclude=.venv --exclude=.preview-data \
    --exclude=__pycache__ --exclude='*.pyc' --exclude='*.db*' -cf - . | tar -C "$INSTALL_DIR" -xf -
chown -R root:root "$INSTALL_DIR"
chmod 0755 "$INSTALL_DIR/install.sh" "$INSTALL_DIR/nft.sh" "$INSTALL_DIR/nfpctl.py" 2>/dev/null || true

log "$(t venv)"
rm -rf "$INSTALL_DIR/.venv"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"
"$INSTALL_DIR/.venv/bin/pip" check

install -d -o root -g root -m 0700 "$DATA_DIR" "$DATA_DIR/avatars"

secret_key=""
preserved_lines=""
if [[ -f "$ENV_FILE" ]]; then
    cp -a "$ENV_FILE" "$ENV_FILE.backup-$timestamp"
    secret_key="$(env_value PANEL_SECRET_KEY)"
    preserved_lines=$(grep -E '^[A-Z_][A-Z0-9_]*=' "$ENV_FILE" 2>/dev/null \
        | grep -Ev '^(PANEL_SECRET_KEY|PANEL_ADMIN_USERNAME|PANEL_ADMIN_PASSWORD|PANEL_DATA_DIR|PANEL_FORWARD_CONFIG|PANEL_MAIN_CONFIG|PANEL_SYSCTL_CONFIG|PANEL_COOKIE_SECURE|PANEL_TRUSTED_PROXY_COUNT|PANEL_POLICY_SCHEDULER|PANEL_LOGIN_WINDOW_SECONDS|PANEL_LOGIN_MAX_ATTEMPTS|PANEL_TIMEZONE|PANEL_BIND)=' || true)
fi
secret_key="${secret_key:-$(openssl rand -hex 32)}"
env_escape() { local value="$1"; value="${value//\\/\\\\}"; value="${value//\"/\\\"}"; printf '"%s"' "$value"; }

if [[ "$ACCESS_MODE" == "https" ]]; then
    panel_bind="$LOCAL_BIND"
    cookie_secure=1
    trusted_proxy=1
else
    panel_bind="0.0.0.0:$HTTP_PORT"
    cookie_secure=0
    trusted_proxy=0
fi

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
PANEL_BIND=$panel_bind
PANEL_COOKIE_SECURE=$cookie_secure
PANEL_TRUSTED_PROXY_COUNT=$trusted_proxy
PANEL_POLICY_SCHEDULER=1
PANEL_LOGIN_WINDOW_SECONDS=600
PANEL_LOGIN_MAX_ATTEMPTS=20
PANEL_TIMEZONE=Asia/Shanghai
EOF
    if [[ -n "$preserved_lines" ]]; then printf '%s\n' "$preserved_lines"; fi
} > "$ENV_FILE"
chmod 0600 "$ENV_FILE"
umask 022
log "$(t env_written "$ENV_FILE")"

install -d -m 0755 /etc/nftables.d
[[ -f /etc/nftables.conf ]] || die "/etc/nftables.conf is missing after nftables installation"
cp -a /etc/nftables.conf "/etc/nftables.conf.backup-$timestamp"
if ! grep -Eq '^[[:space:]]*include[[:space:]]+"/etc/nftables\.d/\*\.conf"' /etc/nftables.conf; then
    printf '\ninclude "/etc/nftables.d/*.conf"\n' >> /etc/nftables.conf
fi
if ! nft -c -f /etc/nftables.conf; then
    cp -a "/etc/nftables.conf.backup-$timestamp" /etc/nftables.conf
    die "$(t nft_bad)"
fi
log "$(t nft_ok)"
systemctl enable --now nftables

cp "$INSTALL_DIR/deploy/$APP_NAME.service" "$SERVICE_FILE"
ln -sfn "$INSTALL_DIR/nft.sh" /usr/local/sbin/nfpctl
systemctl daemon-reload
systemctl enable "$APP_NAME"
systemctl restart "$APP_NAME" || warn "first start returned non-zero; waiting for readiness below"
log "$(t unit_done)"

if [[ "$ACCESS_MODE" == "https" ]]; then
    install -d -o caddy -g caddy -m 0750 /etc/caddy/conf.d /var/log/caddy
    [[ -f "$CADDY_MAIN" ]] || install -o root -g root -m 0644 /dev/null "$CADDY_MAIN"
    cp -a "$CADDY_MAIN" "$CADDY_MAIN.backup-$timestamp"
    if ! grep -Eq '^[[:space:]]*import[[:space:]]+/etc/caddy/conf\.d/\*\.caddy' "$CADDY_MAIN"; then
        printf '\nimport /etc/caddy/conf.d/*.caddy\n' >> "$CADDY_MAIN"
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
    caddy fmt --overwrite "$CADDY_MAIN" >/dev/null
    caddy fmt --overwrite "$CADDY_SNIPPET" >/dev/null
    chown root:caddy "$CADDY_MAIN" "$CADDY_SNIPPET"
    chmod 0644 "$CADDY_MAIN"
    chmod 0640 "$CADDY_SNIPPET"
    if ! runuser -u caddy -- caddy validate --config "$CADDY_MAIN"; then
        cp -a "$CADDY_MAIN.backup-$timestamp" "$CADDY_MAIN"
        rm -f "$CADDY_SNIPPET"
        die "$(t caddy_bad)"
    fi
    systemctl enable --now caddy
    systemctl reload caddy
    log "$(t caddy_done "$DOMAIN")"
    # Leaving the old public HTTP port open after the switch would be a stale
    # hole: nothing listens there any more, but a later service would inherit it.
    if [[ "$previous_mode" == "http" && -n "$previous_port" ]]; then
        firewall_close_port "$previous_port"
    fi
else
    rm -f "$CADDY_SNIPPET"
    if command -v caddy >/dev/null 2>&1 && systemctl is-active --quiet caddy 2>/dev/null; then
        systemctl reload caddy >/dev/null 2>&1 || true
    fi
    firewall_open_port "$HTTP_PORT"
fi

probe_port=8108
[[ "$ACCESS_MODE" == "http" ]] && probe_port="$HTTP_PORT"
ready=0
for _ in $(seq 1 40); do
    if systemctl is-active --quiet "$APP_NAME" && curl -fsS -o /dev/null "http://127.0.0.1:$probe_port/healthz"; then
        ready=1
        break
    fi
    sleep 1
done
[[ $ready -eq 1 ]] || die "$(t ready_bad "$probe_port" "$APP_NAME")"
log "$(t ready_ok "$probe_port")"

if [[ $new_install -eq 1 ]]; then
    sed -i '/^PANEL_ADMIN_PASSWORD=/d' "$ENV_FILE"
fi

if [[ "$ACCESS_MODE" == "https" ]]; then
    access_url="https://$DOMAIN"
else
    access_url="http://<server-ip>:$HTTP_PORT"
fi
printf '\n'
log "$(t summary_url "$access_url")"
log "$(t summary_user "$ADMIN_USERNAME")"
log "$(t summary_status "$APP_NAME")"
log "$(t summary_logs "$APP_NAME")"
if [[ "$ACCESS_MODE" == "http" ]]; then
    log "$(t switch_hint)"
fi
