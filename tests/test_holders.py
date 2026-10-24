"""Offline test for analysis/holders.py.

A synthetic rETH history with a known answer for who left, served by fake sources
that behave like the real ones: one with no range limit, one limited to 3,000 blocks,
an Etherscan-style API capped at 40 logs per call, one that silently drops mints
after a certain block, one that returns nothing and one that is down.

Run: python tests/test_holders.py
"""
import os
import sys
import tempfile

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "analysis"))
import holders  # noqa: E402

ZERO, TOPIC = holders.ZERO, holders.TRANSFER_TOPIC
rng = np.random.default_rng(3)
WHALE = "0x" + "a1" * 20          # holds 10,000; burns 4,000 and sells 2,000 into the pool
POOL = "0x" + "c2" * 20           # a contract holding 5,000 that redeems 4,000: net -2,000
OTHER = "0x" + "e3" * 20          # holds 85,000 and never moves
small = ["0x" + f"{i:040x}" for i in range(1, 60)]
INIT = {WHALE: 10_000 * 10**18, POOL: 5_000 * 10**18, OTHER: 85_000 * 10**18}
S0 = sum(INIT.values())

# ---- synthetic chain: 10 weeks of 5,000 blocks -----------------------------------------
week_blocks = [10_000 + 5_000 * k for k in range(11)]
LOGS = []          # (block, log_index, from, to, wei)


def add(block, frm, to, amount):
    LOGS.append((block, len(LOGS), frm, to, int(round(amount * 1e6)) * 10**12))


for b in (10_050, 10_090, 10_120, 10_150, 10_180):          # visible to the 200-block probe
    add(b, ZERO, small[0], 1.5)
for k in range(10):                                          # at least one mint every week
    add(week_blocks[k] + 100, ZERO, small[k + 1], 3.0)
for a in small:                                              # small holders mint
    add(int(rng.integers(10_001, 60_000)), ZERO, a, float(rng.uniform(5, 50)))
add(20_500, WHALE, ZERO, 4_000)                              # whale burns in week 3
add(41_000, WHALE, POOL, 2_000)                              # whale sells into the pool in week 7
add(41_200, POOL, ZERO, 4_000)                               # the pool redeems 4,000
for _ in range(1_000):                                       # noise between small holders
    f, t = rng.choice(small, 2, replace=False)
    add(int(rng.integers(10_001, 60_000)), f, t, float(rng.uniform(0.01, 0.5)))
LOGS.sort()


def supply_wei(block):
    return S0 + sum(w for b, _, f, t, w in LOGS if b <= block and f == ZERO) \
              - sum(w for b, _, f, t, w in LOGS if b <= block and t == ZERO)


def balance_wei(addr, block):
    return INIT.get(addr, 0) + sum(w for b, _, f, t, w in LOGS if b <= block and t == addr) \
                             - sum(w for b, _, f, t, w in LOGS if b <= block and f == addr)


def rpc_log(entry):
    b, i, f, t, w = entry
    return {"blockNumber": hex(b), "logIndex": hex(i), "transactionHash": "0x" + f"{b:08x}{i:056x}",
            "topics": [TOPIC, "0x" + "0" * 24 + f[2:], "0x" + "0" * 24 + t[2:]], "data": "0x" + f"{w:064x}"}


class FakeRpc(holders.RpcSource):
    def __init__(self, label, limit=None, drop_mints_after=None, empty=False, dead=False):
        super().__init__("https://fake.invalid", label)
        self.limit, self.drop, self.empty, self.dead, self.log_calls = limit, drop_mints_after, empty, dead, 0

    def call(self, method, params, timeout=45):
        if self.dead:
            raise ConnectionError("simulated outage")
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_getLogs":
            self.log_calls += 1
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            if self.limit and hi - lo + 1 > self.limit:
                raise RuntimeError("block range too large")
            if self.empty:
                return []
            out = [e for e in LOGS if lo <= e[0] <= hi]
            if self.drop is not None:
                out = [e for e in out if not (e[2] == ZERO and e[0] >= self.drop)]
            return [rpc_log(e) for e in out]
        if method == "eth_call":
            data, block = params[0]["data"], int(params[1], 16)
            if data == "0x18160ddd":
                return hex(supply_wei(block))
            return hex(balance_wei("0x" + data[-40:], block))
        if method == "eth_getCode":
            return "0x6080" if params[0] == POOL else "0x"
        raise ValueError(method)


class FakeApi(holders.ApiSource):
    """Etherscan format: decimal blocks in, hex fields out, logIndex "0x" for 0, 4 topics."""
    def __init__(self, label, cap):
        super().__init__(label, "https://fake.invalid/api", {})
        self.cap, self.log_calls, self.capped_calls = cap, 0, 0

    def get(self, params, timeout):
        self.log_calls += 1
        lo, hi = int(params["fromBlock"]), int(params["toBlock"])
        res = []
        for e in LOGS:
            if lo <= e[0] <= hi:
                d = rpc_log(e)
                d["logIndex"] = "0x" if e[1] == 0 else d["logIndex"]
                d["topics"] = d["topics"] + [None]
                res.append(d)
        if len(res) >= self.cap:
            self.capped_calls += 1
            return {"status": "1", "message": "OK", "result": res[:self.cap]}
        if not res:
            return {"status": "0", "message": "No records found", "result": []}
        return {"status": "1", "message": "OK", "result": res}


good, ranged = FakeRpc("good"), FakeRpc("ranged", limit=3_000)
trunc, empty, dead = FakeRpc("trunc", drop_mints_after=20_000), FakeRpc("empty", empty=True), FakeRpc("dead", dead=True)
capped = FakeApi("capped", cap=40)
SOURCES = [dead, empty, trunc, capped, ranged, good]

# ---- inputs the script expects -----------------------------------------------------------
work = tempfile.mkdtemp(prefix="holders_test_")
os.chdir(work)
os.makedirs("reth_study")
dates = pd.date_range("2024-01-06", periods=11, freq="7D", tz="UTC")
supply = [supply_wei(b) / 1e18 for b in week_blocks]
panel = pd.DataFrame({"date": dates, "block": week_blocks, "reth_supply": supply})
panel["net_flow_eth"] = panel["reth_supply"].diff() * 1.1
panel.to_csv("reth_study/weekly_panel.csv", index=False)
pd.DataFrame({"date": dates, "unix": 0, "block": week_blocks}).to_csv("reth_study/blocks.csv", index=False)

holders.KEYS = {k: "" for k in holders.KEYS}
holders.build_sources = lambda: SOURCES
holders.blockscout_info = lambda a: {}
holders.pause = lambda s: None
holders.CACHE_DIR = os.path.join(work, "cache")
holders.SATURN1 = str(dates[6].date())
holders.LABELS_FILE = os.path.join(work, "labels.csv")
pd.DataFrame({"address": [POOL, WHALE], "label": ["Test pool", "Test whale"],
              "category": ["DEX liquidity", "Large wallet"]}).to_csv(holders.LABELS_FILE, index=False)

# ---- unit checks ----------------------------------------------------------------------------
lg = rpc_log(next(e for e in LOGS if e[2] == ZERO))
lg["logIndex"] = "0x"
assert holders.norm(lg)[1] == 0 and holders.norm(lg)[3] == ZERO
holders.KEYS["ALCHEMY_KEY"] = "secretkey123456"
assert "secretkey123456" not in holders.redact("https://eth-mainnet.g.alchemy.com/v2/secretkey123456 failed")
assert "abcdef0123456789" not in holders.redact("https://x.io/rpc?api_key=abcdef0123456789")
holders.KEYS["ALCHEMY_KEY"] = ""
wk = [e for e in LOGS if 20_001 <= e[0] <= 25_000]
exp = (supply_wei(25_000) - supply_wei(20_000)) / 1e18
full = holders.to_frame([holders.norm(rpc_log(e)) for e in wk])
cut = holders.to_frame([holders.norm(rpc_log(e)) for e in wk if e[2] != ZERO])
assert holders.check(full, exp)[0] and not holders.check(cut, exp)[0]

# the capped API splits ranges until no call hits the cap, and loses nothing
capped.rng = 5_000
got = holders.to_frame(holders.fetch_range(capped, 10_001, 60_000))
assert len(got) == len(LOGS), (len(got), len(LOGS))
assert capped.capped_calls > 0
capped.rng, capped.log_calls, capped.capped_calls = 5_000, 0, 0

# ---- full run ----------------------------------------------------------------------------------
holders.main()

src = pd.read_csv("reth_study/holders_sources.csv").set_index("source")
assert not bool(src.loc["dead", "chain_ok"]) and not bool(src.loc["empty", "usable"])
assert int(src.loc["ranged", "max_range"]) == 2_000 and bool(src.loc["capped", "usable"])
weeks = pd.read_csv("reth_study/holders_weeks.csv")
assert len(weeks) == 10 and not weeks["source"].isin(["dead", "empty", "MISSING"]).any()
assert not ((weeks["block_from"] > 20_000) & (weeks["source"] == "trunc")).any()

top = pd.read_csv("reth_study/holders_top_leavers.csv", index_col=0)
assert top.index[0] == WHALE, top.index[0]
assert abs(top.loc[WHALE, "net"] + 6_000) < 1e-6
assert abs(top.loc[WHALE, "balance_start"] - 10_000) < 1e-6 and abs(top.loc[WHALE, "balance_end"] - 4_000) < 1e-6
assert top["balance_check"].all()
assert abs(top.loc[POOL, "net"] + 2_000) < 1e-6 and bool(top.loc[POOL, "is_contract"])
assert top.loc[POOL, "label"] == "Test pool"
conc = pd.read_csv("reth_study/holders_concentration.csv")
assert conc.loc[0, "top1_share"] > 0.5, conc.loc[0, "top1_share"]
assert abs(conc.loc[0, "supply_change_reth"] - (supply[-1] - supply[0])) < 1e-6
cat = pd.read_csv("reth_study/holders_by_category.csv", index_col=0)
assert "DEX liquidity" in cat.index and abs(cat["share"].sum() - 1) < 1e-9
s1 = pd.read_csv("reth_study/holders_top_leavers_since_saturn1.csv", index_col=0)
assert {WHALE, POOL} <= set(s1.index) and abs(s1.loc[POOL, "net"] + 2_000) < 1e-6
worst = pd.read_csv("reth_study/holders_worst_weeks.csv")
assert WHALE in set(worst["address"])
assert os.path.exists("reth_study/chart_top_leavers.png")
calls_first = sum(s.log_calls for s in SOURCES)

# rerun: every week comes from the cache, no log requests at all
for s in SOURCES:
    s.log_calls = 0
holders.main()
assert sum(s.log_calls for s in SOURCES) == 0, [(s.label, s.log_calls) for s in SOURCES]
print(f"log requests on first run: {calls_first}; on rerun: 0")
print("ALL HOLDERS TESTS PASSED")
