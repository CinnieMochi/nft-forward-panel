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
os.environ.setdefault("PANEL_DATA_DIR", tempfile.mkdtemp(prefix="identity-import-"))

from app import create_app


def csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not match:
        raise AssertionError("missing csrf token")
    return match.group(1)


class IdentityIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_env = os.environ.copy()
        os.environ["PANEL_ADMIN_USERNAME"] = "admin"
        os.environ["PANEL_ADMIN_PASSWORD"] = "PreviewOnly!2026"
        os.environ["PANEL_SECRET_KEY"] = "test-secret"
        self.data_dir = Path(self.tmp.name) / "data"
        self.config = {
            "TESTING": True,
            "DATA_DIR": str(self.data_dir),
            "DATABASE": str(self.data_dir / "panel.db"),
            "FORWARD_CONFIG": str(Path(self.tmp.name) / "port-forward.conf"),
            "MAIN_CONFIG": str(Path(self.tmp.name) / "nftables.conf"),
            "SYSCTL_CONFIG": str(Path(self.tmp.name) / "sysctl.conf"),
        }
        self.status_patch = patch("app.NftManager.status", return_value={
            "nft_available": True,
            "nft_table_loaded": True,
            "ip_forward": True,
            "firewall": "未检测到",
        })
        self.status_patch.start()
        self.addCleanup(self.status_patch.stop)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    def test_bootstrap_user_has_stable_identity_id(self):
        app = create_app(self.config)
        with app.app_context():
            connection = sqlite3.connect(app.config["DATABASE"])
            connection.row_factory = sqlite3.Row
            try:
                user = connection.execute("SELECT * FROM users WHERE username='admin'").fetchone()
                self.assertRegex(user["identity_id"], r"^usr_[0-9a-f]{32}$")
                index = connection.execute("PRAGMA index_list(users)").fetchall()
                self.assertTrue(any(row[1] == "idx_users_identity_unique" for row in index))
            finally:
                connection.close()

    def test_existing_users_are_backfilled_on_startup(self):
        self.data_dir.mkdir(parents=True)
        database = self.data_dir / "panel.db"
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                INSERT INTO users (username, password_hash, role, active, created_at)
                VALUES ('legacy', 'x', 'admin', 1, '2026-01-01 00:00:00 UTC');
                """
            )
            connection.commit()
        finally:
            connection.close()
        app = create_app(self.config)
        connection = sqlite3.connect(app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        try:
            user = connection.execute("SELECT * FROM users WHERE username='legacy'").fetchone()
            self.assertRegex(user["identity_id"], r"^usr_[0-9a-f]{32}$")
        finally:
            connection.close()

    def test_profile_update_does_not_change_identity_id(self):
        app = create_app(self.config)
        client = app.test_client()
        login_page = client.get("/login")
        token = csrf_token(login_page.get_data(as_text=True))
        self.assertEqual(client.post("/login", data={"csrf_token": token, "identifier": "admin", "password": "PreviewOnly!2026"}).status_code, 302)
        with app.app_context():
            connection = sqlite3.connect(app.config["DATABASE"])
            connection.row_factory = sqlite3.Row
            try:
                before = connection.execute("SELECT identity_id FROM users WHERE username='admin'").fetchone()[0]
            finally:
                connection.close()
        profile_page = client.get("/profile")
        token = csrf_token(profile_page.get_data(as_text=True))
        response = client.post("/profile", data={"csrf_token": token, "username": "renamed", "email": "renamed@example.com"})
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            connection = sqlite3.connect(app.config["DATABASE"])
            connection.row_factory = sqlite3.Row
            try:
                user = connection.execute("SELECT * FROM users WHERE username='renamed'").fetchone()
                self.assertEqual(user["identity_id"], before)
                audit = connection.execute("SELECT * FROM audit_events WHERE action='profile_update' ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(audit["target"], before)
                self.assertIn("username=admin->renamed", audit["details"])
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
