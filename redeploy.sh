#!/bin/bash
# Re-apply owner / permissions / SELinux labels for SigmaSwap.
# Run after EVERY write to the app tree (git pull, edit, pip install):
#
#     cd /data/AAP_Storage/apps/sigmaswap && sudo ./redeploy.sh
#
# Idempotent. Does NOT touch the external cache dir (CACHE_DIR).
set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRET=/data/AAP_Storage/secrets/sigmaswap.env

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run with sudo" >&2
    exit 1
fi
cd "$APP_DIR" || exit 1

# LF line endings (a CRLF shebang = "exec ... No such file or directory")
sed -i 's/\r$//' ./*.pl ./*.pm ./*.py ./*.sh 2>/dev/null

# Ownership + DAC permissions: root owns, apache reads; nothing in the tree is apache-writable.
chown -R root:apache "$APP_DIR"
find "$APP_DIR" -path "$APP_DIR/.git" -prune -o -type d -exec chmod 750 {} +
find "$APP_DIR" -path "$APP_DIR/.git" -prune -o -path "$APP_DIR/venv" -prune -o -type f -exec chmod 640 {} +
chmod 750 "$APP_DIR"/*.pl "$APP_DIR"/redeploy.sh
if [ -d "$APP_DIR/venv" ]; then
    chmod -R g+rX,o-rwx "$APP_DIR/venv"
fi

# SELinux labels (only applies rules that the one-time `semanage fcontext` step installed)
restorecon -RF "$APP_DIR"
[ -f "$SECRET" ] && restorecon -F "$SECRET"

# ---- verification: restorecon says nothing when the fcontext rules are missing
fail=0
label_of() { stat -c %C "$1" 2>/dev/null; }
check() {   # check <path> <expected type> <what>
    local l; l="$(label_of "$1")"
    case "$l" in
        *":$2:"*) echo "  ok    $3 -> $l" ;;
        *) echo "  WRONG $3 -> ${l:-missing} (expected $2)"; fail=1 ;;
    esac
}
echo "SELinux labels:"
check "$APP_DIR/api.pl"       httpd_sys_script_exec_t "api.pl"
check "$APP_DIR/index.html"   httpd_sys_content_t     "index.html"
check "$APP_DIR/sigmaswap.py" httpd_sys_content_t     "sigmaswap.py"
if [ -d "$APP_DIR/venv" ]; then
    check "$APP_DIR/venv" httpd_sys_script_exec_t "venv/"
else
    echo "  WARN  venv/ does not exist yet - create it (DEPLOY.md step 3) and re-run"
    fail=1
fi
if [ -f "$SECRET" ]; then
    check "$SECRET" httpd_sys_content_t "secrets/sigmaswap.env"
else
    echo "  note  $SECRET not present (optional: defaults are used)"
fi

if [ "$fail" -ne 0 ]; then
    cat >&2 <<'MSG'

!!! NOT DONE. One or more labels are wrong, so httpd will answer 403/500.
!!! The one-time `semanage fcontext` rules are missing (DEPLOY.md step 5),
!!! or semanage is not installed (dnf install policycoreutils-python-utils).
!!! Apply the rules, then run this script again.
MSG
    exit 1
fi
echo "Done."
