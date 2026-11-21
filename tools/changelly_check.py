#!/usr/bin/env python3
"""Check what the Changelly API (v2) offers for ERG with YOUR partner key.

Run it on the server, where the key lives. It only reads: it never creates an exchange.
It prints no secret.

    # you already have a Changelly private key (the long hex string):
    sudo install -m 600 /dev/stdin /etc/sigmaswap/changelly.key      # paste the key, Enter, Ctrl-D
    sudo python3 tools/changelly_check.py /etc/sigmaswap/changelly.key

    # you have no key yet: make one, then send the two printed PUBLIC values to Changelly
    sudo python3 tools/changelly_check.py --generate /etc/sigmaswap/changelly.key

No extra packages are needed.
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API_URL = "https://api.changelly.com/v2"
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def der_read(buf, pos):
    """-> (tag, value bytes, next position)"""
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(buf[pos:pos + n], "big")
        pos += n
    return tag, buf[pos:pos + length], pos + length


def der_len(n):
    return bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, "big")


def der_int(i):
    raw = i.to_bytes(i.bit_length() // 8 + 1, "big")
    return b"\x02" + der_len(len(raw)) + raw


def load_key(hex_text):
    """PKCS#8 DER in hex (what Changelly's samples produce) -> (n, e, d)"""
    der = bytes.fromhex("".join(hex_text.split()))
    _, outer, _ = der_read(der, 0)
    _, _, pos = der_read(outer, 0)            # version
    tag, _, pos = der_read(outer, pos)        # algorithm (PKCS#8) or the modulus (bare RSA key)
    if tag == 0x30:
        _, inner, _ = der_read(outer, pos)    # OCTET STRING holding the RSA key
        _, outer, _ = der_read(inner, 0)
    ints, pos = [], 0
    while pos < len(outer) and len(ints) < 4:
        _, val, pos = der_read(outer, pos)
        ints.append(int.from_bytes(val, "big"))
    if len(ints) < 4:
        sys.exit("This does not look like an RSA private key in hex.")
    return ints[1], ints[2], ints[3]


def api_key(n, e):
    body = der_int(n) + der_int(e)
    return base64.b64encode(hashlib.sha256(b"\x30" + der_len(len(body)) + body).digest()).decode()


def public_key_b64(n, e):
    body = der_int(n) + der_int(e)
    return base64.b64encode(b"\x30" + der_len(len(body)) + body).decode()


def sign(n, d, message):
    k = (n.bit_length() + 7) // 8
    t = SHA256_PREFIX + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return base64.b64encode(pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")).decode()


def call(key, method, params):
    n, e, d = key
    body = json.dumps({"jsonrpc": "2.0", "id": "check", "method": method, "params": params}).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "sigmaswap-check/1.0", "X-Api-Key": api_key(n, e), "X-Api-Signature": sign(n, d, body)})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as err:
        return None, "HTTP %s: %s" % (err.code, err.read().decode(errors="replace")[:300])
    except Exception as err:      # network trouble
        return None, "%s: %s" % (type(err).__name__, err)
    if "error" in out:
        return None, json.dumps(out["error"])[:400]
    return out.get("result"), None


def generate(path):
    if os.path.exists(path):
        sys.exit("%s already exists. Not overwriting it." % path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    pem = subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
                         check=True, capture_output=True).stdout
    der = subprocess.run(["openssl", "pkcs8", "-topk8", "-nocrypt", "-outform", "DER"], input=pem,
                         check=True, capture_output=True).stdout
    with os.fdopen(fd, "w") as fh:
        fh.write(der.hex() + "\n")
    n, e, _ = load_key(der.hex())
    print("Private key written to %s (keep it secret, never send it to anyone).\n" % path)
    print("Send these two PUBLIC values to Changelly, with the e-mail address of your Changelly account:\n")
    print("Public API key:\n%s\n" % public_key_b64(n, e))
    print("Public API key Base64 (SHA256 of the public key):\n%s" % api_key(n, e))


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--generate":
        return generate(args[1])
    if len(args) != 1:
        sys.exit(__doc__)
    with open(args[0]) as fh:
        key = load_key(fh.read())
    print("Your API key id (public; must be the one registered with Changelly):\n  %s\n" % api_key(key[0], key[1]))

    res, err = call(key, "getCurrenciesFull", {})
    if err:
        sys.exit("getCurrenciesFull FAILED: %s\n(401 / Unauthorized = this key is not registered with Changelly, or not activated yet.)" % err)
    erg = [c for c in res if c.get("ticker") == "erg"]
    print("Currencies: %d listed, %d enabled" % (len(res), sum(1 for c in res if c.get("enabled"))))
    if not erg:
        sys.exit("ERG is NOT in the currency list. Nothing more to check.")
    print("ERG: " + json.dumps({k: erg[0].get(k) for k in ("enabled", "enabledFrom", "enabledTo", "fixRateEnabled",
                                                           "payinConfirmations", "blockchainPrecision")}))

    res, err = call(key, "getPairs", {"from": "erg"})
    if err:
        print("getPairs from ERG FAILED: %s" % err)
        targets = []
    else:
        targets = sorted(p["to"] for p in res)
        print("\nERG can be exchanged to %d currencies: %s%s" % (len(targets), ", ".join(targets[:40]),
                                                               " ..." if len(targets) > 40 else ""))
    for to in [t for t in ("btc", "eth", "usdt20", "xmr", "ada") if not targets or t in targets]:
        lim, err = call(key, "getPairsParams", [{"from": "erg", "to": to}])
        if err or not lim:
            print("  erg -> %-7s limits FAILED: %s" % (to, err or "empty answer"))
            continue
        low = lim[0].get("minAmountFloat")
        print("  erg -> %-7s min %s ERG, max %s ERG" % (to, low, lim[0].get("maxAmountFloat")))
        try:
            amount = "%.4f" % (float(low) * 2)
        except (TypeError, ValueError):
            continue
        est, err = call(key, "getExchangeAmount", [{"from": "erg", "to": to, "amountFrom": amount}])
        if err or not est:
            print("               estimate for %s ERG FAILED: %s" % (amount, err or "empty answer"))
        else:
            print("               %s ERG -> %s %s (before network fee %s)" % (amount, est[0].get("amountTo"), to,
                                                                           est[0].get("networkFee")))
    print("\nDone. Nothing was created.")


if __name__ == "__main__":
    main()
