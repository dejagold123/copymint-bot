"""Seaport orders: build, hash and sign listings and offers, plus the small calls around them.

The hash code is written by hand and is checked two ways: the tests compare it with eth_account's
independent EIP-712 encoder, and the bot compares it with the Seaport contract itself (getOrderHash,
information) before it posts anything.
"""

from __future__ import annotations

import secrets
import time

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

SEAPORT = "0x0000000000000068F116a894984e2DB1123eB395"
CONDUIT_CONTROLLER = "0x00000000F9490004C11Cef243f5400493c00Ad63"
# OpenSea's conduit key. The bot confirms on-chain that this conduit exists, and against OpenSea's own live listings, before using it.
OPENSEA_CONDUIT_KEY = "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000"
ZERO32 = "0x" + "00" * 32
ZERO_ADDRESS = "0x" + "00" * 20

NATIVE, ERC20, ERC721, ERC1155 = 0, 1, 2, 3
FULL_OPEN = 0

OFFER_ITEM_TYPE = "OfferItem(uint8 itemType,address token,uint256 identifierOrCriteria,uint256 startAmount,uint256 endAmount)"
CONSIDERATION_ITEM_TYPE = "ConsiderationItem(uint8 itemType,address token,uint256 identifierOrCriteria,uint256 startAmount,uint256 endAmount,address recipient)"
ORDER_TYPE = (
    "OrderComponents(address offerer,address zone,OfferItem[] offer,ConsiderationItem[] consideration,uint8 orderType,"
    "uint256 startTime,uint256 endTime,bytes32 zoneHash,uint256 salt,bytes32 conduitKey,uint256 counter)"
    + CONSIDERATION_ITEM_TYPE
    + OFFER_ITEM_TYPE
)
ORDER_TYPEHASH = keccak(text=ORDER_TYPE)
OFFER_TYPEHASH = keccak(text=OFFER_ITEM_TYPE)
CONSIDERATION_TYPEHASH = keccak(text=CONSIDERATION_ITEM_TYPE)
DOMAIN_TYPEHASH = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")


def selector(signature):
    return "0x" + keccak(text=signature).hex()[:8]


GET_COUNTER = selector("getCounter(address)")
INFORMATION = selector("information()")
GET_CONDUIT = selector("getConduit(bytes32)")
IS_APPROVED_FOR_ALL = selector("isApprovedForAll(address,address)")
SET_APPROVAL_FOR_ALL = selector("setApprovalForAll(address,bool)")
ERC20_ALLOWANCE = selector("allowance(address,address)")
ERC20_APPROVE = selector("approve(address,uint256)")
ORDER_COMPONENTS_TUPLE = (
    "(address,address,(uint8,address,uint256,uint256,uint256)[],(uint8,address,uint256,uint256,uint256,address)[],"
    "uint8,uint256,uint256,bytes32,uint256,bytes32,uint256)"
)
GET_ORDER_HASH = selector(f"getOrderHash({ORDER_COMPONENTS_TUPLE})")
CANCEL = selector(f"cancel({ORDER_COMPONENTS_TUPLE}[])")
INCREMENT_COUNTER = selector("incrementCounter()")


def b32(value):
    raw = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    return raw.rjust(32, b"\x00")


def checksum(value):
    return to_checksum_address(value)


def domain_separator(chain_id, verifying=SEAPORT, version="1.6"):
    return keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [DOMAIN_TYPEHASH, keccak(text="Seaport"), keccak(text=version), chain_id, checksum(verifying)],
        )
    )


def _offer_hash(item):
    return keccak(
        encode(
            ["bytes32", "uint8", "address", "uint256", "uint256", "uint256"],
            [OFFER_TYPEHASH, item["itemType"], checksum(item["token"]), int(item["identifierOrCriteria"]), int(item["startAmount"]), int(item["endAmount"])],
        )
    )


def _consideration_hash(item):
    return keccak(
        encode(
            ["bytes32", "uint8", "address", "uint256", "uint256", "uint256", "address"],
            [
                CONSIDERATION_TYPEHASH,
                item["itemType"],
                checksum(item["token"]),
                int(item["identifierOrCriteria"]),
                int(item["startAmount"]),
                int(item["endAmount"]),
                checksum(item["recipient"]),
            ],
        )
    )


def order_hash(c):
    """EIP-712 struct hash of OrderComponents. This is the order hash Seaport reports."""
    offers = keccak(b"".join(_offer_hash(i) for i in c["offer"]))
    considerations = keccak(b"".join(_consideration_hash(i) for i in c["consideration"]))
    return keccak(
        encode(
            ["bytes32", "address", "address", "bytes32", "bytes32", "uint8", "uint256", "uint256", "bytes32", "uint256", "bytes32", "uint256"],
            [
                ORDER_TYPEHASH,
                checksum(c["offerer"]),
                checksum(c["zone"]),
                offers,
                considerations,
                int(c["orderType"]),
                int(c["startTime"]),
                int(c["endTime"]),
                b32(c["zoneHash"]),
                int(c["salt"]),
                b32(c["conduitKey"]),
                int(c["counter"]),
            ],
        )
    )


def digest(c, chain_id, verifying=SEAPORT, version="1.6"):
    return keccak(b"\x19\x01" + domain_separator(chain_id, verifying, version) + order_hash(c))


def sign(c, key, chain_id, verifying=SEAPORT, version="1.6"):
    """0x signature of the order by the account key."""
    signed = Account.unsafe_sign_hash(digest(c, chain_id, verifying, version), key) if hasattr(Account, "unsafe_sign_hash") else Account._sign_hash(digest(c, chain_id, verifying, version), key)
    return "0x" + signed.signature.hex().removeprefix("0x")


def typed_data(c, chain_id, verifying=SEAPORT, version="1.6"):
    """The same order as an EIP-712 payload, for cross-checking with an independent encoder."""
    item = [
        {"name": "itemType", "type": "uint8"},
        {"name": "token", "type": "address"},
        {"name": "identifierOrCriteria", "type": "uint256"},
        {"name": "startAmount", "type": "uint256"},
        {"name": "endAmount", "type": "uint256"},
    ]
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "OrderComponents": [
                {"name": "offerer", "type": "address"},
                {"name": "zone", "type": "address"},
                {"name": "offer", "type": "OfferItem[]"},
                {"name": "consideration", "type": "ConsiderationItem[]"},
                {"name": "orderType", "type": "uint8"},
                {"name": "startTime", "type": "uint256"},
                {"name": "endTime", "type": "uint256"},
                {"name": "zoneHash", "type": "bytes32"},
                {"name": "salt", "type": "uint256"},
                {"name": "conduitKey", "type": "bytes32"},
                {"name": "counter", "type": "uint256"},
            ],
            "OfferItem": item,
            "ConsiderationItem": item + [{"name": "recipient", "type": "address"}],
        },
        "primaryType": "OrderComponents",
        "domain": {"name": "Seaport", "version": version, "chainId": chain_id, "verifyingContract": checksum(verifying)},
        "message": {
            "offerer": checksum(c["offerer"]),
            "zone": checksum(c["zone"]),
            "offer": [{**i, "token": checksum(i["token"]), "identifierOrCriteria": int(i["identifierOrCriteria"]), "startAmount": int(i["startAmount"]), "endAmount": int(i["endAmount"])} for i in c["offer"]],
            "consideration": [
                {**i, "token": checksum(i["token"]), "recipient": checksum(i["recipient"]), "identifierOrCriteria": int(i["identifierOrCriteria"]), "startAmount": int(i["startAmount"]), "endAmount": int(i["endAmount"])}
                for i in c["consideration"]
            ],
            "orderType": int(c["orderType"]),
            "startTime": int(c["startTime"]),
            "endTime": int(c["endTime"]),
            "zoneHash": b32(c["zoneHash"]),
            "salt": int(c["salt"]),
            "conduitKey": b32(c["conduitKey"]),
            "counter": int(c["counter"]),
        },
    }


def split_fees(total, fees):
    """Fee amounts for a total price. fees is [(recipient, percent)], percent like 1.0 or 2.5. Returns [(recipient, wei)]."""
    out = []
    for recipient, percent in fees:
        bps = int(round(float(percent) * 100))
        amount = total * bps // 10_000
        if amount > 0:
            out.append((recipient, amount))
    return out


def new_salt():
    return secrets.randbits(128)


def build_listing(offerer, contract, token_id, std, price_wei, fees, conduit_key, counter, days=30, now=None, quantity=1):
    """A fixed-price listing paid in the chain's native coin. The seller receives the price minus every fee."""
    now = int(now if now is not None else time.time())
    fee_parts = split_fees(price_wei, fees)
    proceeds = price_wei - sum(a for _r, a in fee_parts)
    if proceeds <= 0:
        raise ValueError("fees take the whole price")
    item_type = ERC721 if int(std) == 721 else ERC1155
    consideration = [{"itemType": NATIVE, "token": ZERO_ADDRESS, "identifierOrCriteria": "0", "startAmount": str(proceeds), "endAmount": str(proceeds), "recipient": checksum(offerer)}]
    for recipient, amount in fee_parts:
        consideration.append({"itemType": NATIVE, "token": ZERO_ADDRESS, "identifierOrCriteria": "0", "startAmount": str(amount), "endAmount": str(amount), "recipient": checksum(recipient)})
    return {
        "offerer": checksum(offerer),
        "zone": ZERO_ADDRESS,
        "offer": [{"itemType": item_type, "token": checksum(contract), "identifierOrCriteria": str(int(token_id)), "startAmount": str(quantity), "endAmount": str(quantity)}],
        "consideration": consideration,
        "orderType": FULL_OPEN,
        "startTime": str(now - 60),
        "endTime": str(now + days * 86400),
        "zoneHash": ZERO32,
        "salt": str(new_salt()),
        "conduitKey": conduit_key,
        "counter": str(int(counter)),
    }


def build_offer(offerer, contract, token_id, std, price_wei, weth, fees, conduit_key, counter, hours=24, now=None):
    """An offer on one NFT, paid in WETH. The offerer pays the price; the seller and fee recipients are the consideration."""
    now = int(now if now is not None else time.time())
    fee_parts = split_fees(price_wei, fees)
    proceeds = price_wei - sum(a for _r, a in fee_parts)
    if proceeds <= 0:
        raise ValueError("fees take the whole price")
    item_type = ERC721 if int(std) == 721 else ERC1155
    consideration = [{"itemType": item_type, "token": checksum(contract), "identifierOrCriteria": str(int(token_id)), "startAmount": "1", "endAmount": "1", "recipient": checksum(offerer)}]
    # The seller's own proceeds are filled in by whoever accepts, so the offer lists the fee recipients only here.
    for recipient, amount in fee_parts:
        consideration.append({"itemType": ERC20, "token": checksum(weth), "identifierOrCriteria": "0", "startAmount": str(amount), "endAmount": str(amount), "recipient": checksum(recipient)})
    return {
        "offerer": checksum(offerer),
        "zone": ZERO_ADDRESS,
        "offer": [{"itemType": ERC20, "token": checksum(weth), "identifierOrCriteria": "0", "startAmount": str(price_wei), "endAmount": str(price_wei)}],
        "consideration": consideration,
        "orderType": FULL_OPEN,
        "startTime": str(now - 60),
        "endTime": str(now + hours * 3600),
        "zoneHash": ZERO32,
        "salt": str(new_salt()),
        "conduitKey": conduit_key,
        "counter": str(int(counter)),
    }


def api_parameters(c):
    """The order in the shape OpenSea's POST endpoints take: components plus totalOriginalConsiderationItems."""
    body = {k: v for k, v in c.items()}
    body["totalOriginalConsiderationItems"] = len(c["consideration"])
    return body


def components_tuple(c):
    return (
        checksum(c["offerer"]),
        checksum(c["zone"]),
        [(i["itemType"], checksum(i["token"]), int(i["identifierOrCriteria"]), int(i["startAmount"]), int(i["endAmount"])) for i in c["offer"]],
        [(i["itemType"], checksum(i["token"]), int(i["identifierOrCriteria"]), int(i["startAmount"]), int(i["endAmount"]), checksum(i["recipient"])) for i in c["consideration"]],
        int(c["orderType"]),
        int(c["startTime"]),
        int(c["endTime"]),
        b32(c["zoneHash"]),
        int(c["salt"]),
        b32(c["conduitKey"]),
        int(c["counter"]),
    )


def get_order_hash_call(c):
    return GET_ORDER_HASH + encode([ORDER_COMPONENTS_TUPLE], [components_tuple(c)]).hex()


def cancel_call(components_list):
    return CANCEL + encode([ORDER_COMPONENTS_TUPLE + "[]"], [[components_tuple(c) for c in components_list]]).hex()


def pad_address(value):
    return "0" * 24 + value[2:].lower()


def get_counter_call(offerer):
    return GET_COUNTER + pad_address(offerer)


def conduit_call(key):
    return GET_CONDUIT + b32(key).hex()


def decode_conduit(raw):
    """(address, exists) from ConduitController.getConduit."""
    address, exists = decode(["address", "bool"], bytes.fromhex(raw[2:]))
    return address, exists


def decode_information(raw):
    """(version, domain separator, conduit controller) from Seaport.information()."""
    version, separator, controller = decode(["string", "bytes32", "address"], bytes.fromhex(raw[2:]))
    return version, "0x" + separator.hex(), controller


def approval_call(operator):
    return SET_APPROVAL_FOR_ALL + pad_address(operator) + "0" * 63 + "1"


def approved_call(owner, operator):
    return IS_APPROVED_FOR_ALL + pad_address(owner) + pad_address(operator)


def erc20_approve_call(spender, amount=2**256 - 1):
    return ERC20_APPROVE + pad_address(spender) + f"{amount:064x}"


def erc20_allowance_call(owner, spender):
    return ERC20_ALLOWANCE + pad_address(owner) + pad_address(spender)
