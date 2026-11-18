# SigmaSwap — Deployment (Oracle Linux 9 / Apache / SELinux)

App: `sigmaswap` · URL: `https://public.aap.cornell.edu/apps/sigmaswap/` (works unchanged on `private.aap`)

This app differs from the usual AAP app in three ways — everything else follows `CLAUDE.md`:

| Usual AAP app | SigmaSwap |
|---|---|
| Shibboleth login | **Public, no Shibboleth** (opened by the Ergo wallet app, which cannot log in) |
| MariaDB | **No database** (stateless; nothing is stored) |
| Perl only | Perl CGI **plus a Python virtualenv** (`venv/`) used to build Ergo transactions |

Do the steps in order. Do not test in a wallet until every **GATE** passes.

## 1. Packages

```bash
sudo dnf install -y perl perl-CGI python3 git policycoreutils-python-utils
perl -MCGI -MJSON::PP -MMath::BigFloat -MIPC::Open2 -e 'print "perl ok\n"'    # VERIFY
python3 --version                                                             # 3.9 or newer
```

## 2. Code

```bash
sudo mkdir -p /data/AAP_Storage/apps
cd /data/AAP_Storage/apps
sudo git clone <repo-url> sigmaswap        # later: cd sigmaswap && sudo git pull
```

## 3. Python virtualenv (transaction builder)

```bash
cd /data/AAP_Storage/apps/sigmaswap
sudo python3 -m venv venv
sudo venv/bin/pip install --upgrade pip
sudo venv/bin/pip install -r requirements.txt
sudo venv/bin/python -c 'import ergo_lib_python; print("ergo-lib", ergo_lib_python.__version__)'   # VERIFY
```

`venv/` is git-ignored: a `git pull` never touches it. Re-run the two `pip` lines only when
`requirements.txt` changes (then `sudo ./redeploy.sh`).

## 4. Settings file + cache directory

No secrets exist for this app, but it uses the standard env-file mechanism for settings.

```bash
sudo install -m 640 -o root -g apache sigmaswap.env.example /data/AAP_Storage/secrets/sigmaswap.env
sudo install -d -m 2770 -o apache -g apache /data/AAP_Storage/apps/uploads/sigmaswap    # pool-list cache
```

The defaults in the file are production-ready. `BASE_URL` stays empty so the same checkout serves
both vhosts.

## 5. One-time SELinux rules

```bash
sudo semanage fcontext -a -t httpd_sys_content_t     '/data/AAP_Storage/apps/sigmaswap(/.*)?'
sudo semanage fcontext -a -t httpd_sys_script_exec_t '/data/AAP_Storage/apps/sigmaswap/.*\.pl'
sudo semanage fcontext -a -t httpd_sys_script_exec_t '/data/AAP_Storage/apps/sigmaswap/venv(/.*)?'
sudo semanage fcontext -a -t httpd_sys_content_t     '/data/AAP_Storage/secrets/sigmaswap\.env'
sudo semanage fcontext -a -t httpd_sys_rw_content_t  '/data/AAP_Storage/apps/uploads/sigmaswap(/.*)?'
sudo restorecon -RF /data/AAP_Storage/apps/uploads/sigmaswap /data/AAP_Storage/secrets/sigmaswap.env

# the CGI makes outbound HTTPS calls (Ergo explorer + ErgoDex API)
sudo setsebool -P httpd_can_network_connect 1
```

If an `-a` reports "already defined", use `-m` instead. If a broader existing rule for
`/data/AAP_Storage/apps(/.*)?` wins over these, the more specific rule still applies as long as it
was added later; `redeploy.sh` tells you when it did not.

## 6. Apache

```bash
sudo cp deploy/sigmaswap.conf /etc/httpd/conf.d/sigmaswap.conf
sudo httpd -t && sudo systemctl restart httpd
```

**Shibboleth:** the include sets `AuthType None`, `ShibRequestSetting requireSession false` and
`Require all granted` for this directory. If the vhost protects `/apps` with a `<Location>` block
(Location is merged *after* Directory and would win), add this next to that block:

```apache
<Location /apps/sigmaswap>
    AuthType None
    ShibRequestSetting requireSession false
    Require all granted
</Location>
```

## 7. Redeploy (labels, owner, perms)

```bash
cd /data/AAP_Storage/apps/sigmaswap && sudo ./redeploy.sh
```

It must end with `Done.` — it verifies the labels itself and exits non-zero with instructions when
they are wrong. **Run it after every `git pull`, edit, or `pip install`.**

## 8. GATES

```bash
# G1 labels
ls -Z api.pl            # ... httpd_sys_script_exec_t
ls -dZ venv             # ... httpd_sys_script_exec_t

# G2 outbound network from the server
curl -sS -m 15 https://api.ergoplatform.com/api/v1/info | head -c 80; echo
curl -sS -m 15 -o /dev/null -w '%{http_code}\n' https://api.ergodex.io/v1/amm/pools/stats   # 200

# G3 the whole stack THROUGH Apache + SELinux, and with NO login
curl -sS -X POST -d action=health https://public.aap.cornell.edu/apps/sigmaswap/api.pl; echo
#   {"success":1,"helper":"ok","height":19xxxxx,"pools":NN,"base_url":"https://public.aap.cornell.edu/apps/sigmaswap"}

# G4 the Mosaik app JSON
curl -sS https://public.aap.cornell.edu/apps/sigmaswap/api.pl | head -c 120; echo
#   {"manifest":{"appName":"SigmaSwap",...

# G5 cache is being written outside the tree
ls -l /data/AAP_Storage/apps/uploads/sigmaswap/pools.json
```

G3 must return JSON, **not** a Cornell login redirect (302 / HTML). If it redirects, Shibboleth is
still protecting the path — see step 6.

Then, in the Ergo wallet app: **dApps → enter** `https://public.aap.cornell.edu/apps/sigmaswap/`.
Do a first swap with a small amount.

## Updating

```bash
cd /data/AAP_Storage/apps/sigmaswap && sudo git pull && sudo ./redeploy.sh
```

If the Mosaik screen changed, bump `APP_VERSION` in `api.pl`: wallets that have the app open then
reload it instead of mixing old and new screens.

## Troubleshooting

Always read **`/var/log/httpd/ssl_error_log`** (not `error_log`). Lines from this app start with
`sigmaswap:` or `sigmaswap.py`.

```
Browser/wallet gets a Cornell login page
  -> Shibboleth still applies to /apps/sigmaswap: add the <Location> block from step 6.

403 on everything, or api.pl is downloaded as text
  -> ls -Z api.pl is not httpd_sys_script_exec_t: step 5 rules missing -> apply, sudo ./redeploy.sh
  -> still 403: sudo setenforce 0; retry; sudo setenforce 1
       works with 0  -> SELinux (sudo ausearch -m avc -ts recent | grep -i httpd)
       still fails   -> DAC: namei -l /data/AAP_Storage/apps/sigmaswap/api.pl (a parent dir lacks o+x)

500 "End of script output before headers"
  -> perl -c api.pl                         (syntax / missing module -> step 1)
  -> head -1 api.pl | od -c | head -2       (\r \n = CRLF shebang -> sudo ./redeploy.sh fixes it)
  -> 500 right after a pull/edit            -> sudo ./redeploy.sh

health says "The service is temporarily unavailable." / "... is busy"
  ssl_error_log shows the reason:
  -> "Permission denied" starting venv/bin/python, or an ImportError / "cannot open shared object"
       venv label is wrong: ls -dZ venv ; ls -Z venv/lib/python3*/site-packages/ergo_lib_python/*.so
       -> step 5 venv rule, then sudo ./redeploy.sh
  -> "No such file or directory" for venv/bin/python -> step 3 was skipped
  -> avc denied { name_connect } / URLError / timed out
       -> sudo setsebool -P httpd_can_network_connect 1 ; then G2 (host firewall / egress rules)
  -> works as root but not via Apache:
       sudo -u apache env APP_ENV_FILE=/data/AAP_Storage/secrets/sigmaswap.env \
            REQUEST_METHOD=GET QUERY_STRING= perl api.pl | head -c 300
       prints JSON  -> SELinux/label problem (the command above bypasses SELinux)
       prints error -> code/config problem

App opens but "The pool list is unavailable right now"
  -> G2 for api.ergodex.io. A previously cached list keeps being used while ErgoDex is down;
     on a brand-new install there is no cache yet.

pools.json never appears (G5)
  -> cache dir missing or not apache-writable / not httpd_sys_rw_content_t: step 4 + step 5.
     The app still works without it, only slower.

Wallet shows "The pool did not accept this swap" / "price moved beyond your slippage tolerance"
  -> normal protection: the pool changed between preview and signing. Preview again.

Wallet: transaction rejected after signing ("double spending" / "inputs not found")
  -> somebody else's swap hit the same pool box first, or the wallet's previous transaction is
     still unconfirmed. Nothing was spent. Wait for the next block and preview again.
```
