import unittest

from nft_manager import NftManager, NftOperationError


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


if __name__ == "__main__":
    unittest.main()
