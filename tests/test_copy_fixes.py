import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402

WATCHED = "0x" + "ab" * 20
COLLECTION = "0x" + "ee" * 20
TEST_KEY = "0x" + "11" * 32


def make_bot():
    values = {
        "WATCH_ADDRESSES": WATCHED,
        "MINTER_PRIVATE_KEYS": TEST_KEY,
        "CHAINS": "1",
        "STATE_PATH": os.path.join(tempfile.mkdtemp(), "state.json"),
    }
    with mock.patch.dict(os.environ, values, clear=True):
        return bot.Bot()


def tx(to=COLLECTION, data="0x", value="0x2386f26fc10000"):
    return {"hash": "0xh1", "from": WATCHED, "to": to, "value": value, "input": data}


class EmptyCallData(unittest.TestCase):
    def test_a_plain_eth_send_to_the_collection_is_a_candidate(self):
        b = make_bot()
        self.assertEqual(b.candidates(tx(), {COLLECTION: 1}), [(COLLECTION, "0x", 10**16)])

    def test_a_plain_send_to_anything_else_is_not(self):
        b = make_bot()
        self.assertEqual(b.candidates(tx(to="0x" + "99" * 20), {COLLECTION: 1}), [])

    def test_an_integer_tx_value_does_not_crash(self):
        b = make_bot()
        self.assertEqual(b.candidates(tx(value=10**16), {COLLECTION: 1}), [(COLLECTION, "0x", 10**16)])

    def test_a_free_empty_call_is_not(self):
        b = make_bot()
        self.assertEqual(b.candidates(tx(value="0x0"), {COLLECTION: 1}), [])

    def test_without_the_minted_collections_nothing_changes(self):
        self.assertEqual(make_bot().candidates(tx()), [])


class SimulationBalance(unittest.TestCase):
    def test_simulation_gives_the_minter_a_balance(self):
        b = make_bot()
        b.rpc = mock.Mock()
        b.rpc.call.return_value = [{"calls": [{"status": "0x1", "gasUsed": "0x5208", "logs": []}]}]
        b.simulate(1, b.minters[0], COLLECTION, "0x", 10**16)
        params = b.rpc.call.call_args[0][2]
        overrides = params[0]["blockStateCalls"][0]["stateOverrides"]
        self.assertEqual(overrides[b.minters[0].address]["balance"], bot.SIM_BALANCE)


class Explanations(unittest.TestCase):
    def test_plain_send(self):
        self.assertIn("no call data", make_bot().explain_no_mint(tx()))

    def test_non_mint_call(self):
        data = bot.selector("approve(address,uint256)") + "0" * 128
        self.assertIn("not a mint", make_bot().explain_no_mint(tx(data=data)))

    def test_seadrop_signed_mint_is_named(self):
        text = make_bot().explain_no_mint(tx(data="0x4b61cd6f" + "0" * 64))
        self.assertIn("SeaDrop signed mint", text)
        self.assertEqual(bot.SEADROP_MINT_SIGNED, "0x4b61cd6f")

    def test_unknown_call_shows_its_function_id(self):
        self.assertIn("0xdeadbeef", make_bot().explain_no_mint(tx(data="0xdeadbeef" + "0" * 64)))


if __name__ == "__main__":
    unittest.main()


class SignedAndAllowlistCalls(unittest.TestCase):
    def test_signed_looking_call_is_still_a_candidate(self):
        from eth_abi import encode
        data = "0x12345678" + encode(["uint256", "bytes"], [1, b"\x01" * 64 + b"\x1b"]).hex()
        self.assertTrue(bot.looks_signed(data))
        b = make_bot()
        source = {"hash": "0xs", "from": "0x" + "ab" * 20, "to": "0x" + "ee" * 20, "value": "0x0", "input": data}
        self.assertEqual(len(b.candidates(source, {"0x" + "ee" * 20: 1})), 1)

    def test_only_the_eligible_minter_sends(self):
        from eth_abi import encode
        data = "0x12345678" + encode(["uint256", "bytes"], [1, b"\x01" * 64 + b"\x1b"]).hex()
        values = {
            "WATCH_ADDRESSES": WATCHED,
            "MINTER_PRIVATE_KEYS": TEST_KEY + ",0x" + "22" * 32 + ",0x" + "33" * 32,
            "CHAINS": "1",
            "STATE_PATH": os.path.join(tempfile.mkdtemp(), "state.json"),
        }
        with mock.patch.dict(os.environ, values, clear=True):
            b = bot.Bot()
        collection = "0x" + "ee" * 20
        eligible = b.minters[0].address.lower()
        sims = []

        def simulate(chain_id, acct, target, call, value):
            sims.append(acct.address.lower())
            if acct.address.lower() != eligible:
                return None
            return [], 100_000

        b.simulate = simulate
        b.minted_for = lambda logs, who: {collection: 1} if who.lower() == eligible else {}
        chosen = [b.pick(1, acct, [(collection, data, 0)], {collection: 1}) for acct in b.minters]
        self.assertIsNotNone(chosen[0])
        for other in chosen[1:]:
            self.assertIsNone(other)
        self.assertIn("not eligible", b.last_pick_reason)

    def test_a_wallet_on_the_phase_is_not_failed_with_the_source_signature(self):
        from eth_abi import encode
        data = "0x12345678" + encode(["uint256", "bytes"], [1, b"\x01" * 64 + b"\x1b"]).hex()
        values = {
            "WATCH_ADDRESSES": WATCHED,
            "MINTER_PRIVATE_KEYS": TEST_KEY + ",0x" + "22" * 32,
            "CHAINS": "1",
            "STATE_PATH": os.path.join(tempfile.mkdtemp(), "state.json"),
        }
        with mock.patch.dict(os.environ, values, clear=True):
            b = bot.Bot()
        collection = "0x" + "ee" * 20
        b.simulate = lambda *args, **kwargs: None
        b._revert = {"message": "execution reverted", "data": ""}
        b.rpc = mock.Mock()
        b.rpc.call.side_effect = RuntimeError("no such function")

        def own(chain_id, acct, want):
            if acct is b.minters[0]:
                return ("mint", (0, bot.SEADROP, "0x4b61cd6f", 90_000))
            return ("reason", "not on an allowlist for this drop")

        b.own_phase_choice = own
        first = b.pick(1, b.minters[0], [(collection, data, 0)], {collection: 1})
        second = b.pick(1, b.minters[1], [(collection, data, 0)], {collection: 1})
        self.assertEqual(first[1], bot.SEADROP)
        self.assertIsNone(second)
        self.assertIn("not on an allowlist", b.last_pick_reason)
        self.assertNotIn("source wallet", b.last_pick_reason)

    def test_one_minter_error_does_not_cancel_the_others(self):
        values = {
            "WATCH_ADDRESSES": WATCHED,
            "MINTER_PRIVATE_KEYS": TEST_KEY + ",0x" + "22" * 32,
            "CHAINS": "1",
            "STATE_PATH": os.path.join(tempfile.mkdtemp(), "state.json"),
        }
        with mock.patch.dict(os.environ, values, clear=True):
            b = bot.Bot()
        collection = "0x" + "ee" * 20

        def pick(chain_id, acct, options, want):
            if acct is b.minters[0]:
                raise TypeError("bad log")
            return (0, collection, "0x1234", 80_000)

        b.pick = pick
        b.fees = lambda chain_id: (1, 1)
        b.ensure = lambda *args, **kwargs: True
        b.land = lambda *args, **kwargs: ("0xabc", True, ["0xabc"])
        b.rpc = mock.Mock()
        b.rpc.call.return_value = "0x"
        tx = {"hash": "0x" + "cd" * 32, "from": WATCHED, "to": collection, "value": "0x0", "input": "0x1234"}
        with mock.patch.object(bot, "notify"), mock.patch.object(bot, "log"):
            done = b.copy(1, tx, {collection: 1}, options=[(collection, "0x1234", 0)])
        self.assertEqual(done, 1)


class RealRevertReasons(unittest.TestCase):
    def test_decodes_error_string(self):
        from eth_abi import encode
        data = "0x08c379a0" + encode(["string"], ["Sold out"]).hex()
        self.assertEqual(bot.decode_revert(data), 'the contract said "Sold out"')
        self.assertTrue(bot.classify_reason(bot.decode_revert(data)).startswith("sold out"))

    def test_decodes_known_custom_error(self):
        self.assertEqual(bot.decode_revert(bot.selector("MaxSupplyReached()")), "sold out")
        self.assertIn("not open", bot.decode_revert(bot.selector("MintNotStarted()")))

    def test_decodes_panic_and_message_only(self):
        panic = "0x4e487b71" + "11".rjust(64, "0")
        self.assertIn("0x11", bot.decode_revert(panic))
        self.assertEqual(bot.decode_revert("", "execution reverted: Mint closed"), 'the contract said "Mint closed"')
        self.assertEqual(bot.decode_revert("", "execution reverted"), "")

    def test_sold_out_is_read_from_the_contract(self):
        b = make_bot()
        collection = "0x" + "ee" * 20
        word = "0x" + (1000).to_bytes(32, "big").hex()

        def call(chain_id, method, params):
            sel = params[0]["data"]
            if sel in (bot.selector("totalSupply()"), bot.selector("maxSupply()")):
                return word
            raise RuntimeError("no such function")

        b.rpc = mock.Mock()
        b.rpc.call.side_effect = call
        self.assertEqual(b.explain_revert(1, [(collection, None)]), "sold out: 1000 of 1000 minted")

    def test_pick_reports_the_contract_reason(self):
        from eth_abi import encode
        b = make_bot()
        collection = "0x" + "ee" * 20
        b.rpc = mock.Mock()
        b.rpc.call.side_effect = RuntimeError("no such function")

        def simulate(chain_id, acct, target, call, value):
            b._revert = {"message": "execution reverted", "data": "0x08c379a0" + encode(["string"], ["Mint not live"]).hex()}
            return None

        b.simulate = simulate
        self.assertIsNone(b.pick(1, b.minters[0], [(collection, "0xa0712d68" + "0" * 63 + "1", 0)], {collection: 1}))
        self.assertIn("not open", b.last_pick_reason)
        self.assertNotIn("price, allowlist, phase or supply", b.last_pick_reason)

    def test_no_reason_is_said_plainly(self):
        b = make_bot()
        collection = "0x" + "ee" * 20
        b.rpc = mock.Mock()
        b.rpc.call.side_effect = RuntimeError("no such function")

        def simulate(chain_id, acct, target, call, value):
            b._revert = {"message": "execution reverted", "data": ""}
            return None

        b.simulate = simulate
        b.pick(1, b.minters[0], [(collection, "0xa0712d68" + "0" * 63 + "1", 0)], {collection: 1})
        self.assertEqual(b.last_pick_reason, "the mint reverted and the contract gave no reason")
