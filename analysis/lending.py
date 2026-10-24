"""Does lending demand for rETH follow the looping spread?

On Aave v3 and Spark, most rETH is supplied as collateral to borrow WETH and buy more rETH
("looping"). A loop pays only while the staking yield beats the WETH borrow rate, and its
return on equity moves with leverage times that spread, so a small change in rETH's
yield could matter more here than anywhere else.

At each weekly block of the study this script reads, from an archive node:
  - rETH and wstETH supplied on each market (aToken total supply),
  - the WETH variable borrow rate on each market (Pool.getReserveData),
and tests whether weekly changes in supplied rETH follow the lagged spread between
rETH's yield and the WETH borrow rate, with wstETH as the benchmark.

Run after the notebook (it reads reth_study/weekly_panel.csv):
    python analysis/lending.py
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import holders  # noqa: E402  (sources, archive detection, debug log)

OUT_DIR = holders.OUT_DIR
RETH = holders.RETH
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
WSTETH = "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
VENUES = {  # the rETH aToken of each market; the pool and the other reserves are read from it
    "Aave v3": "0xcc9ee9483f662091a1de4795249e24ac0ac2630f",
    "Spark": "0x9985df20d7e9103ecbceb16a84956434b6f06ae8",
}
RAY = 1e27
SEL = {"POOL": "0x7535d246", "UNDERLYING": "0xb16a19de", "totalSupply": "0x18160ddd",
       "getReserveData": "0x35ea6a75"}
LAGS = (1, 4)
dbg = holders.dbg


def get_archive():
    """Public endpoints that serve archive state, checked against the panel's rETH supply."""
    panel = pd.read_csv(os.path.join(OUT_DIR, "weekly_panel.csv"))
    cands = holders.build_sources()
    probes = holders.probe_all(cands, int(panel["block"].iloc[0]), logs=False)
    rpcs = [s for s, ok in zip(cands, probes["chain_ok"]) if ok and isinstance(s, holders.RpcSource)]
    return holders.find_archive(rpcs, int(panel["block"].iloc[0]), float(panel["reth_supply"].iloc[0]))


def call(archive, to, data, block="latest"):
    """eth_call through the first archive source that answers; '0x' when there is no code."""
    tag = hex(block) if isinstance(block, (int, np.integer)) else block
    last = None
    for src in archive[:4]:
        try:
            return src.call("eth_call", [{"to": to, "data": data}, tag], timeout=30)
        except Exception as e:  # try the next source
            last = e
    raise RuntimeError(f"eth_call to {to} failed on every source: {last}")


def word(res, i):
    return int(res[2 + 64 * i: 2 + 64 * (i + 1)], 16)


def addr(res, i=0):
    return "0x" + res[2 + 64 * i: 2 + 64 * (i + 1)][-40:]


def reserve(archive, pool, asset, block="latest"):
    """Supply rate, variable borrow rate (APR) and aToken of a reserve; None if not listed."""
    res = call(archive, pool, SEL["getReserveData"] + asset[2:].rjust(64, "0"), block)
    if not res or len(res) < 2 + 64 * 9 or word(res, 8) == 0:
        return None
    return {"supply_apr": word(res, 2) / RAY, "borrow_apr": word(res, 4) / RAY, "atoken": addr(res, 8)}


def supply(archive, atoken, block):
    res = call(archive, atoken, SEL["totalSupply"], block)
    return int(res, 16) / 1e18 if res and len(res) > 2 else 0.0


def setup(archive):
    """Pool and aTokens of each market, with a check that the struct layout reads as expected."""
    markets = {}
    for venue, a_reth in VENUES.items():
        pool = addr(call(archive, a_reth, SEL["POOL"]))
        underlying = addr(call(archive, a_reth, SEL["UNDERLYING"]))
        if underlying != RETH:
            raise RuntimeError(f"{venue}: aToken underlying is {underlying}, not rETH")
        r = reserve(archive, pool, RETH)
        if r is None or r["atoken"] != a_reth:
            raise RuntimeError(f"{venue}: getReserveData(rETH) does not return the rETH aToken; layout changed?")
        w = reserve(archive, pool, WSTETH)
        markets[venue] = {"pool": pool, "a_reth": a_reth, "a_wsteth": w["atoken"] if w else None}
        dbg(f"{venue}: pool {pool}, wstETH aToken {markets[venue]['a_wsteth']}")
    return markets


def read_panel(archive, markets, panel):
    rows = []
    for k, r in panel.iterrows():
        b = int(r["block"])
        for venue, m in markets.items():
            weth = reserve(archive, m["pool"], WETH, b)
            rows.append({"date": r["date"], "block": b, "venue": venue,
                         "reth_supplied": supply(archive, m["a_reth"], b),
                         "wsteth_supplied": supply(archive, m["a_wsteth"], b) if m["a_wsteth"] else np.nan,
                         "weth_borrow_apr": weth["borrow_apr"] if weth else np.nan,
                         "weth_supply_apr": weth["supply_apr"] if weth else np.nan})
        if k % 25 == 0:
            dbg(f"  lending: week {k + 1}/{len(panel)}")
    return pd.DataFrame(rows)


def add_spreads(df, panel):
    """Spread in bps between the 4-week staking yield and the WETH borrow rate, and weekly flows."""
    y = panel[["date", "reth_apr", "steth_apr", "eth_usd"]].copy()
    y["reth_apr_4w"] = y["reth_apr"].rolling(4, min_periods=2).mean()
    y["steth_apr_4w"] = y["steth_apr"].rolling(4, min_periods=2).mean()
    y["eth_ret"] = np.log(y["eth_usd"]).diff()
    df = df.merge(y[["date", "reth_apr_4w", "steth_apr_4w", "eth_ret"]], on="date", how="left")
    df["reth_spread_bps"] = (df["reth_apr_4w"] - df["weth_borrow_apr"]) * 1e4
    df["wsteth_spread_bps"] = (df["steth_apr_4w"] - df["weth_borrow_apr"]) * 1e4
    df = df.sort_values(["venue", "date"]).reset_index(drop=True)
    for a in ("reth", "wsteth"):
        s = df[f"{a}_supplied"].where(df[f"{a}_supplied"] > 0)
        df[f"{a}_flow"] = np.log(s).groupby(df["venue"]).diff()
    return df


def combined(df):
    """Both markets together: total supplied and the supply-weighted spread."""
    out = []
    for a in ("reth", "wsteth"):
        g = df.groupby("date")
        tot = g[f"{a}_supplied"].sum()
        w = df.assign(w=df[f"{a}_supplied"] * df[f"{a}_spread_bps"]).groupby("date")["w"].sum() / tot
        out.append(pd.DataFrame({f"{a}_supplied": tot, f"{a}_spread_bps": w}))
    c = pd.concat(out, axis=1).reset_index()
    c = c.merge(df.drop_duplicates("date")[["date", "eth_ret"]], on="date", how="left")
    c["venue"] = "Aave v3 + Spark"
    for a in ("reth", "wsteth"):
        c[f"{a}_flow"] = np.log(c[f"{a}_supplied"].where(c[f"{a}_supplied"] > 0)).diff()
    return c


def regress(df):
    """Weekly flow (% of supplied) on the lagged spread (per 10 bps) and the ETH price change."""
    import statsmodels.api as sm
    rows = []
    for venue, g in df.groupby("venue"):
        g = g.sort_values("date")
        for a in ("reth", "wsteth"):
            for lag in LAGS:
                d = pd.DataFrame({"flow": g[f"{a}_flow"] * 100,
                                  "spread": g[f"{a}_spread_bps"].shift(lag) / 10,
                                  "eth_ret": g["eth_ret"]}).dropna()
                if len(d) < 20 or d["spread"].std() == 0:
                    continue
                m = sm.OLS(d["flow"], sm.add_constant(d[["spread", "eth_ret"]])).fit(
                    cov_type="HAC", cov_kwds={"maxlags": 4})
                lo, hi = m.conf_int().loc["spread"]
                rows.append({"venue": venue, "asset": "rETH" if a == "reth" else "wstETH", "lag_weeks": lag,
                             "beta_pct_per_week_per_10bps": m.params["spread"], "ci_low": lo, "ci_high": hi,
                             "p_value": m.pvalues["spread"], "r2": m.rsquared, "n_weeks": int(m.nobs)})
    return pd.DataFrame(rows)


def summary(df):
    rows = []
    for venue, g in df.groupby("venue"):
        for a in ("reth", "wsteth"):
            s = g[f"{a}_spread_bps"].dropna()
            sup = g[f"{a}_supplied"]
            if not len(s):
                continue
            rows.append({"venue": venue, "asset": "rETH" if a == "reth" else "wstETH",
                         "weeks": len(s), "share_weeks_spread_positive": float((s > 0).mean()),
                         "median_spread_bps": float(s.median()), "last_spread_bps": float(s.iloc[-1]),
                         "supplied_first": float(sup.iloc[0]), "supplied_peak": float(sup.max()),
                         "supplied_last": float(sup.iloc[-1])})
    return pd.DataFrame(rows)


def chart(df, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True, gridspec_kw={"height_ratios": [1, 1]})
    colors = {"Aave v3": "#4c72b0", "Spark": "#dd8452"}
    for venue, g in df[df["venue"] != "Aave v3 + Spark"].groupby("venue"):
        a1.plot(g["date"], g["reth_spread_bps"], color=colors.get(venue), lw=1.6, label=f"rETH on {venue}")
        a2.plot(g["date"], g["reth_supplied"] / 1e3, color=colors.get(venue), lw=1.6, label=f"rETH on {venue}")
    a1.axhline(0, color="black", lw=0.8)
    a1.set_ylabel("rETH yield minus WETH borrow rate (bps)")
    a1.set_title("Looping spread and rETH supplied on Aave v3 and Spark")
    a1.legend(frameon=False)
    a1.grid(alpha=0.3)
    a2.set_ylabel("rETH supplied (thousand)")
    a2.legend(frameon=False)
    a2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)


def chart_index(df, summ, path):
    """rETH against wstETH supplied on both markets, indexed to the first week."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    c = df[df["venue"] == "Aave v3 + Spark"].sort_values("date")
    med = summ[summ["venue"] == "Aave v3 + Spark"].set_index("asset")["median_spread_bps"]
    fig, ax = plt.subplots(figsize=(10, 5.2))
    for a, label, color, lw in (("wsteth", "wstETH", "#9e9e9e", 2.0), ("reth", "rETH", "#2b6cb0", 2.4)):
        idx = c[f"{a}_supplied"] / c[f"{a}_supplied"].iloc[0] * 100
        ax.plot(c["date"], idx, color=color, lw=lw)
        ax.text(c["date"].iloc[-1], idx.iloc[-1], f"  {label} {idx.iloc[-1]:.0f}", color=color, va="center",
                fontsize=10, fontweight="bold" if a == "reth" else "normal")
    ax.axhline(100, color="#777777", lw=0.8, ls="--")
    ax.set_ylabel("Supplied on Aave v3 + Spark (first week = 100)")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(alpha=0.25, axis="y")
    fig.suptitle("Same markets, same borrowing cost: rETH loops halved while wstETH loops grew",
                 x=0.01, ha="left", fontsize=13, fontweight="bold")
    ax.set_title(f"Median looping spread (staking yield minus WETH borrow rate): rETH {med.get('rETH', float('nan')):.0f} bps, "
                 f"wstETH {med.get('wstETH', float('nan')):.0f} bps", loc="left", fontsize=9, color="#555555")
    fig.tight_layout()
    fig.savefig(path, dpi=150)


def main():
    panel = pd.read_csv(os.path.join(OUT_DIR, "weekly_panel.csv"))
    panel["date"] = pd.to_datetime(panel["date"], utc=True)
    panel = panel.sort_values("block").reset_index(drop=True)
    archive = get_archive()
    if not archive:
        raise RuntimeError("No archive endpoint answered; add an ALCHEMY_KEY secret and rerun.")
    dbg(f"Lending: archive sources {', '.join(s.label for s in archive[:3])}")
    markets = setup(archive)
    df = add_spreads(read_panel(archive, markets, panel), panel)
    df = pd.concat([df, combined(df)], ignore_index=True)
    df.to_csv(os.path.join(OUT_DIR, "lending_panel.csv"), index=False)
    reg = regress(df)
    reg.to_csv(os.path.join(OUT_DIR, "lending_regression.csv"), index=False)
    summ = summary(df)
    summ.to_csv(os.path.join(OUT_DIR, "lending_summary.csv"), index=False)
    chart(df, os.path.join(OUT_DIR, "chart_lending_spread.png"))
    chart_index(df, summ, os.path.join(OUT_DIR, "chart_lending_reth_vs_wsteth.png"))
    print(summ.round(3).to_string())
    print(reg.round(4).to_string())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        dbg(f"LENDING FAILED: {type(e).__name__}: {e}")
        raise
    finally:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "lending_debug.log"), "w") as f:
            f.write("\n".join(holders.DEBUG_LINES[-300:]) + "\n")
