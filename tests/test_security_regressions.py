import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PANEL_ADMIN_USERNAME", "admin")
os.environ.setdefault("PANEL_ADMIN_PASSWORD", "PreviewOnly!2026")
os.environ.setdefault("PANEL_SECRET_KEY", "test-secret")
os.environ.setdefault("PANEL_DATA_DIR", tempfile.mkdtemp(prefix="security-import-"))

from app import create_app, desired_pause_reason, validate_rule_note
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

    def test_admin_overview_uses_own_monthly_quota(self):
        admin = self.web.test_client()
        self.login(admin, "admin", "PreviewOnly!2026")
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            connection.execute("UPDATE users SET monthly_quota_bytes=? WHERE username='admin'", (100 * 1024 ** 3,))
            connection.commit()
        finally:
            connection.close()
        with (
            patch("app.TrafficMonitor.sample", return_value={}),
            patch("app.NftManager.connection_snapshot", return_value={"ports": {}, "tcp_ports": {}, "udp_ports": {}}),
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


if __name__ == "__main__":
    unittest.main()
