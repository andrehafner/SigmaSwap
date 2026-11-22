# SigmaSwap

Five small apps for the [Ergo](https://ergoplatform.org) blockchain that run **inside the Ergo wallet
app** (as [Mosaik](https://github.com/MrStahlfelge/mosaik) dApps). You fill in a form in the wallet,
the app prepares a transaction, and your wallet shows it and signs it.

| App | What it does | Open in the wallet's dApps section |
|---|---|---|
| **SigmaSwap** | Swap ERG and tokens on the ErgoDEX liquidity pools: direct swaps, orders, limit orders at a price you set, and token-to-token routes through ERG | `https://my.ergoport.dev/cgi-bin/sigmaswap/` |
| **SigmaLP** | Add and remove liquidity on the same pools | `https://my.ergoport.dev/cgi-bin/sigmaswap/sigmalp/` |
| **SigmaMint** | Mint your own token or NFT | `https://my.ergoport.dev/cgi-bin/sigmaswap/sigmamint/` |
| **SigmaBridge** | Send ERG and tokens from Ergo to other chains over the Rosen bridge | `https://my.ergoport.dev/cgi-bin/sigmaswap/sigmabridge/` |
| **SigmaStats** | Ergo network figures, a portfolio view of any address, and price alerts. Read-only: it never asks for a signature | `https://my.ergoport.dev/cgi-bin/sigmaswap/sigmastats/` |

To use one: open the Ergo wallet app, go to **dApps**, and enter its address. Opening the same
address in a browser shows a short description page.

## Read this before you use it

**This is experimental software that moves real funds. Use it at your own risk.**

- It has not been audited by an outside party.
- Blockchain transactions cannot be undone. Nobody can reverse a swap, a mint or a bridge transfer
  for you.
- Start with small amounts.
- There is no warranty of any kind, and the authors accept no liability for losses.
- It is an independent project. It is not affiliated with or endorsed by ErgoDEX, the Rosen bridge,
  Lighthouse or the Ergo Platform.

### Always read what your wallet shows you

The apps never hold your keys or your funds. Every transaction is built unsigned and handed to your
wallet, and **the wallet's confirmation screen is the final check**. Read the message and the
amounts there every time.

That matters most for links. A link that opens a signing request in your wallet can be made by
anyone, including one that points at this service. If you did not start the action yourself in the
app, do not sign it.

### Things that can cost you money

- **Token names are not unique.** Anyone can create a token called "SigUSD". The apps show the token
  id when two listed tokens share a name, and your wallet shows its own verified badge. Check it.
- **A swap between two tokens that share no pool goes through ERG.** Signing once makes the second half an
  order (see the next point); if it cannot fill at your minimum, cancelling returns ERG, not the token you
  started with. Signing twice makes both halves direct swaps with nothing to front-run, but the price can
  move between them.
- **Orders and limit orders are public while they wait.** Other traders can trade ahead of them, so
  you may receive only your minimum. A limit order may never fill; you can cancel it at any time.
- **Liquidity providers can lose value** compared with simply holding, when prices move.
- **Bridge transfers go to the address you type.** If it is wrong, or not yours, the funds are lost.
  The bridge also has a high minimum, because its fees are fixed amounts.
- **A mint is permanent.** Names, amounts and NFT files cannot be changed afterwards, and files
  stored on IPFS are public and cannot be removed.

## How it works

- **No accounts and no database.** There is no login. The server keeps only short-lived notes about
  the transactions it prepared (public data), so it can tell you afterwards whether one went through.
- **Direct swaps and liquidity changes** are a single transaction between your wallet and the pool.
  They complete exactly as shown or not at all.
- **Orders and limit orders** put your funds in an open ErgoDEX order contract that carries your key,
  so you can always take them back. A small bot run by this service completes an order once the pool
  can pay its minimum. The bot holds no keys: completing an order is a transaction anyone may send,
  and the contract only allows it to pay you.
- **Prices in SigmaStats** are ErgoDEX pool prices, not exchange quotes. The ERG price shown is its SigUSD
  pool price. A price alert is a convenience and can be late or missed: do not rely on it to protect a position.
- **Minting** follows Ergo's token standard (EIP-4). NFT files are stored on IPFS through Lighthouse.
- **Bridging** builds the same "lock" transaction the Rosen web app builds, with fees read from
  Rosen's on-chain fee table. Rosen, an independent project, delivers the funds on the other chain.
- Before a transaction reaches your wallet, the pool and order contracts involved are evaluated
  against it. One that a contract would reject is never handed over.

Pool data comes from the ErgoDEX analytics API and the public Ergo explorer.

## Running your own copy

It is a set of Perl CGI scripts with one Python helper (using
[ergo-lib-python](https://pypi.org/project/ergo-lib-python/)), served by Apache from
`/usr/lib/cgi-bin` on Debian or Ubuntu. There is no build step and no database.

| Path | Purpose |
|---|---|
| `api.pl`, `sigmalp/`, `sigmamint/`, `sigmabridge/`, `sigmastats/` | The five apps (Mosaik views and ErgoPay endpoints) |
| `sigmaswap.py` | All blockchain logic: quotes, transaction building, contract checks, the order bot |
| `AppConfig.pm`, `MosaikKit.pm` | Shared configuration and Mosaik helpers |
| `deploy/` | Apache include and the bot's systemd units |
| `sigmaswap.env.example` | Every setting, with its default |
| `DEPLOY.md` | Step-by-step installation, checks and troubleshooting |
| `App_Summary.md` | What the apps store, send and trust, written for a reviewer |

Follow `DEPLOY.md`, including its "Before going public" section: the apps have no built-in rate
limiting, and the cache directory must not be reachable over the web.

## Reporting a problem

If you find a security problem, please report it privately to the repository owner rather than
opening a public issue, and include enough detail to reproduce it.

## Licence

No licence has been chosen yet. Until one is added, the code is published for reading and review
only; all rights are reserved by its authors.
