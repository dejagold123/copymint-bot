import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from eth_utils import keccak  # noqa: E402

import seaport  # noqa: E402

KEY = "0x" + "11" * 32
ME = Account.from_key(KEY).address
NFT = "0x" + "c1" * 20
WETH = "0x" + "e7" * 20
FEE_OPENSEA = "0x0000a26b00c1F0DF003000390027140000fAa719"
CREATOR = "0x" + "cc" * 20
FEES = [(FEE_OPENSEA, 1.0), (CREATOR, 2.5)]


def listing():
    return seaport.build_listing(ME, NFT, 7, 721, 10**18, FEES, seaport.OPENSEA_CONDUIT_KEY, 3, now=1_800_000_000)


def offer():
    return seaport.build_offer(ME, NFT, 7, 721, 10**18, WETH, FEES, seaport.OPENSEA_CONDUIT_KEY, 0, now=1_800_000_000)


def independent_digest(components, chain_id):
    signable = encode_typed_data(full_message=seaport.typed_data(components, chain_id))
    return keccak(b"\x19" + signable.version + signable.header + signable.body)


class SeaportTests(unittest.TestCase):
    def test_typehash_and_selectors_match_seaport(self):
        self.assertEqual("0x" + seaport.ORDER_TYPEHASH.hex(), "0xfa445660b7e21515a59617fcd68910b487aa5808b8abda3d78bc85df364b2c2f")
        self.assertEqual(seaport.GET_ORDER_HASH, "0x79df72bd")
        self.assertEqual(seaport.CANCEL, "0xfd9f1e10")
        self.assertEqual(seaport.INCREMENT_COUNTER, "0x5b34b966")
        self.assertEqual(seaport.INFORMATION, "0xf47b7740")
        self.assertEqual(seaport.GET_COUNTER, "0xf07ec373")

    def test_digest_matches_an_independent_eip712_encoder(self):
        for order in (listing(), offer()):
            for chain_id in (1, 4663):
                self.assertEqual(seaport.digest(order, chain_id), independent_digest(order, chain_id))

    def test_signature_recovers_the_offerer(self):
        order = listing()
        signature = seaport.sign(order, KEY, 4663)
        self.assertEqual(len(signature), 132)
        recovered = Account._recover_hash(seaport.digest(order, 4663), signature=signature)
        self.assertEqual(recovered, ME)

    def test_listing_pays_the_seller_the_price_minus_fees(self):
        order = listing()
        amounts = [int(i["startAmount"]) for i in order["consideration"]]
        self.assertEqual(sum(amounts), 10**18)
        self.assertEqual(amounts[1], 10**16)
        self.assertEqual(amounts[2], 25 * 10**15)
        self.assertEqual(order["consideration"][0]["recipient"], ME)
        self.assertEqual(order["offer"][0]["itemType"], seaport.ERC721)
        self.assertEqual(seaport.api_parameters(order)["totalOriginalConsiderationItems"], 3)

    def test_offer_is_paid_in_weth(self):
        order = offer()
        self.assertEqual(order["offer"][0]["itemType"], seaport.ERC20)
        self.assertEqual(order["offer"][0]["token"].lower(), WETH)
        self.assertEqual(order["consideration"][0]["itemType"], seaport.ERC721)

    def test_fees_cannot_eat_the_price(self):
        with self.assertRaises(ValueError):
            seaport.build_listing(ME, NFT, 7, 721, 100, [(CREATOR, 100.0)], seaport.OPENSEA_CONDUIT_KEY, 0)

    def test_1155_listing_item_type(self):
        order = seaport.build_listing(ME, NFT, 7, 1155, 10**18, FEES, seaport.OPENSEA_CONDUIT_KEY, 0)
        self.assertEqual(order["offer"][0]["itemType"], seaport.ERC1155)

    def test_call_data_shapes(self):
        order = listing()
        self.assertTrue(seaport.get_order_hash_call(order).startswith("0x79df72bd"))
        self.assertTrue(seaport.cancel_call([order]).startswith("0xfd9f1e10"))
        self.assertEqual(len(seaport.approval_call(NFT)), 10 + 128)
        self.assertEqual(len(seaport.get_counter_call(ME)), 10 + 64)

    def test_decoders(self):
        from eth_abi import encode

        raw = "0x" + encode(["address", "bool"], [NFT, True]).hex()
        self.assertEqual(seaport.decode_conduit(raw), (NFT, True))
        sep = seaport.domain_separator(1)
        info = "0x" + encode(["string", "bytes32", "address"], ["1.6", sep, seaport.CONDUIT_CONTROLLER]).hex()
        self.assertEqual(seaport.decode_information(info)[1], "0x" + sep.hex())


if __name__ == "__main__":
    unittest.main()
