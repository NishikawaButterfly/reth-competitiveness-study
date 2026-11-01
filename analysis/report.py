"""Monthly rETH Demand Monitor report.

Reads the outputs of the holder analysis (reth_study/holders_*.csv), the lending analysis
(reth_study/lending_panel.csv) and the weekly panel (reth_study/weekly_panel.csv), and writes
reports/<YYYY-MM>.md with a chart of the month's net change by holder type.

The section on why the large moves happened is written by hand: it is read from
reports/notes/<YYYY-MM>.md when that file exists, and listed as open questions otherwise.

Run after holders.py and lending.py:
    python analysis/report.py
"""
import os

import numpy as np
import pandas as pd

OUT_DIR = os.environ.get("OUT_DIR", "reth_study")
REPORTS_DIR = os.environ.get("REPORTS_DIR", "reports")
REPO = "https://github.com/NishikawaButterfly/reth-competitiveness-study"
MOVER = 1_000.0
VENUES = ["Aave v3", "Spark"]
PROTOCOL_TYPES = ["Restaking", "Lending and CDPs", "DEX liquidity", "L2 bridges", "Funds and DAO treasuries"]
TYPE_LABEL = {"Large wallets": "Large wallets (500+ rETH moved)",
              "Unidentified contracts": "Unidentified contracts (500+ rETH moved)",
              "Other addresses": "Addresses that moved under 500 rETH"}
KIND_LABEL = {"wallet": "wallet", "safe": "Safe multisig", "contract": "contract"}
MINUS = "−"
TOTAL = "Total (= supply change)"


def signed(v, digits=0):
    if pd.notna(v) and abs(v) < 0.5 * 10 ** -digits:
        return "0"
    return f"{v:+,.{digits}f}".replace("-", MINUS)


def num(v, digits=0):
    return f"{v:,.{digits}f}".replace("-", MINUS)


def day(d):
    return f"{d.day} {d:%B %Y}"


def read(name, **kw):
    path = os.path.join(OUT_DIR, name)
    return pd.read_csv(path, **kw) if os.path.exists(path) else None


def etherscan(addr):
    return f"[{addr[:6]}…{addr[-4:]}](https://etherscan.io/address/{addr})"


def mover_name(addr, r):
    """Protocols by their label; wallets by address only, never by ENS or other personal names."""
    if isinstance(r.get("entity"), str) and r["entity"]:
        label = r["label"] if isinstance(r.get("label"), str) and r["label"] else r["entity"]
        return f"{label} ({etherscan(addr)})"
    kind = r.get("kind") if isinstance(r.get("kind"), str) else ""
    if kind == "contract" and isinstance(r.get("who"), str) and r["who"] and not r["who"].startswith("0x"):
        return f"Contract: {r['who']} ({etherscan(addr)})"
    return f"Unlabelled {KIND_LABEL.get(kind, 'address')} {etherscan(addr)}"


def waterfall(steps, path, title, subtitle):
    """Net change by holder type: protocol types first with a subtotal, then the rest, then the total."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter
    prot = steps[[c for c in PROTOCOL_TYPES if c in steps.index]].sort_values()
    rest = steps[[c for c in steps.index if c not in prot.index]]
    bars, cum = [], 0.0
    for c, v in prot.items():
        bars.append((c, cum, cum + v, v, "step"))
        cum += v
    bars.append(("Protocols, subtotal", 0.0, cum, cum, "sum"))
    for c, v in rest.items():
        bars.append((TYPE_LABEL.get(c, c), cum, cum + v, v, "step"))
        cum += v
    bars.append(("rETH supply change", 0.0, cum, cum, "sum"))
    lo = min(min(a, b) for _, a, b, _, _ in bars)
    hi = max(max(a, b) for _, a, b, _, _ in bars)
    pad = (hi - lo) * 0.18 or 1.0
    fig, ax = plt.subplots(figsize=(10, 5.4))
    for i, (lab, a, b, v, kind) in enumerate(bars):
        color = "#9e9e9e" if kind == "sum" else ("#c44e52" if v < 0 else "#55a868")
        ax.barh(i, b - a, left=a, color=color, alpha=0.85 if kind == "sum" else 0.7, height=0.62)
        x, ha = (min(a, b) - pad * 0.08, "right") if v < 0 else (max(a, b) + pad * 0.08, "left")
        ax.text(x, i, signed(v), va="center", ha=ha, fontsize=9, fontweight="bold" if kind == "sum" else "normal")
    ax.set_yticks(range(len(bars)), [b[0] for b in bars])
    for t, b in zip(ax.get_yticklabels(), bars):
        t.set_fontweight("bold" if b[4] == "sum" else "normal")
    ax.invert_yaxis()
    ax.axvline(0, color="black", lw=0.8)
    ax.set_xlim(lo - pad, hi + pad)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: num(x)))
    ax.set_xlabel("Net change in rETH held")
    ax.grid(alpha=0.25, axis="x")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.suptitle(title, x=0.01, y=0.985, ha="left", fontsize=13, fontweight="bold")
    fig.text(0.01, 0.925, subtitle, ha="left", fontsize=9, color="#555555")
    fig.tight_layout(rect=(0, 0, 1, 0.915))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def lending_rows(lend, start, end, supply_end):
    """Per venue: deposits at both ends of the month, share of all rETH, and the month's median loop spreads."""
    rows = []
    if lend is None:
        return rows
    lend = lend.copy()
    lend["date"] = pd.to_datetime(lend["date"], utc=True)
    for v in VENUES:
        d = lend[lend["venue"] == v].sort_values("date")
        at_start, upto_end = d[d["date"] <= start], d[d["date"] <= end]
        inside = d[(d["date"] > start) & (d["date"] <= end)]
        if at_start.empty or upto_end.empty or inside.empty:
            continue
        s, e = at_start.iloc[-1], upto_end.iloc[-1]
        rows.append({"venue": v, "end_date": e["date"], "reth_start": s["reth_supplied"], "reth_end": e["reth_supplied"],
                     "share_end": e["reth_supplied"] / supply_end * 100,
                     "wsteth_start": s["wsteth_supplied"], "wsteth_end": e["wsteth_supplied"],
                     "reth_spread": inside["reth_spread_bps"].median(),
                     "wsteth_spread": inside["wsteth_spread_bps"].median()})
    return rows


def main():
    meta = read("holders_month.csv")
    if meta is None or meta.empty:
        print("No monthly holder data yet (reth_study/holders_month.csv): nothing to report.")
        return None
    meta = meta.iloc[0]
    month = str(meta["month"])
    start = pd.Timestamp(meta["start_snapshot"], tz="UTC")
    end = pd.Timestamp(meta["end_snapshot"], tz="UTC")
    title_month = pd.Period(month, "M").strftime("%B %Y")

    cat = read("holders_by_category.csv", index_col=0)
    ent = read("holders_by_entity.csv", index_col=0)
    movers = read("holders_month_movers.csv", index_col=0)
    weeks = read("holders_weeks.csv")
    panel = read("weekly_panel.csv")
    lend = read("lending_panel.csv")
    if cat is None or "net_month" not in cat:
        raise RuntimeError("holders_by_category.csv has no month column: run analysis/holders.py first.")

    panel["date"] = pd.to_datetime(panel["date"], utc=True)
    inside = panel[(panel["date"] > start) & (panel["date"] <= end)]
    s0, s1, change = float(meta["supply_start"]), float(meta["supply_end"]), float(meta["supply_change"])

    steps = cat.drop(index=TOTAL)["net_month"]
    steps = steps[steps.abs() > 1e-9]
    prot = steps[[c for c in PROTOCOL_TYPES if c in steps.index]]
    os.makedirs(REPORTS_DIR, exist_ok=True)
    chart = f"{month}-holders.png"
    waterfall(steps, os.path.join(REPORTS_DIR, chart),
              f"rETH supply {signed(change)} in {title_month}: who moved it",
              f"Net change by holder type, weekly snapshots {start.day} {start:%b} to {end.day} {end:%b %Y}. "
              "Red: decrease. Green: increase. Grey: subtotal and total.")

    big = movers[movers["net"].abs() >= MOVER] if movers is not None else pd.DataFrame()
    shown = big if not big.empty else (pd.concat([movers.nsmallest(3, "net"), movers.nlargest(3, "net")])
                                       if movers is not None else pd.DataFrame())
    lrows = lending_rows(lend, start, end, s1)

    # ---- summary ----
    order = steps.sort_values()
    lines = [f"# rETH Demand Monitor: {title_month}", "",
             f"Weekly on-chain snapshots from {day(start)} to {day(end)} ({int(meta['weeks'])} weeks, blocks "
             f"{int(meta['block_from']):,} to {int(meta['block_to']):,}). Generated automatically from the "
             f"[open repository]({REPO}); the files behind every figure are listed at the end.", "",
             "## Summary", "",
             f"- **Supply:** rETH supply went from {num(s0)} to {num(s1)} ({signed(change)} rETH, "
             f"{signed(change / s0 * 100, 1)}%)."]
    if len(order):
        top_neg = [f"{TYPE_LABEL.get(c, c)} {signed(v)}" for c, v in order.items() if v < 0][:2]
        top_pos = [f"{TYPE_LABEL.get(c, c)} {signed(v)}" for c, v in order[::-1].items() if v > 0][:2]
        parts = []
        if top_neg:
            parts.append("largest decreases: " + ", ".join(top_neg))
        if top_pos:
            parts.append("largest increases: " + ", ".join(top_pos))
        lines.append(f"- **By holder type:** {'; '.join(parts)}. Protocol positions together: {signed(prot.sum())} rETH.")
    if not shown.empty:
        names = [f"{mover_name(a, r)} {signed(r['net'])}" for a, r in shown.reindex(
            shown["net"].abs().sort_values(ascending=False).index).head(3).iterrows()]
        lines.append(f"- **Largest single moves:** {'; '.join(names)}.")
    if lrows:
        rs = sum(r["reth_start"] for r in lrows)
        re_ = sum(r["reth_end"] for r in lrows)
        lines.append(f"- **Lending:** rETH deposited on {' and '.join(r['venue'] for r in lrows)} went from {num(rs)} to "
                     f"{num(re_)}. Median loop spread: " + "; ".join(
                         f"{r['venue']} {num(r['reth_spread'])} bps for rETH and {num(r['wsteth_spread'])} bps for wstETH"
                         for r in lrows) + ".")
    if not inside.empty:
        lines.append(f"- **Yield:** median weekly APR {inside['reth_apr'].median() * 100:.2f}% for rETH and "
                     f"{inside['steth_apr'].median() * 100:.2f}% for stETH, a gap of "
                     f"{signed(inside['gap_bps'].median())} bps.")

    # ---- 1. supply and yield ----
    lines += ["", "## 1. Supply and yield", "", "| Measure | Value |", "| --- | --- |",
              f"| rETH supply, start → end | {num(s0)} → {num(s1)} ({signed(change)}) |",
              f"| Minted / burned in the month | {num(meta['minted'])} / {num(meta['burned'])} |",
              f"| Transfers in the month | {int(meta['transfers']):,} |"]
    if not inside.empty:
        lines += [f"| rETH APR, weekly median | {inside['reth_apr'].median() * 100:.2f}% |",
                  f"| stETH APR, weekly median | {inside['steth_apr'].median() * 100:.2f}% |",
                  f"| Yield gap rETH − stETH, weekly median | {signed(inside['gap_bps'].median())} bps |"]
        if "premium_pct" in inside and inside["premium_pct"].notna().any():
            lines.append(f"| rETH market price vs protocol rate, weekly median | "
                         f"{signed(inside['premium_pct'].median(), 2)}% |")

    # ---- 2. who moved ----
    lines += ["", "## 2. Who moved rETH", "", f"![rETH net change by holder type, {title_month}]({chart})", "",
              "| Holder type | Net change (rETH) | Addresses |", "| --- | ---: | ---: |"]
    for c in cat.index:
        if c == TOTAL:
            continue
        v, n = cat.loc[c, "net_month"], cat.loc[c, "addresses_month"] if "addresses_month" in cat else np.nan
        if abs(v) > 1e-9:
            lines.append(f"| {TYPE_LABEL.get(c, c)} | {signed(v)} | {int(n):,} |")
    lines.append(f"| **Total (= supply change)** | **{signed(cat.loc[TOTAL, 'net_month'])}** | |")

    if ent is not None and "net_month" in ent:
        e = ent[ent["net_month"].abs() >= 10].sort_values("net_month")
        if not e.empty:
            lines += ["", "**Identified protocols and treasuries**", "",
                      "| Entity | Type | Net change in the month | Since Saturn 1 | Since January 2024 |",
                      "| --- | --- | ---: | ---: | ---: |"]
            for name, r in e.iterrows():
                lines.append(f"| {name} | {r['category']} | {signed(r['net_month'])} | "
                             f"{signed(r.get('net_since_saturn1', np.nan))} | {signed(r.get('net_since_2024', np.nan))} |")

    if movers is not None:
        lines += ["", f"**Every address that moved {num(MOVER)} rETH or more**" if not big.empty
                  else f"**No address moved {num(MOVER)} rETH or more; the largest moves were:**", "",
                  "| Who | Holder type | Net change | Balance at start | Balance at end |",
                  "| --- | --- | ---: | ---: | ---: |"]
        for a, r in shown.sort_values("net").iterrows():
            b0 = num(r["balance_start"]) if "balance_start" in r and pd.notna(r["balance_start"]) else "n/a"
            b1 = num(r["balance_end"]) if "balance_end" in r and pd.notna(r["balance_end"]) else "n/a"
            lines.append(f"| {mover_name(a, r)} | {TYPE_LABEL.get(r['holder_type'], r['holder_type'])} | "
                         f"{signed(r['net'])} | {b0} | {b1} |")

    # ---- 3. lending ----
    if lrows:
        lines += ["", "## 3. Lending markets", "",
                  "| Market | rETH deposited, start → end | Share of all rETH | wstETH deposited, start → end | "
                  "Median loop spread, rETH | Median loop spread, wstETH |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for r in lrows:
            lines.append(f"| {r['venue']} | {num(r['reth_start'])} → {num(r['reth_end'])} | {r['share_end']:.1f}% | "
                         f"{num(r['wsteth_start'])} → {num(r['wsteth_end'])} | {num(r['reth_spread'])} bps | "
                         f"{num(r['wsteth_spread'])} bps |")
        lines += ["", "Loop spread: staking yield (4-week average) minus the WETH variable borrow rate on the same "
                  "market. Both assets borrow the same WETH, so the gap between the two columns is the staking "
                  "yield gap."]
        stale = [r["venue"] for r in lrows if r["end_date"] < end]
        if stale:
            lines.append(f"Lending data for {', '.join(stale)} ends before {day(end)}; the latest week is shown.")

    # ---- 4. why ----
    notes = os.path.join(REPORTS_DIR, "notes", f"{month}.md")
    lines += ["", "## 4. What changed and why", ""]
    if os.path.exists(notes):
        lines.append(open(notes, encoding="utf-8").read().strip())
    else:
        lines.append("Not yet written. The reasons behind these moves still need checking:")
        lines.append("")
        for a, r in shown.sort_values("net").iterrows():
            lines.append(f"- {mover_name(a, r)}: {signed(r['net'])} rETH.")

    # ---- 5. context ----
    lines += ["", "## 5. Context", ""]
    if "net_since_2024" in cat:
        tot = cat.loc[TOTAL, "net_since_2024"]
        p = cat.loc[[c for c in PROTOCOL_TYPES if c in cat.index], "net_since_2024"].sum()
        lines.append(f"- Since January 2024, rETH supply changed by {signed(tot)}; protocol positions explain "
                     f"{p / tot:.0%} of it.")
    if "net_since_saturn1" in cat:
        lines.append(f"- Since Saturn 1 (18 February 2026): {signed(cat.loc[TOTAL, 'net_since_saturn1'])} rETH.")

    # ---- checks and files ----
    ok = None
    if weeks is not None:
        w = weeks.copy()
        w["week_ending"] = pd.to_datetime(w["week_ending"], utc=True)
        w = w[(w["week_ending"] > start) & (w["week_ending"] <= end)]
        ok = (len(w) == int(meta["weeks"]) and (w["source"] != "MISSING").all()
              and ((w["minted"] - w["burned"] - w["supply_change"]).abs() < 1e-6).all())
    lines += ["", "## Data and checks", ""]
    if ok:
        lines.append(f"- All {int(meta['weeks'])} weeks reconcile exactly with the on-chain rETH supply change "
                     "(mints minus burns).")
    elif ok is not None:
        lines.append("- **Warning:** not every week in this month reconciled with the supply change. Treat the "
                     "holder figures as provisional.")
    lines += ["- Balances at the start and end of the month are read from archive state for every listed address.",
              "- Holder types and protocol labels, with the evidence for each, are in "
              f"[analysis/labels.csv]({REPO}/blob/main/analysis/labels.csv).",
              f"- Files: [holders_month.csv]({REPO}/blob/main/reth_study/holders_month.csv), "
              f"[holders_month_movers.csv]({REPO}/blob/main/reth_study/holders_month_movers.csv), "
              f"[holders_by_category.csv]({REPO}/blob/main/reth_study/holders_by_category.csv), "
              f"[holders_by_entity.csv]({REPO}/blob/main/reth_study/holders_by_entity.csv), "
              f"[lending_panel.csv]({REPO}/blob/main/reth_study/lending_panel.csv), "
              f"[weekly_panel.csv]({REPO}/blob/main/reth_study/weekly_panel.csv).",
              "", "Not financial advice."]
    path = os.path.join(REPORTS_DIR, f"{month}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote {path}")
    return path


if __name__ == "__main__":
    main()
