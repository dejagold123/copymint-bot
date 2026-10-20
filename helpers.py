"""Pure helpers shared by the bot: formatting, parsing, ABI and revert decoding, selectors.

Nothing here touches the network, Telegram or the bot's state, so it is safe to import anywhere.
"""

from __future__ import annotations

import html
import os
import re
from decimal import Decimal

from eth_utils import keccak, to_checksum_address


def env(name, default=""):
    return os.environ.get(name, default).strip().strip('"').strip("'")


def flag(name, default="false"):
    return env(name, default).lower() in ("1", "true", "yes", "on")

TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()

TRANSFER_SINGLE = "0x" + keccak(text="TransferSingle(address,address,address,uint256,uint256)").hex()

TRANSFER_BATCH = "0x" + keccak(text="TransferBatch(address,address,address,uint256[],uint256[])").hex()

ZERO_TOPIC = "0x" + "0" * 64

def selector(signature):
    return "0x" + keccak(text=signature).hex()[:8]

OWNER_OF = selector("ownerOf(uint256)")

BALANCE_OF_721 = selector("balanceOf(address)")

BALANCE_OF_1155 = selector("balanceOf(address,uint256)")

TOKEN_BY_INDEX = selector("tokenOfOwnerByIndex(address,uint256)")

SUPPORTS_INTERFACE = selector("supportsInterface(bytes4)")

TOTAL_SUPPLY = selector("totalSupply()")

SAFE_TRANSFER_721 = selector("safeTransferFrom(address,address,uint256)")

SAFE_TRANSFER_1155 = selector("safeTransferFrom(address,address,uint256,uint256,bytes)")

SEND_PLAN_SECONDS = 120

SEAPORT_ADDRESSES = {"0x0000000000000068f116a894984e2db1123eb395", "0x00000000000000adc04c56bf30ac9d3c0aaf14dc"}

DEEP_SCAN_LIMIT = 5000

MAX_HOLDINGS = 5000

def uint_word(value):
    return f"{int(value):064x}"

def addr_word(address):
    return "0" * 24 + address[2:].lower()

def parse_id_list(text, limit=20000):
    """Token IDs from text like "5,7,100-120". Returns (ids, error)."""
    ids, seen = [], set()
    for part in re.split(r"[,;\s]+", (text or "").strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            return None, f"\"{part}\" is not a token ID or a range like 100-120."
        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) else low
        if high < low or high - low + 1 > limit:
            return None, f"The range {part} is empty or longer than {limit}."
        for token in range(low, high + 1):
            if token not in seen:
                seen.add(token)
                ids.append(token)
        if len(ids) > limit:
            return None, f"That is more than {limit} token IDs."
    if not ids:
        return None, "ids= needs at least one token ID, for example ids=5,7 or ids=1-500."
    return ids, None

def esc(value):
    return html.escape(str(value), quote=False)

def code(value):
    return f"<code>{esc(value)}</code>"

def card(title, *rows):
    """A Telegram HTML message: a bold headline, then 'Label: value' lines.

    Row values are used as given, so callers escape or wrap them with esc() and code().
    """
    lines = [f"<b>{esc(title)}</b>"]
    lines += [f"{label}: {value}" for label, value in rows if value not in (None, "")]
    return "\n".join(lines)

NATIVE_SYMBOL = {56: "BNB", 137: "POL", 999: "HYPE"}

def native_symbol(chain_id):
    return NATIVE_SYMBOL.get(chain_id, "ETH")

def fmt_native(wei, chain_id):
    amount = Decimal(int(wei)) / Decimal(10**18)
    text = f"{amount:.4f}" if amount >= Decimal("0.0001") or amount == 0 else f"{amount:.6f}"
    return f"{text} {native_symbol(chain_id)}"

def decode_abi_string(raw):
    """Decode the return value of a function that returns one string. Returns '' if it is not one."""
    try:
        data = bytes.fromhex((raw or "0x")[2:])
        offset = int.from_bytes(data[0:32], "big")
        length = int.from_bytes(data[offset : offset + 32], "big")
        text = data[offset + 32 : offset + 32 + length].decode("utf-8", "ignore")
    except Exception:
        return ""
    return "".join(ch for ch in text if ch.isprintable()).strip()[:40]

def as_int(value):
    if isinstance(value, int):
        return value
    if value in (None, "0x", ""):
        return 0
    text = str(value)
    return int(text, 16) if text.startswith("0x") else int(text)

REVERT_NAMES = {
    "sold out": [
        "SoldOut()", "MintedOut()", "MaxSupplyReached()", "ExceedsMaxSupply()", "MaxSupplyExceeded()",
        "SupplyExceeded()", "NoMoreTokens()", "AllTokensMinted()", "CollectionSoldOut()",
        "MintQuantityExceedsMaxSupply(uint256,uint256)", "ExceedsSupply()", "InsufficientSupply()",
    ],
    "the mint is not open (not started or already closed)": [
        "NotStarted()", "MintNotStarted()", "MintNotOpen()", "SaleNotStarted()", "SaleNotActive()",
        "SaleNotOpen()", "MintClosed()", "MintNotActive()", "MintInactive()", "NotOpen()", "SaleClosed()",
        "NotActive(uint256,uint256,uint256)", "MintEnded()", "PublicSaleNotActive()",
    ],
    "the mint is paused": ["Paused()", "MintPaused()", "EnforcedPause()", "ContractPaused()"],
    "wallet limit reached": [
        "WalletLimitExceeded()", "MaxPerWalletExceeded()", "ExceedsWalletLimit()", "MaxMintsPerWalletExceeded()",
        "MintQuantityExceedsMaxMintedPerWallet(uint256,uint256)", "MaxPerAddressExceeded()", "AlreadyClaimed()",
    ],
    "not on the allowlist for this phase": [
        "NotAllowlisted()", "NotOnAllowlist()", "InvalidProof()", "NotWhitelisted()", "NotEligible()",
        "InvalidMerkleProof()", "NotAllowed()",
    ],
    "wrong payment amount": [
        "InsufficientPayment()", "IncorrectPayment()", "WrongPrice()", "IncorrectPayment(uint256,uint256)",
        "InsufficientFunds()", "InvalidPayment()", "WrongPayment()",
    ],
}

REVERT_BY_SELECTOR = {selector(sig): label for label, sigs in REVERT_NAMES.items() for sig in sigs}

REVERT_WORDS = (
    ("sold out", ("sold out", "soldout", "sold-out", "max supply", "exceeds max", "exceed max", "minted out", "no more", "all minted", "supply reached", "supply exceeded", "exceeds supply", "out of stock")),
    ("the mint is paused", ("paused",)),
    ("the mint is not open (not started or already closed)", ("not started", "not open", "not active", "not live", "closed", "has ended", "inactive", "not begun", "too early", "sale is not", "minting is not")),
    ("not on the allowlist for this phase", ("allowlist", "whitelist", "merkle", "proof", "not eligible", "not allowed", "not authorized")),
    ("wallet limit reached", ("per wallet", "per address", "wallet limit", "already minted", "already claimed", "max per", "limit reached", "exceeds limit")),
    ("wrong payment amount", ("insufficient", "incorrect", "wrong price", "wrong value", "payment", "ether sent", "not enough")),
)

def decode_revert(data, message=""):
    """Turn revert data and an RPC message into plain words. Returns "" when nothing readable is found."""
    data = (data or "").strip()
    message = str(message or "")
    if not data.startswith("0x"):
        match = re.search(r"0x[0-9a-fA-F]{8,}", message)
        data = match.group(0) if match else ""
    if len(data) >= 10:
        sel = data[:10].lower()
        if sel == "0x08c379a0":
            try:
                text = decode_abi_string("0x" + data[10:]).strip()
            except Exception:
                text = ""
            if text:
                return f"the contract said \"{text[:120]}\""
        elif sel == "0x4e487b71":
            code = data[10:74].lstrip("0") or "0"
            return f"the contract hit an internal error (panic code 0x{code})"
        elif sel in REVERT_BY_SELECTOR:
            return REVERT_BY_SELECTOR[sel]
    text = re.sub(r"^.*?(execution reverted:?|reverted:?)", "", message, flags=re.I).strip(" :\"'")
    if text and not text.lower().startswith(("0x", "execution")):
        return f"the contract said \"{text[:120]}\""
    return ""

def classify_reason(text):
    """Map a decoded revert sentence onto one of the plain labels when it clearly means one."""
    lower = text.lower()
    for label, words in REVERT_WORDS:
        if any(word in lower for word in words):
            return f"{label} ({text})" if text.startswith("the contract said") else label
    return text

def topic_address(topic):
    return "0x" + topic[-40:]

def nft_received(logs, contract, buyer, token_id):
    """True when the logs show the buyer receiving that token from that contract (ERC-721 or ERC-1155)."""
    buyer = buyer.lower()
    for entry in logs or []:
        if (entry.get("address") or "").lower() != contract.lower():
            continue
        topics = [str(t).lower() for t in entry.get("topics") or []]
        data = entry.get("data") or "0x"
        if len(topics) == 4 and topics[0] == TRANSFER and topic_address(topics[2]) == buyer and int(topics[3], 16) == token_id:
            return True
        if len(topics) == 4 and topics[0] == TRANSFER_SINGLE and topic_address(topics[3]) == buyer and len(data) >= 2 + 128:
            if int(data[2:66], 16) == token_id:
                return True
    return False

def to_wei(amount):
    return int(Decimal(amount) * 10**18)

def valid_address(text):
    """Return a lower-case EVM address, or None if the input is malformed."""
    text = text.strip()
    if len(text) == 42 and text.startswith("0x") and all(char in "0123456789abcdefABCDEF" for char in text[2:]):
        return text.lower()
    return None

def short_address(address):
    return address[:6] + "…" + address[-4:]

def word(addr):
    return addr[2:].lower().rjust(64, "0")
