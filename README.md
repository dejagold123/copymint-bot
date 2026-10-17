# Copymint bot

> **Use at your own risk.** This bot signs and sends real transactions from the private keys you give it. Use fresh wallets that hold only what you can afford to lose, never your main wallet. Start with `DRY_RUN=true`. Mints can fail, copy the wrong thing, or be scams, and nothing here is financial advice. Never commit a `.env` file or share your keys, RPC URLs or Telegram bot token.

**New here? Follow the [step-by-step setup guide](GUIDE.md).**

Watches one or more wallets. When one of them mints an NFT to itself, your wallets send that same mint for the same quantity.

A copy is taken from the mint call only, including a mint buried in a multicall. The bot simulates the copy first and broadcasts only when that simulation mints the same collection and quantity. A send that does not land is retried. If it is still pending after two blocks, it is replaced once with a higher fee.

If a minter does not have enough native coin on that chain, the bot swaps or bridges funds in with [LI.FI](https://li.quest) from another chain, then mints. A separate funder wallet can pay for the minters.

It starts at the current block. It does not replay old mints.

Allowlist and signed mints are tried too, but each minter is simulated on its own. A minter that is eligible on-chain (for example a wallet on the contract's allowlist) mints, and the others are skipped as "not eligible". A signature or Merkle proof made for the watched wallet still will not work for your minters, so those mints fail for everyone.

When a minter's simulated mint reverts, the bot reads the contract to say why: sold out (minted count against its supply cap), paused, the contract's own revert message, or a known error such as mint not open, wallet limit, not allowlisted or wrong payment. If the contract gives no reason and exposes no supply, the message says so rather than guessing.

## Chains

Ethereum, Optimism, BNB Chain, Polygon, Base, Arbitrum, HyperEVM, and Robinhood. Set `CHAINS` to the ones you want.

## GitHub

From this folder:

```bash
git init
git add .
git commit -m "copy mint bot"
```

Create a new empty repository on GitHub, then:

```bash
git remote add origin git@github.com:YOUR_USER/copymint-bot.git
git branch -M main
git push -u origin main
```

Do not commit a `.env` file. Keys belong in Railway variables only.

## Railway

1. [railway.app](https://railway.app) → New Project → Deploy from GitHub repo → select this repo.
2. Open the service → Variables, and add:

| Variable | Example |
|---|---|
| `WATCH_ADDRESSES` | `0xabc...,0xdef...` |
| `MINTER_PRIVATE_KEYS` | `0xkey1,0xkey2` |
| `FUNDER_PRIVATE_KEY` | optional, the wallet that holds funds on other chains |
| `CHAINS` | `1,10,56,137,8453,42161,999,4663` |
| `RPC_1` | your Ethereum RPC |
| `RPC_4663` | your Robinhood RPC |
| `RPC_8453` | Base, and the same for any chain you leave unset only if you accept the public RPC |
| `ENABLE_BRIDGE` | `true` |
| `MAX_MINT_ETH` | `0.05`, the cap in each chain's native coin |
| `MAX_MINT_<chainId>` | optional per-chain cap, for example `MAX_MINT_56=0.1` for BNB Chain |
| `MINT_COOLDOWN_SECONDS` | `0` (off) by default. Set a number of seconds to ignore repeat mints of the same collection on the same chain. Leave it off if watched wallets mint several times in a row, because every repeat would be skipped |
| `WSS_<chainId>` | optional `wss://` endpoint (for example `WSS_8453`) for instant detection; see WebSocket mode |
| `WSS_BACKUP_SECONDS` | `30` by default; how often the bot checks the latest block number on a chain whose socket is live. It is one cheap request, and a missed stretch is scanned once if the socket drops |
| `WSS_BACKUP_SCAN` | `false` by default; set `true` to also re-scan blocks for mint logs on that tick, which costs far more on a fast chain |
| `LOGS_MAX_BLOCKS` | `10` by default, the most Alchemy's free plan allows per `eth_getLogs` request; raise it on a plan that allows more |
| `TELEGRAM_BOT_TOKEN` | optional, from @BotFather |
| `TELEGRAM_CHAT_ID` | optional, the chat that receives alerts |
| `ALLOW_ESTIMATE_FALLBACK` | `false` by default, see Simulation below |
| `STATE_PATH` | `/data/state.json` |

3. Settings → add a Volume, mount path `/data`. That keeps the last processed block across restarts. Without it, a restart can miss mints that happened while it was down.
4. The start command is `python bot.py`. Redeploy.

Public RPCs are filled in for any `RPC_<chainId>` you leave empty. They rate-limit. Set Alchemy or an equivalent URL for any chain you really care about.

Watch the Deploy logs. A healthy start looks like:

```
watching 1 wallet(s) on Ethereum, Robinhood
minters 0x..., 0x...
bridge on dry off
Robinhood watching from 84123456
```

When a watched wallet mints, the log names the chain, the contract, and each copy:

```
mint by 0xabc... on Base -> 0xcontract value 0.000000 0xhash
funding 0x0381... from BNB Chain native
bridge OK 0xhash
OK 0x0381... 0xhash
```

## Telegram alerts

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` and the bot messages you when it starts and sends one summary after each mint it copies (or fails to copy), plus a bridge timeout or a repeated websocket failure. Leave them unset and the bot logs only. Alerts are sent in the background, so a slow Telegram never delays a mint.

After a mint is copied, the bot sends one summary message: the collection name and contract address, how many NFTs were minted, how many of your minters copied, why any were skipped, and the total cost (mint price plus gas). A minter that already minted the collection and is now refused is left out quietly. If a minter has to retry, the summary arrives when it succeeds or the bot gives up.

## Telegram controls

With the same two variables set, type `/` in the chat with the bot. Telegram shows the command menu, so you do not have to type the names. The menu is registered when the bot starts. Redeploy after pulling this version, then open the chat and type `/`.

| Command | What it does |
|---|---|
| `/status` | Shows whether copying is on or paused, dry run, chains, and how many retries are waiting |
| `/off` | Pauses copying. The bot keeps watching blocks but skips new mints and tells you each one it skipped |
| `/on` | Resumes copying |
| `/wallets` | Lists the wallets being watched |
| `/watch 0xADDRESS` | Adds one or more wallets to watch, separated by spaces |
| `/unwatch 0xADDRESS` | Removes a wallet. The last wallet cannot be removed, so use `/off` to stop |
| `/minters` | Shows your minter and funder addresses, never their keys |
| `/balance` | Shows each minter's native balance on every chain |
| `/cap` | Shows the mint value cap per chain. `/cap 56 0.1` sets one chain and `/cap all 0.05` sets all |
| `/mint [CHAIN_ID] 0xCONTRACT [QTY] [PRICE]` | Mints a public-mint contract with every minter, without a watched wallet. The chain ID is only needed with several chains. Quantity defaults to 1. The price per NFT is read from the contract if you leave it out. Example: `/mint 4663 0xabc… 2 0.001` |
| `/snipe [CHAIN_ID] 0xCONTRACT [QTY] [PRICE] TIME [wallets=1,3]` | Schedules a timed mint. `PRICE` is per NFT, or `auto` to read it from the contract. `TIME` is a countdown such as `10m`, `90s` or `1h30m`. The bot replies with its plan, and nothing happens until you send `/snipe confirm`. `/snipe cancel` stops it. Example: `/snipe 4663 0xabc… 2 0.001 10m` |
| `/help` | Lists the commands |

- `/mint` tries the common mint calls (`mint`, `publicMint`, `purchase`, `claim`, and a plain ETH send) and checks each with a simulation per minter, so a minter only sends if the simulation shows it receiving the NFTs. It respects the mint cap and dry run. It cannot do allowlist, signed or website-only mints. It answers at once and sends one summary when done.
- `/snipe` finds the mint function in the contract's code (following a proxy), checks that every minter has funds, and signs each minter's transaction a few seconds before the start. At the start time it sends them all at once with no simulation. After a reverted try it sends another, and never sends a second one while the first is still pending, so it cannot mint twice by accident. It stops when a minter has its NFTs, after `SNIPE_WINDOW_SECONDS` (default 10), or at the tries limit, so a mint that never opens cannot drain a wallet. `SNIPE_RETRY_SECONDS` (default 0.5) sets the gap between tries. If several mint functions are found, add `fn=publicMint(uint256)` to the command to pick one. `QTY` defaults to 1 and `PRICE` defaults to `auto`, so the shortest command is `/snipe 0xabc… 42m wallets=1`. Add `wallets=1,3` to use only those minters, numbered as `/minters` lists them (or give address starts such as `wallets=0x1a2b3c`, or `wallets=all`). The default is every minter. The plan shows which minters will send, and the summary counts only them. The gas limit is estimated from the contract when possible. Before the mint opens the estimate usually fails, so it falls back to a default sized by the quantity. A snipe is not saved, so a restart cancels it. It cannot do allowlist, signed or website-only mints, and it will not beat professional sniper bots.
- Wallets added or removed from Telegram are saved and then replace `WATCH_ADDRESSES`. Changing that variable later has no effect until the saved state is cleared. Your own minter and funder addresses cannot be added, and the list holds up to 50 wallets. A new wallet is watched from the next block, with no replay.
- `/cap` sets at most 1 native coin per chain. For a higher cap, change `MAX_MINT_ETH` or `MAX_MINT_<chainId>` in the host settings. A cap set from Telegram is saved and beats the variable.
- Saved settings (pause, wallets, caps) live in the state file, so they only survive a restart if `STATE_PATH` is on a volume. Without one, the bot goes back to the variables.
- Only messages from `TELEGRAM_CHAT_ID` are obeyed. Use a private chat with your bot, not a group, because anyone in a group chat could pause or resume it.
- Mints that happen while paused are not copied later, so there is no burst of late mints when you resume.
- The paused state is saved with the rest of the state. With a `/data` volume it survives restarts and redeploys.
- There is deliberately no command to turn dry run off. Change `DRY_RUN` in Railway for that.

To get a chat id, message your bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`.

## WebSocket mode

Set `WSS_<chainId>` to your provider's `wss://` URL (Alchemy gives one per chain) and the bot subscribes to mint logs for your watched wallets instead of waiting for the next poll. It reads each transaction's receipt, so a mint of 10 is copied as 10. While a socket is live the bot only checks the latest block number now and then (`WSS_BACKUP_SECONDS`). If the socket drops or reconnects, it scans the missed blocks once and polls at full speed until the socket is back. A transaction seen by both is copied once. The bot reconnects with backoff, resubscribes when you change the watch list, and sends one Telegram alert after five failed connects in a row. Look for `<chain> websocket live` in the logs, or `websocket live:` in `/status`. Chains without a `WSS_` variable just poll.

## Simulation

Every copy is simulated first, using `eth_simulateV1` or `debug_traceCall`. Public RPCs often support neither. When the RPC cannot simulate, the bot skips the mint and logs which `RPC_<chainId>` to replace. Setting `ALLOW_ESTIMATE_FALLBACK=true` lets it send on a gas estimate alone, which does not prove that the minter receives the NFT. Leave it off unless you accept that.

## Tests

```bash
python -m unittest discover -s tests
```

No network or keys are needed. The tests cover call decoding, mint detection, fee-bump tracking, caps, state saving and alerts.

## Rules

- `WATCH_ADDRESSES` must be other people. Do not put your own minter keys in that list, or a copy could loop.
- Cross-wallet duplicate suppression is shared across all watched wallets and keyed by chain plus NFT collection. The first eligible mint claims the cooldown and other transactions for that collection are skipped until it expires, including repeat mints by the same wallet. If a copy finishes with no minter minting and is not retrying, the claim is released. It is off by default; set `MINT_COOLDOWN_SECONDS` above 0 to turn it on. Suppressed mints are logged, not sent to Telegram. A source transaction touching several collections is suppressed as a whole if any of them is cooling down.
- `MAX_MINT_ETH` skips a mint whose transaction value is above the cap. The number is in each chain's own coin (ETH, BNB, POL), so use `MAX_MINT_<chainId>` where 0.05 is the wrong size. Raise it if you want to copy paid mints.
- Minter wallets are hot wallets. Keep only what a few mints need in them, and refill from the funder.
- A copied mint can still be a token you cannot sell. The simulation proves you receive the NFT, not that it has value.
- `DRY_RUN=true` logs the transaction and does not broadcast. Each minter that would have minted is reported on Telegram as "would mint", and nothing is sent or bridged.
- Bridging is not instant. A mint can sell out while funds are in flight. `BRIDGE_TIMEOUT` defaults to 180 seconds.
- Each minter sends the full copied transaction, so four minters mint the same quantity each, not split across them.
