# Copymint Bot: Setup Guide

## What it does

The bot watches wallets you choose. When one of them mints an NFT to itself, your own minter wallets send the same mint for the same quantity, on EVM chains. Before it sends, it simulates the copy and only broadcasts if the simulation mints the same collection and quantity. You control it from Telegram.

**Risk warning.** The bot signs and sends real transactions from the private keys you give it. Use fresh wallets that hold only what you can afford to lose, never your main wallet. Start in dry-run mode. A copied mint can still be a token you cannot sell, and nothing here is financial advice.

**What you need**

- A GitHub account (to fork the code)
- A Telegram account
- An Alchemy account (or another RPC provider that supports simulation and WebSockets)
- A Railway account to host the bot
- One or more fresh wallets to act as minters, with a little native coin on the chain you mint on

This guide covers a single-chain setup on Robinhood Chain (chain ID 4663). Other chains work the same way: add their chain ID and an RPC for each.

## Step 1: Get your own copy of the code

1. Open github.com/dejagold123/copymint-bot while signed in to GitHub.
2. Click **Fork** and create the fork under your account. Railway can deploy from a private fork too.
3. Never commit a `.env` file or any key. Keys and tokens belong only in Railway variables.

## Step 2: Create a Telegram bot and find your chat ID

The bot sends you alerts and takes commands through Telegram.

1. In Telegram, open **@BotFather** and send `/newbot`. Pick a name and a username that ends in `bot`.
2. BotFather replies with a token. This is your `TELEGRAM_BOT_TOKEN`. Treat it like a password: anyone holding it can control your bot. If it leaks, send `/revoke` to BotFather and use the new token.
3. Open a private chat with your new bot and send it any message, for example `hi`.
4. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, replacing `<TOKEN>` with your token.
5. Find `chat` and then `id` in the result. That number is your `TELEGRAM_CHAT_ID`.

The bot only obeys messages from that one chat ID. Use a private chat, not a group, because anyone in a group could pause or resume it.

## Step 3: Get an RPC and WebSocket URL from Alchemy

The bot needs a dedicated RPC to read blocks and to simulate each mint before sending. Public RPCs rate-limit and often cannot simulate, so the bot skips mints it cannot simulate.

1. Sign up at Alchemy and create an app for the chain you want (here, Robinhood).
2. Copy the **HTTPS** URL. It goes into `RPC_4663`.
3. Copy the **WSS** URL (it starts with `wss://`). It goes into `WSS_4663`. WebSocket mode detects mints almost instantly instead of waiting for the next poll.
4. For another chain, repeat with its own URLs and use `RPC_<chainId>` and `WSS_<chainId>` (for example `RPC_8453` for Base).

You need both URLs. The bot uses HTTPS for reads and simulation, and the socket for detection.

On Alchemy's free plan a single log request can cover at most 10 blocks. The bot defaults to 10 (`LOGS_MAX_BLOCKS`). Only raise it if your plan allows more.

## Step 4: Prepare minter wallets and fund them

1. Create one or more **new** wallets in any EVM wallet app. These are your minters. Do not reuse a wallet that holds anything valuable.
2. Export each wallet's private key. Put them in `MINTER_PRIVATE_KEYS`, separated by commas: `0xkey1,0xkey2`.
3. Send each minter enough native coin on the mint chain to cover the mint price plus gas. Each minter sends the full copied transaction, so four minters mint the full quantity each, not split between them.
4. Optional: a separate **funder** wallet (`FUNDER_PRIVATE_KEY`). If a minter is short on the mint chain, the bot swaps or bridges funds in from the funder's other chains using LI.FI, then mints. Bridging is not instant, so a mint can sell out while funds are in flight. Skip this and the bot only uses what is already on each minter.
5. Choose the wallets to copy and put their addresses in `WATCH_ADDRESSES`, separated by commas. These must be other people's wallets. Never put your own minter addresses there, or a copy could loop.

Minters are hot wallets. Keep only what a few mints need in them.

## Step 5: Deploy on Railway and add variables

1. In Railway, choose **New Project**, then **Deploy from GitHub repo**, and select your fork.
2. Open the service and go to **Variables**. Add the ones below. This is the minimum for a Robinhood-only setup.

| Variable | Value |
|---|---|
| `WATCH_ADDRESSES` | The wallets to copy, comma-separated |
| `MINTER_PRIVATE_KEYS` | Your minter keys, comma-separated |
| `CHAINS` | `4663` |
| `RPC_4663` | Your Alchemy HTTPS URL |
| `WSS_4663` | Your Alchemy WSS URL |
| `POLL_SECONDS` | `2` |
| `MAX_MINT_ETH` | `0.05`, the most a single mint may cost in the chain's native coin |
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID |
| `STATE_PATH` | `/data/state.json` |
| `DRY_RUN` | `true` (for now) |

3. In the service settings, add a **Volume** and mount it at `/data`. This saves the last processed block, your pause state and any wallets or caps you change from Telegram. Without it, a restart can miss mints and settings reset to the variables.
4. The start command is `python bot.py`. It is already set in the repo, so a redeploy is all you need after saving variables.

When you set a variable such as `WATCH_ADDRESSES`, spaces after commas are fine.

## Step 6: Test in dry-run mode

With `DRY_RUN=true` the bot logs and reports what it would send, and broadcasts and bridges nothing.

1. Open the Deploy logs. A healthy start looks like this:

```
watching 1 wallet(s) on Robinhood
minters 0x..., 0x...
bridge on dry on
Robinhood watching from 84123456
Robinhood websocket live
```

2. In Telegram, the bot sends a startup message. Type `/` in the chat to see the command menu.
3. Send `/status` to confirm copying is on, dry run is on, and the websocket is live.
4. Send `/minters` and `/balance` to check your minter addresses and that each has funds on the chain.
5. Send `/wallets` to confirm the right wallets are watched.
6. When a watched wallet mints, you get one summary message. In dry run, each minter that would have minted shows as "would mint".

If the websocket line is missing, re-check `WSS_4663`. If you see nothing at all, re-check `WATCH_ADDRESSES` and `CHAINS`.

## Step 7: Go live

1. In Railway, change `DRY_RUN` to `false` (or delete it) and redeploy. There is deliberately no Telegram command to turn dry run off.
2. Keep funds small at first: enough for one or two cheap mints per minter.
3. Keep `MAX_MINT_ETH` low. The bot skips any mint that costs more than the cap. You can change it per chain with `/cap`.
4. Watch the first real copy. After each mint the bot sends one summary: the collection name and contract, how many NFTs were minted, how many minters copied, why any were skipped, and the total cost including gas.
5. Use `/off` any time to pause. Mints that happen while paused are not copied later, so nothing fires in a burst when you resume with `/on`.

## Telegram commands reference

Type `/` in the chat with your bot to see the menu.

| Command | What it does |
|---|---|
| `/status` | Shows whether copying is on or paused, dry run, chains, websocket state and waiting retries |
| `/on` | Resumes copying |
| `/off` | Pauses copying. The bot keeps watching but skips new mints |
| `/wallets` | Lists the watched wallets |
| `/watch 0xADDRESS` | Adds one or more wallets to watch, separated by spaces |
| `/unwatch 0xADDRESS` | Removes a wallet. The last one cannot be removed, so use `/off` to stop |
| `/minters` | Shows your numbered minter and funder addresses, never the keys |
| `/balance` | Shows each minter's native balance on every chain |
| `/cap` | Shows the mint cap per chain. `/cap 4663 0.1` sets one chain, `/cap all 0.05` sets all |
| `/mint [CHAIN_ID] 0xCONTRACT [QTY] [PRICE]` | Mints a public-mint contract with every minter, without a watched wallet. Quantity defaults to 1. If you leave out the price, it is read from the contract |
| `/help` | Lists the commands |

Notes:

- Wallets added or removed from Telegram are saved and then replace `WATCH_ADDRESSES`. Changing that variable later has no effect until the saved state is cleared.
- A cap set with `/cap` is saved and beats the variable. It can be at most 1 native coin per chain. For a higher cap, change `MAX_MINT_ETH` or `MAX_MINT_<chainId>` in Railway.
- `/mint` tries the common mint calls and checks each with a simulation first. It cannot do allowlist, signed or website-only mints.

## Variables reference

| Variable | Default | Purpose |
|---|---|---|
| `WATCH_ADDRESSES` | none | Wallets to copy, comma-separated |
| `MINTER_PRIVATE_KEYS` | none | Your minter keys, comma-separated |
| `FUNDER_PRIVATE_KEY` | empty | Optional wallet that funds minters from other chains |
| `CHAINS` | all supported | Chain IDs to use: 1, 10, 56, 137, 8453, 42161, 999, 4663 |
| `RPC_<chainId>` | public RPC | Dedicated HTTPS RPC per chain. Set this for any chain you care about |
| `WSS_<chainId>` | none | WebSocket URL per chain for near-instant detection |
| `WSS_BACKUP_SECONDS` | `30` | How often to check the latest block while the socket is live (one cheap request) |
| `WSS_BACKUP_SCAN` | `false` | Also re-scan blocks for mint logs on that check. Costs far more on fast chains |
| `LOGS_MAX_BLOCKS` | `10` | Blocks per log request. Alchemy's free plan allows 10 |
| `POLL_SECONDS` | `0.4` | How often to look for new blocks on a chain without a live socket |
| `MAX_MINT_ETH` | `0.05` | Skip any mint costing more than this, in each chain's native coin |
| `MAX_MINT_<chainId>` | none | Cap for one chain, for example `MAX_MINT_56=0.1` |
| `MINT_COOLDOWN_SECONDS` | `0` (off) | Ignore repeat mints of the same collection on the same chain for this long. Leave off if watched wallets mint several times in a row |
| `ENABLE_BRIDGE` | `true` | Allow swapping or bridging funds in when a minter is short |
| `BRIDGE_TIMEOUT` | `180` | Seconds to wait for a bridge |
| `ALLOW_ESTIMATE_FALLBACK` | `false` | Send on a gas estimate alone if the RPC cannot simulate. Does not prove you receive the NFT |
| `TELEGRAM_BOT_TOKEN` | empty | From BotFather. Empty means log only |
| `TELEGRAM_CHAT_ID` | empty | The one chat that gets alerts and may give commands |
| `STATE_PATH` | `/data/state.json` | Where state is saved. Needs a volume to survive restarts |
| `DRY_RUN` | `false` | Log what would be sent but broadcast and bridge nothing |

## Troubleshooting and limits

**A watched wallet minted but the bot did not copy it**

- If the mint was an allowlist or signed mint, it cannot be copied. The signature is locked to the watched wallet, so the copied call reverts. The bot skips it and tells you.
- If the RPC cannot simulate, the bot skips the mint and logs which `RPC_<chainId>` to replace. Use Alchemy or an equivalent.
- If the mint costs more than `MAX_MINT_ETH` (or the chain's own cap), it is skipped. Raise the cap if you want paid mints.
- If a minter already minted that collection, it is left out of the summary quietly.
- If the bot is paused with `/off`, new mints are skipped and not replayed.

**My Alchemy usage is draining fast**

- Set `WSS_<chainId>` so the bot listens on a socket and only makes one cheap request every `WSS_BACKUP_SECONDS`.
- Keep `WSS_BACKUP_SCAN` at `false` and `LOGS_MAX_BLOCKS` at your plan's limit.
- Only set `CHAINS` to the chains you really use.

**The build crashes on start**

- Check that every variable is spelled exactly as in the reference table.
- Check `WATCH_ADDRESSES` holds valid `0x` addresses, comma-separated.

**Settings keep resetting after a restart**

- Add the Railway Volume at `/data` and set `STATE_PATH=/data/state.json`.

**Limits to know about**

- Allowlist, signed and website-only mints cannot be copied.
- The bot is a copy tool, not a guarantee of a fast or successful mint. A mint can sell out first, and bridging takes time.
- A copied mint can still be a token you cannot sell. The simulation proves you receive the NFT, not that it has value.
- Minter wallets are hot wallets. Fund them lightly and refill from the funder.
- The code is provided as is, under the MIT licence, with no warranty.
