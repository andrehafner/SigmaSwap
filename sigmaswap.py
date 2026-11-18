#!/usr/bin/env python3
"""SigmaSwap chain helper.

Called by api.pl (never directly by a browser/wallet):

    venv/bin/python sigmaswap.py <pools|quote|build>

One JSON request on stdin, one JSON response on stdout:
    {"ok": true, ...}  or  {"ok": false, "error": "message safe to show the user"}

Details of unexpected failures go to stderr (-> Apache ssl_error_log) only.

What it does
  pools  - list the liquid ErgoDex pools (from the ErgoDex analytics API)
  quote  - read one pool box from the chain and price a swap
  build  - build + reduce the swap transaction for ErgoPay (EIP-19/EIP-20)

A swap is a direct, single transaction against the pool box:

    inputs : [pool box, user box(es)...]
    outputs: [new pool box, user box(es) with the swap result + change, miner fee]

The pool contract itself enforces the constant-product rule, and the
transaction is "reduced" here, which means the pool script is really evaluated
before we ever hand the transaction to a wallet. This server holds no keys and
signs nothing: the user's wallet reviews and signs.

Written for Python 3.9+ (Oracle Linux 9 system python) + ergo-lib-python.
"""

import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

ERG_ID = "0" * 64
FEE_DENOM = 1000

# The only two ErgoDex pool contracts that exist on mainnet (ERG<->token and
# token<->token). A pool box guarded by anything else is refused.
N2T_POOL_TREE = (
    "1999030f0400040204020404040405feffffffffffffffff0105feffffffffffffffff01050004d00f0400040004"
    "06050005000580dac409d819d601b2a5730000d602e4c6a70404d603db63087201d604db6308a7d605b272037301"
    "00d606b27204730200d607b27203730300d608b27204730400d6099973058c720602d60a999973068c7205027209"
    "d60bc17201d60cc1a7d60d99720b720cd60e91720d7307d60f8c720802d6107e720f06d6117e720d06d612998c72"
    "0702720fd6137e720c06d6147308d6157e721206d6167e720a06d6177e720906d6189c72117217d6199c72157217"
    "d1ededededededed93c27201c2a793e4c672010404720293b27203730900b27204730a00938c7205018c72060193"
    "8c7207018c72080193b17203730b9593720a730c95720e929c9c721072117e7202069c7ef07212069a9c72137e72"
    "14067e9c720d7e72020506929c9c721372157e7202069c7ef0720d069a9c72107e7214067e9c72127e7202050695"
    "ed720e917212730d907216a19d721872139d72197210ed9272189c721672139272199c7216721091720b730e"
)
T2T_POOL_TREE = (
    "19a9030f040004020402040404040406040605feffffffffffffffff0105feffffffffffffffff01050004d00f04"
    "00040005000500d81ad601b2a5730000d602e4c6a70404d603db63087201d604db6308a7d605b27203730100d606"
    "b27204730200d607b27203730300d608b27204730400d609b27203730500d60ab27204730600d60b9973078c7206"
    "02d60c999973088c720502720bd60d8c720802d60e998c720702720dd60f91720e7309d6108c720a02d6117e7210"
    "06d6127e720e06d613998c7209027210d6147e720d06d615730ad6167e721306d6177e720c06d6187e720b06d619"
    "9c72127218d61a9c72167218d1edededededed93c27201c2a793e4c672010404720292c17201c1a793b27203730b"
    "00b27204730c00938c7205018c720601ed938c7207018c720801938c7209018c720a019593720c730d95720f929c"
    "9c721172127e7202069c7ef07213069a9c72147e7215067e9c720e7e72020506929c9c721472167e7202069c7ef0"
    "720e069a9c72117e7215067e9c72137e7202050695ed720f917213730e907217a19d721972149d721a7211ed9272"
    "199c7217721492721a9c72177211"
)

MIN_BOX_VALUE_PER_BYTE = 360
MAX_TOKENS_PER_BOX = 80          # keeps a change box well under the 4096-byte box limit
MAX_USER_INPUTS = 60
MAX_USER_BOXES_SCANNED = 300
HTTP_TIMEOUT = 15


class SwapError(Exception):
    """An error whose message is safe and useful to show to the user."""


# ---------------------------------------------------------------- http ----

def http_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sigmaswap/1"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def explorer(cfg, path):
    return http_json(cfg["explorer_url"].rstrip("/") + "/api/v1/" + path)


# ------------------------------------------------------------ validation ----

def hexid(obj):
    """Hex form of a TokenId / BoxId / TxId."""
    return bytes(obj).hex()


def is_hex64(s):
    return isinstance(s, str) and len(s) == 64 and all(c in "0123456789abcdef" for c in s)


def req_token_id(v, what):
    if not is_hex64(v):
        raise SwapError("Invalid %s." % what)
    return v


def req_amount(v, what):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise SwapError("Invalid %s." % what)
    if n <= 0 or n >= 2 ** 63:
        raise SwapError("Invalid %s." % what)
    return n


def req_address(v):
    """Mainnet pay-to-public-key address only (that is what a wallet address is)."""
    from ergo_lib_python.chain import Address, NetworkPrefix
    if not isinstance(v, str) or not (40 <= len(v) <= 60) or not v.isalnum():
        raise SwapError("Please choose a valid wallet address.")
    try:
        addr = Address(v, NetworkPrefix.Mainnet)
    except Exception:
        raise SwapError("Please choose a valid Ergo mainnet wallet address.")
    if not bytes(addr.ergo_tree()).hex().startswith("0008cd"):
        raise SwapError("Only standard wallet (P2PK) addresses are supported.")
    return addr


# ----------------------------------------------------------------- pools ----

def cmd_pools(req):
    cfg = req["cfg"]
    min_tvl = float(cfg.get("min_tvl_usd", 100))
    stats = http_json(cfg["pools_api_url"])
    if not isinstance(stats, list):
        raise SwapError("Pool list is unavailable right now.")
    best = {}
    for p in stats:
        try:
            x, y = p["lockedX"], p["lockedY"]
            tvl = float(p["tvl"]["value"])
            if tvl < min_tvl or not is_hex64(p["id"]) or not is_hex64(x["id"]) or not is_hex64(y["id"]):
                continue
            if int(x["amount"]) <= 0 or int(y["amount"]) <= 0 or x["id"] == y["id"]:
                continue
            if y["id"] == ERG_ID:      # ERG is always the X side of an N2T pool
                continue
            entry = {
                "id": p["id"],
                "tvl": tvl,
                "x": _asset(x),
                "y": _asset(y),
            }
        except (KeyError, TypeError, ValueError):
            continue
        key = tuple(sorted((x["id"], y["id"])))
        if key not in best or best[key]["tvl"] < tvl:     # deepest pool per pair
            best[key] = entry
    pools = sorted(best.values(), key=lambda e: -e["tvl"])
    if not pools:
        raise SwapError("Pool list is unavailable right now.")
    return {"pools": pools}


def _asset(a):
    ticker = a.get("ticker")
    ticker = ticker.strip() if isinstance(ticker, str) else ""
    # tickers are untrusted on-chain text: keep them short and printable
    ticker = "".join(ch for ch in ticker if ch.isprintable())[:24]
    if a["id"] == ERG_ID:
        ticker = "ERG"
    decimals = a.get("decimals")
    decimals = int(decimals) if isinstance(decimals, int) and 0 <= decimals <= 18 else 0
    return {"id": a["id"], "ticker": ticker or (a["id"][:6] + "..."), "decimals": decimals}


# ------------------------------------------------------------- pool state ----

class Pool(object):
    """Reserves + fee of one pool, read from its (validated) on-chain box."""

    def __init__(self, box_json):
        from ergo_lib_python.chain import ErgoBox, NonMandatoryRegisterId
        self.json = box_json
        self.box = ErgoBox.from_json(box_json)          # also re-derives + checks the box id
        tree = bytes(self.box.ergo_tree).hex()
        toks = [(hexid(t.token_id), t.amount) for t in self.box.tokens]
        if tree == N2T_POOL_TREE and len(toks) == 3:
            self.kind = "n2t"
            self.x_id, self.x_amt = ERG_ID, self.box.value
            self.y_id, self.y_amt = toks[2]
        elif tree == T2T_POOL_TREE and len(toks) == 4:
            self.kind = "t2t"
            self.x_id, self.x_amt = toks[2]
            self.y_id, self.y_amt = toks[3]
        else:
            raise SwapError("This pool type is not supported.")
        self.nft = toks[0][0]
        self.tokens = toks
        regs = self.box.additional_registers
        if len(regs) != 1:
            raise SwapError("This pool type is not supported.")
        self.registers = {NonMandatoryRegisterId.R4: regs[NonMandatoryRegisterId.R4]}
        fee = regs[NonMandatoryRegisterId.R4].value
        if not isinstance(fee, int) or not (0 < fee <= FEE_DENOM):
            raise SwapError("This pool type is not supported.")
        self.fee_num = fee

    def quote(self, from_id, amount_in):
        """-> (to_id, amount_out, reserve_in, reserve_out). Fee is taken from the input."""
        if from_id == self.x_id:
            r_in, r_out, to_id = self.x_amt, self.y_amt, self.y_id
        elif from_id == self.y_id:
            r_in, r_out, to_id = self.y_amt, self.x_amt, self.x_id
        else:
            raise SwapError("That token is not part of this pool.")
        out = (r_out * amount_in * self.fee_num) // (r_in * FEE_DENOM + amount_in * self.fee_num)
        return to_id, out, r_in, r_out


def load_pool(cfg, pool_id):
    res = explorer(cfg, "boxes/unspent/byTokenId/" + pool_id)
    items = (res or {}).get("items") or []
    # the pool NFT (first token, amount 1) identifies the pool box
    items = [b for b in items if b.get("assets") and b["assets"][0].get("tokenId") == pool_id
             and int(b["assets"][0].get("amount", 0)) == 1]
    if len(items) != 1:
        raise SwapError("Pool not found.")
    return Pool(items[0])


def impact_bps(amount_in, amount_out, r_in, r_out, fee_num):
    """Price impact vs. the pool's current (fee-adjusted) spot price, in basis points."""
    if amount_out <= 0:
        return 10000
    ideal_num = amount_in * r_out * fee_num          # ideal_out = ideal_num / ideal_den
    ideal_den = r_in * FEE_DENOM
    bps = 10000 - (amount_out * ideal_den * 10000) // ideal_num
    return max(0, min(10000, bps))


# ----------------------------------------------------------------- quote ----

def cmd_quote(req):
    cfg = req["cfg"]
    pool = load_pool(cfg, req_token_id(req.get("pool"), "pool"))
    from_id = req_token_id(req.get("from"), "token")
    amount = req_amount(req.get("amount"), "amount")
    to_id, out, r_in, r_out = pool.quote(from_id, amount)
    res = {
        "pool": pool.nft, "kind": pool.kind, "from": from_id, "to": to_id,
        "amount": str(amount), "out": str(out),
        "fee_num": pool.fee_num, "fee_denom": FEE_DENOM,
        "impact_bps": impact_bps(amount, out, r_in, r_out, pool.fee_num),
        "reserve_in": str(r_in), "reserve_out": str(r_out),
    }
    if req.get("address"):
        req_address(req["address"])
        res["balance"] = _balance(cfg, req["address"], from_id)
    return res


def cmd_balance(req):
    req_address(req.get("address"))
    return {"balance": _balance(req["cfg"], req["address"], req_token_id(req.get("token"), "token"))}


def _balance(cfg, address, token_id):
    bal = explorer(cfg, "addresses/%s/balance/confirmed" % urllib.parse.quote(address, safe="")) or {}
    if token_id == ERG_ID:
        return str(int(bal.get("nanoErgs", 0)))
    for t in bal.get("tokens") or []:
        if t.get("tokenId") == token_id:
            return str(int(t.get("amount", 0)))
    return "0"


# ----------------------------------------------------------------- build ----

def change_box_min_value(n_tokens):
    # generous size estimate: fixed part + 42 bytes per token, at 360 nanoERG/byte
    return max(1000000, (150 + 42 * n_tokens) * MIN_BOX_VALUE_PER_BYTE)


def fetch_user_boxes(cfg, address):
    from ergo_lib_python.chain import ErgoBox
    boxes, offset = [], 0
    while offset < MAX_USER_BOXES_SCANNED:
        page = explorer(cfg, "boxes/unspent/byAddress/%s?limit=100&offset=%d"
                        % (urllib.parse.quote(address, safe=""), offset)) or {}
        items = page.get("items") or []
        for b in items:
            boxes.append(ErgoBox.from_json(b))
        offset += 100
        if len(items) < 100:
            break
    return boxes


def select_inputs(boxes, need_erg, need_token_id, need_token_amt):
    """Pick user boxes covering the sold token (if any) and enough ERG.

    Returns (selected boxes, total nanoERG, {token_id: amount}). `need_erg` is a
    function of the number of distinct tokens that end up in the change, because
    every extra token makes the change box bigger and so needs more ERG.
    """
    def tok(b):
        return dict((hexid(t.token_id), t.amount) for t in b.tokens)

    remaining = list(boxes)
    chosen = []
    if need_token_id is not None:
        have = 0
        with_token = sorted((b for b in remaining if need_token_id in tok(b)),
                            key=lambda b: -tok(b)[need_token_id])
        for b in with_token:
            if have >= need_token_amt:
                break
            chosen.append(b)
            have += tok(b)[need_token_id]
        if have < need_token_amt:
            raise SwapError("Insufficient balance of the token you are selling on this address.")
        ids = set(hexid(b.box_id) for b in chosen)
        remaining = [b for b in remaining if hexid(b.box_id) not in ids]

    def totals():
        erg, toks = 0, {}
        for b in chosen:
            erg += b.value
            for k, v in tok(b).items():
                toks[k] = toks.get(k, 0) + v
        return erg, toks

    remaining.sort(key=lambda b: -b.value)
    while True:
        erg, toks = totals()
        if erg >= need_erg(toks):
            return chosen, erg, toks
        if not remaining:
            raise SwapError("Insufficient ERG on this address for the swap plus network fee.")
        if len(chosen) >= MAX_USER_INPUTS:
            raise SwapError("This address holds too many small boxes. Consolidate the wallet "
                            "(send your funds to yourself) and try again.")
        chosen.append(remaining.pop(0))


def cmd_build(req):
    from ergo_lib_python.chain import (ErgoBoxCandidate, ErgoStateContext, Header, Parameters,
                                       PreHeader, Token, TokenId, NetworkPrefix)
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection, Wallet

    cfg = req["cfg"]
    pool_id = req_token_id(req.get("pool"), "pool")
    from_id = req_token_id(req.get("from"), "token")
    amount = req_amount(req.get("amount"), "amount")
    min_out = req_amount(req.get("min_out"), "minimum output")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    fee = int(cfg.get("miner_fee", 1100000))
    if not (1000000 <= fee <= 100000000):
        raise SwapError("Service misconfigured.")

    pool = load_pool(cfg, pool_id)
    to_id, out, r_in, r_out = pool.quote(from_id, amount)
    if out <= 0:
        raise SwapError("Amount is too small to swap.")
    if out < min_out:
        raise SwapError("The price moved beyond your slippage tolerance. Please preview the swap again.")
    if out >= r_out:
        raise SwapError("Not enough liquidity in this pool.")

    # what the user pays / receives, split into ERG and token parts
    erg_in = amount if from_id == ERG_ID else 0
    erg_out = out if to_id == ERG_ID else 0
    sell_token = None if from_id == ERG_ID else from_id

    def need_erg(toks):
        n = len(set(toks) | ({to_id} if to_id != ERG_ID else set()))
        extra_boxes = max(0, (n - 1) // MAX_TOKENS_PER_BOX)
        need = erg_in + fee - erg_out
        need += change_box_min_value(min(n, MAX_TOKENS_PER_BOX))
        need += extra_boxes * change_box_min_value(MAX_TOKENS_PER_BOX)
        return need

    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    chosen, user_erg, user_toks = select_inputs(user_boxes, need_erg, sell_token, amount)

    headers = [Header.from_json(h) for h in (explorer(cfg, "blocks/headers?limit=10") or {}).get("items", [])]
    if len(headers) != 10:
        raise SwapError("Blockchain data is unavailable right now. Please try again.")
    height = headers[0].height

    # ---- new pool box: identical script/registers/token ids, only reserves change
    new_tokens = list(pool.tokens)
    new_value = pool.box.value
    if pool.kind == "n2t":
        if from_id == ERG_ID:
            new_value += amount
            new_tokens[2] = (pool.y_id, pool.y_amt - out)
        else:
            new_value -= out
            new_tokens[2] = (pool.y_id, pool.y_amt + amount)
    else:
        if from_id == pool.x_id:
            new_tokens[2] = (pool.x_id, pool.x_amt + amount)
            new_tokens[3] = (pool.y_id, pool.y_amt - out)
        else:
            new_tokens[2] = (pool.x_id, pool.x_amt - out)
            new_tokens[3] = (pool.y_id, pool.y_amt + amount)
    pool_out = ErgoBoxCandidate(
        value=new_value, script=pool.box.ergo_tree, creation_height=height,
        tokens=[Token(TokenId(t), a) for t, a in new_tokens],
        registers=pool.registers)

    # ---- user output(s): swap result + all change, back to the same address
    out_toks = dict(user_toks)
    if sell_token is not None:
        out_toks[sell_token] -= amount
        if out_toks[sell_token] == 0:
            del out_toks[sell_token]
    if to_id != ERG_ID:
        out_toks[to_id] = out_toks.get(to_id, 0) + out
    out_erg = user_erg - erg_in + erg_out - fee

    # received token first so it is the first thing the wallet shows
    tok_list = sorted(out_toks.items(), key=lambda kv: (kv[0] != to_id, kv[0]))
    chunks = [tok_list[i:i + MAX_TOKENS_PER_BOX] for i in range(0, len(tok_list), MAX_TOKENS_PER_BOX)] or [[]]
    extra_value = change_box_min_value(MAX_TOKENS_PER_BOX)
    main_value = out_erg - extra_value * (len(chunks) - 1)
    if main_value < change_box_min_value(len(chunks[0])):
        raise SwapError("Insufficient ERG on this address for the swap plus network fee.")
    user_outs = []
    for i, chunk in enumerate(chunks):
        user_outs.append(ErgoBoxCandidate(
            value=main_value if i == 0 else extra_value, script=address.ergo_tree(),
            creation_height=height, tokens=[Token(TokenId(t), a) for t, a in chunk]))

    inputs = [pool.box] + chosen
    builder = TxBuilder(BoxSelection(inputs, []), [pool_out] + user_outs, height, fee, address)
    unsigned = builder.build()      # refuses unbalanced ERG/tokens (no silent burn, no surprise change)

    ctx = ErgoStateContext(PreHeader(headers[0]), headers, Parameters.default())
    # Prove the pool contract accepts this exact transaction BEFORE handing it to
    # a wallet: "sign" the pool input with a wallet that holds no keys. That only
    # succeeds when the pool script evaluates to true for this transaction. (Do
    # not rely on reduction alone - a script that evaluates to false still
    # reduces without an error.)
    try:
        Wallet([]).sign_tx_input(unsigned, 0, inputs, [], ctx)
    except Exception as e:
        sys.stderr.write("sigmaswap.py: pool %s rejected the swap: %s\n" % (pool.nft, str(e)[:300]))
        raise SwapError("The pool did not accept this swap. Please preview it again.")
    reduced = ReducedTransaction.from_unsigned_tx(unsigned, inputs, [], ctx)

    return {
        "reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"),
        "tx_id": hexid(unsigned.id),
        "address": addr_str,
        "pool": pool.nft, "from": from_id, "to": to_id,
        "amount": str(amount), "out": str(out), "miner_fee": str(fee),
        "inputs": len(inputs), "height": height,
    }


# ------------------------------------------------------------------ main ----

def cmd_health(req):
    """Deploy gate: proves python + ergo-lib-python load under Apache/SELinux."""
    import ergo_lib_python
    res = {"python": "%d.%d.%d" % sys.version_info[:3], "ergo_lib": ergo_lib_python.__version__}
    if req.get("network"):
        info = explorer(req["cfg"], "info") or {}
        res["height"] = int(info.get("height", 0))
    return res


COMMANDS = {"health": cmd_health, "pools": cmd_pools,"quote": cmd_quote, "balance": cmd_balance, "build": cmd_build}


def main():
    try:
        cmd = sys.argv[1] if len(sys.argv) > 1 else ""
        if cmd not in COMMANDS:
            raise SwapError("Unknown command.")
        req = json.loads(sys.stdin.read() or "{}")
        res = COMMANDS[cmd](req)
        res["ok"] = True
    except SwapError as e:
        res = {"ok": False, "error": str(e)}
    except Exception as e:                       # details to the server log only
        sys.stderr.write("sigmaswap.py %s failed: %s: %s\n"
                         % (sys.argv[1:2], type(e).__name__, str(e)[:500]))
        msg = "Could not reach the Ergo network. Please try again."
        if type(e).__name__ in ("WalletException", "ValueError"):
            msg = "The swap could not be built. The pool may have just changed - please preview again."
        res = {"ok": False, "error": msg}
    sys.stdout.write(json.dumps(res))


if __name__ == "__main__":
    main()
