import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402
from test_sendnfts import C721, DEST, KEYS, make_bot, node  # noqa: E402

COLLECTION = "0xa3396ed78ce03ae268e995374ab54d6631d95756"


def run(b, *words):
    with mock.patch.object(bot.threading, "Thread") as thread:
        reply = b.sendnfts_command(list(words))
    if not thread.called:
        return reply, None
    return reply, thread.call_args.kwargs["args"]  # dest, accounts, chain_ids, extra, deep, given


class EasySendCommand(unittest.TestCase):
    def setUp(self):
        self.b = node(make_bot(MINTER_PRIVATE_KEYS=KEYS + ",0x" + "33" * 32))

    def test_the_users_form(self):
        reply, (dest, accounts, chains, extra, deep, given) = run(self.b, "from", "wallet", "2", "to", "wallet", "1", f"collection={COLLECTION}")
        self.assertIn("Looking for NFTs in 1 wallet", reply)
        self.assertEqual(dest, self.b.minters[0].address.lower())
        self.assertEqual([a.address for a in accounts], [self.b.minters[1].address])
        self.assertEqual(extra, [COLLECTION])
        self.assertFalse(deep)

    def test_variants(self):
        m = self.b.minters
        _r, (dest, accounts, *_rest) = run(self.b, "from", "wallets", "1,", "3", "to", "2")
        self.assertEqual((dest, [a.address for a in accounts]), (m[1].address.lower(), [m[0].address, m[2].address]))
        _r, (dest, accounts, *_rest) = run(self.b, "from", "all", "to", DEST)
        self.assertEqual((dest, len(accounts)), (DEST, 3))
        _r, (dest, accounts, *_rest) = run(self.b, "send", "all", "nfts", "to", "wallet", "1", "from", "wallet", "2", "deep")
        self.assertEqual((dest, [a.address for a in accounts]), (m[0].address.lower(), [m[1].address]))
        _r, (dest, accounts, chains, extra, deep, given) = run(self.b, "FROM", "Wallet", "2", "TO", "Wallet", "1", "deep")
        self.assertTrue(deep)
        _r, (dest, accounts, *_rest) = run(self.b, "from", "2", "to", "wallet", "3")
        self.assertEqual(dest, m[2].address.lower())

    def test_explicit_wallets_option_wins_over_from(self):
        _r, (dest, accounts, *_rest) = run(self.b, "from", "2", "wallets=3", "to", "1")
        self.assertEqual([a.address for a in accounts], [self.b.minters[2].address])

    def test_errors(self):
        reply, args = run(self.b, "from", "wallet", "2", "to", "wallet", "9")
        self.assertIn("no minter number 9", reply)
        self.assertIsNone(args)
        self.assertIn("no minter number 7", run(self.b, "from", "7", "to", "1")[0])
        self.assertIn("could not read", run(self.b, "from", "2", "to", "nowhere")[0])
        self.assertIn("Say where", run(self.b, "from", "2", "to")[0])
        self.assertIn("destination", run(self.b, "from", "2")[0])

    def test_old_form_still_works(self):
        _r, (dest, accounts, *_rest) = run(self.b, DEST, "wallets=2", f"collection={COLLECTION}")
        self.assertEqual(dest, DEST)
        self.assertEqual([a.address for a in accounts], [self.b.minters[1].address])

    def test_usage_shows_the_easy_form(self):
        self.assertIn("from wallet 2 to wallet 1", self.b.sendnfts_command([]))


if __name__ == "__main__":
    unittest.main()
