import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402


def build(**env):
    values = {"WATCH_ADDRESSES": "0x" + "ab" * 20, "MINTER_PRIVATE_KEYS": "0x" + "11" * 32, "STATE_PATH": os.path.join(tempfile.mkdtemp(), "s.json")}
    values.update(env)
    with mock.patch.dict(os.environ, values, clear=True):
        return bot.Bot()


class ChainTests(unittest.TestCase):
    def test_ink_is_supported(self):
        b = build(CHAINS="57073")
        self.assertEqual(b.chains[57073].name, "Ink")

    def test_unlisted_chain_needs_an_rpc(self):
        with self.assertRaises(SystemExit) as caught:
            build(CHAINS="99999")
        self.assertIn("RPC_99999", str(caught.exception))
        b = build(CHAINS="99999", RPC_99999="https://example.invalid", CHAIN_NAME_99999="Newchain")
        self.assertEqual(b.chains[99999].name, "Newchain")
        self.assertEqual(build(CHAINS="99999", RPC_99999="https://example.invalid").chains[99999].name, "Chain 99999")


if __name__ == "__main__":
    unittest.main()
