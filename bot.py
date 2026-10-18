#!/usr/bin/env python3
"""Watch wallets. When one mints an NFT to itself, mint the same quantity.

The copy is the mint call only. Signature and allowlist calls are tried too, and each minter is simulated on its own, so only eligible minters send.
It is broadcast only when a simulation shows the minter receives the same
collection and quantity. A send that does not land is retried, and the
transaction is replaced once if it is still pending after two blocks.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from eth_account import Account
from eth_utils import keccak, to_checksum_address

TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
TRANSFER_SINGLE = "0x" + keccak(text="TransferSingle(address,address,address,uint256,uint256)").hex()
TRANSFER_BATCH = "0x" + keccak(text="TransferBatch(address,address,address,uint256[],uint256[])").hex()
ZERO_TOPIC = "0x" + "0" * 64
LIFI = "https://li.quest/v1/quote"
MAX_ATTEMPTS = 3
# Blocks per eth_getLogs request. Alchemy's free plan rejects ranges above 10, so 10 is the safe default.
# Raise LOGS_MAX_BLOCKS on a plan that allows larger ranges.
try:
    LOG_CHUNK = max(1, int(os.environ.get("LOGS_MAX_BLOCKS", "10") or 10))
except ValueError:
    LOG_CHUNK = 10
MAX_WATCH = 50
# The largest cap /cap will set from Telegram, in native coin. Raise MAX_MINT_ETH in the environment for more.
CAP_COMMAND_LIMIT = Decimal("1")


def selector(signature):
    return "0x" + keccak(text=signature).hex()[:8]


MINT_OK = {
    selector("mint(uint256)"),
    selector("mint(address,uint256)"),
    selector("mint(uint256,address)"),
    selector("publicMint(uint256)"),
    selector("mintPublic(address,address,address,uint256)"),
    selector("claim(uint256)"),
    selector("claim(address,uint256)"),
    selector("purchase(uint256)"),
}
SEADROP_MINT_SIGNED = selector(
    "mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)"
)
# Mint functions the /mint command tries. "q" is the quantity and "a" is the minter's own address.
MANUAL_CALLS = (
    ("mint(uint256)", "q"),
    ("mint(address,uint256)", "aq"),
    ("mint(uint256,address)", "qa"),
    ("publicMint(uint256)", "q"),
    ("purchase(uint256)", "q"),
    ("claim(uint256)", "q"),
    ("claim(address,uint256)", "aq"),
    ("mint()", ""),
)
PRICE_VIEWS = ("mintPrice()", "price()", "cost()", "publicPrice()", "PRICE()", "salePrice()", "MINT_PRICE()")
MAX_MANUAL_QTY = 200
SNIPE_PREP_SECONDS = 5
EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"


OWNER_OF = selector("ownerOf(uint256)")
BALANCE_OF_721 = selector("balanceOf(address)")
BALANCE_OF_1155 = selector("balanceOf(address,uint256)")
TOKEN_BY_INDEX = selector("tokenOfOwnerByIndex(address,uint256)")
SUPPORTS_INTERFACE = selector("supportsInterface(bytes4)")
TOTAL_SUPPLY = selector("totalSupply()")
SAFE_TRANSFER_721 = selector("safeTransferFrom(address,address,uint256)")
SAFE_TRANSFER_1155 = selector("safeTransferFrom(address,address,uint256,uint256,bytes)")
SEND_PLAN_SECONDS = 120
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


def parse_countdown(tokens):
    """Seconds from text like '10m', '90s', '1h30m' or 'in 10m'. None if it is not a countdown."""
    tokens = list(tokens)
    if tokens and tokens[0].lower() == "in":
        tokens = tokens[1:]
    text = "".join(tokens).lower()
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text.strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds

NON_MINT = {
    selector("approve(address,uint256)"),
    selector("transfer(address,uint256)"),
    selector("transferFrom(address,address,uint256)"),
    selector("setApprovalForAll(address,bool)"),
    selector("deposit()"),
    selector("withdraw(uint256)"),
    selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"),
    selector("exactInput((bytes,address,uint256,uint256,uint256))"),
    selector("swapExactTokensForTokens(uint256,uint256,address[],address,uint256)"),
    selector("swapExactETHForTokens(uint256,address[],address,uint256)"),
    selector("execute(bytes,bytes[],uint256)"),
}
MULTICALL = {
    selector("multicall(bytes[])"),
    selector("multicall(uint256,bytes[])"),
    selector("aggregate((address,bytes)[])"),
    selector("aggregate3((address,bool,bytes)[])"),
    selector("tryAggregate(bool,(address,bytes)[])"),
}

# token address, decimals. Native is the zero address.
STABLES = {
    1: [("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6), ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6)],
    10: [("0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", 6), ("0x94b008aA00579c1307B0EF2c499aD98a8ce58e58", 6)],
    56: [("0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18), ("0x55d398326f99059fF775485246999027B3197955", 18)],
    137: [("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6), ("0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6)],
    8453: [("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6)],
    42161: [("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6), ("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6)],
    999: [("0xb88339CB7199b77E23DB6E890353E22632Ba630f", 6)],
}

CHAINS = {
    1: ("Ethereum", "https://ethereum.publicnode.com"),
    10: ("Optimism", "https://optimism.publicnode.com"),
    56: ("BNB Chain", "https://bsc.publicnode.com"),
    137: ("Polygon", "https://polygon-bor.publicnode.com"),
    8453: ("Base", "https://base.publicnode.com"),
    42161: ("Arbitrum", "https://arbitrum.publicnode.com"),
    999: ("HyperEVM", "https://rpc.hyperliquid.xyz/evm"),
    4663: ("Robinhood", "https://rpc.mainnet.chain.robinhood.com"),
}


def env(name, default=""):
    return os.environ.get(name, default).strip().strip('"').strip("'")


def flag(name, default="false"):
    return env(name, default).lower() in ("1", "true", "yes", "on")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def notify(message, html=False):
    """Send a Telegram message when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.

    Runs in a background thread so a slow Telegram never delays a mint, and
    never raises. The failure log leaves out the URL because it holds the token.
    """
    token = env("TELEGRAM_BOT_TOKEN")
    chat = env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return

    params = {"chat_id": chat, "text": message[:3900], "disable_web_page_preview": "true"}
    if html:
        params["parse_mode"] = "HTML"

    def worker():
        try:
            body = urllib.parse.urlencode(
                params
            ).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=body,
                headers={"user-agent": "copymint-bot"},
            )
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:
            log(f"telegram alert failed: {type(exc).__name__}")

    threading.Thread(target=worker, daemon=True).start()


def alert(msg, rich=None):
    """Log plain text. Send the Telegram HTML version when one is given."""
    log(msg)
    if rich is None:
        notify(msg)
    else:
        notify(rich, html=True)


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


def collections_html(want):
    """One line per collection: the full contract address and how many were minted."""
    return "\n".join(f"{code(collection)} ×{amount}" for collection, amount in want.items())


MENU = (
    ("status", "📡 Copying, dry run, and wallets"),
    ("off", "⏸ Pause copying"),
    ("on", "▶️ Resume copying"),
    ("wallets", "👀 List watched wallets"),
    ("watch", "➕ Add a wallet address"),
    ("unwatch", "➖ Remove a watched wallet"),
    ("minters", "👛 Minter and funder addresses"),
    ("balance", "💰 Native balance on each chain"),
    ("cap", "🎯 Show or set the mint cap"),
    ("mint", "🎟 Mint a contract with all minters"),
    ("snipe", "🎯 Schedule a timed mint"),
    ("sendnfts", "📦 Send all NFTs to one wallet"),
    ("help", "ℹ️ List every command"),
)


def publish_commands():
    """Register the slash menu. Telegram shows it when the user types /."""
    commands = [{"command": name, "description": text} for name, text in MENU]
    telegram_call("setMyCommands", {"commands": json.dumps(commands)})


def telegram_call(method, params=None, timeout=35):
    """Call the Telegram Bot API without logging the token-bearing URL."""
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(params or {}).encode(),
        headers={"user-agent": "copymint-bot"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    if not payload.get("ok", False):
        raise RuntimeError(f"Telegram API {method} failed")
    return payload


class SeenSet:
    """A set that remembers insertion order, so the newest keys survive trimming."""

    def __init__(self, items=()):
        self._items = dict.fromkeys(items)

    def add(self, key):
        self._items[key] = None

    def __contains__(self, key):
        return key in self._items

    def __len__(self):
        return len(self._items)

    def tail(self, count):
        return list(self._items)[-count:]


def as_int(value):
    if isinstance(value, int):
        return value
    if value in (None, "0x", ""):
        return 0
    text = str(value)
    return int(text, 16) if text.startswith("0x") else int(text)


def word_int(body, index):
    return int.from_bytes(body[index : index + 32], "big")


def read_bytes(body, offset):
    length = word_int(body, offset)
    start = offset + 32
    return body[start : start + length]


def decode_bytes_array(body, start):
    count = word_int(body, start)
    items = []
    for i in range(count):
        relative = word_int(body, start + 32 + 32 * i)
        # Offsets in a dynamic array are relative to the word after the length.
        items.append(read_bytes(body, start + 32 + relative))
    return items


def decode_calls(body, start, with_bool=False):
    count = word_int(body, start)
    calls = []
    for i in range(count):
        relative = word_int(body, start + 32 + 32 * i)
        # Offsets in a dynamic array are relative to the first offset word.
        tuple_at = start + 32 + relative
        target = "0x" + body[tuple_at + 12 : tuple_at + 32].hex()
        head = tuple_at + 32
        if with_bool:
            head += 32
        data = read_bytes(body, tuple_at + word_int(body, head))
        calls.append((target, "0x" + data.hex()))
    return calls


def decode_multicall(data):
    signature = data[:10].lower()
    body = bytes.fromhex(data[10:])
    if signature == selector("multicall(bytes[])"):
        return [(None, "0x" + item.hex()) for item in decode_bytes_array(body, word_int(body, 0))]
    if signature == selector("multicall(uint256,bytes[])"):
        return [(None, "0x" + item.hex()) for item in decode_bytes_array(body, word_int(body, 32))]
    if signature == selector("aggregate((address,bytes)[])"):
        return decode_calls(body, word_int(body, 0), with_bool=False)
    if signature == selector("aggregate3((address,bool,bytes)[])"):
        return decode_calls(body, word_int(body, 0), with_bool=True)
    if signature == selector("tryAggregate(bool,(address,bytes)[])"):
        return decode_calls(body, word_int(body, 32), with_bool=False)
    return []


def expand_calls(target, data, value, depth=0):
    signature = data[:10].lower() if len(data) >= 10 else ""
    if depth < 3 and signature in MULTICALL:
        try:
            inners = decode_multicall(data)
        except Exception:
            inners = []
        if inners:
            found = []
            for inner_target, inner_data in inners:
                found.extend(expand_calls(inner_target or target, inner_data, value, depth + 1))
            return found
    return [(target, data, value)]


# Simulations run as if the minter could pay, so a low balance never makes a mint look like it reverts.
# The bot bridges or swaps in the missing funds after the simulation passes.
SIM_BALANCE = hex(10**24)


def looks_signed(data):
    if len(data) < 10:
        return False
    try:
        body = bytes.fromhex(data[10:])
    except ValueError:
        return False
    for index in range(0, max(0, len(body) - 96), 32):
        if word_int(body, index) != 65:
            continue
        if index + 32 + 65 > len(body):
            continue
        if body[index + 32 + 64] in (0, 1, 27, 28):
            return True
    return False


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


def single_qty(data):
    if not data or len(data) < 2 + 128:
        return 1
    return int(data[2 + 64 : 2 + 128], 16)


def batch_qty(data):
    try:
        body = bytes.fromhex(data[2:])
        amounts_at = word_int(body, 32)
        count = word_int(body, amounts_at)
        total = 0
        for i in range(count):
            total += word_int(body, amounts_at + 32 + 32 * i)
        return total
    except Exception:
        return 0


def topic_address(topic):
    return "0x" + topic[-40:]


def collect_logs(node, found):
    if isinstance(node, dict):
        topics = node.get("topics")
        address = node.get("address")
        if isinstance(topics, list) and isinstance(address, str) and address.startswith("0x") and len(address) == 42 and "data" in node:
            found.append(node)
        for value in node.values():
            collect_logs(value, found)
    elif isinstance(node, list):
        for value in node:
            collect_logs(value, found)


def method_missing(exc):
    text = str(exc).lower()
    return "not found" in text or "not available" in text or "unsupported" in text or "32601" in text or "does not exist" in text


@dataclass
class Chain:
    id: int
    name: str
    rpc: str
    wss: str = ""


class RPC:
    def __init__(self, chains: dict[int, Chain]):
        self.chains = chains

    def call(self, chain_id, method, params, timeout=25):
        url = self.chains[chain_id].rpc
        req = urllib.request.Request(
            url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            headers={"content-type": "application/json", "user-agent": "copymint-bot"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:240]
            raise RuntimeError(f"{method} {exc.code} {detail}") from exc
        if body.get("error"):
            raise RuntimeError(body["error"])
        return body["result"]


def load_keys(raw):
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.startswith("0x"):
            part = "0x" + part
        out.append(Account.from_key(part))
    return out


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


def rewrite(data, old, new):
    if old.lower() == new.lower():
        return data
    return "0x" + data[2:].lower().replace(word(old), word(new))


class Bot:
    def __init__(self):
        # Commas, spaces and new lines all separate addresses, so "0xA, 0xB" works.
        watch = []
        for part in re.split(r"[,;\s]+", env("WATCH_ADDRESSES")):
            if not part:
                continue
            address = valid_address(part)
            if address is None:
                raise SystemExit(f"WATCH_ADDRESSES has an invalid address: {part[:12]}...")
            watch.append(address)
        if not watch:
            raise SystemExit("Set WATCH_ADDRESSES")
        self.watch = {a.lower() for a in watch}
        self.minters = load_keys(env("MINTER_PRIVATE_KEYS"))
        if not self.minters:
            raise SystemExit("Set MINTER_PRIVATE_KEYS")
        funder = env("FUNDER_PRIVATE_KEY")
        self.funder = load_keys(funder)[0] if funder else None
        self.own = {a.address.lower() for a in self.minters}
        if self.funder:
            self.own.add(self.funder.address.lower())

        selected = [int(x) for x in env("CHAINS", "1,10,56,137,8453,42161,999,4663").split(",") if x.strip()]
        chains = {}
        for cid in selected:
            if cid not in CHAINS:
                raise SystemExit(f"Unknown chain id {cid}")
            name, default = CHAINS[cid]
            wss = env(f"WSS_{cid}")
            if wss and not wss.startswith(("wss://", "ws://")):
                # The value is not echoed, because the URL usually contains an API key.
                raise SystemExit(f"WSS_{cid} must start with wss://")
            chains[cid] = Chain(cid, name, env(f"RPC_{cid}", default), wss)
        self.rpc = RPC(chains)
        self.chains = chains
        # Caps are in each chain's native coin (ETH, BNB, POL...). MAX_MINT_<chainId> overrides the default.
        default_cap = env("MAX_MINT_ETH", "0.05")
        self.max_values = {cid: to_wei(env(f"MAX_MINT_{cid}", default_cap)) for cid in chains}
        self.allow_estimate = flag("ALLOW_ESTIMATE_FALLBACK")
        self.bridge = flag("ENABLE_BRIDGE", "true")
        self.bridge_timeout = int(env("BRIDGE_TIMEOUT", "180"))
        self.poll = float(env("POLL_SECONDS", "0.4"))
        self.dry = flag("DRY_RUN")
        self.state_path = Path(env("STATE_PATH", "/data/state.json"))
        if str(self.state_path).startswith("/data"):
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.state_path = Path("state.json")
                log("no /data volume, using ./state.json")
        self.heads = {}
        self.seen = SeenSet()
        self.pending = {}
        try:
            self.mint_cooldown_seconds = max(0, int(env("MINT_COOLDOWN_SECONDS", "0")))
        except ValueError as exc:
            raise SystemExit("MINT_COOLDOWN_SECONDS must be a non-negative integer") from exc
        # One cooldown per chain + NFT collection, shared by every watched wallet.
        self.collection_cooldowns = {}
        # chain:collection -> minter addresses this bot has already minted it from.
        self.minted_before = {}
        # chain:collection:wallet -> {"std": 721 or 1155, "ids": [token ids]} for NFTs the minters received.
        self.holdings = {}
        self.send_plan = None
        self.send_cancel = threading.Event()
        self._send_lock = threading.Lock()
        self._names = {}
        self._collection_lock = threading.Lock()
        self.paused = False
        self.cap_overrides = {}
        self.watch_saved = False
        self._state_lock = threading.Lock()
        self._watch_lock = threading.RLock()
        # WebSocket mode. A chain with WSS_<chainId> set is pushed new mints and is polled
        # only every WSS_BACKUP_SECONDS as a safety net, or at full speed while its socket is down.
        self.wss_backup = float(env("WSS_BACKUP_SECONDS", "30"))
        # While a socket is live the backup tick only moves the scan position forward (one cheap request).
        # Set WSS_BACKUP_SCAN=true to also re-scan the blocks for logs, which costs far more on a fast chain.
        self.wss_backup_scan = flag("WSS_BACKUP_SCAN")
        # Chains whose socket just connected or dropped; they get one full catch-up scan.
        self.ws_gap = set()
        self.snipe_retry = max(0.1, float(env("SNIPE_RETRY_SECONDS", "0.5")))
        self.snipe_window = max(1.0, float(env("SNIPE_WINDOW_SECONDS", "10")))
        self.snipe_plan = None
        self.snipe_cancel = threading.Event()
        self._snipe_lock = threading.Lock()
        self.ws_live = {}
        self.last_poll = {}
        self.watch_version = 0
        # One copy at a time, because the minters' nonces are shared by every thread.
        self._copy_lock = threading.RLock()
        self._ws_lock = threading.Lock()
        self._ws_recent = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ws")
        self._load()

    def cap(self, chain_id):
        return self.max_values[chain_id]

    def _load(self):
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text())
            self.heads = {int(k): int(v) for k, v in (data.get("heads") or {}).items()}
            self.seen = SeenSet(data.get("seen") or [])
            self.pending = data.get("pending") or {}
            self.collection_cooldowns = {}
            now = time.time()
            for key, record in (data.get("collection_cooldowns") or {}).items():
                try:
                    until = float(record.get("until", 0))
                    tx_hash = str(record.get("tx", "")).lower()
                except (AttributeError, TypeError, ValueError):
                    continue
                if until > now and tx_hash:
                    self.collection_cooldowns[str(key)] = {"until": until, "tx": tx_hash}

            self.minted_before = {}
            for key, addresses in (data.get("minted_before") or {}).items():
                if isinstance(addresses, list):
                    clean = [a.lower() for a in addresses if valid_address(str(a))]
                    if clean:
                        self.minted_before[str(key)] = clean
            self.holdings = {}
            for key, entry in (data.get("holdings") or {}).items():
                try:
                    std = int(entry.get("std"))
                    ids = [str(int(i)) for i in entry.get("ids") or []]
                except (AttributeError, TypeError, ValueError):
                    continue
                if std in (721, 1155) and ids and len(str(key).split(":")) == 3:
                    self.holdings[str(key)] = {"std": std, "ids": ids}
            self.paused = bool(data.get("paused", False))
            saved_watch = data.get("watch")
            if isinstance(saved_watch, list) and saved_watch:
                # Re-validate persisted data before trusting it.
                clean = {a.lower() for a in saved_watch if valid_address(str(a))}
                clean.difference_update(self.own)
                if clean:
                    self.watch = clean
                    self.watch_saved = True
            for raw_cid, raw_value in (data.get("caps") or {}).items():
                try:
                    cid, value = int(raw_cid), int(raw_value)
                except (TypeError, ValueError):
                    continue
                if cid in self.max_values and value >= 0:
                    self.max_values[cid] = value
                    self.cap_overrides[cid] = value

    def _save(self):
        # Both the scanner and Telegram control loop can save state.
        with self._state_lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            state = {
                "heads": self.heads,
                "seen": self.seen.tail(5000),
                "pending": self.pending,
                "paused": self.paused,
            }
            lock = getattr(self, "_collection_lock", None)
            now = time.time()
            if lock:
                with lock:
                    cooldowns = dict(self.collection_cooldowns)
            else:
                cooldowns = dict(getattr(self, "collection_cooldowns", {}))
            live = {k: v for k, v in cooldowns.items() if float(v.get("until", 0)) > now}
            if live:
                state["collection_cooldowns"] = live
            minted = dict(getattr(self, "minted_before", {}))
            if minted:
                state["minted_before"] = minted
            holdings = getattr(self, "holdings", None)
            if isinstance(holdings, dict) and holdings:
                state["holdings"] = dict(holdings)
            if self.watch_saved:
                state["watch"] = sorted(self.watch)
            if self.cap_overrides:
                state["caps"] = {str(cid): str(value) for cid, value in self.cap_overrides.items()}
            tmp.write_text(json.dumps(state))
            tmp.replace(self.state_path)

    def balances_text(self):
        accounts = list(self.minters) + ([self.funder] if self.funder else [])
        jobs = [(index, cid) for index in range(len(accounts)) for cid in self.chains]

        def fetch(job):
            index, cid = job
            try:
                return index, cid, self.balance(cid, "native", accounts[index].address)
            except Exception:
                return index, cid, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(fetch, jobs))
        lines = []
        for index, account in enumerate(accounts):
            label = "🏦 funder" if self.funder is not None and account is self.funder else "👛 minter"
            lines.append(f"{label} {short_address(account.address)}")
            for row_index, cid, balance in rows:
                if row_index == index:
                    amount = "n/a" if balance is None else f"{balance / 1e18:.4f}"
                    lines.append(f"  • {self.chains[cid].name}: {amount}")
        return "\n".join(lines) or "📭 No minter or funder wallets configured."

    def wallets_text(self):
        wallets = sorted(self.watch)
        source = "saved list" if self.watch_saved else "WATCH_ADDRESSES"
        return f"👀 Watching {len(wallets)} wallet(s) ({source}):\n" + "\n".join(
            "• " + to_checksum_address(address) for address in wallets
        )

    def watch_command(self, args):
        if not args:
            return "✏️ Usage: /watch 0xADDRESS [0xADDRESS ...]"
        updated = set(self.watch)
        lines = []
        for raw in args:
            address = valid_address(raw)
            if address is None:
                lines.append(f"⚠️ {raw[:12]}: invalid EVM address")
            elif address in self.own:
                lines.append(f"🚫 {short_address(address)}: one of your own wallets; not added")
            elif address in updated:
                lines.append(f"👀 {short_address(address)}: already watched")
            elif len(updated) >= MAX_WATCH:
                lines.append(f"⛔ {short_address(address)}: limit of {MAX_WATCH} watched wallets reached")
            else:
                updated.add(address)
                lines.append(f"✅ {short_address(address)}: added")
        if updated != self.watch:
            with self._watch_lock:
                self.watch = updated
            self.watch_saved = True
            self._watch_changed()
            self._save()
            lines.append("⏱ Changes take effect from the next scanned block; old mints are not replayed.")
        return "\n".join(lines)

    def unwatch_command(self, args):
        if not args:
            return "✏️ Usage: /unwatch 0xADDRESS [0xADDRESS ...]"
        updated = set(self.watch)
        lines = []
        for raw in args:
            address = valid_address(raw)
            if address is None:
                lines.append(f"⚠️ {raw[:12]}: invalid EVM address")
            elif address not in updated:
                lines.append(f"🔍 {short_address(address)}: not currently watched")
            elif len(updated) <= 1:
                lines.append(f"🔒 {short_address(address)}: cannot remove the last watched wallet; use /off")
            else:
                updated.remove(address)
                lines.append(f"✅ {short_address(address)}: removed")
        if updated != self.watch:
            with self._watch_lock:
                self.watch = updated
            self.watch_saved = True
            self._watch_changed()
            self._save()
        return "\n".join(lines)

    def manual_options(self, chain_id, contract, qty, price_wei=None):
        """The calls /mint will try. Each minter simulates them and uses the first that gives it the NFTs."""
        cap = self.cap(chain_id)
        value = 0
        if price_wei is not None:
            value = price_wei * qty
        else:
            for signature in PRICE_VIEWS:
                try:
                    raw = self.rpc.call(chain_id, "eth_call", [{"to": to_checksum_address(contract), "data": selector(signature)}, "latest"])
                    unit = as_int(raw)
                except Exception:
                    continue
                if 0 < unit * qty <= cap:
                    value = unit * qty
                    break
        with self._watch_lock:
            placeholder = sorted(self.watch)[0]  # swapped for each minter's own address when it simulates
        options = []
        for signature, layout in MANUAL_CALLS:
            if signature == "mint()" and qty != 1:
                continue
            words = "".join(
                f"{qty:064x}" if kind == "q" else "0" * 24 + placeholder[2:] for kind in layout
            )
            options.append((contract, selector(signature) + words, value))
        if value > 0:
            options.append((contract, "0x", value))
        return options

    def pull_option(self, args, prefixes):
        """Remove a name=value option from args (a trailing comma pulls in the next word). Returns the value or None."""
        for index, token in enumerate(list(args)):
            if token.lower().startswith(prefixes):
                end = index + 1
                value = token.split("=", 1)[1]
                while value.endswith(",") and end < len(args):
                    value += args[end]
                    end += 1
                del args[index:end]
                return value
        return None

    def mint_command(self, args):
        args = list(args)
        usage = (
            "✏️ Usage: /mint [CHAIN_ID] 0xCONTRACT [QUANTITY] [PRICE_PER_NFT] [wallets=1,3]\n"
            "Example: /mint 4663 0xabc… 2 0.001\n"
            "Add wallets=1,3 to use only those minters (numbers as /minters lists them). Default is all.\n"
            "Add chain=robinhood if more than one chain is enabled and the address is on several."
        )
        if not args:
            return usage
        wallet_spec = self.pull_option(args, ("wallets=", "wallet=", "w="))
        chain_word = self.pull_option(args, ("chain=", "c="))
        accounts, error = self.choose_minters(wallet_spec)
        if error:
            return error
        chain_id = None
        if chain_word is not None:
            chain_id = self.chain_from_word(chain_word)
            if chain_id is None:
                names = ", ".join(f"{c.name} ({cid})" for cid, c in self.chains.items())
                return f"⚠️ I do not know the chain \"{chain_word}\". Enabled chains: {names}."
        elif args and args[0].isdigit():
            chain_id = int(args.pop(0))
            if chain_id not in self.chains:
                return f"⚠️ Chain {chain_id} is not enabled."
        if chain_word is not None and args and args[0].isdigit() and int(args[0]) in self.chains and len(args) > 1 and valid_address(args[1]):
            args.pop(0)
        contract = valid_address(args[0]) if args else None
        if contract is None:
            return "⚠️ That is not a valid contract address.\n" + usage
        if chain_id is None:
            if len(self.chains) == 1:
                chain_id = next(iter(self.chains))
            else:
                found_on = [cid for cid in self.chains if self.has_code(cid, contract)]
                if len(found_on) == 1:
                    chain_id = found_on[0]
                elif not found_on:
                    names = ", ".join(c.name for c in self.chains.values())
                    return f"⚠️ I could not find a contract at that address on any enabled chain ({names}). Check the address, or add chain=NAME."
                else:
                    names = ", ".join(self.chains[cid].name for cid in found_on)
                    return f"⚠️ That address has a contract on several chains ({names}). Add chain=NAME, for example chain=robinhood."
        try:
            qty = int(args[1]) if len(args) > 1 else 1
        except ValueError:
            return "⚠️ Quantity must be a whole number.\n" + usage
        if not 1 <= qty <= MAX_MANUAL_QTY:
            return f"⚠️ Quantity must be between 1 and {MAX_MANUAL_QTY}."
        price_wei = None
        if len(args) > 2:
            try:
                price = Decimal(args[2])
            except Exception:
                return "⚠️ Price must be a number, in the chain's coin per NFT."
            if not price.is_finite() or price < 0:
                return "⚠️ Price must be zero or more."
            price_wei = to_wei(price)
        try:
            code_at = self.rpc.call(chain_id, "eth_getCode", [to_checksum_address(contract), "latest"])
        except Exception:
            return "⚠️ Could not reach the chain's RPC. Try again."
        if not code_at or code_at in ("0x", "0x0"):
            return "⚠️ There is no contract at that address on this chain."
        job = (chain_id, contract, qty, price_wei)
        if len(accounts) != len(self.minters):
            job += ([a.address for a in accounts],)
        threading.Thread(target=self.run_manual_mint, args=job, daemon=True).start()
        mode = " (dry run, nothing will be sent)" if self.dry else ""
        who = f"{len(self.minters)} minter(s)" if len(accounts) == len(self.minters) else "minter " + ", ".join(
            f"#{self.minters.index(a) + 1}" for a in accounts
        )
        return (
            f"🎟 Minting {qty} from {contract} on {self.chains[chain_id].name} with {who}{mode}.\n"
            "A summary follows when it is done."
        )

    def run_manual_mint(self, chain_id, contract, qty, price_wei, wallets=None):
        try:
            options = self.manual_options(chain_id, contract, qty, price_wei)
            tx = {
                "hash": f"manual-{int(time.time())}-{contract[2:8]}",
                "from": "manual",
                "to": contract,
                "value": "0x0",
                "input": "0x",
            }
            with self._copy_lock:
                self.copy(chain_id, tx, {contract: qty}, options=options, wallets=wallets)
        except Exception as exc:
            log(f"/mint {contract} {type(exc).__name__}: {exc}")
            notify(f"⚠️ /mint failed: {type(exc).__name__}")

    def mint_functions_in(self, chain_id, contract):
        """Known mint functions found in the contract's code, following a proxy to its implementation."""
        code_at = self.rpc.call(chain_id, "eth_getCode", [to_checksum_address(contract), "latest"]) or "0x"
        blobs = [code_at.lower()]
        implementation = None
        if code_at.lower().startswith("0x363d3d373d3d3d363d73") and len(code_at) >= 62:
            implementation = "0x" + code_at[22:62]
        else:
            try:
                slot = self.rpc.call(chain_id, "eth_getStorageAt", [to_checksum_address(contract), EIP1967_IMPLEMENTATION_SLOT, "latest"])
                if slot and int(slot, 16):
                    implementation = "0x" + slot[-40:]
            except Exception:
                pass
        if implementation:
            try:
                blobs.append((self.rpc.call(chain_id, "eth_getCode", [to_checksum_address(implementation), "latest"]) or "0x").lower())
            except Exception:
                pass
        joined = "".join(blobs)
        return [(sig, layout) for sig, layout in MANUAL_CALLS if selector(sig)[2:] in joined]

    def has_code(self, chain_id, contract):
        """True or False if the chain says whether a contract is there, None if the RPC could not answer."""
        try:
            code = self.rpc.call(chain_id, "eth_getCode", [to_checksum_address(contract), "latest"])
        except Exception:
            return None
        return bool(code) and code not in ("0x", "0x0")

    def chain_from_word(self, word):
        """A chain ID or name (robinhood, eth) typed by the user, as an enabled chain ID or None."""
        word = word.strip().lower()
        if word.isdigit():
            return int(word) if int(word) in self.chains else None
        for cid, chain in self.chains.items():
            name = chain.name.lower()
            if word == name or (len(word) >= 3 and name.startswith(word)):
                return cid
        return None

    def choose_minters(self, spec):
        """Minters named by 1-based numbers (as /minters lists them) or addresses. Returns (accounts, error)."""
        if spec is None or spec.lower() in ("all", "*"):
            return list(self.minters), None
        chosen, seen = [], set()
        for part in re.split(r"[,;\s]+", spec.strip()):
            if not part:
                continue
            match = None
            if part.isdigit():
                index = int(part)
                if not 1 <= index <= len(self.minters):
                    return None, f"⚠️ There is no minter number {index}. You have {len(self.minters)}. Send /minters to see them."
                match = self.minters[index - 1]
            elif part.lower().startswith("0x") and len(part) >= 8:
                hits = [a for a in self.minters if a.address.lower().startswith(part.lower())]
                if len(hits) != 1:
                    return None, f"⚠️ {part} does not match exactly one of your minters. Send /minters to see them."
                match = hits[0]
            else:
                return None, f"⚠️ I could not read \"{part}\" as a minter. Use numbers like wallets=1,3 or addresses."
            if match.address.lower() not in seen:
                seen.add(match.address.lower())
                chosen.append(match)
        if not chosen:
            return None, "⚠️ wallets= needs at least one minter, for example wallets=1,3 or wallets=all."
        return chosen, None

    def snipe_call_data(self, signature, layout, qty, minter):
        words = "".join(f"{qty:064x}" if kind == "q" else "0" * 24 + minter[2:].lower() for kind in layout)
        return selector(signature) + words

    def custom_call(self, chain_id, contract, signature):
        """A mint function the user named, such as publicMint(uint256), if its call data is simple and it is in the contract."""
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\(([a-z0-9,]*)\)", signature.strip())
        if not match:
            return None
        types = [t for t in match.group(2).split(",") if t]
        layout = ""
        for kind in types:
            if kind == "uint256":
                layout += "q"
            elif kind == "address":
                layout += "a"
            else:
                return None
        if layout.count("q") > 1:
            return None
        try:
            code = (self.rpc.call(chain_id, "eth_getCode", [to_checksum_address(contract), "latest"]) or "").lower()
        except Exception:
            return None
        blobs = [code]
        try:
            slot = self.rpc.call(chain_id, "eth_getStorageAt", [to_checksum_address(contract), EIP1967_IMPLEMENTATION_SLOT, "latest"])
            if slot and int(slot, 16):
                blobs.append((self.rpc.call(chain_id, "eth_getCode", [to_checksum_address("0x" + slot[-40:]), "latest"]) or "").lower())
        except Exception:
            pass
        if selector(signature)[2:] not in "".join(blobs):
            return None
        return (signature, layout)

    def build_snipe_plan(self, chain_id, contract, qty, price_wei, delay, override=None, accounts=None):
        """Work out the mint call, price and gas before the mint opens. Returns (plan, error)."""
        found = [(sig, layout) for sig, layout in self.mint_functions_in(chain_id, contract) if not (sig == "mint()" and qty != 1)]
        if override and not any(sig == override for sig, _ in found):
            custom = self.custom_call(chain_id, contract, override)
            if custom:
                found = [custom]
        if not found:
            chain_name = self.chains[chain_id].name
            return None, (
                f"⚠️ I could not find a standard mint function in that contract on {chain_name} (chain {chain_id}), so I cannot prepare it.\n"
                f"If {chain_name} is the wrong chain, add chain=NAME to the command.\n"
                "If it is the right chain, the contract uses a custom mint function. Add fn=NAME(types) if you know it, "
                "for example fn=publicMint(uint256), or use /mint once the mint is open."
            )
        if override:
            matches = [item for item in found if item[0] == override]
            if not matches:
                names = ", ".join(sig for sig, _ in found)
                return None, f"⚠️ {override} was not found in that contract. Functions I can use: {names}."
            found = matches
        signature, layout = found[0]
        others = [sig for sig, _ in found[1:]]
        if price_wei is None:
            unit = 0
            for view in PRICE_VIEWS:
                try:
                    unit = as_int(self.rpc.call(chain_id, "eth_call", [{"to": to_checksum_address(contract), "data": selector(view)}, "latest"]))
                except Exception:
                    continue
                if unit:
                    break
            value = unit * qty
        else:
            value = price_wei * qty
        if value > self.cap(chain_id):
            return None, (
                f"⚠️ The total price {fmt_native(value, chain_id)} is above your cap of {fmt_native(self.cap(chain_id), chain_id)}. "
                "Raise it with /cap first."
            )
        accounts = list(accounts or self.minters)
        first = accounts[0]
        data = self.snipe_call_data(signature, layout, qty, first.address)
        gas_note = "default"
        try:
            estimate = as_int(self.rpc.call(chain_id, "eth_estimateGas", [{"from": first.address, "to": to_checksum_address(contract), "data": data, "value": hex(value)}]))
            gas, gas_note = int(estimate * 1.5) + 20_000, "estimated"
        except Exception:
            gas = 100_000 + 120_000 * qty
        fee, _tip = self.fees(chain_id)
        attempts = int(self.snipe_window / self.snipe_retry) + 1
        plan = {
            "chain": chain_id,
            "contract": contract,
            "qty": qty,
            "value": value,
            "signature": signature,
            "layout": layout,
            "gas": gas,
            "gas_note": gas_note,
            "fire_at": time.time() + delay,
            "attempts": attempts,
            "worst": attempts * gas * fee,
            "others": others,
            "wallets": [a.address.lower() for a in accounts],
            "state": "planned",
        }
        return plan, None

    def plan_wallets_text(self, plan):
        wallets = plan.get("wallets") or [a.address.lower() for a in self.minters]
        if len(wallets) == len(self.minters):
            return f"all {len(self.minters)}"
        numbers = {a.address.lower(): i + 1 for i, a in enumerate(self.minters)}
        names = ", ".join(f"#{numbers.get(w, '?')} {short_address(w)}" for w in wallets)
        return f"{len(wallets)} of {len(self.minters)} ({names})"

    def snipe_plan_text(self, plan):
        chain = self.chains[plan["chain"]]
        seconds = max(0, int(plan["fire_at"] - time.time()))
        gas_line = (
            f"gas limit {plan['gas']:,} (estimated from the contract)"
            if plan["gas_note"] == "estimated"
            else f"gas limit {plan['gas']:,} (default: the mint is not open yet, so it cannot be estimated)"
        )
        price = "free" if plan["value"] == 0 else f"{fmt_native(plan['value'], plan['chain'])} per minter"
        return (
            f"🎯 Snipe plan · {chain.name}\n"
            f"Contract: {plan['contract']}\n"
            f"Call: {plan['signature']}, {plan['qty']} each, {price}\n"
            + (f"Other mint functions found: {', '.join(plan['others'])}. Add fn=NAME(types) to the command to pick another.\n" if plan.get("others") else "")
            + f"Minters: {self.plan_wallets_text(plan)}\n"
            f"Starts in: {seconds}s\n"
            f"Sends at the start, then retries every {self.snipe_retry:g}s for up to {self.snipe_window:g}s, "
            f"at most {plan['attempts']} tries per minter\n"
            f"{gas_line}\n"
            f"Worst case if every try fails: about {fmt_native(plan['worst'], plan['chain'])} in gas per minter\n"
            + ("Dry run is on: nothing will be sent\n" if self.dry else "")
            + "\nSend /snipe confirm to arm it, or /snipe cancel."
        )

    def snipe_command(self, args):
        args = list(args)
        usage = (
            "✏️ Usage: /snipe [CHAIN_ID] 0xCONTRACT [QTY] [PRICE] TIME [wallets=1,3]\n"
            "QTY defaults to 1. PRICE is per NFT in the chain's coin and defaults to auto, which reads it from the contract.\n"
            "TIME is a countdown such as 10m, 90s or 1h30m.\n"
            "Example: /snipe 4663 0xabc… 2 0.001 10m\n"
            "Shortest form: /snipe 0xabc… 42m wallets=1\n"
            "Add chain=robinhood (or the chain ID) if more than one chain is enabled and the address is on several.\n"
            "Add wallets=1,3 to use only those minters (numbers as /minters lists them), or wallets=all. Default is all.\n"
            "Then /snipe confirm to arm it, or /snipe cancel."
        )
        if not args:
            plan = self.snipe_plan
            if plan:
                return f"State: {plan['state']}\n" + self.snipe_plan_text(plan)
            return usage
        word = args[0].lower()
        if word == "cancel":
            with self._snipe_lock:
                plan, self.snipe_plan = self.snipe_plan, None
            if not plan:
                return "Nothing is scheduled."
            self.snipe_cancel.set()
            return "🛑 Snipe cancelled."
        if word == "confirm":
            with self._snipe_lock:
                plan = self.snipe_plan
                if not plan or plan["state"] != "planned":
                    return "There is no plan waiting for confirmation. Create one with /snipe first."
                if plan["fire_at"] - time.time() < SNIPE_PREP_SECONDS + 3:
                    self.snipe_plan = None
                    return "⚠️ Too late: the start is less than 10 seconds away. Send the /snipe command again with a longer countdown."
                plan["state"] = "armed"
                self.snipe_cancel.clear()
            threading.Thread(target=self.run_snipe, args=(plan,), daemon=True).start()
            return f"✅ Armed. It will fire in {int(plan['fire_at'] - time.time())}s. Send /snipe cancel to stop it."
        if self.snipe_plan and self.snipe_plan["state"] == "armed":
            return "A snipe is already armed. Send /snipe cancel first."
        override = None
        wallet_spec = None
        for token in list(args):
            if token.lower().startswith("fn="):
                override = token[3:]
                args.remove(token)
        for index, token in enumerate(list(args)):
            lowered = token.lower()
            if lowered.startswith(("wallets=", "wallet=", "w=")):
                end = index + 1
                spec = token.split("=", 1)[1]
                # Allow "wallets=1, 3": a trailing comma pulls in the next word.
                while spec.endswith(",") and end < len(args):
                    spec += args[end]
                    end += 1
                wallet_spec = spec
                del args[index:end]
                break
        chain_word = None
        for index, token in enumerate(list(args)):
            if token.lower().startswith(("chain=", "c=")):
                chain_word = token.split("=", 1)[1]
                del args[index]
                break
        accounts, error = self.choose_minters(wallet_spec)
        if error:
            return error
        delay = None
        for index, token in enumerate(args):
            if token.lower() == "in":
                continue
            seconds = parse_countdown([token])
            if seconds is not None:
                first = index - 1 if index and args[index - 1].lower() == "in" else index
                delay = seconds
                del args[first : index + 1]
                break
        if delay is None:
            return "⚠️ I could not read the countdown.\n" + usage
        if delay < SNIPE_PREP_SECONDS + 10:
            return "⚠️ The countdown must be at least 15 seconds."
        position = next((i for i, token in enumerate(args) if valid_address(token)), None)
        if position is None:
            return "⚠️ I could not find a valid contract address.\n" + usage
        contract = valid_address(args[position])
        before, after = args[:position], args[position + 1 :]
        chain_id = None
        if chain_word is not None:
            chain_id = self.chain_from_word(chain_word)
            if chain_id is None:
                names = ", ".join(f"{c.name} ({cid})" for cid, c in self.chains.items())
                return f"⚠️ I do not know the chain \"{chain_word}\". Enabled chains: {names}."
        elif before and before[0].isdigit() and int(before[0]) in self.chains:
            lead = int(before[0])
            # With one chain the number is the chain. With several it could be the quantity,
            # so it counts as the chain only if the contract is really on that chain.
            if len(self.chains) == 1 or self.has_code(lead, contract) is not False:
                chain_id = lead
                before.pop(0)
        if len(before) > 1 or len(after) > 2:
            return "⚠️ I did not understand part of that command.\n" + usage
        qty_text = before[0] if before else None
        if after:
            if qty_text is not None:
                return "⚠️ Give the quantity once.\n" + usage
            qty_text = after[0]
        price_text = after[1] if len(after) > 1 else "auto"
        if chain_id is None:
            if len(self.chains) == 1:
                chain_id = next(iter(self.chains))
            else:
                found_on = [cid for cid in self.chains if self.has_code(cid, contract)]
                if len(found_on) == 1:
                    chain_id = found_on[0]
                elif not found_on:
                    names = ", ".join(c.name for c in self.chains.values())
                    return f"⚠️ I could not find a contract at that address on any enabled chain ({names}). Check the address, or add chain=NAME."
                else:
                    names = ", ".join(self.chains[cid].name for cid in found_on)
                    return f"⚠️ That address has a contract on several chains ({names}). Add chain=NAME, for example chain=robinhood."
        try:
            qty = int(qty_text) if qty_text is not None else 1
        except ValueError:
            return "⚠️ Quantity must be a whole number.\n" + usage
        if not 1 <= qty <= MAX_MANUAL_QTY:
            return f"⚠️ Quantity must be between 1 and {MAX_MANUAL_QTY}."
        price_wei = None
        if price_text.lower() != "auto":
            try:
                price = Decimal(price_text)
            except Exception:
                return "⚠️ Price must be a number, or auto.\n" + usage
            if not price.is_finite() or price < 0:
                return "⚠️ Price must be zero or more."
            price_wei = to_wei(price)
        try:
            code_at = self.rpc.call(chain_id, "eth_getCode", [to_checksum_address(contract), "latest"])
        except Exception:
            return "⚠️ Could not reach the chain's RPC. Try again."
        if not code_at or code_at in ("0x", "0x0"):
            return "⚠️ There is no contract at that address on this chain."
        try:
            plan, error = self.build_snipe_plan(chain_id, contract, qty, price_wei, delay, override, accounts)
        except Exception as exc:
            log(f"snipe plan {type(exc).__name__}: {exc}")
            return "⚠️ Could not prepare the plan. Try again."
        if error:
            return error
        with self._snipe_lock:
            self.snipe_plan = plan
        return self.snipe_plan_text(plan)

    def snipe_wait(self, moment):
        """Sleep until the moment. Returns False if the snipe was cancelled first."""
        while True:
            remaining = moment - time.time()
            if remaining <= 0:
                return not self.snipe_cancel.is_set()
            if self.snipe_cancel.wait(min(remaining, 0.25 if remaining > 0.5 else 0.002)):
                return False

    def snipe_sign(self, chain_id, acct, plan, nonce, fee, tip):
        data = self.snipe_call_data(plan["signature"], plan["layout"], plan["qty"], acct.address)
        signed = acct.sign_transaction(
            {
                "chainId": chain_id,
                "nonce": nonce,
                "gas": plan["gas"],
                "maxFeePerGas": fee,
                "maxPriorityFeePerGas": tip,
                "to": to_checksum_address(plan["contract"]),
                "value": plan["value"],
                "data": data,
                "type": 2,
            }
        )
        return "0x" + signed.raw_transaction.hex()

    def snipe_worker(self, acct, plan, nonce, fee, tip, first_raw, deadline, report):
        """Send one minter's mint at the start, then retry after each reverted try until it lands or time runs out."""
        chain_id = plan["chain"]
        who = acct.address.lower()
        failed, last, reason = [], None, "did not land in time"
        raw = first_raw
        while True:
            if last is None:
                try:
                    if raw is None:
                        raw = self.snipe_sign(chain_id, acct, plan, nonce, fee, tip)
                    last = self.rpc.call(chain_id, "eth_sendRawTransaction", [raw])
                except Exception as exc:
                    reason = f"sending failed: {str(exc)[:80]}"
                    log(f"snipe {acct.address[:8]} {reason}")
                    last = None
                    if "nonce" in str(exc).lower():
                        try:
                            nonce = int(self.rpc.call(chain_id, "eth_getTransactionCount", [acct.address, "pending"]), 16)
                        except Exception:
                            pass
                raw = None
            else:
                try:
                    receipt = self.rpc.call(chain_id, "eth_getTransactionReceipt", [last])
                except Exception:
                    receipt = None
                if receipt:
                    if as_int(receipt.get("status", "0x1")) == 1:
                        report[who] = {"status": "minted", "txh": last, "value": plan["value"], "failed_txs": failed}
                        self.remember_minted(chain_id, acct.address, {plan["contract"]: plan["qty"]})
                        self.record_holdings_later(chain_id, acct.address, last)
                        return
                    failed.append(last)  # reverted, most likely because the mint is not open yet
                    reason = "every try reverted"
                    nonce += 1
                    last = None
                    if time.time() < deadline:
                        continue
            if time.time() >= deadline:
                break
            time.sleep(self.snipe_retry)
        record = {"status": "failed", "reason": reason, "failed_txs": failed}
        if last:
            record["reason"] = "the last try had not confirmed when time ran out"
            record["failed_txs"] = failed
        report[who] = record

    def run_snipe(self, plan):
        chain_id = plan["chain"]
        contract = plan["contract"]
        chain = self.chains[chain_id]
        report = {}
        try:
            fee, tip = self.fees(chain_id)
            active = []
            chosen = [a for a in self.minters if a.address.lower() in set(plan.get("wallets") or [x.address.lower() for x in self.minters])]
            for acct in chosen:
                if self.snipe_cancel.is_set():
                    return
                if self.ensure(acct, chain_id, plan["value"] + plan["gas"] * fee * 3):
                    active.append(acct)
                else:
                    report[acct.address.lower()] = {"status": "failed", "reason": "not enough funds and no bridge route"}
            if not self.snipe_wait(plan["fire_at"] - SNIPE_PREP_SECONDS):
                return
            if self.dry:
                for acct in active:
                    report[acct.address.lower()] = {"status": "dry"}
                    log(f"🧪 DRY snipe {acct.address} {plan['signature']} x{plan['qty']} on {chain.name}")
            else:
                self.rpc.call(chain_id, "eth_chainId", [])  # keeps the connection warm
                fee, tip = self.fees(chain_id)
                prepared = []
                for acct in active:
                    nonce = int(self.rpc.call(chain_id, "eth_getTransactionCount", [acct.address, "pending"]), 16)
                    prepared.append((acct, nonce, self.snipe_sign(chain_id, acct, plan, nonce, fee, tip)))
                if not self.snipe_wait(plan["fire_at"]):
                    return
                deadline = time.time() + self.snipe_window
                with self._copy_lock:
                    with ThreadPoolExecutor(max_workers=max(1, len(prepared))) as pool:
                        jobs = [
                            pool.submit(self.snipe_worker, acct, plan, nonce, fee, tip, raw, deadline, report)
                            for acct, nonce, raw in prepared
                        ]
                        for job in jobs:
                            job.result()
            tx = {"hash": f"snipe-{int(plan['fire_at'])}", "from": "manual", "to": contract}
            self.send_summary(chain_id, tx, {contract: plan["qty"]}, {"report": report, "total": len(chosen)}, kind="snipe")
        except Exception as exc:
            log(f"snipe {type(exc).__name__}: {exc}")
            notify(f"⚠️ Snipe failed: {type(exc).__name__}")
        finally:
            with self._snipe_lock:
                if self.snipe_plan is plan:
                    self.snipe_plan = None

    # ---- sending NFTs out ----

    def view(self, chain_id, to, data):
        try:
            raw = self.rpc.call(chain_id, "eth_call", [{"to": to_checksum_address(to), "data": data}, "latest"])
        except Exception:
            return None
        return raw if isinstance(raw, str) and raw.startswith("0x") and len(raw) > 2 else None

    def view_int(self, chain_id, to, data):
        raw = self.view(chain_id, to, data)
        if not raw or len(raw) < 66:
            return None
        try:
            return int(raw[:66], 16)
        except ValueError:
            return None

    def add_holding(self, chain_id, collection, wallet, std, ids):
        key = f"{chain_id}:{collection.lower()}:{wallet.lower()}"
        entry = self.holdings.setdefault(key, {"std": std, "ids": []})
        entry["std"] = std
        known = set(entry["ids"])
        for token_id in ids:
            if str(token_id) not in known:
                entry["ids"].append(str(token_id))
        del entry["ids"][:-MAX_HOLDINGS]
        while len(self.holdings) > 2000:
            self.holdings.pop(next(iter(self.holdings)))

    def drop_holding(self, chain_id, collection, wallet, token_id):
        key = f"{chain_id}:{collection.lower()}:{wallet.lower()}"
        entry = self.holdings.get(key)
        if not entry:
            return
        entry["ids"] = [i for i in entry["ids"] if i != str(token_id)]
        if not entry["ids"]:
            self.holdings.pop(key, None)

    def record_holdings(self, chain_id, minter, txh):
        """Remember the token IDs a minter received in one landed transaction, so they can be sent on later."""
        try:
            receipt = self.rpc.call(chain_id, "eth_getTransactionReceipt", [txh])
            if not receipt:
                return
            who = minter.lower()
            for entry in receipt.get("logs") or []:
                topics = [t.lower() for t in entry.get("topics") or []]
                collection = (entry.get("address") or "").lower()
                data = entry.get("data") or "0x"
                if not collection or len(topics) != 4:
                    continue
                if topics[0] == TRANSFER and topic_address(topics[2]) == who:
                    self.add_holding(chain_id, collection, who, 721, [int(topics[3], 16)])
                elif topics[0] == TRANSFER_SINGLE and topic_address(topics[3]) == who and len(data) >= 2 + 128:
                    self.add_holding(chain_id, collection, who, 1155, [int(data[2:66], 16)])
                elif topics[0] == TRANSFER_BATCH and topic_address(topics[3]) == who:
                    from eth_abi import decode

                    ids, _values = decode(["uint256[]", "uint256[]"], bytes.fromhex(data[2:]))
                    self.add_holding(chain_id, collection, who, 1155, list(ids))
            self._save()
        except Exception as exc:
            log(f"record holdings {type(exc).__name__}: {exc}")

    def record_holdings_later(self, chain_id, minter, txh):
        threading.Thread(target=self.record_holdings, args=(chain_id, minter, txh), daemon=True).start()

    def detect_standard(self, chain_id, collection, wallet):
        for interface, std in (("80ac58cd", 721), ("d9b67a26", 1155)):
            if self.view_int(chain_id, collection, SUPPORTS_INTERFACE + interface + "0" * 56):
                return std
        if self.view_int(chain_id, collection, BALANCE_OF_721 + addr_word(wallet)) is not None:
            return 721
        return None

    def owns_721(self, chain_id, collection, token_id, wallet):
        raw = self.view(chain_id, collection, OWNER_OF + uint_word(token_id))
        return bool(raw) and len(raw) >= 66 and raw[-40:].lower() == wallet[2:].lower()

    def check_owned(self, chain_id, collection, wallet, token_ids):
        """The token IDs from the list that the wallet owns, checked in parallel."""
        token_ids = list(token_ids)
        if len(token_ids) < 20:
            return [t for t in token_ids if self.owns_721(chain_id, collection, t, wallet)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            flags = list(pool.map(lambda t: self.owns_721(chain_id, collection, t, wallet), token_ids))
        return [t for t, mine in zip(token_ids, flags) if mine]

    def nfts_721(self, chain_id, collection, wallet, recorded, deep, given=()):
        """Token IDs this wallet owns in an ERC-721 collection, how many more it holds that were not found, and a note on why."""
        balance = self.view_int(chain_id, collection, BALANCE_OF_721 + addr_word(wallet))
        if not balance:
            return [], 0, ""
        owned = set(self.check_owned(chain_id, collection, wallet, [int(i) for i in list(recorded) + list(given)]))
        note = ""
        if len(owned) < balance:
            for index in range(balance):
                token = self.view_int(chain_id, collection, TOKEN_BY_INDEX + addr_word(wallet) + uint_word(index))
                if token is None:
                    break
                owned.add(token)
        if len(owned) < balance and deep:
            supply = self.view_int(chain_id, collection, TOTAL_SUPPLY)
            if not supply:
                note = "its totalSupply() could not be read, so there is no range to scan; add ids=1-5000 (your own range) to scan one"
            elif supply > DEEP_SCAN_LIMIT:
                note = f"it has {supply} NFTs, above the deep scan limit of {DEEP_SCAN_LIMIT}; add ids=FIRST-LAST to scan a range you choose"
            else:
                candidates = [i for i in range(0, supply + 2) if i not in owned]
                owned.update(self.check_owned(chain_id, collection, wallet, candidates))
                if len(owned) < balance:
                    note = f"I checked token IDs 0 to {supply + 1} and the rest are not in that range; add ids=FIRST-LAST for a wider range"
        elif len(owned) < balance and given:
            note = "none of the token IDs you gave, or the rest, are owned by this wallet"
        return sorted(owned), max(0, balance - len(owned)), note

    def nfts_1155(self, chain_id, collection, wallet, recorded):
        found = []
        for raw_id in recorded:
            amount = self.view_int(chain_id, collection, BALANCE_OF_1155 + addr_word(wallet) + uint_word(int(raw_id)))
            if amount:
                found.append((int(raw_id), amount))
        return sorted(found)

    def find_nfts(self, chain_id, acct, extra=(), deep=False, given=()):
        """Everything this wallet holds on one chain: a list of dicts with collection, std, items and missing."""
        wallet = acct.address.lower()
        hints = {}
        for key, entry in self.holdings.items():
            cid, collection, holder = key.split(":")
            if int(cid) == chain_id and holder == wallet:
                hints[collection] = (entry["std"], entry["ids"])
        for key, holders in self.minted_before.items():
            cid, _, collection = key.partition(":")
            if cid == str(chain_id) and wallet in holders:
                hints.setdefault(collection.lower(), (None, []))
        for collection in extra:
            hints.setdefault(collection.lower(), (None, []))
        found = []
        for collection, (std, recorded) in hints.items():
            std = std or self.detect_standard(chain_id, collection, wallet)
            note = ""
            extra_ids = list(given) if collection in {c.lower() for c in extra} else []
            if std == 721:
                ids, missing, note = self.nfts_721(chain_id, collection, wallet, recorded, deep, extra_ids)
                items = [(i, 1) for i in ids]
            elif std == 1155:
                items, missing = self.nfts_1155(chain_id, collection, wallet, list(recorded) + [str(i) for i in extra_ids]), 0
            else:
                continue
            if items or missing:
                found.append({"collection": collection, "std": std, "items": items, "missing": missing, "note": note})
        return found

    def transfer_data(self, std, sender, dest, token_id, amount):
        from eth_abi import encode

        if std == 721:
            return SAFE_TRANSFER_721 + addr_word(sender) + addr_word(dest) + uint_word(token_id)
        body = encode(["address", "address", "uint256", "uint256", "bytes"], [to_checksum_address(sender), to_checksum_address(dest), token_id, amount, b""])
        return SAFE_TRANSFER_1155 + body.hex()

    def sendnfts_command(self, args):
        args = list(args)
        usage = (
            "✏️ Usage: /sendnfts 0xDESTINATION [wallets=1,3] [chain=robinhood] [collection=0xabc…] [deep]\n"
            "Sends every NFT the chosen minters hold (default: all minters, all enabled chains) to the destination.\n"
            "I list what I found and what it will cost, then you send /sendnfts confirm within 2 minutes, or /sendnfts cancel.\n"
            "I know the NFTs your minters minted through this bot since this feature was added. For older NFTs add collection=0xabc… "
            "(finds them if the collection can list a wallet's tokens), deep (checks token by token, collections up to "
            f"{DEEP_SCAN_LIMIT} NFTs), or ids=5,7,100-120 with one collection= if you know the token IDs or a range to check."
        )
        if not args:
            return usage
        word0 = args[0].lower()
        if word0 == "cancel":
            with self._send_lock:
                plan, self.send_plan = self.send_plan, None
            if not plan:
                return "Nothing is waiting to be sent."
            self.send_cancel.set()
            return "🛑 Cancelled." if plan["state"] == "planned" else "🛑 Stopping. Transfers already sent cannot be undone."
        if word0 == "confirm":
            with self._send_lock:
                plan = self.send_plan
                if not plan or plan["state"] != "planned":
                    return "There is no plan waiting for confirmation. Create one with /sendnfts first."
                if time.time() - plan["created"] > SEND_PLAN_SECONDS:
                    self.send_plan = None
                    return "⌛ That plan expired. Send the /sendnfts command again."
                plan["state"] = "running"
                self.send_cancel.clear()
            threading.Thread(target=self.run_send, args=(plan,), daemon=True).start()
            mode = " (dry run, nothing will be sent)" if self.dry else ""
            return f"✅ Sending {len(plan['transfers'])} NFT transfer(s) to {plan['dest']}{mode}. A summary follows."
        with self._send_lock:
            if self.send_plan and self.send_plan["state"] == "running":
                return "A send is already running. Send /sendnfts cancel to stop it."
        deep = False
        for token in list(args):
            if token.lower() == "deep":
                deep = True
                args.remove(token)
        wallet_spec = self.pull_option(args, ("wallets=", "wallet=", "w="))
        chain_word = self.pull_option(args, ("chain=", "c="))
        collection_spec = self.pull_option(args, ("collection=", "collections="))
        ids_spec = self.pull_option(args, ("ids=", "id="))
        accounts, error = self.choose_minters(wallet_spec)
        if error:
            return error
        chain_ids = list(self.chains)
        if chain_word is not None:
            only = self.chain_from_word(chain_word)
            if only is None:
                names = ", ".join(f"{c.name} ({cid})" for cid, c in self.chains.items())
                return f"⚠️ I do not know the chain \"{chain_word}\". Enabled chains: {names}."
            chain_ids = [only]
        extra = []
        for part in re.split(r"[,;\s]+", collection_spec or ""):
            if not part:
                continue
            if valid_address(part) is None:
                return f"⚠️ \"{part}\" is not a valid collection address."
            extra.append(valid_address(part))
        given = []
        if ids_spec is not None:
            if len(extra) != 1:
                return "⚠️ ids= goes with exactly one collection=0x…, so I know which collection the IDs belong to."
            given, id_error = parse_id_list(ids_spec)
            if id_error:
                return "⚠️ " + id_error
        if len(args) != 1:
            return "⚠️ I need exactly one destination address.\n" + usage
        dest = valid_address(args[0])
        if dest is None:
            return "⚠️ That is not a valid destination address.\n" + usage
        if int(dest, 16) == 0:
            return "⚠️ I will not send NFTs to the zero address."
        threading.Thread(target=self.prepare_send, args=(dest, accounts, chain_ids, extra, deep, given), daemon=True).start()
        return f"🔎 Looking for NFTs in {len(accounts)} wallet(s) on {', '.join(self.chains[c].name for c in chain_ids)}. The plan follows in a moment."

    def prepare_send(self, dest, accounts, chain_ids, extra, deep, given=()):
        try:
            transfers, blocked, missing, skipped = [], [], [], []
            names = {}
            for chain_id in chain_ids:
                for acct in accounts:
                    if acct.address.lower() == dest:
                        skipped.append(acct.address)
                        continue
                    where = [c for c in extra if self.has_code(chain_id, c)]
                    for group in self.find_nfts(chain_id, acct, where, deep, given):
                        name = self.collection_name(chain_id, group["collection"]) or short_address(group["collection"])
                        names[(chain_id, group["collection"])] = name
                        if group["missing"]:
                            missing.append((chain_id, acct.address, group["collection"], group["missing"], group.get("note", "")))
                        for token_id, amount in group["items"]:
                            data = self.transfer_data(group["std"], acct.address, dest, token_id, amount)
                            transfer = {
                                "chain": chain_id,
                                "wallet": acct.address.lower(),
                                "collection": group["collection"],
                                "std": group["std"],
                                "id": str(token_id),
                                "amount": amount,
                            }
                            try:
                                estimate = as_int(
                                    self.rpc.call(chain_id, "eth_estimateGas", [{"from": acct.address, "to": to_checksum_address(group["collection"]), "data": data, "value": "0x0"}])
                                )
                                transfer["gas"] = int(estimate * 1.3) + 20_000
                                transfers.append(transfer)
                            except Exception as exc:
                                reason = decode_revert("", str(exc)) or "the contract refused the transfer"
                                blocked.append((transfer, reason))
            plan = {"dest": dest, "transfers": transfers, "created": time.time(), "state": "planned", "blocked": len(blocked)}
            text = self.send_plan_text(plan, names, blocked, missing, skipped, deep)
            with self._send_lock:
                if transfers and not (self.send_plan and self.send_plan["state"] == "running"):
                    self.send_plan = plan
            notify(text)
        except Exception as exc:
            log(f"/sendnfts plan {type(exc).__name__}: {exc}")
            notify(f"⚠️ /sendnfts failed while looking for NFTs: {type(exc).__name__}")

    def send_plan_text(self, plan, names, blocked, missing, skipped, deep):
        transfers = plan["transfers"]
        numbers = {a.address.lower(): i + 1 for i, a in enumerate(self.minters)}
        groups = {}
        for t in transfers:
            groups.setdefault((t["chain"], t["wallet"], t["collection"]), []).append(t)
        lines = []
        for (chain_id, wallet, collection), items in list(groups.items())[:25]:
            count = sum(t["amount"] for t in items)
            label = names.get((chain_id, collection)) or short_address(collection)
            lines.append(f"• {self.chains[chain_id].name} · #{numbers.get(wallet, '?')} {short_address(wallet)} · {label} ×{count}")
        if len(groups) > 25:
            lines.append(f"…and {len(groups) - 25} more group(s)")
        head = f"📦 Send NFTs plan\nTo: {plan['dest']}\n"
        if not transfers:
            body = "I found no NFTs that can be sent.\n"
        else:
            total = sum(t["amount"] for t in transfers)
            body = "\n".join(lines) + f"\nTotal: {total} NFT(s) in {len(transfers)} transfer(s)\n"
            if any(self.has_code(c, plan["dest"]) for c in {t["chain"] for t in transfers}):
                body += "⚠️ The destination is a contract on one of these chains. Double-check that it can receive NFTs.\n"
        if blocked:
            body += f"Cannot send {len(blocked)} NFT(s) (the contract refused the transfer): {blocked[0][1]}\n"
        if missing:
            more = sum(m[3] for m in missing)
            body += f"{more} more NFT(s) are held but I cannot list their token IDs."
            notes = []
            for m in missing:
                if m[4] and m[4] not in notes:
                    notes.append(m[4])
            if notes:
                body += " Why: " + "; ".join(notes) + "."
            elif not deep:
                body += " Add collection=0xabc… and deep to look harder."
            body += "\n"
        if skipped:
            body += "Skipped (already the destination): " + ", ".join(short_address(a) for a in skipped) + "\n"
        if self.dry:
            body += "Dry run is on: nothing will be sent\n"
        tail = f"\nSend /sendnfts confirm within {SEND_PLAN_SECONDS // 60} minutes to send, or /sendnfts cancel." if transfers else ""
        return head + body + tail

    def send_worker(self, plan, chain_id, acct, items, results):
        for t in items:
            if self.send_cancel.is_set():
                results.append((t, "cancelled", "stopped before this one"))
                continue
            data = self.transfer_data(t["std"], acct.address, plan["dest"], int(t["id"]), t["amount"])
            try:
                with self._copy_lock:
                    txh, ok, _sent = self.land(chain_id, acct, t["collection"], data, 0, t["gas"])
            except Exception as exc:
                results.append((t, "failed", str(exc)[:80]))
                continue
            if ok:
                results.append((t, "dry" if self.dry else "sent", txh))
                if not self.dry:
                    self.drop_holding(chain_id, t["collection"], acct.address, t["id"])
            else:
                results.append((t, "failed", "did not confirm in time"))
        self._save()

    def run_send(self, plan):
        results = []
        try:
            by_wallet = {}
            for t in plan["transfers"]:
                by_wallet.setdefault((t["chain"], t["wallet"]), []).append(t)
            accounts = {a.address.lower(): a for a in self.minters}
            with ThreadPoolExecutor(max_workers=max(1, len(by_wallet))) as pool:
                jobs = [
                    pool.submit(self.send_worker, plan, chain_id, accounts[wallet], items, results)
                    for (chain_id, wallet), items in by_wallet.items()
                    if wallet in accounts
                ]
                for job in jobs:
                    job.result()
            sent = [r for r in results if r[1] in ("sent", "dry")]
            failed = [r for r in results if r[1] == "failed"]
            stopped = [r for r in results if r[1] == "cancelled"]
            total = len(plan["transfers"])
            title = "🧪 Would send" if self.dry else ("✅ Sent" if len(sent) == total else "⚠️ Sent")
            lines = [f"{title} {len(sent)} of {total} NFT transfer(s) to {plan['dest']}"]
            if failed:
                reasons = {}
                for _t, _status, reason in failed:
                    reasons[reason] = reasons.get(reason, 0) + 1
                lines.append("Failed: " + "; ".join(f"{n}× {reason}" for reason, n in reasons.items()))
            if stopped:
                lines.append(f"Stopped before sending {len(stopped)}.")
            notify("\n".join(lines))
        except Exception as exc:
            log(f"/sendnfts {type(exc).__name__}: {exc}")
            notify(f"⚠️ /sendnfts failed: {type(exc).__name__}")
        finally:
            with self._send_lock:
                if self.send_plan is plan:
                    self.send_plan = None

    def cap_command(self, args):
        if not args:
            rows = [f"• {chain.name} ({cid}): {self.cap(cid) / 1e18:g}" for cid, chain in self.chains.items()]
            return (
                "🎯 Mint value caps in each chain's native coin:\n"
                + "\n".join(rows)
                + "\nSet one: /cap 56 0.1\nSet all: /cap all 0.05"
            )
        if len(args) != 2:
            return "✏️ Usage: /cap CHAIN_ID AMOUNT or /cap all AMOUNT"
        target, amount_text = args
        try:
            amount = Decimal(amount_text)
        except Exception:
            return "⚠️ Amount must be a number."
        if not amount.is_finite() or amount < 0 or amount > CAP_COMMAND_LIMIT:
            return (
                f"⚠️ Amount must be between 0 and {CAP_COMMAND_LIMIT}. "
                "For a higher cap, edit MAX_MINT_ETH or MAX_MINT_<chainId>."
            )
        if target.lower() == "all":
            chain_ids = list(self.chains)
        else:
            try:
                cid = int(target)
            except ValueError:
                return "⚠️ Chain ID must be an integer or 'all'."
            if cid not in self.chains:
                return f"⚠️ Chain {cid} is not enabled."
            chain_ids = [cid]
        value = to_wei(amount_text)
        for cid in chain_ids:
            self.max_values[cid] = value
            self.cap_overrides[cid] = value
        self._save()
        return "✅ Cap set to {amount} on {names}".format(
            amount=amount_text,
            names=", ".join(self.chains[cid].name for cid in chain_ids),
        )

    def command_reply(self, command, args=()):
        if command == "/status":
            names = ", ".join(chain.name for chain in self.chains.values())
            state = "⏸ PAUSED" if self.paused else "🟢 ON"
            dry = "🧪 on" if self.dry else "off"
            return (
                f"copymint is {state}\n"
                f"dry run: {dry}\n"
                f"⛓ chains: {names}\n"
                f"👀 watching {len(self.watch)} wallet(s), {len(self.minters)} minter(s)\n"
                f"🔁 copies waiting to retry: {len(self.pending)}\n"
                f"⚡ websocket live: {self._live_names()}"
            )
        if command == "/off":
            self.paused = True
            for key in list(self.pending):
                self.seen.add(key)
            self.pending.clear()
            self._save()
            return "⏸ Paused. New mints will be skipped, and pending retries have been dropped. Use /on to resume."
        if command == "/on":
            self.paused = False
            self._save()
            return "▶️ Resumed. Only mints seen from now on will be copied; paused mints are not replayed."
        if command == "/wallets":
            return self.wallets_text()
        if command == "/watch":
            return self.watch_command(args)
        if command == "/unwatch":
            return self.unwatch_command(args)
        if command == "/minters":
            lines = [f"👛 {number}. {account.address}" for number, account in enumerate(self.minters, 1)]
            if self.funder:
                lines.append(f"🏦 funder {self.funder.address}")
            return "\n".join(lines) or "📭 No minter wallets configured."
        if command == "/balance":
            return self.balances_text()
        if command == "/cap":
            return self.cap_command(args)
        if command == "/mint":
            return self.mint_command(args)
        if command == "/snipe":
            return self.snipe_command(args)
        if command == "/sendnfts":
            return self.sendnfts_command(args)
        if command in ("/help", "/start"):
            lines = ["Tap / to open the menu.\n"]
            lines.extend(f"/{name}  {text}" for name, text in MENU)
            return "\n".join(lines)
        return None

    def handle_command(self, update, chat_id):
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != str(chat_id):
            return
        parts = (message.get("text") or "").strip().split()
        if not parts:
            return
        reply = self.command_reply(parts[0].split("@")[0].lower(), parts[1:])
        if reply:
            log(f"telegram command {parts[0].split('@')[0]}")
            notify(reply)

    def control_loop(self):
        chat_id = env("TELEGRAM_CHAT_ID")
        try:
            publish_commands()
            log("telegram command menu registered")
        except Exception as exc:
            log(f"telegram command menu failed: {type(exc).__name__}")
        offset = None
        try:
            # Ignore commands sent before this process started.
            first = telegram_call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
            updates = first.get("result") or []
            if updates:
                offset = updates[-1]["update_id"] + 1
        except Exception as exc:
            log(f"telegram command startup failed: {type(exc).__name__}")
        while True:
            try:
                params = {"timeout": 25, "allowed_updates": json.dumps(["message"])}
                if offset is not None:
                    params["offset"] = offset
                for update in telegram_call("getUpdates", params).get("result") or []:
                    offset = update["update_id"] + 1
                    self.handle_command(update, chat_id)
            except Exception as exc:
                log(f"telegram command polling failed: {type(exc).__name__}")
                time.sleep(10)

    def balance(self, chain_id, token, who):
        if token == "native":
            return int(self.rpc.call(chain_id, "eth_getBalance", [who, "latest"]), 16)
        data = "0x70a08231" + word(who)
        return int(self.rpc.call(chain_id, "eth_call", [{"to": token, "data": data}, "latest"]), 16)

    def gas_price(self, chain_id):
        return int(self.rpc.call(chain_id, "eth_gasPrice", []), 16)

    def fees(self, chain_id):
        try:
            hist = self.rpc.call(chain_id, "eth_feeHistory", ["0x5", "latest", [50]])
            base = as_int(hist["baseFeePerGas"][-1])
            tips = []
            for row in hist.get("reward") or []:
                if row and row[0] not in (None, "0x"):
                    tips.append(as_int(row[0]))
            tip = max(tips) if tips else max(base // 10, 1)
        except Exception:
            base = self.gas_price(chain_id)
            tip = max(base // 10, 1)
        if tip < 1:
            tip = 1
        return base * 2 + tip, min(tip, base * 2 + tip)

    def send(self, chain_id, acct, to, data="0x", value=0, gas=None, fee=None, tip=None, nonce=None, wait=True):
        if self.dry:
            log(f"DRY {acct.address} {self.chains[chain_id].name} to {to} value {value} gas {gas if gas is not None else 'auto'}")
            return "dry", True
        if fee is None or tip is None:
            fee, tip = self.fees(chain_id)
        if gas is None:
            est = int(
                self.rpc.call(
                    chain_id,
                    "eth_estimateGas",
                    [{"from": acct.address, "to": to, "data": data, "value": hex(value)}],
                ),
                16,
            )
            gas = int(est * 1.3) + 20_000
        have = self.balance(chain_id, "native", acct.address)
        cost = value + gas * fee
        if have < cost:
            raise RuntimeError(f"short {have} < {cost}")
        if self.dry:
            log(f"DRY {acct.address[:8]} {self.chains[chain_id].name} value {value} gas {gas}")
            return "dry", True
        if nonce is None:
            nonce = int(self.rpc.call(chain_id, "eth_getTransactionCount", [acct.address, "pending"]), 16)
        signed = acct.sign_transaction(
            {
                "chainId": chain_id,
                "nonce": nonce,
                "gas": gas,
                "maxFeePerGas": fee,
                "maxPriorityFeePerGas": tip,
                "to": to_checksum_address(to),
                "value": value,
                "data": data,
                "type": 2,
            }
        )
        txh = self.rpc.call(chain_id, "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
        if not wait:
            return txh, False
        for _ in range(40):
            rec = self.rpc.call(chain_id, "eth_getTransactionReceipt", [txh])
            if rec:
                return txh, int(rec["status"], 16) == 1
            time.sleep(0.5)
        return txh, False

    def land(self, chain_id, acct, to, data, value, gas):
        if self.dry:
            txh, ok = self.send(chain_id, acct, to, data, value, gas)
            return txh, ok, [txh]
        fee, tip = self.fees(chain_id)
        nonce = int(self.rpc.call(chain_id, "eth_getTransactionCount", [acct.address, "pending"]), 16)
        start = int(self.rpc.call(chain_id, "eth_blockNumber", []), 16)
        txh, ok = self.send(chain_id, acct, to, data, value, gas, fee, tip, nonce, wait=False)
        if self.dry:
            return txh, True, [txh]
        # Keep every hash sent for this nonce. Either the original or the
        # replacement can land, so both have to be watched for a receipt.
        sent = [txh]
        bumped = False
        deadline = time.time() + 90
        while time.time() < deadline:
            hit = self.receipt_for(chain_id, sent)
            if hit:
                return hit[0], hit[1], sent
            now = int(self.rpc.call(chain_id, "eth_blockNumber", []), 16)
            if not bumped and now >= start + 2:
                bumped = True
                new_fee = max(int(fee * 2), fee + 1)
                new_tip = min(max(int(tip * 2), tip + 1), new_fee)
                try:
                    new_hash, _ = self.send(chain_id, acct, to, data, value, gas, new_fee, new_tip, nonce, wait=False)
                    sent.append(new_hash)
                    log(f"replaced {acct.address[:8]} {new_hash}")
                except Exception as exc:
                    if "nonce" not in str(exc).lower():
                        log(f"replace {acct.address[:8]} {exc}")
            time.sleep(0.6)
        return sent[-1], False, sent

    def receipt_for(self, chain_id, hashes):
        """Return (hash, success) for the first hash that has a receipt, else None."""
        for txh in hashes:
            rec = self.rpc.call(chain_id, "eth_getTransactionReceipt", [txh])
            if rec:
                return txh, int(rec["status"], 16) == 1
        return None

    def lifi_quote(self, src, dst, token, amount, sender, recipient):
        qs = urllib.parse.urlencode(
            {
                "fromChain": str(src),
                "toChain": str(dst),
                "fromToken": "0x0000000000000000000000000000000000000000" if token == "native" else token,
                "toToken": "0x0000000000000000000000000000000000000000",
                "fromAmount": str(amount),
                "fromAddress": sender,
                "toAddress": recipient,
                "slippage": "0.03",
            }
        )
        req = urllib.request.Request(LIFI + "?" + qs, headers={"accept": "application/json", "user-agent": "copymint-bot"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def sized_quote(self, src, dst, token, decimals, available, sender, recipient, need):
        reserve = 0 if token != "native" else 300_000 * max(self.gas_price(src), 1) * 3
        spendable = available - reserve
        if spendable <= 0:
            return None
        probe = max(spendable // 8, 10**decimals // 1000)
        probe = min(probe, spendable)
        try:
            quote = self.lifi_quote(src, dst, token, probe, sender, recipient)
        except Exception as exc:
            log(f"quote {src}->{dst} {exc}")
            return None
        out = int((quote.get("estimate") or {}).get("toAmount") or 0)
        if out <= 0:
            return None
        if out >= need and probe > 0:
            scaled = int(probe * need / out * 1.2) + 1
            scaled = min(max(scaled, 10**decimals // 1000), spendable)
            try:
                quote = self.lifi_quote(src, dst, token, scaled, sender, recipient)
                out = int((quote.get("estimate") or {}).get("toAmount") or 0)
            except Exception as exc:
                log(f"sized quote {exc}")
                return None
        if out < need:
            return None
        return quote

    def execute_quote(self, chain_id, acct, quote, token):
        approval = (quote.get("estimate") or {}).get("approvalAddress")
        amount = int((quote.get("action") or {}).get("fromAmount") or (quote.get("estimate") or {}).get("fromAmount") or 0)
        if approval and token != "native" and amount:
            allowance = int(
                self.rpc.call(
                    chain_id,
                    "eth_call",
                    [{"to": token, "data": "0xdd62ed3e" + word(acct.address) + word(approval)}, "latest"],
                ),
                16,
            )
            if allowance < amount:
                data = "0x095ea7b3" + word(approval) + hex(amount)[2:].rjust(64, "0")
                txh, ok = self.send(chain_id, acct, token, data, 0)
                log(f"approve {'OK' if ok else 'FAIL'} {txh}")
                if not ok:
                    return False
        tx = quote.get("transactionRequest") or {}
        if not tx.get("to") or not tx.get("data"):
            return False
        value = as_int(tx.get("value") or 0)
        gas = as_int(tx.get("gasLimit") or tx.get("gas") or 0)
        txh, ok = self.send(chain_id, acct, tx["to"], tx["data"], value, int(gas * 1.2) if gas else None)
        log(f"bridge {'OK' if ok else 'FAIL'} {txh}")
        return ok

    def holdings(self, holder):
        """Every non-zero native and stablecoin balance a wallet has, read in parallel."""
        jobs = []
        for src in self.chains:
            jobs.append((src, "native", 18))
            for token, decimals in STABLES.get(src, []):
                jobs.append((src, token, decimals))

        def fetch(job):
            src, token, decimals = job
            try:
                return src, token, decimals, self.balance(src, token, holder.address)
            except Exception as exc:
                log(f"balance {self.chains[src].name} {exc}")
                return src, token, decimals, 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(fetch, jobs))
        return [row for row in rows if row[3] > 0]

    def ensure(self, minter, chain_id, need):
        current = self.balance(chain_id, "native", minter.address)
        if self.dry:
            if current < need:
                log(f"DRY {minter.address[:8]} short {(need - current) / 1e18:.6f} on {self.chains[chain_id].name}, not bridging")
            return True
        if current >= need:
            return True
        if not self.bridge:
            return False
        short = need - current
        holders = [minter]
        if self.funder and self.funder.address.lower() != minter.address.lower():
            holders.append(self.funder)
        for holder in holders:
            for src, token, decimals, have in self.holdings(holder):
                if src == chain_id and token == "native":
                    continue
                quote = self.sized_quote(src, chain_id, token, decimals, have, holder.address, minter.address, short)
                if not quote:
                    continue
                label = "native" if token == "native" else token[:8]
                log(f"funding {minter.address[:8]} from {self.chains[src].name} {label}")
                if not self.execute_quote(src, holder, quote, token):
                    continue
                deadline = time.time() + self.bridge_timeout
                while time.time() < deadline:
                    if self.balance(chain_id, "native", minter.address) >= need:
                        return True
                    time.sleep(3)
                alert(f"🌉 bridge timed out for {minter.address[:8]} on {self.chains[chain_id].name}")
        return self.balance(chain_id, "native", minter.address) >= need

    def rewrite_watched(self, data, new):
        with self._watch_lock:
            watched = list(self.watch)
        for old in watched:
            data = rewrite(data, old, new)
        return data

    def minted_map(self, logs, recipients=None):
        # Take one reference to the set, since the Telegram thread can replace self.watch at any time.
        recipients = self.watch if recipients is None else recipients
        totals = {}
        for entry in logs:
            topics = [t.lower() for t in entry.get("topics") or []]
            collection = entry.get("address", "").lower()
            qty = 0
            if len(topics) >= 3 and topics[0] == TRANSFER and topics[1] == ZERO_TOPIC:
                if topic_address(topics[2]) in recipients:
                    qty = 1
            elif len(topics) >= 4 and topics[0] == TRANSFER_SINGLE and topics[2] == ZERO_TOPIC:
                if topic_address(topics[3]) in recipients:
                    qty = single_qty(entry.get("data") or "0x")
            elif len(topics) >= 4 and topics[0] == TRANSFER_BATCH and topics[2] == ZERO_TOPIC:
                if topic_address(topics[3]) in recipients:
                    qty = batch_qty(entry.get("data") or "0x")
            if qty > 0 and collection:
                totals[collection] = totals.get(collection, 0) + qty
        return totals

    def minted_for(self, logs, recipient):
        # Pass the recipient in rather than swapping self.watch: a /watch from Telegram
        # during the swap would be overwritten when the old set was restored.
        return self.minted_map(logs, {recipient.lower()})

    def _note_revert(self, error, data=None):
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            data = data or error.get("data") or error.get("output")
        elif error:
            message = str(error)
        self._revert = {"message": message, "data": data if isinstance(data, str) else ""}

    def simulate(self, chain_id, acct, target, data, value):
        self._revert = None
        call = {
            "from": acct.address,
            "to": to_checksum_address(target),
            "data": data,
            "value": hex(value),
            "gas": hex(1_500_000),
        }
        try:
            result = self.rpc.call(
                chain_id,
                "eth_simulateV1",
                [
                    {
                        "blockStateCalls": [
                            {"stateOverrides": {call["from"]: {"balance": SIM_BALANCE}}, "calls": [call]}
                        ],
                        "validation": False,
                        "traceTransfers": True,
                    },
                    "latest",
                ],
            )
            status = 1
            gas_used = 0
            if isinstance(result, list) and result and isinstance(result[0], dict):
                calls = result[0].get("calls") or []
                if calls:
                    if calls[0].get("error"):
                        status = 0
                        self._note_revert(calls[0].get("error"), calls[0].get("returnData") or calls[0].get("data"))
                    elif calls[0].get("status") is not None:
                        status = as_int(calls[0].get("status"))
                    gas_used = as_int(calls[0].get("gasUsed"))
            if status == 0:
                return None
            logs = []
            collect_logs(result, logs)
            return logs, gas_used or 400_000
        except Exception as exc:
            if not method_missing(exc):
                text = str(exc).lower()
                if "revert" in text or "execution" in text:
                    self._note_revert(str(exc))
                    return None
                if not method_missing(exc):
                    log(f"simulate {exc}")
                    return None
        try:
            result = self.rpc.call(
                chain_id,
                "debug_traceCall",
                [
                    call,
                    "latest",
                    {
                        "tracer": "callTracer",
                        "tracerConfig": {"withLog": True},
                        "stateOverrides": {call["from"]: {"balance": SIM_BALANCE}},
                    },
                ],
            )
        except Exception as exc:
            if method_missing(exc):
                return "unsupported"
            if "revert" in str(exc).lower():
                self._note_revert(str(exc))
                return None
            return "unsupported"
        if isinstance(result, dict) and result.get("error"):
            self._note_revert(result.get("error"), result.get("output"))
            return None
        logs = []
        collect_logs(result, logs)
        gas_used = as_int(result.get("gasUsed")) if isinstance(result, dict) else 0
        if not logs:
            return "unsupported"
        return logs, gas_used or 400_000

    def candidates(self, tx, want=None):
        calls = expand_calls(tx["to"], tx.get("input") or "0x", int(tx.get("value") or "0", 16))
        collections = {c.lower() for c in (want or {})}
        kept = []
        for target, data, value in calls:
            if len(data) < 10:
                # A plain ETH send to the NFT contract can be the mint (a receive() or fallback mint).
                # Simulation still has to show the same NFTs before anything is sent.
                if value > 0 and target.lower() in collections:
                    kept.append((target, "0x", value))
                continue
            if data[:10].lower() in NON_MINT:
                continue
            # A call that looks signed or allowlisted is kept. Whether a minter is eligible is
            # decided by simulating the call from that minter, so an eligible wallet can still mint
            # while the others are skipped.
            kept.append((target, data, value))
        return kept

    def collection_name(self, chain_id, collection):
        """The collection's name() if it has one, cached so each contract is asked once."""
        key = f"{chain_id}:{collection.lower()}"
        if key not in self._names:
            name = ""
            try:
                raw = self.rpc.call(chain_id, "eth_call", [{"to": to_checksum_address(collection), "data": "0x06fdde03"}, "latest"])
                name = decode_abi_string(raw)
            except Exception:
                pass
            self._names[key] = name
        return self._names[key]

    def mint_cost(self, chain_id, txh, value):
        """Mint price plus gas for one landed transaction, in wei, or None if the receipt can't be read."""
        try:
            receipt = self.rpc.call(chain_id, "eth_getTransactionReceipt", [txh])
            if not receipt:
                return None
            price = as_int(receipt.get("effectiveGasPrice") or receipt.get("gasPrice"))
            return as_int(receipt.get("gasUsed")) * price + int(value or 0)
        except Exception:
            return None

    def send_summary(self, chain_id, tx, want, item, gave_up=False, note="", kind="copy"):
        """One Telegram message for a finished mint: who copied, who didn't, why, and what it cost."""
        chain = self.chains[chain_id]
        report = item.get("report") or {}
        total = int(item.get("total") or len(self.minters))
        minted = [r for r in report.values() if r.get("status") == "minted"]
        dry = [r for r in report.values() if r.get("status") == "dry"]
        quiet = [r for r in report.values() if r.get("status") == "quiet"]
        problems = [r for r in report.values() if r.get("status") in ("failed", "retrying")]
        if not minted and not dry and not problems and not note and not gave_up:
            log(f"{chain.name} {tx['hash']}: nothing to report, every minter had already minted this collection")
            return
        per_minter = sum(want.values())
        sniped = kind == "snipe"
        if minted and len(minted) == total:
            title, mood = ("🎯 Sniped" if sniped else "✅ Copied"), "ok"
        elif minted:
            title, mood = ("⚠️ Partially sniped" if sniped else "⚠️ Partially copied"), "partial"
        elif dry:
            title, mood = "🧪 Dry run", "dry"
        elif gave_up:
            title, mood = "🛑 Gave up", "bad"
        else:
            title, mood = ("❌ Missed" if sniped else "❌ Not copied"), "bad"
        names = [self.collection_name(chain_id, c) for c in want]
        if len(want) == 1:
            collection_rows = [
                ("Collection", esc(names[0]) if names[0] else None),
                ("CA", code(next(iter(want))) + f" ×{per_minter}"),
            ]
        else:
            lines = []
            for (collection, amount), name in zip(want.items(), names):
                lines.append((esc(name) + "\n" if name else "") + f"{code(collection)} ×{amount}")
            collection_rows = [("Collections", "\n" + "\n".join(lines))]
        reasons = {}
        for record in problems:
            reasons[record.get("reason") or "did not land"] = reasons.get(record.get("reason") or "did not land", 0) + 1
        skipped_text = "\n".join(f"• {count} minter(s): {esc(reason)}" for reason, count in reasons.items())
        costs = [self.mint_cost(chain_id, r["txh"], r.get("value")) for r in minted if r.get("txh")]
        # Attempts that reverted still cost gas, so they count toward the total.
        for record in report.values():
            costs += [self.mint_cost(chain_id, h, 0) for h in record.get("failed_txs") or []]
        costs = [c for c in costs if c is not None]
        done_count = len(minted) or len(dry)
        rows = collection_rows + [
            ("Minted" if minted else None, f"{per_minter * len(minted)} NFTs across {len(minted)} minter(s)" if minted else None),
            ("Would mint" if dry and not minted else None, f"{per_minter * len(dry)} NFTs across {len(dry)} minter(s)" if dry and not minted else None),
            ("Minters minted" if sniped else "Minters copied", f"{done_count} of {total}"),
            ("Skipped", ("\n" + skipped_text) if skipped_text else None),
            ("Already minted", f"{len(quiet)} minter(s), limit reached" if quiet and (minted or problems) else None),
            ("Note", esc(note) if note else None),
            ("Total cost", fmt_native(sum(costs), chain_id) if costs else None),
            ("Source wallet", code(tx["from"]) if tx["from"] != "manual" else None),
            ("Source tx", code(tx["hash"]) if tx["from"] != "manual" else None),
        ]
        rows = [(label, value) for label, value in rows if label]
        plain = f"{title} on {chain.name}: {done_count} of {total} minters, source {tx['hash']}"
        alert(plain, card(f"{title} · {chain.name}", *rows))

    def remember_minted(self, chain_id, minter, want):
        """Note that this minter has minted these collections, so a later refusal is expected."""
        who = minter.lower()
        for collection in want:
            key = f"{chain_id}:{collection.lower()}"
            holders = self.minted_before.setdefault(key, [])
            if who not in holders:
                holders.append(who)
        while len(self.minted_before) > 2000:
            self.minted_before.pop(next(iter(self.minted_before)))
        self._save()

    def minted_before_any(self, chain_id, minter, want):
        who = minter.lower()
        return any(who in self.minted_before.get(f"{chain_id}:{c.lower()}", ()) for c in want)

    def explain_no_mint(self, tx):
        """Say why none of the calls in a source transaction could be copied."""
        calls = expand_calls(tx["to"], tx.get("input") or "0x", int(tx.get("value") or "0", 16))
        if not calls:
            return "the transaction has no calls"
        parts = []
        for target, data, value in calls[:3]:
            where = f"to {short_address(target)}"
            if len(data) < 10:
                parts.append(f"plain ETH send with no call data {where}")
            elif data[:10].lower() == SEADROP_MINT_SIGNED:
                parts.append(
                    f"OpenSea SeaDrop signed mint {where}: the project's server signs it for one wallet, "
                    "so it cannot be repeated from another wallet"
                )
            elif looks_signed(data):
                parts.append(f"signed or allowlist mint (call {data[:10]}) {where}; the signature or proof only works for the source wallet")
            elif data[:10].lower() in NON_MINT:
                parts.append(f"not a mint (call {data[:10]}) {where}")
            else:
                parts.append(f"call {data[:10]} {where}")
        return "; ".join(parts)

    def fallback(self, chain_id, acct, options, want):
        simple = []
        for target, data, value in options:
            signature = data[:10].lower()
            if signature in MINT_OK or target.lower() in want:
                simple.append((target, self.rewrite_watched(data, acct.address), value))
        if len(simple) != 1:
            return None
        target, data, value = simple[0]
        if value > self.cap(chain_id):
            value = 0
        try:
            est = int(
                self.rpc.call(
                    chain_id,
                    "eth_estimateGas",
                    [{"from": acct.address, "to": target, "data": data, "value": hex(value)}],
                ),
                16,
            )
        except Exception:
            return None
        log(f"{acct.address[:8]} RPC cannot show mint logs, using the gas estimate")
        return value, target, data, est

    def read_uint(self, chain_id, target, signature):
        try:
            raw = self.rpc.call(chain_id, "eth_call", [{"to": to_checksum_address(target), "data": selector(signature)}, "latest"])
            if isinstance(raw, str) and len(raw) >= 66:
                return int(raw[:66], 16)
        except Exception:
            pass
        return None

    def supply_state(self, chain_id, target):
        """Minted count, supply cap and paused flag read from the contract, cached for a few seconds."""
        cache = self.__dict__.setdefault("_supply_cache", {})
        key = (chain_id, target.lower())
        hit = cache.get(key)
        if hit and time.time() - hit[0] < 5:
            return hit[1]
        minted = None
        for sig in ("totalSupply()", "totalMinted()"):
            value = self.read_uint(chain_id, target, sig)
            if value is not None:
                minted = value if minted is None else max(minted, value)
        cap = None
        for sig in ("maxSupply()", "MAX_SUPPLY()", "maxTokens()", "MAX_TOKENS()", "collectionSize()", "maxTotalSupply()", "supplyCap()"):
            value = self.read_uint(chain_id, target, sig)
            if value:
                cap = value
                break
        paused = self.read_uint(chain_id, target, "paused()")
        state = {"minted": minted, "cap": cap, "paused": bool(paused)}
        cache[key] = (time.time(), state)
        return state

    def explain_revert(self, chain_id, reverts):
        """The real reason a simulated mint reverted. reverts is a list of (target, revert info)."""
        reasons = []
        for target, info in reverts:
            state = self.supply_state(chain_id, target)
            if state["cap"] and state["minted"] is not None and state["minted"] >= state["cap"]:
                return f"sold out: {state['minted']} of {state['cap']} minted"
            if state["paused"]:
                reasons.append("the mint is paused")
            info = info or {}
            text = decode_revert(info.get("data"), info.get("message"))
            if text:
                reasons.append(classify_reason(text))
        return reasons[0] if reasons else ""

    def pick(self, chain_id, acct, options, want):
        saw_real = False
        unsupported = False
        best = None
        reverted = 0
        different = 0
        reverts = []
        self.last_pick_reason = ""
        self.last_pick_kind = ""
        for target, raw, hinted in options:
            data = self.rewrite_watched(raw, acct.address)
            values = []
            for value in (0, hinted):
                if value not in values and value <= self.cap(chain_id):
                    values.append(value)
            for value in values:
                result = self.simulate(chain_id, acct, target, data, value)
                if result == "unsupported":
                    unsupported = True
                    continue
                saw_real = True
                if not result:
                    reverted += 1
                    reverts.append((target, getattr(self, "_revert", None)))
                    continue
                logs, gas_used = result
                if self.minted_for(logs, acct.address) != want:
                    different += 1
                    continue
                if best is None or value < best[0]:
                    best = (value, target, data, max(gas_used, 80_000))
                break
        if best:
            return best
        if different:
            self.last_pick_kind = "different"
            self.last_pick_reason = "the simulated mint did not give the same NFTs as the source wallet got"
        elif reverted:
            self.last_pick_kind = "reverted"
            self.last_pick_reason = "the mint reverted and the contract gave no reason"
            try:
                explained = self.explain_revert(chain_id, reverts)
            except Exception as exc:
                log(f"explain revert {exc}")
                explained = ""
            if explained:
                self.last_pick_reason = explained
            elif any(looks_signed(raw) or raw[:10].lower() == SEADROP_MINT_SIGNED for _t, raw, _v in options):
                self.last_pick_reason = "not eligible: the mint needs a signature or allowlist proof made for the source wallet"
        if not saw_real and unsupported:
            self.last_pick_kind = "unsupported"
            self.last_pick_reason = "your RPC cannot simulate; set a node with eth_simulateV1 or debug_traceCall"
            if self.allow_estimate:
                return self.fallback(chain_id, acct, options, want)
            log(
                f"{self.chains[chain_id].name} RPC cannot simulate, skipping. "
                f"Set RPC_{chain_id} to a node with eth_simulateV1 or debug_traceCall, "
                "or set ALLOW_ESTIMATE_FALLBACK=true to send on a gas estimate alone."
            )
        return None

    def copy(self, chain_id, tx, want, options=None, wallets=None):
        key = f"{chain_id}:{tx['hash']}"
        if key in self.seen:
            return
        item = self.pending.get(key) or {
            "chain": chain_id,
            "hash": tx["hash"],
            "from": tx["from"],
            "to": tx["to"],
            "input": tx.get("input") or "0x",
            "value": int(tx.get("value") or "0", 16),
            "want": want,
            "attempts": 0,
            "done": [],
            "inflight": {},
        }
        if wallets is not None and not item.get("wallets"):
            item["wallets"] = [w.lower() for w in wallets]
        accounts = [a for a in self.minters if not item.get("wallets") or a.address.lower() in item["wallets"]]
        item["total"] = len(accounts)
        if options is not None and not item.get("options"):
            item["options"] = [list(option) for option in options]
        if item.get("options"):
            # A /mint command saves the calls it built, so retries use the same ones.
            options = [tuple(option) for option in item["options"]]
        else:
            options = self.candidates(tx, want)
        chain = self.chains[chain_id]
        qty = ", ".join(f"{collection[:8]} x{amount}" for collection, amount in want.items())
        if item["attempts"] == 0:
            log(f"🎯 mint by {tx['from']} on {chain.name} -> {tx['to']} {qty} {tx['hash']}")
        if not options:
            self.send_summary(chain_id, tx, want, item, note="could not copy: " + self.explain_no_mint(tx))
            self.seen.add(key)
            self.pending.pop(key, None)
            self._save()
            return
        item["attempts"] = int(item.get("attempts") or 0) + 1
        temporary = False
        done = set(a.lower() for a in item.get("done") or [])
        inflight = item.get("inflight") or {}
        report = item.setdefault("report", {})
        for acct in accounts:
            who = acct.address.lower()
            if who in done:
                continue
            previous = inflight.get(who)
            if previous:
                # Older state files hold one hash as a string, newer ones a list.
                hashes = [previous] if isinstance(previous, str) else list(previous)
                hit = self.receipt_for(chain_id, hashes)
                if hit is None:
                    temporary = True
                    continue
                inflight.pop(who, None)
                if hit[1]:
                    done.add(who)
                    self.remember_minted(chain_id, acct.address, want)
                    self.record_holdings_later(chain_id, acct.address, hit[0])
                    report[who] = {"status": "minted", "txh": hit[0], "value": (report.get(who) or {}).get("value", 0)}
                    log(f"✅ OK {acct.address} {hit[0]}")
                    continue
            chosen = self.pick(chain_id, acct, options, want)
            if not chosen and getattr(self, "last_pick_kind", "") == "reverted" and self.minted_before_any(chain_id, acct.address, want):
                # The contract refuses a minter that already minted this collection, most likely a per-wallet limit.
                log(f"skip {acct.address[:8]} already minted this collection and the contract now refuses, likely at its limit")
                done.add(who)
                report[who] = {"status": "quiet"}
                continue
            if not chosen:
                reason = getattr(self, "last_pick_reason", "") or "no working mint call found"
                log(f"skip {acct.address[:8]} not copied on {chain.name}: {reason}")
                report[who] = {"status": "failed", "reason": reason}
                continue
            value, target, data, gas_used = chosen
            gas = int(gas_used * 1.25) + 20_000
            fee, _tip = self.fees(chain_id)
            if not self.ensure(acct, chain_id, value + gas * fee):
                log(f"💸 skip {acct.address[:8]} not enough on {chain.name} and no route")
                report[who] = {"status": "retrying", "reason": "not enough funds and no bridge route"}
                temporary = True
                continue
            try:
                txh, ok, sent = self.land(chain_id, acct, target, data, value, gas)
            except Exception as exc:
                log(f"send {acct.address[:8]} {exc}")
                report[who] = {"status": "retrying", "reason": "sending the mint failed"}
                temporary = True
                continue
            if ok:
                done.add(who)
                if not self.dry:
                    self.remember_minted(chain_id, acct.address, want)
                    self.record_holdings_later(chain_id, acct.address, txh)
                if self.dry:
                    log(f"🧪 DRY {acct.address} would mint on {chain.name}, not broadcast")
                    report[who] = {"status": "dry"}
                else:
                    log(f"✅ OK {acct.address} {txh}")
                    report[who] = {"status": "minted", "txh": txh, "value": value}
            else:
                inflight[who] = sent
                report[who] = {"status": "retrying", "reason": "did not land in time", "value": value}
                temporary = True
                log(f"pending {acct.address} {txh}")
        item["done"] = sorted(done)
        item["inflight"] = inflight
        if len(done) == len(accounts) or not temporary:
            self.send_summary(chain_id, tx, want, item)
            self.seen.add(key)
            self.pending.pop(key, None)
        elif item["attempts"] >= MAX_ATTEMPTS:
            self.send_summary(chain_id, tx, want, item, gave_up=True, note=f"gave up after {item['attempts']} attempts")
            self.seen.add(key)
            self.pending.pop(key, None)
        else:
            self.pending[key] = item
        self._save()
        return len(done)

    def claim_collection_cooldown(self, chain_id, tx_hash, want):
        """Atomically reserve every collection in one source mint event.

        A multi-collection transaction is one opportunity: if any collection is cooling
        down from a different transaction, none are claimed and the whole event is
        suppressed, so a transaction is never copied in part. The same source
        transaction may continue through retry processing.
        Returns (allowed, blocked), where blocked is a list of (key, seconds left).
        """
        if self.mint_cooldown_seconds <= 0 or not want:
            return True, []
        now = time.time()
        tx_hash = str(tx_hash).lower()
        keys = [f"{chain_id}:{collection.lower()}" for collection in want]
        with self._collection_lock:
            for key, record in list(self.collection_cooldowns.items()):
                if float(record.get("until", 0)) <= now:
                    self.collection_cooldowns.pop(key, None)
            blocked = []
            for key in keys:
                record = self.collection_cooldowns.get(key)
                if record and record.get("tx") != tx_hash:
                    blocked.append((key, max(0, int(record["until"] - now))))
            if not blocked:
                expiry = now + self.mint_cooldown_seconds
                for key in keys:
                    record = self.collection_cooldowns.get(key)
                    if not record or record.get("tx") != tx_hash:
                        self.collection_cooldowns[key] = {"until": expiry, "tx": tx_hash}
        if not blocked:
            self._save()
            return True, []
        return False, blocked

    def release_collection_cooldown(self, chain_id, tx_hash, want):
        """Give back a claim when the copy never went through, so a later wallet can still trigger it."""
        tx_hash = str(tx_hash).lower()
        with self._collection_lock:
            for collection in want:
                key = f"{chain_id}:{collection.lower()}"
                if (self.collection_cooldowns.get(key) or {}).get("tx") == tx_hash:
                    del self.collection_cooldowns[key]
        self._save()

    def watch_topics(self):
        """The three log filters that match a mint to any watched wallet."""
        with self._watch_lock:
            watched_addresses = list(self.watch)
        watched = ["0x" + word(addr) for addr in watched_addresses]
        return [
            [TRANSFER, ZERO_TOPIC, watched],
            [TRANSFER_SINGLE, None, ZERO_TOPIC, watched],
            [TRANSFER_BATCH, None, ZERO_TOPIC, watched],
        ]

    def logs_for(self, chain_id, start, end):
        found = []
        for topics in self.watch_topics():
            found.extend(
                self.rpc.call(
                    chain_id,
                    "eth_getLogs",
                    [{"fromBlock": hex(start), "toBlock": hex(end), "topics": topics}],
                )
                or []
            )
        return found

    def take_range(self, chain_id, start, end):
        try:
            entries = self.logs_for(chain_id, start, end)
        except Exception as exc:
            if start == end:
                log(f"{self.chains[chain_id].name} skip block {start} {exc}")
                return end
            mid = (start + end) // 2
            reached = self.take_range(chain_id, start, mid)
            if reached < mid:
                return reached
            return self.take_range(chain_id, mid + 1, end)
        grouped = {}
        for entry in entries:
            grouped.setdefault(entry["transactionHash"], []).append(entry)
        ordered = sorted(grouped.items(), key=lambda item: (as_int(item[1][0].get("blockNumber")), as_int(item[1][0].get("transactionIndex"))))
        for txh, logs in ordered:
            self.handle_tx(chain_id, txh, logs)
        return end

    def handle_tx(self, chain_id, txh, logs):
        """Copy one transaction if it is a mint by a watched wallet. Polling and the websocket both land here."""
        want = self.minted_map(logs)
        if not want:
            return
        key = f"{chain_id}:{txh}"
        with self._copy_lock:
            # Checked inside the lock, so a mint seen by both paths is copied once.
            if key in self.seen:
                return
            try:
                tx = self.rpc.call(chain_id, "eth_getTransactionByHash", [txh])
            except Exception as exc:
                log(f"tx {txh} {exc}")
                return
            if not tx or not tx.get("to"):
                return
            sender = (tx.get("from") or "").lower()
            if sender not in self.watch or sender in self.own:
                return
            if int(tx.get("value") or "0", 16) > self.cap(chain_id):
                log(f"skip {txh} value {int(tx.get('value'), 16) / 1e18:.4f} over cap")
                self.seen.add(key)
                self._save()
                return
            if self.paused:
                self.seen.add(key)
                self._save()
                alert(
                    f"⏸ paused; skipped mint by {sender} on {self.chains[chain_id].name} {txh}",
                    card(
                        f"⏸ Paused, mint skipped · {self.chains[chain_id].name}",
                        ("Wallet", code(sender)),
                        ("Collection (CA)", "\n" + collections_html(want)),
                        ("Tx", code(txh)),
                    ),
                )
                return
            allowed, blocked = self.claim_collection_cooldown(chain_id, txh, want)
            if not allowed:
                names = ", ".join(item[0].split(":", 1)[1] for item in blocked)
                wait = max((item[1] for item in blocked), default=0)
                log(f"skip {txh}: collection cooldown active for {names}; about {wait}s remaining")
                self.seen.add(key)
                self._save()
                return
            try:
                finished = self.copy(chain_id, tx, want)
            except Exception as exc:
                log(f"tx {txh} {exc}")
                return
            if not finished and key not in self.pending:
                # Nothing was minted and nothing is retrying, so do not hold the collection for nobody.
                self.release_collection_cooldown(chain_id, txh, want)

    def _live_names(self):
        live = getattr(self, "ws_live", {})
        names = [chain.name for cid, chain in self.chains.items() if live.get(cid)]
        return ", ".join(names) if names else "none"

    def _watch_changed(self):
        """Tell the websocket threads to resubscribe, since their filters hold the old wallet list."""
        self.watch_version = getattr(self, "watch_version", 0) + 1

    def chains_due(self, now):
        """Chains to poll this round. A chain with a live websocket is only polled as a backup."""
        due = []
        for cid in self.chains:
            if self.ws_live.get(cid) and now - self.last_poll.get(cid, 0) < self.wss_backup:
                continue
            self.last_poll[cid] = now
            due.append(cid)
        return due

    def advance_head(self, chain_id):
        """Move the scan position to the latest block without reading logs. The socket covers these blocks."""
        head = int(self.rpc.call(chain_id, "eth_blockNumber", []), 16)
        last = self.heads.get(chain_id)
        if last is None or head > last:
            self.heads[chain_id] = head
            self._save()

    def poll_chain(self, chain_id):
        """One polling round for a chain. With a healthy socket this is a single cheap request."""
        if self.ws_live.get(chain_id) and chain_id not in self.ws_gap and not self.wss_backup_scan:
            self.advance_head(chain_id)
            return
        gap = chain_id in self.ws_gap
        self.ws_gap.discard(chain_id)
        try:
            self.scan(chain_id)
        except Exception:
            if gap:
                self.ws_gap.add(chain_id)
            raise

    def ws_message(self, chain_id, raw):
        """Handle one websocket frame. Only the first log of each transaction starts any work."""
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if message.get("method") != "eth_subscription":
            return
        entry = (message.get("params") or {}).get("result") or {}
        txh = entry.get("transactionHash")
        if not txh or entry.get("removed"):
            return
        key = f"{chain_id}:{txh}"
        with self._ws_lock:
            if key in self._ws_recent or key in self.seen:
                return
            self._ws_recent[key] = None
            if len(self._ws_recent) > 2000:
                del self._ws_recent[next(iter(self._ws_recent))]
        self._pool.submit(self.process_ws_tx, chain_id, txh)

    def process_ws_tx(self, chain_id, txh):
        """Read the whole receipt, so a mint of 10 NFTs is copied as 10 and not as the first log's 1."""
        try:
            receipt = None
            for _ in range(5):
                receipt = self.rpc.call(chain_id, "eth_getTransactionReceipt", [txh])
                if receipt:
                    break
                time.sleep(0.2)
            if not receipt:
                log(f"{self.chains[chain_id].name} no receipt yet for {txh}, the next poll will catch it")
                self.ws_gap.add(chain_id)  # the next poll does a full scan instead of the cheap tick
                return
            if as_int(receipt.get("status", "0x1")) != 1:
                return
            self.handle_tx(chain_id, txh, receipt.get("logs") or [])
        except Exception as exc:
            log(f"websocket tx {txh} {type(exc).__name__}: {exc}")

    def ws_subscribe(self, ws, chain_id):
        filters = self.watch_topics()
        for number, topics in enumerate(filters, 1):
            ws.send(json.dumps({"jsonrpc": "2.0", "id": number, "method": "eth_subscribe", "params": ["logs", {"topics": topics}]}))
        confirmed = 0
        deadline = time.time() + 15
        while confirmed < len(filters):
            if time.time() > deadline:
                raise RuntimeError("subscribe timed out")
            raw = ws.recv()
            if not raw:
                raise ConnectionError("closed while subscribing")
            message = json.loads(raw)
            if message.get("error"):
                raise RuntimeError(f"subscribe refused: {str(message['error'])[:120]}")
            if message.get("id") in range(1, len(filters) + 1) and message.get("result"):
                confirmed += 1
            else:
                self.ws_message(chain_id, raw)

    def ws_loop(self, chain_id):
        try:
            import websocket  # the websocket-client package
        except ImportError:
            log("websocket-client is not installed, polling only")
            return
        chain = self.chains[chain_id]
        delay = 1
        failures = 0
        while True:
            version = self.watch_version
            resubscribe = False
            ws = None
            try:
                ws = websocket.create_connection(chain.wss, timeout=10)
                ws.settimeout(5)
                self.ws_subscribe(ws, chain_id)
                self.ws_gap.add(chain_id)
                self.ws_live[chain_id] = True
                failures = 0
                delay = 1
                log(f"{chain.name} websocket live")
                last_ping = time.time()
                while True:
                    if self.watch_version != version:
                        log(f"{chain.name} watch list changed, resubscribing")
                        resubscribe = True
                        break
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        raw = None
                    else:
                        if not raw:
                            raise ConnectionError("closed")
                    if time.time() - last_ping > 20:
                        ws.ping()
                        last_ping = time.time()
                    if raw:
                        self.ws_message(chain_id, raw)
            except Exception as exc:
                # Only the error type is logged, since messages can carry the URL and its key.
                log(f"{chain.name} websocket {type(exc).__name__}, polling covers it")
                failures += 1
                if failures == 5:
                    notify(f"{chain.name} websocket keeps failing. The bot is polling that chain instead.")
            finally:
                self.ws_live[chain_id] = False
                self.ws_gap.add(chain_id)
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if resubscribe:
                continue
            time.sleep(delay)
            delay = min(delay * 2, 30)

    def scan(self, chain_id):
        head = int(self.rpc.call(chain_id, "eth_blockNumber", []), 16)
        last = self.heads.get(chain_id)
        if last is None:
            self.heads[chain_id] = head
            self._save()
            log(f"{self.chains[chain_id].name} watching from {head}")
            return
        if head < last:
            last = head - 1
        if head <= last:
            return
        cursor = last + 1
        while cursor <= head:
            end = min(head, cursor + LOG_CHUNK - 1)
            reached = self.take_range(chain_id, cursor, end)
            self.heads[chain_id] = reached
            self._save()
            if reached < end:
                break
            cursor = reached + 1

    def retry_pending(self):
        if self.paused:
            return
        for key, item in list(self.pending.items()):
            chain_id = int(item["chain"])
            if chain_id not in self.chains:
                continue
            tx = {
                "hash": item["hash"],
                "from": item["from"],
                "to": item["to"],
                "input": item["input"],
                "value": hex(int(item["value"])),
            }
            try:
                with self._copy_lock:
                    self.copy(chain_id, tx, {k.lower(): int(v) for k, v in item["want"].items()})
            except Exception as exc:
                log(f"retry {item['hash']} {exc}")

    def run(self):
        names = ", ".join(c.name for c in self.chains.values())
        log(f"watching {len(self.watch)} wallet(s) on {names}")
        log(f"minters {', '.join(a.address for a in self.minters)}")
        if self.funder:
            log(f"funder {self.funder.address}")
        log(f"bridge {'on' if self.bridge else 'off'} dry {'on' if self.dry else 'off'}")
        state = "⏸ PAUSED" if self.paused else "🟢 on"
        notify(
            f"🤖 copymint-bot started ({state}): {len(self.watch)} wallet(s) on {names}, "
            f"dry run {'🧪 on' if self.dry else 'off'}"
        )
        if env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"):
            threading.Thread(target=self.control_loop, daemon=True).start()
            log("telegram controls enabled")
        streaming = [cid for cid, chain in self.chains.items() if chain.wss]
        if streaming:
            log(f"websocket mode on for {', '.join(self.chains[cid].name for cid in streaming)}; backup tick every {self.wss_backup:g}s")
            for cid in streaming:
                threading.Thread(target=self.ws_loop, args=(cid,), daemon=True).start()
        while True:
            for cid in self.chains_due(time.time()):
                try:
                    self.poll_chain(cid)
                except Exception as exc:
                    log(f"{self.chains[cid].name} {exc}")
            try:
                self.retry_pending()
            except Exception as exc:
                log(f"retry {exc}")
            time.sleep(self.poll)


if __name__ == "__main__":
    Bot().run()
