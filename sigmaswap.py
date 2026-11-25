#!/usr/bin/env python3
"""SigmaSwap chain helper.

Called by api.pl (never directly by a browser/wallet):

    venv/bin/python sigmaswap.py <pools|quote|build>

One JSON request on stdin, one JSON response on stdout:
    {"ok": true, ...}  or  {"ok": false, "error": "message safe to show the user"}

Details of unexpected failures go to stderr (-> the web server error log) only.

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
    """GET a JSON document. None for 404. One retry on a network hiccup (never for the
    short, optional mempool lookups, which pass their own small timeout)."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sigmaswap/1"})
    attempts = 2 if timeout >= HTTP_TIMEOUT else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code < 500 or attempt == attempts - 1:
                raise
        except (urllib.error.URLError, OSError):       # timeouts, resets, DNS
            if attempt == attempts - 1:
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
            if x["id"] == ERG_ID:      # ERG locked in the pool, shown next to the token name
                entry["erg"] = int(x["amount"])
            entry["xa"], entry["ya"] = str(int(x["amount"])), str(int(y["amount"]))   # reserves, for prices
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
    """Confirmed balance of one token, or of several at once (`tokens`: one explorer call)."""
    req_address(req.get("address"))
    wanted = req.get("tokens")
    if isinstance(wanted, list):
        ids = [req_token_id(t, "token") for t in wanted[:20]]
        bal = explorer(req["cfg"], "addresses/%s/balance/confirmed" % urllib.parse.quote(req["address"], safe="")) or {}
        have = {t.get("tokenId"): int(t.get("amount", 0)) for t in bal.get("tokens") or []}
        have[ERG_ID] = int(bal.get("nanoErgs", 0))
        return {"balances": {i: str(have.get(i, 0)) for i in ids}}
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
            raise SwapError("Insufficient ERG on this address for this transaction plus the network fee.")
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
        raise SwapError("Insufficient ERG on this address for this transaction plus the network fee.")
    outs = []
    for i, chunk in enumerate(chunks):
        outs.append(ErgoBoxCandidate(
            value=main_value if i == 0 else extra_value, script=address.ergo_tree(),
            creation_height=height, tokens=[Token(TokenId(t), a) for t, a in chunk]))
    return outs


def cmd_build(req):
    if req.get("mode") == "order":
        return build_order(req)
    if req.get("mode") == "hop":
        return build_hop(req)
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
        "impact_bps": impact_bps(amount, out, r_in, r_out, pool.fee_num),
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

MAX_ORDER_SLIPPAGE_BPS = 1000    # widest gap between an order's minimum and today's price (the UI's largest setting)
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
    # An order can be filled by anyone at any price down to its minimum, so the minimum
    # is the user's protection. It arrives in the signing link: never accept one far
    # below today's price, whatever the link says.
    if min_out < pool.quote(from_id, amount)[1] * (10000 - MAX_ORDER_SLIPPAGE_BPS) // 10000:
        raise SwapError("That minimum is too far below the current price (more than %d %%). "
                        "Preview the order again in SigmaSwap." % (MAX_ORDER_SLIPPAGE_BPS // 100))
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
    # A limit order's minimum may be above what the pool pays today. Then the check
    # runs against the pool as it will look when the price reaches the limit.
    current_out = pool.quote(from_id, amount)[1]
    limit = bool(req.get("limit")) and current_out < min_out
    check_pool = pool_at_price(pool, from_id, amount, min_out) if limit else pool
    out, ex_fee = simulate_execution(check_pool, order_box, terms, address, amount, min_out, op, ctx, height)
    parsed = parse_order(json.loads(order_box.json()), bytes(pk_constant(address)))
    if not parsed or not completing_tx(cfg, check_pool, parsed, ctx, height):
        raise SwapError("This order could not be prepared. Please preview again.")

    reduced = ReducedTransaction.from_unsigned_tx(unsigned, chosen, [], ctx)
    return {
        "reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"),
        "tx_id": hexid(unsigned.id),
        "address": addr_str, "mode": "order",
        "pool": pool.nft, "from": from_id, "to": terms["to"],
        "amount": str(amount), "out": str(out), "min_out": str(min_out),
        "ex_fee": str(ex_fee), "max_ex_fee": str(op["max_ex_fee"]), "miner_fee": str(fee),
        "inputs": len(chosen), "height": height,
        "watch_box": hexid(chosen[0].box_id), "order_box": hexid(order_box.box_id), "limit": limit,
    }


def hop_amount(out_erg, op):
    """ERG sold in the second swap of a route: the first swap's ERG minus the order's fee deposit."""
    return out_erg - op["extra"]


def build_hop(req):
    """Token A -> ERG -> token B with ONE signature.

    Two pools cannot share a transaction (each must be its first output), so the
    route is: a direct swap A -> ERG whose ERG does not return to the user but goes
    straight into an ordinary ErgoDex order "sell this ERG for B, at least min_out".
    A bot (ours, or anyone's) completes the order a little later; until then the
    user can cancel it and take the ERG. `min_out` is the minimum of B.
    """
    from ergo_lib_python.chain import ErgoBox, ErgoBoxCandidate, NetworkPrefix
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection, Wallet

    cfg = req["cfg"]
    pool1_id = req_token_id(req.get("pool"), "pool")
    pool2_id = req_token_id(req.get("hop_pool"), "pool")
    from_id = req_token_id(req.get("from"), "token")
    amount = req_amount(req.get("amount"), "amount")
    min_out = req_amount(req.get("min_out"), "minimum output")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    fee = miner_fee(cfg)
    op = order_params(cfg)
    if pool1_id == pool2_id or from_id == ERG_ID:
        raise SwapError("Invalid route.")

    pool1 = load_pool(cfg, pool1_id)
    pool2 = load_pool(cfg, pool2_id)
    if pool1.kind != "n2t" or pool2.kind != "n2t":
        raise SwapError("This route is not available.")
    mid_id, out1, r_in, r_out = pool1.quote(from_id, amount)
    if mid_id != ERG_ID:
        raise SwapError("This route is not available.")
    if out1 >= r_out:
        raise SwapError("Not enough liquidity in this pool.")
    amount2 = hop_amount(out1, op)
    if amount2 < 10 * op["max_ex_fee"]:
        raise SwapError("This amount is too small for a route through ERG: the fees would eat it.")
    to_id, out2, r_in2, r_out2 = pool2.quote(ERG_ID, amount2)
    if out2 < min_out:
        raise SwapError("The price moved beyond your slippage tolerance. Please preview the swap again.")
    if out2 >= r_out2:
        raise SwapError("Not enough liquidity in the second pool.")
    # the order half can be filled by anyone down to its minimum: never accept one far below today's price
    if min_out < out2 * (10000 - MAX_ORDER_SLIPPAGE_BPS) // 10000:
        raise SwapError("That minimum is too far below the current price (more than %d %%). "
                        "Preview the swap again in SigmaSwap." % (MAX_ORDER_SLIPPAGE_BPS // 100))
    terms = order_terms(pool2, ERG_ID, amount2, min_out, op)
    if terms["box_value"] != out1 or terms["to"] == from_id:
        raise SwapError("This route is not available.")

    def need_erg(toks):
        n = len(set(toks) - ({from_id} if toks.get(from_id) == amount else set()))
        return fee + change_need(n)

    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    chosen, user_erg, user_toks = select_inputs(user_boxes, need_erg, from_id, amount)

    ctx, height = chain_context(cfg)
    pool_out = new_pool_candidate(pool1, from_id, amount, out1, height)
    order_out = ErgoBoxCandidate(
        value=out1, script=order_tree(terms, pool2, pk_constant(address), amount2, min_out, op),
        creation_height=height)
    out_toks = dict(user_toks)
    out_toks[from_id] -= amount
    user_outs = user_outputs(address, height, out_toks, user_erg - fee)

    inputs = [pool1.box] + chosen
    unsigned = TxBuilder(BoxSelection(inputs, []), [pool_out, order_out] + user_outs, height, fee, address).build()
    try:
        Wallet([]).sign_tx_input(unsigned, 0, inputs, [], ctx)       # the first pool accepts this transaction
    except Exception as e:
        sys.stderr.write("sigmaswap.py: pool %s rejected the swap: %s\n" % (pool1.nft, str(e)[:300]))
        raise SwapError("The pool did not accept this swap. Please preview it again.")

    # the order box exactly as it will exist, and proof that it can be completed and belongs to the user
    order_box = ErgoBox.from_box_candidate(order_out, unsigned.id, 1)
    out, ex_fee = simulate_execution(pool2, order_box, terms, address, amount2, min_out, op, ctx, height)
    parsed = parse_order(json.loads(order_box.json()), bytes(pk_constant(address)))
    if not parsed or parsed.get("amount") != str(amount2) or parsed.get("min_out") != str(min_out) \
            or not completing_tx(cfg, pool2, parsed, ctx, height):
        raise SwapError("This swap could not be prepared. Please preview again.")

    reduced = ReducedTransaction.from_unsigned_tx(unsigned, inputs, [], ctx)
    return {
        "reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"),
        "tx_id": hexid(unsigned.id),
        "address": addr_str, "mode": "order", "hop": True,
        "pool": pool1.nft, "hop_pool": pool2.nft, "from": from_id, "to": to_id,
        "amount": str(amount), "mid": str(out1), "out": str(out), "min_out": str(min_out),
        "ex_fee": str(ex_fee), "max_ex_fee": str(op["max_ex_fee"]), "miner_fee": str(fee),
        "impact_bps": impact_bps(amount, out1, r_in, r_out, pool1.fee_num)
                      + impact_bps(amount2, out2, r_in2, r_out2, pool2.fee_num),
        "inputs": len(inputs), "height": height,
        "watch_box": hexid(pool1.box.box_id), "order_box": hexid(order_box.box_id), "limit": False,
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
            from ergo_lib_python.chain import Address, NetworkPrefix
            owner = Address.recreate_from_ergo_tree(
                ErgoTree.from_bytes(b"\x00" + bytes(consts[ix["pk"]]))).to_str(NetworkPrefix.Mainnet)
            return {"box": box, "box_id": hexid(box.box_id), "kind": kind, "owner": owner,
                    "max_miner_fee": str(consts[ix["max_miner_fee"]].value),
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


# ------------------------------------------------------------- liquidity ----
# SigmaLP (sigmalp/): add and remove liquidity, directly against the pool box.
# The pool contract's own rules (v1 Pool.sc):
#   add    : LP tokens out <= min(dX * supply / X, dY * supply / Y)
#   remove : X out <= LP in * X / supply   and   Y out <= LP in * Y / supply
# where supply = LP_EMISSION - (LP tokens still held by the pool).
# As with swaps, the pool contract is evaluated against the exact transaction
# before anything is handed to a wallet.

LP_EMISSION = 0x7fffffffffffffff


def pool_state(pool):
    """-> (x_id, X, y_id, Y, lp_id, supply) for either pool kind."""
    lp_id, lp_reserve = pool.tokens[1]
    return pool.x_id, pool.x_amt, pool.y_id, pool.y_amt, lp_id, LP_EMISSION - lp_reserve


def pool_asset_names(pool):
    """Token names/decimals as the explorer reports them (for pools not in the swap list)."""
    names = {ERG_ID: {"ticker": "ERG", "decimals": 9}}
    for a in pool.json.get("assets") or []:
        name = a.get("name")
        name = "".join(ch for ch in name if ch.isprintable())[:24].strip() if isinstance(name, str) else ""
        dec = a.get("decimals")
        names[a.get("tokenId")] = {"ticker": name or (str(a.get("tokenId"))[:6] + "..."),
                                   "decimals": dec if isinstance(dec, int) and 0 <= dec <= 18 else 0}
    return names


def add_amounts(pool, amount, side="x"):
    """Depositing `amount` of X (of Y when side is "y"): the matching other side, rounded up so the
    given side is the binding one and is taken exactly, and the LP received. -> (dx, dy, lp)"""
    _x, X, _y, Y, _lp, supply = pool_state(pool)
    if supply <= 0:
        raise SwapError("This pool has no liquidity yet.")
    if side == "y":
        dy = amount
        dx = -(-dy * X // Y)
    else:
        dx = amount
        dy = -(-dx * Y // X)
    lp = min(dx * supply // X, dy * supply // Y)
    if dx <= 0 or dy <= 0 or lp <= 0:
        raise SwapError("That amount is too small for this pool.")
    return dx, dy, lp


def remove_amounts(pool, lp):
    _x, X, _y, Y, _lp, supply = pool_state(pool)
    if lp > supply:
        raise SwapError("That is more than this pool has issued.")
    return lp * X // supply, lp * Y // supply


def lp_pool_candidate(pool, dx, dy, dlp_user, height):
    """Pool box after the user adds (dx,dy > 0, dlp_user > 0) or removes (dx,dy < 0, dlp_user < 0)."""
    from ergo_lib_python.chain import ErgoBoxCandidate, Token, TokenId
    tokens = list(pool.tokens)
    value = pool.box.value
    tokens[1] = (tokens[1][0], tokens[1][1] - dlp_user)
    if pool.kind == "n2t":
        value += dx
        tokens[2] = (tokens[2][0], tokens[2][1] + dy)
    else:
        tokens[2] = (tokens[2][0], tokens[2][1] + dx)
        tokens[3] = (tokens[3][0], tokens[3][1] + dy)
    return ErgoBoxCandidate(value=value, script=pool.box.ergo_tree, creation_height=height,
                            tokens=[Token(TokenId(t), a) for t, a in tokens], registers=pool.registers)


def select_inputs_multi(boxes, need_erg, need_tokens):
    """Like select_inputs, for several tokens at once. need_tokens: {token id: amount}."""
    def tok(b):
        return dict((hexid(t.token_id), t.amount) for t in b.tokens)

    remaining = list(boxes)
    chosen, have = [], {}
    for tid, amt in need_tokens.items():
        for b in sorted((b for b in remaining if tid in tok(b)), key=lambda b: -tok(b)[tid]):
            if have.get(tid, 0) >= amt:
                break
            chosen.append(b)
            remaining = [r for r in remaining if hexid(r.box_id) != hexid(b.box_id)]
            for k, v in tok(b).items():
                have[k] = have.get(k, 0) + v
        if have.get(tid, 0) < amt:
            raise SwapError("Insufficient token balance on this address.")

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
            raise SwapError("Insufficient ERG on this address for this transaction plus the network fee.")
        if len(chosen) >= MAX_USER_INPUTS:
            raise SwapError("This address holds too many small boxes. Consolidate the wallet "
                            "(send your funds to yourself) and try again.")
        chosen.append(remaining.pop(0))


def _lp_info(pool, names=None):
    x_id, X, y_id, Y, lp_id, supply = pool_state(pool)
    names = names or pool_asset_names(pool)
    return {"pool": pool.nft, "kind": pool.kind, "x": x_id, "y": y_id, "lp": lp_id,
            "x_reserve": str(X), "y_reserve": str(Y), "supply": str(supply), "fee_num": pool.fee_num,
            "x_info": names.get(x_id), "y_info": names.get(y_id)}


def cmd_lpquote(req):
    """Price an add (amount of X in, or of Y with side "y") or a remove (amount of LP in)."""
    cfg = req["cfg"]
    pool = load_pool(cfg, req_token_id(req.get("pool"), "pool"))
    res = _lp_info(pool)
    amount = req_amount(req.get("amount"), "amount")
    if req.get("action") == "remove":
        x_out, y_out = remove_amounts(pool, amount)
        res.update({"lp_in": str(amount), "x_out": str(x_out), "y_out": str(y_out)})
    else:
        dx, dy, lp = add_amounts(pool, amount, "y" if req.get("side") == "y" else "x")
        supply = int(res["supply"])
        res.update({"x_in": str(dx), "y_in": str(dy), "lp_out": str(lp),
                    "share_bps": lp * 10000 // (supply + lp)})
        if req.get("address"):
            req_address(req["address"])
            res["x_balance"] = _balance(cfg, req["address"], pool.x_id)
            res["y_balance"] = _balance(cfg, req["address"], pool.y_id)
    return res


def cmd_lpbuild(req):
    from ergo_lib_python.chain import NetworkPrefix
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection, Wallet

    cfg = req["cfg"]
    action = req.get("action")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    amount = req_amount(req.get("amount"), "amount")
    fee = miner_fee(cfg)
    pool = load_pool(cfg, req_token_id(req.get("pool"), "pool"))
    x_id, X, y_id, Y, lp_id, supply = pool_state(pool)

    if action == "add":
        side = "y" if req.get("side") == "y" else "x"
        dx, dy, lp = add_amounts(pool, amount, side)
        # the price may have moved since the preview: never take more of the other token, or give
        # fewer LP, than agreed
        other, limit = (dx, "max_x") if side == "y" else (dy, "max_y")
        if other > req_amount(req.get(limit), "limit") or lp < req_amount(req.get("min_lp"), "limit"):
            raise SwapError("The pool price moved. Please preview again.")
        dlp = lp
        need = {y_id: dy}
        if x_id != ERG_ID:
            need[x_id] = dx
        erg_delta = -(dx if x_id == ERG_ID else 0)
        token_delta = {lp_id: lp, y_id: -dy}
        if x_id != ERG_ID:
            token_delta[x_id] = -dx
        first = lp_id
    elif action == "remove":
        x_out, y_out = remove_amounts(pool, amount)
        if x_out < req_amount(req.get("min_x"), "limit") or y_out < req_amount(req.get("min_y"), "limit"):
            raise SwapError("The pool price moved. Please preview again.")
        if x_out <= 0 or y_out <= 0:
            raise SwapError("That amount is too small to remove.")
        if pool.kind == "n2t" and pool.box.value - x_out <= 10000000:
            raise SwapError("The pool keeps a small minimum of ERG; remove slightly less.")
        dx, dy, dlp = -x_out, -y_out, -amount
        need = {lp_id: amount}
        erg_delta = x_out if x_id == ERG_ID else 0
        token_delta = {lp_id: -amount, y_id: y_out}
        if x_id != ERG_ID:
            token_delta[x_id] = x_out
        first = y_id
    else:
        raise SwapError("Invalid request.")

    def need_erg(toks):
        n = len(set(toks) | set(token_delta))
        return fee - erg_delta + change_need(n)

    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    chosen, user_erg, user_toks = select_inputs_multi(user_boxes, need_erg, need)

    ctx, height = chain_context(cfg)
    pool_out = lp_pool_candidate(pool, dx, dy, dlp, height)
    out_toks = dict(user_toks)
    for tid, d in token_delta.items():
        out_toks[tid] = out_toks.get(tid, 0) + d
        if out_toks[tid] < 0:
            raise SwapError("Insufficient token balance on this address.")
    user_outs = user_outputs(address, height, out_toks, user_erg + erg_delta - fee, first)

    inputs = [pool.box] + chosen
    unsigned = TxBuilder(BoxSelection(inputs, []), [pool_out] + user_outs, height, fee, address).build()
    try:
        Wallet([]).sign_tx_input(unsigned, 0, inputs, [], ctx)      # the pool contract accepts this exact change
    except Exception as e:
        sys.stderr.write("sigmaswap.py: pool %s rejected the liquidity change: %s\n" % (pool.nft, str(e)[:300]))
        raise SwapError("The pool did not accept this. Please preview again.")
    reduced = ReducedTransaction.from_unsigned_tx(unsigned, inputs, [], ctx)
    res = _lp_info(pool)
    res.update({
        "reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"),
        "tx_id": hexid(unsigned.id), "address": addr_str, "mode": "lp", "action": action,
        "watch_box": hexid(pool.box.box_id), "miner_fee": str(fee),
        "x_amount": str(abs(dx)), "y_amount": str(abs(dy)), "lp_amount": str(abs(dlp)),
    })
    return res


def cmd_lpmap(req):
    """{LP token id: pool id} for every ErgoDex pool (the pool boxes all sit at two addresses)."""
    from ergo_lib_python.chain import Address, NetworkPrefix
    from ergo_lib_python.ergo_tree import ErgoTree
    cfg = req["cfg"]
    mapping = {}
    for tree_hex, n_tokens in ((N2T_POOL_TREE, 3), (T2T_POOL_TREE, 4)):
        addr = Address.recreate_from_ergo_tree(ErgoTree.from_bytes(bytes.fromhex(tree_hex))).to_str(NetworkPrefix.Mainnet)
        offset = 0
        while offset < 3000:
            page = explorer(cfg, "boxes/unspent/byAddress/%s?limit=100&offset=%d"
                            % (urllib.parse.quote(addr, safe=""), offset)) or {}
            items = page.get("items") or []
            for b in items:
                assets = b.get("assets") or []
                if len(assets) == n_tokens and int(assets[0].get("amount", 0)) == 1 and b.get("ergoTree") == tree_hex:
                    mapping[assets[1]["tokenId"]] = assets[0]["tokenId"]
            offset += 100
            if len(items) < 100:
                break
    if not mapping:
        raise SwapError("Pool data is unavailable right now.")
    return {"map": mapping}


def cmd_lppositions(req):
    """The address's liquidity positions: LP tokens it holds, and what they are worth right now."""
    cfg = req["cfg"]
    req_address(req.get("address"))
    lpmap = req.get("map") or {}
    bal = explorer(cfg, "addresses/%s/balance/confirmed" % urllib.parse.quote(req["address"], safe="")) or {}
    positions = []
    for t in bal.get("tokens") or []:
        pool_id = lpmap.get(t.get("tokenId"))
        if not pool_id or not is_hex64(pool_id) or len(positions) >= 12:
            continue
        try:
            pool = load_pool(cfg, pool_id)
            lp = int(t.get("amount", 0))
            if hexid_safe(pool.tokens[1][0]) != t.get("tokenId") or lp <= 0:
                continue
            x_out, y_out = remove_amounts(pool, lp)
            info = _lp_info(pool)
            info.update({"lp_amount": str(lp), "x_out": str(x_out), "y_out": str(y_out),
                         "share_bps": lp * 10000 // max(1, int(info["supply"]))})
            positions.append(info)
        except SwapError:
            continue
    return {"positions": positions}


def hexid_safe(v):
    return v if isinstance(v, str) else hexid(v)


# ---------------------------------------------------------------- bridge ----
# SigmaBridge (sigmabridge/): send ERG or a token from Ergo to another chain over
# the Rosen bridge. From Ergo that is ONE transaction: a "lock" box at Rosen's lock
# address holding the amount, with register R4 = [target chain, target address,
# network fee, bridge fee, sender address] (same as the Rosen web app builds it).
# Rosen's watchers and guards do the rest on the other chain.
#
# A lock box with a bad target address or too small a fee is NOT refunded
# automatically, so everything here is checked hard:
#   - fees are read from Rosen's own on-chain fee configuration at build time
#   - the target address must pass that chain's checksum
#   - the lock address must show recent bridge activity, or we refuse (it changes
#     when Rosen upgrades its contracts)

ROSEN_LOCK_ADDRESS = ("nB3L2PD3J4rMmyGk7nnNdESpPXxhPRQ4t1chF8LTXtceMQjKCEgL2pFjPY6cehGjyEFZyHEomBTFXZyqfonvxDozr"
                      "TtK5JzatD8SdmcPeJNWPvdRb5UxEMXE4WQtpAFzt2veT8Z6bmoWN")
ROSEN_MINFEE_NFT = "e2ed4d64393222db666f20e67803e9e6fbe6d64531e14ff52ddd95615b0cbf17"
ROSEN_LOCK_BOX_VALUE = 2000000          # nanoERG carried by a token lock box (what the Rosen app uses)
ROSEN_FEE_RATIO_DIVISOR = 10000
ROSEN_MAX_IDLE_DAYS = 10
BRIDGE_CHAINS = ("cardano", "ethereum", "binance", "bitcoin-runes", "bitcoin", "doge", "firo")   # every target is checksum-verified

_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_decode(addr, const=1):
    """-> (hrp, data5 without checksum) for a valid bech32 string, else None.
    const=1 is bech32; const=0x2bc830a3 is bech32m (Taproot addresses)."""
    if addr != addr.lower() or "1" not in addr:
        return None
    pos = addr.rfind("1")
    hrp, rest = addr[:pos], addr[pos + 1:]
    if not hrp or len(rest) < 7 or any(c not in _BECH32 for c in rest):
        return None
    data = [_BECH32.find(c) for c in rest]
    chk = 1
    for v in [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + data:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i, g in enumerate((0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)):
            if (top >> i) & 1:
                chk ^= g
    return (hrp, data[:-6]) if chk == const else None


def _base58check(addr):
    """-> (version byte, payload) for a valid Base58Check string, else None."""
    import hashlib
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for c in addr:
        if c not in alphabet:
            return None
        n = n * 58 + alphabet.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\x00" * (len(addr) - len(addr.lstrip("1"))) + raw
    if len(raw) < 5 or hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] != raw[-4:]:
        return None
    return raw[0], raw[1:-4]


def _convert_bits(data, frm, to):
    acc = bits = 0
    out = []
    for v in data:
        acc = (acc << frm) | v
        bits += frm
        while bits >= to:
            bits -= to
            out.append((acc >> bits) & ((1 << to) - 1))
    if bits >= frm or ((acc << (to - bits)) & ((1 << to) - 1)):
        return None
    return out


def _keccak256(data):
    """Keccak-256 (Ethereum's hash; NOT the same as hashlib.sha3_256)."""
    rc = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
          0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
          0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
          0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
          0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
    rot = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
    mask = (1 << 64) - 1
    rol = lambda x, n: ((x << n) | (x >> (64 - n))) & mask if n else x
    rate = 136
    msg = bytearray(data) + b"\x01" + b"\x00" * ((-len(data) - 2) % rate) + b"\x80"
    if (len(data) + 1) % rate == 0:
        msg = bytearray(data) + b"\x81"
    st = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        for i in range(rate // 8):
            st[i % 5][i // 5] ^= int.from_bytes(msg[off + 8 * i: off + 8 * i + 8], "little")
        for rnd in range(24):
            c = [st[x][0] ^ st[x][1] ^ st[x][2] ^ st[x][3] ^ st[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ rol(c[(x + 1) % 5], 1) for x in range(5)]
            st = [[st[x][y] ^ d[x] for y in range(5)] for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = rol(st[x][y], rot[x][y])
            st = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            st[0][0] ^= rc[rnd]
    return b"".join(st[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def bridge_target_address(chain, raw):
    """Validate a target-chain address. -> the address exactly as it goes into the lock box."""
    a = raw.strip() if isinstance(raw, str) else ""
    bad = SwapError("That is not a valid %s address. Check it carefully: a wrong address cannot be corrected."
                    % {"cardano": "Cardano", "ethereum": "Ethereum", "binance": "BNB Chain", "bitcoin": "Bitcoin",
                       "bitcoin-runes": "Bitcoin Runes (Taproot)", "doge": "Dogecoin", "firo": "Firo"}.get(chain, chain))
    if not a or len(a) > 120 or not a.isascii() or not a.isalnum():
        raise bad
    if chain == "cardano":
        dec = _bech32_decode(a)
        if not dec or dec[0] != "addr":
            raise bad
        payload = _convert_bits(dec[1], 5, 8)
        # header: high nibble = address type (0-7: payment address), low nibble = network (1 = mainnet)
        if not payload or (payload[0] & 0x0f) != 1 or (payload[0] >> 4) > 7 or len(payload) not in (29, 57):
            raise bad
        return a
    if chain == "bitcoin":
        dec = _bech32_decode(a)
        if not dec or dec[0] != "bc" or not dec[1] or dec[1][0] != 0:
            raise SwapError("Use a Bitcoin address that starts with bc1q (native SegWit). Other formats are not supported by the bridge.")
        prog = _convert_bits(dec[1][1:], 5, 8)
        if not prog or len(prog) not in (20, 32):
            raise SwapError("Use a Bitcoin address that starts with bc1q (native SegWit). Other formats are not supported by the bridge.")
        return a
    if chain == "bitcoin-runes":                 # Rosen accepts Taproot only: bc1p..., bech32m, 32-byte key
        dec = _bech32_decode(a, 0x2bc830a3)
        prog = _convert_bits(dec[1][1:], 5, 8) if dec and dec[1] else None
        if not dec or dec[0] != "bc" or dec[1][0] != 1 or not prog or len(prog) != 32:
            raise SwapError("Use a Taproot address that starts with bc1p. Runes can only be received at Taproot addresses.")
        return a
    if chain in ("doge", "firo"):
        dec = _base58check(a)
        versions = (0x1e, 0x16) if chain == "doge" else (0x52,)      # Doge P2PKH / P2SH; Firo P2PKH
        if not dec or dec[0] not in versions or len(dec[1]) != 20:
            raise bad
        return a
    if chain in ("ethereum", "binance"):
        if len(a) != 42 or not a.startswith("0x") or any(c not in "0123456789abcdefABCDEF" for c in a[2:]):
            raise bad
        body = a[2:]
        if body != body.lower() and body != body.upper():          # mixed case carries a checksum (EIP-55)
            digest = _keccak256(body.lower().encode("ascii")).hex()
            expect = "".join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(body.lower()))
            if expect != body:
                raise bad
        if int(body, 16) == 0:
            raise bad
        return "0x" + body.lower()
    raise SwapError("That target chain is not supported.")


def _plain(c):
    from ergo_lib_python.chain import Constant
    v = c.value if isinstance(c, Constant) else c
    return [_plain(x) for x in v] if isinstance(v, (list, tuple)) else v


def rosen_fee_config(cfg, token):
    """Rosen's on-chain fee configuration for one token (token = 'erg' or a token id)."""
    from ergo_lib_python.chain import Constant
    nft = cfg.get("rosen_minfee_nft") or ROSEN_MINFEE_NFT
    boxes, offset = [], 0
    while offset < 1000:
        page = explorer(cfg, "boxes/unspent/byTokenId/%s?limit=100&offset=%d" % (nft, offset)) or {}
        items = page.get("items") or []
        boxes += items
        offset += 100
        if len(items) < 100:
            break
    def is_for(b):
        ids = [a.get("tokenId") for a in b.get("assets") or []]
        if nft not in ids or len(b.get("additionalRegisters") or {}) < 6:
            return False
        return ids == [nft] if token == "erg" else (len(ids) == 2 and token in ids)
    mine = [b for b in boxes if is_for(b)]
    if len(mine) != 1:                       # none, or ambiguous: never guess a fee
        raise SwapError("The bridge does not list fees for this token right now.")
    reg = lambda k: _plain(Constant.from_bytes(bytes.fromhex(mine[0]["additionalRegisters"][k]["serializedValue"])))
    chains = [bytes(x).decode("utf-8") for x in reg("R4")]
    heights, bridge, network, ratio = reg("R5"), reg("R6"), reg("R7"), reg("R9")
    n = len(heights)
    if "ergo" not in chains or not n or not all(len(x) == n for x in (bridge, network, ratio)):
        raise SwapError("The bridge's fee data could not be read. Please try again later.")
    return {"chains": chains, "heights": heights, "bridge": bridge, "network": network, "ratio": ratio}


def rosen_fees(conf, to_chain, height, amount):
    """-> (bridge fee, network fee) for sending `amount` now. If a newer fee table is
    about to take over, the HIGHER of the two is used, so the transfer stays valid."""
    if to_chain not in conf["chains"]:
        raise SwapError("The bridge does not send this token to that chain.")
    ei, ci = conf["chains"].index("ergo"), conf["chains"].index(to_chain)
    current = None
    for i in range(len(conf["heights"]) - 1, -1, -1):
        h = int(conf["heights"][i][ei])
        if h != -1 and h < height:
            current = i
            break
    if current is None:
        raise SwapError("The bridge does not list fees for this token right now.")
    best = None
    for i in range(current, len(conf["heights"])):
        if int(conf["heights"][i][ei]) == -1 or int(conf["heights"][i][ci]) == -1 or int(conf["bridge"][i][ci]) == -1:
            if i == current:
                raise SwapError("The bridge does not send this token to that chain right now.")
            continue
        base, net, ratio = int(conf["bridge"][i][ci]), int(conf["network"][i][ci]), int(conf["ratio"][i][ci])
        fee = (max(base, amount * ratio // ROSEN_FEE_RATIO_DIVISOR), net)
        best = fee if best is None else (max(best[0], fee[0]), max(best[1], fee[1]))
    return best


def rosen_lock_address(cfg):
    """The lock address, only if it shows recent bridge activity."""
    import time
    from ergo_lib_python.chain import Address, NetworkPrefix
    lock = cfg.get("rosen_lock_address") or ROSEN_LOCK_ADDRESS
    address = Address(lock, NetworkPrefix.Mainnet)
    txs = explorer(cfg, "addresses/%s/transactions?limit=1" % urllib.parse.quote(lock, safe="")) or {}
    items = txs.get("items") or []
    if not items or time.time() - int(items[0].get("timestamp", 0)) / 1000 > ROSEN_MAX_IDLE_DAYS * 86400:
        sys.stderr.write("sigmaswap.py: Rosen lock address shows no recent activity - bridging refused\n")
        raise SwapError("The bridge is unavailable right now. Please try again later.")
    return address, lock


def _bridge_quote(cfg, token, chain, amount, height):
    if chain not in BRIDGE_CHAINS:
        raise SwapError("That target chain is not supported.")
    conf = rosen_fee_config(cfg, token)
    bridge_fee, network_fee = rosen_fees(conf, chain, height, amount)
    return {"bridge_fee": str(bridge_fee), "network_fee": str(network_fee),
            "min_transfer": str(bridge_fee + network_fee + 1),
            "receive": str(max(0, amount - bridge_fee - network_fee)),
            "chains": [c for c in conf["chains"] if c in BRIDGE_CHAINS]}


def cmd_bridgequote(req):
    cfg = req["cfg"]
    token = "erg" if req.get("token") in ("erg", ERG_ID) else req_token_id(req.get("token"), "token")
    amount = req_amount(req.get("amount"), "amount")
    rosen_lock_address(cfg)
    info = explorer(cfg, "info") or {}
    res = _bridge_quote(cfg, token, req.get("chain"), amount, int(info.get("height", 0)))
    if req.get("to") is not None:
        res["to"] = bridge_target_address(req.get("chain"), req.get("to"))
    if req.get("address"):
        req_address(req["address"])
        res["balance"] = _balance(cfg, req["address"], ERG_ID if token == "erg" else token)
    return res


def cmd_bridgebuild(req):
    from ergo_lib_python.chain import Constant, ErgoBoxCandidate, NetworkPrefix, NonMandatoryRegisterId, Token, TokenId
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection

    cfg = req["cfg"]
    token = "erg" if req.get("token") in ("erg", ERG_ID) else req_token_id(req.get("token"), "token")
    chain = req.get("chain")
    amount = req_amount(req.get("amount"), "amount")
    max_fees = req_amount(req.get("max_fees"), "fee limit")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    to_address = bridge_target_address(chain, req.get("to"))
    fee = miner_fee(cfg)

    lock_address, lock_str = rosen_lock_address(cfg)
    ctx, height = chain_context(cfg)
    q = _bridge_quote(cfg, token, chain, amount, height)
    bridge_fee, network_fee = int(q["bridge_fee"]), int(q["network_fee"])
    if bridge_fee + network_fee > max_fees:
        raise SwapError("The bridge fees changed. Please preview again.")
    if amount <= bridge_fee + network_fee:
        raise SwapError("The amount is not more than the bridge fees.")

    r4 = [to.encode("utf-8") for to in (chain, to_address, str(network_fee), str(bridge_fee), addr_str)]
    registers = {NonMandatoryRegisterId.R4: Constant([Constant(x) for x in r4])}
    if token == "erg":
        lock_out = ErgoBoxCandidate(value=amount, script=lock_address.ergo_tree(), creation_height=height, registers=registers)
        need_tokens, erg_out = {}, amount
    else:
        lock_out = ErgoBoxCandidate(value=ROSEN_LOCK_BOX_VALUE, script=lock_address.ergo_tree(), creation_height=height,
                                    tokens=[Token(TokenId(token), amount)], registers=registers)
        need_tokens, erg_out = {token: amount}, ROSEN_LOCK_BOX_VALUE

    def need_erg(toks):
        return erg_out + fee + change_need(len(toks))

    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    chosen, user_erg, user_toks = select_inputs_multi(user_boxes, need_erg, need_tokens)
    out_toks = dict(user_toks)
    if token != "erg":
        out_toks[token] -= amount
    change = user_outputs(address, height, out_toks, user_erg - erg_out - fee)
    unsigned = TxBuilder(BoxSelection(chosen, []), [lock_out] + change, height, fee, address).build()

    # read the lock box back out of the finished transaction and compare it with what was asked
    built = json.loads(unsigned.json())["outputs"][0]
    back = [bytes(x).decode("utf-8") for x in _plain(Constant.from_bytes(bytes.fromhex(built["additionalRegisters"]["R4"])))]
    if (back != [chain, to_address, str(network_fee), str(bridge_fee), addr_str]
            or built["ergoTree"] != bytes(lock_address.ergo_tree()).hex()):
        raise SwapError("The transfer could not be prepared. Please try again.")
    reduced = ReducedTransaction.from_unsigned_tx(unsigned, chosen, [], ctx)
    return {
        "reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"),
        "tx_id": hexid(unsigned.id), "address": addr_str, "mode": "bridge",
        "watch_box": hexid(chosen[0].box_id), "token": token, "chain": chain, "to": to_address,
        "amount": str(amount), "bridge_fee": str(bridge_fee), "network_fee": str(network_fee),
        "receive": str(amount - bridge_fee - network_fee), "miner_fee": str(fee),
    }


# ------------------------------------------------------------------- bot ----
# Orders placed through SigmaSwap are completed by SigmaSwap itself. Completing an
# order needs NO key: the order contract lets anyone spend the order box as long as
# the owner receives what the contract demands. A systemd timer runs
# `sigmaswap.py bot` every minute; it looks at the orders SigmaSwap registered,
# and when the pool can fill one at or above its minimum it builds the completing
# transaction and broadcasts it. Everything left in the order box (the unused
# execution-fee deposit) goes back to the owner; only the miner fee is spent.
# This is what makes limit orders work: ErgoDex's own bots rarely pick up an order
# that has been waiting.

def pool_at_price(pool, from_id, amount, target_out):
    """A copy of `pool` with its input reserve lowered until selling `amount` yields
    `target_out`. Used to check, before a limit order is created, that the order
    contract will accept execution once the price gets there."""
    from ergo_lib_python.chain import ErgoBox, ErgoBoxCandidate, Token, TokenId
    from ergo_lib_python.transaction import TxId
    _to, _out, r_in, r_out = pool.quote(from_id, amount)
    if target_out >= r_out:
        raise SwapError("That limit can never be reached with this pool's liquidity.")
    a_f = amount * pool.fee_num
    new_in = (r_out * a_f // target_out - a_f) // FEE_DENOM
    while new_in > 0 and (r_out * a_f) // (new_in * FEE_DENOM + a_f) < target_out:
        new_in -= max(1, new_in // 1000000)
    if new_in <= 0:
        raise SwapError("That limit can never be reached with this pool's liquidity.")
    tokens = list(pool.tokens)
    value = pool.box.value
    if from_id == ERG_ID:
        value = max(new_in, 10000000)
    else:
        tokens[2] = (tokens[2][0], new_in)
    cand = ErgoBoxCandidate(value=value, script=pool.box.ergo_tree, creation_height=pool.box.creation_height,
                            tokens=[Token(TokenId(t), a) for t, a in tokens], registers=pool.registers)
    synthetic = Pool(json.loads(ErgoBox.from_box_candidate(cand, TxId(b"\x00" * 32), 0).json()))
    if synthetic.quote(from_id, amount)[1] < target_out:
        raise SwapError("That limit can never be reached with this pool's liquidity.")
    return synthetic


def completing_tx(cfg, pool, order, ctx, height):
    """The keyless transaction that fills `order` (a parse_order dict) from `pool`, or
    None when the pool cannot fill it at its minimum right now.

    outputs: [pool after the swap, everything else to the owner, miner fee]
    """
    from ergo_lib_python.chain import Address, ErgoBoxCandidate, Token, TokenId
    from ergo_lib_python.transaction import Transaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection, Wallet
    box = order["box"]
    amount, min_out = int(order["amount"]), int(order["min_out"])
    to_id, out, _r_in, r_out = pool.quote(order["from"], amount)
    if out < min_out or out >= r_out:
        return None
    owner = Address(order["owner"])
    fee = min(miner_fee(cfg), int(order["max_miner_fee"]))
    if order["kind"] == "sell":
        reward = ErgoBoxCandidate(value=box.value - amount - fee, script=owner.ergo_tree(),
                                  creation_height=height, tokens=[Token(TokenId(to_id), out)])
    else:
        reward = ErgoBoxCandidate(value=box.value + out - fee, script=owner.ergo_tree(), creation_height=height)
    inputs = [pool.box, box]
    unsigned = TxBuilder(BoxSelection(inputs, []),
                         [new_pool_candidate(pool, order["from"], amount, out, height), reward],
                         height, fee, owner).build()
    # both inputs are scripts that must evaluate to true by themselves: an empty
    # wallet "signs" them, or raises if either contract rejects the transaction
    signed_inputs = [Wallet([]).sign_tx_input(unsigned, i, inputs, [], ctx) for i in (0, 1)]
    tx = Transaction(signed_inputs, [], unsigned.output_candidates)
    return {"tx": tx, "tx_id": hexid(tx.id), "out": out, "fee": fee}


def submit_tx(cfg, tx):
    body = tx.json().encode("utf-8")
    req = urllib.request.Request(cfg["explorer_url"].rstrip("/") + "/api/v1/mempool/transactions/submit",
                                 data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "sigmaswap/1"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def cmd_bot(req):
    """One pass over the registered orders (CACHE_DIR/orders/<order box id>.json)."""
    import os
    import time
    cfg = req["cfg"]
    directory = req.get("dir") or ""
    dry_run = bool(req.get("dry_run"))
    if not os.path.isdir(directory):
        return {"checked": 0, "filled": 0}
    now = time.time()
    pools, context = {}, None
    checked = filled = 0
    # Registering an order is cheap and can be done by anyone, so the registry is
    # treated as a hint only: the open order boxes are read from the chain in a few
    # calls, and only registered ids that really exist there are looked at.
    on_chain = {}
    for template_hash in (ORDER_SELL_TEMPLATE_HASH, ORDER_BUY_TEMPLATE_HASH):
        offset = 0
        while offset < 2000:
            page = explorer(cfg, "boxes/unspent/byErgoTreeTemplateHash/%s?limit=100&offset=%d"
                            % (template_hash, offset)) or {}
            items = page.get("items") or []
            for item in items:
                on_chain[item.get("boxId")] = item
            offset += 100
            if len(items) < 100:
                break
    for name in sorted(os.listdir(directory)):
        if not (name.endswith(".json") and is_hex64(name[:-5])):
            continue
        path = os.path.join(directory, name)
        box_id = name[:-5]
        try:
            b = on_chain.get(box_id)
            if b is None:                       # not signed yet, already filled, or cancelled
                if now - os.path.getmtime(path) > 3 * 3600:
                    os.remove(path)
                continue
            with open(path) as f:
                rec = json.load(f)
            if now - float(rec.get("submitted", 0)) < 300:        # a fill is on its way: give it time
                continue
            order = parse_order(b)
            if not order:
                os.remove(path)
                continue
            checked += 1
            if order["pool"] not in pools:
                pools[order["pool"]] = load_pool(cfg, order["pool"])
            if context is None:
                context = chain_context(cfg)
            done = completing_tx(cfg, pools[order["pool"]], order, context[0], context[1])
            if not done:
                continue
            if not dry_run:
                submit_tx(cfg, done["tx"])
                rec["submitted"] = now
                rec["fill_tx"] = done["tx_id"]
                with open(path, "w") as f:
                    json.dump(rec, f)
            del pools[order["pool"]]                               # that pool box is now spent
            filled += 1
            sys.stderr.write("sigmaswap.py bot: %s order %s with tx %s\n"
                             % ("would fill" if dry_run else "filled", box_id[:12], done["tx_id"]))
        except Exception as e:
            sys.stderr.write("sigmaswap.py bot: order %s skipped: %s: %s\n" % (box_id[:12], type(e).__name__, str(e)[:200]))
    return {"checked": checked, "filled": filled}


def read_env_file(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env[k.strip()] = v
    except OSError:
        pass
    return env


def bot_main():
    """Entry point for the systemd timer: `sigmaswap.py bot` (reads the app's env file itself)."""
    import os
    env = read_env_file(os.environ.get("APP_ENV_FILE", ""))
    cache = env.get("CACHE_DIR") or ""
    if not cache:
        return 0
    cfg = {
        "explorer_url": env.get("EXPLORER_URL") or "https://api.ergoplatform.com",
        "miner_fee": int(env.get("MINER_FEE") or 1100000),
        "order_min_ex_fee": int(env.get("ORDER_MIN_EX_FEE") or 6000000),
        "order_miner_fee": int(env.get("ORDER_MINER_FEE") or 2000000),
        "use_mempool": (env.get("USE_MEMPOOL", "1") or "1") != "0",
    }
    if (env.get("ORDER_BOT", "1") or "1") != "0":
        res = cmd_bot({"cfg": cfg, "dir": os.path.join(cache, "orders")})
        if res["filled"]:
            sys.stderr.write("sigmaswap.py bot: checked %d, filled %d\n" % (res["checked"], res["filled"]))
    if (env.get("PRICE_ALERTS", "1") or "1") != "0":
        try:
            cfg["pools_api_url"] = env.get("POOLS_API_URL") or "https://api.ergodex.io/v1/amm/pools/stats"
            cfg["min_tvl_usd"] = env.get("MIN_TVL_USD") or 100
            res = cmd_alerts({"cfg": cfg, "dir": os.path.join(cache, "alerts")})
            if res["hit"]:
                sys.stderr.write("sigmaswap.py bot: %d price alert(s) reached\n" % res["hit"])
        except Exception as e:
            sys.stderr.write("sigmaswap.py bot: price alerts skipped: %s: %s\n" % (type(e).__name__, str(e)[:200]))
    return 0


# ------------------------------------------------------------------ mint ----
# SigmaMint (sigmamint/): mint a fungible token or an NFT following EIP-4.
# A mint is an ordinary transaction from the user's own boxes; the new token's id
# is, by protocol rule, the id of the transaction's first input.
#   R4 name, R5 description, R6 decimals            (every token)
#   R7 media type, R8 SHA-256 of the file, R9 link   (NFTs)

NFT_TYPES = {"picture": bytes([1, 1]), "audio": bytes([1, 2]), "video": bytes([1, 3])}
MAX_NAME_BYTES = 120
MAX_DESC_BYTES = 600


def clean_text(v, what, max_bytes, required):
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise SwapError("Invalid %s." % what)
    # one normal form, no invisible characters: "Sig<zero-width space>USD" must not pass as "SigUSD"
    import unicodedata
    v = unicodedata.normalize("NFKC", v)
    v = "".join(ch for ch in v if (ch.isprintable() or ch == "\n") and unicodedata.category(ch) != "Cf").strip()
    if required and not v:
        raise SwapError("Please enter a %s." % what)
    if len(v.encode("utf-8")) > max_bytes:
        raise SwapError("The %s is too long." % what)
    return v


def req_cid(v):
    """An IPFS content id (CIDv0 'Qm...' or CIDv1 base32 'b...'), nothing else."""
    import re
    if isinstance(v, str):
        v = v.strip()
        for prefix in ("ipfs://", "ipfs/"):
            if v.startswith(prefix):
                v = v[len(prefix):]
        if re.match(r"^(Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{50,100})$", v):
            return v
    raise SwapError("That is not a valid IPFS link. It should look like ipfs://Qm... or ipfs://bafy...")


def sniff_media(data):
    """File type from the file's STRUCTURE -> (nft type, extension) or None. Allowlist only.

    A few magic bytes are easy to put in front of anything, so each format's
    container is walked: the file has to hold together as what it claims to be.
    """
    import struct
    import zlib
    n = len(data)
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):                       # chunks with valid CRCs, IHDR first, IEND last
            pos, first, last = 8, None, None
            while pos + 12 <= n:
                length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
                end = pos + 12 + length
                if end > n or zlib.crc32(data[pos + 4:pos + 8 + length]) != struct.unpack(">I", data[end - 4:end])[0]:
                    return None
                first = first or ctype
                last = ctype
                pos = end
                if ctype == b"IEND":
                    break
            return ("picture", "png") if first == b"IHDR" and last == b"IEND" and pos == n else None
        if data.startswith(b"\xff\xd8\xff"):                             # marker segments up to the image data, EOI at the end
            pos, saw_frame = 2, False
            while pos + 4 <= n and data[pos] == 0xFF:
                marker = data[pos + 1]
                if marker == 0xDA:                                       # start of scan: compressed data follows
                    # phone cameras often append extra data after the image, so only require an end marker to exist
                    return ("picture", "jpg") if saw_frame and b"\xff\xd9" in data[pos:] else None
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    saw_frame = True
                pos += 2 + struct.unpack(">H", data[pos + 2:pos + 4])[0]
            return None
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return ("picture", "gif") if w and h and data[-1:] == b"\x3b" else None
        if data[:4] == b"RIFF" and data[8:12] in (b"WEBP", b"WAVE"):     # the declared size is the real size
            if struct.unpack("<I", data[4:8])[0] + 8 not in (n, n - 1):
                return None
            if data[8:12] == b"WEBP":
                return ("picture", "webp") if data[12:16] in (b"VP8 ", b"VP8L", b"VP8X") else None
            return ("audio", "wav") if data[12:16] == b"fmt " else None
        if data[:4] == b"OggS":
            return ("audio", "ogg") if data[4] == 0 and n > 27 + data[26] and b"OggS" in data[27:65536] + data[-65536:] else None
        if data[4:8] == b"ftyp":                                         # top-level boxes tile the whole file
            pos, kinds = 0, []
            while pos + 8 <= n:
                size, kind = struct.unpack(">I4s", data[pos:pos + 8])
                if size == 1 and pos + 16 <= n:
                    size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
                elif size == 0:
                    size = n - pos
                if size < 8 or pos + size > n:
                    return None
                kinds.append(kind)
                pos += size
            return ("video", "mp4") if pos == n and b"moov" in kinds and b"mdat" in kinds else None
        if data[:4] == b"\x1a\x45\xdf\xa3":
            return ("video", "webm") if b"webm" in data[:64] and b"\x18\x53\x80\x67" in data[:4096] else None
        # MP3: optional ID3v2 tag, then at least four frames that follow one another exactly
        pos = 0
        if data[:3] == b"ID3" and n > 10:
            pos = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
        bitrates = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
        rates = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000)}
        frames = 0
        while pos + 4 <= n and frames < 4:
            b1, b2 = data[pos + 1], data[pos + 2]
            version, layer, br, sr = (b1 >> 3) & 3, (b1 >> 1) & 3, b2 >> 4, (b2 >> 2) & 3
            if data[pos] != 0xFF or (b1 & 0xE0) != 0xE0 or version not in rates or layer != 1 or br in (0, 15) or sr == 3:
                break
            kbps = bitrates[br] if version == 3 else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)[br]
            pos += (144 if version == 3 else 72) * kbps * 1000 // rates[version][sr] + ((b2 >> 1) & 1)
            frames += 1
        return ("audio", "mp3") if frames == 4 else None
    except (struct.error, IndexError):
        return None


def cmd_upload(req):
    """Relay one uploaded file to Lighthouse (IPFS/Filecoin) and return its content id.

    The file is read from the web server's temp file, checked, hashed and sent on.
    Nothing is kept on this server. The API key arrives on stdin and is only ever
    used as the Authorization header to the configured Lighthouse host.
    """
    import hashlib
    import os
    import uuid
    cfg = req["cfg"]
    key = req.get("key") or ""
    path = req.get("path") or ""
    max_bytes = int(req.get("max_bytes") or 0)
    if not key or not path or max_bytes <= 0:
        raise SwapError("Uploads are not available.")
    size = os.path.getsize(path)
    if size <= 0:
        raise SwapError("The file is empty.")
    if size > max_bytes:
        raise SwapError("The file is too large (limit %d MB)." % (max_bytes // (1024 * 1024)))
    with open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    kind = sniff_media(data)
    if not kind:
        raise SwapError("This file type is not supported. Use PNG, JPG, GIF, WEBP, MP3, WAV, OGG, MP4 or WEBM.")
    media, ext = kind
    digest = hashlib.sha256(data).hexdigest()

    boundary = "----sigmamint" + uuid.uuid4().hex
    filename = "sigmamint-%s.%s" % (digest[:16], ext)          # never the client's file name
    body = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n" % (boundary, filename)).encode("ascii")
            + data + ("\r\n--%s--\r\n" % boundary).encode("ascii"))
    request = urllib.request.Request(
        cfg["lighthouse_upload_url"], data=body, method="POST",
        headers={"Authorization": "Bearer " + key, "User-Agent": "sigmamint/1",
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):    # the key must never follow a redirect elsewhere
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.stderr.write("sigmaswap.py: Lighthouse upload refused: HTTP %d\n" % e.code)   # never log the key
        if e.code in (401, 403):
            raise SwapError("Uploads are not available right now.")
        raise SwapError("The storage service did not accept the file. Please try again.")
    cid = req_cid(res.get("Hash") if isinstance(res, dict) else None)
    return {"cid": cid, "sha256": digest, "size": size, "media": media, "ext": ext}


def cmd_mint(req):
    from ergo_lib_python.chain import (Constant, ErgoBoxCandidate, NetworkPrefix, NonMandatoryRegisterId,
                                       Token, TokenId)
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection
    R = NonMandatoryRegisterId

    cfg = req["cfg"]
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    fee = miner_fee(cfg)
    kind = req.get("kind")
    name = clean_text(req.get("name"), "name", MAX_NAME_BYTES, True).replace("\n", " ")
    desc = clean_text(req.get("description"), "description", MAX_DESC_BYTES, False)

    regs = {R.R4: Constant(name.encode("utf-8")), R.R5: Constant(desc.encode("utf-8"))}
    extra_bytes = len(name.encode("utf-8")) + len(desc.encode("utf-8"))
    if kind == "token":
        amount = req_amount(req.get("amount"), "amount")
        try:
            decimals = int(req.get("decimals"))
        except (TypeError, ValueError):
            raise SwapError("Invalid decimals.")
        if not (0 <= decimals <= 9):
            raise SwapError("Decimals must be between 0 and 9.")
        regs[R.R6] = Constant(str(decimals).encode("ascii"))
    elif kind == "nft":
        amount, decimals = 1, 0
        media = req.get("media")
        if media not in NFT_TYPES:
            raise SwapError("Invalid media type.")
        cid = req_cid(req.get("cid"))
        sha = req.get("sha256")
        if not is_hex64(sha):
            raise SwapError("Invalid file fingerprint.")
        link = "ipfs://" + cid
        regs[R.R6] = Constant(b"0")
        regs[R.R7] = Constant(NFT_TYPES[media])
        regs[R.R8] = Constant(bytes.fromhex(sha))
        regs[R.R9] = Constant(link.encode("ascii"))
        extra_bytes += 2 + 32 + len(link)
    else:
        raise SwapError("Invalid request.")

    # the box that carries the new token: sized generously for its registers
    mint_value = max(1000000, (220 + extra_bytes) * MIN_BOX_VALUE_PER_BYTE)

    def need_erg(toks):
        return mint_value + fee + change_need(len(toks))

    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    chosen, user_erg, user_toks = select_inputs(user_boxes, need_erg, None, 0)

    ctx, height = chain_context(cfg)
    token_id = hexid(chosen[0].box_id)             # protocol rule: new token id = first input's box id
    mint_out = ErgoBoxCandidate(value=mint_value, script=address.ergo_tree(), creation_height=height,
                                tokens=[Token(TokenId(token_id), amount)], registers=regs)
    change = user_outputs(address, height, dict(user_toks), user_erg - mint_value - fee)
    unsigned = TxBuilder(BoxSelection(chosen, []), [mint_out] + change, height, fee, address).build()
    if hexid(unsigned.inputs[0].box_id) != token_id:
        raise SwapError("The mint could not be prepared. Please try again.")
    reduced = ReducedTransaction.from_unsigned_tx(unsigned, chosen, [], ctx)
    return {
        "reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"),
        "tx_id": hexid(unsigned.id), "address": addr_str, "mode": "mint",
        "token_id": token_id, "watch_box": token_id, "kind": kind, "name": name,
        "amount": str(amount), "decimals": decimals, "miner_fee": str(fee), "box_value": str(mint_value),
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


# ---------------------------------------------------------------- Swapuz ----
# SigmaBridge's second way out of Ergo: Swapuz (swapuz.com), a custodial instant
# exchange. The user sends ERG to a deposit address Swapuz gives for one order, and
# Swapuz pays another coin to the address the user named. Unlike Rosen this is a
# company holding the funds in between, so everything it answers is checked before
# a transaction is built from it, above all the deposit address.

SWAPUZ_API = "https://api.swapuz.com/api"
SWAPUZ_MAX_RATE_DROP_BPS = 300      # refuse an order whose estimate is this far below the preview


def _sz_text(v, limit=80):
    return "".join(ch for ch in v if ch.isprintable())[:limit] if isinstance(v, str) else ""


def _sz_code(v):
    """A coin or network code as Swapuz writes it, or None."""
    import re
    return v if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._\-]{1,24}", v) else None


def swapuz_call(cfg, path, body=None, timeout=HTTP_TIMEOUT):
    """-> the `result` of a Swapuz API answer. Numbers are kept exact (Decimal).
    A refusal with a message from Swapuz (below the minimum, pair closed) is passed on."""
    import decimal
    headers = {"Accept": "application/json", "User-Agent": "sigmaswap/1.0"}
    key = cfg.get("swapuz_api_key") or ""
    if key:
        headers["Api-key"] = key
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(SWAPUZ_API + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        raw = e.read(64 * 1024)
    try:
        doc = json.loads(raw.decode("utf-8"), parse_float=decimal.Decimal)
    except ValueError:
        raise SwapError("Swapuz is not answering right now. Please try again.")
    if not isinstance(doc, dict) or doc.get("status") != 200 or doc.get("result") is None:
        msg = _sz_text(doc.get("message") if isinstance(doc, dict) else "", 200) \
            or _sz_text(doc.get("result") if isinstance(doc, dict) else "", 200)
        raise SwapError("Swapuz: " + msg if msg else "Swapuz could not do this right now. Please try again.")
    return doc["result"]


def _sz_dec(v):
    """A Swapuz number as an exact, plain decimal string ("0.001699"), or None."""
    import decimal
    try:
        d = decimal.Decimal(str(v))
    except (decimal.InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return format(d.normalize(), "f")


def _erg_text(nano):
    return ("%d.%09d" % (nano // 10 ** 9, nano % 10 ** 9)).rstrip("0").rstrip(".")


def cmd_szcoins(req):
    """Coins Swapuz can pay out, reduced to what the form needs. The names are theirs."""
    coins = swapuz_call(req["cfg"], "/home/v1/coins", timeout=60)
    out, erg_ok = [], False
    for c in coins if isinstance(coins, list) else []:
        try:
            code = _sz_code(c.get("shortName"))
            nets = []
            for n in c.get("network") or []:
                ncode = _sz_code(n.get("shortName"))
                if not ncode or not n.get("isActive"):
                    continue
                if code == "ERG" and ncode == "ERGO" and n.get("isDeposit"):
                    erg_ok = True
                if not n.get("isWithdraw"):
                    continue
                rx, mrx = n.get("regexAddress"), n.get("memoRegex")
                nets.append({"s": ncode, "n": _sz_text(n.get("fullName") or n.get("name")) or ncode,
                             "rx": rx if isinstance(rx, str) and 0 < len(rx) <= 300 else "",
                             "memo": bool(n.get("isMemo")),
                             "mrx": mrx if isinstance(mrx, str) and 0 < len(mrx) <= 200 else "",
                             "fee": _sz_dec(n.get("withdrawFee")) or "0", "def": bool(n.get("isDefault"))})
            if code and code != "ERG" and nets:
                rating = c.get("rating")
                out.append({"c": code, "n": _sz_text(c.get("name")) or code,
                            "r": rating if isinstance(rating, int) and rating > 0 else 10 ** 6, "nets": nets[:40]})
        except (AttributeError, TypeError):
            continue
    if not out:
        raise SwapError("Swapuz is not answering right now. Please try again.")
    out.sort(key=lambda c: (c["r"], c["c"]))
    return {"coins": out[:1500], "erg_ok": erg_ok}


def _sz_rate(cfg, to, net, amount):
    q = urllib.parse.urlencode({"from": "ERG", "to": to, "amount": _erg_text(amount), "fromNetwork": "ERGO",
                                "toNetwork": net, "mode": "float"})
    r = swapuz_call(cfg, "/home/v1/rate/?" + q)
    out = _sz_dec(r.get("result")) if isinstance(r, dict) else None
    if out is None or out == "0":
        raise SwapError("Swapuz has no rate for this right now.")
    return {"out": out, "withdraw_fee": _sz_dec(r.get("withdrawFee")) or "0",
            "min": _sz_dec(r.get("minAmount")) or "0", "max": _sz_dec(r.get("maxAmount")) or "0"}


def cmd_szquote(req):
    """What Swapuz would pay for `amount` nanoERG right now (floating rate, an estimate)."""
    cfg = req["cfg"]
    to, net = _sz_code(req.get("to")), _sz_code(req.get("net"))
    if not to or not net:
        raise SwapError("Choose the coin to receive.")
    res = _sz_rate(cfg, to, net, req_amount(req.get("amount"), "amount"))
    if req.get("address"):
        req_address(req["address"])
        res["balance"] = _balance(cfg, req["address"], ERG_ID)
    return res


def _sz_order_view(r):
    """The fields of a Swapuz order we rely on, or None when it is not shaped like one."""
    if not isinstance(r, dict):
        return None
    try:
        return {"uid": r.get("uid"), "from": (r.get("from") or {}).get("shortName"), "to": (r.get("to") or {}).get("shortName"),
                "from_net": r.get("addressFromNetwork"), "to_net": r.get("addressToNetwork"),
                "deposit": r.get("addressFrom"), "dest": r.get("addressTo"), "amount": _sz_dec(r.get("amount")),
                "out": _sz_dec(r.get("amountResult")), "status": r.get("status"), "expires": _sz_text(r.get("finishPayment"), 40),
                "refund": r.get("addressRefund"), "aml": bool(r.get("amlError")), "memo_from": r.get("memoFrom") or r.get("depositMemo"),
                "w_tx": _sz_text(r.get("wTxId"), 120), "d_tx": _sz_text(r.get("dTxId"), 120)}
    except AttributeError:
        return None


def cmd_szorder(req):
    """Open (or re-use) the Swapuz order for one previewed exchange and build the payment to it.

    Called when the wallet fetches the transaction. Nothing Swapuz answers is taken on
    trust: the order must be ours (id, coins, networks, amount, payout address) and
    its deposit address must be an ordinary Ergo mainnet address that is not the
    user's own. Only then is `amount` sent there, in a plain payment transaction.
    """
    import decimal
    import re
    from ergo_lib_python.chain import ErgoBoxCandidate, NetworkPrefix
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection

    cfg = req["cfg"]
    if not cfg.get("swapuz_api_key"):
        raise SwapError("Swapuz is not set up on this server.")
    to, net = _sz_code(req.get("to")), _sz_code(req.get("net"))
    dest, memo = req.get("dest"), req.get("memo") or None
    amount = req_amount(req.get("amount"), "amount")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    if not to or not net or not isinstance(dest, str) or not re.fullmatch(r"[A-Za-z0-9:._\-]{10,130}", dest) \
            or (memo is not None and not (isinstance(memo, str) and re.fullmatch(r"[A-Za-z0-9:._\-]{1,120}", memo))):
        raise SwapError("Invalid exchange request.")
    fee = miner_fee(cfg)

    # funds first: do not open an order the address cannot pay for
    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    chosen, user_erg, user_toks = select_inputs(user_boxes, lambda toks: amount + fee + change_need(len(toks)), None, 0)

    known = req.get("order") if isinstance(req.get("order"), dict) else None
    order = None
    if known and isinstance(known.get("uid"), str) and re.fullmatch(r"[A-Za-z0-9\-]{6,60}", known["uid"]):
        # the wallet asked before: the same order while Swapuz still waits for its payment
        seen = _sz_order_view(swapuz_call(cfg, "/order/uid/" + known["uid"]))
        if seen and seen["uid"] == known["uid"] and seen["deposit"] == known.get("deposit") and seen["status"] == 0:
            order = seen
    if order is None:
        est = _sz_rate(cfg, to, net, amount)
        floor = req.get("min_out")
        if isinstance(floor, str) and _sz_dec(floor) is not None \
                and decimal.Decimal(est["out"]) < decimal.Decimal(floor):
            raise SwapError("The Swapuz rate dropped since the preview. Please preview again.")
        body = {"from": "ERG", "fromNetwork": "ERGO", "to": to, "toNetwork": net, "address": dest,
                "amount": float(_erg_text(amount)), "modeCurs": "float", "addressRefund": addr_str}
        if isinstance(req.get("uuid"), str) and re.fullmatch(r"[0-9a-f\-]{32,36}", req["uuid"]):
            body["uuid"] = req["uuid"]
        if memo:
            body["memo"] = memo
        order = _sz_order_view(swapuz_call(cfg, "/home/v1/order", body, timeout=60))

    problem = None
    if not order or not isinstance(order["uid"], str) or not re.fullmatch(r"[A-Za-z0-9\-]{6,60}", order["uid"]):
        problem = "no order id"
    elif order["from"] != "ERG" or order["from_net"] != "ERGO" or order["to"] != to or order["to_net"] != net:
        problem = "other coins or networks than asked"
    elif order["dest"] != dest:
        problem = "another payout address than asked"
    elif order["amount"] is None or decimal.Decimal(order["amount"]) != decimal.Decimal(_erg_text(amount)):
        problem = "another amount than asked"
    elif order["status"] != 0:
        problem = "order not waiting for payment"
    elif order["memo_from"]:
        problem = "a deposit memo, which an Ergo payment cannot carry"
    if problem:
        sys.stderr.write("sigmaswap.py swapuz: order refused: %s\n" % problem)
        raise SwapError("Swapuz answered with an order that does not match your request. Nothing was sent. Please try again.")
    try:
        deposit = req_address(order["deposit"])
    except SwapError:
        sys.stderr.write("sigmaswap.py swapuz: order %s has no usable Ergo deposit address\n" % order["uid"])
        raise SwapError("Swapuz did not give a usable Ergo deposit address. Nothing was sent.")
    deposit_str = deposit.to_str(NetworkPrefix.Mainnet)
    if deposit_str == addr_str:
        raise SwapError("Swapuz did not give a usable Ergo deposit address. Nothing was sent.")

    ctx, height = chain_context(cfg)
    pay = ErgoBoxCandidate(value=amount, script=deposit.ergo_tree(), creation_height=height)
    change = user_outputs(address, height, dict(user_toks), user_erg - amount - fee)
    unsigned = TxBuilder(BoxSelection(chosen, []), [pay] + change, height, fee, address).build()
    built = json.loads(unsigned.json())["outputs"][0]
    if built["ergoTree"] != bytes(deposit.ergo_tree()).hex() or int(built["value"]) != amount or built.get("assets"):
        raise SwapError("The payment could not be prepared. Please try again.")
    reduced = ReducedTransaction.from_unsigned_tx(unsigned, chosen, [], ctx)
    return {
        "reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"),
        "tx_id": hexid(unsigned.id), "address": addr_str, "watch_box": hexid(chosen[0].box_id),
        "uid": order["uid"], "deposit": deposit_str, "amount": str(amount), "out": order["out"] or "0",
        "expires": order["expires"], "refund_set": order["refund"] == addr_str, "miner_fee": str(fee),
    }


def cmd_szstatus(req):
    """Where a Swapuz order stands. Their numbers: 0 waiting for the payment, 1-5 in
    progress, 6 completed, 10 overdue."""
    import re
    uid = req.get("uid")
    if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9\-]{6,60}", uid):
        raise SwapError("Unknown exchange.")
    o = _sz_order_view(swapuz_call(req["cfg"], "/order/uid/" + uid))
    # an answer about some other order must never be shown as this one
    if not o or o["uid"] != uid or o["deposit"] != req.get("deposit"):
        raise SwapError("Swapuz is not answering right now. Please try again.")
    return {"status": o["status"] if isinstance(o["status"], int) else -1, "out": o["out"] or "0",
            "aml": o["aml"], "w_tx": o["w_tx"], "d_tx": o["d_tx"]}


# ---------------------------------------------------------------- Lithos ----
# sigmalithos/: the Lithos mining pool (lithosprotocol.org) from the wallet: pool
# figures, a miner's payouts, and buying / selling LIT on LithosDex with orders.
#
# LithosDex orders are boxes under Lithos's own order contracts. Those are EIP-5
# templates: one pinned tree per order kind in which every per-order term is a
# sentinel constant (Lithos-Client, lithosdex/contracts/LDOrderContracts.scala).
# The two trees below are those pinned trees. An order is the tree with the
# sentinels replaced. That is done on the bytes, because the contracts use Ergo 6.0
# operations this library cannot parse; for the same reason an order's execution
# cannot be simulated here. What is checked instead: a tree built this way parses
# back to the terms asked for, and the construction reproduced real, filled mainnet
# orders byte for byte when it was written.

LITHOS_API = "https://lithosprotocol.org/api/v1"
LIT_ID = "c1980d829988229516430a47a5eca376060b6ce859616db0936e78ab25cb6de7"
LD_POOL_NFT = "c0b21ac51481a5d242809d9b1e189a8baf3803fe347ca01833c1aeb380e6eb29"      # the ERG:LIT pool
LD_FEE_DENOM = 10000
LD_EXECUTOR_FEE = 3000000        # what the Lithos client offers by default; miners skip orders paying less
LD_MAX_MINER_FEE = 1000000
LD_RETURNED = 1000000            # nanoERG an order box holds beyond its fill, returned with the fill
LD_MAX_SLIPPAGE_BPS = 1000
LD_SELL_PINNED = (
    "1ba6041a040008cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f8179806084c444f5503"
    "0303030402040004000e205d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d0100040204"
    "00040204a09c01040204000300020002ff020006084c444f560404040406084c444f5301010101040206010104000500"
    "0e691005040004000e36100204a00b08cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f8"
    "1798ea02d192a39a8cc7a701730073011001020402d19683030193a38cc7b2a57300000193c2b2a57301007473027303"
    "830108cdeeac93b1a5730406084c444f5402020202d803d601b2a4730000d6027301d60373029595ed93db6508fe7303"
    "91b1db630872017304938cb2db630872017305000173067307d809d604b2a5730800d605b2db63087204730900d606b2"
    "db63087201730a00d6077e8c72050206d608e4c672010611d609e4c672010511d60a7e730b06d60b9972039d9c72037e"
    "b27209730c0006720ad60c7eb27209730d0006d19683070193e5dc650cfe02730e730f027310731193c27204d0720293"
    "8c7205018c7206019272077312927ec172040699997ec1a70672037313909c9c7e998c720602b2720873140006720b72"
    "0c9c9a720773159a9c7e99c17201b2720873160006720a9c720b720c907eb0a57317d9010d4163d802d60f8c720d02d6"
    "108c720d019593c2720f73189a7210c1720f72100673197202")
LD_BUY_PINNED = (
    "1b940419040008cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f817980402040004000e"
    "205d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d010004000402040204a09c01040404"
    "000300020002ff020006084c444f5604040404040006084c444f5301010101060101040205000e691005040004000e36"
    "100204a00b08cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798ea02d192a39a8cc7"
    "a701730073011001020402d19683030193a38cc7b2a57300000193c2b2a57301007473027303830108cdeeac93b1a573"
    "0406084c444f5402020202d802d601b2a4730000d60273019595ed93db6508fe730291b1db630872017303938cb2db63"
    "0872017304000173057306d80ad603b2db6308a7730700d604b2db63087201730800d605b2a5730900d6067e99c17205"
    "c1a706d607e4c672010611d6087e8c72030206d609e4c672010511d60a7e730a06d60b9972089d9c72087eb27209730b"
    "0006720ad60c7eb27209730c0006d19683060193e5dc650cfe02730d730e02730f7310938c7203018c72040193c27205"
    "d072029272067311909c9c7e99c17201b2720773120006720b720c9c9a9a7206731373149a9c7e998c720402b2720773"
    "150006720a9c720b720c907eb0a57316d9010d4163d802d60f8c720d02d6108c720d019593c2720f73179a7210c1720f"
    "72100673187202")
# the two liquidity orders: add both sides for a provision (and the NFT that owns it), and close one
LD_DEPOSIT_PINNED = (
    "1b8a051d040006084c444f570505050508cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16"
    "f817980402040004000e205d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d0100040004"
    "0205feffffffffffffffff010400040204020404040205000300020002ff020106084c444f5806060606040006084c44"
    "4f530101010106010006010005000e691005040004000e36100204a00b08cd0279be667ef9dcbbac55a06295ce870b07"
    "029bfcdb2dce28d959f2815b16f81798ea02d192a39a8cc7a701730073011001020402d19683030193a38cc7b2a57300"
    "000193c2b2a57301007473027303830108cdeeac93b1a5730406084c444f5402020202d803d601b2a4730000d6027301"
    "d60373029595ed93db6508fe730391b1db630872017304938cb2db630872017305000173067307d80ed604b2db6308a7"
    "730800d605b2db63087201730900d6068c720501d6077e99730ae4c67201040506d608e4c672010611d6097e99c17201"
    "b27208730b0006d60a9d9c720272077209d60b7e998c720502b27208730c0006d60c9d9c7e8c720402067207720bd60d"
    "a1720a720cd60eb2a5730d00d60fb2a5730e00d610db6308720fd611b27210730f01860272067310d196830a0193e5dc"
    "650cfe02731173120273137314938c720401720692720d7315927ee4c6720e070506720d93c2720fd07203938cb27210"
    "73160001c57201927e9ac1720fc1720e069a99997ec1a706720273179591720a720c9d9c99720a720c72097207731893"
    "8c7211017206927e8c721102069591720c720a9d9c99720c720a720b72077319907eb0a5731ad901124163d802d6148c"
    "721202d6158c7212019593c27214731b9a7215c17214721506731c7203")
LD_REDEEM_PINNED = (
    "1b940418040008cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f817980404040004000e"
    "205d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d5d01000402040205feffffffffffffff"
    "ff010402040005000300020002ff02020400040006084c444f5301010101040205000e691005040004000e36100204a0"
    "0b08cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798ea02d192a39a8cc7a7017300"
    "73011001020402d19683030193a38cc7b2a57300000193c2b2a57301007473027303830108cdeeac93b1a5730406084c"
    "444f5402020202d802d601b2a4730000d60273019595ed93db6508fe730291b1db630872017303938cb2db6308720173"
    "04000173057306d808d603b2a4730700d604b2a5730800d6057ee4c67203070506d606e4c672010611d6077e997309e4"
    "c67201040506d608b2db63087201730a00d6098c720801d60ab2db63087204730b0186027209730cd19683070193e5dc"
    "650cfe02730d730e02730f731093e4c67203060e8cb2db6308a77311000193c27204d07202927ec1720406999a7e9ac1"
    "a7c17203069d9c72057e99c17201b272067312000672077313938c720a017209927e8c720a02069d9c72057e998c7208"
    "02b27206731400067207907eb0a57315d9010b4163d802d60d8c720b02d60e8c720b019593c2720d73169a720ec1720d"
    "720e0673177202")
# sha256 of each contract without its constants: how the explorer indexes order boxes
LD_SELL_TEMPLATE_HASH = "b4fd5d9be2ed32a5cc516e431bf317033d1033078026bb04baf7ae355c665dfc"
LD_BUY_TEMPLATE_HASH = "7122d56978cf08f66ff5168a160e9e750002c62e79a6d985503da8ad02f11a98"
LD_DEPOSIT_TEMPLATE_HASH = "28027ccf9c2a5ac3d31178e97a387aae3207ddec642b5662e63af30bbe642c17"
LD_REDEEM_TEMPLATE_HASH = "d67c6f6e4dfaba9b7aefce23c63add22dbc5fdd34a27946771965be943baeb04"
_LD_PINNED = {"sell": LD_SELL_PINNED, "buy": LD_BUY_PINNED, "deposit": LD_DEPOSIT_PINNED, "redeem": LD_REDEEM_PINNED}
_LD_TEMPLATE_HASHES = (LD_SELL_TEMPLATE_HASH, LD_BUY_TEMPLATE_HASH, LD_DEPOSIT_TEMPLATE_HASH, LD_REDEEM_TEMPLATE_HASH)

# The rest of the ERG:LIT deployment (Lithos-Client, lithosdex/LDHelpers.scala and LDContracts.scala).
# A liquidity position is a "provision" box holding one provision token under the guard script below;
# the guard runs the provision logic handed to it in context variable 64. Both were rebuilt from Lithos's
# pinned templates and tie back to the chain: the hash of this logic is inside this guard, the hash of
# this guard is inside the live pool box's script, and every open provision sits at this guard.
LD_VAULT_NFT = "7f45bcbf13b9925c3eef35ff247ae46b80db2e2703ce2a8bfddf294e1e60c2d7"
LD_PROV_TOKEN = "b279187c993465c666cb17646fb87325dd7d03de6ac16dfac9c50eaea71f2fc6"
LD_PROVISION_MIN = 600000000     # nanoERG a provision box holds; it comes back when the position is closed
LD_VAULT_MIN = 100000000
LD_PROVISION_GUARD_TREE = (
    "1b31010e20245ef9157dbd5f538a9240da4d631863cd44a2b132afda94555063a51e12aefbea02d193cbe4e3400e7300"
    "d40840")
LD_PROVISION_LOGIC_VALUE = (
    "d809d601e4e30002d602e4c6a7060ed603aea4d9010363aedb63087203d901054d0e938c7205017202d604b2a5db6508"
    "fe00d605db6308a7d606e4c6a70705d607e4c6a70406d608e4c6a70506d6090e20c0b21ac51481a5d242809d9b1e189a"
    "8baf3803fe347ca01833c1aeb380e6eb29959372010200d1eded938cb2db6308b2a4040000040000010e207f45bcbf13"
    "b9925c3eef35ff247ae46b80db2e2703ce2a8bfddf294e1e60c2d772039683080193c27204c2a792c17204c1a793db63"
    "087204720593e4c67204060e720293e4c672040705720692e4c672040406720792e4c6720405067208efe6c67204080e"
    "959372010201d1eded7203938cb2db6308b2a4040000040000017209afa5d9010a63efaedb6308720ad9010c4d0e938c"
    "720c017202959372010202d1ed8f8cc7a70199a304c094409683090193c27204c2a792c17204c1a793db630872047205"
    "93e4c672040406720793e4c672040506720893e4c67204060e720293e4c6720407057206918cc772040199a304d00fef"
    "e6c67204080e959372010203d1eded7203938cb2db6308b2a40400000400000172099683050193c27204c2a792c17204"
    "c1a793db63087204720593e4c67204060e7202efe6c67204080ed10100")
_LD_SENTINELS = {
    "pk": bytes.fromhex("08cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
    "pool": bytes.fromhex("0e20" + "5d" * 32),
    "ex": bytes.fromhex("06084c444f5301010101"), "mf": bytes.fromhex("06084c444f5402020202"),
    "base": bytes.fromhex("06084c444f5503030303"), "min": bytes.fromhex("06084c444f5604040404"),
    "dx": bytes.fromhex("06084c444f5705050505"), "shares": bytes.fromhex("06084c444f5806060606"),
}
# where Lithos rollups sit while they pay out / collect proofs (P2S addresses of Payout and Holding_Guard)
LITHOS_PAYOUT_ADDRESS = "3VaP5ZZQFaC1ucEnpUue1ZfgmX2bRamvJezvrVxfxNYmzy4fi4c9XFRmc5k3cfzw1YUFLdRHrX74tvGtpoPvh9NXZQSoSDVUS3DqdurRuVr7z6fkZkNRGZDjMZWHPZzSNRAKETELbx34wi1vqmNAqVMqHsHsTWax2XfdVaphxAsvv5HPbNkHp4tPg3APPzcC54WHuvfDqG8VaftmdZvqxh8G9xhfdd9LnUdFGQYRt3hLx6XcSqHmezE3ZH6KAG8pLbPp6LNVgiLdyFuunf9wA6EixdWxtXRaa7FG7CtARqWaRicNGLAdey9jdL5iwwHzYizAa2J42yjgoSxWF5nNz3Ma7LBdAxoQmfQwDsHWaKrP5ovicNkzMurGY8JUFTPMzR5nyCYfY6zoqce5Riubf4hZdqYUTD4vb4prUiJaFhYaUVwwMP73ZK6V54jiRyJArdseAkvXDnt3BbdFtMPvEswXcg5utsTjbzHmhXbnD7KiiQPLYzMdSXW4Ts5CCbVhqiK5Sd2DQbhTi3yvpdbQk1XZp9gienjRVkYT6kGnnnzGQXBVN1ffv1jVBTZ6b1TFqMkC39R4cChPHQiYh7gXyBjFHo2vTdTAWbUpHjjqu6HafRsawpaAtqrv1n73N2MhvmDgsgq9HP1KeHoc9qMbVTRznf3tHSLp2s4NaP3WhukTcECzPZ75BEStM1xzTy4BamkV2DCS44Fx3tMiW7CQkfPDw3o62jMRUBof6rQMHgsexwrHVfKzAbhwTCY5hNaxgXEVGjogz6Pin8P29dmnRNm9qosShogi6To6hMUXG1aFLFjBTwhprJ3L2JRfaYWEC41un66ZjPvExQaW65htdyACjKBsL74xFm6LmRs5XXAzctP23y3dMKZokCQttH87kBwMh8MCnAsv6Vba3cu8TiYiBJTdJG4K4dz4F7Yy3AC3VisGHcZ91UNPcfAQxb2wvp65CYXMiiapV5tq3FXrpNqpDuRwnkxAeVPcYW2Qm1qHQQcPpC2ZdiXEtFJAXcUCKnFHuYkoXCPfh4tg1P9zNFeSu6ntjkvfru3V6mmukBmupinCzr2Eo48gHj19E49ShwLszqqhrCQXcWRK7HTgmjS5xCfC5AcxaZW3fQCMELLSAmw6othTs7iusf84SjqvwfMZshtpaeiDBMTU6WvJ9iNhPbdtKKjA8vhE6bfNLVGtFgx6xepxq36mYYZpB3dVxoQpYN5WcsLXtmfLFJB5Nx7FG9eMbgLSPJLLcKo4UidBkmQfo7VxJZGGqyZQa29eGZf3YRd19J4GgxtRyXKDJ61x6mTxFdMuEWsiehPD7B2V5RVDtTUUz7MvZUbCpyk2vLxnPf5euKvQ7hkxohvvkLMwAhYn3pFudWsubNG42BHezbNpwPPtm1o8aAhbNktdrZgXPXV6WNMkHkmzG4H7LRguwBs1n132Tz7WZd5CnkREw3WDWwfSiSYkQAxm2SxX3ahnGx2aSk2V2NuNPShbGfdCHzHDUCLwn4wQ1cRd5VUTVwdrQwzYj6vZwuE3LiPd4nfRMzhMus5Y2Q4HPLHabzY1MDi2PU4uaHsPWZF9SHt6zDMoj88U4zddQLHJMBe47hjwT1nxRymibX4eZCJ1JxrF7cvtcPh7z2LGP6kVZmNySF2tRHcHdddPdPNuXTmwnQMyX8iAU515XU9z8n9NmQPXhfEKUT14AZfiyM2mws5SA8cXYEzQyTqy6fYbk8s8Cwe5FoKpF3cNuEeq4"
LITHOS_HOLDING_ADDRESS = "WvonkGz7pj74cH7K3ZQvkwp99FSJVoGwqcqUuGCNMEYRsHscxLxALs2h95uwkx7QAyWv1xuWa9eedFUKmVKRVHrJDjis475EsFsf7Xw7mS689A5VeZMeRTBCQZyDMA3E6N7AKzANMwbjDnMPySLLsqC74dZhvY59bBE8ZCoxq4niuNp6cjVYh82xWKBuh3sps"
LITHOS_COLLATERAL_TOKEN = "a8a790e784e93ac0e68649181ae3d251e84fb5c741624100e7e945ae1e82dc98"


def _vlq(n):
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _read_vlq(buf, i):
    n = shift = 0
    while True:
        c = buf[i]
        i += 1
        n |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return n, i


def _ld_bigint(v):
    """A BigInt constant as ErgoTree serializes it (type 6, length, two's complement)."""
    size = max(1, (v.bit_length() + 8) // 8)
    return b"\x06" + _vlq(size) + v.to_bytes(size, "big", signed=True)


def _ld_segments(kind):
    """The pinned tree cut at its sentinels -> (fixed byte runs, term names in order)."""
    raw = bytes.fromhex(_LD_PINNED[kind])
    size, i = _read_vlq(raw, 1)
    body = raw[i:]
    if raw[0] != 0x1B or size != len(body):
        raise SwapError("Service misconfigured.")
    cuts = sorted((body.find(v), k) for k, v in _LD_SENTINELS.items() if body.find(v) >= 0)
    segs, names, last = [], [], 0
    for pos, k in cuts:
        segs.append(body[last:pos])
        names.append(k)
        last = pos + len(_LD_SENTINELS[k])
    segs.append(body[last:])
    return segs, names


def ld_order_tree(kind, pk_const, min_quote=None, base=None, dx=None, shares=None):
    """ErgoTree bytes of a LithosDex order on the ERG:LIT pool, for the owner key `pk_const`
    (35 bytes, 08cd + key).
      sell     sells `base` nanoERG for at least `min_quote` LIT units
      buy      sells the LIT in the box for at least `min_quote` nanoERG after the executor fee
      deposit  adds up to `dx` nanoERG and the LIT in the box for at least `shares` pool shares
      redeem   closes the provision owned by the NFT in the box"""
    vals = {"pk": pk_const, "pool": b"\x0e\x20" + bytes.fromhex(LD_POOL_NFT), "ex": _ld_bigint(LD_EXECUTOR_FEE),
            "mf": _ld_bigint(LD_MAX_MINER_FEE)}
    for name, v in (("min", min_quote), ("base", base), ("dx", dx), ("shares", shares)):
        if v is not None:
            vals[name] = _ld_bigint(v)
    segs, names = _ld_segments(kind)
    body = b"".join(seg + vals[k] for seg, k in zip(segs, names)) + segs[-1]
    return b"\x1b" + _vlq(len(body)) + body


def ld_parse_tree(tree):
    """-> {"kind", "pk" (35 bytes), "pool", "ex", "mf", "min", "base"} for a LithosDex swap
    order tree, or None. A tree counts only if rebuilding it from its terms gives it back."""
    for kind in ("sell", "buy", "deposit", "redeem"):
        try:
            if tree[0] != 0x1B:
                return None
            size, i = _read_vlq(tree, 1)
            body = tree[i:]
            if size != len(body):
                return None
            segs, names = _ld_segments(kind)
            out, p, ok = {"kind": kind}, 0, True
            for seg, k in zip(segs, names):
                if body[p:p + len(seg)] != seg:
                    ok = False
                    break
                p += len(seg)
                if k == "pk":
                    out[k], p = body[p:p + 35], p + 35
                elif k == "pool":
                    out[k], p = body[p + 2:p + 34].hex(), p + 34
                else:
                    if body[p] != 6:
                        ok = False
                        break
                    n, q = _read_vlq(body, p + 1)
                    out[k], p = int.from_bytes(body[q:q + n], "big", signed=True), q + n
            if not ok or body[p:] != segs[-1] or out["pk"][:2] != b"\x08\xcd":
                continue
            rebuilt = ld_order_tree(kind, out["pk"], out.get("min"), out.get("base"), out.get("dx"), out.get("shares")) \
                if (out["pool"], out["ex"], out["mf"]) == (LD_POOL_NFT, LD_EXECUTOR_FEE, LD_MAX_MINER_FEE) else None
            out["ours"] = rebuilt == tree        # exactly what this app would have built for these terms
        except (IndexError, ValueError):
            continue
        return out
    return None


class LdPool(object):
    """The ERG:LIT pool box. Reserves are the box's holdings less the fees it has set aside."""

    def __init__(self, box):
        assets = box.get("assets") or []
        if len(assets) < 2 or assets[0].get("tokenId") != LD_POOL_NFT or assets[1].get("tokenId") != LIT_ID:
            raise SwapError("The LithosDex pool is unavailable right now.")
        reg = box.get("additionalRegisters") or {}
        try:
            fees = json.loads(reg["R5"]["renderedValue"])
            pending = json.loads(reg["R6"]["renderedValue"])
            self.fee, self.fee_x, self.fee_y = int(fees[0]), int(fees[1]), int(fees[2])
            self.rx = int(box["value"]) - int(pending[0])
            self.ry = int(assets[1]["amount"]) - int(pending[1])
        except (KeyError, IndexError, TypeError, ValueError):
            raise SwapError("The LithosDex pool is unavailable right now.")
        if not (0 < self.fee <= LD_FEE_DENOM and 0 <= self.fee_x < LD_FEE_DENOM and 0 <= self.fee_y < LD_FEE_DENOM
                and self.rx > 0 and self.ry > 0):
            raise SwapError("The LithosDex pool is unavailable right now.")
        self.box_id = box.get("boxId")
        self.raw = box

    def sell_out(self, base):
        """LIT units the pool pays for `base` nanoERG (the order contract's own formula)."""
        traded = base - base * self.fee_x // LD_FEE_DENOM
        return self.ry * traded * self.fee // (self.rx * LD_FEE_DENOM + traded * self.fee)

    def buy_out(self, sold):
        """nanoERG the pool pays for `sold` LIT units, before the executor fee."""
        traded = sold - sold * self.fee_y // LD_FEE_DENOM
        return self.rx * traded * self.fee // (self.ry * LD_FEE_DENOM + traded * self.fee)


MINER_FEE_TREE = ("1005040004000e36100204a00b08cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798ea02d192a3"
                  "9a8cc7a701730073011001020402d19683030193a38cc7b2a57300000193c2b2a57301007473027303830108cdeeac93b1a57304")
LD_LOCKED_LP = 0x7FFFFFFFFFFFFFFF
LD_ACC_SCALE = 10 ** 27
_LIB_V6 = None


def lib_evaluates_v6():
    """Can the installed ergo-lib evaluate Ergo 6.0 contracts (Lithos's)? 0.29 can, 0.28 cannot."""
    global _LIB_V6
    if _LIB_V6 is None:
        try:
            from ergo_lib_python.ergo_tree import ErgoTree
            ErgoTree.from_bytes(bytes.fromhex(LD_SELL_PINNED)).constants()
            _LIB_V6 = True
        except Exception:
            _LIB_V6 = False
    return _LIB_V6


def _coll_long(values):
    """Coll[Long] as a register holds it: type 0x11, count, zigzag varints."""
    return (b"\x11" + _vlq(len(values)) + b"".join(_vlq((v << 1) ^ (v >> 63)) for v in values)).hex()


def ld_simulate_fill(pool, order_box, kind, amount, address, ctx, height):
    """Build the transaction a Lithos miner would build to fill `order_box` right now and have
    BOTH contracts judge it: the pool and the order. Needs lib_evaluates_v6().

    [pool (swap), order] -> [pool', the owner's reward, the executor, miner fee]: the shape of
    every fill on the chain. Raises SwapError when either contract refuses, which means the
    order as built could not be filled at today's price. -> what the owner would receive."""
    from ergo_lib_python.chain import ErgoBox
    from ergo_lib_python.transaction import UnsignedTransaction
    from ergo_lib_python.wallet import Wallet
    raw = pool.raw
    reg = raw["additionalRegisters"]
    try:
        liquidity = int(reg["R4"]["renderedValue"])
        pend = [int(x) for x in json.loads(reg["R6"]["renderedValue"])]
        acc_x = int(reg["R7"]["renderedValue"].strip("CBigInt()"))
        acc_y = int(reg["R8"]["renderedValue"].strip("CBigInt()"))
        supply = LD_LOCKED_LP - liquidity
        assets = [dict(tokenId=a["tokenId"], amount=int(a["amount"])) for a in raw["assets"]]
        order_value = int(json.loads(order_box.json())["value"])
        own_tree = bytes(address.ergo_tree()).hex()
        if kind == "sell":                              # ERG into the pool, LIT out
            out = pool.sell_out(amount)
            fee_part = pool.fee_x * amount // LD_FEE_DENOM
            pend[0] += fee_part
            acc_x += fee_part * LD_ACC_SCALE // supply
            pool_value = int(raw["value"]) + amount
            assets[1]["amount"] -= out
            reward = {"value": order_value - amount - LD_EXECUTOR_FEE, "assets": [{"tokenId": LIT_ID, "amount": out}]}
            got = out
        else:                                           # LIT into the pool, ERG out
            quote = pool.buy_out(amount)
            fee_part = pool.fee_y * amount // LD_FEE_DENOM
            pend[1] += fee_part
            acc_y += fee_part * LD_ACC_SCALE // supply
            pool_value = int(raw["value"]) - quote
            assets[1]["amount"] += amount
            got = quote - LD_EXECUTOR_FEE
            reward = {"value": order_value + got, "assets": []}
        registers = {"R4": reg["R4"]["serializedValue"], "R5": reg["R5"]["serializedValue"], "R6": _coll_long(pend),
                     "R7": _ld_bigint(acc_x).hex(), "R8": _ld_bigint(acc_y).hex()}

        def box(value, tree, assets_=(), regs=None):
            return {"value": value, "ergoTree": tree, "creationHeight": height, "assets": list(assets_),
                    "additionalRegisters": regs or {}}
        tx = UnsignedTransaction.from_json(json.dumps({
            "inputs": [{"boxId": raw["boxId"], "extension": {"0": "0200"}},        # CTX_OP = 0: swap
                       {"boxId": hexid(order_box.box_id), "extension": {}}],
            "dataInputs": [],
            "outputs": [box(pool_value, raw["ergoTree"], assets, registers),
                        box(reward["value"], own_tree, reward["assets"]),
                        box(LD_EXECUTOR_FEE - LD_MAX_MINER_FEE, own_tree),          # any address: the contract does not care
                        box(LD_MAX_MINER_FEE, MINER_FEE_TREE)]}))
        boxes = [ErgoBox.from_json(raw), order_box]
        Wallet([]).sign_tx_input(tx, 0, boxes, [], ctx)       # the pool accepts
        Wallet([]).sign_tx_input(tx, 1, boxes, [], ctx)       # the order accepts, with no key
    except SwapError:
        raise
    except Exception as e:
        sys.stderr.write("sigmaswap.py: lithos fill simulation failed: %s: %s\n" % (type(e).__name__, str(e)[:300]))
        raise SwapError("This order could not be filled at the current price. Please preview it again.")
    return got


def ld_pool(cfg):
    page = explorer(cfg, "boxes/unspent/byTokenId/%s?limit=2" % LD_POOL_NFT) or {}
    items = page.get("items") or []
    if len(items) != 1:
        raise SwapError("The LithosDex pool is unavailable right now.")
    return LdPool(items[0])


def _ld_terms(pool, side, amount):
    """side "buy" = pay ERG, receive LIT (a sell order in LithosDex's naming); "sell" = pay LIT."""
    if side == "buy":
        return {"kind": "sell", "out": pool.sell_out(amount), "spot_out": amount * pool.ry // pool.rx,
                "from": ERG_ID, "to": LIT_ID}
    if side == "sell":
        return {"kind": "buy", "out": pool.buy_out(amount) - LD_EXECUTOR_FEE, "spot_out": amount * pool.rx // pool.ry,
                "from": LIT_ID, "to": ERG_ID}
    raise SwapError("Choose whether to buy or sell LIT.")


def cmd_ldquote(req):
    cfg = req["cfg"]
    pool = ld_pool(cfg)
    res = {"price_9": str(pool.rx * 10 ** 9 // pool.ry), "reserve_erg": str(pool.rx), "reserve_lit": str(pool.ry),
           "executor_fee": str(LD_EXECUTOR_FEE), "returned": str(LD_RETURNED)}
    if req.get("amount") is not None:
        amount = req_amount(req.get("amount"), "amount")
        t = _ld_terms(pool, req.get("side"), amount)
        if t["out"] <= 0:
            raise SwapError("This amount is too small: the fees would eat it.")
        res.update({"out": str(t["out"]), "from": t["from"], "to": t["to"], "amount": str(amount),
                    "impact_bps": max(0, (t["spot_out"] - t["out"]) * 10000 // max(1, t["spot_out"])),
                    "pool_fee_bps": (LD_FEE_DENOM - pool.fee) + (pool.fee_x if t["kind"] == "sell" else pool.fee_y)})
    if req.get("address"):
        req_address(req["address"])
        bal = explorer(cfg, "addresses/%s/balance/confirmed" % urllib.parse.quote(req["address"], safe="")) or {}
        res["balance_erg"] = str(int(bal.get("nanoErgs", 0)))
        res["balance_lit"] = str(next((int(x.get("amount", 0)) for x in bal.get("tokens") or []
                                       if x.get("tokenId") == LIT_ID), 0))
    return res


def cmd_ldbuild(req):
    """Place a LithosDex swap order: one transaction from the user's boxes to the order box."""
    from ergo_lib_python.chain import ErgoBox, ErgoBoxCandidate, NetworkPrefix, Token, TokenId
    from ergo_lib_python.ergo_tree import ErgoTree
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection

    cfg = req["cfg"]
    side = req.get("side")
    amount = req_amount(req.get("amount"), "amount")
    min_out = req_amount(req.get("min_out"), "minimum output")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    fee = miner_fee(cfg)
    pool = ld_pool(cfg)
    t = _ld_terms(pool, side, amount)
    if t["out"] <= 0:
        raise SwapError("This amount is too small: the fees would eat it.")
    # a waiting order is filled at whatever the pool pays then, down to its minimum: the minimum is
    # the user's protection and arrives in the signing link, so never accept one far below today's price
    if min_out < t["out"] * (10000 - LD_MAX_SLIPPAGE_BPS) // 10000:
        raise SwapError("That minimum is too far below the current price (more than %d %%). Preview the order again."
                        % (LD_MAX_SLIPPAGE_BPS // 100))
    if min_out > t["out"]:
        raise SwapError("The price moved beyond your slippage tolerance. Please preview the order again.")

    pk = bytes(address.ergo_tree())[1:]            # 08cd + the 33-byte key
    if t["kind"] == "sell":
        tree = ld_order_tree("sell", pk, min_out, amount)
        box_value, box_tokens, sell_token = amount + LD_EXECUTOR_FEE + LD_RETURNED, [], None
    else:
        tree = ld_order_tree("buy", pk, min_out)
        box_value, box_tokens, sell_token = LD_RETURNED, [Token(TokenId(LIT_ID), amount)], LIT_ID
    back = ld_parse_tree(tree)
    if not back or not back["ours"] or back["kind"] != t["kind"] or back["pk"] != pk or back["min"] != min_out \
            or back.get("base") != (amount if t["kind"] == "sell" else None):
        raise SwapError("The order could not be prepared. Please try again.")

    def need_erg(toks):
        n = len(set(toks) - ({sell_token} if sell_token and toks.get(sell_token) == amount else set()))
        return box_value + fee + change_need(n)

    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    chosen, user_erg, user_toks = select_inputs(user_boxes, need_erg, sell_token, amount)
    ctx, height = chain_context(cfg)
    order_out = ErgoBoxCandidate(value=box_value, script=ErgoTree.from_bytes(tree), creation_height=height,
                                 tokens=box_tokens)
    out_toks = dict(user_toks)
    if sell_token:
        out_toks[sell_token] -= amount
    change = user_outputs(address, height, out_toks, user_erg - box_value - fee)
    unsigned = TxBuilder(BoxSelection(chosen, []), [order_out] + change, height, fee, address).build()
    built = json.loads(unsigned.json())["outputs"][0]
    if built["ergoTree"] != tree.hex() or int(built["value"]) != box_value:
        raise SwapError("The order could not be prepared. Please try again.")
    reduced = ReducedTransaction.from_unsigned_tx(unsigned, chosen, [], ctx)
    order_box = ErgoBox.from_box_candidate(order_out, unsigned.id, 0)
    # with a library that can run Lithos's contracts: prove a miner can fill exactly this box now,
    # and that the fill pays what the preview said. Without one the byte-level check above stands alone.
    checked = False
    if lib_evaluates_v6():
        if ld_simulate_fill(pool, order_box, t["kind"], amount, address, ctx, height) != t["out"]:
            raise SwapError("The order could not be prepared. Please try again.")
        checked = True
    return {
        "checked": checked,
        "reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"),
        "tx_id": hexid(unsigned.id), "address": addr_str, "side": side, "from": t["from"], "to": t["to"],
        "amount": str(amount), "out": str(t["out"]), "min_out": str(min_out), "miner_fee": str(fee),
        "executor_fee": str(LD_EXECUTOR_FEE), "watch_box": hexid(chosen[0].box_id), "order_box": hexid(order_box.box_id),
    }


def _ld_order_view(box, pk=None):
    """An explorer box as an order on the ERG:LIT pool -> dict, or None."""
    try:
        terms = ld_parse_tree(bytes.fromhex(box["ergoTree"]))
        if not terms or terms["pool"] != LD_POOL_NFT or (pk is not None and terms["pk"] != pk):
            return None
        assets = box.get("assets") or []
        kind = terms["kind"]
        view = {"box_id": box["boxId"], "kind": kind, "value": str(int(box["value"]))}
        if kind == "sell":
            view.update({"from": ERG_ID, "to": LIT_ID, "amount": str(terms["base"]), "min_out": str(terms["min"])})
        elif kind == "buy":
            if not assets or assets[0].get("tokenId") != LIT_ID:
                return None
            view.update({"from": LIT_ID, "to": ERG_ID, "amount": str(int(assets[0]["amount"])), "min_out": str(terms["min"])})
        elif kind == "deposit":
            if not assets or assets[0].get("tokenId") != LIT_ID:
                return None
            view.update({"amount_x": str(terms["dx"]), "amount_y": str(int(assets[0]["amount"])), "min_shares": str(terms["shares"])})
        else:
            if not assets or int(assets[0].get("amount", 0)) != 1:
                return None
            view.update({"nft": assets[0]["tokenId"]})
        return view
    except (KeyError, TypeError, ValueError):
        return None


def cmd_ldorders(req):
    """Open LithosDex orders of one address on the ERG:LIT pool, read from the chain."""
    cfg = req["cfg"]
    address = req_address(req.get("address"))
    pk = bytes(address.ergo_tree())[1:]
    orders = []
    wanted = ("sell", "buy") if req.get("kinds") == "swap" else ("deposit", "redeem") if req.get("kinds") == "liquidity" \
        else ("sell", "buy", "deposit", "redeem")
    for template_hash in _LD_TEMPLATE_HASHES:
        offset = 0
        while offset < 1000:
            page = explorer(cfg, "boxes/unspent/byErgoTreeTemplateHash/%s?limit=100&offset=%d"
                            % (template_hash, offset)) or {}
            items = page.get("items") or []
            orders.extend(o for o in (_ld_order_view(b, pk) for b in items) if o and o["kind"] in wanted)
            offset += 100
            if len(items) < 100:
                break
    return {"orders": orders}


def cmd_ldcancel(req):
    """Take an open order back: the owner spends the order box with their own key.

    The order contract cannot be evaluated by this library (Ergo 6.0 operations), so the
    transaction is reduced against a stand-in: a box with the same value and tokens that
    is plainly the owner's. On the refund path the contract reduces to exactly the
    owner's key, so the result is the same; only the box id differs, and it is put back.
    The order sits at input 0, where the contract can only be refunded, never executed.
    """
    from ergo_lib_python.chain import ErgoBox, ErgoBoxCandidate, NetworkPrefix, Token, TokenId
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder, TxId
    from ergo_lib_python.wallet import BoxSelection

    cfg = req["cfg"]
    box_id = req_token_id(req.get("order_box"), "order")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    pk = bytes(address.ergo_tree())[1:]
    fee = miner_fee(cfg)
    raw = explorer(cfg, "boxes/" + box_id)
    if not raw or raw.get("spentTransactionId"):
        raise SwapError("This order is no longer open. It was filled or already cancelled.")
    order = _ld_order_view(raw, pk)
    if not order:
        raise SwapError("This is not an open order of this address.")
    # a miner's fill may already be waiting for the next block: a cancel would only be refused as a double spend
    if any(i.get("boxId") == box_id for t in mempool_txs(cfg, raw.get("address") or "") for i in t.get("inputs") or []):
        raise SwapError("A miner is filling this order right now. It cannot be cancelled any more; check again in a few minutes.")
    value = int(raw["value"])
    toks = {a["tokenId"]: int(a["amount"]) for a in raw.get("assets") or []}
    ctx, height = chain_context(cfg)
    stand_in = ErgoBox.from_box_candidate(
        ErgoBoxCandidate(value=value, script=address.ergo_tree(), creation_height=int(raw["creationHeight"]),
                         tokens=[Token(TokenId(t), a) for t, a in toks.items()]),
        TxId(bytes.fromhex(raw["transactionId"])), int(raw["index"]))

    chosen, user_erg, user_toks = [], 0, {}
    if value < fee + change_need(len(toks)):          # an order holding only tokens cannot pay its own way out
        user_boxes = fetch_user_boxes(cfg, addr_str)
        need = lambda t: fee + change_need(len(set(t) | set(toks))) - value       # noqa: E731
        chosen, user_erg, user_toks = select_inputs(user_boxes, need, None, 0)
    out_toks = dict(user_toks)
    for t, a in toks.items():
        out_toks[t] = out_toks.get(t, 0) + a
    outs = user_outputs(address, height, out_toks, value + user_erg - fee)
    checked = False
    if lib_evaluates_v6():
        # the library can run the contract: reduce against the real order box, which only succeeds
        # if the contract really does hand the box to the owner's key from this position
        inputs = [ErgoBox.from_json(raw)] + chosen
        unsigned = TxBuilder(BoxSelection(inputs, []), outs, height, fee, address).build()
        try:
            data = bytes(ReducedTransaction.from_unsigned_tx(unsigned, inputs, [], ctx))
        except Exception as e:
            sys.stderr.write("sigmaswap.py: lithos cancel refused by the contract: %s\n" % str(e)[:300])
            raise SwapError("The cancellation could not be prepared. Please try again.")
        checked = True
    else:
        inputs = [stand_in] + chosen
        unsigned = TxBuilder(BoxSelection(inputs, []), outs, height, fee, address).build()
        data = bytes(ReducedTransaction.from_unsigned_tx(unsigned, inputs, [], ctx))
        fake, real = bytes(stand_in.box_id), bytes.fromhex(box_id)
        if data.count(fake) != 1:
            raise SwapError("The cancellation could not be prepared. Please try again.")
        data = data.replace(fake, real)
    final = ReducedTransaction.from_bytes(data)       # must still be a well-formed reduced transaction
    spent = json.loads(final.unsigned_tx().json())
    own = bytes(address.ergo_tree()).hex()
    if spent["inputs"][0]["boxId"] != box_id or len(spent["outputs"]) < 2 \
            or any(o["ergoTree"] != own for o in spent["outputs"][:-1]):
        raise SwapError("The cancellation could not be prepared. Please try again.")
    return {"reduced_tx": base64.urlsafe_b64encode(data).decode("ascii"), "tx_id": hexid(final.unsigned_tx().id),
            "address": addr_str, "kind": order["kind"], "from": order.get("from"), "amount": order.get("amount"),
            "amount_x": order.get("amount_x"), "amount_y": order.get("amount_y"), "value": str(value),
            "miner_fee": str(fee), "checked": checked}


def cmd_ldstatus(req):
    """What happened to an order this app placed.
    states: pending | failed | unknown (the placing transaction) | open | filled | cancelled"""
    cfg = req["cfg"]
    tx_id = req_token_id(req.get("tx_id"), "transaction")
    watch = req_token_id(req.get("watch_box"), "transaction")
    order_box = req_token_id(req.get("order_box"), "order")
    address = req.get("address")
    tx = _tx_or_none(cfg, tx_id)
    if tx and tx.get("inclusionHeight"):
        b = explorer(cfg, "boxes/" + order_box) or {}
        spent_by = b.get("spentTransactionId")
        if not spent_by:
            return {"state": "open"}
        stx = _tx_or_none(cfg, spent_by) or {}
        ins, outs = stx.get("inputs") or [], stx.get("outputs") or []
        pool_first = bool(ins) and any(a.get("tokenId") == LD_POOL_NFT for a in ins[0].get("assets") or [])
        if pool_first and len(outs) >= 2:
            reward = outs[1]                  # the order contract fixes the owner's reward at output 1
            if req.get("to") == ERG_ID:
                got = max(0, int(reward.get("value", 0)) - int(b.get("value", 0)))
            else:
                got = next((int(a.get("amount", 0)) for a in reward.get("assets") or []
                            if a.get("tokenId") == LIT_ID), 0)
            return {"state": "filled", "received": str(got)}
        return {"state": "cancelled"}
    if address:
        req_address(address)
        if any(t.get("id") == tx_id for t in mempool_txs(cfg, address)):
            return {"state": "pending"}
    b = explorer(cfg, "boxes/" + watch)
    if b and b.get("spentTransactionId") and b["spentTransactionId"] != tx_id:
        return {"state": "failed"}
    return {"state": "unknown"}


# ---- LithosDex liquidity: deposit, positions, fee claims, closing ----------------
# All of it needs lib_evaluates_v6(): these transactions spend or are checked against
# Lithos's own contracts, and nothing here is handed to a wallet unproven.

def _ld_registers(raw):
    reg = raw["additionalRegisters"]
    big = lambda r: int(reg[r]["renderedValue"].strip("CBigInt()"))      # noqa: E731
    return {"liquidity": int(reg["R4"]["renderedValue"]), "pend": [int(x) for x in json.loads(reg["R6"]["renderedValue"])],
            "acc_x": big("R7"), "acc_y": big("R8")}


def _long_const(v):
    return (b"\x05" + _vlq((v << 1) ^ (v >> 63))).hex()


def _bytes_const(raw_bytes):
    return (b"\x0e" + _vlq(len(raw_bytes)) + raw_bytes).hex()


def _box_json(value, tree, height, assets=(), regs=None):
    return {"value": int(value), "ergoTree": tree, "creationHeight": height,
            "assets": [{"tokenId": t, "amount": int(a)} for t, a in assets if int(a) > 0],
            "additionalRegisters": regs or {}}


def _user_boxes_json(address, height, toks, erg, first_token=None):
    """JSON form of user_outputs(): the user's change, split when it carries many tokens."""
    own = bytes(address.ergo_tree()).hex()
    tok_list = sorted(((t, a) for t, a in toks.items() if a > 0), key=lambda kv: (kv[0] != first_token, kv[0]))
    chunks = [tok_list[i:i + MAX_TOKENS_PER_BOX] for i in range(0, len(tok_list), MAX_TOKENS_PER_BOX)] or [[]]
    extra = change_box_min_value(MAX_TOKENS_PER_BOX)
    main = erg - extra * (len(chunks) - 1)
    if main < change_box_min_value(len(chunks[0])):
        raise SwapError("Insufficient ERG on this address for this transaction plus the network fee.")
    return [_box_json(main if i == 0 else extra, own, height, chunk) for i, chunk in enumerate(chunks)]


def _need_v6():
    if not lib_evaluates_v6():
        raise SwapError("Liquidity on LithosDex is not available on this server yet.")


def ld_vault(cfg):
    items = (explorer(cfg, "boxes/unspent/byTokenId/%s?limit=2" % LD_VAULT_NFT) or {}).get("items") or []
    if len(items) != 1 or not (items[0].get("assets") or []) or items[0]["assets"][0].get("tokenId") != LD_VAULT_NFT:
        raise SwapError("The LithosDex fee vault is unavailable right now.")
    v = items[0]
    reg = v["additionalRegisters"]
    v["_acc_x"] = int(reg["R4"]["renderedValue"].strip("CBigInt()"))
    v["_acc_y"] = int(reg["R5"]["renderedValue"].strip("CBigInt()"))
    return v


def _ld_split(pool, supply, x, y):
    """How a deposit order offering x nanoERG and y LIT is filled (the executor's own arithmetic)."""
    by_x, by_y = x * supply // pool.rx, y * supply // pool.ry
    excess_x = (by_x - by_y) * pool.rx // supply if by_x > by_y else 0
    excess_y = (by_y - by_x) * pool.ry // supply if by_y > by_x else 0
    return {"shares": min(by_x, by_y), "taken_x": x - excess_x, "taken_y": y - excess_y,
            "excess_x": excess_x, "excess_y": excess_y}


def cmd_ldlpquote(req):
    """The pool as a liquidity provider sees it, and what adding `amount` nanoERG would take and give."""
    cfg = req["cfg"]
    _need_v6()
    pool = ld_pool(cfg)
    st = _ld_registers(pool.raw)
    supply = LD_LOCKED_LP - st["liquidity"]
    res = {"reserve_erg": str(pool.rx), "reserve_lit": str(pool.ry), "supply": str(supply),
           "provision_min": str(LD_PROVISION_MIN), "executor_fee": str(LD_EXECUTOR_FEE), "returned": str(LD_RETURNED),
           "pool_fee_bps": pool.fee_x + pool.fee_y}
    if req.get("amount") is not None:
        x = req_amount(req.get("amount"), "amount")
        y = -(-x * pool.ry // pool.rx)                      # the matching LIT, rounded up
        sp = _ld_split(pool, supply, x, y)
        if sp["shares"] <= 0:
            raise SwapError("That amount is too small for this pool.")
        res.update({"amount_x": str(x), "amount_y": str(y), "shares": str(sp["shares"]),
                    "share_bps": sp["shares"] * 10000 // (supply + sp["shares"])})
    if req.get("address"):
        req_address(req["address"])
        bal = explorer(cfg, "addresses/%s/balance/confirmed" % urllib.parse.quote(req["address"], safe="")) or {}
        res["balance_erg"] = str(int(bal.get("nanoErgs", 0)))
        res["balance_lit"] = str(next((int(t.get("amount", 0)) for t in bal.get("tokens") or [] if t.get("tokenId") == LIT_ID), 0))
    return res


def _prove(tx_json, boxes, ctx, indexes):
    """Every listed input's contract must accept this transaction with no key at all."""
    from ergo_lib_python.transaction import UnsignedTransaction
    from ergo_lib_python.wallet import Wallet
    tx = UnsignedTransaction.from_json(json.dumps(tx_json))
    for i in indexes:
        Wallet([]).sign_tx_input(tx, i, boxes, [], ctx)
    return tx


def ld_simulate_deposit(pool, order_box, x, y, address, ctx, height):
    """The fill of a deposit order as a miner would build it; pool and order must both accept. -> shares."""
    from ergo_lib_python.chain import ErgoBox
    raw = pool.raw
    try:
        st = _ld_registers(raw)
        supply = LD_LOCKED_LP - st["liquidity"]
        sp = _ld_split(pool, supply, x, y)
        reg = raw["additionalRegisters"]
        a = raw["assets"]
        own = bytes(address.ergo_tree()).hex()
        order_value = int(json.loads(order_box.json())["value"])
        outputs = [
            _box_json(int(raw["value"]) + sp["taken_x"], raw["ergoTree"], height,
                      [(a[0]["tokenId"], 1), (a[1]["tokenId"], int(a[1]["amount"]) + sp["taken_y"]), (a[2]["tokenId"], int(a[2]["amount"]) - 1)],
                      {"R4": _long_const(st["liquidity"] - sp["shares"]), "R5": reg["R5"]["serializedValue"],
                       "R6": reg["R6"]["serializedValue"], "R7": reg["R7"]["serializedValue"], "R8": reg["R8"]["serializedValue"]}),
            _box_json(LD_PROVISION_MIN, LD_PROVISION_GUARD_TREE, height, [(LD_PROV_TOKEN, 1)],
                      {"R4": _ld_bigint(st["acc_x"]).hex(), "R5": _ld_bigint(st["acc_y"]).hex(),
                       "R6": _bytes_const(bytes.fromhex(raw["boxId"])), "R7": _long_const(sp["shares"])}),
            # the owner: the NFT that owns the provision (its id is the pool box's id), and whatever did not fit
            _box_json(order_value - sp["taken_x"] - LD_PROVISION_MIN - LD_EXECUTOR_FEE, own, height,
                      [(raw["boxId"], 1), (LIT_ID, sp["excess_y"])]),
            _box_json(LD_EXECUTOR_FEE - LD_MAX_MINER_FEE, own, height),
            _box_json(LD_MAX_MINER_FEE, MINER_FEE_TREE, height)]
        _prove({"inputs": [{"boxId": raw["boxId"], "extension": {"0": "0201"}},            # pool: deposit
                           {"boxId": hexid(order_box.box_id), "extension": {}}],
                "dataInputs": [], "outputs": outputs}, [ErgoBox.from_json(raw), order_box], ctx, (0, 1))
    except SwapError:
        raise
    except Exception as e:
        sys.stderr.write("sigmaswap.py: lithos deposit simulation failed: %s: %s\n" % (type(e).__name__, str(e)[:300]))
        raise SwapError("This deposit could not be filled at the current pool price. Please preview it again.")
    return sp["shares"]


def cmd_ldlpbuild(req):
    """Place a deposit order: ERG and LIT in, and when a miner fills it, a provision plus the NFT that owns it."""
    from ergo_lib_python.chain import ErgoBox, ErgoBoxCandidate, NetworkPrefix, Token, TokenId
    from ergo_lib_python.ergo_tree import ErgoTree
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection
    cfg = req["cfg"]
    _need_v6()
    x = req_amount(req.get("amount"), "amount")
    max_y = req_amount(req.get("max_y"), "limit")
    min_shares = req_amount(req.get("min_shares"), "limit")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    fee = miner_fee(cfg)
    pool = ld_pool(cfg)
    supply = LD_LOCKED_LP - _ld_registers(pool.raw)["liquidity"]
    y = -(-x * pool.ry // pool.rx)
    sp = _ld_split(pool, supply, x, y)
    # the price may have moved since the preview: never take more LIT, or give fewer shares, than agreed
    if y > max_y or sp["shares"] < min_shares:
        raise SwapError("The pool price moved. Please preview again.")
    if min_shares < sp["shares"] * 95 // 100:
        raise SwapError("That minimum is too far below what the pool gives now. Preview again.")
    pk = bytes(address.ergo_tree())[1:]
    tree = ld_order_tree("deposit", pk, dx=x, shares=min_shares)
    back = ld_parse_tree(tree)
    if not back or not back["ours"] or back["kind"] != "deposit" or back["pk"] != pk or back["dx"] != x or back["shares"] != min_shares:
        raise SwapError("The order could not be prepared. Please try again.")
    box_value = x + LD_PROVISION_MIN + LD_EXECUTOR_FEE + LD_RETURNED

    def need_erg(toks):
        n = len(set(toks) - ({LIT_ID} if toks.get(LIT_ID) == y else set()))
        return box_value + fee + change_need(n)

    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    chosen, user_erg, user_toks = select_inputs(user_boxes, need_erg, LIT_ID, y)
    ctx, height = chain_context(cfg)
    order_out = ErgoBoxCandidate(value=box_value, script=ErgoTree.from_bytes(tree), creation_height=height,
                                 tokens=[Token(TokenId(LIT_ID), y)])
    out_toks = dict(user_toks)
    out_toks[LIT_ID] -= y
    change = user_outputs(address, height, out_toks, user_erg - box_value - fee)
    unsigned = TxBuilder(BoxSelection(chosen, []), [order_out] + change, height, fee, address).build()
    built = json.loads(unsigned.json())["outputs"][0]
    if built["ergoTree"] != tree.hex() or int(built["value"]) != box_value:
        raise SwapError("The order could not be prepared. Please try again.")
    order_box = ErgoBox.from_box_candidate(order_out, unsigned.id, 0)
    if ld_simulate_deposit(pool, order_box, x, y, address, ctx, height) != sp["shares"]:
        raise SwapError("The order could not be prepared. Please try again.")
    reduced = ReducedTransaction.from_unsigned_tx(unsigned, chosen, [], ctx)
    return {"reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"), "tx_id": hexid(unsigned.id),
            "address": addr_str, "amount_x": str(x), "amount_y": str(y), "shares": str(sp["shares"]),
            "min_shares": str(min_shares), "miner_fee": str(fee), "executor_fee": str(LD_EXECUTOR_FEE),
            "watch_box": hexid(chosen[0].box_id), "order_box": hexid(order_box.box_id), "kind": "deposit"}


def _ld_provisions(cfg):
    """Every open provision of the ERG:LIT pool: {owner NFT id: explorer box}."""
    found, offset = {}, 0
    while offset < 2000:
        page = explorer(cfg, "boxes/unspent/byTokenId/%s?limit=100&offset=%d" % (LD_PROV_TOKEN, offset)) or {}
        items = page.get("items") or []
        for b in items:
            try:
                if b.get("ergoTree") != LD_PROVISION_GUARD_TREE:
                    continue
                reg = b["additionalRegisters"]
                nft = reg["R6"]["renderedValue"]
                if is_hex64(nft):
                    b["_shares"] = int(reg["R7"]["renderedValue"])
                    b["_entry_x"] = int(reg["R4"]["renderedValue"].strip("CBigInt()"))
                    b["_entry_y"] = int(reg["R5"]["renderedValue"].strip("CBigInt()"))
                    found[nft] = b
            except (KeyError, TypeError, ValueError):
                continue
        offset += 100
        if len(items) < 100:
            break
    return found


def _ld_position(prov, pool, supply, st, vault):
    sh = prov["_shares"]
    return {"nft": prov["additionalRegisters"]["R6"]["renderedValue"], "box_id": prov["boxId"], "shares": str(sh),
            "value_erg": str(sh * pool.rx // supply), "value_lit": str(sh * pool.ry // supply),
            "share_bps": sh * 10000 // supply, "deposit": str(int(prov["value"])),
            # fees already moved to the vault: claimable now
            "claim_erg": str(max(0, sh * (vault["_acc_x"] - prov["_entry_x"]) // LD_ACC_SCALE)),
            "claim_lit": str(max(0, sh * (vault["_acc_y"] - prov["_entry_y"]) // LD_ACC_SCALE)),
            # fees earned since the last flush: still in the pool, claimable after the next flush
            "pending_erg": str(max(0, sh * (st["acc_x"] - max(vault["_acc_x"], prov["_entry_x"])) // LD_ACC_SCALE)),
            "pending_lit": str(max(0, sh * (st["acc_y"] - max(vault["_acc_y"], prov["_entry_y"])) // LD_ACC_SCALE))}


def cmd_ldpositions(req):
    """The liquidity positions an address owns: the provisions whose owner NFT it holds."""
    cfg = req["cfg"]
    _need_v6()
    address = req_address(req.get("address"))
    bal = explorer(cfg, "addresses/%s/balance/confirmed" % urllib.parse.quote(req["address"], safe="")) or {}
    held = set(t.get("tokenId") for t in bal.get("tokens") or [] if int(t.get("amount", 0)) == 1)
    pool = ld_pool(cfg)
    st = _ld_registers(pool.raw)
    supply = LD_LOCKED_LP - st["liquidity"]
    vault = ld_vault(cfg)
    provs = _ld_provisions(cfg)
    return {"positions": [_ld_position(p, pool, supply, st, vault) for nft, p in provs.items() if nft in held],
            "providers": len(provs)}


def cmd_ldclaim(req):
    """Collect a position's fees from the vault: the owner spends the box holding the NFT next to
    the vault and the provision. vault(claim, count 1), provision(claim) -> vault', provision', owner."""
    from ergo_lib_python.chain import ErgoBox, NetworkPrefix
    from ergo_lib_python.transaction import ReducedTransaction
    cfg = req["cfg"]
    _need_v6()
    nft = req_token_id(req.get("nft"), "position")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    fee = miner_fee(cfg)
    prov = _ld_provisions(cfg).get(nft)
    if not prov:
        raise SwapError("This position is no longer open.")
    vault = ld_vault(cfg)
    owed_x = prov["_shares"] * (vault["_acc_x"] - prov["_entry_x"]) // LD_ACC_SCALE
    owed_y = prov["_shares"] * (vault["_acc_y"] - prov["_entry_y"]) // LD_ACC_SCALE
    if vault["_acc_x"] <= prov["_entry_x"] and vault["_acc_y"] <= prov["_entry_y"]:
        raise SwapError("There is nothing to claim yet. Fees become claimable after the pool's next flush.")
    if owed_x < 0 or owed_y < 0 or int(vault["value"]) - owed_x < LD_VAULT_MIN:
        raise SwapError("The fee vault cannot pay this right now. Please try again later.")
    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    try:
        chosen, user_erg, user_toks = select_inputs(user_boxes, lambda t: fee + change_need(len(t) + 1) - owed_x, nft, 1)
    except SwapError:
        raise SwapError("This address does not hold the NFT that owns this position, or lacks ERG for the network fee.")
    ctx, height = chain_context(cfg)
    va = vault["assets"]
    vault_lit = int(va[1]["amount"]) if len(va) > 1 else 0
    if owed_y > vault_lit:
        raise SwapError("The fee vault cannot pay this right now. Please try again later.")
    vreg, preg = vault["additionalRegisters"], prov["additionalRegisters"]
    out_toks = dict(user_toks)
    out_toks[LIT_ID] = out_toks.get(LIT_ID, 0) + owed_y
    outputs = [
        _box_json(int(vault["value"]) - owed_x, vault["ergoTree"], height,
                  [(LD_VAULT_NFT, 1)] + ([(va[1]["tokenId"], vault_lit - owed_y)] if len(va) > 1 else []),
                  {"R4": vreg["R4"]["serializedValue"], "R5": vreg["R5"]["serializedValue"]}),
        _box_json(int(prov["value"]), prov["ergoTree"], height, [(LD_PROV_TOKEN, 1)],
                  {"R4": vreg["R4"]["serializedValue"], "R5": vreg["R5"]["serializedValue"],       # entries move up to the vault's
                   "R6": preg["R6"]["serializedValue"], "R7": preg["R7"]["serializedValue"]}),
    ] + _user_boxes_json(address, height, out_toks, user_erg + owed_x - fee) + [_box_json(fee, MINER_FEE_TREE, height)]
    logic = _bytes_const(bytes.fromhex(LD_PROVISION_LOGIC_VALUE))
    tx_json = {"inputs": [{"boxId": vault["boxId"], "extension": {"0": "0200", "1": "0402"}},       # vault: claim, one provision
                          {"boxId": prov["boxId"], "extension": {"0": "0200", "64": logic}}]        # provision: claim
                         + [{"boxId": hexid(b.box_id), "extension": {}} for b in chosen],
               "dataInputs": [], "outputs": outputs}
    clean = lambda b: {k: v for k, v in b.items() if not k.startswith("_")}     # noqa: E731
    boxes = [ErgoBox.from_json(clean(vault)), ErgoBox.from_json(clean(prov))] + chosen
    try:
        tx = _prove(tx_json, boxes, ctx, (0, 1))            # the vault and the provision both accept
        reduced = ReducedTransaction.from_unsigned_tx(tx, boxes, [], ctx)
    except Exception as e:
        sys.stderr.write("sigmaswap.py: lithos claim refused: %s: %s\n" % (type(e).__name__, str(e)[:300]))
        raise SwapError("The claim could not be prepared. The vault may have just changed; please try again.")
    return {"reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"), "tx_id": hexid(tx.id),
            "address": addr_str, "claim_erg": str(owed_x), "claim_lit": str(owed_y), "miner_fee": str(fee),
            "watch_box": vault["boxId"], "nft": nft}


def ld_simulate_redeem(pool, prov, order_box, address, ctx, height):
    """The fill of a redeem order as a miner would build it; pool, provision and order must all accept."""
    from ergo_lib_python.chain import ErgoBox
    raw = pool.raw
    try:
        st = _ld_registers(raw)
        supply = LD_LOCKED_LP - st["liquidity"]
        sh = prov["_shares"]
        out_x, out_y = sh * pool.rx // supply, sh * pool.ry // supply
        reg, a = raw["additionalRegisters"], raw["assets"]
        own = bytes(address.ergo_tree()).hex()
        order_value = int(json.loads(order_box.json())["value"])
        outputs = [
            _box_json(int(raw["value"]) - out_x, raw["ergoTree"], height,
                      [(a[0]["tokenId"], 1), (a[1]["tokenId"], int(a[1]["amount"]) - out_y), (a[2]["tokenId"], int(a[2]["amount"]) + 1)],
                      {"R4": _long_const(st["liquidity"] + sh), "R5": reg["R5"]["serializedValue"],
                       "R6": reg["R6"]["serializedValue"], "R7": reg["R7"]["serializedValue"], "R8": reg["R8"]["serializedValue"]}),
            _box_json(order_value + int(prov["value"]) + out_x - LD_EXECUTOR_FEE, own, height, [(LIT_ID, out_y)]),
            _box_json(LD_EXECUTOR_FEE - LD_MAX_MINER_FEE, own, height),
            _box_json(LD_MAX_MINER_FEE, MINER_FEE_TREE, height)]
        logic = _bytes_const(bytes.fromhex(LD_PROVISION_LOGIC_VALUE))
        clean = {k: v for k, v in prov.items() if not k.startswith("_")}
        _prove({"inputs": [{"boxId": raw["boxId"], "extension": {"0": "0202"}},                     # pool: redeem
                           {"boxId": prov["boxId"], "extension": {"0": "0201", "64": logic}},      # provision: redeem
                           {"boxId": hexid(order_box.box_id), "extension": {}}],
                "dataInputs": [], "outputs": outputs},
               [ErgoBox.from_json(raw), ErgoBox.from_json(clean), order_box], ctx, (0, 1, 2))
    except SwapError:
        raise
    except Exception as e:
        sys.stderr.write("sigmaswap.py: lithos redeem simulation failed: %s: %s\n" % (type(e).__name__, str(e)[:300]))
        raise SwapError("This position could not be closed right now. Please try again.")
    return out_x, out_y


def cmd_ldredeem(req):
    """Place a redeem order: the owner NFT goes into the order, a miner closes the provision and
    pays its share of both reserves, the 0.6 ERG deposit included. Unclaimed fees do NOT come along."""
    from ergo_lib_python.chain import ErgoBox, ErgoBoxCandidate, NetworkPrefix, Token, TokenId
    from ergo_lib_python.ergo_tree import ErgoTree
    from ergo_lib_python.transaction import ReducedTransaction, TxBuilder
    from ergo_lib_python.wallet import BoxSelection
    cfg = req["cfg"]
    _need_v6()
    nft = req_token_id(req.get("nft"), "position")
    address = req_address(req.get("address"))
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    fee = miner_fee(cfg)
    prov = _ld_provisions(cfg).get(nft)
    if not prov:
        raise SwapError("This position is no longer open.")
    pool = ld_pool(cfg)
    vault = ld_vault(cfg)
    st = _ld_registers(pool.raw)
    pos = _ld_position(prov, pool, LD_LOCKED_LP - st["liquidity"], st, vault)
    # closing forfeits whatever is still claimable, so unless the user insisted, make them claim first
    if not req.get("force") and (int(pos["claim_erg"]) > 1000000 or int(pos["claim_lit"]) > 0):
        raise SwapError("This position has unclaimed fees. Claim them first: closing a position leaves them behind for good.")
    pk = bytes(address.ergo_tree())[1:]
    tree = ld_order_tree("redeem", pk)
    back = ld_parse_tree(tree)
    if not back or not back["ours"] or back["kind"] != "redeem" or back["pk"] != pk:
        raise SwapError("The order could not be prepared. Please try again.")
    user_boxes = fetch_user_boxes(cfg, addr_str)
    if not user_boxes:
        raise SwapError("No funds found on this address.")
    try:
        chosen, user_erg, user_toks = select_inputs(user_boxes, lambda t: LD_RETURNED + fee + change_need(len(t)), nft, 1)
    except SwapError:
        raise SwapError("This address does not hold the NFT that owns this position, or lacks ERG for the network fee.")
    ctx, height = chain_context(cfg)
    order_out = ErgoBoxCandidate(value=LD_RETURNED, script=ErgoTree.from_bytes(tree), creation_height=height,
                                 tokens=[Token(TokenId(nft), 1)])
    out_toks = dict(user_toks)
    out_toks[nft] -= 1
    change = user_outputs(address, height, out_toks, user_erg - LD_RETURNED - fee)
    unsigned = TxBuilder(BoxSelection(chosen, []), [order_out] + change, height, fee, address).build()
    built = json.loads(unsigned.json())["outputs"][0]
    if built["ergoTree"] != tree.hex() or [x["tokenId"] for x in built["assets"]] != [nft]:
        raise SwapError("The order could not be prepared. Please try again.")
    order_box = ErgoBox.from_box_candidate(order_out, unsigned.id, 0)
    out_x, out_y = ld_simulate_redeem(pool, prov, order_box, address, ctx, height)
    reduced = ReducedTransaction.from_unsigned_tx(unsigned, chosen, [], ctx)
    return {"reduced_tx": base64.urlsafe_b64encode(bytes(reduced)).decode("ascii"), "tx_id": hexid(unsigned.id),
            "address": addr_str, "nft": nft, "out_erg": str(out_x + int(prov["value"]) - LD_EXECUTOR_FEE + LD_RETURNED),
            "out_lit": str(out_y), "miner_fee": str(fee), "executor_fee": str(LD_EXECUTOR_FEE),
            "watch_box": hexid(chosen[0].box_id), "order_box": hexid(order_box.box_id), "kind": "redeem"}


def cmd_lithosstats(req):
    """Pool figures from Lithos's own public statistics API, passed through as they are."""
    res = {}
    for key, path in (("stats", "/stats"), ("supply", "/supply/lit"), ("blocks", "/blocks")):
        try:
            doc = http_json(LITHOS_API + path)
            if isinstance(doc, dict):
                res[key] = doc
        except Exception as e:
            sys.stderr.write("sigmaswap.py lithosstats: %s: %s: %s\n" % (path, type(e).__name__, str(e)[:160]))
    if "stats" not in res:
        raise SwapError("Lithos is not answering right now. Please try again.")
    if "blocks" in res:
        res["blocks"] = (res["blocks"].get("blocks") or [])[:8]
    return res


def cmd_lithosminer(req):
    """A miner address's recent Lithos activity, from its own transactions on the chain.

    payout:  a rollup at the Payout contract paid this address          (net ERG and LIT)
    found:   this address found a block (the finder's share when a collateral box is spent)
    proof:   this address submitted a share proof to a rollup           (its cost)
    """
    cfg = req["cfg"]
    address = req_address(req.get("address"))
    from ergo_lib_python.chain import NetworkPrefix
    addr = address.to_str(NetworkPrefix.Mainnet)

    def held(boxes, token=None):
        mine = [b for b in boxes if b.get("address") == addr]
        if token is None:
            return sum(int(b.get("value", 0)) for b in mine)
        return sum(int(a.get("amount", 0)) for b in mine for a in b.get("assets") or [] if a.get("tokenId") == token)

    events, scanned, oldest, total = [], 0, 0, 0
    for offset in (0, 100):          # the full format is needed (who the other boxes belong to), about 2 MB a page
        page = explorer(cfg, "addresses/%s/transactions?limit=100&offset=%d"
                        % (urllib.parse.quote(addr, safe=""), offset), 40) or {}
        items = page.get("items") or []
        total = int(page.get("total") or 0)
        for t in items:
            scanned += 1
            ins, outs = t.get("inputs") or [], t.get("outputs") or []
            oldest = int(t.get("timestamp") or 0) // 1000 or oldest
            if not ins:
                continue
            first = ins[0]
            if first.get("address") == LITHOS_PAYOUT_ADDRESS:
                kind = "payout"
            elif any(a.get("tokenId") == LITHOS_COLLATERAL_TOKEN for a in first.get("assets") or []):
                kind = "found"
            elif first.get("address") == LITHOS_HOLDING_ADDRESS:
                kind = "proof"
            else:
                continue
            erg, lit = held(outs) - held(ins), held(outs, LIT_ID) - held(ins, LIT_ID)
            # a block somebody else found can pay this address as lender or bidder: only the finder
            # receives the finder's LIT with nothing of their own going in
            if kind == "found" and not (lit > 0 and held(ins) == 0):
                continue
            events.append({"kind": kind, "erg": str(erg), "lit": str(lit), "ts": int(t.get("timestamp") or 0) // 1000,
                           "height": int(t.get("inclusionHeight") or 0), "tx": t.get("id")})
        if len(items) < 100:
            break
    return {"address": addr, "events": events[:400], "scanned": scanned, "total": total, "since": oldest}


# ------------------------------------------------------------ SigmaStats ----
# sigmastats/: network figures, a portfolio view and price alerts. Read-only.

def cmd_netstats(req):
    """Network figures from the explorer. Every part is optional: one slow endpoint must not blank the screen."""
    cfg = req["cfg"]
    base = cfg["explorer_url"].rstrip("/")
    res = {}

    def part(fn):
        try:
            fn()
        except Exception as e:
            sys.stderr.write("sigmaswap.py netstats: %s: %s\n" % (type(e).__name__, str(e)[:200]))

    def headers():
        h = (explorer(cfg, "blocks/headers?limit=1") or {}).get("items") or []
        if h:
            res["height"], res["block_ts"] = int(h[0]["height"]), int(h[0]["timestamp"]) // 1000

    def stats():
        st = http_json(base + "/api/v0/stats") or {}
        b, t, m = st.get("blockSummary") or {}, st.get("transactionsSummary") or {}, st.get("miningCost") or {}
        res.update({"blocks_24h": int(b.get("total", 0)), "avg_block_ms": int(b.get("averageMiningTime", 0)),
                    "tx_24h": int(t.get("total", 0)), "difficulty": int(m.get("difficulty", 0)),
                    "hashrate": int(m.get("hashRate", 0))})

    def info():
        res["supply"] = str(int((http_json(base + "/api/v0/info") or {}).get("supply", 0)))

    def mempool():
        res["mempool"] = int((http_json(base + "/api/v0/transactions/unconfirmed?limit=1") or {}).get("total", 0))

    for fn in (headers, stats, info, mempool):
        part(fn)
    if "height" not in res:
        raise SwapError("The Ergo explorer is not answering right now. Please try again.")
    return res


def cmd_portfolio(req):
    """Confirmed balance of one address: ERG and tokens (names are untrusted on-chain text)."""
    cfg = req["cfg"]
    address = req_address(req.get("address"))
    from ergo_lib_python.chain import NetworkPrefix
    addr_str = address.to_str(NetworkPrefix.Mainnet)
    bal = explorer(cfg, "addresses/%s/balance/confirmed" % urllib.parse.quote(addr_str, safe="")) or {}
    toks = []
    for t in (bal.get("tokens") or [])[:1000]:
        try:
            if not is_hex64(t.get("tokenId")) or int(t["amount"]) <= 0:
                continue
            a = _asset({"id": t["tokenId"], "ticker": t.get("name"), "decimals": t.get("decimals")})
            toks.append({"id": a["id"], "name": a["ticker"], "decimals": a["decimals"], "amount": str(int(t["amount"]))})
        except (KeyError, TypeError, ValueError):
            continue
    return {"address": addr_str, "erg": str(int(bal.get("nanoErgs", 0))), "tokens": toks}


SIGUSD_ID = "03faf2cb329f2e90d6d23b58d91bbb6c046aa143261cc21f52fbe2824bfcbf04"


def alert_prices(pools):
    """{asset id: price as a 1e-9 fixed-point int}. For a token: ERG per 1 token, from its
    ERG pool. For ERG itself: SigUSD per 1 ERG, from the ERG/SigUSD pool."""
    prices = {}
    for p in pools:
        try:
            if p["x"]["id"] != ERG_ID:
                continue
            xa, ya, dec = int(p["xa"]), int(p["ya"]), int(p["y"]["decimals"])
            if xa <= 0 or ya <= 0:
                continue
            prices[p["y"]["id"]] = xa * 10 ** dec // ya           # nanoERG per whole token = 1e-9 ERG units
            if p["y"]["id"] == SIGUSD_ID:
                prices[ERG_ID] = ya * 10 ** (18 - dec) // xa       # 1e-9 SigUSD per whole ERG
        except (KeyError, TypeError, ValueError):
            continue
    return prices


def cmd_alerts(req):
    """One pass over the price alerts (CACHE_DIR/alerts/<installation>.json, written by
    sigmastats/api.pl). An alert that is reached is appended to <installation>.hits; the
    alert file itself is never written here, so the two sides cannot overwrite each other."""
    import os
    import time
    directory = req.get("dir") or ""
    if not os.path.isdir(directory):
        return {"checked": 0, "hit": 0}
    pending = []
    for name in sorted(os.listdir(directory))[:20000]:
        if not (name.endswith(".json") and len(name) == 37):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path) as f:
                alerts = json.load(f).get("alerts") or []
            done = set()
            hits_path = path[:-5] + ".hits"
            if os.path.exists(hits_path):
                with open(hits_path) as f:
                    for line in f:
                        try:
                            done.add(json.loads(line).get("id"))
                        except ValueError:
                            pass
            for a in alerts[:20]:
                if a.get("id") not in done and is_hex64(a.get("asset")) and a.get("dir") in ("above", "below"):
                    pending.append((hits_path, a["id"], a["asset"], a["dir"], int(a["price"])))
        except Exception as e:
            sys.stderr.write("sigmaswap.py alerts: %s skipped: %s\n" % (name[:12], type(e).__name__))
    if not pending:
        return {"checked": 0, "hit": 0}
    prices = alert_prices(cmd_pools({"cfg": req["cfg"]})["pools"])
    hit = 0
    for hits_path, alert_id, asset, direction, target in pending:
        now = prices.get(asset)
        if now is None or not ((direction == "above" and now >= target) or (direction == "below" and now <= target)):
            continue
        with open(hits_path, "a") as f:
            f.write(json.dumps({"id": alert_id, "ts": int(time.time()), "price": str(now)}) + "\n")
        hit += 1
    return {"checked": len(pending), "hit": hit}


# ------------------------------------------------------------------ main ----

def cmd_health(req):
    """Deploy gate: proves python + ergo-lib-python load under the web server."""
    import ergo_lib_python
    res = {"python": "%d.%d.%d" % sys.version_info[:3], "ergo_lib": ergo_lib_python.__version__}
    if req.get("network"):
        info = explorer(req["cfg"], "info") or {}
        res["height"] = int(info.get("height", 0))
    return res


COMMANDS = {"health": cmd_health, "pools": cmd_pools, "quote": cmd_quote, "balance": cmd_balance,
            "build": cmd_build, "orders": cmd_orders, "refund": cmd_refund, "status": cmd_status,
            "mint": cmd_mint, "upload": cmd_upload,
            "lpquote": cmd_lpquote, "lpbuild": cmd_lpbuild, "lpmap": cmd_lpmap, "lppositions": cmd_lppositions,
            "bridgequote": cmd_bridgequote, "bridgebuild": cmd_bridgebuild,
            "netstats": cmd_netstats, "portfolio": cmd_portfolio,
            "ldlpquote": cmd_ldlpquote, "ldlpbuild": cmd_ldlpbuild, "ldpositions": cmd_ldpositions, "ldclaim": cmd_ldclaim,
            "ldredeem": cmd_ldredeem, "ldquote": cmd_ldquote, "ldbuild": cmd_ldbuild, "ldorders": cmd_ldorders, "ldcancel": cmd_ldcancel,
            "ldstatus": cmd_ldstatus, "lithosstats": cmd_lithosstats, "lithosminer": cmd_lithosminer,
            "szcoins": cmd_szcoins, "szquote": cmd_szquote, "szorder": cmd_szorder, "szstatus": cmd_szstatus}


def main():
    try:
        cmd = sys.argv[1] if len(sys.argv) > 1 else ""
        if cmd == "bot":
            return bot_main()
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
