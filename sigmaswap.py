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
MAX_PENDING_CHAIN = 30           # how many unconfirmed swaps on one pool we are willing to build on
HTTP_TIMEOUT = 15
MEMPOOL_TIMEOUT = 4              # mempool data is optional: never let it hold up a quote


class SwapError(Exception):
    """An error whose message is safe and useful to show to the user."""


# ---------------------------------------------------------------- http ----

def http_json(url, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sigmaswap/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def explorer(cfg, path, timeout=HTTP_TIMEOUT):
    return http_json(cfg["explorer_url"].rstrip("/") + "/api/v1/" + path, timeout)


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

    pending = 0     # number of unconfirmed swaps this box sits on top of

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


def mempool_txs(cfg, address):
    """Unconfirmed transactions touching `address` ([] when unavailable or switched off).

    Best effort by design: if the explorer's mempool view is missing or wrong, the
    worst case is a transaction the network rejects, which costs the user nothing.
    """
    if not cfg.get("use_mempool", True):
        return []
    try:
        page = explorer(cfg, "mempool/transactions/byAddress/%s?limit=100"
                        % urllib.parse.quote(address, safe=""), MEMPOOL_TIMEOUT) or {}
        return [t for t in (page.get("items") or []) if isinstance(t, dict)]
    except Exception as e:
        sys.stderr.write("sigmaswap.py: mempool lookup failed, using confirmed state: %s\n" % str(e)[:200])
        return []


def load_pool(cfg, pool_id):
    """The pool's newest box: the confirmed one, advanced over swaps still in the mempool.

    Building on top of pending swaps lets several swaps on one pool land in the same
    block instead of all but the first being rejected.
    """
    res = explorer(cfg, "boxes/unspent/byTokenId/" + pool_id)
    items = (res or {}).get("items") or []
    # the pool NFT (first token, amount 1) identifies the pool box
    def is_pool_box(b):
        return bool(b.get("assets")) and b["assets"][0].get("tokenId") == pool_id \
            and int(b["assets"][0].get("amount", 0)) == 1
    items = [b for b in items if is_pool_box(b)]
    if len(items) != 1:
        raise SwapError("Pool not found.")
    pool = Pool(items[0])
    pool.pending = 0

    txs = mempool_txs(cfg, items[0].get("address") or "")
    spender = {}
    for t in txs:
        for i in t.get("inputs") or []:
            spender[i.get("boxId")] = t
    current = items[0]
    while pool.pending < MAX_PENDING_CHAIN and current.get("boxId") in spender:
        nxt = [o for o in (spender[current["boxId"]].get("outputs") or [])
               if is_pool_box(o) and o.get("ergoTree") == current.get("ergoTree")]
        if len(nxt) != 1:
            break
        try:
            candidate = Pool(nxt[0])      # same validation as a confirmed pool box (incl. box id check)
        except Exception:
            break
        candidate.pending = pool.pending + 1
        pool, current = candidate, nxt[0]
    return pool


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
        "pending": pool.pending,
        "reserve_in": str(r_in), "reserve_out": str(r_out),
    }
    if pool.kind == "n2t":
        op = order_params(cfg)
        res["order"] = {"min_ex_fee": str(op["min_ex_fee"]), "max_ex_fee": str(op["max_ex_fee"]),
                        "extra": str(op["extra"])}
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
    """Spendable boxes of the address, taking its own unconfirmed transactions into account:
    boxes already spent in the mempool are dropped, unconfirmed change is added."""
    from ergo_lib_python.chain import Address, ErgoBox
    boxes, offset = {}, 0
    while offset < MAX_USER_BOXES_SCANNED:
        page = explorer(cfg, "boxes/unspent/byAddress/%s?limit=100&offset=%d"
                        % (urllib.parse.quote(address, safe=""), offset)) or {}
        items = page.get("items") or []
        for b in items:
            box = ErgoBox.from_json(b)
            boxes[hexid(box.box_id)] = box
        offset += 100
        if len(items) < 100:
            break

    txs = mempool_txs(cfg, address)
    if txs:
        own_tree = bytes(Address(address).ergo_tree()).hex()
        spent = set(i.get("boxId") for t in txs for i in (t.get("inputs") or []))
        for t in txs:
            for o in t.get("outputs") or []:
                if o.get("ergoTree") != own_tree or o.get("boxId") in spent:
                    continue
                try:
                    box = ErgoBox.from_json(o)
                    boxes[hexid(box.box_id)] = box
                except Exception:
                    pass
        for box_id in spent:
            boxes.pop(box_id, None)
    return list(boxes.values())


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


def miner_fee(cfg):
    fee = int(cfg.get("miner_fee", 1100000))
    if not (1000000 <= fee <= 100000000):
        raise SwapError("Service misconfigured.")
    return fee


def chain_context(cfg):
    """-> (ErgoStateContext, current height) from the last 10 block headers."""
    from ergo_lib_python.chain import ErgoStateContext, Header, Parameters, PreHeader
    headers = [Header.from_json(h) for h in (explorer(cfg, "blocks/headers?limit=10") or {}).get("items", [])]
    if len(headers) != 10:
        raise SwapError("Blockchain data is unavailable right now. Please try again.")
    return ErgoStateContext(PreHeader(headers[0]), headers, Parameters.default()), headers[0].height


def new_pool_candidate(pool, from_id, amount, out, height):
    """The pool box after a swap: identical script/registers/token ids, only reserves change."""
    from ergo_lib_python.chain import ErgoBoxCandidate, Token, TokenId
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
    return ErgoBoxCandidate(
        value=new_value, script=pool.box.ergo_tree, creation_height=height,
        tokens=[Token(TokenId(t), a) for t, a in new_tokens],
        registers=pool.registers)


def change_need(n_tokens):
    """nanoERG needed to carry `n_tokens` distinct tokens back to the user."""
    extra_boxes = max(0, (n_tokens - 1) // MAX_TOKENS_PER_BOX)
    return (change_box_min_value(min(n_tokens, MAX_TOKENS_PER_BOX))
            + extra_boxes * change_box_min_value(MAX_TOKENS_PER_BOX))


def user_outputs(address, height, out_toks, out_erg, first_token=None):
    """Box(es) returning `out_erg` + `out_toks` to the user (split when there are many tokens)."""
    from ergo_lib_python.chain import ErgoBoxCandidate, Token, TokenId
    # `first_token` (the received token) first, so it is the first thing the wallet shows
    tok_list = sorted(((t, a) for t, a in out_toks.items() if a > 0), key=lambda kv: (kv[0] != first_token, kv[0]))
    chunks = [tok_list[i:i + MAX_TOKENS_PER_BOX] for i in range(0, len(tok_list), MAX_TOKENS_PER_BOX)] or [[]]
    extra_value = change_box_min_value(MAX_TOKENS_PER_BOX)
    main_value = out_erg - extra_value * (len(chunks) - 1)
    if main_value < change_box_min_value(len(chunks[0])):
        raise SwapError("Insufficient ERG on this address for the swap plus network fee.")
    outs = []
    for i, chunk in enumerate(chunks):
        outs.append(ErgoBoxCandidate(
            value=main_value if i == 0 else extra_value, script=address.ergo_tree(),
            creation_height=height, tokens=[Token(TokenId(t), a) for t, a in chunk]))
    return outs


def cmd_build(req):
    if req.get("mode") == "order":
        return build_order(req)
    from ergo_lib_python.chain import NetworkPrefix
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection, Wallet

    cfg = req["cfg"]
    pool_id = req_token_id(req.get("pool"), "pool")
    from_id = req_token_id(req.get("from"), "token")
    amount = req_amount(req.get("amount"), "amount")
    min_out = req_amount(req.get("min_out"), "minimum output")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    fee = miner_fee(cfg)

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
        return erg_in + fee - erg_out + change_need(n)

    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    chosen, user_erg, user_toks = select_inputs(user_boxes, need_erg, sell_token, amount)

    ctx, height = chain_context(cfg)
    pool_out = new_pool_candidate(pool, from_id, amount, out, height)

    # ---- user output(s): swap result + all change, back to the same address
    out_toks = dict(user_toks)
    if sell_token is not None:
        out_toks[sell_token] -= amount
    if to_id != ERG_ID:
        out_toks[to_id] = out_toks.get(to_id, 0) + out
    user_outs = user_outputs(address, height, out_toks, user_erg - erg_in + erg_out - fee, to_id)

    inputs = [pool.box] + chosen
    builder = TxBuilder(BoxSelection(inputs, []), [pool_out] + user_outs, height, fee, address)
    unsigned = builder.build()      # refuses unbalanced ERG/tokens (no silent burn, no surprise change)

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
        "address": addr_str, "mode": "direct",
        "pool": pool.nft, "from": from_id, "to": to_id,
        "amount": str(amount), "out": str(out), "miner_fee": str(fee),
        "inputs": len(inputs), "height": height,
        "watch_box": hexid(pool.box.box_id), "pending": pool.pending,
    }


# ---------------------------------------------------------------- orders ----
# "Order" mode = the classic ErgoDex flow. Instead of touching the pool, the user
# sends the funds to a swap-order box. ErgoDex's off-chain bots execute it against
# the pool (retrying as the pool moves), and the order contract guarantees the
# user gets at least `min_out` at the pool's own price, or the user cancels and
# takes everything back with their own signature. Never fails on a busy pool.
#
# The two contracts below are the ErgoDex native-fee ERG<->token swap orders
# (ergo-dex-sdk-js: nativeFee/n2tTemplates SwapSellSample / SwapBuySample). Trees
# built from them were checked byte-for-byte against orders the bots executed.

ORDER_SELL_SAMPLE = "19c4031808d2040005e012040404060402040004000e2002020202020202020202020202020202020202020202020202020202020202020e20010101010101010101010101010101010101010101010101010101010101010105c00c05040514040404c80f06010104d00f05e01204c80f0e691005040004000e36100204a00b08cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798ea02d192a39a8cc7a701730073011001020402d19683030193a38cc7b2a57300000193c2b2a57301007473027303830108cdeeac93b1a573040500050005a09c010100d803d6017300d602b2a4730100d6037302eb027201d195ed92b1a4730393b1db630872027304d804d604db63087202d605b2a5730500d606b2db63087205730600d6077e8c72060206edededededed938cb2720473070001730893c27205d07201938c72060173099272077e730a06927ec172050699997ec1a7069d9c72077e730b067e730c067e720306909c9c7e8cb27204730d0002067e7203067e730e069c9a7207730f9a9c7ec17202067e7310067e9c73117e7312050690b0ada5d90108639593c272087313c1720873147315d90108599a8c7208018c72080273167317"
ORDER_BUY_SAMPLE = "1980031508d2040004040406040205140512040004000e20020202020202020202020202020202020202020202020202020202020202020205c00c04c80f060101040404d00f04c80f0e691005040004000e36100204a00b08cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798ea02d192a39a8cc7a701730073011001020402d19683030193a38cc7b2a57300000193c2b2a57301007473027303830108cdeeac93b1a573040500050005a09c010100d802d6017300d602b2a4730100eb027201d195ed92b1a4730293b1db630872027303d804d603db63087202d604b2a5730400d6059d9c7e99c17204c1a7067e7305067e730606d6068cb2db6308a773070002edededed938cb2720373080001730993c27204d072019272057e730a06909c9c7ec17202067e7206067e730b069c9a7205730c9a9c7e8cb27203730d0002067e730e067e9c72067e730f050690b0ada5d90107639593c272077310c1720773117312d90107599a8c7207018c72070273137314"
# sha256 of each contract with its constants removed: how the explorer indexes orders
ORDER_SELL_TEMPLATE_HASH = "36d1944fe6d743312e93541a1daf5592519772310ce924b2ec10e3f9b450e13a"
ORDER_BUY_TEMPLATE_HASH = "be1312f720ad4de7a8528a0306478292e48fdc996d44e9014f704632b512b691"

# positions of the per-order constants inside each contract
SELL_IDX = {"pk": 0, "base": (2, 17), "pool": 8, "quote": 9, "min_out": 10,
            "fee_num": 11, "fee_den": 12, "pool_fee": (14, 18), "max_miner_fee": 22}
BUY_IDX = {"pk": 0, "fee_den": 5, "fee_den_minus_num": 6, "pool": 9, "min_out": 10,
           "pool_fee": (11, 15), "max_miner_fee": 19}

ORDER_BOX_RESERVE = 60000        # nanoERG kept in the order box on top of the execution fee
MAX_ORDERS_SCANNED = 400


def order_params(cfg):
    """Execution-fee settings. Defaults are what the ErgoDex web UI uses."""
    min_fee = int(cfg.get("order_min_ex_fee", 6000000))
    exec_fee = int(cfg.get("order_miner_fee", 2000000))
    if not (exec_fee < min_fee <= 1000000000 and 1000000 <= exec_fee):
        raise SwapError("Service misconfigured.")
    max_fee = min_fee * 12 // 10                    # covers up to 20 % more output than the minimum
    return {"min_ex_fee": min_fee, "max_ex_fee": max_fee, "exec_miner_fee": exec_fee,
            "extra": max_fee + ORDER_BOX_RESERVE}


def pk_constant(address):
    """The address's public key as a SigmaProp constant (first constant of an order)."""
    from ergo_lib_python.chain import Constant
    return Constant.from_bytes(bytes(address.ergo_tree())[1:])


def order_terms(pool, from_id, amount, min_out, op):
    """Everything that defines an order for this swap. Raises when an order makes no sense."""
    if pool.kind != "n2t":
        raise SwapError("Orders are only available for pairs with ERG. Use a direct swap for this pair.")
    if from_id == ERG_ID:
        kind, to_id = "sell", pool.y_id
        # execution fee = received tokens * num / den, sized so it is min_ex_fee at min_out
        den = 10 ** 18
        num = -(-op["min_ex_fee"] * den // min_out)
        while num >= 2 ** 63 and den > 1:
            den //= 10
            num = -(-op["min_ex_fee"] * den // min_out)
        if num >= 2 ** 63:
            raise SwapError("This amount is too small for an order. Use a direct swap.")
        return {"kind": kind, "to": to_id, "fee_num": num, "fee_den": den,
                "box_value": amount + op["extra"], "box_tokens": []}
    if from_id != pool.y_id:
        raise SwapError("That token is not part of this pool.")
    # buying ERG: the fee is a share of the ERG received, so the order must be worth a few fees
    if min_out < 3 * op["max_ex_fee"]:
        raise SwapError("This amount is too small for an order (the execution fee would eat it). "
                        "Use a direct swap.")
    den = 10 ** 18
    num = -(-op["min_ex_fee"] * den // min_out)
    return {"kind": "buy", "to": ERG_ID, "fee_num": num, "fee_den": den,
            "box_value": op["extra"], "box_tokens": [(from_id, amount)]}


def order_tree(terms, pool, pk_const, amount, min_out, op):
    from ergo_lib_python.chain import Constant
    from ergo_lib_python.ergo_tree import ErgoTree
    i64, i32 = Constant.from_i64, Constant.from_i32
    if terms["kind"] == "sell":
        tree, ix = ErgoTree.from_bytes(bytes.fromhex(ORDER_SELL_SAMPLE)), SELL_IDX
        sets = [(ix["pk"], pk_const), (ix["base"][0], i64(amount)), (ix["base"][1], i64(amount)),
                (ix["pool"], Constant(bytes.fromhex(pool.nft))), (ix["quote"], Constant(bytes.fromhex(pool.y_id))),
                (ix["min_out"], i64(min_out)), (ix["fee_num"], i64(terms["fee_num"])),
                (ix["fee_den"], i64(terms["fee_den"])),
                (ix["pool_fee"][0], i32(pool.fee_num)), (ix["pool_fee"][1], i32(pool.fee_num)),
                (ix["max_miner_fee"], i64(op["exec_miner_fee"]))]
    else:
        tree, ix = ErgoTree.from_bytes(bytes.fromhex(ORDER_BUY_SAMPLE)), BUY_IDX
        sets = [(ix["pk"], pk_const), (ix["fee_den"], i64(terms["fee_den"])),
                (ix["fee_den_minus_num"], i64(terms["fee_den"] - terms["fee_num"])),
                (ix["pool"], Constant(bytes.fromhex(pool.nft))), (ix["min_out"], i64(min_out)),
                (ix["pool_fee"][0], i32(pool.fee_num)), (ix["pool_fee"][1], i32(pool.fee_num)),
                (ix["max_miner_fee"], i64(op["exec_miner_fee"]))]
    for idx, const in sets:
        tree = tree.with_constant(idx, const)
    return tree


def simulate_execution(pool, order_box, terms, address, amount, min_out, op, ctx, height):
    """Build the transaction a bot would build to execute `order_box` right now and
    check that BOTH the pool and the order contract accept it.

    This is the proof that the order is not a dead end: at the current pool price a
    standard execution is valid. Returns (tokens/nanoERG the user would receive,
    execution fee). Raises SwapError if the order could not be executed as-is.
    """
    from ergo_lib_python.chain import ErgoBoxCandidate, Token, TokenId
    from ergo_lib_python.transaction import TxBuilder
    from ergo_lib_python.wallet import BoxSelection, Wallet
    from_id = ERG_ID if terms["kind"] == "sell" else pool.y_id
    to_id, out, _r_in, r_out = pool.quote(from_id, amount)
    if out < min_out or out >= r_out:
        raise SwapError("The price moved beyond your slippage tolerance. Please preview the swap again.")
    try:
        if terms["kind"] == "sell":
            ex_fee = out * terms["fee_num"] // terms["fee_den"]
            reward = ErgoBoxCandidate(value=order_box.value - amount - ex_fee, script=address.ergo_tree(),
                                      creation_height=height, tokens=[Token(TokenId(to_id), out)])
        else:
            # round the user's share UP: the contract re-derives the gross amount from it
            # with a floor division and must arrive back at (at least) the pool's output
            net = -(-out * (terms["fee_den"] - terms["fee_num"]) // terms["fee_den"])
            ex_fee = out - net
            reward = ErgoBoxCandidate(value=order_box.value + net, script=address.ergo_tree(),
                                      creation_height=height)
        if not (op["exec_miner_fee"] < ex_fee <= op["max_ex_fee"]):
            raise ValueError("execution fee %d outside the funded range" % ex_fee)
        bot = ErgoBoxCandidate(value=ex_fee - op["exec_miner_fee"], script=address.ergo_tree(),
                               creation_height=height)      # any address: the contract does not care
        pool_out = new_pool_candidate(pool, from_id, amount, out, height)
        inputs = [pool.box, order_box]
        tx = TxBuilder(BoxSelection(inputs, []), [pool_out, reward, bot], height,
                       op["exec_miner_fee"], address).build()
        Wallet([]).sign_tx_input(tx, 0, inputs, [], ctx)      # pool accepts
        Wallet([]).sign_tx_input(tx, 1, inputs, [], ctx)      # order accepts, with no user key
    except SwapError:
        raise
    except Exception as e:
        sys.stderr.write("sigmaswap.py: order simulation failed for pool %s: %s\n" % (pool.nft, str(e)[:300]))
        raise SwapError("This order could not be executed at the current price. "
                        "Please preview again or use a direct swap.")
    return (out if terms["kind"] == "sell" else net), ex_fee


def build_order(req):
    from ergo_lib_python.chain import ErgoBox, ErgoBoxCandidate, NetworkPrefix, Token, TokenId
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection

    cfg = req["cfg"]
    pool_id = req_token_id(req.get("pool"), "pool")
    from_id = req_token_id(req.get("from"), "token")
    amount = req_amount(req.get("amount"), "amount")
    min_out = req_amount(req.get("min_out"), "minimum output")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    fee = miner_fee(cfg)
    op = order_params(cfg)

    pool = load_pool(cfg, pool_id)
    terms = order_terms(pool, from_id, amount, min_out, op)
    sell_token = None if from_id == ERG_ID else from_id

    def need_erg(toks):
        n = len(set(toks) - ({sell_token} if sell_token and toks.get(sell_token) == amount else set()))
        return terms["box_value"] + fee + change_need(n)

    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    chosen, user_erg, user_toks = select_inputs(user_boxes, need_erg, sell_token, amount)

    ctx, height = chain_context(cfg)
    order_out = ErgoBoxCandidate(
        value=terms["box_value"], script=order_tree(terms, pool, pk_constant(address), amount, min_out, op),
        creation_height=height, tokens=[Token(TokenId(t), a) for t, a in terms["box_tokens"]])

    out_toks = dict(user_toks)
    if sell_token is not None:
        out_toks[sell_token] -= amount
    user_outs = user_outputs(address, height, out_toks, user_erg - terms["box_value"] - fee)

    unsigned = TxBuilder(BoxSelection(chosen, []), [order_out] + user_outs, height, fee, address).build()

    # the order box exactly as it will exist once this transaction confirms
    order_box = ErgoBox.from_box_candidate(order_out, unsigned.id, 0)
    out, ex_fee = simulate_execution(pool, order_box, terms, address, amount, min_out, op, ctx, height)

    reduced = ReducedTransaction.from_unsigned_tx(unsigned, chosen, [], ctx)
    return {
        "reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"),
        "tx_id": hexid(unsigned.id),
        "address": addr_str, "mode": "order",
        "pool": pool.nft, "from": from_id, "to": terms["to"],
        "amount": str(amount), "out": str(out), "min_out": str(min_out),
        "ex_fee": str(ex_fee), "max_ex_fee": str(op["max_ex_fee"]), "miner_fee": str(fee),
        "inputs": len(chosen), "height": height,
        "watch_box": hexid(chosen[0].box_id), "order_box": hexid(order_box.box_id),
    }


def parse_order(box_json, pk_bytes=None):
    """Recognise one of OUR two order contracts. -> dict, or None for anything else.

    A box counts only if its script is exactly the known contract with different
    constants (rebuilt and compared byte-for-byte), and, when `pk_bytes` is given,
    only if it belongs to that public key.
    """
    from ergo_lib_python.chain import ErgoBox
    from ergo_lib_python.ergo_tree import ErgoTree
    try:
        box = ErgoBox.from_json(box_json)
        tree = box.ergo_tree
        consts = tree.constants()
        raw = bytes(tree)
        for kind, sample, ix in (("sell", ORDER_SELL_SAMPLE, SELL_IDX), ("buy", ORDER_BUY_SAMPLE, BUY_IDX)):
            ref = ErgoTree.from_bytes(bytes.fromhex(sample))
            if len(ref.constants()) != len(consts):
                continue
            positions = []
            for v in ix.values():
                positions.extend(v if isinstance(v, tuple) else (v,))
            for p in positions:
                ref = ref.with_constant(p, consts[p])
            if bytes(ref) != raw:
                continue
            if pk_bytes is not None and bytes(consts[ix["pk"]]) != pk_bytes:
                return None
            toks = [(hexid(t.token_id), t.amount) for t in box.tokens]
            if kind == "sell":
                amount, from_id, to_id = consts[ix["base"][0]].value, ERG_ID, bytes(consts[ix["quote"]].value).hex()
            else:
                if len(toks) != 1:
                    return None
                (from_id, amount), to_id = toks[0], ERG_ID
            return {"box": box, "box_id": hexid(box.box_id), "kind": kind,
                    "pool": bytes(consts[ix["pool"]].value).hex(),
                    "from": from_id, "to": to_id, "amount": str(amount),
                    "min_out": str(consts[ix["min_out"]].value), "value": str(box.value)}
    except Exception as e:
        sys.stderr.write("sigmaswap.py: skipped unreadable order box: %s\n" % str(e)[:200])
    return None


def cmd_orders(req):
    """Open (unexecuted) swap orders of one address, found on-chain - nothing is stored here."""
    cfg = req["cfg"]
    address = req_address(req.get("address"))
    pk = bytes(pk_constant(address))
    found = []
    for template_hash in (ORDER_SELL_TEMPLATE_HASH, ORDER_BUY_TEMPLATE_HASH):
        offset = 0
        while offset < MAX_ORDERS_SCANNED:
            page = explorer(cfg, "boxes/unspent/byErgoTreeTemplateHash/%s?limit=100&offset=%d"
                            % (template_hash, offset)) or {}
            items = page.get("items") or []
            for b in items:
                # cheap pre-filter: the key is embedded in the script
                if pk.hex() not in (b.get("ergoTree") or ""):
                    continue
                o = parse_order(b, pk)
                if o:
                    del o["box"]
                    o["height"] = int(b.get("settlementHeight") or 0)
                    found.append(o)
            offset += 100
            if len(items) < 100:
                break
    found.sort(key=lambda o: -o["height"])
    return {"orders": found[:20]}


def cmd_refund(req):
    """Cancel an open order: the owner takes the whole order box back (minus the miner fee)."""
    from ergo_lib_python.chain import ErgoBoxCandidate, NetworkPrefix
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection
    cfg = req["cfg"]
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    box_id = req_token_id(req.get("box"), "order")
    fee = miner_fee(cfg)

    b = explorer(cfg, "boxes/" + box_id)
    if not b or b.get("spentTransactionId"):
        raise SwapError("This order is no longer open: it was already executed or cancelled.")
    o = parse_order(b, bytes(pk_constant(address)))
    if not o:
        raise SwapError("This order does not belong to the selected address.")
    box = o["box"]
    if box.value - fee < change_box_min_value(len(box.tokens)):
        raise SwapError("This order box is too small to pay the network fee for a refund.")

    ctx, height = chain_context(cfg)
    back = ErgoBoxCandidate(value=box.value - fee, script=address.ergo_tree(),
                            creation_height=height, tokens=list(box.tokens))
    unsigned = TxBuilder(BoxSelection([box], []), [back], height, fee, address).build()
    reduced = ReducedTransaction.from_unsigned_tx(unsigned, [box], [], ctx)
    return {
        "reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"),
        "tx_id": hexid(unsigned.id), "address": addr_str, "mode": "refund",
        "from": o["from"], "amount": o["amount"], "value": str(box.value - fee), "miner_fee": str(fee),
    }


# ---------------------------------------------------------------- status ----

def _tx_or_none(cfg, tx_id):
    return explorer(cfg, "transactions/" + tx_id)


def cmd_status(req):
    """What happened to a transaction this app built. Read-only, from public chain data.

    states: confirmed | pending | failed | unknown        (any mode)
            order_open | executed | cancelled              (order mode, once the order confirmed)
    """
    cfg = req["cfg"]
    tx_id = req_token_id(req.get("tx_id"), "transaction")
    watch = req_token_id(req.get("watch_box"), "transaction")
    mode = req.get("mode")
    address = req.get("address")

    tx = _tx_or_none(cfg, tx_id)
    if tx and tx.get("inclusionHeight"):
        res = {"state": "confirmed", "confirmations": int(tx.get("numConfirmations") or 0)}
        if mode != "order":
            return res
        order_box = req_token_id(req.get("order_box"), "order")
        b = explorer(cfg, "boxes/" + order_box) or {}
        spent_by = b.get("spentTransactionId")
        if not spent_by:
            # executed/cancelled but not yet in a block?
            req_address(address)
            for t in mempool_txs(cfg, b.get("address") or ""):
                if any(i.get("boxId") == order_box for i in t.get("inputs") or []):
                    res["state"] = "executing"
                    return res
            res["state"] = "order_open"
            return res
        stx = _tx_or_none(cfg, spent_by) or {}
        outs = stx.get("outputs") or []
        if len(stx.get("inputs") or []) >= 2 and len(outs) >= 2:
            reward = outs[1]          # the order contract fixes the user's reward at output 1
            res["state"] = "executed"
            assets = reward.get("assets") or []
            if req.get("to") == ERG_ID:
                res["received"] = str(max(0, int(reward.get("value", 0)) - int(b.get("value", 0))))
            elif assets and assets[0].get("tokenId") == req.get("to"):
                res["received"] = str(int(assets[0].get("amount", 0)))
        else:
            res["state"] = "cancelled"
        return res

    # not in a block: is it waiting in the mempool?
    if address:
        req_address(address)
        if any(t.get("id") == tx_id for t in mempool_txs(cfg, address)):
            return {"state": "pending"}
    # did something else spend the box our transaction needed? then ours can never confirm
    b = explorer(cfg, "boxes/" + watch)
    if b and b.get("spentTransactionId") and b["spentTransactionId"] != tx_id:
        return {"state": "failed"}
    return {"state": "unknown"}


# ------------------------------------------------------------------ main ----

def cmd_health(req):
    """Deploy gate: proves python + ergo-lib-python load under Apache/SELinux."""
    import ergo_lib_python
    res = {"python": "%d.%d.%d" % sys.version_info[:3], "ergo_lib": ergo_lib_python.__version__}
    if req.get("network"):
        info = explorer(req["cfg"], "info") or {}
        res["height"] = int(info.get("height", 0))
    return res


COMMANDS = {"health": cmd_health, "pools": cmd_pools, "quote": cmd_quote, "balance": cmd_balance,
            "build": cmd_build, "orders": cmd_orders, "refund": cmd_refund, "status": cmd_status}


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
