# SigmaSwap — App Summary

## 1. App overview

- A Mosaik dApp (runs inside the Ergo wallet app) that swaps ERG and Ergo tokens against the
  ErgoDex liquidity pools. Swaps only — no liquidity provision, no order book.
- Two ways to execute a swap: **direct** (one transaction against the pool) or, for ERG pairs,
  an **order** (funds go into an ErgoDex swap-order contract that ErgoDex's bots execute).
- Used by anyone with an Ergo wallet. It is public: no login, no accounts.
- The server prepares an unsigned transaction; the user's wallet displays and signs it (ErgoPay).
  The server never holds keys or funds. It stores no accounts; the only thing it keeps is a
  short-lived note of each transaction it built, to report afterwards whether it went through.

- **SigmaMint** (`sigmamint/`, same deployment): mints fungible tokens and NFTs (EIP-4) the same way,
  as unsigned transactions signed in the user's wallet. Its browser page lets a visitor upload the
  NFT's media, which the server checks and relays to Lighthouse (IPFS/Filecoin).

- **SigmaLP** (`sigmalp/`): add and remove liquidity on the same pools, one direct transaction each.
- **SigmaBridge** (`sigmabridge/`): send ERG and tokens out of Ergo to Cardano, Ethereum, BNB Chain, Bitcoin Runes,
  Bitcoin, Dogecoin or Firo over the Rosen bridge (an independent project). The app builds Rosen's "lock" transaction.
- **SigmaStats** (`sigmastats/`): read-only. Network figures (explorer), a portfolio view of any address valued at
  ErgoDex pool prices, and price alerts. It builds no transactions and asks for no signature.
- **Routes through ERG** (SigmaSwap, direct screen): two tokens with no shared pool are swapped A → ERG → B with one
  signature. The first swap is direct; its ERG output is an ordinary ErgoDex order for B, owned by the user's key,
  which the bot completes. A pool must be output 0 of its transaction, so two pools cannot share one.
- **Order bot**: a systemd timer runs `sigmaswap.py bot` every minute. It completes orders placed
  through SigmaSwap when the pool can pay their minimum, by broadcasting a transaction that needs
  no key. This is the one place the server sends a transaction to the network itself.

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
- **SigmaMint mint notes** — same `tx/` store: token name, description, amount, decimals, IPFS id and
  file fingerprint, later the transaction and token id. All public on-chain after the mint. **Low**.
- **SigmaMint uploads** — NOT stored here. A file is spooled to `CACHE_DIR/tmp/` while it is checked
  and sent to Lighthouse, then deleted. `CACHE_DIR/mint-limits.json` holds upload counters and, for
  the current day only, a salted hash of each uploader's IP address (no raw addresses). **Low**.
- **Price alerts** — `CACHE_DIR/alerts/<hash>.json` (asset id, direction, price, time; at most 10) and
  `<hash>.hits` (alerts reached), one pair per wallet installation. `<hash>` is a hash of the random
  installation id the wallet sends; no address, no personal data. Removed when the last alert is removed. **Low**.
- **Network figures** — `CACHE_DIR/netstats.json`, public chain data cached for a minute. **Low**.
- **Portfolio lookups** — not stored. The address goes to the explorer and into no file. **Low** (public data),
  but note the web server's access log does not contain it either: it is sent in the request body.
- **Order registry** — `CACHE_DIR/orders/<order box id>.json`: order box id, owner address, time,
  and once filled the fill transaction id. Removed when the order is filled or cancelled. **Low**.
- **Notification lists** — `CACHE_DIR/watch/<hash>.json`: per wallet installation (keyed by a hash
  of the random id the wallet sends, never the id itself), the last 40 things it started (note id,
  transaction id, amounts) and the latest message. Entries older than 60 days are dropped. **Low**.
- **LP map** — `CACHE_DIR/lpmap.json`: which LP token belongs to which pool (public chain data).
- **Request data, otherwise not stored**: Ergo address, token ids, amounts. Ergo addresses are public
  pseudonymous identifiers. Classification: **low**. No FERPA data, no NetIDs, no names, no email.
- **Logs** (`/var/log/apache2/error.log`): transaction id + first 8 characters of the pool id per built swap,
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

- **SigmaMint mint**: wallet `POST sigmamint/api.pl?action=preview` (name, description, and
  amount + decimals, or for an NFT the file attached to the wallet session) → input checked →
  a mint note is stored under a random id → wallet `GET ...?action=ergopay&n=<id>&addr=` →
  `sigmaswap.py mint` builds `[user boxes] → [box with the new token + EIP-4 registers, change,
  miner fee]` → signed in the wallet → `action=status` reports it and shows the token id.
- **SigmaMint NFT file**: Mosaik has no file picker, so the NFT form's "Choose file" button opens
  the browser page with the wallet session's id (`?s=`). The id is issued only by the wallet
  screens and signed (HMAC) with a secret kept in `CACHE_DIR/mint-secret`; it is valid for two
  hours. Browser `POST sigmamint/api.pl` (FormData `action=upload`, `s`, one file) → session id
  verified → size and rate limits → `sigmaswap.py upload` checks the file's signature bytes against
  an allowlist, hashes it, sends it to Lighthouse with the API key → the content id and hash are
  stored against the session. Back in the wallet, Preview picks them up. Without a valid session
  id the page offers no upload and the endpoint refuses.

- **Limit order**: its own screen from the start page. It shows the pool's current price and a
  price field that starts there; amount x price becomes the order's minimum. Above today's price
  the order waits; at or below it, it fills right away. Before it is created the order contract is checked against the
  pool as it will look at the limit price, with both a standard fill and the bot's fill.
- **Bot fill**: `sigmaswap.py bot` reads `orders/`, re-reads each order box and its pool from the
  chain, and when the pool pays at least the order's minimum builds `[pool, order] → [pool, all of
  the proceeds and the unused fee deposit to the owner, miner fee]`. Both contracts are evaluated;
  the transaction is posted to the explorer's submit endpoint. It cannot pay anyone but the owner
  the contract names: the order contract would reject it.
- **Notifications**: wallet `GET api.pl?action=notify` with its installation id → the app checks
  the open items of that installation on-chain → "your order was filled", "your swap completed", etc.
- **SigmaLP**: `POST sigmalp/api.pl?action=addq|posq` → `lpquote` / `lppositions` read the pool →
  ErgoPay → `lpbuild` builds `[pool, user boxes] → [pool, user, miner fee]` under the pool's own
  deposit/redeem rules, evaluates the pool contract against it, and enforces a 1 % tolerance.
- **SigmaSwap route through ERG**: preview → two `quote` calls (A→ERG, then ERG→B for the ERG that remains after
  the order's fee deposit). ErgoPay (`&h=<second pool>`) → `build` mode `hop` builds
  `[pool A, user boxes] → [pool A, order box (ERG→B, user's key, minimum of B), user change, miner fee]`, proves
  pool A accepts it, then simulates the order's execution against pool B and checks the order parses back as the
  user's own with the agreed amount and minimum. Both pools must be on the published list; the minimum may not be
  more than 10 % below the current quote. The order is registered for the bot and tracked like any order.
- **SigmaStats**: `GET sigmastats/api.pl` → `netstats` (explorer) + pool list. `POST ?action=portq` → `portfolio`
  reads the address's confirmed balance; tokens are valued only if they are on the published pool list (names of
  all other tokens are shown with their id and marked unverified). `POST ?action=alertset` stores an alert; the
  bot (`cmd_alerts`) compares alerts with pool prices each minute and appends reached ones to the `.hits` file;
  `GET ?action=notify` reports the newest reached alert to that installation.
- **SigmaBridge**: `POST sigmabridge/api.pl?action=preview` → `bridgequote` reads Rosen's on-chain
  fee table for the token, validates the target address with that chain's checksum and Rosen's own
  format rules (Cardano bech32 mainnet; Bitcoin `bc1q`; Runes Taproot `bc1p`; EVM hex with EIP-55 when
  mixed case; Dogecoin and Firo Base58Check), requires
  recent activity at the lock address → the previewed transfer is stored under a random id →
  ErgoPay → `bridgebuild` recomputes the fees (refuses if they rose), builds `[user boxes] → [lock
  box with R4 = chain, target address, fees, sender; change; miner fee]`, and reads the lock box
  back out of the built transaction to compare it with the request before releasing it.

## 5. Authentication & authorization

- **Authentication: none, by design.** There is no login of any kind
  (`deploy/sigmaswap.conf`: `Require all granted`). The Ergo wallet app cannot perform an interactive SSO login.
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
- **Lighthouse** (`upload.lighthouse.storage`, `gateway.lighthouse.storage`): SigmaMint stores
  uploaded NFT media there with the operator's API key. The wallet loads the preview picture
  from the gateway directly. Files on IPFS are public and effectively permanent.
- **Rosen bridge** (third party): SigmaBridge sends funds to Rosen's lock address; delivery on the
  other chain is done by Rosen's watchers and guards. Lock address, fee-table token and box format
  were taken from Rosen's public code and confirmed against live transfers on-chain.
- **Explorer submit endpoint**: used only by the order bot to broadcast fill transactions.
- No e-mail and no database (no MySQL or MariaDB). Outbound hosts are fixed in configuration, never taken from a
  request (no SSRF surface).

## 7. File locations

- App: `/usr/lib/cgi-bin/sigmaswap/` (URL `/cgi-bin/sigmaswap/`)
  - `api.pl` all HTTP logic · `AppConfig.pm` config + helper runner · `sigmaswap.py` chain logic
  - `index.html` / `app.js` / `style.css` / `icon.png` landing page
  - `icons/<token id>.png` bundled token logos (shown on the quote card and the landing page),
    built by the dev tool `tools/fetch_icons.py` from ergodex.io and the Spectrum token-logos
    repository; `icons/unknown.png` is the placeholder
  - `venv/` Python virtualenv (not in git) · `deploy/sigmaswap.conf` · `redeploy.sh`
- Settings: `/etc/sigmaswap/sigmaswap.env` (holds the one secret, `LIGHTHOUSE_API_KEY`)
- Cache: `/var/lib/sigmaswap/` (`pools.json`, `tx/`, `watch/`, `orders/`, `tmp/`, `mint-secret`, `mint-limits.json`)
- Logs: `/var/log/apache2/error.log`, `/var/log/apache2/access.log`

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
- **Secrets**: one, `LIGHTHOUSE_API_KEY`, in the env file outside the web root (`640 root:www-data`).
  It is passed to the helper on stdin (never on a command line), sent only as the Authorization
  header to the configured Lighthouse host, never returned to a client and never logged.
- **Upload controls (SigmaMint)**: size limit (default 10 MB, enforced before and after receipt);
  type allowlist by signature bytes, not by name or declared type (PNG, JPG, GIF, WEBP, MP3, WAV,
  OGG, MP4, WEBM — no SVG or HTML); the client's file name is discarded; per-visitor hourly and
  global daily limits; kill switch `MINT_UPLOADS=0`; nothing is kept on this server.
- **Uploads only from a wallet session**: the upload endpoint requires a signed, unexpired session
  id that only the wallet screens hand out; forged or stale ids are refused.
- **No server-side fetching of user-supplied locations**: SigmaMint never downloads anything a
  user points it at.
- **Token names**: names equal to a token in the swap list are refused, and the app states that a
  name does not make a token official.
- **Web exposure**: Apache serves only the web files; `.py`, `.pm`, `.sh`, `.md`, `venv/`,
  `deploy/`, `.git` are denied. App tree is root-owned and not writable by the web server.
- **Transport**: HTTPS only (ErgoPay `ergopay://` URLs are fetched over HTTPS by the wallet).

## 9. Backup & recovery

- Nothing to back up: no database, no uploads. The cache is disposable and rebuilds itself; losing
  `tx/` only means recent swaps show "waiting for your wallet" instead of their status.
- Recovery = redeploy from git following `DEPLOY.md` (clone, venv, settings file,
  Apache include, `redeploy.sh`).

## 10. Known limitations & risks

- **Public and unauthenticated.** There is no rate limiting in the app; each quote/swap request makes 1–4 outbound API
  calls and starts a Python process. Add Apache-level rate limiting if it is abused.
- **SigmaMint: anonymous uploads go to the operator's Lighthouse account.** Uploading needs
  a session started in the wallet app, which stops drive-by use of the page, but the app has no
  login, so anyone who opens it can still store a picture, sound or video (up to the limits) under
  that account, and it cannot be removed from IPFS afterwards. Content is not reviewed or scanned. The limits and the kill switch
  bound the volume, not the content. This is the main policy risk of the deployment.
- **The order bot sends transactions on its own.** It holds no keys and can only fill an order in
  the owner's favour (the contract enforces it), but it is an always-on process
  acting on a public blockchain. `ORDER_BOT=0` or disabling the timer stops it; orders then
  wait for ErgoDex's own bots or the owner's cancel.
- **Limit orders may never fill**, and fill only while the bot runs. They can be cancelled any time.
- **Routes through ERG are half order.** The second swap waits for a bot and can be front-run down to the user's
  minimum; if it cannot fill, cancelling returns ERG, not the original token. No route order from this app has been
  filled on-chain yet: the build was checked against live pools (pool acceptance and a simulated execution) only.
- **Price alerts are best effort.** They use the pool list from the ErgoDex API, checked once a minute, and reach
  the user only when the wallet next asks for notifications. They are not a stop-loss. Not yet seen in a real wallet.
- **SigmaStats prices are pool prices**, and the ERG price is its SigUSD pool price, which can differ from exchanges.
- **SigmaBridge: funds leave Ergo irreversibly.** A wrong (but well-formed) target address, or an
  address the user does not control, loses the funds. Checksums catch typos, not wrong addresses.
- **SigmaBridge depends on Rosen**, an independent bridge: delivery time, availability and the
  safety of locked funds are Rosen's. If Rosen changes contracts, bridging stops until the built-in
  values are updated; a stale lock address is the scenario the activity check exists to prevent.
- **SigmaBridge has not carried a real transfer.** Its lock box encoding matches 37 of 37 recent
  real transfers byte for byte and its fees match the real ones under the current fee table, but
  no transfer from this app has been broadcast. First use should be the minimum amount.
- **SigmaLP: liquidity providers can lose value** relative to holding when prices move.
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
- Data classification: public blockchain data only; no personal or account data.
