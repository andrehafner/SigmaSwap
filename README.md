# SigmaSwap

Seven small apps for the [Ergo](https://ergoplatform.org) blockchain that run **inside the Ergo wallet
app** (as [Mosaik](https://github.com/MrStahlfelge/mosaik) dApps). You fill in a form in the wallet,
the app prepares a transaction, and your wallet shows it and signs it.

| App | What it does | Open in the wallet's dApps section |
|---|---|---|
| **SigmaSwap** | Swap ERG and tokens on the ErgoDEX liquidity pools: direct swaps, orders, limit orders at a price you set, and token-to-token routes through ERG | `https://my.ergoport.dev/cgi-bin/sigmaswap/` |
| **SigmaLP** | Add and remove liquidity on the same pools | `https://my.ergoport.dev/cgi-bin/sigmaswap/sigmalp/` |
| **SigmaMint** | Mint your own token or NFT | `https://my.ergoport.dev/cgi-bin/sigmaswap/sigmamint/` |
| **SigmaBridge** | Take funds out of Ergo: ERG and tokens to other chains over the Rosen bridge, or ERG sold for another coin through the Swapuz exchange | `https://my.ergoport.dev/cgi-bin/sigmaswap/sigmabridge/` |
| **SigmaStats** | Ergo network figures, a portfolio view of any address, and price alerts. Read-only: it never asks for a signature | `https://my.ergoport.dev/cgi-bin/sigmaswap/sigmastats/` |
| **SigmaLithos** | The Lithos mining pool: a pool dashboard, what a mining address earned, buying or selling LIT on LithosDex, providing liquidity there, and lending collateral to the pool | `https://my.ergoport.dev/cgi-bin/sigmaswap/sigmalithos/` |
| **SigmaFi** | Peer-to-peer loans with SigmaFi V2 bonds: borrow ERG against tokens, fund a request, post a standing offer to lend, repay, add collateral, and collect | `https://my.ergoport.dev/cgi-bin/sigmaswap/sigmafi/` |

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
  Lithos, Swapuz, Lighthouse or the Ergo Platform.

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
- **Swapuz is a custodial exchange, not a bridge.** It holds your ERG until it pays out the other coin. The
  rate shown is an estimate, there is a high minimum, and Swapuz can put an exchange on hold and ask for
  identity documents. Problems with an exchange are between you and Swapuz (support@swapuz.com).
- **LIT "Swap now" trades with the LithosDex pool directly**, in one transaction you sign, with no miner fee. If
  another trade uses the pool first, yours is rejected and nothing is spent. It needs the newer library build in
  `vendor/`; without it only orders are offered.
- **LIT orders wait for a Lithos miner.** An order on LithosDex is filled by a Lithos miner, usually within a
  few minutes, at the pool price then and never below your minimum. If the price is out of reach it waits. LIT is thinly traded, so larger trades move the price a lot. You can cancel until it fills.
  Signing needs the 2.5 release of the Ergo wallet app from Google Play or the Apple App Store, or the latest 2.5 Android APK from GitHub; other builds refuse to sign and nothing is sent.
- **A LithosDex liquidity position is an NFT.** Whoever holds the LD-NFT owns the position, its fees and its
  share of the pool. Send it away or burn it and the position is gone. Collect fees before closing: closing
  leaves uncollected fees behind for good.
- **SigmaFi loans are between two people, on new contracts.** A borrower who has not repaid in full by
  maturity can no longer repay, and the lender takes ALL the collateral whatever it is worth. A lender gets the
  collateral, not ERG, if the borrower does not repay, and nothing checks what a collateral token is worth
  (only SigUSD is measured, by the ERG/USD oracle, at 1 USD each). The lender's claim is a bond token: lose it
  and the claim is lost. SigmaFi V2's authors still describe it as not deployed and re-issued its contracts
  several times in November 2026; this app pins one version and does not see loans made under another.
  Only the ERG market, plain requests, and the maturity and ERG/SigUSD oracle rules are handled.
  A standing offer to lend only funds SigUSD-backed loans, and anyone may match it with a request that meets
  its terms: the lender does not choose the borrower. Each loan from an offer costs the offer 0.005 ERG.
- **Lending collateral cannot be cancelled.** A position locks 2.915 ERG, an optional priority fee and a LIT
  permit (several thousand LIT) until a Lithos miner uses it. How long that takes depends on the queue and on
  how often Lithos finds blocks; if it stopped finding blocks the position would keep waiting. When it is used
  the LIT returns at once and the block's reward (3 ERG plus fees) is paid to your key, collectable in the app
  about a day later. One position per address: Lithos's rules forfeit a duplicate, so the app refuses one, but
  it cannot stop you opening a second from the same address elsewhere.
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
- **Swapuz exchanges** open an order with Swapuz when you sign, then pay your ERG to the deposit address Swapuz
  gives for that order. The app checks that the order matches what you asked and that the deposit address is a
  normal Ergo address before it builds the payment.
- Before a transaction reaches your wallet, the pool and order contracts involved are evaluated
  against it. One that a contract would reject is never handed over.

Pool data comes from the ErgoDEX analytics API and the public Ergo explorer.

## Running your own copy

It is a set of Perl CGI scripts with one Python helper (using
[ergo-lib-python](https://pypi.org/project/ergo-lib-python/)), served by Apache from
`/usr/lib/cgi-bin` on Debian or Ubuntu. There is no build step and no database.

| Path | Purpose |
|---|---|
| `api.pl`, `sigmalp/`, `sigmamint/`, `sigmabridge/`, `sigmastats/`, `sigmalithos/`, `sigmafi/` | The seven apps (Mosaik views and ErgoPay endpoints; `sigmafi/` runs the code in `sigmalithos/api.pl` under its own name) |
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

Copyright (C) 2026 the SigmaSwap authors.

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU Affero General Public License, version 3, as published by the Free Software Foundation. It is
distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. The full text is in [`LICENSE`](LICENSE).

In practice: you may use, copy, change and run it, including commercially. If you pass it on, or
run a changed version as a service that other people use over a network, you must make the source
of your version available to them under the same licence.

The token logos in `icons/` belong to their projects and are not covered by this licence. Lithos's
order contract templates in `sigmaswap.py` come from the Lithos client, which its authors published
under CC0.
