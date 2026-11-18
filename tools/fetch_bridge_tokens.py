#!/usr/bin/env python3
"""Refresh sigmabridge/tokens.json from Rosen's published token map.

DEV TOOL - run on a development machine, then commit the result:

    git clone --depth 1 https://github.com/rosen-bridge/contract /tmp/rosen-contract
    python3 tools/fetch_bridge_tokens.py /tmp/rosen-contract

It reads tokensMap/*.mainnet-public-launch.json and keeps, for every token that
exists on Ergo, its Ergo id, name, decimals and the target chains SigmaBridge
supports. Tokens whose Ergo amount would need rescaling for another chain are
left out on purpose (none at the time of writing).
"""
import glob
import json
import os
import sys

CHAINS = ("cardano", "ethereum", "binance", "bitcoin-runes", "bitcoin", "doge", "firo")      # keep in step with BRIDGE_CHAINS in sigmaswap.py


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "."
    out = []
    for path in sorted(glob.glob(os.path.join(src, "tokensMap", "*.mainnet-public-launch.json"))):
        with open(path) as f:
            d = json.load(f)
        ergo = d.get("ergo")
        if not ergo:
            continue
        if ergo["decimals"] != min(v["decimals"] for v in d.values()):
            print("skipped (needs rescaling):", ergo["name"])
            continue
        token_id = "erg" if ergo["tokenId"] == "erg" else ergo["tokenId"]
        if token_id != "erg" and not (len(token_id) == 64 and all(c in "0123456789abcdef" for c in token_id)):
            continue
        chains = [c for c in CHAINS if c in d]
        if chains:
            out.append({"id": token_id, "name": str(ergo["name"])[:24], "decimals": int(ergo["decimals"]), "chains": chains})
    out.sort(key=lambda t: (t["id"] != "erg", t["name"].lower()))
    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sigmabridge", "tokens.json")
    with open(dest, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print("wrote %d tokens to %s" % (len(out), dest))


if __name__ == "__main__":
    main()
