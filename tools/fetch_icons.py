#!/usr/bin/env python3
"""Refresh the bundled token logos in icons/.

DEV TOOL - run it on a development machine, not on the web server:

    python3 tools/fetch_icons.py            # then commit icons/ and deploy as usual

For every token in the liquid ErgoDex pools it downloads the logo (ergodex.io,
then the Spectrum token-logos repository) and renders it to a 192x192 PNG named
icons/<token id>.png. The wallet cannot draw SVG, and bundling the files means
the app does not depend on another site being up. Tokens without a logo get
none; the app then shows icons/unknown.png.

Needs: python3, and node with playwright + chromium (for the SVG/PNG rendering).
"""
import json
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "tools", ".raw")
POOLS_API = "https://api.ergodex.io/v1/amm/pools/stats"
SOURCES = [
    ("png", "https://ergodex.io/token-logos/%s.png"),
    ("svg", "https://ergodex.io/token-logos/%s.svg"),
    ("svg", "https://raw.githubusercontent.com/spectrum-finance/token-logos/master/logos/ergo/%s.svg"),
]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "sigmaswap-icons/1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.headers.get("content-type", ""), r.read()


def main():
    min_tvl = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0
    pools = json.loads(get(POOLS_API)[1])
    ids = {}
    for p in pools:
        if float(p["tvl"]["value"]) >= min_tvl:
            for side in (p["lockedX"], p["lockedY"]):
                ids[side["id"]] = side.get("ticker") or "?"
    os.makedirs(RAW, exist_ok=True)
    jobs, missing = [], []
    for token_id, ticker in sorted(ids.items()):
        if not (len(token_id) == 64 and all(c in "0123456789abcdef" for c in token_id)):
            continue
        for ext, pattern in SOURCES:
            try:
                ctype, body = get(pattern % token_id)
            except Exception:
                continue
            if not ctype.startswith("image/") or not body or len(body) > 2000000:
                continue
            raw = os.path.join(RAW, "%s.%s" % (token_id, ext))
            with open(raw, "wb") as f:
                f.write(body)
            jobs.append({"id": token_id, "raw": raw, "mime": "image/svg+xml" if ext == "svg" else "image/png"})
            break
        else:
            missing.append(ticker)
    with open(os.path.join(RAW, "jobs.json"), "w") as f:
        json.dump({"out": os.path.join(ROOT, "icons"), "jobs": jobs}, f)
    subprocess.run(["node", os.path.join(ROOT, "tools", "render_icons.js"), os.path.join(RAW, "jobs.json")], check=True)
    print("logos: %d of %d tokens; none for: %s" % (len(jobs), len(ids), ", ".join(missing) or "-"))


if __name__ == "__main__":
    main()
