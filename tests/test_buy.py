import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot  # noqa: E402
import opensea  # noqa: E402
from eth_abi import decode  # noqa: E402

WATCHED = "0x" + "ab" * 20
KEYS = "0x" + "11" * 32 + ",0x" + "22" * 32
NFT = "0x" + "c1" * 20
SEAPORT = "0x0000000000000068F116a894984e2DB1123eB395"
LINK = f"https://opensea.io/item/robinhood/{NFT}/7"
ETH = 10**18


def make_bot(**env):
    values = {
        "WATCH_ADDRESSES": WATCHED,
        "MINTER_PRIVATE_KEYS": KEYS,
        "CHAINS": "4663",
        "STATE_PATH": os.path.join(tempfile.mkdtemp(), "state.json"),
        "OPENSEA_API_KEY": "test-key",
    }
    values.update(env)
    with mock.patch.dict(os.environ, values, clear=True):
        return bot.Bot()


def listing(price=10**15, native=True, quantity=1):
    return {"hash": "0xhash", "protocol_address": SEAPORT, "price_wei": price, "native": native, "quantity": quantity}


class FakeOpenSea:
    def __init__(self, price=10**15, found=True, **extra):
        self.found = found
        self.price = price
        self.listing = listing(price, **extra)
        self.to = SEAPORT
        self.value = price
        self.calls = []

    def collection_slug(self, chain, contract, token):
        return "robo-cards", "Robo Card"

    def best_listing(self, chain, collection, contract, token):
        return self.listing if self.found else None

    def fulfilment(self, lst, chain, buyer):
        self.calls.append(buyer)
        return self.to, self.value, "0xdeadbeef", "fulfillBasicOrder_efficient_6GL6yc"


def transfer_log(contract, to, token):
    return {
        "address": contract,
        "topics": [bot.TRANSFER, "0x" + "0" * 64, "0x" + "0" * 24 + to[2:].lower(), hex(token)],
        "data": "0x",
    }


class LinkAndCalldataTests(unittest.TestCase):
    def test_selectors_match_seaport(self):
        self.assertEqual(opensea.selector(opensea.ADVANCED_SIGNATURE), "0xe7acab24")
        self.assertEqual(opensea.selector(opensea.BASIC_SIGNATURES["fulfillBasicOrder_efficient_6GL6yc"]), "0x00000000")
        self.assertEqual(opensea.selector(opensea.BASIC_SIGNATURES["fulfillBasicOrder"]), "0xfb0f3ee1")

    def test_parse_links(self):
        for text in (LINK, f"opensea.io/item/robinhood/{NFT}/7", f"https://opensea.io/assets/robinhood/{NFT}/7", f"buy https://opensea.io/en/item/robinhood/{NFT}/7?x=1"):
            self.assertEqual(opensea.parse_item_link(text), ("robinhood", NFT, 7))
        self.assertIsNone(opensea.parse_item_link("https://opensea.io/collection/robo"))

    def test_normalise_shapes(self):
        best = {"order_hash": "0xaa", "protocol_address": SEAPORT, "price": {"current": {"currency": "ETH", "decimals": 18, "value": "1000"}}}
        one = opensea.normalise_listing(best)
        self.assertEqual((one["hash"], one["price_wei"], one["native"], one["quantity"]), ("0xaa", 1000, True, 1))
        orders = {"orders": [{"order_hash": "0xbb", "current_price": "2000", "protocol_data": {"parameters": {"consideration": [{"itemType": 0}]}}}]}
        self.assertEqual(opensea.normalise_listing(orders)["price_wei"], 2000)
        token = {"orders": [{"order_hash": "0xcc", "current_price": "5", "protocol_data": {"parameters": {"consideration": [{"itemType": 1}]}}}]}
        self.assertFalse(opensea.normalise_listing(token)["native"])
        self.assertIsNone(opensea.normalise_listing({"price": {}}))

    def test_basic_order_round_trip(self):
        params = {
            "considerationToken": opensea.ZERO_ADDRESS, "considerationIdentifier": "0", "considerationAmount": "900",
            "offerer": "0x" + "01" * 20, "zone": opensea.ZERO_ADDRESS, "offerToken": NFT, "offerIdentifier": "7",
            "offerAmount": "1", "basicOrderType": 0, "startTime": "1", "endTime": "99", "zoneHash": opensea.ZERO32,
            "salt": "5", "offererConduitKey": opensea.ZERO32, "fulfillerConduitKey": opensea.ZERO32,
            "totalOriginalAdditionalRecipients": "1", "additionalRecipients": [{"amount": "100", "recipient": "0x" + "02" * 20}],
            "signature": "0x1234",
        }
        data = opensea.build_calldata("fulfillBasicOrder_efficient_6GL6yc", {"parameters": params})
        self.assertTrue(data.startswith("0x00000000"))
        (decoded,) = decode([opensea.BASIC_ORDER], bytes.fromhex(data[10:]))
        self.assertEqual(decoded[2], 900)
        self.assertEqual(decoded[6], 7)
        self.assertEqual(decoded[16][0][0], 100)
        self.assertEqual(decoded[17], bytes.fromhex("1234"))

    def test_advanced_order_round_trip(self):
        order = {
            "parameters": {
                "offerer": "0x" + "01" * 20, "zone": opensea.ZERO_ADDRESS,
                "offer": [{"itemType": 2, "token": NFT, "identifierOrCriteria": "7", "startAmount": "1", "endAmount": "1"}],
                "consideration": [{"itemType": 0, "token": opensea.ZERO_ADDRESS, "identifierOrCriteria": "0", "startAmount": "900", "endAmount": "900", "recipient": "0x" + "01" * 20}],
                "orderType": 0, "startTime": "1", "endTime": "99", "zoneHash": opensea.ZERO32, "salt": "5",
                "conduitKey": opensea.ZERO32, "totalOriginalConsiderationItems": 1,
            },
            "numerator": 1, "denominator": 1, "signature": "0xabcd", "extraData": "0x",
        }
        buyer = "0x" + "03" * 20
        data = opensea.build_calldata("fulfillAdvancedOrder", {"orders": [order], "fulfillerConduitKey": opensea.ZERO32, "recipient": buyer})
        self.assertTrue(data.startswith("0xe7acab24"))
        decoded = decode([opensea.ADVANCED_ORDER, f"{opensea.CRITERIA_RESOLVER}[]", "bytes32", "address"], bytes.fromhex(data[10:]))
        self.assertEqual(decoded[0][0][2][0][2], 7)
        self.assertEqual(decoded[0][3], bytes.fromhex("abcd"))
        self.assertEqual(decoded[3].lower(), buyer)

    def test_unknown_function_and_bad_input(self):
        with self.assertRaises(opensea.OpenSeaError):
            opensea.build_calldata("matchOrders", {})
        with self.assertRaises(opensea.OpenSeaError):
            opensea.build_calldata("fulfillAdvancedOrder", {"orders": [{"parameters": {}}]})

    def test_nft_received(self):
        buyer = "0x" + "03" * 20
        self.assertTrue(bot.nft_received([transfer_log(NFT, buyer, 7)], NFT, buyer, 7))
        self.assertFalse(bot.nft_received([transfer_log(NFT, buyer, 8)], NFT, buyer, 7))
        self.assertFalse(bot.nft_received([transfer_log(NFT, "0x" + "04" * 20, 7)], NFT, buyer, 7))
        self.assertFalse(bot.nft_received([transfer_log("0x" + "c9" * 20, buyer, 7)], NFT, buyer, 7))


class BuyCommandTests(unittest.TestCase):
    def test_needs_api_key(self):
        b = make_bot(OPENSEA_API_KEY="")
        self.assertIn("OPENSEA_API_KEY", b.buy_command([LINK]))

    def test_usage_and_bad_link(self):
        b = make_bot()
        self.assertIn("Usage", b.buy_command([]))
        self.assertIn("item link", b.buy_command(["https://opensea.io/collection/x"]))

    def test_chain_not_enabled(self):
        b = make_bot()
        text = b.buy_command([f"https://opensea.io/item/base/{NFT}/7"])
        self.assertIn("not running", text)

    def test_no_listing_and_limits(self):
        b = make_bot()
        b.opensea = FakeOpenSea(found=False)
        self.assertIn("no active listing", b.buy_command([LINK]))
        b.opensea = FakeOpenSea(native=False)
        self.assertIn("token", b.buy_command([LINK]))
        b.opensea = FakeOpenSea(quantity=3)
        self.assertIn("more than one", b.buy_command([LINK]))
        b.opensea = FakeOpenSea(price=ETH)
        self.assertIn("above your limit", b.buy_command([LINK]))
        self.assertIn("above your limit", b.buy_command([LINK, "max=5"]))
        self.assertIn("could not read", b.buy_command([LINK, "max=abc"]))
        self.assertIsNone(b.buy_plan)

    def test_preview_then_confirm_runs(self):
        b = make_bot(DRY_RUN="true")
        b.opensea = FakeOpenSea()
        with mock.patch.object(b, "balance", return_value=ETH):
            text = b.buy_command([LINK, "wallets=2"])
        self.assertIn("/buy confirm", text)
        self.assertIn(NFT, text)
        self.assertEqual(b.buy_plan["wallets"], [b.minters[1].address.lower()])
        sent = []
        with mock.patch.object(bot, "alert", side_effect=lambda msg, rich=None: sent.append(msg)), \
             mock.patch.object(b, "balance", return_value=ETH), \
             mock.patch.object(b, "fees", return_value=(10**9, 10**9)), \
             mock.patch.object(b, "simulate", return_value=([transfer_log(NFT, b.minters[1].address, 7)], 200_000)), \
             mock.patch.object(bot.threading, "Thread") as thread:
            self.assertIn("Buying", b.buy_command(["confirm"]))
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        self.assertTrue(sent[0].startswith("Would buy"))
        self.assertEqual(b.opensea.calls, [b.minters[1].address])
        self.assertIsNone(b.buy_plan)

    def test_confirm_without_plan_cancel_and_expiry(self):
        b = make_bot()
        self.assertIn("no purchase", b.buy_command(["confirm"]))
        self.assertIn("Nothing", b.buy_command(["cancel"]))
        b.buy_plan = {"state": "planned", "created": time.time() - 500}
        self.assertIn("expired", b.buy_command(["confirm"]))

    def run_plan(self, b, sim, land=(("0xtx", True, ["0xtx"])), balance=ETH, fos=None):
        b.opensea = fos or FakeOpenSea()
        with mock.patch.object(b, "balance", return_value=ETH):
            b.buy_command([LINK])
        sent = []
        with mock.patch.object(bot, "alert", side_effect=lambda msg, rich=None: sent.append((msg, rich))), \
             mock.patch.object(b, "balance", return_value=balance), \
             mock.patch.object(b, "fees", return_value=(10**9, 10**9)), \
             mock.patch.object(b, "simulate", side_effect=sim), \
             mock.patch.object(b, "land", return_value=land) as landed, \
             mock.patch.object(b, "record_holdings_later") as record:
            plan = b.buy_plan
            plan["state"] = "running"
            b.run_buy(plan)
        return sent, landed, record

    def test_buys_with_first_wallet_and_stops(self):
        b = make_bot()
        first = b.minters[0].address
        sent, landed, record = self.run_plan(b, [([transfer_log(NFT, first, 7)], 200_000)])
        self.assertEqual(landed.call_count, 1)
        self.assertEqual(landed.call_args.args[1].address, first)
        self.assertEqual(landed.call_args.args[2], SEAPORT)
        record.assert_called_once()
        self.assertTrue(sent[0][0].startswith("Bought"))

    def test_second_wallet_after_first_fails_sim(self):
        b = make_bot()
        sims = [None, ([transfer_log(NFT, b.minters[1].address, 7)], 200_000)]
        b._revert = None
        sent, landed, _ = self.run_plan(b, sims)
        self.assertEqual(landed.call_count, 1)
        self.assertEqual(landed.call_args.args[1].address, b.minters[1].address)
        self.assertIn("would fail", sent[0][1])

    def test_no_nft_in_simulation_blocks_send(self):
        b = make_bot()
        sent, landed, _ = self.run_plan(b, [([transfer_log(NFT, "0x" + "09" * 20, 7)], 200_000)] * 2)
        landed.assert_not_called()
        self.assertTrue(sent[0][0].startswith("Not bought"))

    def test_unsupported_simulation_blocks_send(self):
        b = make_bot()
        sent, landed, _ = self.run_plan(b, ["unsupported", "unsupported"])
        landed.assert_not_called()
        self.assertIn("cannot simulate", sent[0][1])

    def test_price_jump_skips(self):
        b = make_bot()
        fos = FakeOpenSea()
        fos.value = 10**15 * 2
        sent, landed, _ = self.run_plan(b, [], fos=fos)
        landed.assert_not_called()
        self.assertIn("price moved", sent[0][1])

    def test_price_over_cap_at_send_time_skips(self):
        b = make_bot(MAX_BUY_ETH="0.001")
        fos = FakeOpenSea(price=10**15)
        fos.value = 10**15 + 1
        sent, landed, _ = self.run_plan(b, [], fos=fos)
        landed.assert_not_called()
        self.assertIn("above your limit", sent[0][1])

    def test_unknown_target_refused(self):
        b = make_bot()
        fos = FakeOpenSea()
        fos.to = "0x" + "99" * 20
        sent, landed, _ = self.run_plan(b, [], fos=fos)
        landed.assert_not_called()
        self.assertIn("Seaport", sent[0][1])

    def test_short_funds_skips(self):
        b = make_bot()
        sent, landed, _ = self.run_plan(b, [], balance=10)
        landed.assert_not_called()
        self.assertIn("needs", sent[0][1])

    def test_failed_transaction_reports(self):
        b = make_bot()
        first = b.minters[0].address
        second = b.minters[1].address
        sims = [([transfer_log(NFT, first, 7)], 200_000), ([transfer_log(NFT, second, 7)], 200_000)]
        with mock.patch.object(b, "receipt_for", return_value=("0xtx", False)):
            sent, landed, record = self.run_plan(b, sims, land=("0xtx", False, ["0xtx"]))
        self.assertEqual(landed.call_count, 2)
        record.assert_not_called()
        self.assertTrue(sent[0][0].startswith("Not bought"))

    def test_slow_confirmation_is_pending_and_stops_other_wallets(self):
        b = make_bot()
        first = b.minters[0].address
        sims = [([transfer_log(NFT, first, 7)], 200_000)] * 2
        with mock.patch.object(b, "receipt_for", return_value=None):
            sent, landed, record = self.run_plan(b, sims, land=("0xtx", False, ["0xtx"]))
        self.assertEqual(landed.call_count, 1)
        self.assertTrue(sent[0][0].startswith("Sent, waiting"))

    def test_late_receipt_counts_as_bought(self):
        b = make_bot()
        first = b.minters[0].address
        with mock.patch.object(b, "receipt_for", return_value=("0xtx", True)):
            sent, landed, record = self.run_plan(b, [([transfer_log(NFT, first, 7)], 200_000)], land=("0xtx", False, ["0xtx"]))
        record.assert_called_once()
        self.assertTrue(sent[0][0].startswith("Bought"))

    def test_cancel_keeps_plan_until_run_ends_and_blocks_a_new_buy(self):
        b = make_bot()
        b.opensea = FakeOpenSea()
        with mock.patch.object(b, "balance", return_value=ETH):
            b.buy_command([LINK])
        plan = b.buy_plan
        plan["state"] = "running"
        self.assertIn("Stopping", b.buy_command(["cancel"]))
        self.assertTrue(plan["cancel"].is_set())
        self.assertIs(b.buy_plan, plan)
        self.assertIn("already running or stopping", b.buy_command([LINK]))


if __name__ == "__main__":
    unittest.main()
