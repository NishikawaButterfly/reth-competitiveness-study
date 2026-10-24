"""Offline test for analysis/holders.py: synthetic rETH transfers, an endpoint that
refuses wide block ranges, and a known answer for who left.

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

ZERO = holders.ZERO
rng = np.random.default_rng(3)
WHALE = "0x" + "a1" * 20          # leaves with 6,000 rETH
POOL = "0x" + "c2" * 20           # a contract that loses 2,000 rETH
small = ["0x" + f"{i:040x}" for i in range(1, 60)]

# ---- synthetic chain: 10 weekly blocks, transfers in between ------------------------
week_blocks = [10_000 + 5_000 * k for k in range(11)]
logs = []
def add(block, frm, to, amt):
    logs.append({"blockNumber": block, "transactionHash": bytes(rng.integers(0, 255, 32, dtype=np.uint8)),
                 "logIndex": len(logs), "topics": [bytes(32), bytes(12) + bytes.fromhex(frm[2:]),
                                                    bytes(12) + bytes.fromhex(to[2:])],
                 "data": int(amt * 1e18).to_bytes(32, "big")})
for a in small:                                   # small holders mint and burn a little
    add(int(rng.integers(10_001, 60_000)), ZERO, a, float(rng.uniform(1, 50)))
add(20_500, WHALE, ZERO, 4_000)                   # whale burns in week 3
add(41_000, WHALE, POOL, 2_000)                   # whale sells into a pool in week 7
add(41_200, POOL, ZERO, 4_000)                    # pool redeems 4,000 (net -2,000 for the pool)
for _ in range(200):                              # noise between small holders
    f, t = rng.choice(small, 2, replace=False)
    add(int(rng.integers(10_001, 60_000)), f, t, float(rng.uniform(0.1, 2)))

class FakeEth:
    calls = 0
    def get_logs(self, p):
        FakeEth.calls += 1
        if p["toBlock"] - p["fromBlock"] > 3_000:
            raise ValueError("block range too large")
        return [l for l in logs if p["fromBlock"] <= l["blockNumber"] <= p["toBlock"]]
    def get_code(self, a):
        return b"\x60" if a.lower().startswith("0xc2") else b""
class FakeW3:
    eth = FakeEth()
    @staticmethod
    def to_checksum_address(a):
        return a

# ---- inputs the script expects ---------------------------------------------------------
work = tempfile.mkdtemp(prefix="holders_test_")
os.chdir(work)
os.makedirs("reth_study")
dates = pd.date_range("2024-01-06", periods=11, freq="7D", tz="UTC")
pd.DataFrame({"date": dates, "unix": 0, "block": week_blocks}).to_csv("reth_study/blocks.csv", index=False)
supply0 = 100_000.0
mint_burn = []
for b in week_blocks:
    m = sum(int.from_bytes(l["data"], "big") / 1e18 * (1 if l["topics"][1][-20:] == bytes(20) else 0)
            - int.from_bytes(l["data"], "big") / 1e18 * (1 if l["topics"][2][-20:] == bytes(20) else 0)
            for l in logs if week_blocks[0] < l["blockNumber"] <= b)
    mint_burn.append(supply0 + m)
panel = pd.DataFrame({"date": dates, "block": week_blocks, "reth_supply": mint_burn})
panel["net_flow_eth"] = panel["reth_supply"].diff()
panel.to_csv("reth_study/weekly_panel.csv", index=False)

holders.connect_all = lambda start: [("fake", FakeW3())]
holders.LABELS_FILE = os.path.join(work, "labels.csv")
pd.DataFrame({"address": [POOL], "label": ["Test pool"]}).to_csv(holders.LABELS_FILE, index=False)

holders.main()
first_calls = FakeEth.calls

top = pd.read_csv("reth_study/holders_top_leavers.csv", index_col=0)
conc = pd.read_csv("reth_study/holders_concentration.csv").iloc[0]
assert top.index[0] == WHALE, top.index[0]
assert abs(top.loc[WHALE, "net"] + 6_000) < 1e-6
assert abs(top.loc[POOL, "net"] + 2_000) < 1e-6 and bool(top.loc[POOL, "is_contract"])
assert top.loc[POOL, "label"] == "Test pool"
assert conc["top1_share"] > 0.5, conc["top1_share"]
worst = pd.read_csv("reth_study/holders_worst_weeks.csv")
assert WHALE in set(worst["address"])
assert os.path.exists("reth_study/chart_top_leavers.png")

# rerun: everything comes from the cache, no new log requests
FakeEth.calls = 0
holders.main()
assert FakeEth.calls == 0, FakeEth.calls
print(f"log requests on first run: {first_calls}; on rerun: 0")
print("ALL HOLDERS TESTS PASSED")

# endpoint rotation: the first endpoint always fails, the second works
class BadEth:
    def get_logs(self, p):
        raise ConnectionError("simulated outage")
class BadW3:
    eth = BadEth()
holders.time.sleep = lambda *_: None
tr = holders.fetch_transfers([("bad", BadW3()), ("good", FakeW3())], week_blocks[0] + 1, week_blocks[-1],
                             os.path.join(work, "rotation_cache.csv"))
assert len(tr) == len(logs), (len(tr), len(logs))
print("endpoint rotation OK")
