import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eth_abi import encode  # noqa: E402

import bot  # noqa: E402
import market  # noqa: E402
import opensea  # noqa: E402
import seaport  # noqa: E402
import settings  # noqa: E402
from test_buy import ETH, NFT, make_bot  # noqa: E402

LINK = f"https://opensea.io/item/robinhood/{NFT}/7"
CREATOR = "0x" + "cc" * 20
FEES = [("0x0000a26b00c1F0DF003000390027140000fAa719", 1.0), (CREATOR, 2.5)]
CONDUIT = "0x" + "aa" * 20


class MarketOpenSea:
    def __init__(self, offer=10**15, fees=FEES, known=True):
        self.offer = offer
        self.fees = fees
        self.known = known
        self.posted = []
        self.cancelled = []
        self.post_error = None
        self.sales = 5

    def collection_slug(self, chain, contract, token):
        return "robo-cards", "Robo Card"

    def contract_collection(self, chain, contract):
        return ("robo-cards", "Robo Card") if self.known else (None, None)

    def best_offer(self, slug, token):
        return {"hash": "0xo", "price_wei": self.offer, "token": "0x" + "e7" * 20} if self.offer else None

    def collection_info(self, slug):
        return {"name": "Robo Cards", "fees": self.fees}

    def collection_sales(self, slug):
        return self.sales

    def post_order(self, chain, kind, params, signature, protocol_address=seaport.SEAPORT):
        if self.post_error:
            raise self.post_error
        self.posted.append((chain, kind, params, signature))
        return f"0xorder{len(self.posted)}"

    def cancel_order(self, chain, order_hash, protocol_address=seaport.SEAPORT):
        self.cancelled.append(order_hash)


def make_market(**env):
    b = make_bot(**env)
    b.opensea = MarketOpenSea()
    b._seaport[4663] = {"version": "1.6", "conduit": CONDUIT}
    return b


def patch_chain(b, owner=True, approved=1, counter=0):
    """Fake the reads list_one makes. Returns the send_native_tx mock."""
    b.owns = lambda *a, **k: owner
    b.view_int = lambda chain, to, data: {seaport.IS_APPROVED_FOR_ALL: approved, seaport.GET_COUNTER: counter}.get(data[:10], 0)
    b.prepare_order = lambda chain, acct, comps: ("0x" + "ab" * 65, None)
    send = mock.Mock(return_value=("0xtx", True))
    b.send_native_tx = send
    return send


class SettingsTests(unittest.TestCase):
    def test_show_set_reset_and_persist(self):
        b = make_bot()
        self.assertIn("autolist = off", b.settings_command([]))
        self.assertIn("✅ autolist = on", b.settings_command(["autolist", "on"]))
        self.assertTrue(b.setting("autolist"))
        self.assertIn("autolist = on ✏️", b.settings_command([]))
        b.settings_command(["autolist_multiple", "3"])
        again = bot.Bot.__new__(bot.Bot)
        self.assertEqual(b.settings["autolist_multiple"], 3.0)
        self.assertIn("back to its default", b.settings_command(["reset", "autolist"]))
        self.assertFalse(b.setting("autolist"))
        self.assertIn("Every setting", b.settings_command(["reset", "all"]))

    def test_persists_across_restart(self):
        b = make_bot()
        b.settings_command(["autolist", "on"])
        with mock.patch.dict(os.environ, {"WATCH_ADDRESSES": "0x" + "ab" * 20, "MINTER_PRIVATE_KEYS": "0x" + "11" * 32, "CHAINS": "4663", "STATE_PATH": str(b.state_path)}, clear=True):
            fresh = bot.Bot()
        self.assertTrue(fresh.setting("autolist"))

    def test_bad_values(self):
        b = make_bot()
        for args in (["nope", "1"], ["autolist", "maybe"], ["autolist_multiple", "0.5"], ["autolist_multiple", "abc"], ["autolist_seconds", "1"], ["autolist_seconds", "1.5"], ["autolist_minimum", "-1"]):
            self.assertIn("⚠️", b.settings_command(args), args)
        self.assertEqual(settings.parse_value("autolist_minimum", "0.010")[0], "0.01")


class PriceSpecTests(unittest.TestCase):
    def test_specs(self):
        self.assertEqual(market.parse_price_spec("2x"), ("multiple", 2.0))
        self.assertEqual(market.parse_price_spec("0.05")[0], "price")
        for bad in ("0.5x", "100x", "abc", "-1", "nan", "0"):
            self.assertEqual(market.parse_price_spec(bad), (None, None), bad)


class SeaportReadyTests(unittest.TestCase):
    def fake_view(self, version="1.6", exists=True, chain=4663):
        sep = seaport.domain_separator(chain, seaport.SEAPORT, version)

        def view(chain_id, to, data):
            if data == seaport.INFORMATION:
                return "0x" + encode(["string", "bytes32", "address"], [version, sep, seaport.CONDUIT_CONTROLLER]).hex()
            if data.startswith(seaport.GET_CONDUIT):
                return "0x" + encode(["address", "bool"], [CONDUIT, exists]).hex()
            return None

        return view

    def test_ok_and_cached(self):
        b = make_bot()
        b.view = self.fake_view()
        info, error = b.seaport_ready(4663)
        self.assertIsNone(error)
        self.assertEqual(info["conduit"], CONDUIT)
        b.view = lambda *a: None
        self.assertIsNotNone(b.seaport_ready(4663)[0])

    def test_domain_mismatch_and_missing_conduit(self):
        b = make_bot()
        b.view = self.fake_view(chain=1)
        self.assertIn("domain", b.seaport_ready(4663)[1])
        b = make_bot()
        b.view = self.fake_view(exists=False)
        self.assertIn("conduit", b.seaport_ready(4663)[1])
        b = make_bot()
        b.view = lambda *a: None
        self.assertIn("did not answer", b.seaport_ready(4663)[1])

    def test_prepare_order_checks_the_contract_hash(self):
        b = make_bot()
        comps = seaport.build_listing(b.minters[0].address, NFT, 7, 721, 10**18, FEES, seaport.OPENSEA_CONDUIT_KEY, 0)
        b.view = lambda chain, to, data: "0x" + seaport.order_hash(comps).hex()
        signature, error = b.prepare_order(4663, b.minters[0], comps)
        self.assertIsNone(error)
        self.assertEqual(len(signature), 132)
        b.view = lambda chain, to, data: "0x" + "00" * 32
        self.assertIsNone(b.prepare_order(4663, b.minters[0], comps)[0])
        b.view = lambda chain, to, data: None
        self.assertIn("encoding", b.prepare_order(4663, b.minters[0], comps)[1])


class ListOneTests(unittest.TestCase):
    def call(self, b, price=10**17, managed=True):
        return b.list_one(4663, b.minters[0], NFT, 7, 721, price, "robo-cards", 14, managed)

    def test_listed(self):
        b = make_market()
        send = patch_chain(b)
        status, detail, entry = self.call(b)
        self.assertEqual(status, "listed")
        chain, kind, params, signature = b.opensea.posted[0]
        self.assertEqual((chain, kind), ("robinhood", "listings"))
        self.assertEqual(params["totalOriginalConsiderationItems"], 3)
        self.assertEqual(entry["hash"], "0xorder1")
        self.assertEqual(entry["price"], str(10**17))
        self.assertEqual(int(entry["net"]), 10**17 - 10**15 - 25 * 10**14)
        send.assert_not_called()

    def test_sends_approval_first_when_needed(self):
        b = make_market()
        send = patch_chain(b, approved=0)
        self.assertEqual(self.call(b)[0], "listed")
        self.assertEqual(send.call_args.args[2], NFT)
        self.assertTrue(send.call_args.args[3].startswith(seaport.SET_APPROVAL_FOR_ALL))

    def test_failed_approval_stops(self):
        b = make_market()
        send = patch_chain(b, approved=0)
        send.return_value = ("0xtx", False)
        self.assertEqual(self.call(b)[0], "failed")
        self.assertEqual(b.opensea.posted, [])

    def test_dry_run_posts_nothing(self):
        b = make_market(DRY_RUN="true")
        send = patch_chain(b, approved=0)
        self.assertEqual(self.call(b)[0], "would")
        send.assert_not_called()
        self.assertEqual(b.opensea.posted, [])

    def test_not_owner_no_fees_and_api_errors(self):
        b = make_market()
        patch_chain(b, owner=False)
        self.assertIn("does not hold", self.call(b)[1])
        b = make_market()
        b.opensea.fees = []
        patch_chain(b)
        self.assertIn("fee details", self.call(b)[1])
        b = make_market()
        patch_chain(b)
        b.opensea.post_error = opensea.OpenSeaError("bad order", 400)
        self.assertEqual(self.call(b)[0], "failed")
        b.opensea.post_error = opensea.OpenSeaError("down", 503)
        self.assertEqual(self.call(b)[0], "retry")

    def test_seaport_not_ready(self):
        b = make_market()
        b._seaport.clear()
        b.view = lambda *a: None
        self.assertEqual(self.call(b)[0], "failed")


class ListCommandTests(unittest.TestCase):
    def setup(self, **env):
        b = make_market(**env)
        patch_chain(b)
        b.detect_standard = lambda *a: 721
        return b

    def test_usage_and_key(self):
        b = self.setup()
        self.assertIn("Usage", b.list_command([]))
        self.assertIn("OPENSEA_API_KEY", make_bot(OPENSEA_API_KEY="").list_command([LINK, "0.1"]))

    def test_multiple_of_top_offer_then_confirm(self):
        b = self.setup()
        text = b.list_command([LINK, "2x"])
        self.assertIn("/list confirm", text)
        self.assertIn("auto-listing may raise", text)
        self.assertEqual(b.market_plan["actions"][0]["price"], 2 * 10**15)
        self.assertTrue(b.market_plan["actions"][0]["managed"])
        with mock.patch.object(bot.threading, "Thread") as thread, mock.patch.object(b, "alert"):
            self.assertIn("Working on it", b.list_command(["confirm"]))
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        self.assertEqual(len(b.opensea.posted), 1)
        self.assertEqual(len(b.autolist), 1)
        self.assertIsNone(b.market_plan)

    def test_fixed_price_is_not_managed(self):
        b = self.setup()
        b.list_command([LINK, "0.05"])
        self.assertFalse(b.market_plan["actions"][0]["managed"])
        self.assertEqual(b.market_plan["actions"][0]["price"], 5 * 10**16)

    def test_no_offer_for_multiple_and_other_refusals(self):
        b = self.setup()
        b.opensea.offer = 0
        self.assertIn("no offer", b.list_command([LINK, "2x"]))
        b = self.setup()
        self.assertIn("price", b.list_command([LINK]))
        self.assertIn("item link", b.list_command(["0.1"]))
        b.settings["autolist_minimum"] = "1"
        self.assertIn("below your autolist_minimum", b.list_command([LINK, "0.1"]))
        b = self.setup()
        b.owns = lambda *a, **k: False
        self.assertIn("None of the chosen wallets", b.list_command([LINK, "0.1"]))

    def test_confirm_without_plan_and_cancel(self):
        b = self.setup()
        self.assertIn("no list waiting", b.list_command(["confirm"]))
        b.list_command([LINK, "0.1"])
        self.assertIn("Cancelled", b.list_command(["cancel"]))
        self.assertIsNone(b.market_plan)

    def test_busy_blocks_new_plan(self):
        b = self.setup()
        b.list_command([LINK, "0.1"])
        b.market_plan["state"] = "running"
        self.assertIn("running", b.list_command([LINK, "0.1"]))


class UnlistTests(unittest.TestCase):
    def entry(self, b):
        comps = seaport.build_listing(b.minters[0].address, NFT, 7, 721, 10**17, FEES, seaport.OPENSEA_CONDUIT_KEY, 0)
        return {"chain": 4663, "contract": NFT, "token": "7", "std": 721, "wallet": b.minters[0].address.lower(), "price": str(10**17), "net": "1", "hash": "0xorder1", "components": comps, "managed": True}

    def test_cancel_sends_on_chain_cancel_and_tells_opensea(self):
        b = make_market()
        send = patch_chain(b)
        entry = self.entry(b)
        b.autolist[b.entry_key(entry)] = entry
        self.assertIn("1 listing", b.unlist_command(["all"]))
        with mock.patch.object(bot.threading, "Thread") as thread, mock.patch.object(b, "alert"):
            b.unlist_command(["confirm"])
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        self.assertTrue(send.call_args.args[3].startswith(seaport.CANCEL))
        self.assertEqual(b.opensea.cancelled, ["0xorder1"])
        self.assertEqual(b.autolist, {})

    def test_failed_cancel_keeps_the_entry(self):
        b = make_market()
        send = patch_chain(b)
        send.return_value = ("0xtx", False)
        entry = self.entry(b)
        b.autolist[b.entry_key(entry)] = entry
        ok, _detail = b.cancel_entry(entry)
        self.assertFalse(ok)
        self.assertEqual(b.opensea.cancelled, [])

    def test_nothing_to_unlist(self):
        b = make_market()
        self.assertIn("no active listing", b.unlist_command(["all"]))
        self.assertIn("Usage", b.unlist_command([]))


class SellAllTests(unittest.TestCase):
    def setup(self):
        b = make_market()
        patch_chain(b)
        me = b.minters[0].address.lower()
        b.holdings[f"4663:{NFT}:{me}"] = {"std": 721, "ids": ["1", "2", "3"]}
        return b

    def test_plan_uses_each_nfts_offer_and_skips_none(self):
        b = self.setup()
        offers = {"1": 10**15, "2": 2 * 10**15, "3": 0}
        b.opensea.best_offer = lambda slug, token: ({"hash": "0x", "price_wei": offers[token], "token": None} if offers[token] else None)
        text = b.sellall_command(["2x"])
        self.assertIn("List 2 NFT", text)
        self.assertIn("Skipped 1 with no offer", text)
        self.assertEqual(sorted(a["price"] for a in b.market_plan["actions"]), [2 * 10**15, 4 * 10**15])
        with mock.patch.object(bot.threading, "Thread") as thread, mock.patch.object(b, "alert"), mock.patch.object(market.time, "sleep"):
            b.sellall_command(["confirm"])
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        self.assertEqual(len(b.opensea.posted), 2)
        self.assertEqual(len(b.autolist), 2)

    def test_unknown_collection_and_nothing_held(self):
        b = self.setup()
        b.opensea.known = False
        self.assertIn("does not know", b.sellall_command(["2x"]))
        b = make_market()
        self.assertIn("Nothing to list", b.sellall_command(["2x"]))
        self.assertIn("price", b.sellall_command(["abc"]))

    def test_batch_is_capped(self):
        b = make_market()
        patch_chain(b)
        me = b.minters[0].address.lower()
        b.holdings[f"4663:{NFT}:{me}"] = {"std": 721, "ids": [str(i) for i in range(market.MAX_PLAN_ITEMS + 5)]}
        text = b.sellall_command(["0.01"])
        self.assertEqual(len(b.market_plan["actions"]), market.MAX_PLAN_ITEMS)
        self.assertIn("5 more", text)


class OfferTests(unittest.TestCase):
    def setup(self, held=ETH):
        b = make_market()
        b.detect_standard = lambda *a: 721
        wallet_calls = {market.BALANCE_OF_ERC20: held, seaport.ERC20_ALLOWANCE: 0}
        b.view_int = lambda chain, to, data: {**wallet_calls, seaport.GET_COUNTER: 0}.get(data[:10], 0)
        b.prepare_order = lambda chain, acct, comps: ("0x" + "ab" * 65, None)
        b.send_native_tx = mock.Mock(return_value=("0xtx", True))
        return b

    def test_preview_and_post(self):
        b = self.setup()
        text = b.offer_command([LINK, "0.002", "hours=12"])
        self.assertIn("/offer confirm", text)
        with mock.patch.object(bot.threading, "Thread") as thread, mock.patch.object(b, "alert"):
            b.offer_command(["confirm"])
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        chain, kind, params, _sig = b.opensea.posted[0]
        self.assertEqual(kind, "offers")
        self.assertEqual(params["offer"][0]["itemType"], seaport.ERC20)
        self.assertTrue(b.send_native_tx.call_args.args[3].startswith(seaport.ERC20_APPROVE))

    def test_limits_and_weth(self):
        b = self.setup()
        self.assertIn("above your limit", b.offer_command([LINK, "5"]))
        self.assertIn("price", b.offer_command([LINK]))
        self.assertIn("Usage", b.offer_command([]))

    def test_not_enough_wrapped_coin(self):
        b = self.setup(held=10)
        b.offer_command([LINK, "0.002"])
        plan = b.market_plan
        detail, ok = b.offer_one(4663, b.minters[0], plan)
        self.assertFalse(ok)
        self.assertIn("not enough", detail)
        self.assertEqual(b.opensea.posted, [])

    def test_weth_sources(self):
        b = make_bot()
        with mock.patch.dict(os.environ, {"WETH_4663": "0x" + "77" * 20}):
            self.assertEqual(b.weth_for(4663, "0x" + "e7" * 20), "0x" + "77" * 20)
        b = make_bot()
        self.assertEqual(b.weth_for(4663, "0x" + "e7" * 20), "0x" + "e7" * 20)
        self.assertIsNone(b.weth_for(4663))
        self.assertEqual(b.weth_for(1).lower(), market.WETH[1].lower())


class AutoListTests(unittest.TestCase):
    def setup(self, offer=10**15):
        b = make_market()
        b.opensea.offer = offer
        patch_chain(b)
        b.settings["autolist"] = True
        b.holdings[f"4663:{NFT}:{b.minters[0].address.lower()}"] = {"std": 721, "ids": ["7"]}
        return b

    def cycle(self, b):
        with mock.patch.object(market.time, "sleep"):
            return b.autolist_cycle()

    def test_off_by_default(self):
        b = self.setup()
        b.settings["autolist"] = False
        self.assertEqual(self.cycle(b), [])
        self.assertEqual(b.opensea.posted, [])

    def test_lists_at_double_the_top_offer(self):
        b = self.setup()
        lines = self.cycle(b)
        self.assertEqual(len(b.opensea.posted), 1)
        entry = next(iter(b.autolist.values()))
        self.assertEqual(int(entry["price"]), 2 * 10**15)
        self.assertIn("listed at", lines[0])

    def test_no_offer_no_listing(self):
        b = self.setup(offer=0)
        self.cycle(b)
        self.assertEqual(b.opensea.posted, [])

    def test_raises_when_offer_rises_by_the_step_and_never_lowers(self):
        b = self.setup()
        self.cycle(b)
        key = next(iter(b.autolist))
        # a small rise (under the 10% step) changes nothing
        b.opensea.offer = int(10**15 * 1.05)
        self.cycle(b)
        self.assertEqual(len(b.opensea.posted), 1)
        # a fall never reprices
        b.opensea.offer = 5 * 10**14
        self.cycle(b)
        self.assertEqual(len(b.opensea.posted), 1)
        self.assertEqual(int(b.autolist[key]["price"]), 2 * 10**15)
        # a real rise cancels the old listing on-chain, then lists higher
        b.opensea.offer = 2 * 10**15
        lines = self.cycle(b)
        self.assertEqual(len(b.opensea.posted), 2)
        self.assertEqual(int(b.autolist[key]["price"]), 4 * 10**15)
        self.assertTrue(any(c.args[3].startswith(seaport.CANCEL) for c in b.send_native_tx.call_args_list))
        self.assertIn("raised to", lines[0])

    def test_failed_cancel_blocks_the_new_listing(self):
        b = self.setup()
        self.cycle(b)
        b.opensea.offer = 3 * 10**15
        b.send_native_tx.return_value = ("0xtx", False)
        lines = self.cycle(b)
        self.assertEqual(len(b.opensea.posted), 1)
        self.assertIn("could not cancel", lines[0])

    def test_minimum_price_floor(self):
        b = self.setup()
        b.settings["autolist_minimum"] = "0.01"
        self.cycle(b)
        self.assertEqual(int(next(iter(b.autolist.values()))["price"]), 10**16)

    def test_nft_that_left_is_forgotten_and_counted_as_sold(self):
        b = self.setup()
        self.cycle(b)
        b.owns = lambda *a, **k: False
        lines = self.cycle(b)
        self.assertEqual(b.autolist, {})
        self.assertIn("left while listed", lines[0])
        sale = [e for e in b.ledger if e["kind"] == "sale"]
        self.assertEqual(len(sale), 1)
        self.assertGreater(sale[0]["wei"], 0)

    def test_collection_unknown_to_opensea_is_left_alone(self):
        b = self.setup()
        b.opensea.known = False
        self.cycle(b)
        self.assertEqual(b.opensea.posted, [])

    def test_unmanaged_listings_are_never_touched(self):
        b = self.setup()
        b.holdings.clear()
        b.autolist["4663:x:1"] = {"chain": 4663, "contract": NFT, "token": "7", "std": 721, "wallet": b.minters[0].address.lower(), "price": "5", "hash": "0xh", "managed": False}
        self.cycle(b)
        self.assertEqual(b.opensea.posted, [])


class PnlTests(unittest.TestCase):
    def test_pnl(self):
        b = make_market()
        me = b.minters[0].address
        self.assertIn("Nothing recorded", b.pnl_command([]))
        b.ledger_add("mint", 4663, me, NFT, 3 * 10**15)
        b.ledger_add("buy", 4663, me, NFT, 10**15, "7")
        b.ledger_add("sale", 4663, me, NFT, 10**16, "7")
        text = b.pnl_command([])
        self.assertIn("Minted: 0.0030 ETH", text)
        self.assertIn("Bought: 0.0010 ETH", text)
        self.assertIn("Net so far: +0.0060 ETH", text)

    def test_ledger_survives_restart(self):
        b = make_market()
        b.ledger_add("mint", 4663, b.minters[0].address, NFT, 5)
        with mock.patch.dict(os.environ, {"WATCH_ADDRESSES": "0x" + "ab" * 20, "MINTER_PRIVATE_KEYS": "0x" + "11" * 32, "CHAINS": "4663", "STATE_PATH": str(b.state_path)}, clear=True):
            self.assertEqual(len(bot.Bot().ledger), 1)


class PremintFilterTests(unittest.TestCase):
    def test_off_by_default_and_unknown_collections_pass(self):
        b = make_market()
        self.assertEqual(b.premint_blocked(4663, {NFT: 1}), "")
        b.settings["premint_min_sales"] = 10
        b.opensea.known = False
        self.assertEqual(b.premint_blocked(4663, {NFT: 1}), "")

    def test_blocks_thin_sales_and_passes_busy_ones(self):
        b = make_market()
        b.settings["premint_min_sales"] = 10
        b.opensea.sales = 3
        self.assertIn("3 sale", b.premint_blocked(4663, {NFT: 1}))
        b2 = make_market()
        b2.settings["premint_min_sales"] = 10
        b2.opensea.sales = 50
        self.assertEqual(b2.premint_blocked(4663, {NFT: 1}), "")

    def test_api_errors_never_block_a_mint(self):
        b = make_market()
        b.settings["premint_min_sales"] = 10

        def boom(slug):
            raise opensea.OpenSeaError("down", 500)

        b.opensea.collection_sales = boom
        self.assertEqual(b.premint_blocked(4663, {NFT: 1}), "")


class SweepFilterTests(unittest.TestCase):
    def test_parse_traits_and_rank(self):
        from test_sweep import COLLECTION, SweepOpenSea

        b = make_bot()
        b.opensea = SweepOpenSea([])
        with mock.patch.object(b, "balance", return_value=ETH):
            text = b.sweep_command([COLLECTION, "max=0.002", "trait=Background:Light_Blue", "trait=Eyes:Laser", "rank=500"])
        self.assertEqual(b.sweep_plan["traits"], {"background": "light blue", "eyes": "laser"})
        self.assertEqual(b.sweep_plan["rank"], 500)
        self.assertIn("Only with", text)
        self.assertIn("rank 500", text)
        self.assertIn("could not read trait", b.sweep_command([COLLECTION, "max=0.002", "trait=oops"]))
        self.assertIn("rank=", b.sweep_command([COLLECTION, "max=0.002", "rank=abc"]))

    def test_matching(self):
        b = make_bot()
        plan = {"traits": {"background": "blue"}, "rank": 100, "details": {}, "slug": "robinhood"}
        calls = []

        def details(slug, contract, token):
            calls.append(token)
            return {"traits": {"background": "blue"}, "rank": 50 if token == 1 else 500, "name": ""}

        b.opensea = mock.Mock()
        b.opensea.nft_details = details
        self.assertTrue(b.sweep_matches(plan, {"contract": NFT, "token_id": 1}))
        self.assertFalse(b.sweep_matches(plan, {"contract": NFT, "token_id": 2}))
        b.sweep_matches(plan, {"contract": NFT, "token_id": 1})
        self.assertEqual(calls, [1, 2])  # cached
        plan["traits"] = {"background": "red"}
        self.assertFalse(b.sweep_matches(plan, {"contract": NFT, "token_id": 1}))
        plan["traits"] = {}
        plan["rank"] = None
        self.assertTrue(b.sweep_matches(plan, {"contract": NFT, "token_id": 99}))

    def test_unknown_rank_fails_a_rank_filter(self):
        b = make_bot()
        plan = {"traits": {}, "rank": 100, "details": {}, "slug": "x"}
        b.opensea = mock.Mock()
        b.opensea.nft_details = lambda *a: {"traits": {}, "rank": None, "name": ""}
        self.assertFalse(b.sweep_matches(plan, {"contract": NFT, "token_id": 1}))


class OpenSeaClientTests(unittest.TestCase):
    def client(self, responses):
        c = opensea.OpenSea("k")
        seen = []

        def request(method, path, params=None, body=None, timeout=20):
            seen.append((method, path, body))
            value = responses.get(path)
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise opensea.OpenSeaError("not found", 404)
            return value

        c.request = request
        c.seen = seen
        return c

    def test_normalise_offer(self):
        shape = {"order_hash": "0xa", "price": {"current": {"currency": "WETH", "decimals": 18, "value": "500"}}, "protocol_data": {"parameters": {"offer": [{"itemType": 1, "token": "0xweth"}]}}}
        self.assertEqual(opensea.normalise_offer(shape), {"hash": "0xa", "price_wei": 500, "token": "0xweth"})
        self.assertEqual(opensea.normalise_offer({"offers": [shape]})["price_wei"], 500)
        self.assertIsNone(opensea.normalise_offer({"order_hash": "0xa", "price": {"current": {"decimals": 6, "value": "5"}}}))
        self.assertIsNone(opensea.normalise_offer({"price": {}}))

    def test_best_offer_falls_back_to_collection_offers(self):
        row = lambda h, v: {"order_hash": h, "price": {"current": {"decimals": 18, "value": str(v)}}}
        c = self.client({"/api/v2/offers/collection/robo": {"offers": [row("0x1", 5), row("0x2", 9), row("0x3", 7)]}})
        self.assertEqual(c.best_offer("robo", 7)["price_wei"], 9)

    def test_best_offer_prefers_the_nft_endpoint_and_surfaces_real_errors(self):
        row = {"order_hash": "0x1", "price": {"current": {"decimals": 18, "value": "42"}}}
        c = self.client({"/api/v2/offers/collection/robo/nfts/7/best": row})
        self.assertEqual(c.best_offer("robo", 7)["price_wei"], 42)
        c = self.client({"/api/v2/offers/collection/robo/nfts/7/best": opensea.OpenSeaError("down", 500), "/api/v2/offers/collection/robo": {"offers": []}})
        with self.assertRaises(opensea.OpenSeaError):
            c.best_offer("robo", 7)
        self.assertIsNone(self.client({}).best_offer("robo", 7))

    def test_collection_info_nft_details_and_contract(self):
        c = self.client(
            {
                "/api/v2/collections/robo": {"name": "Robo", "fees": [{"fee": 1.0, "recipient": CREATOR, "required": True}, {"fee": "x"}]},
                f"/api/v2/chain/robinhood/contract/{NFT}/nfts/7": {"nft": {"name": "Seven", "traits": [{"trait_type": "Background", "value": "Blue"}], "rarity": {"rank": 12}}},
                f"/api/v2/chain/robinhood/contract/{NFT}": {"collection": "robo", "name": "Robo"},
                "/api/v2/collections/robo/stats": {"total": {"sales": 31}},
            }
        )
        info = c.collection_info("robo")
        self.assertEqual(len(info["fees"]), 1)
        self.assertEqual(c.nft_details("robinhood", NFT, 7), {"traits": {"background": "blue"}, "rank": 12, "name": "Seven"})
        self.assertEqual(c.contract_collection("robinhood", NFT), ("robo", "Robo"))
        self.assertEqual(c.collection_sales("robo"), 31)
        self.assertEqual(self.client({}).contract_collection("robinhood", NFT), (None, None))

    def test_post_and_cancel_paths(self):
        c = self.client({"/api/v2/orders/robinhood/seaport/listings": {"order_hash": "0xabc"}, f"/api/v2/orders/chain/robinhood/protocol/{seaport.SEAPORT}/0xabc/cancel": {}})
        self.assertEqual(c.post_order("robinhood", "listings", {"a": 1}, "0xsig"), "0xabc")
        method, path, body = c.seen[0]
        self.assertEqual((method, body["signature"], body["protocol_address"]), ("POST", "0xsig", opensea.SEAPORT_16))
        c.cancel_order("robinhood", "0xabc")
        self.assertTrue(c.seen[1][1].endswith("/0xabc/cancel"))


class StreamTests(unittest.TestCase):
    def test_event_slug_tolerates_layouts(self):
        s = opensea.Stream
        self.assertEqual(s.event_slug({"topic": "collection:robo", "event": "item_listed", "payload": {}}), ("item_listed", "robo"))
        self.assertEqual(s.event_slug({"topic": "collection:robo", "event": "x", "payload": {"event_type": "item_sold"}}), ("item_sold", "robo"))
        self.assertEqual(s.event_slug({"topic": "phoenix", "event": "phx_reply"}), ("phx_reply", None))
        self.assertEqual(s.event_slug("junk"), (None, None))

    def test_sweep_waits_on_the_feed_only_when_live(self):
        b = make_bot()
        b.sweep_poll = 0.01
        b.stream = opensea.Stream("k")
        b.stream.live = False
        b.sweep_wait()  # polls, returns quickly
        b.stream.live = True
        b.stream.changed.set()
        b.sweep_wait()
        self.assertFalse(b.stream.changed.is_set())
        b.settings["stream"] = False
        b.sweep_wait()


if __name__ == "__main__":
    unittest.main()
