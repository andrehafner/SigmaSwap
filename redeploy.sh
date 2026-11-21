#!/bin/bash
# Re-apply owner and permissions for the SigmaSwap apps. Run after every change to
# the app directory (git pull, edit, pip install):
#
#     cd /usr/lib/cgi-bin/sigmaswap && sudo ./redeploy.sh
#
# Idempotent. It never touches the settings file's contents or the cache directory's
# contents. On a server with SELinux enforcing it also restores the file labels.
set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${APP_ENV_FILE:-/etc/sigmaswap/sigmaswap.env}"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run with sudo" >&2
    exit 1
fi
cd "$APP_DIR" || exit 1

# the user the web server runs CGI programs as
if   id -u www-data >/dev/null 2>&1; then WEB=www-data
elif id -u apache   >/dev/null 2>&1; then WEB=apache
else echo "ERROR: no www-data or apache user found" >&2; exit 1
fi

# LF line endings (a CRLF first line makes the web server fail to start the script)
sed -i 's/\r$//' ./*.pl ./*.pm ./*.py ./*.sh ./sigmamint/*.pl ./sigmalp/*.pl ./sigmabridge/*.pl 2>/dev/null

# root owns everything and nothing here is writable by the web server. The tree holds
# no secrets (those are in the settings file), so it is world-readable: 755 / 644.
chown -R root:"$WEB" "$APP_DIR"
find "$APP_DIR" -path "$APP_DIR/.git" -prune -o -type d -exec chmod 755 {} +
find "$APP_DIR" -path "$APP_DIR/.git" -prune -o -path "$APP_DIR/venv" -prune -o -type f -exec chmod 644 {} +
chmod 755 "$APP_DIR"/*.pl "$APP_DIR"/sigmamint/*.pl "$APP_DIR"/sigmalp/*.pl "$APP_DIR"/sigmabridge/*.pl "$APP_DIR"/redeploy.sh
[ -d "$APP_DIR/.git" ] && chmod -R a+rX "$APP_DIR/.git"
[ -d "$APP_DIR/venv" ] && chmod -R a+rX "$APP_DIR/venv"

# the settings file: readable by the web server, by nobody else
if [ -f "$ENV_FILE" ]; then
    chown root:"$WEB" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
fi

fail=0
echo "Web user: $WEB"
for f in api.pl sigmamint/api.pl sigmalp/api.pl sigmabridge/api.pl; do
    if [ -x "$APP_DIR/$f" ] && head -1 "$APP_DIR/$f" | grep -q '^#!/usr/bin/perl'; then echo "  ok    $f"
    else echo "  WRONG $f (missing, not executable, or bad first line)"; fail=1; fi
done
if [ -x "$APP_DIR/venv/bin/python" ]; then
    if "$APP_DIR/venv/bin/python" -I -c 'import ergo_lib_python' 2>/dev/null; then echo "  ok    venv (ergo-lib-python loads)"
    else echo "  WRONG venv exists but ergo-lib-python does not load: venv/bin/python -m pip install -r requirements.txt"; fail=1; fi
else
    echo "  WRONG venv/ is missing: python3 -m venv venv && venv/bin/python -m pip install -r requirements.txt"; fail=1
fi
if [ -f "$ENV_FILE" ]; then echo "  ok    settings file $ENV_FILE"
else echo "  note  $ENV_FILE not present: defaults are used (see DEPLOY.md step 4)"; fi

# SELinux servers only (RHEL family): labels follow the rules installed with semanage
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
    restorecon -RF "$APP_DIR"
    [ -f "$ENV_FILE" ] && restorecon -F "$ENV_FILE"
    case "$(stat -c %C "$APP_DIR/api.pl" 2>/dev/null)" in
        *":httpd_sys_script_exec_t:"*) echo "  ok    SELinux label on api.pl" ;;
        *) echo "  WRONG SELinux label on api.pl (see DEPLOY.md, SELinux appendix)"; fail=1 ;;
    esac
fi

if [ "$fail" -ne 0 ]; then
    echo >&2
    echo "!!! NOT DONE. Fix the lines marked WRONG, then run this script again." >&2
    exit 1
fi
echo "Done."
