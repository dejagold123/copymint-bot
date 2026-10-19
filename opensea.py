"""OpenSea API v2 client and Seaport call data for /buy and /sweep.

Nothing here sends a transaction. It reads listings, asks OpenSea for the
fulfilment data for a buyer, and turns that into the call data the bot then
simulates and sends itself.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

API = "https://api.opensea.io"

# OpenSea's chain slug (as in item links) -> chain ID.
SLUG_TO_CHAIN = {
    "ethereum": 1,
    "optimism": 10,
    "bsc": 56,
    "matic": 137,
    "polygon": 137,
    "base": 8453,
    "arbitrum": 42161,
    "hyperevm": 999,
    "robinhood": 4663,
}
CHAIN_TO_SLUG = {1: "ethereum", 10: "optimism", 56: "bsc", 137: "matic", 8453: "base", 42161: "arbitrum", 999: "hyperevm", 4663: "robinhood"}

SEAPORT_16 = "0x0000000000000068F116a894984e2DB1123eB395"

OFFER_ITEM = "(uint8,address,uint256,uint256,uint256)"
CONSIDERATION_ITEM = "(uint8,address,uint256,uint256,uint256,address)"
ORDER_PARAMETERS = f"(address,address,{OFFER_ITEM}[],{CONSIDERATION_ITEM}[],uint8,uint256,uint256,bytes32,uint256,bytes32,uint256)"
ADVANCED_ORDER = f"({ORDER_PARAMETERS},uint120,uint120,bytes,bytes)"
CRITERIA_RESOLVER = "(uint256,uint8,uint256,uint256,bytes32[])"
BASIC_ORDER = (
    "(address,uint256,uint256,address,address,address,uint256,uint256,uint8,uint256,uint256,"
    "bytes32,uint256,bytes32,bytes32,uint256,(uint256,address)[],bytes)"
)

ADVANCED_SIGNATURE = f"fulfillAdvancedOrder({ADVANCED_ORDER},{CRITERIA_RESOLVER}[],bytes32,address)"
BASIC_SIGNATURES = {
    "fulfillBasicOrder": f"fulfillBasicOrder({BASIC_ORDER})",
    "fulfillBasicOrder_efficient_6GL6yc": f"fulfillBasicOrder_efficient_6GL6yc({BASIC_ORDER})",
}
ZERO32 = "0x" + "00" * 32
ZERO_ADDRESS = "0x" + "00" * 20


class OpenSeaError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def selector(signature):
    return "0x" + keccak(text=signature).hex()[:8]


def parse_item_link(text):
    """(chain slug, contract, token id) from an OpenSea item link, or None."""
    match = re.search(
        r"opensea\.io/(?:[a-z]{2}/)?(?:item|assets)/([a-z0-9_-]+)/(0x[0-9a-fA-F]{40})/(\d+)",
        text or "",
    )
    if not match:
        return None
    return match.group(1).lower(), match.group(2).lower(), int(match.group(3))


def parse_collection_link(text):
    """The collection slug from an OpenSea collection link, or None."""
    match = re.search(r"opensea\.io/(?:[a-z]{2}/)?collection/([a-z0-9_-]+)", text or "", re.IGNORECASE)
    return match.group(1).lower() if match else None


def to_int(value):
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    raise ValueError(f"not a number: {value!r}")


def to_bytes(value):
    text = (value or "0x")
    text = text[2:] if text.startswith("0x") else text
    return bytes.fromhex(text)


def to_bytes32(value):
    raw = to_bytes(value)
    return raw.rjust(32, b"\x00")


def addr(value):
    return to_checksum_address(value)


def _offer_item(item):
    return (
        to_int(item["itemType"]),
        addr(item["token"]),
        to_int(item["identifierOrCriteria"]),
        to_int(item["startAmount"]),
        to_int(item["endAmount"]),
    )


def _consideration_item(item):
    return _offer_item(item) + (addr(item["recipient"]),)


def _order_parameters(p):
    consideration = p["consideration"]
    return (
        addr(p["offerer"]),
        addr(p["zone"]),
        [_offer_item(i) for i in p["offer"]],
        [_consideration_item(i) for i in consideration],
        to_int(p["orderType"]),
        to_int(p["startTime"]),
        to_int(p["endTime"]),
        to_bytes32(p["zoneHash"]),
        to_int(p["salt"]),
        to_bytes32(p["conduitKey"]),
        to_int(p.get("totalOriginalConsiderationItems", len(consideration))),
    )


def _advanced_order(order):
    params = order["parameters"]
    return (
        _order_parameters(params),
        to_int(order.get("numerator", 1)),
        to_int(order.get("denominator", 1)),
        to_bytes(order.get("signature")),
        to_bytes(order.get("extraData")),
    )


def _criteria_resolver(item):
    return (
        to_int(item["orderIndex"]),
        to_int(item["side"]),
        to_int(item["index"]),
        to_int(item["identifier"]),
        [to_bytes32(p) for p in item.get("criteriaProof") or []],
    )


def build_calldata(function, input_data):
    """Call data for the Seaport function OpenSea named, built from its input_data.

    Supports fulfillAdvancedOrder and the two basic-order functions, which cover
    ordinary fixed-price listings. Anything else raises OpenSeaError.
    """
    name = str(function or "").split("(")[0].strip()
    if not isinstance(input_data, dict):
        raise OpenSeaError("OpenSea returned no call data to build from")
    try:
        if name == "fulfillAdvancedOrder":
            order = input_data.get("advancedOrder") or (input_data.get("orders") or [None])[0]
            if not isinstance(order, dict):
                raise OpenSeaError("OpenSea's fulfilment data has no order to build from")
            body = encode(
                [ADVANCED_ORDER, f"{CRITERIA_RESOLVER}[]", "bytes32", "address"],
                [
                    _advanced_order(order),
                    [_criteria_resolver(r) for r in input_data.get("criteriaResolvers") or []],
                    to_bytes32(input_data.get("fulfillerConduitKey") or ZERO32),
                    addr(input_data.get("recipient") or ZERO_ADDRESS),
                ],
            )
            return selector(ADVANCED_SIGNATURE) + body.hex()
        if name in BASIC_SIGNATURES:
            p = input_data.get("parameters") or input_data.get("basicOrderParameters")
            if not isinstance(p, dict):
                raise OpenSeaError("OpenSea's fulfilment data has no basic-order parameters")
            extra = p.get("additionalRecipients") or []
            body = encode(
                [BASIC_ORDER],
                [
                    (
                        addr(p["considerationToken"]),
                        to_int(p["considerationIdentifier"]),
                        to_int(p["considerationAmount"]),
                        addr(p["offerer"]),
                        addr(p["zone"]),
                        addr(p["offerToken"]),
                        to_int(p["offerIdentifier"]),
                        to_int(p["offerAmount"]),
                        to_int(p["basicOrderType"]),
                        to_int(p["startTime"]),
                        to_int(p["endTime"]),
                        to_bytes32(p["zoneHash"]),
                        to_int(p["salt"]),
                        to_bytes32(p["offererConduitKey"]),
                        to_bytes32(p["fulfillerConduitKey"]),
                        to_int(p.get("totalOriginalAdditionalRecipients", len(extra))),
                        [(to_int(r["amount"]), addr(r["recipient"])) for r in extra],
                        to_bytes(p.get("signature")),
                    )
                ],
            )
            return selector(BASIC_SIGNATURES[name]) + body.hex()
    except OpenSeaError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise OpenSeaError(f"could not read OpenSea's fulfilment data ({type(exc).__name__}: {exc})") from exc
    raise OpenSeaError(f"OpenSea chose a purchase function this bot does not build yet ({name or 'unknown'})")


def native_listing(protocol_data):
    """True when every price item of the order is the chain's native coin (item type 0)."""
    try:
        items = protocol_data["parameters"]["consideration"]
        return bool(items) and all(to_int(i["itemType"]) == 0 for i in items)
    except (KeyError, TypeError, ValueError):
        return None


def normalise_listing(raw):
    """One listing in either response shape, as {hash, protocol_address, price_wei, native, quantity}."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("listings"), list) and raw["listings"]:
        return normalise_listing(raw["listings"][0])
    if isinstance(raw.get("orders"), list) and raw["orders"]:
        return normalise_listing(raw["orders"][0])
    order_hash = raw.get("order_hash") or raw.get("orderHash") or raw.get("hash")
    if not order_hash:
        return None
    protocol_data = raw.get("protocol_data") or {}
    price = None
    native = native_listing(protocol_data)
    current = (raw.get("price") or {}).get("current") if isinstance(raw.get("price"), dict) else None
    try:
        if isinstance(current, dict):
            price = to_int(current.get("value"))
            decimals = to_int(current.get("decimals", 18))
            if decimals != 18:
                native = False
            symbol = str(current.get("currency") or "").upper()
            if native is None:
                native = symbol in ("ETH", "WETH-NATIVE", "")
        elif raw.get("current_price") is not None:
            price = to_int(raw["current_price"])
    except ValueError:
        price = None
    if price is None:
        return None
    remaining = raw.get("remaining_quantity")
    try:
        quantity = to_int(remaining) if remaining is not None else 1
    except ValueError:
        quantity = 1
    return {
        "hash": str(order_hash),
        "protocol_address": raw.get("protocol_address") or SEAPORT_16,
        "price_wei": price,
        "native": native,
        "quantity": quantity,
    }


class OpenSea:
    def __init__(self, key):
        self.key = key

    def request(self, method, path, params=None, body=None, timeout=20):
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "x-api-key": self.key,
                "user-agent": "copymint-bot",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            text = exc.read().decode(errors="replace")[:300]
            raise OpenSeaError(f"OpenSea {exc.code}: {text}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise OpenSeaError(f"could not reach OpenSea ({type(exc).__name__})") from exc

    def collection_slug(self, chain_slug, contract, token_id):
        data = self.request("GET", f"/api/v2/chain/{chain_slug}/contract/{contract}/nfts/{token_id}")
        nft = data.get("nft") if isinstance(data, dict) else None
        slug = (nft or {}).get("collection")
        if isinstance(slug, dict):
            slug = slug.get("slug")
        return slug or None, (nft or {}).get("name")

    def best_listing(self, chain_slug, collection, contract, token_id):
        """The cheapest active listing of one NFT, or None when it is not for sale."""
        errors = []
        if collection:
            try:
                data = self.request("GET", f"/api/v2/listings/collection/{collection}/nfts/{token_id}/best")
                found = normalise_listing(data)
                if found:
                    return found
            except OpenSeaError as exc:
                errors.append(exc)
        try:
            data = self.request(
                "GET",
                f"/api/v2/orders/{chain_slug}/seaport/listings",
                params={
                    "asset_contract_address": contract,
                    "token_ids": str(token_id),
                    "order_by": "eth_price",
                    "order_direction": "asc",
                    "limit": 1,
                },
            )
            return normalise_listing(data)
        except OpenSeaError as exc:
            errors.append(exc)
        real = [e for e in errors if e.status not in (404,)]
        if real:
            raise real[0]
        return None

    def collection_listings(self, collection, limit=50):
        """Cheapest-first active listings of a collection, normalised, each with the token it sells when known."""
        found, cursor = [], None
        while len(found) < limit:
            params = {"limit": min(100, limit)}
            if cursor:
                params["next"] = cursor
            data = self.request("GET", f"/api/v2/listings/collection/{collection}/best", params=params)
            rows = data.get("listings") if isinstance(data, dict) else None
            for row in rows or []:
                one = normalise_listing(row)
                if not one:
                    continue
                one["token_id"], one["contract"] = listing_token(row)
                found.append(one)
            cursor = data.get("next") if isinstance(data, dict) else None
            if not cursor or not rows:
                break
        return found

    def fulfilment(self, listing, chain_slug, buyer):
        """(to, value, call data, function name) to buy the listing as buyer."""
        data = self.request(
            "POST",
            "/api/v2/listings/fulfillment_data",
            body={
                "listing": {"hash": listing["hash"], "chain": chain_slug, "protocol_address": listing["protocol_address"]},
                "fulfiller": {"address": buyer},
            },
        )
        fulfilment = data.get("fulfillment_data") if isinstance(data, dict) else None
        tx = (fulfilment or {}).get("transaction")
        if not isinstance(tx, dict):
            raise OpenSeaError("OpenSea returned no transaction for this listing")
        function = tx.get("function") or ""
        calldata = build_calldata(function, tx.get("input_data"))
        try:
            return addr(tx["to"]), to_int(tx.get("value") or 0), calldata, function
        except (KeyError, ValueError) as exc:
            raise OpenSeaError("OpenSea's transaction had no usable target or value") from exc


def listing_token(row):
    """(token id, contract) a listing sells, read from its order's offer, or (None, None)."""
    try:
        offer = row["protocol_data"]["parameters"]["offer"]
        if len(offer) == 1 and to_int(offer[0]["itemType"]) in (2, 3):
            return to_int(offer[0]["identifierOrCriteria"]), offer[0]["token"].lower()
    except (KeyError, TypeError, ValueError):
        pass
    return None, None
