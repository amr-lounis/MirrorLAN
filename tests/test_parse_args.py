#!/usr/bin/env python3
"""Tests for main.parse_args (stdlib unittest only)."""
import unittest

from main import parse_args


class TestParseArgs(unittest.TestCase):
    def test_default_is_gui(self):
        mode, config = parse_args([])
        self.assertEqual(mode, "gui")
        self.assertEqual(config.port, 80)

    def test_serve_default_port(self):
        mode, config = parse_args(["--serve"])
        self.assertEqual(mode, "serve")
        self.assertEqual(config.port, 80)

    def test_serve_positional_port(self):
        mode, config = parse_args(["--serve", "8081"])
        self.assertEqual(mode, "serve")
        self.assertEqual(config.port, 8081)

    def test_serve_dir_and_ports(self):
        mode, config = parse_args(
            ["--serve", "8081", "--dir", "./site",
             "--port", "9090", "--turn-port", "3479"])
        self.assertEqual(mode, "serve")
        self.assertEqual(config.port, 9090)  # explicit flag wins
        self.assertEqual(config.turn_port, 3479)
        self.assertTrue(config.www_dir.endswith("site"))

    def test_turn_port_flag(self):
        mode, config = parse_args(["--serve", "--turn-port", "3479"])
        self.assertEqual(mode, "serve")
        self.assertEqual(config.turn_port, 3479)
        mode, config = parse_args(["--serve", "--turn-port", "0"])
        self.assertEqual(config.turn_port, 0)  # 0 = relay disabled
        with self.assertRaises(ValueError):
            parse_args(["--serve", "--turn-port", "99999"])

    def test_unknown_flag_rejected(self):
        with self.assertRaises(ValueError):
            parse_args(["--bogus"])
        with self.assertRaises(ValueError):
            parse_args(["--serve", "--bogus"])

    def test_invalid_port_rejected(self):
        for bad in (["--serve", "0"], ["--serve", "99999"],
                    ["--port", "0"], ["--port", "-1"]):
            with self.assertRaises(ValueError, msg=str(bad)):
                parse_args(bad)


if __name__ == "__main__":
    unittest.main()
