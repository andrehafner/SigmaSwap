# SigmaSwap — App Summary

## 1. App overview

- A Mosaik dApp (runs inside the Ergo wallet app) that swaps ERG and Ergo tokens against the
  ErgoDex liquidity pools. Swaps only — no liquidity provision, no order book.
- Used by anyone with an Ergo wallet. It is public: no Cornell login, no accounts.
- The server prepares an unsigned transaction; the user's wallet displays and signs it (ErgoPay).
  The server never holds keys or funds and stores no user data.

## 2. Roles & permissions

- **Anonymous user** (the only role): view pairs, get a quote, request a swap transaction for an
  address they choose.
- No admin role, no admin pages, no permission table. Configuration is changed only by a server
  administrator editing the env file.
- What protects a user's funds is their wallet signature, not this app: a transaction built for an
  address is useless to anyone who does not hold that address's key.

## 3. Data inventory

- **No database, no tables.** (The standard `permissions` / `users` / `*_history` tables do not
  apply: there are no users or records.)
- **Pool-list cache** — `CACHE_DIR/pools.json`: pool ids, token ids/tickers/decimals, liquidity
  (USD). Public blockchain data. Classification: **low**. Rebuilt every `POOLS_TTL` seconds.
- **Request data, not stored**: Ergo address, token ids, amounts. Ergo addresses are public
  pseudonymous identifiers. Classification: **low**. No FERPA data, no NetIDs, no names, no email.
- **Logs** (`ssl_error_log`): transaction id + first 8 characters of the pool id per built swap,
  and error details. Apache's access log records the ErgoPay request URL, which contains the
  address and amounts (public on-chain once the swap confirms).

## 4. Data flow

- **Open app**: wallet `GET api.pl` → `registry()` reads the cached pool list (or `sigmaswap.py
  pools` → ErgoDex analytics API) → Mosaik view JSON.
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
- **Approve / delete**: not applicable.
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
- No SMTP, no Oracle, no MariaDB. Outbound hosts are fixed in configuration, never taken from a
  request (no SSRF surface).

## 7. File locations

- App: `/data/AAP_Storage/apps/sigmaswap/` (URL `/apps/sigmaswap`)
  - `api.pl` all HTTP logic · `AppConfig.pm` config + helper runner · `sigmaswap.py` chain logic
  - `index.html` / `app.js` / `style.css` / `icon.png` landing page
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
- **Transaction safety**: output amounts come from the pool formula; the builder refuses any
  unbalanced ERG/token total (no silent burn); the pool contract is evaluated against the exact
  transaction before it is released; a re-quote below the user's minimum aborts the swap.
- **SQL**: none. **Shell**: the Python helper is started with a list-form exec (no shell), input
  on stdin as JSON, output captured.
- **Output escaping**: token names are untrusted on-chain text — sanitized to printable ≤ 24 chars,
  JSON-encoded for Mosaik (plain-text labels), `escapeHtml()` on the landing page. Tokens that
  share a ticker are shown with their token id.
- **CSRF**: not applicable — no cookies, no session, no ambient credentials, no server-side state
  change. A forged request can only obtain a quote or an unsigned transaction.
- **Errors**: generic messages to the client, details only in the server log.
- **Secrets**: none exist. The env file is still kept outside the web root, `640 root:apache`.
- **Web exposure**: Apache serves only the web files; `.py`, `.pm`, `.sh`, `.md`, `venv/`,
  `deploy/`, `.git` are denied. App tree is root-owned and not writable by Apache.
- **Transport**: HTTPS only (ErgoPay `ergopay://` URLs are fetched over HTTPS by the wallet).

## 9. Backup & recovery

- Nothing to back up: no database, no uploads. The cache is disposable and rebuilds itself.
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
- **Race with other traders**: if the pool changes between signing and inclusion, the transaction
  is rejected by the network (nothing is spent) and the user must preview again.
- **Single address**: funds are taken from, and change returns to, the one address the user picks;
  boxes on the wallet's other addresses are not used. Confirmed boxes only, at most 60 inputs.
- **Explorer lag**: a second swap right after the first may fail until the first one confirms.
- **Not tested here on a physical wallet**: the Mosaik JSON was written against the Mosaik
  reference serializer/runtime, and the ErgoPay payload was signed with a test wallet in
  ergo-lib, but the first on-device run should use a small amount.
- Data classification: low-risk public data only (Policy 5.10); no moderate/high-risk data.
