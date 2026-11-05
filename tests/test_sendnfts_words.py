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


class OpenSeaHoldings(unittest.TestCase):
    def setup(self, listed):
        b = node(make_bot())
        me = b.minters[0].address.lower()
        b.fake.enumerable = False
        b.fake.owner = {(C721, i): me for i in (11, 12, 13, 14)}
        b.fake.owner[(C721, 99)] = "0x" + "99" * 20
        b.opensea = mock.Mock()
        b.opensea.account_nfts.return_value = listed
        b.collection_slug_for = lambda *a: "robo"
        return b, me

    def test_opensea_lists_tokens_the_contract_cannot(self):
        b, me = self.setup([11, 12, 13, 14])
        owned, missing, note = b.nfts_721(4663, C721, me, [], False)
        self.assertEqual((owned, missing), ([11, 12, 13, 14], 0))
        b.opensea.account_nfts.assert_called_once()

    def test_stale_opensea_ids_are_checked_on_chain(self):
        b, me = self.setup([11, 99, 12, 13, 14])
        owned, missing, _note = b.nfts_721(4663, C721, me, [], False)
        self.assertEqual(owned, [11, 12, 13, 14])

    def test_an_opensea_failure_falls_back_to_the_old_notes(self):
        b, me = self.setup([])
        b.opensea.account_nfts.side_effect = RuntimeError("down")
        owned, missing, note = b.nfts_721(4663, C721, me, [], False)
        self.assertEqual((owned, missing), ([], 4))

    def test_client_filters_by_contract_and_pages(self):
        import opensea

        c = opensea.OpenSea("k")
        pages = [
            {"nfts": [{"contract": C721, "identifier": "5"}, {"contract": "0x" + "ab" * 20, "identifier": "6"}], "next": "p2"},
            {"nfts": [{"contract": C721.upper().replace("0X", "0x"), "identifier": "7"}], "next": None},
        ]
        seen = []

        def request(method, path, params=None, body=None, timeout=20):
            seen.append(params)
            return pages[len(seen) - 1]

        c.request = request
        self.assertEqual(c.account_nfts("robinhood", "0xw", C721, "robo"), [5, 7])
        self.assertEqual(seen[1]["next"], "p2")
        self.assertEqual(seen[0]["collection"], "robo")
