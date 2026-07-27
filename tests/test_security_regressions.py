import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("PANEL_ADMIN_USERNAME", "admin")
os.environ.setdefault("PANEL_ADMIN_PASSWORD", "PreviewOnly!2026")
os.environ.setdefault("PANEL_SECRET_KEY", "test-secret")
os.environ.setdefault("PANEL_DATA_DIR", tempfile.mkdtemp(prefix="security-import-"))

from app import (
    create_app,
    desired_pause_reason,
    desired_rule_pause_reason,
    now,
    reconcile_rule_state,
    validate_rule_note,
)
from monitoring import TrafficMonitor
from nft_manager import NftOperationError
from werkzeug.security import generate_password_hash


def csrf_token(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    if not match:
        raise AssertionError("missing CSRF token")
    return match.group(1)


class SecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data_dir = Path(self.tmp.name) / "data"
        self.web = create_app({
            "TESTING": True,
            "DATA_DIR": str(data_dir),
            "DATABASE": str(data_dir / "panel.db"),
            "FORWARD_CONFIG": str(Path(self.tmp.name) / "port-forward.conf"),
            "MAIN_CONFIG": str(Path(self.tmp.name) / "nftables.conf"),
            "SYSCTL_CONFIG": str(Path(self.tmp.name) / "sysctl.conf"),
        })
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            connection.execute(
                "INSERT INTO users(identity_id, username, email, password_hash, role, active, created_at) VALUES (?, ?, ?, ?, 'user', 1, ?)",
                ("A1B2C3D4", "member", "member@example.com", generate_password_hash("MemberPass!2026"), "2026-01-01 00:00:00 UTC"),
            )
            connection.commit()
        finally:
            connection.close()

    def login(self, client, identifier, password):
        token = csrf_token(client.get("/login"))
        return client.post("/login", data={"csrf_token": token, "identifier": identifier, "password": password})

    def test_password_change_revokes_other_sessions(self):
        first = self.web.test_client()
        second = self.web.test_client()
        self.assertEqual(self.login(first, "member", "MemberPass!2026").status_code, 302)
        self.assertEqual(self.login(second, "member", "MemberPass!2026").status_code, 302)
        token = csrf_token(first.get("/profile"))
        response = first.post("/profile/password", data={
            "csrf_token": token,
            "current_password": "MemberPass!2026",
            "new_password": "MemberPass!2027",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(first.get("/profile").status_code, 200)
        self.assertEqual(second.get("/profile").status_code, 302)

    def test_disabled_account_is_a_pause_reason(self):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        try:
            user = connection.execute("SELECT * FROM users WHERE username='member'").fetchone()
            connection.execute("UPDATE users SET active=0 WHERE id=?", (user["id"],))
            connection.commit()
            user = connection.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            self.assertEqual(desired_pause_reason(user, 0), "disabled")
        finally:
            connection.close()

    def test_ambiguous_historical_identifier_is_rejected(self):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            connection.execute(
                "INSERT INTO users(identity_id, username, email, password_hash, role, active, created_at) VALUES (?, ?, ?, ?, 'user', 1, ?)",
                ("B5C6D7E8", "member@example.com", "legacy@example.com", generate_password_hash("LegacyPass!2026"), "2026-01-01 00:00:00 UTC"),
            )
            connection.commit()
        finally:
            connection.close()
        client = self.web.test_client()
        response = self.login(client, "member@example.com", "MemberPass!2026")
        self.assertEqual(response.status_code, 200)
        self.assertIn("用户名/邮箱或密码错误", response.get_data(as_text=True))
        self.assertEqual(client.get("/profile").status_code, 302)

    def test_security_headers_are_present(self):
        response = self.web.test_client().get("/login", base_url="https://panel.example.test")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("object-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("max-age=31536000", response.headers["Strict-Transport-Security"])
        self.assertIn("camera=()", response.headers["Permissions-Policy"])

    def test_toggle_reconciles_rules_immediately(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
        finally:
            connection.close()
        token = csrf_token(admin.get("/users"))
        with patch("app.reconcile_rule_state") as reconcile:
            response = admin.post(f"/users/{member_id}/toggle", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        reconcile.assert_called_once_with(self.web, force_apply=True)

    def test_toggle_rolls_back_account_state_when_rule_reload_fails(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
        finally:
            connection.close()
        token = csrf_token(admin.get("/users"))
        from nft_manager import NftOperationError
        with patch(
            "app.reconcile_rule_state",
            side_effect=[NftOperationError("reload failed"), None],
        ):
            response = admin.post(f"/users/{member_id}/toggle", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            self.assertEqual(
                connection.execute("SELECT active FROM users WHERE id=?", (member_id,)).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_rule_note_validation(self):
        self.assertEqual(validate_rule_note("  香港入口  "), "香港入口")
        self.assertEqual(validate_rule_note("🇨🇳 中国入口 🚀"), "🇨🇳 中国入口 🚀")
        with self.assertRaises(ValueError):
            validate_rule_note("x" * 81)
        with self.assertRaises(ValueError):
            validate_rule_note("第一行" + chr(10) + "第二行")

    def test_rule_note_column_is_initialized(self):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(forward_rules)")}
            self.assertIn("note", columns)
        finally:
            connection.close()

    def test_rule_delete_requires_explicit_confirmation(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        token = csrf_token(admin.get("/"))
        response = admin.post("/rules/999/delete", data={"csrf_token": token})
        self.assertEqual(response.status_code, 400)

    def test_deleting_rule_keeps_monthly_traffic_history(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id, paused_reason, created_at)
                   VALUES (10115, '8.8.8.8', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                (member_id,),
            )
            rule_id = cursor.lastrowid
            connection.execute(
                """INSERT INTO traffic_buckets
                   (rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes)
                   VALUES (?, ?, ?, 1024, 2048)""",
                (rule_id, member_id, now()),
            )
            connection.commit()
        finally:
            connection.close()

        token = csrf_token(admin.get("/"))
        with (
            patch("app.NftManager.apply_rules"),
            patch("app.NftManager.firewall_close", return_value=[]),
            patch("app.TrafficMonitor.sample", return_value={}),
        ):
            response = admin.post(
                f"/rules/{rule_id}/delete",
                data={"csrf_token": token, "confirm_delete": "1"},
            )
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT inbound_bytes, outbound_bytes FROM traffic_buckets WHERE rule_id=?",
                    (rule_id,),
                ).fetchone(),
                (1024, 2048),
            )
        finally:
            connection.close()

    def test_startup_migrates_legacy_cascading_traffic_history(self):
        data_dir = Path(self.tmp.name) / "legacy-data"
        config = {
            "TESTING": True,
            "DATA_DIR": str(data_dir),
            "DATABASE": str(data_dir / "panel.db"),
            "FORWARD_CONFIG": str(Path(self.tmp.name) / "legacy-forward.conf"),
            "MAIN_CONFIG": str(Path(self.tmp.name) / "legacy-nftables.conf"),
            "SYSCTL_CONFIG": str(Path(self.tmp.name) / "legacy-sysctl.conf"),
        }
        create_app(config)
        connection = sqlite3.connect(config["DATABASE"])
        try:
            admin_id = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id, paused_reason, created_at)
                   VALUES (10117, '8.8.8.8', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                (admin_id,),
            )
            rule_id = cursor.lastrowid
            connection.execute(
                """INSERT INTO traffic_buckets
                   (rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes)
                   VALUES (?, ?, ?, 10, 20)""",
                (rule_id, admin_id, now()),
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.executescript(
                """
                ALTER TABLE traffic_buckets RENAME TO traffic_buckets_current;
                CREATE TABLE traffic_buckets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL REFERENCES forward_rules(id) ON DELETE CASCADE,
                    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    bucket_at TEXT NOT NULL,
                    inbound_bytes INTEGER NOT NULL DEFAULT 0,
                    outbound_bytes INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(rule_id, bucket_at)
                );
                INSERT INTO traffic_buckets
                    (id, rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes)
                SELECT id, rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes
                FROM traffic_buckets_current;
                DROP TABLE traffic_buckets_current;
                """
            )
            connection.commit()
        finally:
            connection.close()

        create_app(config)
        connection = sqlite3.connect(config["DATABASE"])
        try:
            self.assertFalse(
                any(
                    row[2] == "forward_rules"
                    for row in connection.execute("PRAGMA foreign_key_list(traffic_buckets)")
                )
            )
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM forward_rules WHERE id=?", (rule_id,))
            connection.commit()
            self.assertEqual(
                connection.execute(
                    "SELECT inbound_bytes, outbound_bytes FROM traffic_buckets WHERE rule_id=?",
                    (rule_id,),
                ).fetchone(),
                (10, 20),
            )
        finally:
            connection.close()

    def test_owner_transfer_within_same_bucket_does_not_reassign_old_traffic(self):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        try:
            admin_id = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id,
                    paused_reason, created_at)
                   VALUES (10118, '8.8.8.8', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                (member_id,),
            )
            rule_id = cursor.lastrowid
            connection.commit()
            member_rule = connection.execute(
                "SELECT * FROM forward_rules WHERE id=?",
                (rule_id,),
            ).fetchone()

            manager = MagicMock()
            manager.traffic_counters.side_effect = [
                {10118: {"inbound": 100, "outbound": 200}},
                {10118: {"inbound": 300, "outbound": 500}},
                {10118: {"inbound": 450, "outbound": 750}},
            ]
            monitor = TrafficMonitor(manager)
            first = datetime(2026, 7, 27, 12, 1, tzinfo=timezone.utc)
            with patch("monitoring.utc_now", return_value=first):
                monitor.sample(connection, [member_rule])
            with patch("monitoring.utc_now", return_value=first + timedelta(minutes=1)):
                monitor.sample(connection, [member_rule])

            connection.execute(
                "UPDATE forward_rules SET owner_id=? WHERE id=?",
                (admin_id, rule_id),
            )
            connection.commit()
            admin_rule = connection.execute(
                "SELECT * FROM forward_rules WHERE id=?",
                (rule_id,),
            ).fetchone()
            with patch("monitoring.utc_now", return_value=first + timedelta(minutes=2)):
                monitor.sample(connection, [admin_rule])

            history = connection.execute(
                """SELECT owner_id, inbound_bytes, outbound_bytes
                   FROM traffic_buckets WHERE rule_id=? ORDER BY owner_id""",
                (rule_id,),
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in history],
                sorted([
                    (member_id, 200, 300),
                    (admin_id, 150, 250),
                ]),
            )
        finally:
            connection.close()

    def test_subsecond_sample_accumulates_bytes_and_advances_counter_baseline(self):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        first = datetime(2026, 7, 27, 12, 1, tzinfo=timezone.utc)
        sampled = first + timedelta(milliseconds=500)
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id,
                    paused_reason, created_at)
                   VALUES (10119, '8.8.8.8', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                (member_id,),
            )
            rule_id = cursor.lastrowid
            connection.execute(
                """INSERT INTO rule_counter_state
                   (rule_id, inbound_bytes, outbound_bytes, sampled_at,
                    inbound_bps, outbound_bps)
                   VALUES (?, 1000, 2000, ?, 321.5, 654.5)""",
                (rule_id, first.strftime("%Y-%m-%d %H:%M:%S.%f UTC")),
            )
            connection.commit()
            rule = connection.execute(
                "SELECT * FROM forward_rules WHERE id=?",
                (rule_id,),
            ).fetchone()

            manager = MagicMock()
            manager.traffic_counters.return_value = {
                10119: {"inbound": 1100, "outbound": 2200}
            }
            with patch("monitoring.utc_now", return_value=sampled):
                result = TrafficMonitor(manager).sample(connection, [rule])

            self.assertEqual(
                result[rule_id],
                {"inbound_bps": 321.5, "outbound_bps": 654.5},
            )
            state = connection.execute(
                """SELECT inbound_bytes, outbound_bytes, sampled_at,
                          inbound_bps, outbound_bps
                   FROM rule_counter_state WHERE rule_id=?""",
                (rule_id,),
            ).fetchone()
            self.assertEqual(
                tuple(state),
                (
                    1100,
                    2200,
                    sampled.strftime("%Y-%m-%d %H:%M:%S.%f UTC"),
                    321.5,
                    654.5,
                ),
            )
            bucket = connection.execute(
                """SELECT owner_id, inbound_bytes, outbound_bytes
                   FROM traffic_buckets WHERE rule_id=?""",
                (rule_id,),
            ).fetchone()
            self.assertEqual(tuple(bucket), (member_id, 100, 200))
        finally:
            connection.close()

    def test_startup_migrates_legacy_traffic_unique_key_to_owner_dimension(self):
        data_dir = Path(self.tmp.name) / "legacy-owner-buckets"
        config = {
            "TESTING": True,
            "DATA_DIR": str(data_dir),
            "DATABASE": str(data_dir / "panel.db"),
            "FORWARD_CONFIG": str(Path(self.tmp.name) / "owner-forward.conf"),
            "MAIN_CONFIG": str(Path(self.tmp.name) / "owner-nftables.conf"),
            "SYSCTL_CONFIG": str(Path(self.tmp.name) / "owner-sysctl.conf"),
        }
        create_app(config)
        connection = sqlite3.connect(config["DATABASE"])
        try:
            admin_id = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            connection.execute(
                """INSERT INTO users
                   (identity_id, username, email, password_hash, role, active, created_at)
                   VALUES ('F1E2D3C4', 'legacy-member', 'legacy-member@example.com', ?, 'user', 1, ?)""",
                (generate_password_hash("LegacyMember!2026"), now()),
            )
            member_id = connection.execute(
                "SELECT id FROM users WHERE username='legacy-member'"
            ).fetchone()[0]
            connection.execute("DROP TABLE traffic_buckets")
            connection.execute(
                """CREATE TABLE traffic_buckets (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       rule_id INTEGER NOT NULL,
                       owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                       bucket_at TEXT NOT NULL,
                       inbound_bytes INTEGER NOT NULL DEFAULT 0,
                       outbound_bytes INTEGER NOT NULL DEFAULT 0,
                       UNIQUE(rule_id, bucket_at)
                   )"""
            )
            connection.execute(
                """INSERT INTO traffic_buckets
                   (id, rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes)
                   VALUES (7, 99, ?, '2026-07-27 12:00:00 UTC', 123, 456)""",
                (admin_id,),
            )
            connection.commit()
        finally:
            connection.close()

        create_app(config)
        connection = sqlite3.connect(config["DATABASE"])
        try:
            self.assertEqual(
                connection.execute(
                    """SELECT rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes
                       FROM traffic_buckets"""
                ).fetchone(),
                (99, admin_id, "2026-07-27 12:00:00 UTC", 123, 456),
            )
            unique_keys = {
                tuple(
                    column[2]
                    for column in connection.execute(
                        f"PRAGMA index_info('{index_row[1]}')"
                    ).fetchall()
                )
                for index_row in connection.execute("PRAGMA index_list(traffic_buckets)").fetchall()
                if index_row[2]
            }
            self.assertIn(("rule_id", "owner_id", "bucket_at"), unique_keys)
            self.assertNotIn(("rule_id", "bucket_at"), unique_keys)
            connection.execute(
                """INSERT INTO traffic_buckets
                   (rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes)
                   VALUES (99, ?, '2026-07-27 12:00:00 UTC', 10, 20)""",
                (member_id,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO traffic_buckets
                       (rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes)
                       VALUES (99, ?, '2026-07-27 12:00:00 UTC', 1, 2)""",
                    (member_id,),
                )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
            self.assertFalse(
                any(
                    row[2] == "forward_rules"
                    for row in connection.execute("PRAGMA foreign_key_list(traffic_buckets)")
                )
            )
        finally:
            connection.close()

    def test_invalid_policy_numbers_do_not_return_500(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
        finally:
            connection.close()
        token = csrf_token(admin.get("/users"))
        for quota in ("nan", "inf", "1e9999"):
            response = admin.post(f"/users/{member_id}/policy", data={
                "csrf_token": token, "max_rules": "10", "port_min": "1024", "port_max": "65535",
                "default_inbound_mbps": "0", "default_outbound_mbps": "0",
                "monthly_quota_gib": quota, "monthly_reset_day": "1", "monthly_reset_time": "00:00",
                "entry_address": "",
            })
            self.assertEqual(response.status_code, 302)

    def test_policy_database_is_restored_when_nft_reload_fails(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            before = connection.execute("SELECT max_rules, default_inbound_mbps FROM users WHERE id=?", (member_id,)).fetchone()
        finally:
            connection.close()
        token = csrf_token(admin.get("/users"))
        from nft_manager import NftOperationError
        with patch("app.reconcile_rule_state", side_effect=[NftOperationError("reload failed"), None]):
            response = admin.post(f"/users/{member_id}/policy", data={
                "csrf_token": token, "max_rules": "99", "port_min": "1024", "port_max": "65535",
                "default_inbound_mbps": "100", "default_outbound_mbps": "50",
                "monthly_quota_gib": "20", "monthly_reset_day": "1", "monthly_reset_time": "00:00",
                "entry_address": "203.0.113.10",
            })
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            after = connection.execute("SELECT max_rules, default_inbound_mbps FROM users WHERE id=?", (member_id,)).fetchone()
            self.assertEqual(after, before)
        finally:
            connection.close()

    def test_policy_rollback_preserves_concurrent_rule_changes_and_updates_new_defaults(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            admin_id = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            connection.execute(
                """UPDATE users SET default_inbound_mbps=5, default_outbound_mbps=6
                   WHERE id=?""",
                (member_id,),
            )
            transferred_id = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id,
                    inbound_limit_mbps, outbound_limit_mbps, uses_owner_defaults,
                    paused_reason, created_at)
                   VALUES (10123, '8.8.8.8', 443, ?, 5, 6, 1, '', ?)""",
                (member_id, now()),
            ).lastrowid
            customized_id = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id,
                    inbound_limit_mbps, outbound_limit_mbps, uses_owner_defaults,
                    paused_reason, created_at)
                   VALUES (10124, '8.8.4.4', 443, ?, 5, 6, 1, '', ?)""",
                (member_id, now()),
            ).lastrowid
            connection.commit()
        finally:
            connection.close()

        reconcile_calls = 0

        def fail_after_concurrent_rule_changes(*_args, **_kwargs):
            nonlocal reconcile_calls
            reconcile_calls += 1
            if reconcile_calls > 1:
                return False
            concurrent = sqlite3.connect(self.web.config["DATABASE"])
            try:
                concurrent.execute(
                    """UPDATE forward_rules
                       SET owner_id=?, inbound_limit_mbps=701, outbound_limit_mbps=702
                       WHERE id=?""",
                    (admin_id, transferred_id),
                )
                concurrent.execute(
                    """UPDATE forward_rules
                       SET uses_owner_defaults=0,
                           inbound_limit_mbps=801, outbound_limit_mbps=802
                       WHERE id=?""",
                    (customized_id,),
                )
                concurrent.execute(
                    """INSERT INTO forward_rules
                       (listen_port, destination_ip, destination_port, owner_id,
                        inbound_limit_mbps, outbound_limit_mbps, uses_owner_defaults,
                        paused_reason, created_at)
                       VALUES (10125, '1.1.1.1', 443, ?, 100, 50, 1, '', ?)""",
                    (member_id, now()),
                )
                concurrent.commit()
            finally:
                concurrent.close()
            raise NftOperationError("reload failed")

        token = csrf_token(admin.get("/users"))
        with (
            patch("app.TrafficMonitor.sample", return_value={}),
            patch(
                "app.reconcile_rule_state",
                side_effect=fail_after_concurrent_rule_changes,
            ),
        ):
            response = admin.post(f"/users/{member_id}/policy", data={
                "csrf_token": token,
                "max_rules": "10",
                "port_min": "1024",
                "port_max": "65535",
                "default_inbound_mbps": "100",
                "default_outbound_mbps": "50",
                "monthly_quota_gib": "0",
                "monthly_reset_day": "1",
                "monthly_reset_time": "00:00",
                "entry_address": "",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(reconcile_calls, 2)

        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            self.assertEqual(
                connection.execute(
                    """SELECT owner_id, inbound_limit_mbps, outbound_limit_mbps
                       FROM forward_rules WHERE id=?""",
                    (transferred_id,),
                ).fetchone(),
                (admin_id, 701, 702),
            )
            self.assertEqual(
                connection.execute(
                    """SELECT uses_owner_defaults, inbound_limit_mbps, outbound_limit_mbps
                       FROM forward_rules WHERE id=?""",
                    (customized_id,),
                ).fetchone(),
                (0, 801, 802),
            )
            self.assertEqual(
                connection.execute(
                    """SELECT inbound_limit_mbps, outbound_limit_mbps
                       FROM forward_rules WHERE listen_port=10125"""
                ).fetchone(),
                (5, 6),
            )
            self.assertEqual(
                connection.execute(
                    """SELECT default_inbound_mbps, default_outbound_mbps
                       FROM users WHERE id=?""",
                    (member_id,),
                ).fetchone(),
                (5, 6),
            )
        finally:
            connection.close()

    def test_admin_can_set_used_traffic_and_overview_reports_it(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id, paused_reason, created_at)
                   VALUES (10116, '8.8.8.8', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                (member_id,),
            )
            rule_id = cursor.lastrowid
            connection.commit()
        finally:
            connection.close()
        token = csrf_token(admin.get("/users"))

        def settle_pending_traffic(connection, _rules):
            connection.execute(
                """INSERT INTO traffic_buckets
                   (rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes)
                   VALUES (?, ?, ?, ?, 0)""",
                (rule_id, member_id, now(), int(0.5 * 1024 ** 3)),
            )
            connection.commit()
            return {}

        with (
            patch("app.TrafficMonitor.sample", side_effect=settle_pending_traffic),
            patch("app.reconcile_rule_state"),
        ):
            response = admin.post(f"/users/{member_id}/policy", data={
                "csrf_token": token,
                "max_rules": "10",
                "port_min": "1024",
                "port_max": "65535",
                "default_inbound_mbps": "0",
                "default_outbound_mbps": "0",
                "monthly_quota_gib": "100",
                "used_traffic_gib": "6.75",
                "monthly_reset_day": "1",
                "monthly_reset_time": "00:00",
                "entry_address": "",
            })
        self.assertEqual(response.status_code, 302)

        member = self.web.test_client()
        self.assertEqual(self.login(member, "member", "MemberPass!2026").status_code, 302)
        with patch(
            "app.cached_connection_snapshot",
            return_value={"ports": {}, "tcp_ports": {}, "udp_ports": {}},
        ):
            overview = member.get("/api/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.get_json()["totals"]["monthly_bytes"], int(6.75 * 1024 ** 3))

    def test_policy_default_limits_only_update_rules_using_owner_defaults(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            connection.executemany(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id,
                    inbound_limit_mbps, outbound_limit_mbps, uses_owner_defaults,
                    paused_reason, created_at)
                   VALUES (?, '8.8.8.8', 443, ?, ?, ?, ?, '', '2026-01-01 00:00:00 UTC')""",
                [
                    (10120, member_id, 10, 20, 1),
                    (10121, member_id, 30, 40, 0),
                ],
            )
            connection.commit()
        finally:
            connection.close()

        token = csrf_token(admin.get("/users"))
        with (
            patch("app.TrafficMonitor.sample", return_value={}),
            patch("app.reconcile_rule_state"),
        ):
            response = admin.post(f"/users/{member_id}/policy", data={
                "csrf_token": token,
                "max_rules": "10",
                "port_min": "1024",
                "port_max": "65535",
                "default_inbound_mbps": "100",
                "default_outbound_mbps": "50",
                "monthly_quota_gib": "0",
                "monthly_reset_day": "1",
                "monthly_reset_time": "00:00",
                "entry_address": "",
            })
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            limits = connection.execute(
                "SELECT listen_port, inbound_limit_mbps, outbound_limit_mbps "
                "FROM forward_rules ORDER BY listen_port"
            ).fetchall()
            self.assertEqual(limits, [(10120, 100, 50), (10121, 30, 40)])
        finally:
            connection.close()

    def test_shrinking_port_range_marks_existing_rule_out_of_range(self):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        try:
            member = connection.execute("SELECT * FROM users WHERE username='member'").fetchone()
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id,
                    paused_reason, created_at)
                   VALUES (12000, '8.8.8.8', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                (member["id"],),
            )
            connection.commit()
            rule = connection.execute("SELECT * FROM forward_rules WHERE id=?", (cursor.lastrowid,)).fetchone()
            shrunken_policy = dict(member)
            shrunken_policy["port_min"] = 1024
            shrunken_policy["port_max"] = 11000
            self.assertEqual(desired_rule_pause_reason(rule, shrunken_policy, 0), "port_range")
        finally:
            connection.close()

    def test_first_sample_after_nft_apply_counts_from_new_baseline(self):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        sampled_at = datetime.now(timezone.utc).replace(microsecond=0)
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id,
                    paused_reason, created_at)
                   VALUES (10122, '8.8.8.8', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                (member_id,),
            )
            rule_id = cursor.lastrowid
            connection.execute(
                """INSERT INTO rule_counter_state
                   (rule_id, inbound_bytes, outbound_bytes, sampled_at, inbound_bps, outbound_bps)
                   VALUES (?, 1000, 2000, ?, 0, 0)""",
                (
                    rule_id,
                    (sampled_at - timedelta(seconds=2)).strftime(
                        "%Y-%m-%d %H:%M:%S.%f UTC"
                    ),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        pre_apply = {10122: {"inbound": 1500, "outbound": 2700}}
        post_apply_baseline = {10122: {"inbound": 0, "outbound": 0}}
        with (
            patch("monitoring.utc_now", return_value=sampled_at),
            patch(
                "app.NftManager.traffic_counters",
                side_effect=[pre_apply, post_apply_baseline],
            ),
            patch("app.NftManager.apply_rules"),
            patch("app.NftManager.firewall_open", return_value=[]),
        ):
            reconcile_rule_state(self.web, at=sampled_at, force_apply=True)

        connection = sqlite3.connect(self.web.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        try:
            rule = connection.execute(
                "SELECT * FROM forward_rules WHERE id=?",
                (rule_id,),
            ).fetchone()
            later_manager = MagicMock()
            later_manager.traffic_counters.return_value = {
                10122: {"inbound": 100, "outbound": 200}
            }
            with patch(
                "monitoring.utc_now",
                return_value=sampled_at + timedelta(seconds=2),
            ):
                TrafficMonitor(later_manager).sample(connection, [rule])
            totals = connection.execute(
                """SELECT COALESCE(SUM(inbound_bytes), 0),
                          COALESCE(SUM(outbound_bytes), 0)
                   FROM traffic_buckets WHERE rule_id=?""",
                (rule_id,),
            ).fetchone()
            self.assertEqual(tuple(totals), (600, 900))
        finally:
            connection.close()

    def test_reconcile_calculates_quota_with_owner_id_and_closes_firewall(self):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            admin_id = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            connection.execute("UPDATE users SET monthly_quota_bytes=100 WHERE id=?", (member_id,))
            for port in (10140, 10141):
                connection.execute(
                    """INSERT INTO forward_rules
                       (listen_port, destination_ip, destination_port, owner_id, paused_reason, created_at)
                       VALUES (?, '8.8.8.8', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                    (port, admin_id),
                )
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id, paused_reason, created_at)
                   VALUES (10142, '1.1.1.1', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                (member_id,),
            )
            rule_id = cursor.lastrowid
            self.assertNotEqual(rule_id, member_id)
            connection.execute(
                """INSERT INTO traffic_buckets
                   (rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes)
                   VALUES (?, ?, ?, 150, 0)""",
                (rule_id, member_id, now()),
            )
            connection.commit()
        finally:
            connection.close()

        with (
            patch("app.TrafficMonitor.sample", return_value={}),
            patch("app.NftManager") as manager_class,
        ):
            manager = manager_class.return_value
            manager.firewall_open.return_value = []
            manager.firewall_close.return_value = []
            self.assertTrue(reconcile_rule_state(self.web))

        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            reason = connection.execute(
                "SELECT paused_reason FROM forward_rules WHERE id=?",
                (rule_id,),
            ).fetchone()[0]
            self.assertEqual(reason, "quota")
        finally:
            connection.close()
        manager.firewall_close.assert_called_once()
        self.assertEqual(manager.firewall_close.call_args.args[0].id, rule_id)

    def test_reconcile_closes_existing_rule_after_port_range_shrinks(self):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            connection.execute("UPDATE users SET port_min=1024, port_max=11000 WHERE id=?", (member_id,))
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id, paused_reason, created_at)
                   VALUES (12000, '8.8.8.8', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                (member_id,),
            )
            rule_id = cursor.lastrowid
            connection.commit()
        finally:
            connection.close()

        with (
            patch("app.TrafficMonitor.sample", return_value={}),
            patch("app.NftManager") as manager_class,
        ):
            manager = manager_class.return_value
            manager.firewall_open.return_value = []
            manager.firewall_close.return_value = []
            self.assertTrue(reconcile_rule_state(self.web))

        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            reason = connection.execute(
                "SELECT paused_reason FROM forward_rules WHERE id=?",
                (rule_id,),
            ).fetchone()[0]
            self.assertEqual(reason, "port_range")
        finally:
            connection.close()
        manager.firewall_close.assert_called_once()
        self.assertEqual(manager.firewall_close.call_args.args[0].id, rule_id)

    def test_reconcile_retries_firewall_without_reloading_nft_table(self):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id, paused_reason, created_at)
                   VALUES (10143, '8.8.8.8', 443, ?, '', '2026-01-01 00:00:00 UTC')""",
                (member_id,),
            )
            rule_id = cursor.lastrowid
            connection.execute(
                """INSERT INTO firewall_retry_jobs
                   (operation, rule_id, listen_port, destination_ip, destination_port,
                    remove_listen_port, attempts, last_error, created_at)
                   VALUES ('open', ?, 10143, '8.8.8.8', 443, 1, 1, 'failed',
                           '2026-01-01 00:00:00 UTC')""",
                (rule_id,),
            )
            connection.commit()
        finally:
            connection.close()

        with (
            patch("app.TrafficMonitor.sample", return_value={}),
            patch("app.NftManager") as manager_class,
        ):
            manager = manager_class.return_value
            manager.firewall_open.return_value = []
            self.assertTrue(reconcile_rule_state(self.web))
            manager.apply_rules.assert_not_called()

        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM firewall_retry_jobs").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_admin_overview_uses_own_monthly_quota(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            connection.execute("UPDATE users SET monthly_quota_bytes=? WHERE username='admin'", (100 * 1024 ** 3,))
            connection.commit()
        finally:
            connection.close()
        with patch(
            "app.cached_connection_snapshot",
            return_value={"ports": {}, "tcp_ports": {}, "udp_ports": {}},
        ):
            response = admin.get("/api/overview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["totals"]["monthly_quota_bytes"], 100 * 1024 ** 3)

    def test_history_uses_requested_archive_interval(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        for kind, days, seconds in (("bandwidth", 1, 300), ("bandwidth", 7, 3600), ("bandwidth", 30, 86400), ("traffic", 1, 3600)):
            response = admin.get(f"/api/history?kind={kind}&days={days}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["interval_seconds"], seconds)
        self.assertEqual(admin.get("/api/history?kind=bandwidth&days=60").status_code, 400)

    def test_admin_can_edit_rule_owner(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            admin_id = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            member_id = connection.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id, inbound_limit_mbps,
                    outbound_limit_mbps, paused_reason, created_at)
                   VALUES (10110, '8.8.8.8', 443, ?, 0, 0, '', '2026-01-01 00:00:00 UTC')""",
                (admin_id,),
            )
            rule_id = cursor.lastrowid
            connection.commit()
        finally:
            connection.close()
        token = csrf_token(admin.get("/"))
        with (
            patch("app.NftManager.apply_rules"),
            patch("app.NftManager.listening_port_in_use", return_value=False),
            patch("app.NftManager.firewall_open", return_value=[]),
            patch("app.NftManager.firewall_close", return_value=[]) as close_firewall,
        ):
            response = admin.post(f"/rules/{rule_id}/edit", data={
                "csrf_token": token,
                "listen_port": "10111",
                "destination_ip": "1.1.1.1",
                "destination_port": "8443",
                "owner_id": str(member_id),
                "inbound_limit_mbps": "20",
                "outbound_limit_mbps": "10",
                "note": "香港入口",
            })
        self.assertEqual(response.status_code, 302)
        close_firewall.assert_called_once()
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            row = connection.execute(
                "SELECT listen_port, destination_ip, destination_port, owner_id, note FROM forward_rules WHERE id=?",
                (rule_id,),
            ).fetchone()
            self.assertEqual(row, (10111, "1.1.1.1", 8443, member_id, "香港入口"))
        finally:
            connection.close()

    def test_editing_only_destination_keeps_shared_listen_firewall_rule(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            admin_id = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO forward_rules
                   (listen_port, destination_ip, destination_port, owner_id,
                    inbound_limit_mbps, outbound_limit_mbps, paused_reason, created_at)
                   VALUES (10130, '8.8.8.8', 443, ?, 0, 0, '', '2026-01-01 00:00:00 UTC')""",
                (admin_id,),
            )
            rule_id = cursor.lastrowid
            connection.commit()
        finally:
            connection.close()

        token = csrf_token(admin.get("/"))
        with (
            patch("app.NftManager.apply_rules"),
            patch("app.NftManager.firewall_open", return_value=[]),
            patch("app.NftManager.firewall_close", return_value=[]) as close_firewall,
        ):
            response = admin.post(f"/rules/{rule_id}/edit", data={
                "csrf_token": token,
                "listen_port": "10130",
                "destination_ip": "1.1.1.1",
                "destination_port": "8443",
                "owner_id": str(admin_id),
                "inbound_limit_mbps": "20",
                "outbound_limit_mbps": "10",
                "note": "仅修改目标",
            })
        self.assertEqual(response.status_code, 302)
        close_firewall.assert_called_once()
        self.assertFalse(close_firewall.call_args.kwargs["remove_listen_port"])
        self.assertEqual(close_firewall.call_args.args[0].listen_port, 10130)


if __name__ == "__main__":
    unittest.main()
