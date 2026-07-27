import json
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from nft_manager import ForwardRule, NftManager, NftOperationError


class ValidationTests(unittest.TestCase):
    def test_valid_ports(self):
        self.assertEqual(NftManager.validate_port("1"), 1)
        self.assertEqual(NftManager.validate_port(65535), 65535)

    def test_invalid_ports(self):
        for port in ("0", "0001", "65536", "x", "1.2", "-1"):
            with self.assertRaises(NftOperationError):
                NftManager.validate_port(port)

    def test_ipv4_normalisation(self):
        self.assertEqual(NftManager.validate_ipv4("192.168.5.20"), "192.168.5.20")
        for address in ("300.1.1.1", "192.168.01.1", "not-an-ip", "::1"):
            with self.assertRaises(NftOperationError):
                NftManager.validate_ipv4(address)

    def test_unsafe_destination_ranges_are_rejected(self):
        for address in ("0.0.0.0", "127.0.0.1", "169.254.169.254", "224.0.0.1", "240.0.0.1", "255.255.255.255"):
            with self.subTest(address=address), self.assertRaises(NftOperationError):
                NftManager.validate_ipv4(address)

    def test_empty_rules_do_not_need_a_network_address(self):
        manager = NftManager("/tmp/port-forward.conf", "/tmp/nftables.conf", "/tmp/forward.conf")
        rendered = manager._render_config([])
        self.assertIn("table ip port_forward", rendered)
        self.assertNotIn("define LOCAL_IP", rendered)

    def test_dnat_precedes_rule_comment(self):
        manager = NftManager("/tmp/port-forward.conf", "/tmp/nftables.conf", "/tmp/forward.conf")
        manager.local_ipv4 = lambda: "192.0.2.10"
        rendered = manager._render_config([
            ForwardRule(None, 10110, "141.11.219.150", 19849),
        ])
        self.assertIn(
            'tcp dport 10110 counter dnat to 141.11.219.150:19849 comment "nfp:nat:10110"',
            rendered,
        )
        self.assertNotIn('comment "nfp:nat:10110" dnat', rendered)

    def test_traffic_counters_use_one_forward_hook_and_limits_stay_directional(self):
        manager = NftManager("/tmp/port-forward.conf", "/tmp/nftables.conf", "/tmp/forward.conf")
        manager.local_ipv4 = lambda: "192.0.2.10"
        rendered = manager._render_config([
            ForwardRule(None, 10110, "141.11.219.150", 19849, inbound_limit_mbps=8),
        ])
        self.assertNotIn("chain traffic_prerouting", rendered)
        self.assertNotIn("chain traffic_postrouting", rendered)
        self.assertEqual(rendered.count("type filter hook forward priority 10; policy accept;"), 1)
        self.assertIn(
            'ip daddr 141.11.219.150 tcp dport 19849 ct status dnat '
            'ct original protocol tcp ct original proto-dst 10110 counter '
            'comment "nfp:traffic-out:10110"',
            rendered,
        )
        self.assertIn(
            'ip daddr 141.11.219.150 udp dport 19849 ct status dnat '
            'ct original protocol udp ct original proto-dst 10110 counter '
            'comment "nfp:traffic-out:10110"',
            rendered,
        )
        self.assertIn(
            'ip saddr 141.11.219.150 tcp sport 19849 ct status dnat '
            'ct original protocol tcp ct original proto-dst 10110 counter '
            'limit rate over 1000 kbytes/second drop comment "nfp:traffic-in:10110"',
            rendered,
        )
        self.assertEqual(rendered.count('comment "nfp:traffic-in:10110"'), 2)
        self.assertEqual(rendered.count('comment "nfp:traffic-out:10110"'), 2)
        self.assertNotIn("nfp:limit-", rendered)

    def test_firewall_close_can_keep_listen_port_when_only_target_changes(self):
        manager = NftManager("/tmp/port-forward.conf", "/tmp/nftables.conf", "/tmp/forward.conf")
        manager._firewalld_active = lambda: False
        manager._ufw_active = lambda: False
        manager._iptables_available = lambda: True
        calls = []

        def record(args, check=True):
            calls.append(args)
            return CompletedProcess(args, 0, "", "")

        manager._run = record
        manager.firewall_close(
            ForwardRule(None, 10110, "141.11.219.150", 19849),
            destination_still_used=False,
            remove_listen_port=False,
        )
        self.assertFalse(any(args[:3] == ["iptables", "-D", "INPUT"] for args in calls))
        self.assertEqual(
            calls,
            [
                [
                    "iptables", "-D", "FORWARD", "-d", "141.11.219.150",
                    "-p", "tcp", "--dport", "19849", "-j", "ACCEPT",
                ],
                [
                    "iptables", "-D", "FORWARD", "-d", "141.11.219.150",
                    "-p", "udp", "--dport", "19849", "-j", "ACCEPT",
                ],
            ],
        )

    def test_firewall_close_reports_real_delete_failures_for_each_backend(self):
        backends = (
            ("firewalld", True, False, False),
            ("ufw", False, True, False),
            ("iptables", False, False, True),
        )
        for backend, firewalld, ufw, iptables in backends:
            with self.subTest(backend=backend):
                manager = NftManager("/tmp/port-forward.conf", "/tmp/nftables.conf", "/tmp/forward.conf")
                manager._firewalld_active = lambda active=firewalld: active
                manager._ufw_active = lambda active=ufw: active
                manager._iptables_available = lambda active=iptables: active
                manager._run = lambda args, check=True: CompletedProcess(
                    args, 2, "", f"{backend}: permission denied"
                )

                warnings = manager.firewall_close(
                    ForwardRule(None, 10110, "141.11.219.150", 19849),
                    destination_still_used=False,
                )

                self.assertEqual(len(warnings), 1)
                self.assertIn("防火墙清理失败", warnings[0])
                self.assertIn("permission denied", warnings[0])

    def test_firewall_close_treats_known_missing_rules_as_idempotent(self):
        cases = (
            ("firewalld", True, False, False, 12, "Warning: NOT_ENABLED"),
            ("ufw", False, True, False, 1, "Skipping deleting non-existent rule"),
            (
                "iptables", False, False, True, 1,
                "iptables: Bad rule (does a matching rule exist in that chain?).",
            ),
        )
        for backend, firewalld, ufw, iptables, returncode, message in cases:
            with self.subTest(backend=backend):
                manager = NftManager("/tmp/port-forward.conf", "/tmp/nftables.conf", "/tmp/forward.conf")
                manager._firewalld_active = lambda active=firewalld: active
                manager._ufw_active = lambda active=ufw: active
                manager._iptables_available = lambda active=iptables: active

                def missing(args, check=True):
                    if args == ["firewall-cmd", "--reload"]:
                        return CompletedProcess(args, 0, "", "")
                    return CompletedProcess(args, returncode, "", message)

                manager._run = missing
                warnings = manager.firewall_close(
                    ForwardRule(None, 10110, "141.11.219.150", 19849),
                    destination_still_used=False,
                )
                self.assertEqual(warnings, [])

    def test_firewall_close_reports_empty_code_one_for_ufw_and_iptables(self):
        backends = (
            ("ufw", True, False),
            ("iptables", False, True),
        )
        for backend, ufw, iptables in backends:
            with self.subTest(backend=backend):
                manager = NftManager("/tmp/port-forward.conf", "/tmp/nftables.conf", "/tmp/forward.conf")
                manager._firewalld_active = lambda: False
                manager._ufw_active = lambda active=ufw: active
                manager._iptables_available = lambda active=iptables: active
                manager._run = lambda args, check=True: CompletedProcess(args, 1, "", "")

                warnings = manager.firewall_close(
                    ForwardRule(None, 10110, "141.11.219.150", 19849),
                    destination_still_used=False,
                )

                self.assertEqual(len(warnings), 1)
                self.assertIn("防火墙清理失败", warnings[0])
                self.assertIn("未知错误", warnings[0])

    @patch("nft_manager.shutil.which", return_value="/usr/sbin/nft")
    def test_traffic_uses_forwarding_counters_not_nat_first_packet(self, _which):
        manager = NftManager("/tmp/port-forward.conf", "/tmp/nftables.conf", "/tmp/forward.conf")
        payload = {"nftables": [
            {"rule": {"comment": "nfp:nat:10110", "expr": [{"counter": {"bytes": 60}}]}},
            {"rule": {"comment": "nfp:traffic-in:10110", "expr": [{"counter": {"bytes": 1200}}]}},
            {"rule": {"comment": "nfp:traffic-in:10110", "expr": [{"counter": {"bytes": 300}}]}},
            {"rule": {"comment": "nfp:traffic-out:10110", "expr": [{"counter": {"bytes": 900}}]}},
        ]}
        manager._run = lambda *args, **kwargs: CompletedProcess([], 0, json.dumps(payload), "")
        self.assertEqual(manager.traffic_counters(), {10110: {"inbound": 1500, "outbound": 900}})

    @patch("nft_manager.shutil.which", return_value="/usr/sbin/conntrack")
    def test_connections_use_original_destination_port(self, _which):
        manager = NftManager("/tmp/port-forward.conf", "/tmp/nftables.conf", "/tmp/forward.conf")
        output = chr(10).join((
            "tcp 6 431999 ESTABLISHED src=198.51.100.20 dst=192.0.2.10 sport=50000 dport=10110 "
            "src=141.11.219.150 dst=198.51.100.20 sport=19849 dport=50000 [ASSURED] mark=0 use=1",
            "ipv4 2 udp 17 29 src=198.51.100.21 dst=192.0.2.10 sport=50001 dport=10110 "
            "src=141.11.219.150 dst=198.51.100.21 sport=19849 dport=50001 mark=0 use=1",
            "ipv4 2 tcp 6 14 TIME_WAIT src=198.51.100.22 dst=192.0.2.10 sport=50002 dport=10110 "
            "src=141.11.219.150 dst=198.51.100.22 sport=19849 dport=50002 [ASSURED] mark=0 use=1",
        ))
        manager._run = lambda *args, **kwargs: CompletedProcess([], 0, output, "")
        self.assertEqual(manager.connection_counts(), {10110: 2})
        self.assertEqual(manager.connection_snapshot(), {
            "ports": {10110: 2}, "tcp_ports": {10110: 1}, "udp_ports": {10110: 1},
        })


if __name__ == "__main__":
    unittest.main()
