"""Offline test for analysis/lending.py.

Two fake lending markets answer eth_call like Aave v3 and Spark: the rETH aToken knows
its pool and underlying, the pool returns getReserveData structs, and supplied amounts
move with the lagged spread between rETH's yield and the WETH borrow rate with a known
slope. The test passes only if the regression recovers that slope.

Run: python tests/test_lending.py
"""
import os
import sys
import tempfile

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "analysis"))
import holders  # noqa: E402
import lending  # noqa: E402

rng = np.random.default_rng(11)
N = 80
blocks = [20_000_000 + 50_000 * k for k in range(N)]
dates = pd.date_range("2024-01-06", periods=N, freq="7D", tz="UTC")
RETH_APR, STETH_APR = 0.028, 0.030          # constant, so the 4-week mean equals them
BETA = {"reth": 0.50, "wsteth": 0.30}       # % per week per 10 bps of lagged spread

MARKETS = {}
for i, (venue, a_reth) in enumerate(lending.VENUES.items()):
    MARKETS[venue] = {
        "a_reth": a_reth, "pool": "0x" + f"{0xa0 + i:02x}" * 20,
        "a_wsteth": "0x" + f"{0xb0 + i:02x}" * 20, "a_weth": "0x" + f"{0xc0 + i:02x}" * 20,
        "borrow": 0.027 + 0.006 * np.sin(np.arange(N) / (5 + 2 * i)) + rng.normal(0, 0.0005, N),
    }
    for a, apr, start in (("reth", RETH_APR, 40_000.0), ("wsteth", STETH_APR, 400_000.0)):
        spread_bps = (apr - MARKETS[venue]["borrow"]) * 1e4
        logs = [np.log(start)]
        for t in range(1, N):
            flow_pct = BETA[a] * spread_bps[t - 1] / 10 + rng.normal(0, 0.3)
            logs.append(logs[-1] + flow_pct / 100)
        MARKETS[venue][f"{a}_supply"] = np.exp(logs)
week_of = {b: k for k, b in enumerate(blocks)}


def w(v):
    return f"{int(v):064x}"


def a64(a):
    return a[2:].rjust(64, "0")


class FakeArchive(holders.RpcSource):
    def __init__(self):
        super().__init__("https://fake.invalid", "fake-archive")

    def call(self, method, params, timeout=45):
        assert method == "eth_call", method
        to, data, tag = params[0]["to"], params[0]["data"], params[1]
        k = N - 1 if tag == "latest" else week_of[int(tag, 16)]
        for venue, m in MARKETS.items():
            if to == m["a_reth"] and data == lending.SEL["POOL"]:
                return "0x" + a64(m["pool"])
            if to == m["a_reth"] and data == lending.SEL["UNDERLYING"]:
                return "0x" + a64(lending.RETH)
            if to == m["pool"] and data.startswith(lending.SEL["getReserveData"]):
                asset = "0x" + data[-40:]
                atoken = {lending.RETH: m["a_reth"], lending.WSTETH: m["a_wsteth"], lending.WETH: m["a_weth"]}[asset]
                borrow = m["borrow"][k] if asset == lending.WETH else 0.001
                words = [0, 10**27, int(0.01 * 1e27), 10**27, int(borrow * 1e27), 0, 0, 1, int(atoken, 16)] + [0] * 6
                return "0x" + "".join(w(x) for x in words)
            if data == lending.SEL["totalSupply"] and to in (m["a_reth"], m["a_wsteth"]):
                a = "reth" if to == m["a_reth"] else "wsteth"
                return hex(int(m[f"{a}_supply"][k] * 1e18))
        raise RuntimeError("execution reverted")


work = tempfile.mkdtemp(prefix="lending_test_")
os.chdir(work)
os.makedirs("reth_study")
pd.DataFrame({"date": dates, "block": blocks, "reth_supply": 300_000.0, "reth_apr": RETH_APR,
              "steth_apr": STETH_APR, "eth_usd": 3000 * np.exp(np.cumsum(rng.normal(0, 0.03, N)))}).to_csv(
    "reth_study/weekly_panel.csv", index=False)
lending.get_archive = lambda: [FakeArchive()]
lending.main()

reg = pd.read_csv("reth_study/lending_regression.csv")
print(reg.round(3).to_string())
for asset, key in (("rETH", "reth"), ("wstETH", "wsteth")):
    for venue in list(lending.VENUES) + ["Aave v3 + Spark"]:
        r = reg[(reg["asset"] == asset) & (reg["venue"] == venue) & (reg["lag_weeks"] == 1)].iloc[0]
        assert abs(r["beta_pct_per_week_per_10bps"] - BETA[key]) < 0.15, (asset, venue, r.to_dict())
        assert r["ci_low"] < BETA[key] < r["ci_high"], (asset, venue)
summ = pd.read_csv("reth_study/lending_summary.csv")
assert set(summ["venue"]) == set(lending.VENUES) | {"Aave v3 + Spark"}
panel = pd.read_csv("reth_study/lending_panel.csv")
aave = panel[panel["venue"] == "Aave v3"].reset_index(drop=True)
assert abs(aave["reth_supplied"].iloc[-1] - MARKETS["Aave v3"]["reth_supply"][-1]) < 1e-6
assert abs(aave["weth_borrow_apr"].iloc[5] - MARKETS["Aave v3"]["borrow"][5]) < 1e-9
assert os.path.exists("reth_study/chart_lending_spread.png")
print("ALL LENDING TESTS PASSED")
