"""Offline test for analysis/report.py.

Builds a small set of holder, lending and panel outputs for February 2024 with known numbers,
runs the report, and checks the figures, the privacy rule for wallets, dust filtering, the
wallet-pair grouping, the hand-written notes and the reconciliation check.

Run: python tests/test_report.py
"""
import os
import sys
import tempfile

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "analysis"))
import report  # noqa: E402

M = "−"
work = tempfile.mkdtemp(prefix="report_test_")
os.chdir(work)
os.makedirs("reth_study")
report.OUT_DIR, report.REPORTS_DIR = "reth_study", "reports"
os.environ["GITHUB_SHA"] = "abcdef1234567890"

SPARK, WALLET, BAL = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
W2, W3, FRIEND, ARB, POOL = ("0x" + "55" * 20, "0x" + "66" * 20, "0x" + "44" * 20, "0x" + "77" * 20,
                             "0x" + "88" * 20)
POISON = "0x55" + "ab" * 18 + "5555"

# ---- weekly panel: 6 Jan ... 24 Feb 2024; exchange rates grow at exactly 2.7% and 3.0% a year ----
dates = pd.date_range("2024-01-06", periods=8, freq="7D", tz="UTC")
supply = [100_000, 100_500, 101_000, 102_000, 101_000, 100_000, 99_000, 98_000]
t = (dates - dates[0]).days.to_numpy()
panel = pd.DataFrame({"date": dates, "block": range(1_000, 1_008), "reth_supply": supply,
                      "reth_rate": 1.1 * 1.027 ** (t / 365), "steth_rate": 1.2 * 1.030 ** (t / 365),
                      "reth_apr": 0.027, "steth_apr": 0.030, "gap_bps": -30.0,
                      "premium_pct": [-0.1, -0.1, -0.1, -0.1, -0.2, 0.05, -0.31, -0.1]})
panel["reth_backing_eth"] = panel["reth_supply"] * panel["reth_rate"]
panel["steth_pooled_eth"] = 1_000_000.0 * panel["steth_rate"]
panel.loc[7, ["steth_apr", "gap_bps"]] = [0.050, -230.0]          # one odd week, as oracle timing can cause
panel.to_csv("reth_study/weekly_panel.csv", index=False)

pd.DataFrame([{"month": "2024-02", "start_snapshot": "2024-01-27", "end_snapshot": "2024-02-24", "weeks": 4,
               "block_from": 1_004, "block_to": 1_007, "supply_start": 102_000, "supply_end": 98_000,
               "supply_change": -4_000, "transfers": 1_234, "minted": 1_000, "burned": 5_000}]).to_csv(
    "reth_study/holders_month.csv", index=False)
pd.DataFrame({"week_ending": ["2024-01-13", "2024-01-20", "2024-01-27",
                             "2024-02-03", "2024-02-10", "2024-02-17", "2024-02-24"],
              "minted": [600.0, 600.0, 1_100.0, 250.0, 250.0, 250.0, 250.0],
              "burned": [100.0, 100.0, 100.0, 500.0, 500.0, 3_500.0, 500.0],
              "supply_change": [500.0, 500.0, 1_000.0, -250.0, -250.0, -3_250.0, -250.0],
              "source": ["good"] * 7}).to_csv("reth_study/holders_weeks.csv", index=False)
cat = pd.DataFrame({"net_since_2024": [-5_000, -1_000, 400, -2_000, 600], "addresses_since_2024": [1, 3, 2, 5, 900],
                    "net_since_saturn1": [0, -500, 0, -100, 100], "addresses_since_saturn1": [0, 2, 0, 1, 50],
                    "net_month": [0, -3_000, 500, -1_200, -300], "addresses_month": [0, 2, 1, 3, 400]},
                   index=["Restaking", "Lending and CDPs", "DEX liquidity", "Large wallets", "Other addresses"])
cat.loc["Total (= supply change)"] = cat.sum()
cat.to_csv("reth_study/holders_by_category.csv")
pd.DataFrame({"category": ["Lending and CDPs", "DEX liquidity", "Lending and CDPs"],
              "net_since_2024": [-1_000, 400, 0], "net_since_saturn1": [-500, 0, 0], "net_month": [-3_000, 500, 5],
              "balance_month_start": [5_000, 100, 50], "balance_month_end": [2_000, 600, 55]},
             index=pd.Index(["Spark", "Balancer", "Aave v3"], name="entity")).to_csv("reth_study/holders_by_entity.csv")
pd.DataFrame({"net": [-3_000, -1_200, 500, -1_500, 1_500], "label": ["Spark spRETH", "", "Balancer V2 Vault", "", ""],
              "entity": ["Spark", "", "Balancer", "", ""],
              "holder_type": ["Lending and CDPs", "Large wallets", "DEX liquidity", "Large wallets", "Large wallets"],
              "kind": ["contract", "wallet", "contract", "wallet", "wallet"],
              "who": ["Spark spRETH", "someone.eth", "Balancer V2 Vault", "0x5555...5555", "0x6666...6666"],
              "ens": ["", "someone.eth", "", "", ""], "balance_start": [5_000, 1_200, 100, 1_500, 0],
              "balance_end": [2_000, 0, 600, 0, 1_500]},
             index=pd.Index([SPARK, WALLET, BAL, W2, W3], name="address")).to_csv("reth_study/holders_month_movers.csv")
pd.DataFrame({"address": [SPARK, WALLET, WALLET, W2, W2, W3],
              "direction": ["sent to", "sent to", "sent to", "sent to", "sent to", "received from"],
              "counterparty": [WALLET, "0x" + "00" * 20, FRIEND, W3, POISON, W2],
              "counterparty_name": ["0x2222...2222", "rETH contract (burned: redeemed for ETH)", "friend.eth",
                                    "0x6666...6666", "0x55ab...5555", "0x5555...5555"],
              "counterparty_kind": ["wallet", "reth", "wallet", "wallet", "wallet", "wallet"],
              "reth": [3_000, 1_100, 100, 1_500, 0.0, 1_500],
              "share": [1.0, 1_100 / 1_200, 100 / 1_200, 1.0, 0.0, 1.0]}).to_csv(
    "reth_study/holders_month_counterparties.csv", index=False)
pd.DataFrame({"side": ["burned", "burned", "minted"], "address": [ARB, WALLET, FRIEND],
              "reth": [3_900, 1_100, 800], "share": [0.78, 0.22, 0.8],
              "peak_week_ending": ["2024-02-17", "2024-02-10", "2024-02-03"], "peak_week_reth": [3_000, 1_100, 800],
              "kind": ["contract", "wallet", "wallet"], "label": ["", "", ""], "entity": ["", "", ""],
              "who": ["ArbBot", "someone.eth", "friend.eth"]}).to_csv("reth_study/holders_month_mint_burn.csv",
                                                                      index=False)
pd.DataFrame({"net": [-200.0, -60.0, 80.0], "kind": ["wallet", "contract", "wallet"], "who": ["", "UniswapV3Pool", ""]},
             index=pd.Index([FRIEND, POOL, "0x" + "99" * 20], name="address")).to_csv("reth_study/holders_month_others.csv")
rows = []
for d, i in zip(dates, range(8)):
    rows.append({"date": d, "venue": "Aave v3", "reth_supplied": 8_000 + 10 * i, "wsteth_supplied": 50_000,
                 "reth_spread_bps": 10.0 + i, "wsteth_spread_bps": 40.0, "weth_borrow_apr": 0.02})
    rows.append({"date": d, "venue": "Spark", "reth_supplied": 5_000 if d <= dates[3] else 2_000,
                 "wsteth_supplied": 30_000 + 1_000 * i, "reth_spread_bps": 20.0 if i < 7 else -150.0,
                 "wsteth_spread_bps": 50.0 if i < 7 else -120.0, "weth_borrow_apr": 0.02 if i < 7 else 0.045})
pd.DataFrame(rows).to_csv("reth_study/lending_panel.csv", index=False)

# ---- full run --------------------------------------------------------------------------------
path = report.main()
md = open(path, encoding="utf-8").read()
assert path.endswith(os.path.join("reports", "2024-02.md")), path
assert os.path.exists("reports/2024-02-holders.png")
assert md.startswith("# rETH Demand Monitor: February 2024"), md[:80]
assert "between 27 January 2024 and 24 February 2024 (4 weeks)" in md
# summary
assert f"from 102,000 to 98,000 ({M}4,000 rETH, {M}3.9%)" in md, md
assert "The ETH backing rETH changed by" in md and "and stETH's by" in md
assert ("5,000 rETH burned for ETH and 1,000 minted in 4 weeks, or 1,250 and 250 a week, against 100 and 767 a week "
        "over the previous month") in md, md
assert "The week to 17 February 2024 burned 3,500 rETH, 70% of the month's burns." in md
assert "The largest burner was contract ArbBot ([0x777777…777777]" in md
assert (f"- **Largest net decreases:** Spark {M}3,000 (identified protocols and treasuries); other addresses, "
        f"each moving under 500 rETH, {M}300 in total.") in md, md
assert "- **Wallet-to-wallet transfers:** 1 transfer moved 1,500 rETH to new addresses" in md
assert (f"Looping rETH paid a median +16 bps on Aave v3 and +20 bps on Spark, {M}24 bps against wstETH on the same "
        "markets (4-week average staking yields).") in md, md
assert "rETH's APR over the month was 2.70% and stETH's 3.00%, a gap of " + M + "30 bps" in md, md
assert f"Without the week flagged in section 4 the gap was {M}30 bps; section 2 explains that week." in md, md
assert f"| Gap without the flagged week | {M}30 bps |" in md
# section 1
assert f"| Lending and CDPs | {M}3,000 | 2 |" in md and f"| **Total (= supply change)** | **{M}4,000** | |" in md
assert "| Other addresses (each moved under 500 rETH) | " + M + "300 | 400 |" in md
assert (f"those that moved 50 rETH or more were 2 wallets ({M}120), 1 contract ({M}60); everyone else came to "
        f"{M}120. Largest contracts: UniswapV3Pool") in md, md
assert f"| Spark | Lending and CDPs | {M}3,000 | 5,000 → 2,000 | {M}60% | {M}500 | {M}1,000 |" in md, md
assert "Aave v3's WETH borrow rate" not in md                       # mildly negative weeks are not flagged
assert "| Balancer | DEX liquidity | +500 | 100 → 600 | +500% | 0 | +400 |" in md, md
assert "Aave v3 | Lending and CDPs" not in md                      # moved under 10 rETH: not listed
assert "Spark spRETH ([0x111111…111111](https://etherscan.io/address/" + SPARK + "))" in md
assert "Unlabelled wallet [0x222222…222222]" in md
assert "someone.eth" not in md and "friend.eth" not in md          # wallets by address only, never by ENS
assert "| 100% sent to a wallet [0x222222…222222](https://etherscan.io/address/" + WALLET + ") |" in md
assert "| 92% sent to rETH contract (burned: redeemed for ETH) |" in md and POISON not in md
assert "(wallet-to-wallet transfer) |" in md
assert ("| Contract ArbBot ([0x777777…777777](https://etherscan.io/address/" + ARB
        + ")) | 3,900 | 78% | 17 February 2024 (3,000) |") in md
assert "Largest mints: Unlabelled wallet [0x444444…444444]" in md
# section 2: no notes yet
assert "Not yet written." in md
# section 3
assert f"| Spark | 5,000 → 2,000 | 2.0% | 33,000 → 37,000 | +20 bps | +50 bps | {M}30 bps |" in md, md
assert (f"At the 24 February 2024 snapshot, Spark's WETH borrow rate was 4.50%; at that rate loops would pay "
        f"{M}150 bps on rETH") in md
# section 4
assert f"| Weekly gaps, in order (bps) | {M}30, {M}30, {M}30, {M}230 |" in md
assert "| Gap in the months before (bps) | Jan " in md
assert (f"| rETH market price vs protocol rate, weekly snapshots (DefiLlama prices) | {M}0.20%, +0.05%, "
        f"{M}0.31%, {M}0.10% |") in md, md
assert "stETH's weekly APR in the week to 24 February 2024 was 5.00%" in md
# section 5 and checks
assert (f"From 6 January 2024 to 24 February 2024, rETH supply changed by {M}7,000; protocol positions account "
        "for 80%") in md
assert "All weeks passed." in md
assert ("at commit abcdef1: [holders_month.csv](https://github.com/NishikawaButterfly/reth-competitiveness-study/"
        "blob/abcdef1234567890/reth_study/holders_month.csv)") in md

# ---- hand-written notes replace the open questions ---------------------------------------------
os.makedirs("reports/notes")
with open("reports/notes/2024-02.md", "w", encoding="utf-8") as f:
    f.write("Spark lowered rETH's supply cap on 12 February (test note).\n")
md = open(report.main(), encoding="utf-8").read()
assert "Spark lowered rETH's supply cap on 12 February (test note)." in md and "Not yet written." not in md

# ---- a week that does not reconcile is flagged ------------------------------------------------
w = pd.read_csv("reth_study/holders_weeks.csv")
w.loc[5, "minted"] = 260.0          # a February week
w.to_csv("reth_study/holders_weeks.csv", index=False)
md = open(report.main(), encoding="utf-8").read()
assert "**Warning:** not every week" in md and "All weeks passed." not in md

# ---- no monthly data yet: nothing written, no error ------------------------------------------
os.remove("reth_study/holders_month.csv")
assert report.main() is None
print("ALL REPORT TESTS PASSED")
