"""Who is leaving rETH?

Reads every rETH Transfer event between the study's first and last weekly blocks,
nets them by address, and reports how concentrated the outflow is: the top leavers
and their balances at both ends, the share the largest ones account for, what drove
the biggest outflow weeks, and who each top address is.

Data sources: free public Ethereum endpoints (all probed at start; the list is
refreshed from Chainlist) and, when set, keyed APIs: ETHERSCAN_KEY, ALCHEMY_KEY,
INFURA_KEY, BLOCKSCOUT_KEY. Each week of transfers must reconcile with the rETH supply
change in the weekly panel (mints minus burns). A week that does not reconcile is
fetched again from another source. Reconciled weeks are cached in cache/transfers/.

Run after the notebook (it reads reth_study/blocks.csv and weekly_panel.csv):
    python analysis/holders.py
"""
import concurrent.futures as cf
import os
import re
import threading
import time
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import requests

OUT_DIR = os.environ.get("OUT_DIR", "reth_study")
CACHE_DIR = os.environ.get("TRANSFER_CACHE", os.path.join("cache", "transfers"))
RETH = "0xae78736cd615f374d3085123a210448e74fc6393"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
KEYS = {k: os.environ.get(k, "").strip() for k in ("ETHERSCAN_KEY", "ALCHEMY_KEY", "INFURA_KEY", "BLOCKSCOUT_KEY")}
CHAINLIST = "https://raw.githubusercontent.com/DefiLlama/chainlist/main/constants/extraRpcs.js"
PUBLIC_RPCS = [  # Chainlist, October 2026; refreshed at run time
    "https://eth.drpc.org", "https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com",
    "https://public.1rpc.io/eth", "https://rpc.mevblocker.io", "https://rpc.flashbots.net",
    "https://eth.rpc.blxrbdn.com", "https://virginia.rpc.blxrbdn.com", "https://uk.rpc.blxrbdn.com",
    "https://singapore.rpc.blxrbdn.com", "https://cloudflare-eth.com", "https://eth-mainnet.public.blastapi.io",
    "https://ethereum-public.nodies.app", "https://ethereum.public.blockpi.network/v1/rpc/public",
    "https://eth-mainnet.g.alchemy.com/v2/demo", "https://rpc.eth.gateway.fm", "https://eth.meowrpc.com",
    "https://mainnet.gateway.tenderly.co", "https://gateway.tenderly.co/public/mainnet",
    "https://api.zan.top/eth-mainnet", "https://eth.merkle.io", "https://rpc.polysplit.cloud/v1/chain/1",
    "https://ethereum-mainnet.gateway.tatum.io", "https://public-eth.nownodes.io", "https://eth.blockrazor.xyz",
    "https://0xrpc.io/eth", "https://eth.api.onfinality.io/public", "https://ethereum-json-rpc.stakely.io",
    "https://eth.api.pocket.network", "https://rpc.fullsend.to", "https://mainnet.rpc.sentio.xyz",
    "https://rpc.swiftnodes.io/rpc/eth", "https://rpc-eth.blockmachine.io", "https://mainnet.eth.cloud.ava.do",
    "https://ethereumnodelight.app.runonflux.io", "https://xrpc.cl/eth", "https://rpc.hostdefi.com/api/rpc/ethereum",
    "https://lb.routeme.sh/rpc/evm/1", "https://rpc.nodeflare.app/eth/public",
]
LABELS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "labels.csv")
TOP_N = 30
SATURN1 = "2026-02-18"
PROBE_LADDER = (200, 2_000, 10_000, 50_000)
MAX_WORKERS = 12
FETCH_MINUTES = float(os.environ.get("FETCH_MINUTES", "95"))
UA = {"User-Agent": "reth-competitiveness-study (github.com/NishikawaButterfly/reth-competitiveness-study)"}
COLS = ["block", "log_index", "tx", "from", "to", "amount"]
RATE_RE = re.compile(r"rate.?limit|too many requests|quota|throttl|capacity|\b429\b", re.I)

DEBUG_LINES = []


def redact(text):
    """Remove API keys from messages before they are printed or written to the repository."""
    text = str(text)
    for v in KEYS.values():
        if v:
            text = text.replace(v, "***")
    return re.sub(r"(/v[123]/|api_?key=)[A-Za-z0-9_-]{8,}", r"\1***", text, flags=re.I)


def dbg(msg):
    msg = redact(msg)
    print(msg, flush=True)
    DEBUG_LINES.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")


def write_debug():
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "holders_debug.log"), "w") as f:
        f.write("\n".join(DEBUG_LINES[-600:]) + "\n")


def pause(seconds):
    time.sleep(seconds)


class RateLimited(Exception):
    pass


class TooMany(Exception):
    """A capped API returned its maximum number of logs: the range must be split."""


def hexint(x):
    if isinstance(x, (int, np.integer)):
        return int(x)
    x = str(x)
    if x.lower().startswith("0x"):
        return int(x, 16) if len(x) > 2 else 0
    return int(x)


def norm(lg):
    """One Transfer log (JSON-RPC or Etherscan format) as a row of COLS."""
    t = lg["topics"]
    return (hexint(lg["blockNumber"]), hexint(lg.get("logIndex", 0)), str(lg.get("transactionHash", "")),
            "0x" + str(t[1])[-40:].lower(), "0x" + str(t[2])[-40:].lower(), hexint(lg["data"]) / 1e18)


def safe_label(url):
    """Host plus a short path, with anything that looks like a key removed."""
    p = urlparse(url)
    path = "/".join(seg if not re.search(r"[0-9a-f]{12,}|[0-9a-f-]{30,}", seg, re.I) else "..."
                    for seg in p.path.strip("/").split("/") if seg)
    return p.netloc + ("/" + path if path else "")


class RpcSource:
    """A JSON-RPC endpoint."""
    cap = None

    def __init__(self, url, label=None, keyed=False, pause=0.05):
        self.url, self.label, self.keyed, self.pause = url, label or safe_label(url), keyed, pause
        self.max_range = self.rng = 0
        self.session = requests.Session()
        self.session.headers.update(UA)
        self.n = 0

    def call(self, method, params, timeout=45):
        self.n += 1
        r = self.session.post(self.url, json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params},
                              timeout=timeout)
        if r.status_code == 429:
            raise RateLimited("HTTP 429")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        js = r.json()
        if isinstance(js, dict) and js.get("error"):
            msg = str(js["error"])[:200]
            if RATE_RE.search(msg):
                raise RateLimited(msg)
            raise RuntimeError(msg)
        return js["result"]

    def logs(self, lo, hi, timeout=45):
        res = self.call("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi), "address": RETH,
                                         "topics": [TRANSFER_TOPIC]}], timeout=timeout)
        if not isinstance(res, list):
            raise RuntimeError(f"unexpected result: {str(res)[:100]}")
        return [norm(x) for x in res]

    def eth_call(self, data, block, timeout=30):
        return self.call("eth_call", [{"to": RETH, "data": data}, hex(block)], timeout=timeout)


class ApiSource:
    """An Etherscan-compatible logs API (Etherscan, Blockscout): at most `cap` logs per call."""
    cap = 1000

    def __init__(self, label, base, params, keyed=False, pause=0.25):
        self.label, self.base, self.params, self.keyed, self.pause = label, base, params, keyed, pause
        self.max_range, self.rng = 50_000, 5_000
        self.session = requests.Session()
        self.session.headers.update(UA)

    def get(self, params, timeout):
        r = self.session.get(self.base, params=params, timeout=timeout)
        if r.status_code == 429:
            raise RateLimited("HTTP 429")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        return r.json()

    def logs(self, lo, hi, timeout=60):
        js = self.get(dict(self.params, module="logs", action="getLogs", fromBlock=lo, toBlock=hi,
                           address=RETH, topic0=TRANSFER_TOPIC), timeout)
        res = js.get("result")
        if isinstance(res, list):
            if len(res) >= self.cap:
                raise TooMany(len(res))
            return [norm(x) for x in res]
        text = f"{js.get('message', '')} {res}"
        if re.search(r"no (records|logs)", text, re.I):
            return []
        if RATE_RE.search(text):
            raise RateLimited(text[:150])
        raise RuntimeError(text[:200])


def chainlist_rpcs():
    """Public Ethereum mainnet endpoints listed on Chainlist."""
    try:
        s = requests.get(CHAINLIST, timeout=20, headers=UA).text
        m = re.search(r"\n\s*1:\s*\{", s)
        if not m:
            return []
        rest = s[m.end():]
        nxt = re.search(r"\n\s*\d+:\s*\{", rest)
        block = rest[:nxt.start()] if nxt else rest[:20_000]
        return [u for u in re.findall(r'"(https://[^"\s]+)"', block) if "${" not in u]
    except Exception as e:
        dbg(f"Chainlist not reachable ({type(e).__name__}); using the built-in list")
        return []


def build_sources():
    out = []
    if KEYS["ALCHEMY_KEY"]:
        out.append(RpcSource(f"https://eth-mainnet.g.alchemy.com/v2/{KEYS['ALCHEMY_KEY']}", "Alchemy (key)", True))
    if KEYS["INFURA_KEY"]:
        out.append(RpcSource(f"https://mainnet.infura.io/v3/{KEYS['INFURA_KEY']}", "Infura (key)", True))
    if KEYS["ETHERSCAN_KEY"]:
        out.append(ApiSource("Etherscan (key)", "https://api.etherscan.io/v2/api",
                             {"chainid": 1, "apikey": KEYS["ETHERSCAN_KEY"]}, keyed=True, pause=0.35))
    if KEYS["BLOCKSCOUT_KEY"]:
        out.append(ApiSource("Blockscout (key)", "https://api.blockscout.com/v2/api",
                             {"chain_id": 1, "apikey": KEYS["BLOCKSCOUT_KEY"]}, keyed=True, pause=0.25))
    out.append(ApiSource("eth.blockscout.com", "https://eth.blockscout.com/api", {}, pause=0.3))
    seen = set()
    for u in PUBLIC_RPCS + chainlist_rpcs():
        key = u.rstrip("/").lower()
        if key in seen or "virtual." in key:  # Tenderly virtual testnets are forks, not mainnet
            continue
        seen.add(key)
        out.append(RpcSource(u))
    labels = {}
    for s in out:  # unique labels
        n = labels.get(s.label, 0)
        labels[s.label] = n + 1
        if n:
            s.label = f"{s.label} #{n + 1}"
    return out


def probe(src, start, logs=True):
    """Can this source serve rETH Transfer logs from the start of the window, and over how many blocks?"""
    rec = {"source": src.label, "type": "api" if src.cap else "rpc", "keyed": src.keyed, "chain_ok": False,
           "max_range": 0, "probe_logs": np.nan, "seconds": np.nan, "error": ""}
    try:
        if isinstance(src, RpcSource):
            cid = hexint(src.call("eth_chainId", [], timeout=15))
            if cid != 1:
                raise RuntimeError(f"chain id {cid}")
            rec["chain_ok"] = True
        if logs:
            for rng in (PROBE_LADDER if src.cap is None else PROBE_LADDER[:1]):
                t = time.time()
                got = src.logs(start, start + rng - 1, timeout=30)
                if rng == PROBE_LADDER[0]:
                    rec["probe_logs"], rec["seconds"] = len(got), round(time.time() - t, 2)
                rec["max_range"] = rng
    except Exception as e:
        rec["error"] = redact(f"{type(e).__name__}: {str(e)[:160]}")
    return rec


def probe_all(cands, start, logs=True):
    with cf.ThreadPoolExecutor(24) as ex:
        recs = list(ex.map(lambda s: probe(s, start, logs), cands))
    df = pd.DataFrame(recs)
    df["usable"] = False
    if logs:
        pos = df.loc[(df["max_range"] > 0) & (df["probe_logs"] > 0), "probe_logs"]
        if len(pos):
            consensus = int(pos.mode().iloc[0])
            df["usable"] = (df["max_range"] > 0) & (df["probe_logs"] == consensus)
            bad = (df["max_range"] > 0) & ~df["usable"]
            df.loc[bad, "error"] = (df.loc[bad, "error"] + f" probe returned a different count than "
                                    f"the consensus ({consensus})").str.strip()
    for s, (_, r) in zip(cands, df.iterrows()):
        if isinstance(s, RpcSource) and r["max_range"]:
            s.max_range = s.rng = int(r["max_range"])
    return df


def fetch_range(src, lo, hi):
    """All rETH Transfer logs in [lo, hi] from one source, adapting the block range."""
    rows, b, fails = [], lo, 0
    while b <= hi:
        top = min(b + max(1, src.rng) - 1, hi)
        try:
            got = src.logs(b, top)
        except TooMany:
            if src.rng <= 1:
                raise RuntimeError(f"more than {src.cap} transfers in block {b}")
            src.rng = max(1, src.rng // 2)
            continue
        except RateLimited:
            fails += 1
            if fails > 8:
                raise
            pause(min(60, 2 ** fails))
            continue
        except Exception:
            fails += 1
            if fails > 5:
                raise
            src.rng = max(10, src.rng // 2)
            pause(fails)
            continue
        fails = 0
        rows.extend(got)
        b = top + 1
        if src.cap is None or len(got) < src.cap // 2:
            src.rng = min(src.max_range, int(src.rng * 1.5) + 1)
        pause(src.pause)
    return rows


def to_frame(rows):
    df = pd.DataFrame(rows, columns=COLS)
    return df.drop_duplicates(["block", "log_index"]).sort_values(["block", "log_index"]).reset_index(drop=True)


def check(df, expected):
    """Mints minus burns in the week must equal the supply change in the panel."""
    minted = float(df.loc[df["from"] == ZERO, "amount"].sum())
    burned = float(df.loc[df["to"] == ZERO, "amount"].sum())
    ok = abs((minted - burned) - expected) <= 1e-6
    return ok, f"mints - burns {minted - burned:,.6f} vs supply change {expected:,.6f}"


def week_path(lo, hi):
    return os.path.join(CACHE_DIR, f"w{lo}-{hi}.csv.gz")


def save_week(df, lo, hi):
    df.to_csv(week_path(lo, hi), index=False)


def load_week(lo, hi):
    p = week_path(lo, hi)
    if not os.path.exists(p):
        return None
    try:
        return pd.read_csv(p, dtype={"tx": str, "from": str, "to": str})
    except Exception:
        return None


def fetch_all(sources, weeks, expected):
    """Fetch every week that is not cached. One worker per source; a week that fails or
    does not reconcile goes back to the queue for a source that has not tried it yet."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    data, origin, pending = {}, {}, []
    for k, lo, hi in weeks:
        df = load_week(lo, hi)
        if df is not None and check(df, expected[k])[0]:
            data[k], origin[k] = df, "cache"
        else:
            pending.append({"k": k, "lo": lo, "hi": hi, "tried": set()})
    dbg(f"Weeks from cache: {len(data)}; to fetch: {len(pending)} with {len(sources)} sources")
    if not pending or not sources:
        return data, origin, pending
    total = len(pending)
    lock = threading.Lock()
    state = {"busy": 0, "done": 0}
    deadline = time.time() + FETCH_MINUTES * 60

    def take(label):
        with lock:
            if time.time() < deadline:
                for j, item in enumerate(pending):
                    if label not in item["tried"]:
                        state["busy"] += 1
                        return pending.pop(j), False
            return None, state["busy"] == 0

    def worker(src):
        strikes = 0
        while strikes < 3:
            item, finished = take(src.label)
            if item is None:
                if finished:
                    return
                time.sleep(0.2)
                continue
            k, lo, hi = item["k"], item["lo"], item["hi"]
            try:
                t = time.time()
                df = to_frame(fetch_range(src, lo, hi))
                ok, msg = check(df, expected[k])
                if not ok:
                    raise RuntimeError(f"does not reconcile: {msg}")
                save_week(df, lo, hi)
                with lock:
                    data[k], origin[k] = df, src.label
                    state["done"] += 1
                    n = state["done"]
                strikes = 0
                if n % 10 == 0 or n == total:
                    dbg(f"  {n}/{total} weeks fetched (last: {src.label}, {len(df):,} transfers, "
                        f"{time.time() - t:.0f}s)")
            except Exception as e:
                strikes += 1
                dbg(f"{src.label}: week {k} (blocks {lo}-{hi}): {type(e).__name__}: {str(e)[:160]}")
                with lock:
                    item["tried"].add(src.label)
                    pending.append(item)
            finally:
                with lock:
                    state["busy"] -= 1
        dbg(f"{src.label}: retired after 3 failures in a row")

    with cf.ThreadPoolExecutor(len(sources)) as ex:
        for f in [ex.submit(worker, s) for s in sources]:
            f.result()
    return data, origin, pending


# ---- analysis ---------------------------------------------------------------------------

def net_by_address(tr):
    sent = tr.groupby("from")["amount"].sum()
    received = tr.groupby("to")["amount"].sum()
    n_out = tr.groupby("from").size()
    n_in = tr.groupby("to").size()
    df = pd.DataFrame({"sent": sent, "received": received, "n_out": n_out, "n_in": n_in}).fillna(0)
    df["net"] = df["received"] - df["sent"]
    df.index.name = "address"
    return df


def concentration(net, window):
    others = net[net.index != ZERO]
    leavers = others[others["net"] < 0].sort_values("net")
    gainers = others[others["net"] > 0]
    total = float(-leavers["net"].sum())
    out = {"window": window,
           "supply_change_reth": float(-net.loc[ZERO, "net"]) if ZERO in net.index else 0.0,
           "total_decrease_reth": total, "total_increase_reth": float(gainers["net"].sum()),
           "leaving_addresses": int(len(leavers)), "gaining_addresses": int(len(gainers))}
    for k in (1, 5, 10, 20, 50, 100):
        out[f"top{k}_share"] = float(-leavers["net"].head(k).sum() / total) if total else np.nan
    return out


def load_labels():
    """Curated labels: analysis/labels.csv with columns address, label, entity, category, evidence."""
    if not os.path.exists(LABELS_FILE):
        return {}
    lab = pd.read_csv(LABELS_FILE, dtype=str).fillna("")
    out = {}
    for _, r in lab.iterrows():
        out[r["address"].lower()] = {"label": r.get("label", ""), "entity": r.get("entity", "") or r.get("label", ""),
                                     "category": r.get("category", ""), "evidence": r.get("evidence", "")}
    return out


BS_FAILS = [0]


def blockscout_info(addr):
    """Name, public tags, proxy implementation and ENS name from Blockscout, if it answers."""
    if BS_FAILS[0] >= 3:
        return {}
    urls = []
    if KEYS["BLOCKSCOUT_KEY"]:
        urls.append((f"https://api.blockscout.com/1/api/v2/addresses/{addr}", {"apikey": KEYS["BLOCKSCOUT_KEY"]}))
    urls.append((f"https://eth.blockscout.com/api/v2/addresses/{addr}", {}))
    for url, params in urls:
        pause(0.25)
        try:
            r = requests.get(url, params=params, timeout=20, headers={**UA, "accept": "application/json"})
            if r.status_code != 200:
                continue
            js = r.json()
        except Exception:
            continue
        BS_FAILS[0] = 0
        tags = [t.get("name") for t in (js.get("metadata") or {}).get("tags") or [] if t.get("name")]
        tags += [t.get("display_name") for t in js.get("public_tags") or [] if t.get("display_name")]
        impl = [i.get("name") for i in js.get("implementations") or [] if i.get("name")]
        name = js.get("name") or ""
        if impl and impl[0] != name:
            name = f"{name} -> {impl[0]}" if name else impl[0]
        return {"is_contract": js.get("is_contract"), "name": name,
                "tags": "; ".join(dict.fromkeys(tags)), "ens": js.get("ens_domain_name") or ""}
    BS_FAILS[0] += 1
    if BS_FAILS[0] == 3:
        dbg("Blockscout address lookups failing; names will come from labels.csv only")
    return {}


def etherscan_name(addr):
    if not KEYS["ETHERSCAN_KEY"]:
        return ""
    try:
        def get(a):
            r = requests.get("https://api.etherscan.io/v2/api", timeout=20, headers=UA,
                             params={"chainid": 1, "module": "contract", "action": "getsourcecode",
                                     "address": a, "apikey": KEYS["ETHERSCAN_KEY"]})
            res = r.json().get("result")
            return res[0] if isinstance(res, list) and res else {}
        res = get(addr)
        name = res.get("ContractName") or ""
        if res.get("Proxy") == "1" and res.get("Implementation"):
            pause(0.35)
            impl = get(res["Implementation"]).get("ContractName") or ""
            if impl:
                name = f"{name} -> {impl}" if name else impl
        pause(0.35)
        return name
    except Exception:
        return ""


def sourcify_name(addr):
    """Contract name from Sourcify (verified contracts), following a proxy to its implementation."""
    def get(a):
        r = requests.get(f"https://sourcify.dev/server/v2/contract/1/{a}", timeout=20, headers=UA,
                         params={"fields": "compilation,proxyResolution"})
        return r.json() if r.status_code == 200 else {}
    try:
        js = get(addr)
        name = (js.get("compilation") or {}).get("name") or ""
        pr = js.get("proxyResolution") or {}
        if pr.get("isProxy") and pr.get("implementations"):
            impl = (get(pr["implementations"][0]["address"]).get("compilation") or {}).get("name") or ""
            if impl and impl != name:
                name = f"{name} -> {impl}" if name else impl
        return name
    except Exception:
        return ""


ENS_REGISTRY = "0x00000000000c2e074ec69a0dfb2997ba6c7d2e1e"


def view(rpcs, to, data):
    """eth_call at the latest block; None when the function does not exist or returns nothing."""
    for src in rpcs[:3]:
        try:
            res = src.call("eth_call", [{"to": to, "data": data}, "latest"], timeout=20)
            return res if isinstance(res, str) and len(res) > 2 else None
        except Exception as e:
            if re.search(r"revert|execution|invalid opcode", str(e), re.I):
                return None
            continue
    return None


def decode_string(res):
    """An ABI string, or a bytes32 for old tokens."""
    try:
        b = bytes.fromhex(res[2:])
        if len(b) == 32:
            return b.rstrip(b"\0").decode("utf-8", "ignore").strip()
        off = int.from_bytes(b[:32], "big")
        n = int.from_bytes(b[off:off + 32], "big")
        return b[off + 32:off + 32 + n].decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def onchain_profile(addr, rpcs):
    """What a contract is, from its own view functions: a token, a Safe multisig, an AMM pool."""
    out = []
    sym, name = view(rpcs, addr, "0x95d89b41"), view(rpcs, addr, "0x06fdde03")
    s, n = decode_string(sym) if sym else "", decode_string(name) if name else ""
    if s or n:
        out.append((f"token {s}" if s else "token") + (f" ({n})" if n and n != s else ""))
    owners, thr = view(rpcs, addr, "0xa0e67e2b"), view(rpcs, addr, "0xe75235b8")
    if owners and thr:
        b = bytes.fromhex(owners[2:])
        out.append(f"Safe {int(thr, 16)}-of-{int.from_bytes(b[32:64], 'big') if len(b) >= 64 else '?'}")
    t0, t1 = view(rpcs, addr, "0x0dfe1681"), view(rpcs, addr, "0xd21220a7")
    if t0 and t1:
        syms = [decode_string(view(rpcs, "0x" + t[-40:], "0x95d89b41") or "0x") or "?" for t in (t0, t1)]
        out.append(f"pool {syms[0]}/{syms[1]}")
    ilk = view(rpcs, addr, "0xc5ce281e")  # Maker collateral adapters
    if ilk and len(ilk) == 66:
        out.append(f"Maker join {decode_string(ilk)}")
    for sel, what in (("0xb2016bd4", "collateral"), ("0xf8d89898", "debt token"), ("0x2495a599", "underlying")):
        a = word_addr(view(rpcs, addr, sel))
        if a:
            out.append(f"{what} {symbol_of(rpcs, a)}")
    main_ = word_addr(view(rpcs, addr, "0xdffeadd0"))  # Reserve Protocol components
    if main_:
        rt = word_addr(view(rpcs, main_, "0x40c65f72"))
        if rt:
            out.append(f"Reserve RToken {symbol_of(rpcs, rt)}")
    return "; ".join(out)


def word_addr(res):
    """An address returned by eth_call, or None (no answer or the zero address)."""
    if not res or len(res) != 66 or int(res[-40:], 16) == 0:
        return None
    return "0x" + res[-40:]


def symbol_of(rpcs, token):
    return decode_string(view(rpcs, token, "0x95d89b41") or "0x") or short(token)


def ens_reverse(addr, rpcs):
    """The ENS name an address has set for itself, if any."""
    try:
        from eth_utils import keccak
    except Exception:
        return ""
    node = b"\0" * 32
    for label in reversed(f"{addr.lower()[2:]}.addr.reverse".split(".")):
        node = keccak(node + keccak(label.encode()))
    res = view(rpcs, ENS_REGISTRY, "0x0178b8bf" + node.hex())
    if not res or int(res, 16) == 0:
        return ""
    name = view(rpcs, "0x" + res[-40:], "0x691f3431" + node.hex())
    return decode_string(name) if name else ""


def address_info(addr, rpcs, cache={}):
    if addr in cache:
        return cache[addr]
    info = blockscout_info(addr)
    if info.get("is_contract") is None:
        for src in rpcs[:6]:
            try:
                info["is_contract"] = len(src.call("eth_getCode", [addr, "latest"], timeout=20)) > 2
                break
            except Exception:
                continue
    if info.get("is_contract"):
        if not info.get("name"):
            info["name"] = sourcify_name(addr) or etherscan_name(addr)
        info["profile"] = onchain_profile(addr, rpcs)
    if not info.get("ens"):
        info["ens"] = ens_reverse(addr, rpcs)
    cache[addr] = info
    return info


def find_archive(rpcs, block, supply):
    """RPC sources whose rETH totalSupply at `block` matches the panel: they serve archive state."""
    def test(src):
        try:
            v = hexint(src.eth_call("0x18160ddd", block, timeout=20)) / 1e18
            return abs(v - supply) < 1e-6
        except Exception:
            return False
    with cf.ThreadPoolExecutor(16) as ex:
        ok = list(ex.map(test, rpcs))
    return [s for s, good in zip(rpcs, ok) if good]


def balance(archive, addr, block):
    data = "0x70a08231" + addr[2:].lower().rjust(64, "0")
    for src in archive[:3]:
        try:
            return hexint(src.eth_call(data, block)) / 1e18
        except Exception:
            continue
    return np.nan


def enrich(df, labels, rpcs, archive, b_start, b_end):
    out = df.copy()
    out["label"] = [labels.get(a, {}).get("label", "") for a in out.index]
    out["category"] = [labels.get(a, {}).get("category", "") for a in out.index]
    info = {a: address_info(a, rpcs) for a in out.index}
    out["is_contract"] = [info[a].get("is_contract") for a in out.index]
    for col in ("name", "profile", "tags", "ens"):
        out[col] = [info[a].get(col, "") for a in out.index]
    if archive:
        out["balance_start"] = [balance(archive, a, b_start) for a in out.index]
        out["balance_end"] = [balance(archive, a, b_end) for a in out.index]
        out["balance_check"] = (out["balance_end"] - out["balance_start"] - out["net"]).abs() < 1e-6
    return out


BIG = 500.0  # rETH: an unlabelled address that moved at least this much is shown on its own
CATEGORY_ORDER = ["Restaking", "Lending and CDPs", "DEX liquidity", "L2 bridges", "Funds and DAO treasuries",
                  "Large wallets", "Unidentified contracts", "Other addresses"]


def kind_of(addr, rpcs, cache={}):
    """wallet (no code, or an EIP-7702 account), safe (a Safe multisig) or contract."""
    if addr in cache:
        return cache[addr]
    kind = None
    for src in rpcs[:3]:
        try:
            code = src.call("eth_getCode", [addr, "latest"], timeout=20)
            kind = "wallet" if len(code) <= 2 or code.lower().startswith("0xef0100") else "contract"
            break
        except Exception:
            continue
    if kind == "contract" and view(rpcs, addr, "0xe75235b8"):
        kind = "safe"
    cache[addr] = kind
    return kind


def categorize(net, labels, rpcs):
    """Holder type of every address: its labelled category; else a large wallet or an
    unidentified contract when it moved at least BIG rETH; else other addresses."""
    cats = {}
    for a, v in net.loc[net.index != ZERO, "net"].items():
        if labels.get(a, {}).get("category"):
            cats[a] = labels[a]["category"]
        elif abs(v) >= BIG:
            cats[a] = {"wallet": "Large wallets", "safe": "Large wallets",
                       "contract": "Unidentified contracts"}.get(kind_of(a, rpcs), "Other addresses")
        else:
            cats[a] = "Other addresses"
    return pd.Series(cats, dtype=str)


def decompose(nets, labels, rpcs):
    """Net rETH change by holder type, one column per window; each column adds up to the supply change."""
    cols = {}
    for name, net in nets.items():
        s = net.loc[net.index != ZERO, "net"]
        g = s.groupby(categorize(net, labels, rpcs))
        cols[f"net_{name}"], cols[f"addresses_{name}"] = g.sum(), g.size()
    df = pd.DataFrame(cols).fillna(0)
    df = df.reindex([c for c in CATEGORY_ORDER if c in df.index] + [c for c in df.index if c not in CATEGORY_ORDER])
    df.loc["Total (= supply change)"] = df.sum()
    return df


def by_entity(nets, labels):
    """Net change per labelled entity (one entity can have several addresses, e.g. after a migration)."""
    ent = pd.Series({a: v["entity"] for a, v in labels.items() if v.get("entity")})
    cat = {v["entity"]: v["category"] for v in labels.values() if v.get("entity")}
    df = pd.DataFrame({f"net_{name}": net["net"].reindex(ent.index).fillna(0).groupby(ent).sum()
                       for name, net in nets.items()})
    df.insert(0, "category", [cat.get(e, "") for e in df.index])
    df.index.name = "entity"
    return df.sort_values(df.columns[1])


def who(addr, labels, rpcs):
    if addr == ZERO:
        return "rETH contract (burn: redeemed for ETH)"
    if labels.get(addr, {}).get("label"):
        return labels[addr]["label"]
    return display_name(address_info(addr, rpcs), addr)


def counterparties(tr, addrs, labels, rpcs, k=3):
    """Where each address sent its rETH: the k largest recipients."""
    rows = []
    for a in addrs:
        out = tr[(tr["from"] == a) & (tr["to"] != a)]
        total = float(out["amount"].sum())
        if not total:
            continue
        for r, v in out.groupby("to")["amount"].sum().nlargest(k).items():
            rows.append({"address": a, "who": who(a, labels, rpcs), "recipient": r,
                         "recipient_who": who(r, labels, rpcs), "reth": v, "share_of_sent": v / total})
    return pd.DataFrame(rows)


def display_name(row, addr):
    for k in ("label", "tags", "profile", "name", "ens"):
        v = row.get(k, "")
        if isinstance(v, str) and v.strip():
            return v.split(";")[0].strip()[:40]
    return short(addr)


def short(a):
    return a[:6] + "..." + a[-4:]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    panel = pd.read_csv(os.path.join(OUT_DIR, "weekly_panel.csv"))
    panel["date"] = pd.to_datetime(panel["date"], utc=True)
    panel = panel.sort_values("block").reset_index(drop=True)
    blk = panel["block"].astype(int).to_numpy()
    sup = panel["reth_supply"].to_numpy(float)
    weeks = [(k, int(blk[k - 1]) + 1, int(blk[k])) for k in range(1, len(blk))]
    expected = {k: float(sup[k] - sup[k - 1]) for k in range(1, len(blk))}
    start, end = weeks[0][1], weeks[-1][2]
    dbg(f"Window: blocks {start} to {end}, {len(weeks)} weeks; supply {sup[0]:,.1f} -> {sup[-1]:,.1f} rETH")

    # 1. Sources
    cands = build_sources()
    all_cached = all(os.path.exists(week_path(lo, hi)) for _, lo, hi in weeks)
    probes = probe_all(cands, start, logs=not all_cached)
    probes.to_csv(os.path.join(OUT_DIR, "holders_sources.csv"), index=False)
    secs = dict(zip(probes["source"], probes["seconds"].fillna(99)))
    usable = [s for s, u in zip(cands, probes["usable"]) if u]
    usable.sort(key=lambda s: (not s.keyed, -s.max_range, secs[s.label]))
    usable = usable[:MAX_WORKERS]
    rpcs = [s for s, ok in zip(cands, probes["chain_ok"]) if ok and isinstance(s, RpcSource)]
    rpcs.sort(key=lambda s: (not s.keyed, s not in usable))
    if all_cached:
        dbg(f"All weeks cached; {len(rpcs)} of {len(cands)} endpoints answered (used for balances and names)")
    else:
        dbg(f"Sources: {len(cands)} tried, {int(probes['chain_ok'].sum())} answered, {len(usable)} serve logs: "
            + ", ".join(f"{s.label} ({s.max_range:,})" for s in usable))
        for _, r in probes[~probes["usable"]].iterrows():
            dbg(f"  not used: {r['source']}: {r['error'][:140]}")

    # 2. Transfers, week by week, each reconciled with the supply change
    data, origin, failed = fetch_all(usable, weeks, expected)
    rows = []
    for k, lo, hi in weeks:
        df = data.get(k)
        rows.append({"week_ending": panel["date"].iloc[k].date(), "block_from": lo, "block_to": hi,
                     "transfers": len(df) if df is not None else np.nan,
                     "minted": float(df.loc[df["from"] == ZERO, "amount"].sum()) if df is not None else np.nan,
                     "burned": float(df.loc[df["to"] == ZERO, "amount"].sum()) if df is not None else np.nan,
                     "supply_change": expected[k], "source": origin.get(k, "MISSING")})
    pd.DataFrame(rows).to_csv(os.path.join(OUT_DIR, "holders_weeks.csv"), index=False)
    if failed:
        raise RuntimeError(f"{len(failed)} of {len(weeks)} weeks could not be fetched and reconciled "
                           f"(first: blocks {failed[0]['lo']}-{failed[0]['hi']}). Progress is cached; "
                           "rerun, or add a free ETHERSCAN_KEY secret.")
    tr = pd.concat([data[k].assign(week=k) for k, _, _ in weeks], ignore_index=True)
    dbg(f"Transfers in window: {len(tr):,}")

    # 3. Net change by address, whole window and since Saturn 1
    net = net_by_address(tr)
    implied = float(-net.loc[ZERO, "net"])
    if abs(implied - (sup[-1] - sup[0])) > 1e-4:
        raise RuntimeError("Transfer totals do not match the supply change: stop and review.")
    s_idx = int(np.argmax(panel["date"] >= pd.Timestamp(SATURN1, tz="UTC")))
    net_s1 = net_by_address(tr[tr["week"] > s_idx])
    w_all = f"{panel['date'].iloc[0].date()} to {panel['date'].iloc[-1].date()}"
    w_s1 = f"Saturn 1: {panel['date'].iloc[s_idx].date()} to {panel['date'].iloc[-1].date()}"
    conc = concentration(net, w_all)
    conc_s1 = concentration(net_s1, w_s1)
    pd.DataFrame([conc, conc_s1]).to_csv(os.path.join(OUT_DIR, "holders_concentration.csv"), index=False)

    # 4. Who they are
    labels = load_labels()
    archive = find_archive(rpcs, int(blk[0]), float(sup[0])) if rpcs else []
    dbg(f"Archive sources for balances: {', '.join(s.label for s in archive[:3]) or 'none'}")
    top = net[(net.index != ZERO) & (net["net"] < 0)].sort_values("net").head(TOP_N)
    top = enrich(top, labels, rpcs, archive, int(blk[0]), int(blk[-1]))
    top.insert(5, "share_of_total_decrease", -top["net"] / conc["total_decrease_reth"])
    top.to_csv(os.path.join(OUT_DIR, "holders_top_leavers.csv"))
    gainers = net[net.index != ZERO].sort_values("net", ascending=False).head(15)
    gainers = enrich(gainers, labels, rpcs, archive, int(blk[0]), int(blk[-1]))
    gainers.to_csv(os.path.join(OUT_DIR, "holders_top_gainers.csv"))
    top_s1 = net_s1[(net_s1.index != ZERO) & (net_s1["net"] < 0)].sort_values("net").head(20)
    top_s1 = enrich(top_s1, labels, rpcs, archive, int(blk[s_idx]), int(blk[-1]))
    top_s1.insert(5, "share_of_total_decrease", -top_s1["net"] / conc_s1["total_decrease_reth"])
    top_s1.to_csv(os.path.join(OUT_DIR, "holders_top_leavers_since_saturn1.csv"))
    nets = {"since_2024": net, "since_saturn1": net_s1}
    dec = decompose(nets, labels, rpcs)
    for name, change in (("since_2024", sup[-1] - sup[0]), ("since_saturn1", sup[-1] - sup[s_idx])):
        if abs(dec.loc["Total (= supply change)", f"net_{name}"] - change) > 1e-4:
            raise RuntimeError("Holder types do not add up to the supply change: stop and review.")
    dec.to_csv(os.path.join(OUT_DIR, "holders_by_category.csv"))
    if labels:
        by_entity(nets, labels).to_csv(os.path.join(OUT_DIR, "holders_by_entity.csv"))
    big = pd.DataFrame({"net_since_2024": net["net"], "net_since_saturn1": net_s1["net"]}).fillna(0)
    big = big[(big.index != ZERO) & ~big.index.isin(list(labels))
              & ((big["net_since_2024"].abs() >= BIG) | (big["net_since_saturn1"].abs() >= BIG))].copy()
    big["kind"] = [kind_of(a, rpcs) for a in big.index]
    big["who"] = [display_name(address_info(a, rpcs), a)
                  if k == "contract" and max(abs(x), abs(y)) >= 1_000 else ""
                  for a, k, x, y in zip(big.index, big["kind"], big["net_since_2024"], big["net_since_saturn1"])]
    big.sort_values("net_since_2024").to_csv(os.path.join(OUT_DIR, "holders_unlabelled_big.csv"))
    counterparties(tr, list(top.index[:10]), labels, rpcs).to_csv(
        os.path.join(OUT_DIR, "holders_counterparties.csv"), index=False)

    # 5. Biggest outflow weeks: who drove them
    rows = []
    for k in panel.iloc[1:].nsmallest(5, "net_flow_eth").index:
        wn = net_by_address(tr[tr["week"] == k])
        wn = wn[wn.index != ZERO].sort_values("net").head(3)
        for a, r in wn.iterrows():
            rows.append({"week_ending": panel["date"].iloc[k].date(),
                         "week_net_flow_eth": panel["net_flow_eth"].iloc[k],
                         "address": a, "net_reth": r["net"], "who": who(a, labels, rpcs)})
    pd.DataFrame(rows).to_csv(os.path.join(OUT_DIR, "holders_worst_weeks.csv"), index=False)

    # 6. Quarterly timing of the top 10 leavers
    quarter = panel["date"].dt.tz_convert(None).dt.to_period("Q").astype(str).to_numpy()
    tr["quarter"] = quarter[tr["week"].to_numpy()]
    top10 = list(top.index[:10])
    sub = tr[tr["from"].isin(top10) | tr["to"].isin(top10)]
    q = (sub[sub["to"].isin(top10)].groupby(["to", "quarter"])["amount"].sum().rename_axis(["address", "quarter"])
         .sub(sub[sub["from"].isin(top10)].groupby(["from", "quarter"])["amount"].sum()
              .rename_axis(["address", "quarter"]), fill_value=0))
    q.unstack("quarter").reindex(top10).to_csv(os.path.join(OUT_DIR, "holders_top10_by_quarter.csv"))

    # 7. Chart: top 15 leavers
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t15 = top.head(15).iloc[::-1]
    names = [display_name(r, a) if display_name(r, a) == short(a) else f"{display_name(r, a)} ({short(a)})"
             for a, r in t15.iterrows()]
    contract = t15["is_contract"].astype(object).where(t15["is_contract"].notna(), False).astype(bool)
    colors = ["#4c72b0" if c else "#b0b0b0" for c in contract]
    fig, ax = plt.subplots(figsize=(10, 6.5))
    ax.barh(names, -t15["net"] / 1e3, color=colors)
    ax.set_xlabel(f"Net rETH that left the address, {w_all} (thousand)")
    ax.set_title(f"Top 15 addresses by net rETH decrease; top 10 = {conc['top10_share']:.0%} of the total decrease\n"
                 "(blue = contract, grey = wallet or unknown)")
    ax.grid(alpha=0.3, axis="x")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "chart_top_leavers.png"), dpi=150)

    # 8. Chart: the supply change split by holder type (waterfall)
    steps = dec.drop(index="Total (= supply change)")["net_since_2024"]
    fig, ax = plt.subplots(figsize=(10, 5.5))
    cum = 0.0
    for i, (c, v) in enumerate(steps.items()):
        ax.bar(i, v / 1e3, bottom=cum / 1e3, color="#c44e52" if v < 0 else "#55a868", width=0.7)
        ax.text(i, (cum + v) / 1e3, f"{v / 1e3:+,.1f}k", ha="center", va="top" if v < 0 else "bottom", fontsize=8)
        cum += v
    ax.bar(len(steps), cum / 1e3, color="#4c72b0", width=0.7)
    ax.text(len(steps), cum / 1e3, f"{cum / 1e3:+,.1f}k", ha="center", va="top", fontsize=8)
    ax.set_xticks(range(len(steps) + 1), [*steps.index, "rETH supply change"], rotation=25, ha="right")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_ylabel("rETH (thousand)")
    ax.set_title(f"Who the rETH supply change came from, {w_all}")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "chart_supply_change_by_holder.png"), dpi=150)
    print(dec.round(1).to_string())

    show = [c for c in ("net", "share_of_total_decrease", "is_contract", "label", "profile", "name", "tags",
                        "balance_start", "balance_end") if c in top]
    print(pd.DataFrame([conc, conc_s1]).T.to_string())
    print(top[show].head(15).round(3).to_string())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        dbg(f"FAILED: {type(e).__name__}: {e}")
        raise
    finally:
        write_debug()
