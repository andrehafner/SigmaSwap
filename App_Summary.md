# SigmaSwap — App Summary

## 1. App overview

- A Mosaik dApp (runs inside the Ergo wallet app) that swaps ERG and Ergo tokens against the
  ErgoDex liquidity pools. Swaps only — no liquidity provision, no order book.
- Two ways to execute a swap: **direct** (one transaction against the pool) or, for ERG pairs,
  an **order** (funds go into an ErgoDex swap-order contract that ErgoDex's bots execute).
- Used by anyone with an Ergo wallet. It is public: no Cornell login, no accounts.
- The server prepares an unsigned transaction; the user's wallet displays and signs it (ErgoPay).
  The server never holds keys or funds. It stores no accounts; the only thing it keeps is a
  short-lived note of each transaction it built, to report afterwards whether it went through.

## 2. Roles & permissions

- **Anonymous user** (the only role): view pairs, get a quote, request a swap / order / order-refund
  transaction for an address they choose, list the open orders of an address.
- No admin role, no admin pages, no permission table. Configuration is changed only by a server
  administrator editing the env file.
- What protects a user's funds is their wallet signature, not this app: a transaction built for an
  address is useless to anyone who does not hold that address's key.

## 3. Data inventory

- **No database, no tables.** (The standard `permissions` / `users` / `*_history` tables do not
  apply: there are no users or records.)
- **Pool-list cache** — `CACHE_DIR/pools.json`: pool ids, token ids/tickers/decimals, liquidity
  (USD). Public blockchain data. Classification: **low**. Rebuilt every `POOLS_TTL` seconds.
- **Transaction notes** — `CACHE_DIR/tx/<random id>.json`, one per transaction handed to a wallet:
  transaction id, the box it depends on, address, token ids, amounts, time. All of it becomes public
  on-chain once the transaction confirms. Classification: **low**. Deleted after two days.
- **Request data, otherwise not stored**: Ergo address, token ids, amounts. Ergo addresses are public
  pseudonymous identifiers. Classification: **low**. No FERPA data, no NetIDs, no names, no email.
- **Logs** (`ssl_error_log`): transaction id + first 8 characters of the pool id per built swap,
  and error details. Apache's access log records the ErgoPay request URL, which contains the
  address and amounts (public on-chain once the swap confirms).

## 4. Data flow

- **Open app**: wallet `GET api.pl` → start screen with two choices, each explained: *Direct swap*
  and *Order* (with a front-running warning). The choice travels as `&mode=` in the action URLs.
- **Simple mode**: a switch on the start screen. The same swaps with fewer details on screen
  (no rate, price impact, fee breakdown or slippage control; slippage is fixed at 1 %). The
  high-price-impact and front-running warnings are shown in both modes.
- **Form**: `registry()` reads the cached pool list (or `sigmaswap.py pools` → ErgoDex analytics
  API) → Mosaik view JSON for that mode (order mode lists ERG pairs only).
- **Change address / token / direction**: wallet `POST api.pl?action=refresh|flip` with the current
  input values (JSON) → values are validated → form re-rendered; balance of the chosen token read
  from the Ergo explorer.
- **Preview**: `POST api.pl?action=quote` → `sigmaswap.py quote` reads the pool box from the
  explorer, checks it is one of the two known ErgoDex pool contracts, applies the constant-product
  formula → quote card + an `ErgoPayAction` whose URL carries pool, pay token, amount, minimum
  output and address.
- **Swap (submit)**: user taps Swap → wallet `GET api.pl?action=ergopay&…` → parameters
  validated → `sigmaswap.py build`: re-reads the pool, re-prices, enforces the minimum output,
  selects the address's boxes, builds `[pool, user boxes] → [new pool, user change, miner fee]`,
  proves the pool contract accepts it, reduces it → `{reducedTx, address, message}` → wallet shows
  the transaction → user signs → wallet broadcasts. The server is not involved after that.
- **Pending swaps**: the pool box used is the confirmed one advanced over swaps still in the
  mempool (explorer mempool API), and the address's own unconfirmed spends/change are taken into
  account, so several swaps on one pool can land in the same block. `USE_MEMPOOL=0` turns it off.
- **Status**: each quote carries a random 128-bit id. When the wallet fetches the transaction, the
  server writes a note under that id. After signing, the wallet calls `api.pl?action=status&n=<id>`
  → `sigmaswap.py status` looks the transaction up: confirmed, waiting in the mempool, replaced
  (the box it needed was spent by another transaction → "did not go through, nothing spent"), or
  not seen. For orders it also reports waiting / executed (with the amount received) / cancelled.
- **Order (submit)**: same request with `&o=1` → `sigmaswap.py build` (mode `order`): builds
  `[user boxes] → [order box, user change, miner fee]`. The order box is guarded by the ErgoDex
  swap-order contract carrying the user's key, pool, minimum output and execution-fee rate. Before
  release, the helper builds the transaction a bot would use to execute that exact box and checks
  that both the pool and the order contract accept it. ErgoDex's bots then execute it on-chain;
  this server takes no further part.
- **Open orders**: `POST api.pl?action=orders` → `sigmaswap.py orders` asks the explorer for
  unspent boxes of the two order contracts and keeps those carrying the address's key. Nothing is
  stored; the list is read from the chain each time.
- **Cancel order (delete)**: `GET api.pl?action=ergopay&r=<box id>&addr=…` → `sigmaswap.py refund`
  verifies the box is unspent, is one of the two order contracts and carries that address's key,
  then builds `[order box] → [everything back to the address, miner fee]`. Only the key holder can
  sign it.
- **Approve**: not applicable.
- **Landing page**: browser `POST api.pl` (FormData `action=pools`) → pair list.

## 5. Authentication & authorization

- **Authentication: none, by design.** The app is not behind Shibboleth
  (`deploy/sigmaswap.conf`: `AuthType None`, `ShibRequestSetting requireSession false`,
  `Require all granted`). The Ergo wallet app cannot perform an interactive SSO login.
- **Authorization**: nothing to authorize — there are no stored records, no per-user data, and no
  state-changing server action. Every action is a read of public chain data or the construction of
  an unsigned transaction.
- The submitted address is not trusted as identity; it is only used as the source/destination of
  the transaction, and only the key holder can sign it.

## 6. External integrations

- **Ergo Explorer** (`EXPLORER_URL`, default `https://api.ergoplatform.com`): pool box, address
  boxes and balance, last 10 block headers. Read-only HTTPS GET.
- **ErgoDex analytics API** (`POOLS_API_URL`, default `https://api.ergodex.io/v1/amm/pools/stats`):
  list of pools with liquidity. Read-only HTTPS GET.
- **ergo-lib-python 0.28.0** (sigma-rust) in `venv/`: transaction building and script evaluation.
- **ErgoDex off-chain bots** (third party, not operated by us): execute orders placed in order
  mode and take the execution fee. We do not call them; they watch the chain.
- No SMTP, no Oracle, no MariaDB. Outbound hosts are fixed in configuration, never taken from a
  request (no SSRF surface).

## 7. File locations

- App: `/data/AAP_Storage/apps/sigmaswap/` (URL `/apps/sigmaswap`)
  - `api.pl` all HTTP logic · `AppConfig.pm` config + helper runner · `sigmaswap.py` chain logic
  - `index.html` / `app.js` / `style.css` / `icon.png` landing page
  - `icons/<token id>.png` bundled token logos (shown on the quote card and the landing page),
    built by the dev tool `tools/fetch_icons.py` from ergodex.io and the Spectrum token-logos
    repository; `icons/unknown.png` is the placeholder
  - `venv/` Python virtualenv (not in git) · `deploy/sigmaswap.conf` · `redeploy.sh`
- Settings: `/data/AAP_Storage/secrets/sigmaswap.env` (no secrets inside)
- Cache: `/data/AAP_Storage/apps/uploads/sigmaswap/pools.json`
- Logs: `/var/log/httpd/ssl_error_log`, `/var/log/httpd/ssl_access_log`

## 8. Security controls

- **Input validation (allowlists)** in Perl *and again* in Python: address = base58, 40–60 chars,
  must parse as a mainnet P2PK address; token/pool ids = 64 hex chars and must be in the published
  pool list; amounts = positive integers below 2^63; slippage from a fixed set; request body ≤ 64 KB;
  file uploads disabled.
- **Pool allowlist**: a pool box is used only if its script is byte-for-byte one of the two ErgoDex
  pool contracts and it carries the pool's NFT.
- **Order safety**: only the two ErgoDex native-fee ERG↔token order contracts are used, with
  constants filled in by position; trees built this way were compared byte-for-byte with orders
  the bots executed on-chain. Every order carries the owner's key, so the owner can always take
  the funds back. A box is treated as an order (listed, refundable) only if rebuilding the known
  contract with the box's constants reproduces its script exactly.
- **Transaction safety**: output amounts come from the pool formula; the builder refuses any
  unbalanced ERG/token total (no silent burn); the pool contract is evaluated against the exact
  transaction before it is released; a re-quote below the user's minimum aborts the swap.
- **SQL**: none. **Shell**: the Python helper is started with a list-form exec (no shell), input
  on stdin as JSON, output captured.
- **Output escaping**: token names are untrusted on-chain text — sanitized to printable ≤ 24 chars,
  JSON-encoded for Mosaik (plain-text labels), `escapeHtml()` on the landing page. Tokens that
  share a ticker are shown with their token id.
- **CSRF**: not applicable — no cookies, no session, no ambient credentials. A forged request can
  only obtain a quote or an unsigned transaction. The only server-side write is the transaction
  note, stored under an unguessable random id supplied with the request; file names are validated
  (`^[0-9a-f]{32}$`), so nothing else can be written or read through it.
- **Errors**: generic messages to the client, details only in the server log.
- **Secrets**: none exist. The env file is still kept outside the web root, `640 root:apache`.
- **Web exposure**: Apache serves only the web files; `.py`, `.pm`, `.sh`, `.md`, `venv/`,
  `deploy/`, `.git` are denied. App tree is root-owned and not writable by Apache.
- **Transport**: HTTPS only (ErgoPay `ergopay://` URLs are fetched over HTTPS by the wallet).

## 9. Backup & recovery

- Nothing to back up: no database, no uploads. The cache is disposable and rebuilds itself; losing
  `tx/` only means recent swaps show "waiting for your wallet" instead of their status.
- Recovery = redeploy from git following `DEPLOY.md` (clone, venv, env file, SELinux rules,
  Apache include, `redeploy.sh`).

## 10. Known limitations & risks

- **Public and unauthenticated on a Cornell host** — a deliberate exception to the Shibboleth
  baseline. There is no rate limiting in the app; each quote/swap request makes 1–4 outbound API
  calls and starts a Python process. Add Apache-level rate limiting if it is abused.
- **Financial software**: users move their own funds. Bugs here could produce a bad trade; the
  on-chain pool contract and the wallet's confirmation screen are the final safeguards.
- **Third-party dependencies**: quotes and transactions depend on the public Ergo explorer and
  the ErgoDex API being available and honest. A lying explorer cannot steal funds (the pool
  contract and the user's signature still apply) but could cause failed transactions.
- **Token names can be spoofed.** Only pools above `MIN_TVL_USD` are listed and same-ticker tokens
  show their id, but users must check the token in their wallet.
- **Race with other traders (direct mode)**: if the pool changes between signing and inclusion,
  the transaction is rejected by the network (nothing is spent) and the status screen says so.
  Building on pending swaps makes this rarer; order mode avoids it.
- **Mempool data is best effort.** It comes from the public explorer. If its view is stale, a
  swap built on a pending swap can be rejected (nothing spent); if a swap it was built on is
  dropped, it is dropped too.
- **Orders can be front-run.** A waiting order and its minimum are public; another trader can move
  the price first so the order executes at its minimum. The app warns about this on the start
  screen and on the order form; the user's protection is the slippage setting.
- **Order mode depends on ErgoDex's bots.** They are a third party. Recent ERG↔token orders were
  executed within 0–1 blocks (checked on-chain), but if the bots stop, or the price stays below an
  order's minimum, the order simply waits; the funds stay in the order box until the user cancels
  it. Cancelling costs one network fee. Execution costs about 0.006 ERG (max 0.0072).
- **Order mode covers ERG pairs only.** Token↔token orders showed no recent bot executions, so
  those pairs stay direct-only.
- **Open orders are found by scanning** up to 400 unspent boxes per order contract; an address's
  order beyond that window would not be listed (the user could still refund it on ergodex.io).
- **Single address**: funds are taken from, and change returns to, the one address the user picks;
  boxes on the wallet's other addresses are not used. Confirmed boxes only, at most 60 inputs.
- **Order mode has not been executed by a real bot from this app**: order creation, a simulated
  bot execution against live pools, and refunds were all signed with a test wallet in ergo-lib,
  but no order from this app was broadcast. Make the first real order a small one.
- Data classification: low-risk public data only (Policy 5.10); no moderate/high-risk data.
