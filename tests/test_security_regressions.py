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

from app import create_app, desired_pause_reason
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
                ("usr_" + "1" * 32, "member", "member@example.com", generate_password_hash("MemberPass!2026"), "2026-01-01 00:00:00 UTC"),
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
                ("usr_" + "2" * 32, "member@example.com", "legacy@example.com", generate_password_hash("LegacyPass!2026"), "2026-01-01 00:00:00 UTC"),
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


if __name__ == "__main__":
    unittest.main()
