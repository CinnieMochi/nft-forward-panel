import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PANEL_ADMIN_USERNAME", "admin")
os.environ.setdefault("PANEL_ADMIN_PASSWORD", "PreviewOnly!2026")
os.environ.setdefault("PANEL_SECRET_KEY", "test-secret")
os.environ.setdefault("PANEL_DATA_DIR", tempfile.mkdtemp(prefix="nfpctl-import-"))

import nfpctl
from app import create_app
from nft_manager import ForwardRule, NftManager


class FakeManager:
    def __init__(self):
        self.applied = []
        self.opened = []
        self.closed = []

    validate_port = staticmethod(NftManager.validate_port)
    validate_ipv4 = staticmethod(NftManager.validate_ipv4)

    def listening_port_in_use(self, port):
        return False

    def apply_rules(self, rules):
        self.applied.append(list(rules))

    def firewall_open(self, rule):
        self.opened.append(rule)
        return []

    def firewall_close(self, rule, destination_still_used):
        self.closed.append((rule, destination_still_used))
        return []

    def traffic_counters(self):
        return {}

    def status(self):
        return {
            "nft_available": True,
            "nft_table_loaded": True,
            "ip_forward": True,
            "firewall": "未检测到",
        }


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_env = os.environ.copy()
        os.environ["PANEL_ADMIN_USERNAME"] = "admin"
        os.environ["PANEL_ADMIN_PASSWORD"] = "PreviewOnly!2026"
        os.environ["PANEL_SECRET_KEY"] = "test-secret"
        data_dir = Path(self.tmp.name) / "data"
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": str(data_dir),
                "DATABASE": str(data_dir / "panel.db"),
                "FORWARD_CONFIG": str(Path(self.tmp.name) / "port-forward.conf"),
                "MAIN_CONFIG": str(Path(self.tmp.name) / "nftables.conf"),
                "SYSCTL_CONFIG": str(Path(self.tmp.name) / "sysctl.conf"),
            }
        )
        self.fake = FakeManager()
        self.patch = patch("nfpctl.manager_for", return_value=self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    def test_add_rule_uses_database_and_audit_log(self):
        args = argparse.Namespace(
            port="2443",
            target="8.8.8.8",
            target_port="443",
            owner=None,
            inbound_mbps=None,
            outbound_mbps=None,
            force_port_conflict=False,
        )
        nfpctl.add_rule(self.app, args)
        connection = nfpctl.db_connect(self.app)
        try:
            rule = connection.execute("SELECT * FROM forward_rules WHERE listen_port=2443").fetchone()
            self.assertIsNotNone(rule)
            self.assertEqual(rule["destination_ip"], "8.8.8.8")
            audit = connection.execute("SELECT * FROM audit_events WHERE action='rule_create_ssh'").fetchone()
            self.assertIsNotNone(audit)
        finally:
            connection.close()
        self.assertEqual(len(self.fake.applied), 1)
        self.assertIsInstance(self.fake.applied[0][0], ForwardRule)
        self.assertEqual(self.fake.opened[0].listen_port, 2443)

    def test_remove_rule_uses_same_state(self):
        args = argparse.Namespace(
            port="2443",
            target="8.8.8.8",
            target_port="443",
            owner=None,
            inbound_mbps=None,
            outbound_mbps=None,
            force_port_conflict=False,
        )
        nfpctl.add_rule(self.app, args)
        connection = nfpctl.db_connect(self.app)
        try:
            rule_id = connection.execute("SELECT id FROM forward_rules WHERE listen_port=2443").fetchone()[0]
        finally:
            connection.close()
        nfpctl.remove_rule(self.app, rule_id, True)
        connection = nfpctl.db_connect(self.app)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM forward_rules").fetchone()[0], 0)
            audit = connection.execute("SELECT * FROM audit_events WHERE action='rule_delete_ssh'").fetchone()
            self.assertIsNotNone(audit)
        finally:
            connection.close()
        self.assertEqual(self.fake.closed[0][0].listen_port, 2443)

    def test_load_env_file_does_not_eval_shell_syntax(self):
        env_file = Path(self.tmp.name) / "panel.env"
        env_file.write_text("PANEL_SECRET_KEY='abc; touch /tmp/should-not-run'\n", encoding="utf-8")
        os.environ.pop("PANEL_SECRET_KEY", None)
        nfpctl.load_env_file(str(env_file))
        self.assertEqual(os.environ["PANEL_SECRET_KEY"], "abc; touch /tmp/should-not-run")


if __name__ == "__main__":
    unittest.main()
