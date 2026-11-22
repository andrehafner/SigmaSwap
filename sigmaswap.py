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


def add_amounts(pool, dx):
    """Depositing `dx` of X: the matching Y (rounded up so X is the binding side) and LP received."""
    _x, X, _y, Y, _lp, supply = pool_state(pool)
    if supply <= 0:
        raise SwapError("This pool has no liquidity yet.")
    dy = -(-dx * Y // X)
    lp = min(dx * supply // X, dy * supply // Y)
    if dy <= 0 or lp <= 0:
        raise SwapError("That amount is too small for this pool.")
    return dy, lp


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
    """Price an add (amount of X in) or a remove (amount of LP in)."""
    cfg = req["cfg"]
    pool = load_pool(cfg, req_token_id(req.get("pool"), "pool"))
    res = _lp_info(pool)
    amount = req_amount(req.get("amount"), "amount")
    if req.get("action") == "remove":
        x_out, y_out = remove_amounts(pool, amount)
        res.update({"lp_in": str(amount), "x_out": str(x_out), "y_out": str(y_out)})
    else:
        dy, lp = add_amounts(pool, amount)
        supply = int(res["supply"])
        res.update({"x_in": str(amount), "y_in": str(dy), "lp_out": str(lp),
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
        dy, lp = add_amounts(pool, amount)
        # the price may have moved since the preview: never take more Y, or give fewer LP, than agreed
        if dy > req_amount(req.get("max_y"), "limit") or lp < req_amount(req.get("min_lp"), "limit"):
            raise SwapError("The pool price moved. Please preview again.")
        dx, dlp = amount, lp
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
            "netstats": cmd_netstats, "portfolio": cmd_portfolio}


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
