"""Web UI for managing nftables port forwarding rules.

Run behind a reverse proxy with TLS.  The process needs root privileges only
because nftables and sysctl require them; it never places user input in a shell
command.
"""

from __future__ import annotations

import hmac
import ipaddress
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from flask import Flask, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from monitoring import TrafficMonitor, probe_tcp
from nft_manager import ForwardRule, NftManager, NftOperationError


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
LOGIN_WINDOW_SECONDS = max(60, int(os.environ.get("PANEL_LOGIN_WINDOW_SECONDS", "600")))
LOGIN_MAX_ATTEMPTS = max(5, int(os.environ.get("PANEL_LOGIN_MAX_ATTEMPTS", "20")))
MAX_AVATAR_BYTES = 5 * 1024 * 1024
_apply_lock = threading.RLock()
UTC_TIME_FORMAT = "%Y-%m-%d %H:%M:%S UTC"
PANEL_TIMEZONE = ZoneInfo("Asia/Shanghai")
POLICY_INTERVAL_SECONDS = 5


class ApplyLock:
    """Serialize nft changes across worker processes on Linux."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / ".nft-apply.lock"
        self.handle: Any = None

    def __enter__(self) -> "ApplyLock":
        _apply_lock.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            import fcntl  # Linux deployment only

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            # The in-process lock still makes development on Windows safe.
            pass
        return self

    def __exit__(self, *args: object) -> None:
        try:
            try:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass
            if self.handle:
                self.handle.close()
        finally:
            _apply_lock.release()


def now() -> str:
    return datetime.now(timezone.utc).strftime(UTC_TIME_FORMAT)


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, UTC_TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def validate_expiry(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    try:
        local = datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=PANEL_TIMEZONE)
    except ValueError as exc:
        raise ValueError("到期时间必须精确到分钟。") from exc
    return local.astimezone(timezone.utc).strftime(UTC_TIME_FORMAT)


def validate_reset_schedule(day_value: str, time_value: str) -> tuple[int, int]:
    try:
        day = int(day_value)
    except ValueError as exc:
        raise ValueError("月流量重置日必须是 1–31。") from exc
    if not 1 <= day <= 31:
        raise ValueError("月流量重置日必须是 1–31。")
    try:
        reset_time = datetime.strptime(time_value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("月流量重置时间必须精确到分钟。") from exc
    return day, reset_time.hour * 60 + reset_time.minute


def month_last_day(year: int, month: int) -> int:
    if month == 12:
        return (datetime(year + 1, 1, 1) - timedelta(days=1)).day
    return (datetime(year, month + 1, 1) - timedelta(days=1)).day


def monthly_cycle_start(user: sqlite3.Row | dict[str, Any], at: datetime | None = None) -> datetime:
    """Return this user's current monthly quota period in the panel timezone."""
    local_now = (at or datetime.now(timezone.utc)).astimezone(PANEL_TIMEZONE)
    reset_day = int(user["monthly_reset_day"])
    reset_minute = int(user["monthly_reset_minute"])

    def scheduled(year: int, month: int) -> datetime:
        day = min(reset_day, month_last_day(year, month))
        return datetime(year, month, day, reset_minute // 60, reset_minute % 60, tzinfo=PANEL_TIMEZONE)

    start = scheduled(local_now.year, local_now.month)
    if local_now < start:
        previous_year, previous_month = (local_now.year - 1, 12) if local_now.month == 1 else (local_now.year, local_now.month - 1)
        start = scheduled(previous_year, previous_month)
    return start.astimezone(timezone.utc)


def format_expiry(value: str | None) -> str:
    expiry = parse_utc(value)
    return "永不到期" if expiry is None else expiry.astimezone(PANEL_TIMEZONE).strftime("%Y年%m月%d日 %H:%M")


def expiry_input_value(value: str | None) -> str:
    expiry = parse_utc(value)
    return "" if expiry is None else expiry.astimezone(PANEL_TIMEZONE).strftime("%Y-%m-%dT%H:%M")


def format_reset_schedule(day: int, minute: int) -> str:
    return f"每月{day}日 {minute // 60:02d}:{minute % 60:02d}"


def pause_label(reason: str | None) -> str:
    return {"disabled": "账户已停用", "expired": "已到期", "quota": "月流量已用尽"}.get(reason or "", "运行中")


def validate_username(value: str) -> str:
    value = value.strip()
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("用户名须为 3–32 位，仅可使用字母、数字、下划线、连字符或句点。")
    return value


def validate_password(value: str) -> str:
    if not 12 <= len(value) <= 256:
        raise ValueError("密码长度须为 12–256 个字符。")
    return value


def validate_email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL_RE.fullmatch(value) or len(value) > 254:
        raise ValueError("请输入有效的邮箱地址。")
    return value


def validate_rule_note(value: str) -> str:
    value = value.strip()
    if len(value) > 80:
        raise ValueError("规则备注最多 80 个字符。")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("规则备注不能包含换行或控制字符。")
    return value


def parse_bounded_int(value: str | None, label: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value or "")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数。") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label}须为 {minimum}–{maximum}。")
    return parsed


def parse_bounded_float(value: str | None, label: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value or "")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字。") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{label}须为 {minimum:g}–{maximum:g}。")
    return parsed


def account_identifier_conflicts(connection: sqlite3.Connection, username: str, email: str, excluded_id: int | None = None) -> bool:
    sql = "SELECT 1 FROM users WHERE (username COLLATE NOCASE IN (?, ?) OR email COLLATE NOCASE IN (?, ?))"
    params: list[Any] = [username, email, username, email]
    if excluded_id is not None:
        sql += " AND id != ?"
        params.append(excluded_id)
    return connection.execute(sql + " LIMIT 1", params).fetchone() is not None


def validate_entry_address(value: str) -> str:
    """Accept an optional public IPv4 address shown to one account only."""
    value = value.strip()
    if not value:
        return ""
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise ValueError("入口地址必须是有效的 IPv4 地址，或留空。") from exc


def new_identity_id() -> str:
    """Return a stable public account identifier with 128 bits of entropy."""
    return f"usr_{secrets.token_hex(16)}"


def assign_missing_identity_ids(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT id FROM users WHERE identity_id IS NULL OR identity_id = ''").fetchall()
    for row in rows:
        while True:
            identity_id = new_identity_id()
            try:
                connection.execute("UPDATE users SET identity_id = ? WHERE id = ?", (identity_id, row[0]))
                break
            except sqlite3.IntegrityError:
                continue


def image_extension(payload: bytes) -> str:
    if payload.startswith(bytes.fromhex("89504e470d0a1a0a")):
        return "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "webp"
    raise ValueError("头像仅支持 PNG、JPEG 或 WebP 图片。")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        connection = sqlite3.connect(current_app.config["DATABASE"], timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.db = connection
    return g.db


def close_db(_: object | None = None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_schema(app: Flask) -> None:
    data_dir = Path(app.config["DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    connection = sqlite3.connect(app.config["DATABASE"])
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_id TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                entry_address TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS forward_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listen_port INTEGER NOT NULL UNIQUE CHECK(listen_port BETWEEN 1 AND 65535),
                destination_ip TEXT NOT NULL,
                destination_port INTEGER NOT NULL CHECK(destination_port BETWEEN 1 AND 65535),
                owner_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER REFERENCES users(id),
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                remote_addr TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rule_counter_state (
                rule_id INTEGER PRIMARY KEY REFERENCES forward_rules(id) ON DELETE CASCADE,
                inbound_bytes INTEGER NOT NULL DEFAULT 0,
                outbound_bytes INTEGER NOT NULL DEFAULT 0,
                sampled_at TEXT NOT NULL,
                inbound_bps REAL NOT NULL DEFAULT 0,
                outbound_bps REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS traffic_buckets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL REFERENCES forward_rules(id) ON DELETE CASCADE,
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                bucket_at TEXT NOT NULL,
                inbound_bytes INTEGER NOT NULL DEFAULT 0,
                outbound_bytes INTEGER NOT NULL DEFAULT 0,
                UNIQUE(rule_id, bucket_at)
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                remote_addr TEXT NOT NULL,
                attempted_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_login_attempts_addr_time ON login_attempts(remote_addr, attempted_at);
            CREATE INDEX IF NOT EXISTS idx_rules_owner ON forward_rules(owner_id);
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_traffic_owner_time ON traffic_buckets(owner_id, bucket_at);
            """
        )
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        for name, sql in {
            "identity_id": "ALTER TABLE users ADD COLUMN identity_id TEXT",
            "max_rules": "ALTER TABLE users ADD COLUMN max_rules INTEGER NOT NULL DEFAULT 10",
            "port_min": "ALTER TABLE users ADD COLUMN port_min INTEGER NOT NULL DEFAULT 1024",
            "port_max": "ALTER TABLE users ADD COLUMN port_max INTEGER NOT NULL DEFAULT 65535",
            "default_inbound_mbps": "ALTER TABLE users ADD COLUMN default_inbound_mbps INTEGER NOT NULL DEFAULT 0",
            "default_outbound_mbps": "ALTER TABLE users ADD COLUMN default_outbound_mbps INTEGER NOT NULL DEFAULT 0",
            "email": "ALTER TABLE users ADD COLUMN email TEXT",
            "avatar_filename": "ALTER TABLE users ADD COLUMN avatar_filename TEXT",
            "monthly_quota_bytes": "ALTER TABLE users ADD COLUMN monthly_quota_bytes INTEGER NOT NULL DEFAULT 0",
            "expires_at": "ALTER TABLE users ADD COLUMN expires_at TEXT",
            "monthly_reset_day": "ALTER TABLE users ADD COLUMN monthly_reset_day INTEGER NOT NULL DEFAULT 1",
            "monthly_reset_minute": "ALTER TABLE users ADD COLUMN monthly_reset_minute INTEGER NOT NULL DEFAULT 0",
            "entry_address": "ALTER TABLE users ADD COLUMN entry_address TEXT NOT NULL DEFAULT ''",
            "session_version": "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in user_columns:
                connection.execute(sql)
        assign_missing_identity_ids(connection)
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_identity_unique ON users(identity_id) WHERE identity_id IS NOT NULL AND identity_id != ''")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email COLLATE NOCASE) WHERE email IS NOT NULL")
        rule_columns = {row[1] for row in connection.execute("PRAGMA table_info(forward_rules)")}
        for name, sql in {
            "inbound_limit_mbps": "ALTER TABLE forward_rules ADD COLUMN inbound_limit_mbps INTEGER NOT NULL DEFAULT 0",
            "outbound_limit_mbps": "ALTER TABLE forward_rules ADD COLUMN outbound_limit_mbps INTEGER NOT NULL DEFAULT 0",
            "paused_reason": "ALTER TABLE forward_rules ADD COLUMN paused_reason TEXT NOT NULL DEFAULT ''",
            "note": "ALTER TABLE forward_rules ADD COLUMN note TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in rule_columns:
                connection.execute(sql)
        counter_columns = {row[1] for row in connection.execute("PRAGMA table_info(rule_counter_state)")}
        for name, sql in {
            "inbound_bps": "ALTER TABLE rule_counter_state ADD COLUMN inbound_bps REAL NOT NULL DEFAULT 0",
            "outbound_bps": "ALTER TABLE rule_counter_state ADD COLUMN outbound_bps REAL NOT NULL DEFAULT 0",
        }.items():
            if name not in counter_columns:
                connection.execute(sql)
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            username = os.environ.get("PANEL_ADMIN_USERNAME", "")
            password = os.environ.get("PANEL_ADMIN_PASSWORD", "")
            try:
                username = validate_username(username)
                validate_password(password)
            except ValueError as exc:
                raise RuntimeError(
                    "首次启动必须设置 PANEL_ADMIN_USERNAME 和长度至少 12 位的 PANEL_ADMIN_PASSWORD。"
                ) from exc
            identity_id = new_identity_id()
            cursor = connection.execute(
                "INSERT INTO users (identity_id, username, password_hash, role, active, created_at) VALUES (?, ?, ?, 'admin', 1, ?)",
                (identity_id, username, generate_password_hash(password), now()),
            )
            admin_id = int(cursor.lastrowid)
            manager = NftManager(
                app.config["FORWARD_CONFIG"], app.config["MAIN_CONFIG"], app.config["SYSCTL_CONFIG"]
            )
            # Adopt rules made by the supplied script, but do not load or change them.
            for rule in manager.import_rules_from_config():
                connection.execute(
                    "INSERT OR IGNORE INTO forward_rules (listen_port, destination_ip, destination_port, owner_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (rule.listen_port, rule.destination_ip, rule.destination_port, admin_id, now()),
                )
            connection.execute(
                "INSERT INTO audit_events (actor_id, action, target, details, remote_addr, created_at) VALUES (?, 'bootstrap', ?, ?, '', ?)",
                (admin_id, identity_id, f"首次管理员账户已创建; username={username}", now()),
            )
        connection.commit()
    finally:
        connection.close()


def add_audit(action: str, target: str, details: str = "", actor_id: int | None = None) -> None:
    connection = get_db()
    connection.execute(
        "INSERT INTO audit_events (actor_id, action, target, details, remote_addr, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (actor_id if actor_id is not None else session.get("user_id"), action, target, details, request.remote_addr or "", now()),
    )


def current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or not user["active"] or session.get("session_version") != int(user["session_version"]):
        session.clear()
        return None
    return user


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def require_csrf() -> None:
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, "CSRF 校验失败，请刷新页面后重试。")


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if current_user() is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    @login_required
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if current_user()["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def page_context(**extra: Any) -> dict[str, Any]:
    return {"user": current_user(), "csrf_token": csrf_token(), **extra}


def _rate_limit_remaining(remote_addr: str) -> int:
    connection = get_db()
    current = int(time.time())
    cutoff = current - LOGIN_WINDOW_SECONDS
    connection.execute("DELETE FROM login_attempts WHERE attempted_at <= ?", (cutoff,))
    row = connection.execute(
        "SELECT COUNT(*) AS attempts, MIN(attempted_at) AS oldest FROM login_attempts WHERE remote_addr = ?",
        (remote_addr,),
    ).fetchone()
    if int(row["attempts"]) < LOGIN_MAX_ATTEMPTS:
        return 0
    return max(1, int(row["oldest"]) + LOGIN_WINDOW_SECONDS - current)


def _record_failed_login(remote_addr: str) -> None:
    get_db().execute(
        "INSERT INTO login_attempts (remote_addr, attempted_at) VALUES (?, ?)",
        (remote_addr[:255], int(time.time())),
    )


def _clear_failed_logins(remote_addr: str) -> None:
    get_db().execute("DELETE FROM login_attempts WHERE remote_addr = ?", (remote_addr,))


def monthly_usage(connection: sqlite3.Connection, user: sqlite3.Row | dict[str, Any], at: datetime | None = None) -> int:
    start = monthly_cycle_start(user, at).strftime(UTC_TIME_FORMAT)
    row = connection.execute(
        "SELECT COALESCE(SUM(inbound_bytes) + SUM(outbound_bytes), 0) FROM traffic_buckets WHERE owner_id=? AND bucket_at>=?",
        (user["id"], start),
    ).fetchone()
    return int(row[0])


def desired_pause_reason(user: sqlite3.Row | dict[str, Any], usage: int, at: datetime | None = None) -> str:
    if not int(user["active"]):
        return "disabled"
    current = at or datetime.now(timezone.utc)
    expiry = parse_utc(user["expires_at"])
    if expiry is not None and current >= expiry:
        return "expired"
    quota = int(user["monthly_quota_bytes"])
    if quota and usage >= quota:
        return "quota"
    return ""


def reconcile_rule_state(app: Flask, at: datetime | None = None, force_apply: bool = False) -> bool:
    """Persist and load only rules whose accounts are currently allowed to forward."""
    connection = sqlite3.connect(app.config["DATABASE"], timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with ApplyLock(Path(app.config["DATA_DIR"])):
            rows = connection.execute(
                "SELECT r.*, u.identity_id, u.username, u.active, u.expires_at, u.monthly_quota_bytes, u.monthly_reset_day, u.monthly_reset_minute "
                "FROM forward_rules r JOIN users u ON u.id=r.owner_id ORDER BY r.listen_port"
            ).fetchall()
            if not rows:
                return False
            manager = NftManager(app.config["FORWARD_CONFIG"], app.config["MAIN_CONFIG"], app.config["SYSCTL_CONFIG"])
            TrafficMonitor(manager).sample(connection, rows)
            check_time = at or datetime.now(timezone.utc)
            usage_by_owner: dict[int, int] = {}
            reasons: dict[int, str] = {}
            for row in rows:
                owner_id = int(row["owner_id"])
                if owner_id not in usage_by_owner:
                    usage_by_owner[owner_id] = monthly_usage(connection, row, check_time)
                reasons[int(row["id"])] = desired_pause_reason(row, usage_by_owner[owner_id], check_time)
            changed = [row for row in rows if (row["paused_reason"] or "") != reasons[int(row["id"])]]
            if not changed and not force_apply:
                return False
            enabled = [row_to_rule(row) for row in rows if not reasons[int(row["id"])]]
            manager.apply_rules(enabled)
            for row in changed:
                reason = reasons[int(row["id"])]
                connection.execute("UPDATE forward_rules SET paused_reason=? WHERE id=?", (reason, row["id"]))
                action = "rule_paused" if reason else "rule_resumed"
                details = f"{row['listen_port']} → {row['destination_ip']}:{row['destination_port']}; reason={reason or 'policy cleared'}"
                connection.execute(
                    "INSERT INTO audit_events (actor_id, action, target, details, remote_addr, created_at) VALUES (NULL, ?, ?, ?, 'policy-scheduler', ?)",
                    (action, row["identity_id"], details, now()),
                )
            connection.commit()
            return True
    finally:
        connection.close()


def start_policy_scheduler(app: Flask) -> None:
    if not app.config.get("POLICY_SCHEDULER_ENABLED", True):
        return

    def worker() -> None:
        while True:
            try:
                reconcile_rule_state(app)
            except Exception:
                app.logger.exception("Rule policy reconciliation failed")
            time.sleep(POLICY_INTERVAL_SECONDS)

    threading.Thread(target=worker, name="nfp-policy-scheduler", daemon=True).start()


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    data_dir = Path(os.environ.get("PANEL_DATA_DIR", "/var/lib/nft-forward-panel"))
    secret_key = os.environ.get("PANEL_SECRET_KEY")
    if not secret_key and not test_config:
        raise RuntimeError("必须设置 PANEL_SECRET_KEY（例如 openssl rand -hex 32）。")

    app = Flask(__name__)
    proxy_count = max(0, int(os.environ.get("PANEL_TRUSTED_PROXY_COUNT", "0")))
    if proxy_count:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_count, x_proto=proxy_count, x_host=proxy_count)
    app.config.from_mapping(
        SECRET_KEY=secret_key or "test-secret-only",
        DATA_DIR=str(data_dir),
        DATABASE=str(data_dir / "panel.db"),
        FORWARD_CONFIG=os.environ.get("PANEL_FORWARD_CONFIG", "/etc/nftables.d/port-forward.conf"),
        MAIN_CONFIG=os.environ.get("PANEL_MAIN_CONFIG", "/etc/nftables.conf"),
        SYSCTL_CONFIG=os.environ.get("PANEL_SYSCTL_CONFIG", "/etc/sysctl.d/99-nft-forward.conf"),
        UPLOAD_DIR=str(data_dir / "avatars"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("PANEL_COOKIE_SECURE", "1") == "1",
        MAX_CONTENT_LENGTH=MAX_AVATAR_BYTES + 64 * 1024,
        POLICY_SCHEDULER_ENABLED=os.environ.get("PANEL_POLICY_SCHEDULER", "1") == "1",
    )
    if test_config:
        app.config.update(test_config)
        if "POLICY_SCHEDULER_ENABLED" not in test_config:
            app.config["POLICY_SCHEDULER_ENABLED"] = False

    init_schema(app)
    app.jinja_env.filters["expiry_time"] = format_expiry
    app.jinja_env.filters["expiry_input"] = expiry_input_value
    app.jinja_env.filters["reset_schedule"] = format_reset_schedule
    app.jinja_env.filters["pause_label"] = pause_label
    app.teardown_appcontext(close_db)
    start_policy_scheduler(app)

    @app.after_request
    def add_security_headers(response: Any) -> Any:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/")
    @login_required
    def dashboard() -> str:
        user = current_user()
        connection = get_db()
        if user["role"] == "admin":
            rules = connection.execute(
                "SELECT r.*, u.username AS owner_name, u.identity_id AS owner_identity_id, u.expires_at AS owner_expires_at, u.monthly_reset_day AS owner_reset_day, u.monthly_reset_minute AS owner_reset_minute FROM forward_rules r JOIN users u ON u.id = r.owner_id ORDER BY r.listen_port"
            ).fetchall()
            owners = connection.execute("SELECT id, identity_id, username, max_rules, port_min, port_max, default_inbound_mbps, default_outbound_mbps, expires_at, monthly_reset_day, monthly_reset_minute FROM users WHERE active = 1 ORDER BY username").fetchall()
        else:
            rules = connection.execute(
                "SELECT r.*, u.username AS owner_name, u.identity_id AS owner_identity_id, u.expires_at AS owner_expires_at, u.monthly_reset_day AS owner_reset_day, u.monthly_reset_minute AS owner_reset_minute FROM forward_rules r JOIN users u ON u.id = r.owner_id WHERE r.owner_id = ? ORDER BY r.listen_port",
                (user["id"],),
            ).fetchall()
            owners = []
        try:
            status = NftManager(app.config["FORWARD_CONFIG"], app.config["MAIN_CONFIG"], app.config["SYSCTL_CONFIG"]).status()
        except NftOperationError as exc:
            status = {"nft_available": False, "nft_table_loaded": False, "ip_forward": False, "firewall_available": False, "firewall": f"检测失败：{exc}"}
        return render_template("dashboard.html", **page_context(rules=rules, owners=owners, status=status))

    @app.route("/login", methods=["GET", "POST"])
    def login() -> str | Any:
        if current_user():
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            require_csrf()
            address = request.remote_addr or "unknown"
            retry_after = _rate_limit_remaining(address)
            if retry_after:
                retry_minutes = max(1, (retry_after + 59) // 60)
                flash(f"登录尝试过于频繁，请约 {retry_minutes} 分钟后再试。", "error")
                return render_template("login.html", **page_context())
            identifier = request.form.get("identifier", "").strip()
            matches = get_db().execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE LIMIT 2",
                (identifier, identifier),
            ).fetchall()
            user = matches[0] if len(matches) == 1 else None
            if user is None or not user["active"] or not check_password_hash(user["password_hash"], request.form.get("password", "")):
                _record_failed_login(address)
                add_audit("login_failed", identifier or "(empty)", "登录失败", actor_id=None)
                get_db().commit()
                flash("用户名/邮箱或密码错误。", "error")
                return render_template("login.html", **page_context())
            _clear_failed_logins(address)
            session.clear()
            session["user_id"] = user["id"]
            session["session_version"] = int(user["session_version"])
            session["csrf_token"] = secrets.token_urlsafe(32)
            add_audit("login", user["identity_id"], f"username={user['username']}; 登录成功", actor_id=user["id"])
            get_db().commit()
            return redirect(url_for("dashboard"))
        return render_template("login.html", **page_context())

    @app.post("/logout")
    @login_required
    def logout() -> Any:
        require_csrf()
        user = current_user()
        add_audit("logout", user["identity_id"], f"username={user['username']}; 退出登录")
        get_db().commit()
        session.clear()
        return redirect(url_for("login"))

    @app.post("/rules")
    @login_required
    def add_rule() -> Any:
        require_csrf()
        user = current_user()
        manager = NftManager(app.config["FORWARD_CONFIG"], app.config["MAIN_CONFIG"], app.config["SYSCTL_CONFIG"])
        try:
            owner_id = parse_bounded_int(request.form.get("owner_id"), "规则所有者", 1, 2 ** 63 - 1) if user["role"] == "admin" else user["id"]
            owner = get_db().execute("SELECT * FROM users WHERE id = ?", (owner_id,)).fetchone()
            if owner is None or not owner["active"]:
                raise NftOperationError("请选择一个有效的启用用户作为规则所有者。")
            listen_port = manager.validate_port(request.form.get("listen_port", ""))
            if not int(owner["port_min"]) <= listen_port <= int(owner["port_max"]):
                raise NftOperationError(f"该用户只能使用 {owner['port_min']}–{owner['port_max']} 范围内的监听端口。")
            rule_count = get_db().execute("SELECT COUNT(*) FROM forward_rules WHERE owner_id = ?", (owner_id,)).fetchone()[0]
            if int(owner["max_rules"]) > 0 and rule_count >= int(owner["max_rules"]):
                raise NftOperationError(f"该用户最多可创建 {owner['max_rules']} 条转发规则。")
            if user["role"] == "admin":
                inbound_limit = parse_bounded_int(request.form.get("inbound_limit_mbps", "0"), "入站带宽限制", 0, 100000)
                outbound_limit = parse_bounded_int(request.form.get("outbound_limit_mbps", "0"), "出站带宽限制", 0, 100000)
            else:
                inbound_limit = int(owner["default_inbound_mbps"])
                outbound_limit = int(owner["default_outbound_mbps"])
            owner_usage = monthly_usage(get_db(), owner)
            initial_pause = desired_pause_reason(owner, owner_usage)
            note = validate_rule_note(request.form.get("note", ""))
            new_rule = ForwardRule(
                id=None,
                listen_port=listen_port,
                destination_ip=manager.validate_ipv4(request.form.get("destination_ip", "")),
                destination_port=manager.validate_port(request.form.get("destination_port", "")),
                owner_id=owner_id,
                inbound_limit_mbps=inbound_limit,
                outbound_limit_mbps=outbound_limit,
            )
            force_conflict = user["role"] == "admin" and request.form.get("force_conflict") == "1"
            if manager.listening_port_in_use(new_rule.listen_port) and not force_conflict:
                raise NftOperationError("该端口已被本机服务监听。仅管理员确认风险后可以继续添加。")
            with ApplyLock(Path(app.config["DATA_DIR"])):
                connection = get_db()
                existing = connection.execute("SELECT * FROM forward_rules ORDER BY listen_port").fetchall()
                if any(item["listen_port"] == new_rule.listen_port for item in existing):
                    raise NftOperationError(f"监听端口 {new_rule.listen_port} 已有转发规则。")
                candidates = [row_to_rule(item) for item in existing if not item["paused_reason"]]
                if not initial_pause:
                    candidates.append(new_rule)
                manager.apply_rules(candidates)
                cursor = connection.execute(
                    "INSERT INTO forward_rules (listen_port, destination_ip, destination_port, owner_id, inbound_limit_mbps, outbound_limit_mbps, paused_reason, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (new_rule.listen_port, new_rule.destination_ip, new_rule.destination_port, owner_id, inbound_limit, outbound_limit, initial_pause, note, now()),
                )
                warnings = manager.firewall_open(new_rule) if not initial_pause else []
                add_audit("rule_create", str(cursor.lastrowid), f"{new_rule.listen_port} → {new_rule.destination_ip}:{new_rule.destination_port}; owner={owner['username']}; note={note or '(empty)'}; paused={initial_pause or 'no'}; {'; '.join(warnings)}")
                connection.commit()
            if initial_pause:
                flash(f"端口转发已保存，但该用户{pause_label(initial_pause)}，规则暂不加载。", "success")
            else:
                flash("端口转发已添加并加载。" + (" " + " ".join(warnings) if warnings else ""), "success")
        except (ValueError, NftOperationError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.post("/rules/<int:rule_id>/edit")
    @login_required
    def edit_rule(rule_id: int) -> Any:
        require_csrf()
        user = current_user()
        manager = NftManager(app.config["FORWARD_CONFIG"], app.config["MAIN_CONFIG"], app.config["SYSCTL_CONFIG"])
        try:
            with ApplyLock(Path(app.config["DATA_DIR"])):
                connection = get_db()
                current = connection.execute("SELECT * FROM forward_rules WHERE id=?", (rule_id,)).fetchone()
                if current is None:
                    raise NftOperationError("这条规则不存在或已被删除。")
                if user["role"] != "admin" and int(current["owner_id"]) != int(user["id"]):
                    abort(403)

                owner_id = parse_bounded_int(request.form.get("owner_id"), "规则所有者", 1, 2 ** 63 - 1) if user["role"] == "admin" else int(user["id"])
                owner = connection.execute("SELECT * FROM users WHERE id=?", (owner_id,)).fetchone()
                if owner is None or not owner["active"]:
                    raise NftOperationError("请选择一个有效的启用用户作为规则所有者。")
                listen_port = manager.validate_port(request.form.get("listen_port", ""))
                if not int(owner["port_min"]) <= listen_port <= int(owner["port_max"]):
                    raise NftOperationError(f"该用户只能使用 {owner['port_min']}–{owner['port_max']} 范围内的监听端口。")
                owner_rule_count = connection.execute(
                    "SELECT COUNT(*) FROM forward_rules WHERE owner_id=? AND id<>?", (owner_id, rule_id)
                ).fetchone()[0]
                if int(owner["max_rules"]) > 0 and owner_rule_count >= int(owner["max_rules"]):
                    raise NftOperationError(f"该用户最多可创建 {owner['max_rules']} 条转发规则。")
                if connection.execute("SELECT 1 FROM forward_rules WHERE listen_port=? AND id<>?", (listen_port, rule_id)).fetchone():
                    raise NftOperationError(f"监听端口 {listen_port} 已有转发规则。")
                if listen_port != int(current["listen_port"]) and manager.listening_port_in_use(listen_port):
                    if user["role"] != "admin" or request.form.get("force_conflict") != "1":
                        raise NftOperationError("该端口已被本机服务监听。仅管理员确认风险后可以继续修改。")

                if user["role"] == "admin":
                    inbound_limit = parse_bounded_int(request.form.get("inbound_limit_mbps", "0"), "入站带宽限制", 0, 100000)
                    outbound_limit = parse_bounded_int(request.form.get("outbound_limit_mbps", "0"), "出站带宽限制", 0, 100000)
                else:
                    inbound_limit = int(owner["default_inbound_mbps"])
                    outbound_limit = int(owner["default_outbound_mbps"])
                note = validate_rule_note(request.form.get("note", ""))
                updated = ForwardRule(
                    id=rule_id,
                    listen_port=listen_port,
                    destination_ip=manager.validate_ipv4(request.form.get("destination_ip", "")),
                    destination_port=manager.validate_port(request.form.get("destination_port", "")),
                    owner_id=owner_id,
                    inbound_limit_mbps=inbound_limit,
                    outbound_limit_mbps=outbound_limit,
                )
                pause_reason = desired_pause_reason(owner, monthly_usage(connection, owner))
                all_rows = connection.execute("SELECT * FROM forward_rules ORDER BY listen_port").fetchall()
                candidates = [row_to_rule(row) for row in all_rows if int(row["id"]) != rule_id and not row["paused_reason"]]
                if not pause_reason:
                    candidates.append(updated)
                manager.apply_rules(candidates)

                previous = row_to_rule(current)
                connection.execute(
                    """UPDATE forward_rules SET listen_port=?, destination_ip=?, destination_port=?, owner_id=?,
                       inbound_limit_mbps=?, outbound_limit_mbps=?, paused_reason=?, note=? WHERE id=?""",
                    (listen_port, updated.destination_ip, updated.destination_port, owner_id,
                     inbound_limit, outbound_limit, pause_reason, note, rule_id),
                )
                connection.execute("DELETE FROM rule_counter_state WHERE rule_id=?", (rule_id,))
                warnings = manager.firewall_open(updated) if not pause_reason else []
                old_destination_used = any(
                    int(row["id"]) != rule_id and row["destination_ip"] == previous.destination_ip
                    and int(row["destination_port"]) == previous.destination_port for row in all_rows
                )
                firewall_target_changed = (
                    previous.listen_port != updated.listen_port
                    or (previous.destination_ip, previous.destination_port) != (updated.destination_ip, updated.destination_port)
                )
                if firewall_target_changed:
                    warnings.extend(manager.firewall_close(previous, old_destination_used))
                add_audit(
                    "rule_update", str(rule_id),
                    f"{previous.listen_port} → {previous.destination_ip}:{previous.destination_port}; "
                    f"updated={updated.listen_port} → {updated.destination_ip}:{updated.destination_port}; "
                    f"owner={owner['username']}; note={note or '(empty)'}; paused={pause_reason or 'no'}; {'; '.join(warnings)}",
                )
                connection.commit()
            flash("转发规则已修改并重新加载。" + (" " + " ".join(warnings) if warnings else ""), "success")
        except (ValueError, NftOperationError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.post("/rules/<int:rule_id>/delete")
    @login_required
    def delete_rule(rule_id: int) -> Any:
        require_csrf()
        if request.form.get("confirm_delete") != "1":
            abort(400)
        user = current_user()
        manager = NftManager(app.config["FORWARD_CONFIG"], app.config["MAIN_CONFIG"], app.config["SYSCTL_CONFIG"])
        try:
            with ApplyLock(Path(app.config["DATA_DIR"])):
                connection = get_db()
                row = connection.execute("SELECT * FROM forward_rules WHERE id = ?", (rule_id,)).fetchone()
                if row is None:
                    raise NftOperationError("这条规则不存在或已被删除。")
                if user["role"] != "admin" and row["owner_id"] != user["id"]:
                    abort(403)
                removed = row_to_rule(row)
                all_rules = connection.execute("SELECT * FROM forward_rules ORDER BY listen_port").fetchall()
                remaining = [row_to_rule(item) for item in all_rules if item["id"] != rule_id and not item["paused_reason"]]
                manager.apply_rules(remaining)
                shared_destination = any(
                    item.destination_ip == removed.destination_ip and item.destination_port == removed.destination_port
                    for item in remaining
                )
                connection.execute("DELETE FROM forward_rules WHERE id = ?", (rule_id,))
                warnings = manager.firewall_close(removed, shared_destination)
                add_audit("rule_delete", str(rule_id), f"{removed.listen_port} → {removed.destination_ip}:{removed.destination_port}; {'; '.join(warnings)}")
                connection.commit()
            flash("端口转发已删除。" + (" " + " ".join(warnings) if warnings else ""), "success")
        except NftOperationError as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.get("/users")
    @admin_required
    def users() -> str:
        connection = get_db()
        records = []
        for account in connection.execute("SELECT * FROM users ORDER BY role DESC, username").fetchall():
            item = dict(account)
            item["monthly_bytes"] = monthly_usage(connection, account)
            item["effective_state"] = desired_pause_reason(account, item["monthly_bytes"]) or ("" if account["active"] else "disabled")
            records.append(item)
        return render_template("users.html", **page_context(users=records))

    @app.post("/users")
    @admin_required
    def create_user() -> Any:
        require_csrf()
        try:
            username = validate_username(request.form.get("username", ""))
            email = validate_email(request.form.get("email", ""))
            password = validate_password(request.form.get("password", ""))
            role = request.form.get("role", "user")
            if role not in {"admin", "user"}:
                raise ValueError("无效角色。")
            connection = get_db()
            if account_identifier_conflicts(connection, username, email):
                raise ValueError("用户名或邮箱与现有账户标识冲突。")
            identity_id = new_identity_id()
            cursor = connection.execute(
                "INSERT INTO users (identity_id, username, email, password_hash, role, active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (identity_id, username, email, generate_password_hash(password), role, now()),
            )
            add_audit("user_create", identity_id, f"username={username}; email={email}; role={role}; internal_id={cursor.lastrowid}")
            connection.commit()
            flash("账户已创建。", "success")
        except (ValueError, sqlite3.IntegrityError) as exc:
            flash("用户名或邮箱已存在。" if isinstance(exc, sqlite3.IntegrityError) else str(exc), "error")
        return redirect(url_for("users"))

    @app.post("/users/<int:user_id>/toggle")
    @admin_required
    def toggle_user(user_id: int) -> Any:
        require_csrf()
        actor = current_user()
        connection = get_db()
        target = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            flash("账户不存在。", "error")
        elif target["id"] == actor["id"]:
            flash("不能停用自己的账户。", "error")
        elif target["active"] and target["role"] == "admin" and connection.execute("SELECT COUNT(*) FROM users WHERE active = 1 AND role = 'admin'").fetchone()[0] <= 1:
            flash("至少需要保留一个启用的管理员。", "error")
        else:
            next_state = 0 if target["active"] else 1
            connection.execute("UPDATE users SET active = ?, session_version = session_version + 1 WHERE id = ?", (next_state, user_id))
            add_audit("user_toggle", target["identity_id"], f"username={target['username']}; active={next_state}; internal_id={user_id}")
            connection.commit()
            try:
                reconcile_rule_state(app, force_apply=True)
            except NftOperationError as exc:
                flash(f"账户状态已更新，但转发规则重载失败：{exc}", "error")
            else:
                flash("账户状态和转发规则已更新。", "success")
        return redirect(url_for("users"))

    @app.post("/users/<int:user_id>/policy")
    @admin_required
    def update_user_policy(user_id: int) -> Any:
        require_csrf()
        try:
            max_rules = parse_bounded_int(request.form.get("max_rules"), "规则上限", 0, 10000)
            port_min = NftManager.validate_port(request.form.get("port_min", "1024"))
            port_max = NftManager.validate_port(request.form.get("port_max", "65535"))
            inbound = parse_bounded_int(request.form.get("default_inbound_mbps"), "默认入站带宽限制", 0, 100000)
            outbound = parse_bounded_int(request.form.get("default_outbound_mbps"), "默认出站带宽限制", 0, 100000)
            quota_gib = parse_bounded_float(request.form.get("monthly_quota_gib"), "月流量额度", 0, 1048576)
            expires_at = validate_expiry(request.form.get("expires_at", ""))
            reset_day, reset_minute = validate_reset_schedule(request.form.get("monthly_reset_day", "1"), request.form.get("monthly_reset_time", "00:00"))
            entry_address = validate_entry_address(request.form.get("entry_address", ""))
            if port_min > port_max:
                raise ValueError("端口范围起始值不能大于结束值。")
            quota_bytes = int(quota_gib * 1024 ** 3)
            connection = get_db()
            target = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if target is None:
                raise ValueError("账户不存在。")
            previous_policy = dict(target)
            previous_limits = connection.execute(
                "SELECT id, inbound_limit_mbps, outbound_limit_mbps FROM forward_rules WHERE owner_id=?",
                (user_id,),
            ).fetchall()
            connection.execute(
                "UPDATE users SET max_rules=?, port_min=?, port_max=?, default_inbound_mbps=?, default_outbound_mbps=?, monthly_quota_bytes=?, expires_at=?, monthly_reset_day=?, monthly_reset_minute=?, entry_address=? WHERE id=?",
                (max_rules, port_min, port_max, inbound, outbound, quota_bytes, expires_at, reset_day, reset_minute, entry_address, user_id),
            )
            connection.execute(
                "UPDATE forward_rules SET inbound_limit_mbps=?, outbound_limit_mbps=? WHERE owner_id=?",
                (inbound, outbound, user_id),
            )
            connection.commit()
            try:
                reconcile_rule_state(app, force_apply=True)
            except NftOperationError:
                connection.execute(
                    "UPDATE users SET max_rules=?, port_min=?, port_max=?, default_inbound_mbps=?, default_outbound_mbps=?, monthly_quota_bytes=?, expires_at=?, monthly_reset_day=?, monthly_reset_minute=?, entry_address=? WHERE id=?",
                    (previous_policy["max_rules"], previous_policy["port_min"], previous_policy["port_max"],
                     previous_policy["default_inbound_mbps"], previous_policy["default_outbound_mbps"],
                     previous_policy["monthly_quota_bytes"], previous_policy["expires_at"],
                     previous_policy["monthly_reset_day"], previous_policy["monthly_reset_minute"],
                     previous_policy["entry_address"], user_id),
                )
                connection.executemany(
                    "UPDATE forward_rules SET inbound_limit_mbps=?, outbound_limit_mbps=? WHERE id=?",
                    [(row["inbound_limit_mbps"], row["outbound_limit_mbps"], row["id"]) for row in previous_limits],
                )
                connection.commit()
                try:
                    reconcile_rule_state(app, force_apply=True)
                except NftOperationError:
                    app.logger.exception("Failed to restore nftables after rolling back a user policy update")
                raise
            add_audit("user_policy", target["identity_id"], f"username={target['username']}; rules={max_rules}; ports={port_min}-{port_max}; in={inbound}; out={outbound}; monthly_quota_bytes={quota_bytes}; expires_at={expires_at or 'never'}; monthly_reset={reset_day}/{reset_minute}; entry_address={entry_address or '(unset)'}")
            connection.commit()
            flash("用户转发策略已更新，到期和额度状态已重新核算。", "success")
        except (ValueError, NftOperationError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("users"))

    @app.post("/users/<int:user_id>/password")
    @admin_required
    def reset_password(user_id: int) -> Any:
        require_csrf()
        try:
            password = validate_password(request.form.get("password", ""))
            connection = get_db()
            target = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if target is None:
                raise ValueError("账户不存在。")
            connection.execute(
                "UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                (generate_password_hash(password), user_id),
            )
            add_audit("password_reset", target["identity_id"], f"username={target['username']}; 管理员重置密码")
            connection.commit()
            flash("密码已重置。", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("users"))

    def visible_rules(connection: sqlite3.Connection, user: sqlite3.Row) -> list[sqlite3.Row]:
        sql = "SELECT r.*, u.username AS owner_name, u.identity_id AS owner_identity_id, u.expires_at AS owner_expires_at, u.monthly_reset_day AS owner_reset_day, u.monthly_reset_minute AS owner_reset_minute FROM forward_rules r JOIN users u ON u.id=r.owner_id"
        if user["role"] == "admin":
            return connection.execute(sql + " ORDER BY r.listen_port").fetchall()
        return connection.execute(sql + " WHERE r.owner_id=? ORDER BY r.listen_port", (user["id"],)).fetchall()

    @app.get("/api/overview")
    @login_required
    def overview_api() -> Any:
        user = current_user()
        connection = get_db()
        rules = visible_rules(connection, user)
        manager = NftManager(app.config["FORWARD_CONFIG"], app.config["MAIN_CONFIG"], app.config["SYSCTL_CONFIG"])
        with ApplyLock(Path(app.config["DATA_DIR"])):
            live = TrafficMonitor(manager).sample(connection, rules)
        connections = manager.connection_counts()
        rows = []
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(rules)))) as executor:
            probes = list(executor.map(lambda item: probe_tcp(item["destination_ip"], int(item["destination_port"])) if not item["paused_reason"] else {"reachable": False, "latency_ms": None}, rules))
        for rule, reachability in zip(rules, probes):
            rows.append({
                "id": rule["id"], "owner": rule["owner_name"], "listen_port": rule["listen_port"],
                "destination": f"{rule['destination_ip']}:{rule['destination_port']}",
                "reachable": reachability["reachable"], "latency_ms": reachability["latency_ms"],
                "inbound_bps": live.get(rule["id"], {}).get("inbound_bps", 0),
                "outbound_bps": live.get(rule["id"], {}).get("outbound_bps", 0),
                "connections": 0 if rule["paused_reason"] else connections.get(int(rule["listen_port"]), 0),
                "paused_reason": rule["paused_reason"], "paused_label": pause_label(rule["paused_reason"]),
                "inbound_limit_mbps": rule["inbound_limit_mbps"], "outbound_limit_mbps": rule["outbound_limit_mbps"],
            })
        if user["role"] == "admin":
            monthly_bytes = sum(monthly_usage(connection, account) for account in connection.execute("SELECT * FROM users").fetchall())
            monthly_quota = 0
        else:
            monthly_bytes = monthly_usage(connection, user)
            monthly_quota = int(user["monthly_quota_bytes"])
        return jsonify({"rules": rows, "totals": {
            "inbound_bps": sum(float(row["inbound_bps"]) for row in rows),
            "outbound_bps": sum(float(row["outbound_bps"]) for row in rows),
            "connections": sum(int(row["connections"]) for row in rows),
            "monthly_bytes": monthly_bytes,
            "monthly_quota_bytes": monthly_quota,
            "expires_at": format_expiry(user["expires_at"]) if user["role"] != "admin" else "",
            "monthly_reset": format_reset_schedule(int(user["monthly_reset_day"]), int(user["monthly_reset_minute"])) if user["role"] != "admin" else "",
        }, "sampled_at": now()})

    @app.get("/analytics/<kind>")
    @login_required
    def analytics(kind: str) -> str:
        if kind not in {"bandwidth", "traffic", "connections"}:
            abort(404)
        return render_template("analytics.html", **page_context(kind=kind))

    @app.get("/api/history")
    @login_required
    def history_api() -> Any:
        user = current_user()
        try:
            days = min(60, max(1, int(request.args.get("days", "7"))))
        except ValueError:
            days = 7
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S UTC")
        params: list[Any] = [cutoff_text]
        owner_filter = ""
        if user["role"] != "admin":
            owner_filter = " AND t.owner_id=?"
            params.append(user["id"])
        rows = get_db().execute(
            """SELECT substr(t.bucket_at,1,10) AS period, SUM(t.inbound_bytes) AS inbound,
                      SUM(t.outbound_bytes) AS outbound
               FROM traffic_buckets t WHERE t.bucket_at>=?""" + owner_filter + " GROUP BY period ORDER BY period",
            params,
        ).fetchall()
        return jsonify({"days": days, "points": [dict(row) for row in rows]})

    @app.get("/audit")
    @admin_required
    def audit() -> str:
        events = get_db().execute(
            "SELECT a.*, COALESCE(u.username, '系统/已删除用户') AS actor_name FROM audit_events a LEFT JOIN users u ON u.id = a.actor_id ORDER BY a.id DESC LIMIT 300"
        ).fetchall()
        return render_template("audit.html", **page_context(events=events))

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile() -> str | Any:
        if request.method == "POST":
            require_csrf()
            user = current_user()
            try:
                username = validate_username(request.form.get("username", ""))
                email = validate_email(request.form.get("email", ""))
                avatar = request.files.get("avatar")
                avatar_filename = user["avatar_filename"]
                new_avatar_path: Path | None = None
                old_avatar_path: Path | None = None
                if avatar and avatar.filename:
                    payload = avatar.read(MAX_AVATAR_BYTES + 1)
                    if len(payload) > MAX_AVATAR_BYTES:
                        raise ValueError("头像文件必须小于 5 MiB。")
                    extension = image_extension(payload)
                    upload_dir = Path(app.config["UPLOAD_DIR"])
                    upload_dir.mkdir(parents=True, exist_ok=True)
                    os.chmod(upload_dir, 0o700)
                    avatar_filename = f"{user['id']}-{secrets.token_hex(12)}.{extension}"
                    new_avatar_path = upload_dir / avatar_filename
                    new_avatar_path.write_bytes(payload)
                    os.chmod(new_avatar_path, 0o600)
                    if user["avatar_filename"]:
                        old_avatar_path = upload_dir / user["avatar_filename"]
                connection = get_db()
                try:
                    if account_identifier_conflicts(connection, username, email, int(user["id"])):
                        raise ValueError("用户名或邮箱与现有账户标识冲突。")
                    connection.execute("UPDATE users SET username=?, email=?, avatar_filename=? WHERE id=?", (username, email, avatar_filename, user["id"]))
                    add_audit("profile_update", user["identity_id"], f"username={user['username']}->{username}; email={user['email'] or ''}->{email}; avatar={bool(avatar_filename)}")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    if new_avatar_path:
                        new_avatar_path.unlink(missing_ok=True)
                    raise
                if old_avatar_path:
                    old_avatar_path.unlink(missing_ok=True)
                flash("个人资料已更新。", "success")
            except (ValueError, sqlite3.IntegrityError) as exc:
                flash("用户名或邮箱已被使用。" if isinstance(exc, sqlite3.IntegrityError) else str(exc), "error")
            return redirect(url_for("profile"))
        return render_template("profile.html", **page_context())

    @app.post("/profile/password")
    @login_required
    def profile_password() -> Any:
        require_csrf()
        user = current_user()
        record = get_db().execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not check_password_hash(record["password_hash"], request.form.get("current_password", "")):
            flash("当前密码不正确。", "error")
        else:
            try:
                password = validate_password(request.form.get("new_password", ""))
                connection = get_db()
                connection.execute(
                    "UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                    (generate_password_hash(password), user["id"]),
                )
                add_audit("password_change", user["identity_id"], f"username={user['username']}; 用户修改自己的密码")
                connection.commit()
                session["session_version"] = int(user["session_version"]) + 1
                flash("密码已更新。", "success")
            except ValueError as exc:
                flash(str(exc), "error")
        return redirect(url_for("profile"))

    @app.get("/uploads/avatars/<path:filename>")
    @login_required
    def avatar_upload(filename: str) -> Any:
        return send_from_directory(app.config["UPLOAD_DIR"], filename, conditional=True)

    return app


def row_to_rule(row: sqlite3.Row) -> ForwardRule:
    return ForwardRule(
        id=int(row["id"]),
        listen_port=int(row["listen_port"]),
        destination_ip=str(row["destination_ip"]),
        destination_port=int(row["destination_port"]),
        owner_id=int(row["owner_id"]),
        inbound_limit_mbps=int(row["inbound_limit_mbps"]),
        outbound_limit_mbps=int(row["outbound_limit_mbps"]),
    )


app = create_app()
