"""Offline test for analysis/report.py.

Builds a small set of holder, lending and panel outputs for February 2024 with known numbers,
runs the report, and checks the figures, the privacy rule for wallets, the hand-written
notes and the reconciliation check.

Run: python tests/test_report.py
"""
import os
import sys
import tempfile

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "analysis"))
import report  # noqa: E402

work = tempfile.mkdtemp(prefix="report_test_")
os.chdir(work)
os.makedirs("reth_study")
report.OUT_DIR, report.REPORTS_DIR = "reth_study", "reports"

SPARK, WALLET, BAL = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
dates = pd.date_range("2024-01-06", periods=8, freq="7D", tz="UTC")   # 6 Jan ... 24 Feb
supply = [100_000, 100_500, 101_000, 102_000, 101_000, 100_000, 99_000, 98_000]
pd.DataFrame({"date": dates, "block": range(1_000, 1_008), "reth_supply": supply,
              "reth_apr": 0.027, "steth_apr": 0.030, "gap_bps": -30.0, "premium_pct": -0.10}).to_csv(
    "reth_study/weekly_panel.csv", index=False)
pd.DataFrame([{"month": "2024-02", "start_snapshot": "2024-01-27", "end_snapshot": "2024-02-24", "weeks": 4,
               "block_from": 1_004, "block_to": 1_007, "supply_start": 102_000, "supply_end": 98_000,
               "supply_change": -4_000, "transfers": 1_234, "minted": 1_000, "burned": 5_000}]).to_csv(
    "reth_study/holders_month.csv", index=False)
pd.DataFrame({"week_ending": ["2024-02-03", "2024-02-10", "2024-02-17", "2024-02-24"],
              "minted": [250.0] * 4, "burned": [1_250.0] * 4, "supply_change": [-1_000.0] * 4,
              "source": ["good"] * 4}).to_csv("reth_study/holders_weeks.csv", index=False)
cat = pd.DataFrame({"net_since_2024": [-5_000, -1_000, 400, -2_000, 600], "addresses_since_2024": [1, 3, 2, 5, 900],
                    "net_since_saturn1": [0, -500, 0, -100, 100], "addresses_since_saturn1": [0, 2, 0, 1, 50],
                    "net_month": [0, -3_000, 500, -1_200, -300], "addresses_month": [0, 2, 1, 1, 400]},
                   index=["Restaking", "Lending and CDPs", "DEX liquidity", "Large wallets", "Other addresses"])
cat.loc["Total (= supply change)"] = cat.sum()
cat.to_csv("reth_study/holders_by_category.csv")
pd.DataFrame({"category": ["Lending and CDPs", "DEX liquidity", "Lending and CDPs"],
              "net_since_2024": [-1_000, 400, 0], "net_since_saturn1": [-500, 0, 0], "net_month": [-3_000, 500, 5]},
             index=pd.Index(["Spark", "Balancer", "Aave v3"], name="entity")).to_csv("reth_study/holders_by_entity.csv")
pd.DataFrame({"net": [-3_000, -1_200, 500], "label": ["Spark spRETH", "", "Balancer V2 Vault"],
              "entity": ["Spark", "", "Balancer"], "holder_type": ["Lending and CDPs", "Large wallets", "DEX liquidity"],
              "kind": ["contract", "wallet", "contract"], "who": ["Spark spRETH", "someone.eth", "Balancer V2 Vault"],
              "ens": ["", "someone.eth", ""], "balance_start": [5_000, 1_200, 100], "balance_end": [2_000, 0, 600]},
             index=pd.Index([SPARK, WALLET, BAL], name="address")).to_csv("reth_study/holders_month_movers.csv")
rows = []
for d, i in zip(dates, range(8)):
    rows.append({"date": d, "venue": "Aave v3", "reth_supplied": 8_000 + 10 * i, "wsteth_supplied": 50_000,
                 "reth_spread_bps": 10.0 + i, "wsteth_spread_bps": 40.0})
    rows.append({"date": d, "venue": "Spark", "reth_supplied": 5_000 if d <= dates[3] else 2_000,
                 "wsteth_supplied": 30_000 + 1_000 * i, "reth_spread_bps": 20.0, "wsteth_spread_bps": 50.0})
pd.DataFrame(rows).to_csv("reth_study/lending_panel.csv", index=False)

# ---- full run --------------------------------------------------------------------------------
path = report.main()
md = open(path, encoding="utf-8").read()
assert path.endswith(os.path.join("reports", "2024-02.md")), path
assert os.path.exists("reports/2024-02-holders.png")
assert md.startswith("# rETH Demand Monitor: February 2024"), md[:80]
assert "from 102,000 to 98,000 (−4,000 rETH, −3.9%)" in md, md
assert "27 January 2024 to 24 February 2024" in md
assert "| Lending and CDPs | −3,000 | 2 |" in md and "| **Total (= supply change)** | **−4,000** | |" in md
assert "| Spark | Lending and CDPs | −3,000 | −500 | −1,000 |" in md
assert "| Balancer | DEX liquidity | +500 | 0 | +400 |" in md          # zero shown as 0, not +0
assert "Aave v3 | Lending and CDPs" not in md                      # moved under 10 rETH: not listed
assert "largest decreases: Lending and CDPs −3,000" in md       # names keep their capitals
assert "Spark spRETH ([0x1111…1111](https://etherscan.io/address/" + SPARK + "))" in md
assert "Unlabelled wallet [0x2222…2222]" in md and "someone.eth" not in md   # wallets by address only
assert "| Spark | 5,000 → 2,000 | 2.0% | 33,000 → 37,000 | 20 bps | 50 bps |" in md, md
assert "All 4 weeks reconcile exactly" in md
assert "Not yet written." in md and "Protocol positions together: −2,500 rETH" in md
assert "a gap of −30 bps" in md and "−0.10%" in md

# ---- hand-written notes replace the open questions ---------------------------------------------
os.makedirs("reports/notes")
with open("reports/notes/2024-02.md", "w", encoding="utf-8") as f:
    f.write("Spark lowered rETH's supply cap on 12 February (test note).\n")
md = open(report.main(), encoding="utf-8").read()
assert "Spark lowered rETH's supply cap on 12 February (test note)." in md and "Not yet written." not in md

# ---- a week that does not reconcile is flagged ------------------------------------------------
w = pd.read_csv("reth_study/holders_weeks.csv")
w.loc[2, "minted"] = 260.0
w.to_csv("reth_study/holders_weeks.csv", index=False)
md = open(report.main(), encoding="utf-8").read()
assert "**Warning:** not every week" in md and "All 4 weeks reconcile" not in md

# ---- no monthly data yet: nothing written, no error ------------------------------------------
os.remove("reth_study/holders_month.csv")
assert report.main() is None
print("ALL REPORT TESTS PASSED")
