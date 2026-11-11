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


class GasWarning(unittest.TestCase):
    def test_plan_warns_when_wallet_cannot_pay_gas(self):
        b = make_bot()
        wallet = b.minters[0].address.lower()
        transfers = [{"chain": 4663, "wallet": wallet, "collection": C721, "std": 721, "id": "1", "amount": 1, "gas": 100_000}]
        with mock.patch.object(b, "fees", return_value=(10**9, 1)), mock.patch.object(b, "balance", return_value=5):
            short = b.gas_shortfalls(transfers)
        self.assertEqual(short, [(4663, wallet, 5, 100_000 * 10**9)])
        plan = {"dest": DEST, "transfers": transfers, "shortfalls": short}
        text = b.send_plan_text(plan, {}, [], [], [], False)
        self.assertIn("for gas", text)
        with mock.patch.object(b, "fees", return_value=(10**9, 1)), mock.patch.object(b, "balance", return_value=10**18):
            self.assertEqual(b.gas_shortfalls(transfers), [])


if __name__ == "__main__":
    unittest.main()


class RefusalDiagnosis(unittest.TestCase):
    def test_unknown_custom_error_is_named_not_garbled(self):
        from helpers import decode_revert

        text = decode_revert("", "execution reverted {'message': 'execution reverted', 'data': '0x369c2f4c'}")
        self.assertIn("0x369c2f4c", text)
        self.assertNotIn("'data'", text)
        self.assertNotIn("{", decode_revert("", "execution reverted: ', 'data': {x}"))

    def test_code_kinds(self):
        b = make_bot()
        for code, want in (("0x", "none"), ("0xef0100" + "ab" * 20, "delegated"), ("0x6080604052", "contract")):
            b.rpc.call = lambda chain, method, params, code=code: code
            self.assertEqual(b.code_kind(4663, DEST), want)

    def test_refusal_says_whether_the_receiver_or_the_collection_is_to_blame(self):
        b = make_bot()
        t = {"std": 721, "wallet": "0x" + "11" * 20, "collection": C721, "id": "5"}
        b.rpc.call = lambda chain, method, params: "0x5208"  # a plain transfer works
        self.assertIn("receiving address", b.refusal_reason(4663, t, DEST, "refused"))

        def fail(chain, method, params):
            raise RuntimeError("execution reverted")

        b.rpc.call = fail
        self.assertIn("collection itself", b.refusal_reason(4663, t, DEST, "refused"))

    def test_plan_groups_refusals_by_collection(self):
        b = make_bot()
        t = {"chain": 4663, "wallet": "0x" + "11" * 20, "collection": C721, "std": 721, "id": "1", "amount": 1}
        text = b.send_plan_text({"dest": DEST, "transfers": [], "shortfalls": []}, {}, [(t, "locked", "Yokai"), (dict(t, id="2"), "locked", "Yokai")], [], [], False)
        self.assertIn("• Yokai ×2: locked", text)
