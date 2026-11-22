# SigmaSwap — Deployment (Debian / Ubuntu, Apache, `/usr/lib/cgi-bin`)

Installs the six apps (SigmaSwap, SigmaLP, SigmaMint, SigmaBridge, SigmaStats, SigmaLithos) from one checkout:

| | |
|---|---|
| Code | `/usr/lib/cgi-bin/sigmaswap/` |
| URL | `https://YOUR-HOST/cgi-bin/sigmaswap/` (and `…/sigmalp/`, `…/sigmamint/`, `…/sigmabridge/`, `…/sigmastats/`, `…/sigmalithos/`) |
| Settings | `/etc/sigmaswap/sigmaswap.env` |
| Cache | `/var/lib/sigmaswap/` (writable by the web server, not served) |
| Logs | `/var/log/apache2/error.log` (lines start with `sigmaswap`) |

The examples use `my.ergoport.dev`. Replace it with your host name.

**There is no database.** The apps need no MySQL or MariaDB: no tables, no user, no grants. They
keep a few small files in the cache directory and read everything else from the blockchain.

The server must already serve HTTPS for the host name and have the standard
`ScriptAlias /cgi-bin/ /usr/lib/cgi-bin/` (Debian's `serve-cgi-bin` conf, on by default). On a
RHEL-family server with SELinux, also read the appendix at the end.

## 1. Packages

```bash
sudo apt update
sudo apt install -y apache2 git perl libcgi-pm-perl python3 python3-venv ca-certificates curl
```

Everything else the Perl scripts use ships with Perl itself. Python 3.8 or newer is required.

## 2. Code

```bash
sudo git clone https://github.com/andrehafner/SigmaSwap.git /usr/lib/cgi-bin/sigmaswap
cd /usr/lib/cgi-bin/sigmaswap
```

## 3. Python virtualenv (transaction builder)

```bash
cd /usr/lib/cgi-bin/sigmaswap
sudo python3 -m venv venv
sudo venv/bin/python -m pip install -r requirements.txt
sudo venv/bin/python -I -c 'import ergo_lib_python; print("ok")'      # must print ok
```

## 4. Settings file + cache directory

```bash
sudo mkdir -p /etc/sigmaswap /var/lib/sigmaswap
sudo chown www-data:www-data /var/lib/sigmaswap && sudo chmod 750 /var/lib/sigmaswap
sudo cp sigmaswap.env.example /etc/sigmaswap/sigmaswap.env
sudo nano /etc/sigmaswap/sigmaswap.env
```

Set these three lines (everything else can stay as it is):

```
CACHE_DIR=/var/lib/sigmaswap
BASE_URL=https://my.ergoport.dev/cgi-bin/sigmaswap
LIGHTHOUSE_API_KEY=<your Lighthouse key>      # the only secret; empty = NFT minting off, tokens still work
```

Set `BASE_URL` always. It is what the signing links handed to wallets are built from, and it is
required if HTTPS ends at a proxy (Cloudflare, nginx) in front of Apache.

## 5. Apache

```bash
sudo cp deploy/sigmaswap.conf /etc/apache2/conf-available/sigmaswap.conf
sudo a2enmod cgid headers
sudo a2enconf sigmaswap
sudo apache2ctl configtest && sudo systemctl reload apache2
```

Why the include is needed: `/cgi-bin/` is a ScriptAlias, so Apache would try to *run* every file
in it, including `index.html`, the style sheet and the logos. The include makes only the `.pl`
files programs, serves the web files as plain files, and refuses everything else (`.py`, `.pm`,
`.md`, `venv/`, `deploy/`, `tools/`, `.git`).

If a module was newly enabled, use `sudo systemctl restart apache2` instead of reload.

## 6. Owner and permissions

```bash
cd /usr/lib/cgi-bin/sigmaswap && sudo ./redeploy.sh
```

It must end with `Done.` Any line marked `WRONG` says what to fix. Run it again after every
change to the directory.

## 7. Checks (do these before opening the wallet)

```bash
H=https://my.ergoport.dev/cgi-bin/sigmaswap

# C1 the whole stack through Apache
curl -sS "$H/api.pl?action=health"; echo
#   {"success":1,"helper":"ok","height":19xxxxx,"pools":NN,"base_url":"https://my.ergoport.dev/cgi-bin/sigmaswap"}
#   base_url must be exactly your public https address.

# C2 the six apps answer with their Mosaik JSON
for a in "" sigmalp/ sigmamint/ sigmabridge/ sigmastats/ sigmalithos/; do curl -sS "$H/${a}api.pl" | head -c 45; echo; done
#   {"manifest":{"appName":"SigmaSwap",...   SigmaLP, SigmaMint, SigmaBridge, SigmaStats, SigmaLithos

# C3 web files are served as files (200), source and secrets are not (403)
for p in / style.css icons/unknown.png sigmamint/ sigmaswap.py AppConfig.pm venv/pyvenv.cfg .git/config README.md; do
  printf '%s  %s\n' "$(curl -sS -o /dev/null -w '%{http_code}' "$H/$p")" "$p"; done
#   200 for the first four, 403 for the rest

# C4 the cache is being written
sudo ls -l /var/lib/sigmaswap/          # pools.json appears after C1
```

Then, in the Ergo wallet app: **dApps → enter `https://my.ergoport.dev/cgi-bin/sigmaswap/`**, and
the same with `sigmalp/`, `sigmamint/`, `sigmabridge/`, `sigmastats/`, `sigmalithos/` appended.

## 8. Order bot (limit orders, routes through ERG, price alerts)

Orders are completed by a small bot: once a minute it looks for open SigmaSwap orders and, when
the pool can pay an order's minimum, broadcasts the transaction that fills it. That covers limit
orders and the second half of a token-to-token swap through ERG. In the same run it checks
SigmaStats price alerts against the pool prices (`PRICE_ALERTS=0` switches that part off). It holds no keys; filling an order is a transaction anyone may send. Without it,
limit orders wait for somebody else's bot.

```bash
cd /usr/lib/cgi-bin/sigmaswap
sudo cp deploy/sigmaswap-bot.service deploy/sigmaswap-bot.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sigmaswap-bot.timer

sudo systemctl start sigmaswap-bot.service; systemctl status sigmaswap-bot.service --no-pager | head -8
#   status=0/SUCCESS (it exits at once when there is nothing to do)
systemctl list-timers sigmaswap-bot.timer --no-pager          # next run within a minute
sudo journalctl -u sigmaswap-bot.service -n 20 --no-pager     # "filled order ..." lines appear here
```

It runs as `www-data` with the same settings file. `ORDER_BOT=0` in the settings file makes every
run exit immediately; `sudo systemctl disable --now sigmaswap-bot.timer` removes it.

## Updating

```bash
cd /usr/lib/cgi-bin/sigmaswap && sudo git pull && sudo ./redeploy.sh
```

If `requirements.txt` changed, also run `sudo venv/bin/python -m pip install -r requirements.txt`.
If `deploy/sigmaswap.conf` changed, copy it again (step 5). If a Mosaik screen changed, the
`APP_VERSION` in that app's `api.pl` was bumped, and wallets that have the app open reload it.

## Before going public

1. **Rate limiting.** There is no login, and every request starts a Python process and makes
   several calls to the Ergo explorer. Nothing in the code limits how often one visitor may do
   that. Put a per-address limit on `/cgi-bin/sigmaswap` first, for example
   `sudo apt install libapache2-mod-evasive` (or `libapache2-mod-qos`), or a rate-limiting rule at
   your CDN. A few requests per second per address is far more than a real wallet needs.
2. **The cache directory must stay outside anything Apache serves.** It holds `mint-secret` (the
   key that signs upload sessions), upload counters, transaction notes and notification lists.
   `/var/lib/sigmaswap` is correct; never point `CACHE_DIR` into `/usr/lib/cgi-bin` or `/var/www`.
3. **The settings file** holds the Lighthouse and Swapuz keys: `640 root:www-data` (`redeploy.sh` sets it).
4. **Lighthouse uploads use your account.** The limits and the kill switch (`MINT_UPLOADS=0`) are
   in `sigmaswap.env.example`.

## Notes on the individual apps

**SigmaMint.** The server relays files chosen in the "Choose file" step to Lighthouse, so it needs
outbound HTTPS to `upload.lighthouse.storage`. `curl -sS -F action=config $H/sigmamint/api.pl`
answering `{"success":1,"uploads":0,"max_mb":10}` is correct: uploading only works from a session
started in the wallet. The real test is in the wallet: Mint an NFT → Choose file → small picture →
"File added" → back in the wallet, Preview shows the picture. Files are spooled to `tmp/` in the
cache directory while they are checked and relayed, then deleted.

**SigmaBridge, Swapuz option.** Set `SWAPUZ_API_KEY` in the settings file (the key from your Swapuz partner
account) and make sure the server reaches `api.swapuz.com`:
`curl -sS -m 15 'https://api.swapuz.com/api/home/getLimits?coin=ERG'` must answer with a `minAmount`. Without
the key the start screen shows Swapuz as not available and everything else works. The coin list is cached for
ten minutes in `swapuz-coins.json`. An order is opened with Swapuz only when a wallet fetches the transaction
to sign; an order nobody pays simply expires there. `error.log` records the Swapuz order id and the payment
transaction id for each one (`sigmabridge: swapuz order …`), which is what Swapuz support asks for.

**SigmaBridge, Rosen option.** The Rosen lock address and fee-table token are built into `sigmaswap.py`. When
Rosen upgrades its contracts the app refuses to bridge by itself ("The bridge is unavailable right
now"), because it requires recent activity at the lock address and a readable fee table. To
switch before a code update, set `ROSEN_LOCK_ADDRESS` / `ROSEN_MINFEE_NFT` in the settings file,
taking the values ONLY from Rosen's own release file. `sigmabridge/tokens.json` is refreshed with
`tools/fetch_bridge_tokens.py`.

**SigmaStats.** Read-only: network figures from the explorer (cached for a minute in
`netstats.json`), a portfolio view, and price alerts. Alerts live in `alerts/` in the cache
directory, one small file per wallet installation, at most 10 alerts each. Without the bot timer
(step 8) alerts are never reached.

**SigmaLithos.** Needs no key. The dashboard reads Lithos's public statistics, so the server must reach
`lithosprotocol.org` (`curl -sS -m 15 https://lithosprotocol.org/api/v1/stats | head -c 80`); they are cached for a
minute in `lithos-stats.json`. "My mining" and LIT trading read the chain through the explorer. LIT orders are
filled by Lithos miners, not by this service's bot. The order contracts and the addresses of Lithos's payout and
holding contracts are built into `sigmaswap.py`; if Lithos redeploys its contracts, "My mining" shows nothing and
new orders would not be filled until the app is updated, so watch their release notes.

**Notifications.** All six apps tell the wallet where to ask for news (`api.pl?action=notify`).
What an installation started is kept in `watch/` in the cache directory. Nothing to install.

**Token logos.** `icons/` are ordinary files in the repository. To add logos for newly listed
tokens run `python3 tools/fetch_icons.py` on a development machine (needs node + playwright),
commit, and update as usual. A token without a logo shows a placeholder.

## Troubleshooting

Read **`/var/log/apache2/error.log`** first (or the vhost's own `ErrorLog` if it sets one).

```
index.html gives 500 "Exec format error" / "End of script output before headers: index.html"
  -> Apache is trying to run the page: deploy/sigmaswap.conf is not active.
     step 5, then: sudo apache2ctl -S | head ; ls /etc/apache2/conf-enabled/sigmaswap.conf

api.pl is shown or downloaded as text
  -> mod_cgid/mod_cgi is not loaded: sudo a2enmod cgid && sudo systemctl restart apache2

404 on everything under /cgi-bin/sigmaswap/
  -> this virtual host has no ScriptAlias for /cgi-bin/: sudo a2enconf serve-cgi-bin, or add
     "ScriptAlias /cgi-bin/ /usr/lib/cgi-bin/" to the HTTPS virtual host.

403 on api.pl
  -> sudo ./redeploy.sh (api.pl must be executable); error.log names the reason.

500 on api.pl
  -> perl -c api.pl                      (syntax / missing module -> step 1)
  -> head -1 api.pl | od -c | head -2    (\r \n = Windows line ending -> sudo ./redeploy.sh)
  -> run it as the web server does:
       sudo -u www-data env APP_ENV_FILE=/etc/sigmaswap/sigmaswap.env \
            REQUEST_METHOD=GET QUERY_STRING=action=health perl api.pl

health says "The service is temporarily unavailable." / "... is busy"   (reason is in error.log)
  -> "No such file or directory" for venv/bin/python, or ImportError -> step 3
  -> URLError / timed out -> the server cannot reach the internet over HTTPS:
       curl -sS -m 15 -o /dev/null -w '%{http_code}\n' https://api.ergoplatform.com/api/v1/info
       curl -sS -m 15 -o /dev/null -w '%{http_code}\n' https://api.ergodex.io/v1/amm/pools/stats

health works but base_url starts with http:// or shows the wrong host
  -> set BASE_URL in the settings file (step 4).

Wallet says the app cannot be loaded, but C2 works
  -> the address entered in the wallet must be https and end in /cgi-bin/sigmaswap/ ; the
     certificate must be valid for the host name (the wallet does not accept self-signed ones).

"The pool list is unavailable right now"
  -> api.ergodex.io unreachable (see above). A cached list keeps being used while it is down; a
     brand-new install has no cache yet.

Status screen always says "Waiting for your wallet" / pools.json never appears
  -> the web server cannot write CACHE_DIR: step 4 (owner www-data), and CACHE_DIR spelled right.

SigmaMint: "Uploads are not available right now"
  -> error.log "Lighthouse upload refused: HTTP 401/403" = wrong or revoked API key.
SigmaMint: picker page says "This page has expired" straight away
  -> key missing/empty, MINT_UPLOADS=0, or the cache directory is not writable.
SigmaMint: "Too many uploads right now"
  -> a limit was hit (per visitor per hour, or per day). Raise it in the settings file if intended.

Bot: status 203/EXEC -> venv missing (step 3). Wrong user -> edit User=/Group= in the unit.

Wallet shows "The pool did not accept this swap" / "price moved beyond your slippage tolerance"
  -> normal protection: the pool changed between preview and signing. Preview again.

Order placed but not executed after several minutes
  -> the pool price is below the order's minimum. The funds are safe in the order box:
     "My open orders" -> "Cancel and refund" returns them.
  -> "My open orders" is empty right after placing an order: the explorer lists it once the
     transaction has confirmed (about 2 minutes).

Wallet: transaction rejected after signing ("double spending" / "inputs not found")
  -> somebody else's swap hit the same pool box first, or the wallet's previous transaction is
     still unconfirmed. Nothing was spent. Wait for the next block and preview again.
```

## Appendix: RHEL family (Oracle / Rocky / Alma) with SELinux

The web user is `apache`, the service is `httpd`, includes go in `/etc/httpd/conf.d/`, the log is
`/var/log/httpd/ssl_error_log`, and the packages are `httpd perl perl-CGI python3 git
policycoreutils-python-utils`. Change the paths in `deploy/sigmaswap.conf` and the bot unit if you
install somewhere other than `/usr/lib/cgi-bin/sigmaswap`, and set `User=apache` in the unit.
With SELinux enforcing, install these rules once, then run `redeploy.sh` (it relabels and checks):

```bash
A=/usr/lib/cgi-bin/sigmaswap
sudo semanage fcontext -a -t httpd_sys_content_t     "$A(/.*)?"
sudo semanage fcontext -a -t httpd_sys_script_exec_t "$A(/.*)?/api\.pl"
sudo semanage fcontext -a -t httpd_sys_script_exec_t "$A/venv(/.*)?"
sudo semanage fcontext -a -t httpd_sys_content_t     "/etc/sigmaswap(/.*)?"
sudo semanage fcontext -a -t httpd_sys_rw_content_t  "/var/lib/sigmaswap(/.*)?"
sudo setsebool -P httpd_can_network_connect 1
sudo restorecon -RF /etc/sigmaswap /var/lib/sigmaswap
```

Denials show with `sudo ausearch -m avc -ts recent | grep -i httpd`.
