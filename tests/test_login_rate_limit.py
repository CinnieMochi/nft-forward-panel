import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PANEL_ADMIN_USERNAME", "admin")
os.environ.setdefault("PANEL_ADMIN_PASSWORD", "PreviewOnly!2026")
os.environ.setdefault("PANEL_SECRET_KEY", "test-secret")
os.environ.setdefault("PANEL_DATA_DIR", tempfile.mkdtemp(prefix="rate-limit-import-"))

import app


class LoginRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data_dir = Path(self.tmp.name) / "data"
        self.web = app.create_app({
            "TESTING": True,
            "DATA_DIR": str(data_dir),
            "DATABASE": str(data_dir / "panel.db"),
            "FORWARD_CONFIG": str(Path(self.tmp.name) / "port-forward.conf"),
            "MAIN_CONFIG": str(Path(self.tmp.name) / "nftables.conf"),
            "SYSCTL_CONFIG": str(Path(self.tmp.name) / "sysctl.conf"),
        })

    def insert_attempts(self, attempted_at):
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            connection.executemany(
                "INSERT INTO login_attempts(remote_addr, attempted_at) VALUES ('127.0.0.1', ?)",
                [(attempted_at,)] * app.LOGIN_MAX_ATTEMPTS,
            )
            connection.commit()
        finally:
            connection.close()

    def test_default_limit_is_relaxed(self):
        self.assertEqual(app.LOGIN_MAX_ATTEMPTS, 20)
        self.assertEqual(app.LOGIN_WINDOW_SECONDS, 600)

    def test_limit_reports_remaining_time_at_threshold(self):
        self.insert_attempts(100)
        with self.web.test_request_context(), patch("app.time.time", return_value=150):
            self.assertEqual(app._rate_limit_remaining("127.0.0.1"), 550)

    def test_expired_attempts_are_removed(self):
        self.insert_attempts(100)
        with self.web.test_request_context(), patch("app.time.time", return_value=701):
            self.assertEqual(app._rate_limit_remaining("127.0.0.1"), 0)
            remaining = app.get_db().execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]
            self.assertEqual(remaining, 0)

    def test_success_clear_is_shared_in_database(self):
        self.insert_attempts(100)
        with self.web.test_request_context():
            app._clear_failed_logins("127.0.0.1")
            app.get_db().commit()
        connection = sqlite3.connect(self.web.config["DATABASE"])
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0], 0)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
