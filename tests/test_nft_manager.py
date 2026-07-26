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

    def test_traffic_counters_use_interface_hooks_and_limits_stay_directional(self):
        manager = NftManager("/tmp/port-forward.conf", "/tmp/nftables.conf", "/tmp/forward.conf")
        manager.local_ipv4 = lambda: "192.0.2.10"
        rendered = manager._render_config([
            ForwardRule(None, 10110, "141.11.219.150", 19849, inbound_limit_mbps=8),
        ])
        self.assertIn("type filter hook prerouting priority 0; policy accept;", rendered)
        self.assertIn("type filter hook postrouting priority 0; policy accept;", rendered)
        self.assertIn(
            'ct status dnat ct original protocol tcp ct original proto-dst 10110 '
            'counter comment "nfp:traffic-in:10110"',
            rendered,
        )
        self.assertIn(
            'ct status dnat ct original protocol udp ct original proto-dst 10110 '
            'counter comment "nfp:traffic-out:10110"',
            rendered,
        )
        self.assertIn(
            'ip saddr 141.11.219.150 tcp sport 19849 ct status dnat '
            'ct original protocol tcp ct original proto-dst 10110 counter '
            'limit rate over 1000 kbytes/second drop comment "nfp:limit-in:10110"',
            rendered,
        )
        self.assertNotIn('comment "nfp:traffic-in:10110" limit', rendered)

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
            "udp 17 29 src=198.51.100.21 dst=192.0.2.10 sport=50001 dport=10110 "
            "src=141.11.219.150 dst=198.51.100.21 sport=19849 dport=50001 mark=0 use=1",
        ))
        manager._run = lambda *args, **kwargs: CompletedProcess([], 0, output, "")
        self.assertEqual(manager.connection_counts(), {10110: 2})


if __name__ == "__main__":
    unittest.main()
