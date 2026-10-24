"""Offline test: runs reth_study_v2.ipynb with mocked network access and a synthetic
market in which weekly rETH flows respond to the yield gap with a known slope
(BETA_TRUE). Passes only if the notebook recovers that slope.

Run: python tests/test_notebook_v2.py
"""
import json, os, time, shutil
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")

import tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NB = os.path.join(ROOT, "reth_study_v2.ipynb")
WORK = tempfile.mkdtemp(prefix="reth_test_")
os.chdir(WORK)
time.sleep = lambda *_: None

# ---- synthetic market --------------------------------------------------------
BETA_TRUE = 0.010           # % of supply per week, per bp of 4-week yield gap
rng = np.random.default_rng(7)
grid = pd.date_range("2023-12-02", "2027-01-02", freq="7D", tz="UTC")
n = len(grid)
i = np.arange(n)
r_apr = 0.028 + 0.0025 * np.sin(i / 9)
s_apr = 0.030 + 0.0005 * np.sin(i / 13)
r_rate = 1.095 * np.cumprod(np.r_[1.0, 1 + r_apr[1:] * 7 / 365])
s_rate = 1.150 * np.cumprod(np.r_[1.0, 1 + s_apr[1:] * 7 / 365])
gap = (r_apr - s_apr) * 1e4
gap4 = pd.Series(gap).rolling(4).mean().to_numpy()
flow = np.full(n, -0.30)
for k in range(5, n):
    flow[k] = -0.30 + BETA_TRUE * gap4[k - 1] + rng.normal(0, 0.05)
supply = 470_000 * np.cumprod(np.r_[1.0, 1 + flow[1:] / 100])
# stETH pooled ETH: rewards plus independent net flows (market control, unrelated to rETH here)
steth_flow = rng.normal(0.05, 0.3, n)
pooled = 9_500_000 * np.cumprod(np.r_[1.0, (s_rate[1:] / s_rate[:-1]) * (1 + steth_flow[1:] / 100)])
GRID_TS = np.array([int(t.timestamp()) for t in grid])

T0 = int(GRID_TS[0]); B0 = 18_700_000

def block_of(ts): return int(B0 + (ts - T0) // 12)
def ts_of(b): return int(T0 + (b - B0) * 12)
def interp(ts, ys): return float(np.interp(ts, GRID_TS, ys))
def eth_usd_at(ts): return 2300 + 900 * np.sin((ts - T0) / 86400 / 120)

# ---- fake HTTP ------------------------------------------------------------------
CALLS = {"http": 0}
class FakeResp:
    def __init__(self, data, status=200): self._d, self.status_code = data, status
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")
    def json(self): return self._d

def fake_request(method, url, timeout=30, **kwargs):
    CALLS["http"] += 1
    if "/block/ethereum/" in url:
        ts = int(url.rsplit("/", 1)[1])
        return FakeResp({"height": block_of(ts), "timestamp": ts})
    if "/prices/historical/" in url:
        ts = int(url.split("/prices/historical/")[1].split("/")[0])
        eth = eth_usd_at(ts)
        reth = eth * interp(ts, r_rate) * (1 + 0.003 * np.sin(ts / 7e5))
        return FakeResp({"coins": {"coingecko:rocket-pool-eth": {"price": reth},
                                   "coingecko:ethereum": {"price": eth}}})
    return FakeResp({}, 404)

import requests
requests.request = fake_request

# ---- fake archive node ------------------------------------------------------------
class _Call:
    def __init__(self, fn): self.fn = fn
    def call(self, block_identifier="latest"): return self.fn(block_identifier)
class _Functions:
    def totalSupply(self): return _Call(lambda b: int(interp(ts_of(b), supply) * 1e18))
    def getExchangeRate(self): return _Call(lambda b: int(interp(ts_of(b), r_rate) * 1e18))
    def getPooledEthByShares(self, sh): return _Call(lambda b: int(interp(ts_of(b), s_rate) * sh))
    def getTotalPooledEther(self): return _Call(lambda b: int(interp(ts_of(b), pooled) * 1e18))
class _Contract:
    def __init__(self): self.functions = _Functions()
class _Eth:
    block_number = block_of(int(pd.Timestamp("2026-10-24 12:00", tz="UTC").timestamp()))
    def contract(self, address, abi): return _Contract()
    def get_block(self, b):
        b = self.block_number if b == "latest" else b
        return {"number": b, "timestamp": ts_of(b)}
class FakeW3: eth = _Eth()

# ---- run ---------------------------------------------------------------------------------
nb = json.load(open(NB))
cells = [c for c in nb["cells"] if c["cell_type"] == "code"]
checks = [{"label": "synthetic", "date": d,
           "reth_supply": interp(int(pd.Timestamp(d, tz="UTC").timestamp()), supply)}
          for d in ("2025-09-09", "2026-10-24")]

def run_all(label):
    ns = {"__name__": "__main__"}
    for k, cell in enumerate(cells):
        src = "".join(cell["source"])
        if src.lstrip().startswith("!pip"):
            continue
        exec(compile(src.replace("plt.show()", "plt.close(fig)"), f"{label}-cell{k}", "exec"), ns)
        if k == 1:
            ns["CHECKS"] = checks
        if k == 2:
            ns["connect_archive"] = lambda *a, **kw: FakeW3()
    print(f"{label}: all {len(cells)} code cells OK")
    return ns

ns = run_all("first run")
CALLS["http"] = 0
ns2 = run_all("rerun")
assert CALLS["http"] == 2, CALLS["http"]

resp = ns["response"]
print(resp.round(4).to_string())
b = resp.iloc[0]
assert 0.007 < b["beta_gap"] < 0.013, b["beta_gap"]
assert b["ci95_low"] < BETA_TRUE < b["ci95_high"], (b["ci95_low"], b["ci95_high"])
mc = resp.set_index("model").loc["lag 1, with stETH flows as market control"]
assert mc["ci95_low"] < BETA_TRUE < mc["ci95_high"], (mc["ci95_low"], mc["ci95_high"])
assert resp["p_holm"].notna().sum() == 4
panel_t = ns["panel"]
true_flow = pd.Series(steth_flow[5:]).reset_index(drop=True)
assert abs(panel_t["steth_flow_pct"].dropna().mean() - 0.05) < 0.1, panel_t["steth_flow_pct"].mean()

scen = ns["scen"]
print(scen[scen.beta_case == "mid"][["scenario", "holder_apr_gain_bps", "weekly_flow_pct",
                                      "eth_behind_reth_12m", "voter_income_vs_today",
                                      "operator_income_vs_today"]].round(4).to_string())
mid = scen[scen.beta_case == "mid"].set_index("scenario")
assert abs(mid.loc["14% (5% + 9%), today", "voter_income_vs_today"]) < 1e-12
assert mid.loc["12% (5% + 7%)", "holder_apr_gain_bps"] > 0
assert mid.loc["12% (5% + 7%)", "operator_income_vs_today"] > 0      # same operator share, bigger rETH
print("gross APR", round(ns["gross_apr"], 5), "effective fee", round(ns["effective_reth_fee"], 4))
assert 0.031 < ns["gross_apr"] < 0.035
for f in ["weekly_panel.csv", "demand_response.csv", "fee_scenarios.csv", "chart_scenarios.png",
          "quarterly.csv", "chart_quarterly_gap.png"]:
    assert os.path.exists(os.path.join("reth_study", f)), f
print("ALL V2 TESTS PASSED")
