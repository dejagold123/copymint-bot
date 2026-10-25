"""Which OpenSea drop phase a minter is actually on, and the mint OpenSea builds for that wallet.

The public sale time is not used when the wallet is on an earlier allowlist.
Nothing here broadcasts a transaction.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address

SITE = "https://opensea.io"
GQL = "https://gql.opensea.io/graphql"
SIWE_STATEMENT = (
    "Click to sign in and accept the OpenSea Terms of Service "
    "(https://opensea.io/tos) and Privacy Policy (https://opensea.io/privacy)."
)
SLUG_QUERY = """
query CollectionsByQuery($query: String!) {
  collectionsByQuery(query: $query, limit: 5) {
    slug
    address
    chain { identifier }
  }
}
"""
ELIGIBILITY_QUERY = """
query DropEligibilityQuery($collectionSlug: String!, $address: Address!) {
  dropBySlug(slug: $collectionSlug) {
    __typename
    ... on Erc721SeaDropV1 { minterQuantityMinted(minter: $address) }
    stages {
      __typename
      label
      stageType
      stageIndex
      startTime
      endTime
      isEligible
      maxTotalMintableByWallet
      eligibleMaxTotalMintableByWallet
      price { token { unit } }
      eligiblePrice { token { unit } }
    }
  }
}
"""
MINT_QUERY = """
query MintActionTimelineQuery(
  $address: Address!
  $fromAssets: [AssetQuantityInput!]!
  $toAssets: [AssetQuantityInput!]!
  $recipient: Address
) {
  swap(
    address: $address
    fromAssets: $fromAssets
    toAssets: $toAssets
    recipient: $recipient
    action: MINT
  ) {
    actions {
      __typename
      ... on TransactionAction {
        transactionSubmissionData {
          to
          data
          value
          chain { networkId identifier }
        }
      }
    }
    errors { __typename }
  }
}
"""


def checks_enabled():
    """On for the running bot. Off under the unit tests, which must not sign in to OpenSea."""
    flag = os.environ.get("PHASE_CHECKS")
    if flag == "0":
        return False
    if flag == "1":
        return True
    return "unittest" not in sys.modules


def parse_time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return int(datetime.fromisoformat(text).timestamp())


def price_wei(unit):
    """OpenSea sends a coin amount such as 0.005, not wei. Zero stays zero."""
    if unit is None:
        return 0
    amount = Decimal(str(unit))
    if amount <= 0:
        return 0
    if amount < Decimal(1000):
        return int(amount * Decimal(10**18))
    return int(amount)


def choose_stage(stages, now):
    """The phase this wallet should use: the earliest one it can still mint, not the public sale."""
    usable = []
    for stage in stages or []:
        if not stage.get("eligible"):
            continue
        end = stage.get("end")
        if end is not None and end <= now:
            continue
        remaining = stage.get("max")
        minted = int(stage.get("minted") or 0)
        if remaining is not None and minted >= int(remaining):
            continue
        usable.append(stage)
    if not usable:
        return None
    open_now = [stage for stage in usable if (stage.get("start") or 0) <= now]
    # An allowlist that is open beats the public sale even when public started earlier.
    allowlists = [stage for stage in open_now if stage.get("kind") != "PUBLIC_SALE"]
    pool = allowlists or open_now or usable

    def rank(stage):
        # An allowlist that is open beats a public sale with the same start.
        public = 1 if stage.get("kind") == "PUBLIC_SALE" else 0
        return (stage.get("start") or 0, public)

    return min(pool, key=rank)


def decode_eligibility(body):
    """(minted count, stages) from DropEligibilityQuery. Raises ValueError on a bad payload."""
    if not isinstance(body, dict):
        raise ValueError("empty eligibility")
    if body.get("errors"):
        raise ValueError("eligibility query failed")
    drop = ((body.get("data") or {}).get("dropBySlug")) or {}
    if not drop:
        raise ValueError("no drop")
    minted = drop.get("minterQuantityMinted")
    try:
        minted = int(minted or 0)
    except (TypeError, ValueError):
        minted = 0
    stages = []
    for raw in drop.get("stages") or []:
        if not isinstance(raw, dict):
            continue
        price = raw.get("eligiblePrice") or raw.get("price") or {}
        unit = (price.get("token") or {}).get("unit") if isinstance(price, dict) else None
        cap = raw.get("eligibleMaxTotalMintableByWallet")
        if cap is None:
            cap = raw.get("maxTotalMintableByWallet")
        try:
            cap = int(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap = None
        start = parse_time(raw.get("startTime"))
        end = parse_time(raw.get("endTime"))
        if start is None or end is None or end <= start:
            continue
        stages.append(
            {
                "label": raw.get("label") or raw.get("stageType") or "phase",
                "kind": raw.get("stageType") or "",
                "start": start,
                "end": end,
                "eligible": bool(raw.get("isEligible")),
                "max": cap,
                "price_wei": price_wei(unit),
                "minted": minted,
            }
        )
    return minted, stages


def _parse_value(raw):
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    return int(text, 16) if text.startswith(("0x", "0X")) else int(text)


def mint_call_from_action(body, chain_id, chain_slug, nft, expected_value, seadrop):
    """(value, target, data) from a mint-action payload, or (None, reason).

    Refuses a transaction that is not this collection, on another chain, or at a different price.
    """
    if not isinstance(body, dict):
        return None, "OpenSea returned nothing"
    if body.get("errors"):
        return None, "OpenSea could not build the mint"
    swap = ((body.get("data") or {}).get("swap")) or {}
    errors = [str(item.get("__typename") or "") for item in (swap.get("errors") or []) if isinstance(item, dict)]
    for action in swap.get("actions") or []:
        tx = (action or {}).get("transactionSubmissionData") if isinstance(action, dict) else None
        if not tx or not tx.get("to") or not tx.get("data"):
            continue
        chain = tx.get("chain") or {}
        ident = str(chain.get("identifier") or "").lower()
        net = chain.get("networkId")
        if ident not in ("", chain_slug) and str(net) not in ("", str(chain_id)):
            return None, "OpenSea built the mint for a different chain"
        target = str(tx["to"])
        data = str(tx["data"])
        if not data.startswith("0x"):
            data = "0x" + data
        nft_hex = nft.lower().replace("0x", "")
        if nft_hex not in data.lower():
            return None, "OpenSea built a mint for a different collection"
        allowed = {seadrop.lower(), nft.lower()}
        if target.lower() not in allowed:
            return None, "OpenSea built a mint to an unexpected contract"
        try:
            value = _parse_value(tx.get("value"))
        except (TypeError, ValueError):
            return None, "OpenSea built a mint with a price that could not be read"
        if value != int(expected_value):
            return None, "OpenSea built a mint at a different price than this phase"
        return (value, target, data), None
    if any("NotMint" in name or "NotActive" in name or "NotStart" in name for name in errors):
        return None, "not minting yet"
    if any("Insufficient" in name or "Remaining" in name for name in errors):
        return None, "no mints left on this phase"
    if any("Eligib" in name or "Allow" in name for name in errors):
        return None, "not eligible for this phase"
    if errors:
        return None, "not eligible for this phase"
    return None, "not minting yet"


class DropSession:
    """One wallet signed in to OpenSea. The cookie jar is the session; the key never leaves the signer."""

    def __init__(self, account, slug, chain_slug, chain_id):
        self.account = account
        self.address = to_checksum_address(account.address)
        self.slug = slug
        self.chain_slug = chain_slug
        self.chain_id = chain_id
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [
            ("user-agent", "Mozilla/5.0"),
            ("origin", SITE),
        ]
        self.ready = False

    def _req(self, url, data=None, headers=None):
        head = {"accept": "application/json", "origin": SITE, "referer": f"{SITE}/collection/{self.slug}"}
        if headers:
            head.update(headers)
        body = None
        method = "GET"
        if data is not None:
            body = json.dumps(data).encode()
            method = "POST"
            head["content-type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=head, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def authenticate(self):
        last = "siwe failed"
        for chain in ("1", str(self.chain_id)):
            status, raw = self._req(f"{SITE}/__api/auth/siwe/nonce", data={})
            try:
                nonce = json.loads(raw)["nonce"]
            except (ValueError, KeyError, TypeError):
                last = f"siwe nonce {status}"
                continue
            issued = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            uri = f"{SITE}/collection/{self.slug}"
            message = (
                f"opensea.io wants you to sign in with your Ethereum account:\n"
                f"{self.address}\n\n{SIWE_STATEMENT}\n\nURI: {uri}\nVersion: 1\n"
                f"Chain ID: {chain}\nNonce: {nonce}\nIssued At: {issued}"
            )
            sig = self.account.sign_message(encode_defunct(text=message)).signature.hex()
            if not sig.startswith("0x"):
                sig = "0x" + sig
            payload = {
                "message": {
                    "domain": "opensea.io",
                    "address": self.address,
                    "statement": SIWE_STATEMENT,
                    "uri": uri,
                    "version": "1",
                    "chainId": chain,
                    "nonce": nonce,
                    "issuedAt": issued,
                    "accountType": "Ethereum",
                },
                "signature": sig,
                "chainArch": "EVM",
            }
            status, raw = self._req(f"{SITE}/__api/auth/siwe/verify", data=payload)
            try:
                user = ((json.loads(raw).get("user") or {}).get("address") or "").lower()
            except ValueError:
                user = ""
            if status == 200 and user == self.address.lower():
                self.ready = True
                return
            last = f"siwe {status}"
        raise RuntimeError(last)

    def eligibility(self):
        if not self.ready:
            self.authenticate()
        status, raw = self._req(
            GQL,
            data={
                "operationName": "DropEligibilityQuery",
                "query": ELIGIBILITY_QUERY,
                "variables": {"collectionSlug": self.slug, "address": self.address},
            },
            headers={"x-app-id": "os2-web"},
        )
        if status == 401:
            self.ready = False
            self.authenticate()
            status, raw = self._req(
                GQL,
                data={
                    "operationName": "DropEligibilityQuery",
                    "query": ELIGIBILITY_QUERY,
                    "variables": {"collectionSlug": self.slug, "address": self.address},
                },
                headers={"x-app-id": "os2-web"},
            )
        if status != 200:
            raise RuntimeError(f"eligibility {status}")
        return decode_eligibility(json.loads(raw))

    def mint_action(self, nft, qty):
        if not self.ready:
            self.authenticate()
        variables = {
            "address": self.address,
            "fromAssets": [
                {
                    "asset": {
                        "contractAddress": "0x0000000000000000000000000000000000000000",
                        "chain": self.chain_slug,
                        "tokenId": None,
                    },
                    "quantity": None,
                }
            ],
            "toAssets": [
                {"asset": {"contractAddress": nft, "chain": self.chain_slug, "tokenId": "0"}, "quantity": str(int(qty))}
            ],
            "recipient": None,
        }
        status, raw = self._req(
            GQL,
            data={"operationName": "MintActionTimelineQuery", "query": MINT_QUERY, "variables": variables},
            headers={"x-app-id": "os2-web"},
        )
        if status == 401:
            self.ready = False
            self.authenticate()
            status, raw = self._req(
                GQL,
                data={"operationName": "MintActionTimelineQuery", "query": MINT_QUERY, "variables": variables},
                headers={"x-app-id": "os2-web"},
            )
        if status != 200:
            raise RuntimeError(f"mint action {status}")
        return json.loads(raw)


def find_slug(chain_slug, contract):
    """Collection slug for a contract, from OpenSea's public search. None if it is unknown."""
    req = urllib.request.Request(
        GQL,
        data=json.dumps({"operationName": "CollectionsByQuery", "query": SLUG_QUERY, "variables": {"query": contract}}).encode(),
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "x-app-id": "os2-web",
            "origin": SITE,
            "user-agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read())
    rows = ((body.get("data") or {}).get("collectionsByQuery")) or []
    want = contract.lower()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (row.get("address") or "").lower() != want:
            continue
        ident = ((row.get("chain") or {}).get("identifier") or "").lower()
        if ident in ("", chain_slug):
            slug = row.get("slug")
            if slug:
                return str(slug)
    return None
