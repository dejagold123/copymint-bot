# Copymint bot

A Telegram-controlled EVM bot that copies NFT mints from wallets you watch, and also mints, snipes, buys, sells and moves NFTs on demand. It runs on [Railway](https://railway.app) and signs real transactions from keys you give it.

> **Use at your own risk.** This bot sends real transactions from the private keys you give it. Use fresh wallets that hold only what you can afford to lose, never your main wallet. Start with `DRY_RUN=true`. Mints can fail, copy the wrong thing, or be scams, and nothing here is financial advice. Never commit a `.env` file or share your keys, RPC URLs or Telegram bot token.
>
> The bot's tests use mocked chains and a mocked OpenSea. The OpenSea features (`/buy`, `/sweep`, `/list`, `/offer`, auto-list) were built from OpenSea's documentation and are the least proven part: try each on one cheap NFT first.

**Prefer a click-by-click walkthrough? See the [step-by-step setup guide](GUIDE.md).** This README is the full reference.

## Contents

1. [What it does](#what-it-does)
2. [Quick start](#quick-start)
3. [Variables](#variables)
4. [Chains](#chains)
5. [Telegram commands](#telegram-commands)
6. [Settings](#settings)
7. [How the main features work](#how-the-main-features-work)
8. [Security checklist](#security-checklist)
9. [Troubleshooting](#troubleshooting)
10. [Development](#development)

## What it does

| Feature | Command | In one line |
|---|---|---|
| Copy minting | automatic | When a watched wallet mints, your minters send the same mint for the same quantity |
| Manual mint | `/mint` | Mint a public-mint contract with your minters, no watched wallet needed |
| Timed mint | `/snipe` | Schedule a mint for a countdown or a clock time such as `5pm UTC` |
| Buy | `/buy`, `/sweep` | Buy one OpenSea listing, or sweep a collection under a price |
| Sell | `/list`, `/sellall`, `/unlist`, auto-list | List held NFTs, priced off the top offer |
| Offers | `/offer` | Bid on an NFT |
| Move NFTs | `/sendnfts` | Send NFTs from your minters to another wallet |
| Forward funds | `/collect` | Send part of each deposit to a fixed wallet |
| Records | `/history`, `/pnl` | What is scheduled, what happened, what you spent and made |

Copying works from the mint call only, including a mint buried in a multicall. Every copy is simulated first and only broadcast when the simulation gives your wallet the same collection and quantity. A send that does not land is retried, and replaced once with a higher fee if it is still pending after two blocks. If a minter is short of the chain's native coin, the bot can bridge or swap funds in with [LI.FI](https://li.quest) from another chain, optionally paid by a separate funder wallet. The bot starts at the current block and never replays old mints.

## Quick start

You need: a GitHub account, a Telegram account, a Railway account, an RPC provider (Alchemy is the easiest), and one or more **fresh** wallets with a little native coin on the chain you want to mint on.

1. **Get the code.** Fork this repository to your own GitHub account (a private fork is fine). Never commit keys.
2. **Create a Telegram bot.** In Telegram, message **@BotFather**, send `/newbot`, and keep the token it gives you (`TELEGRAM_BOT_TOKEN`). Send your new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat` → `id` (`TELEGRAM_CHAT_ID`). Use a private chat, not a group.
3. **Get an RPC.** In Alchemy, create an app for each chain you want. Copy its HTTPS URL (`RPC_<chainId>`) and, for fast detection, its `wss://` URL (`WSS_<chainId>`). Public RPCs rate-limit and often cannot simulate, so the bot skips mints it cannot simulate.
4. **Deploy on Railway.** New Project → Deploy from GitHub repo → pick your fork. The start command is `python bot.py`.
5. **Add a volume.** Service → Settings → Volumes → mount path `/data`. This keeps your settings, watch list and records across restarts. Without it, the bot forgets them on every redeploy.
6. **Add variables** (Service → Variables). The minimum is:

   ```
   WATCH_ADDRESSES=0xWalletToCopy1,0xWalletToCopy2
   MINTER_PRIVATE_KEYS=0xYourMinterKey1,0xYourMinterKey2
   CHAINS=4663
   RPC_4663=https://your-rpc-url
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   STATE_PATH=/data/state.json
   DRY_RUN=true
   ```

7. **Redeploy and watch the Deploy logs.** A healthy start looks like:

   ```
   watching 1 wallet(s) on Robinhood
   minters 0x..., 0x...
   bridge on dry on
   Robinhood watching from 84123456
   ```

   Your Telegram chat gets a "started" message. Type `/` in the chat to see the command menu.
8. **Test in dry run.** With `DRY_RUN=true` nothing is sent or bridged, and each minter that would have minted is reported as "would mint". When you are happy, set `DRY_RUN=false` and redeploy.
9. **Fund your minters** with a little native coin on each chain. Check with `/balance`.

To get a chat ID or token wrong is the most common setup mistake. If the bot does not answer in Telegram, see [Troubleshooting](#troubleshooting).

## Variables

Set these in Railway → Service → Variables (or in a local `.env` when you run it yourself). See `.env.example` for a copy-paste template.

### Required

| Variable | What it is |
|---|---|
| `WATCH_ADDRESSES` | Comma-separated wallets to copy. Must be other people's wallets, never your own minters |
| `MINTER_PRIVATE_KEYS` | Comma-separated private keys of your minter wallets. Each one sends the copied mint |
| `CHAINS` | Comma-separated chain IDs to watch, for example `1,8453,4663` |
| `RPC_<chainId>` | An RPC URL per chain, for example `RPC_4663`. A public default is used when you leave one out, but set your own |

### Strongly recommended

| Variable | What it is |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alerts and the command menu. Without them the bot only logs |
| `STATE_PATH` | `/data/state.json`, with a Railway volume at `/data` |
| `DRY_RUN` | `true` while testing |
| `MAX_MINT_ETH` | The most a single copied mint may cost, in the chain's own coin. Default `0.05`. `MAX_MINT_<chainId>` overrides one chain |
| `WSS_<chainId>` | A `wss://` endpoint for near-instant detection. Polling stays on as a backup |

### Optional

| Variable | What it does |
|---|---|
| `FUNDER_PRIVATE_KEY` | A wallet that pays for the minters by bridging or swapping when they run short |
| `ENABLE_BRIDGE` | `true` by default. Bridge or swap from other chains when a minter is short |
| `BRIDGE_TIMEOUT` | Seconds to wait for a bridge, default `180` |
| `POLL_SECONDS` | How often to look for new blocks, default `0.4` |
| `MINT_COOLDOWN_SECONDS` | `0` (off) by default. Ignore repeat mints of the same collection on the same chain for this long. Leave off if watched wallets mint several times in a row |
| `ALLOW_ESTIMATE_FALLBACK` | `false` by default. `true` lets the bot send on a gas estimate when the RPC cannot simulate. That does not prove you receive the NFT |
| `WSS_BACKUP_SECONDS` | `30`. How often to check the latest block while a socket is live |
| `WSS_BACKUP_SCAN` | `false`. Also rescan blocks for mint logs on that tick (costs far more on a fast chain) |
| `LOGS_MAX_BLOCKS` | `10`, the most Alchemy's free plan allows per `eth_getLogs`. Raise it on a paid plan |
| `SNIPE_WINDOW_SECONDS` | `10`. How long `/snipe` keeps trying after the start |
| `SNIPE_RETRY_SECONDS` | `0.5`. Gap between `/snipe` tries |
| `OPENSEA_API_KEY` | Needed for `/buy`, `/sweep`, `/list`, `/sellall`, `/offer`, auto-list, OpenSea names and the OpenSea part of `/sendnfts`. Get one from OpenSea's developer settings |
| `MAX_BUY_ETH` | `0.05`. The most one OpenSea purchase or offer may cost. `max=` in a command can only go below it |
| `SWEEP_POLL_SECONDS` | `5`. How often a sweep re-reads listings |
| `WETH_<chainId>` | Wrapped-coin address for offers on a chain the bot does not know |
| `OPENSEA_STREAM_URL` | Override OpenSea's live feed address |
| `COLLECT_TO` | Destination for `/collect`. Can only be changed here, never from Telegram |
| `COLLECT_PRIVATE_KEY` | The wallet `/collect` watches and sends from (separate from your minters) |
| `COLLECT_WALLET` | Instead of a key, use minter number N for `/collect` |
| `COLLECT_POLL_SECONDS` | `5`. How often `/collect` checks the balance |
| `RPC_<chainId>`, `CHAIN_NAME_<chainId>` | Use a chain the bot does not list. See [Chains](#chains) |
| `PHASE_CHECKS` | Set to `0` to stop asking OpenSea which drop phase each wallet is on |

## Chains

Built in: Ethereum (1), Optimism (10), BNB Chain (56), Polygon (137), Base (8453), Arbitrum (42161), HyperEVM (999), Robinhood Chain (4663) and Ink (57073). Put the IDs you want in `CHAINS`.

Any other EVM chain works if you set `RPC_<chainId>` and add its ID to `CHAINS`. Optionally set `CHAIN_NAME_<chainId>` so messages show a name. OpenSea features only work on chains OpenSea supports. If a chain's RPC cannot simulate, the bot skips mints it cannot check rather than guess (see [Simulation](#simulation)).

## Telegram commands

Type `/` in your chat with the bot to see the menu. Only messages from `TELEGRAM_CHAT_ID` are obeyed. Wallets are numbered as `/minters` lists them, so `wallets=1,3` means your first and third minter.

### Control and information

| Command | What it does |
|---|---|
| `/status` | Copying on or paused, dry run, chains, waiting retries |
| `/off`, `/on` | Pause or resume copying. Mints seen while paused are skipped, never replayed |
| `/wallets`, `/watch 0xADDR`, `/unwatch 0xADDR` | List, add or remove watched wallets (the last one cannot be removed) |
| `/minters` | Your minter and funder addresses, never their keys |
| `/balance` | Each minter's native balance on every chain |
| `/cap`, `/cap 56 0.1`, `/cap all 0.05` | Show or set the per-chain mint cap |
| `/history` (or `/log`) | What is scheduled and the latest events |
| `/pnl` | What you spent on mints and buys, estimated sales, open listings |
| `/settings` | See and change saved settings |
| `/help` | List the commands |

`/off` pauses only copying. `/mint`, `/snipe`, `/buy`, `/sweep`, `/sendnfts` and the rest keep working.

### Minting

| Command | What it does |
|---|---|
| `/mint 0xCONTRACT [QTY] [PRICE] [wallets=1,3] [chain=robinhood]` | Mints a public-mint contract with your minters. Price is read from the contract when left out. It tries the common mint calls and checks each with a simulation per minter |
| `/snipe 0xCONTRACT [QTY] [PRICE] [TIME] [wallets=1,3] [chain=robinhood] [fn=NAME(types)]` | Schedules a mint. `TIME` is a countdown (`10m`, `1h30m`) or a clock time (`5pm UTC`, `17:30 WAT`). With no time, it uses the phase your wallets are on. Add `/snipe cancel` to stop it |

Both can use allowlist phases when OpenSea reports your wallet as eligible. They cannot do signature or website-only mints that OpenSea does not expose. `/snipe` signs a few seconds early, sends at the start, retries after a revert without ever sending a second transaction while the first is pending, and stops after `SNIPE_WINDOW_SECONDS`. A restart cancels a snipe.

### OpenSea trading (needs `OPENSEA_API_KEY`)

| Command | What it does |
|---|---|
| `/buy 0xCONTRACT 123 [under 0.01] [wallet 2] [at 5pm UTC]` | Buy one NFT at its lowest listing. Or paste an OpenSea item link instead of `0xCONTRACT 123` |
| `/sweep 0xCONTRACT under 0.002 [budget 0.5] [wallets 1 and 2] [on robinhood]` | Buy every listing at or below the price, cheapest first, until `/sweep stop` or the budget is spent. Add `trait=Background:Blue` and `rank=500` to filter. Or paste a collection link |
| `/list 0xCONTRACT 123 for 2x` | List an NFT you hold. A price is `0.05` or a multiple of the top offer (`2x`) |
| `/sellall 2x [wallets=1,3] [collection=0x…]` | List up to 40 held NFTs at a multiple of each one's top offer, or one fixed price |
| `/unlist 0xCONTRACT 123` or `/unlist all` | Cancel the bot's listings on-chain |
| `/offer 0xCONTRACT 123 0.01 [hours 6]` | Bid in wrapped coin (for example WETH) on one NFT |

Every command also takes the older forms (`wallets=1,3`, `max=0.01`, an OpenSea link). You can type a sentence, and `/buy` and `/sweep` accept a start time.

### Moving NFTs and funds

| Command | What it does |
|---|---|
| `/sendnfts from wallet 2 to wallet 1 collection=0xabc… [count=5]` | Sends NFTs your minters hold. `collection=` sends only that collection. `count=5` (or `send 5 nfts`) sends only that many |
| `/collect on`, `/collect off`, `/collect reset`, `/collect` | Forward part of every deposit to `COLLECT_TO` |

## Settings

Change with `/settings KEY VALUE`, restore with `/settings reset KEY`. They are saved in the state file, so they survive restarts only if `STATE_PATH` is on a volume.

| Setting | Default | What it does |
|---|---|---|
| `confirm_trades` | off | When on, `/buy`, `/sweep`, `/list`, `/offer` and `/snipe` wait for `confirm`. When off they start right after showing their plan (like `/mint`) |
| `confirm_bulk` | on | When on, `/sellall`, `/unlist` and `/sendnfts` wait for `confirm` |
| `autolist` | off | List every NFT the bot mints or buys at a multiple of its top offer |
| `autolist_multiple` | 2 | The multiple of the top offer to list at |
| `autolist_step` | 10 | Only reprice when the new price is this many percent higher |
| `autolist_minimum` | 0 | Never list below this price |
| `autolist_days` | 14 | How long a listing the bot makes stays valid |
| `autolist_seconds` | 60 | How often listings are checked |
| `premint_min_sales` | 0 | Skip copying a collection OpenSea knows with fewer sales than this (new collections always pass) |
| `opensea_names` | on | Use OpenSea collection names in alerts |
| `stream` | on | Use OpenSea's live feed to wake `/sweep` the moment a listing appears |
| `collect` | off | Switched with `/collect on` and `/collect off` |
| `collect_percent` | 40 | Share of the wallet's whole balance sent on when a deposit lands |
| `collect_minimum` | 0 | Ignore deposits smaller than this |

## How the main features work

### Copy minting

The bot watches your `WATCH_ADDRESSES`. When one mints to itself, each of your minters simulates the same mint and sends it if the simulation shows the minter receiving the NFT. Allowlist and signed mints are tried too, but each minter is simulated on its own: a minter that is eligible on-chain mints, and the others are skipped as "not eligible". A signature or Merkle proof made for the watched wallet will not work for your minters. When a simulation reverts, the bot reads the contract to say why (sold out, paused, not open, wallet limit, not allowlisted, wrong payment, or the contract's own message).

After each copy you get one summary: collection name and contract, how many minted, why any minters were skipped, and the total cost (price plus gas).

### Simulation

Every copy is simulated first, using `eth_simulateV1` or `debug_traceCall`. Public RPCs often support neither. When the RPC cannot simulate, the bot skips the mint and logs which `RPC_<chainId>` to replace. Setting `ALLOW_ESTIMATE_FALLBACK=true` lets it send on a gas estimate alone, which does not prove that the minter receives the NFT. Leave it off unless you accept that.

### WebSocket mode

Set `WSS_<chainId>` to your provider's `wss://` URL and the bot subscribes to mint logs for your watched wallets instead of waiting for the next poll. While a socket is live it only checks the latest block number now and then. If the socket drops, it scans the missed blocks once and polls at full speed until it is back. Look for `<chain> websocket live` in the logs. Chains without a `WSS_` variable just poll.

### OpenSea buying and selling

- Purchases are limited to fixed-price, single-item listings paid in the chain's own coin. Each is simulated and sent only if the simulation gives that wallet the NFT. `MAX_BUY_ETH` caps every purchase.
- Listings and offers are Seaport orders. Before posting, the bot checks its own encoding against the Seaport contract and refuses if the hashes differ.
- Auto-list (`/settings autolist on`) lists every NFT the bot minted or bought at `autolist_multiple` times its top offer. It only ever raises a price: when the top offer rises enough, it cancels the old listing on-chain and lists again higher. It never lowers a listing and never touches one you listed by hand at a fixed price. The first listing from a wallet in a collection sends one approval transaction (gas).
- A sweep ends when the bot restarts and never resumes on its own.

### Sending NFTs

`/sendnfts` knows the NFTs your minters received through the bot, and checks on-chain that the wallet still owns each one before sending. For older NFTs, add `collection=0xabc…`: the bot lists the wallet's tokens if the contract can, and otherwise asks OpenSea (when `OPENSEA_API_KEY` is set) and confirms each token on-chain. `deep` checks token by token for collections up to 20000 NFTs. If it still cannot list them, give token IDs yourself with `ids=5,7,100-120` next to one `collection=`.

The plan lists what it will send, what a contract refuses (grouped by collection, with whether the receiver or the collection is to blame), and any wallet that cannot pay gas. It sends with `safeTransferFrom`, so a destination that cannot receive NFTs makes the transfer revert and it is reported, not sent.

### Forwarding deposits (`/collect`)

Set `COLLECT_TO` and `COLLECT_PRIVATE_KEY` in Railway, then send `/collect on`. When coin arrives in that wallet and the balance holds for two checks, `collect_percent` (default 40) of the wallet's whole balance at that moment goes to `COLLECT_TO` on that chain and the rest stays. Gas is kept back. Only coin is forwarded, and the sender is not checked. The destination can only be changed in Railway, on purpose.

## Security checklist

- **Fresh wallets only.** Minters are hot wallets. Keep only what a few mints need, and refill from the funder.
- **Private chat.** Only `TELEGRAM_CHAT_ID` can give commands. Use a private chat with your bot, never a group.
- **Protect the bot token.** Anyone with `TELEGRAM_BOT_TOKEN` can control your Telegram bot. If it leaks, send `/revoke` to @BotFather and set the new token in Railway. Do the same for any GitHub token you have pasted anywhere.
- **Keys live in Railway variables only.** Never commit a `.env` file, and never paste a private key into chat or an issue.
- **Dry run first.** Test with `DRY_RUN=true`. There is deliberately no command to turn dry run off; change it in Railway.
- **Caps.** Keep `MAX_MINT_ETH` and `MAX_BUY_ETH` small. Spending commands start straight away by default, so turn `confirm_trades` on if you want a confirm step.
- **Fixed destination.** `/collect` can only be pointed at a new wallet from Railway, so someone who reaches your chat cannot redirect it.
- **A copied token can still be worthless.** The simulation proves you receive the NFT, not that it has value.
- Bridging is not instant. A mint can sell out while funds are in flight.

## Troubleshooting

| Symptom | What to check |
|---|---|
| The bot does not answer in Telegram | `TELEGRAM_CHAT_ID` must be the chat you are typing in, and the bot token must be right. Send your bot a message first, then re-check `getUpdates` |
| "start" message never arrives | Both Telegram variables must be set, then redeploy. Look in the Deploy logs for the error |
| A mint was skipped with "cannot simulate" | Your RPC does not support simulation. Use an Alchemy (or similar) URL for that chain in `RPC_<chainId>` |
| It misses mints after a restart | There is no volume. Add one at `/data` and set `STATE_PATH=/data/state.json` |
| "not enough gas" in a plan | The wallet needs a little native coin on that chain. Send some and run the command again |
| `/buy` or `/list` says OpenSea is not set up | Set `OPENSEA_API_KEY` in Railway and redeploy |
| `/snipe` plan lists no eligible wallets | OpenSea says none of your wallets are on that phase, or could not be asked. Check on opensea.io, or use a plain `/mint` once the sale is open |
| A chain crashes the build | Use only chain IDs the bot knows, or set `RPC_<chainId>` for it (and `CHAIN_NAME_<chainId>`) |
| Rate limit errors in the logs | Use your own Alchemy URLs, and keep `LOGS_MAX_BLOCKS` at your plan's limit |
| It copies a mint twice or loops | `WATCH_ADDRESSES` must not contain your own minters |

Logs are in Railway → Service → Deployments → View logs. Errors are also sent to Telegram when it is configured.

## Development

```bash
pip install -r requirements.txt
python -m unittest discover -s tests
python bot.py
```

The tests need no network or keys. They cover call decoding, mint detection, fee bumping, caps, state saving, alerts, Seaport encoding, OpenSea flows, sending and settings.

Code layout: `bot.py` (watching, copy minting, Telegram, snipe, mint), `trade.py` (`/buy`, `/sweep`), `market.py` (selling, offers, auto-list, `/pnl`), `nftsend.py` (`/sendnfts`), `collect.py` (`/collect`), `easy.py` (plain-words commands), `history.py` (`/history`), `phases.py` (OpenSea drop phases), `settings.py`, `seaport.py` (builds and signs Seaport orders), `opensea.py` (API client and live feed) and `helpers.py` (pure helpers).

Rules the code follows: your minters must never appear in `WATCH_ADDRESSES`; each minter sends the full copied transaction, so four minters mint the full quantity each, not split across them; and wallets added or removed from Telegram are saved and then replace `WATCH_ADDRESSES`.

## License

See [LICENSE](LICENSE).
