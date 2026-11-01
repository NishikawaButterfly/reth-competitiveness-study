"""Monthly rETH Demand Monitor report.

Reads the outputs of the holder analysis (reth_study/holders_*.csv), the lending analysis
(reth_study/lending_panel.csv) and the weekly panel (reth_study/weekly_panel.csv), and writes
reports/<YYYY-MM>.md with a chart of the month's net change by holder type.

Section 2, on why the large moves happened, is written by hand: it is read from
reports/notes/<YYYY-MM>.md when that file exists, and listed as open questions otherwise.

Run after holders.py and lending.py:
    python analysis/report.py
"""
import os
import subprocess

import numpy as np
import pandas as pd

OUT_DIR = os.environ.get("OUT_DIR", "reth_study")
REPORTS_DIR = os.environ.get("REPORTS_DIR", "reports")
REPO = "https://github.com/NishikawaButterfly/reth-competitiveness-study"
SATURN1 = "2026-02-18"
MOVER = 1_000.0
DUST = 0.01  # rETH: transfers below this are ignored as counterparties
VENUES = ["Aave v3", "Spark"]
PROTOCOL_TYPES = ["Restaking", "Lending and CDPs", "DEX liquidity", "L2 bridges", "Funds and DAO treasuries"]
TYPE_LABEL = {"Large wallets": "Large wallets (each moved 500+ rETH)",
              "Unidentified contracts": "Unidentified contracts (each moved 500+ rETH)",
              "Other addresses": "Other addresses (each moved under 500 rETH)"}
KIND_LABEL = {"wallet": "wallet", "safe": "Safe multisig", "contract": "contract"}
MINUS = "−"
TOTAL = "Total (= supply change)"


# ---- formatting ------------------------------------------------------------------------------

def signed(v, digits=0):
    if pd.isna(v):
        return "n/a"
    if abs(v) < 0.5 * 10 ** -digits:
        return "0"
    return f"{v:+,.{digits}f}".replace("-", MINUS)


def num(v, digits=0):
    return "n/a" if pd.isna(v) else f"{v:,.{digits}f}".replace("-", MINUS)


def day(d):
    return f"{d.day} {d:%B %Y}"


def read(name, **kw):
    path = os.path.join(OUT_DIR, name)
    return pd.read_csv(path, **kw) if os.path.exists(path) else None


def etherscan(addr):
    """Eight leading and six trailing characters: enough to tell an address from the look-alikes
    that address-poisoning spam creates (they copy the first and last four)."""
    return f"[{addr[:8]}…{addr[-6:]}](https://etherscan.io/address/{addr})"


def mover_name(addr, r):
    """Protocols by their label; wallets by address only, never by ENS or other personal names."""
    if isinstance(r.get("entity"), str) and r["entity"]:
        label = r["label"] if isinstance(r.get("label"), str) and r["label"] else r["entity"]
        return f"{label} ({etherscan(addr)})"
    kind = r.get("kind") if isinstance(r.get("kind"), str) else ""
    if kind == "contract" and isinstance(r.get("who"), str) and r["who"] and not r["who"].startswith("0x"):
        return f"Contract {r['who'].split('->')[-1].strip()} ({etherscan(addr)})"
    return f"Unlabelled {KIND_LABEL.get(kind, 'address')} {etherscan(addr)}"


def commit_sha():
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            sha = ""
    return sha or "main"


# ---- counterparties --------------------------------------------------------------------------

def top_counterparty(addr, cp):
    """The largest counterparty of an address, ignoring dust (zero-value transfers from address-poisoning
    look-alike addresses are common and must never be shown as a counterparty)."""
    if cp is None:
        return None
    rows = cp[(cp["address"] == addr) & (cp["reth"] >= DUST)]
    return None if rows.empty else rows.sort_values("reth", ascending=False).iloc[0]


def main_flow(addr, cp):
    """The largest counterparty of a mover in the month, e.g. '100% sent to Balancer V2 Vault'."""
    r = top_counterparty(addr, cp)
    if r is None:
        return ""
    kind = r["counterparty_kind"]
    if kind == "wallet":
        name = f"a wallet {etherscan(r['counterparty'])}"
    elif kind == "contract":
        name = f"contract {str(r['counterparty_name']).split('->')[-1].strip()} ({etherscan(r['counterparty'])})"
    else:
        name = r["counterparty_name"]
    return f"{r['share']:.0%} {r['direction']} {name}"


def wallet_pairs(shown, cp):
    """Wallet-to-wallet transfers among the listed movers: A sent (almost) all it moved to B, and B's
    gain matches. They move rETH between addresses without changing supply."""
    pairs = {}
    for a, r in shown[shown["net"] < 0].iterrows():
        c = top_counterparty(a, cp)
        if c is None or c["counterparty_kind"] != "wallet" or c["share"] < 0.99 or c["counterparty"] not in shown.index:
            continue
        if abs(shown.loc[c["counterparty"], "net"] + r["net"]) <= 0.01 * abs(r["net"]):
            pairs[a] = pairs[c["counterparty"]] = -r["net"]
    return pairs


# ---- yield, flows and lending ------------------------------------------------------------------

def apr_between(panel, a, b):
    """Annualised growth of each token's exchange rate between two snapshots."""
    d = (b["date"] - a["date"]).days
    return ((b["reth_rate"] / a["reth_rate"]) ** (365 / d) - 1, (b["steth_rate"] / a["steth_rate"]) ** (365 / d) - 1)


def month_gaps(panel, month, n=3):
    """The rETH − stETH gap for each of the n months before `month`, from exchange rates (bps)."""
    p = panel.reset_index(drop=True)
    pm = p["date"].dt.tz_convert(None).dt.to_period("M")
    out = []
    for m in [pd.Period(month, "M") - i for i in range(n, 0, -1)]:
        idx = p.index[pm == m]
        if not len(idx):
            continue
        a0 = idx[0] - 1 if idx[0] > 0 else idx[0]          # the first month of data starts at its first snapshot
        if idx[-1] > a0:
            r, s = apr_between(p, p.loc[a0], p.loc[idx[-1]])
            out.append((m.strftime("%b"), (r - s) * 1e4))
    return out


def yield_stats(panel, start, end):
    """APR over the month from each token's exchange rate at both ends, the weekly gaps, and weeks whose
    weekly APR is far from the month's median (oracle timing or a one-off event: worth a look)."""
    s, e = panel[panel["date"] == start], panel[panel["date"] == end]
    inside = panel[(panel["date"] > start) & (panel["date"] <= end)]
    if s.empty or e.empty or inside.empty:
        return None
    reth, steth = apr_between(panel, s.iloc[0], e.iloc[0])
    odd, odd_dates = [], set()
    for col, name in (("reth_apr", "rETH"), ("steth_apr", "stETH")):
        med = inside[col].median()
        for _, w in inside[(inside[col] - med).abs() > 0.01].iterrows():
            odd.append(f"{name}'s weekly APR in the week to {day(w['date'])} was {w[col] * 100:.2f}%, against a "
                       f"monthly median of {med * 100:.2f}%")
            odd_dates.add(w["date"])
    gap_ex = np.nan
    if odd_dates:  # the gap over the other weeks, chaining each token's weekly rate growth
        win = panel[(panel["date"] >= start) & (panel["date"] <= end)].reset_index(drop=True)
        g_r = g_s = 1.0
        days = 0
        for i in range(1, len(win)):
            if win.loc[i, "date"] in odd_dates:
                continue
            g_r *= win.loc[i, "reth_rate"] / win.loc[i - 1, "reth_rate"]
            g_s *= win.loc[i, "steth_rate"] / win.loc[i - 1, "steth_rate"]
            days += (win.loc[i, "date"] - win.loc[i - 1, "date"]).days
        if days:
            gap_ex = (g_r ** (365 / days) - g_s ** (365 / days)) * 1e4
    prem = inside["premium_pct"].dropna() if "premium_pct" in inside else pd.Series(dtype=float)
    return {"reth": reth, "steth": steth, "gap": (reth - steth) * 1e4, "gap_ex": gap_ex,
            "weekly_gaps": ", ".join(signed(g) for g in inside["gap_bps"]),
            "weekly_reth": ", ".join(f"{v * 100:.2f}%" for v in inside["reth_apr"]),
            "odd": odd, "weekly_prem": ", ".join(f"{signed(v, 2)}%" for v in prem)}


def flow_context(weeks, month):
    """Minted and burned in the month, per week, against the six months before (per week, so months
    of four and five weeks compare fairly), and the week with the largest burns."""
    if weeks is None:
        return None
    w = weeks.copy()
    w["week_ending"] = pd.to_datetime(w["week_ending"])
    w["m"] = w["week_ending"].dt.to_period("M").astype(str)
    g = w.groupby("m").agg(minted=("minted", "sum"), burned=("burned", "sum"), weeks=("minted", "size"))
    if month not in g.index:
        return None
    prev = g[g.index < month].tail(6)
    cur = w[w["m"] == month]
    peak = cur.loc[cur["burned"].idxmax()]
    return {"minted": g.loc[month, "minted"], "burned": g.loc[month, "burned"], "weeks": int(g.loc[month, "weeks"]),
            "minted_pw": g.loc[month, "minted"] / g.loc[month, "weeks"],
            "burned_pw": g.loc[month, "burned"] / g.loc[month, "weeks"],
            "minted_pw_prev": prev["minted"].sum() / prev["weeks"].sum() if len(prev) else np.nan,
            "burned_pw_prev": prev["burned"].sum() / prev["weeks"].sum() if len(prev) else np.nan,
            "n_prev": len(prev), "peak_date": peak["week_ending"], "peak_burned": peak["burned"],
            "peak_change": peak["supply_change"]}


def lending_rows(lend, start, end, supply_end):
    """Per venue: deposits at both ends of the month, share of all rETH, the month's median loop spreads,
    the median of the weekly differences between them, and the worst week."""
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
        worst = inside.loc[inside["reth_spread_bps"].idxmin()]
        rows.append({"venue": v, "end_date": e["date"], "reth_start": s["reth_supplied"], "reth_end": e["reth_supplied"],
                     "share_end": e["reth_supplied"] / supply_end * 100,
                     "wsteth_start": s["wsteth_supplied"], "wsteth_end": e["wsteth_supplied"],
                     "reth_spread": inside["reth_spread_bps"].median(),
                     "wsteth_spread": inside["wsteth_spread_bps"].median(),
                     "gap": (inside["reth_spread_bps"] - inside["wsteth_spread_bps"]).median(),
                     "worst_date": worst["date"], "worst_reth": worst["reth_spread_bps"],
                     "worst_wsteth": worst["wsteth_spread_bps"],
                     "worst_borrow": worst.get("weth_borrow_apr", np.nan)})
    return rows


# ---- chart -------------------------------------------------------------------------------------

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


# ---- report ------------------------------------------------------------------------------------

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
    cp = read("holders_month_counterparties.csv")
    mb = read("holders_month_mint_burn.csv")
    others = read("holders_month_others.csv", index_col=0)
    weeks = read("holders_weeks.csv")
    panel = read("weekly_panel.csv")
    lend = read("lending_panel.csv")
    if cat is None or "net_month" not in cat:
        raise RuntimeError("holders_by_category.csv has no month column: run analysis/holders.py first.")
    panel["date"] = pd.to_datetime(panel["date"], utc=True)
    panel = panel.sort_values("date").reset_index(drop=True)
    s0, s1, change = float(meta["supply_start"]), float(meta["supply_end"]), float(meta["supply_change"])
    sha = commit_sha()

    steps = cat.drop(index=TOTAL)["net_month"]
    steps = steps[steps.abs() > 1e-9]
    os.makedirs(REPORTS_DIR, exist_ok=True)
    chart = f"{month}-holders.png"
    waterfall(steps, os.path.join(REPORTS_DIR, chart),
              f"rETH supply {signed(change)} in {title_month}: who moved it",
              f"Net change by holder type, weekly snapshots {start.day} {start:%b} to {end.day} {end:%b %Y}. "
              "Red: decrease. Green: increase. Grey: subtotal and total.")

    big = movers[movers["net"].abs() >= MOVER] if movers is not None else pd.DataFrame()
    shown = big if not big.empty else (pd.concat([movers.nsmallest(3, "net"), movers.nlargest(3, "net")])
                                       if movers is not None else pd.DataFrame())
    pairs = wallet_pairs(shown, cp) if not shown.empty else {}
    lrows = lending_rows(lend, start, end, s1)
    ys = yield_stats(panel, start, end)
    fc = flow_context(weeks, month)
    gaps_before = month_gaps(panel, month)
    p_s, p_e, p_0 = panel[panel["date"] == start], panel[panel["date"] == end], panel.iloc[0]
    backing = None
    if not p_s.empty and not p_e.empty and {"reth_backing_eth", "steth_pooled_eth"} <= set(panel.columns):
        p_s, p_e = p_s.iloc[0], p_e.iloc[0]
        backing = {"reth_m": (p_e["reth_backing_eth"] / p_s["reth_backing_eth"] - 1) * 100,
                   "steth_m": (p_e["steth_pooled_eth"] / p_s["steth_pooled_eth"] - 1) * 100,
                   "reth_all": (p_e["reth_backing_eth"] / p_0["reth_backing_eth"] - 1) * 100,
                   "steth_all": (p_e["steth_pooled_eth"] / p_0["steth_pooled_eth"] - 1) * 100}
    burns = mb[mb["side"] == "burned"].sort_values("reth", ascending=False) if mb is not None else pd.DataFrame()
    mints = mb[mb["side"] == "minted"].sort_values("reth", ascending=False) if mb is not None else pd.DataFrame()
    ent_m = ent[ent["net_month"].abs() >= 10].sort_values("net_month") if ent is not None and "net_month" in ent \
        else pd.DataFrame()

    # ---- title and summary ----
    lines = [f"# rETH Demand Monitor: {title_month}", "",
             f"Who holds rETH, who left and why, from weekly on-chain snapshots between {day(start)} and "
             f"{day(end)} ({int(meta['weeks'])} weeks). The tables are generated from the "
             f"[open repository]({REPO}); section 2 is written by hand. Sources and checks are listed at the end.",
             "", "## Summary", ""]
    sup = (f"- **Supply:** rETH supply went from {num(s0)} to {num(s1)} ({signed(change)} rETH, "
           f"{signed(change / s0 * 100, 1)}%).")
    if backing:
        sup += (f" The ETH backing rETH changed by {signed(backing['reth_m'], 1)}% and stETH's by "
                f"{signed(backing['steth_m'], 1)}%.")
    lines.append(sup)
    if fc:
        txt = (f"- **Redemptions:** {num(fc['burned'])} rETH burned for ETH and {num(fc['minted'])} minted in "
               f"{fc['weeks']} weeks, or {num(fc['burned_pw'])} and {num(fc['minted_pw'])} a week")
        if fc["n_prev"]:
            txt += (f", against {num(fc['burned_pw_prev'])} and {num(fc['minted_pw_prev'])} a week over the "
                    f"{prev_months(fc['n_prev'])}")
        txt += (f". The week to {day(fc['peak_date'])} burned {num(fc['peak_burned'])} rETH, "
                f"{fc['peak_burned'] / fc['burned']:.0%} of the month's burns.")
        if not burns.empty:
            b = burns.iloc[0]
            txt += f" The largest burner was {in_sentence(burner_name(b))} ({num(b['reth'])} rETH)."
        lines.append(txt)
    dec = [f"{n} {signed(r['net_month'])}" for n, r in ent_m[ent_m["net_month"] < 0].head(3).iterrows()]
    if dec or "Other addresses" in steps.index:
        txt = "- **Largest net decreases:** " + (", ".join(dec) + " (identified protocols and treasuries)" if dec else "")
        if "Other addresses" in steps.index:
            txt += ("; " if dec else "") + f"other addresses, each moving under 500 rETH, {signed(steps['Other addresses'])} in total"
        lines.append(txt + ".")
    if pairs:
        n = len(pairs) // 2
        lines.append(f"- **Wallet-to-wallet transfers:** {n} transfer{'s' if n != 1 else ''} moved "
                     f"{num(sum(pairs.values()) / 2)} rETH to new addresses, possibly the same owners'. "
                     "They do not change the supply.")
    if lrows:
        lines.append(f"- **Lending:** rETH deposited on {' and '.join(r['venue'] for r in lrows)} went from "
                     f"{num(sum(r['reth_start'] for r in lrows))} to {num(sum(r['reth_end'] for r in lrows))}. "
                     "Looping rETH paid a median " + " and ".join(f"{signed(r['reth_spread'])} bps on {r['venue']}"
                                                                  for r in lrows)
                     + f", {signed(lrows[0]['gap'])} bps against wstETH on the same markets (4-week average "
                     "staking yields).")
    if ys:
        txt = (f"- **Yield:** rETH's APR over the month was {ys['reth'] * 100:.2f}% and stETH's "
               f"{ys['steth'] * 100:.2f}%, a gap of {signed(ys['gap'])} bps")
        if gaps_before:
            txt += " (" + ", ".join(f"{m} {signed(g)}" for m, g in gaps_before) + " bps in the months before)"
        if ys["odd"] and pd.notna(ys["gap_ex"]):
            txt += (f". Without the week flagged in section 4 the gap was {signed(ys['gap_ex'])} bps; "
                    "section 2 explains that week")
        lines.append(txt + ".")

    # ---- 1. who moved ----
    lines += ["", "## 1. Who moved rETH", "", f"![rETH net change by holder type, {title_month}]({chart})", "",
              "| Holder type | Net change (rETH) | Addresses |", "| --- | ---: | ---: |"]
    shown_types = []
    for c in cat.index:
        if c == TOTAL:
            continue
        v = cat.loc[c, "net_month"]
        n = cat.loc[c, "addresses_month"] if "addresses_month" in cat else np.nan
        if abs(v) > 1e-9:
            lines.append(f"| {TYPE_LABEL.get(c, c)} | {signed(v)} | {num(n)} |")
            shown_types.append(v)
    lines.append(f"| **Total (= supply change)** | **{signed(cat.loc[TOTAL, 'net_month'])}** | |")
    note = rounding_note(shown_types, cat.loc[TOTAL, "net_month"])
    if note:
        lines += ["", note]
    if others is not None and not others.empty and "Other addresses" in steps.index:
        k = others.groupby("kind")["net"].agg(["sum", "size"])
        parts = [f"{int(k.loc[x, 'size'])} {KIND_LABEL.get(x, x)}{'s' if k.loc[x, 'size'] != 1 else ''} "
                 f"({signed(k.loc[x, 'sum'])})" for x in ("wallet", "safe", "contract") if x in k.index]
        rest = steps["Other addresses"] - others["net"].sum()
        txt = (f"Within other addresses, those that moved 50 rETH or more were {', '.join(parts)}; "
               f"everyone else came to {signed(rest)}.")
        con = others[others["kind"] == "contract"].reindex(others[others["kind"] == "contract"]["net"].abs()
                                                           .sort_values(ascending=False).index).head(3)
        if not con.empty:
            txt += " Largest contracts: " + ", ".join(
                f"{(str(r['who']).split('->')[-1].strip() if isinstance(r['who'], str) and r['who'] else 'unnamed')} "
                f"{etherscan(a)} {signed(r['net'])}" for a, r in con.iterrows()) + "."
        lines += ["", txt]

    if not ent_m.empty:
        has_bal = {"balance_month_start", "balance_month_end"} <= set(ent_m.columns)
        lines += ["", "**Identified protocols and treasuries**", "",
                  "| Entity | Type | Net change | Holdings, start → end | Change in holdings | Since Saturn 1 | "
                  "Since January 2024 |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
        for name, r in ent_m.iterrows():
            hold, pct = "n/a", "n/a"
            if has_bal and pd.notna(r["balance_month_start"]) and pd.notna(r["balance_month_end"]):
                hold = f"{num(r['balance_month_start'])} → {num(r['balance_month_end'])}"
                if r["balance_month_start"] > 0:
                    pct = pct_text((r["balance_month_end"] / r["balance_month_start"] - 1) * 100)
            lines.append(f"| {name} | {r['category']} | {signed(r['net_month'])} | {hold} | {pct} | "
                         f"{signed(r.get('net_since_saturn1', np.nan))} | {signed(r.get('net_since_2024', np.nan))} |")

    if movers is not None:
        lines += ["", f"**Every address that moved {num(MOVER)} rETH or more**" if not big.empty
                  else f"**No address moved {num(MOVER)} rETH or more; the largest moves were:**", "",
                  "| Who | Holder type | Net change | Balance at start | Balance at end | Main counterparty |",
                  "| --- | --- | ---: | ---: | ---: | --- |"]
        for a, r in shown.sort_values("net").iterrows():
            b0 = num(r["balance_start"]) if "balance_start" in r and pd.notna(r["balance_start"]) else "n/a"
            b1 = num(r["balance_end"]) if "balance_end" in r and pd.notna(r["balance_end"]) else "n/a"
            flow = main_flow(a, cp) + (" (wallet-to-wallet transfer)" if a in pairs else "")
            lines.append(f"| {mover_name(a, r)} | {TYPE_LABEL.get(r['holder_type'], r['holder_type'])} | "
                         f"{signed(r['net'])} | {b0} | {b1} | {flow} |")

    if not burns.empty:
        has_src = "same_tx_share" in burns.columns and burns["same_tx_share"].notna().any()
        head = "| Who | Burned | Share of the month's burns | Week with most burning |"
        rule = "| --- | ---: | ---: | --- |"
        if has_src:
            head += " Main source of its rETH | Burned on arrival |"
            rule += " --- | ---: |"
        lines += ["", "**Largest redemptions (rETH burned for ETH)**", "", head, rule]
        for _, b in burns.head(5).iterrows():
            row = (f"| {burner_name(b)} | {num(b['reth'])} | {b['share']:.0%} | "
                   f"{day(pd.Timestamp(b['peak_week_ending']))} ({num(b['peak_week_reth'])}) |")
            if has_src:
                arrival = f"{b['same_tx_share']:.0%}" if pd.notna(b["same_tx_share"]) else "n/a"
                row += f" {source_text(b)} | {arrival} |"
            lines.append(row)
        if has_src:
            lines += ["", "Main source: the address that sent it the most rETH during the month. Burned on arrival: "
                      "the share of what it burned that it received in the same transaction, as when rETH is bought "
                      "on a market or routed from a seller and redeemed at once."]
        if not mints.empty:
            lines += ["", "Largest mints: " + ", ".join(f"{burner_name(b)} {num(b['reth'])}"
                                                        for _, b in mints.head(3).iterrows()) + "."]

    # ---- 2. why ----
    notes = os.path.join(REPORTS_DIR, "notes", f"{month}.md")
    lines += ["", "## 2. Why it happened", ""]
    if os.path.exists(notes):
        lines.append(open(notes, encoding="utf-8").read().strip())
    else:
        lines += ["Not yet written. The reasons behind these moves still need checking:", ""]
        for a, r in shown.sort_values("net").iterrows():
            flow = main_flow(a, cp)
            lines.append(f"- {mover_name(a, r)}: {signed(r['net'])} rETH" + (f"; {flow}." if flow else "."))

    # ---- 3. lending ----
    if lrows:
        lines += ["", "## 3. Lending markets", "",
                  "| Market | rETH deposited, start → end | Share of all rETH | wstETH deposited, start → end | "
                  "Median loop spread, rETH | Median loop spread, wstETH | Median weekly gap |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for r in lrows:
            lines.append(f"| {r['venue']} | {num(r['reth_start'])} → {num(r['reth_end'])} | {r['share_end']:.1f}% | "
                         f"{num(r['wsteth_start'])} → {num(r['wsteth_end'])} | {signed(r['reth_spread'])} bps | "
                         f"{signed(r['wsteth_spread'])} bps | {signed(r['gap'])} bps |")
        lines += ["", "Loop spread: staking yield (4-week average) minus the WETH variable borrow rate on the same "
                  "market. The last column is the median of each week's rETH spread minus wstETH spread; both "
                  "borrow the same WETH, so that difference is the staking yield gap. Deposits are the market's "
                  "aToken supply, which includes any rETH lent out, so they can sit slightly above the rETH "
                  "balance in section 1."]
        for r in lrows:
            if r["worst_reth"] < -25:
                lines += ["", f"At the {day(r['worst_date'])} snapshot, {r['venue']}'s WETH borrow rate was "
                          f"{r['worst_borrow'] * 100:.2f}%; at that rate loops would pay {signed(r['worst_reth'])} bps "
                          f"on rETH and {signed(r['worst_wsteth'])} bps on wstETH. The rate is read at one block, "
                          "not averaged over the week."]
        stale = [r["venue"] for r in lrows if r["end_date"] < end]
        if stale:
            lines += ["", f"Lending data for {', '.join(stale)} ends before {day(end)}; the latest week is shown."]

    # ---- 4. supply and yield ----
    lines += ["", "## 4. Supply and yield", "", "| Measure | Value |", "| --- | --- |",
              f"| rETH supply, start → end | {num(s0)} → {num(s1)} ({signed(change)}) |"]
    if fc:
        lines.append(f"| Burned / minted in the month | {num(fc['burned'])} / {num(fc['minted'])} "
                     f"({num(fc['burned_pw'])} / {num(fc['minted_pw'])} a week) |")
        if fc["n_prev"]:
            lines.append(f"| Burned / minted a week, {prev_months(fc['n_prev'])} | {num(fc['burned_pw_prev'])} / "
                         f"{num(fc['minted_pw_prev'])} |")
    if ys:
        lines += [f"| rETH APR over the month, from its exchange rate | {ys['reth'] * 100:.2f}% |",
                  f"| stETH APR over the month, from its exchange rate | {ys['steth'] * 100:.2f}% |",
                  f"| Gap, rETH − stETH | {signed(ys['gap'])} bps |"
                  + ("" if pd.isna(ys["gap_ex"]) else f"\n| Gap without the flagged week | {signed(ys['gap_ex'])} bps |"),
                  f"| rETH weekly APR, in order | {ys['weekly_reth']} |",
                  f"| Weekly gaps, in order (bps) | {ys['weekly_gaps']} |"]
        if gaps_before:
            lines.append("| Gap in the months before (bps) | "
                         + ", ".join(f"{m} {signed(g)}" for m, g in gaps_before) + " |")
        if ys["weekly_prem"]:
            lines.append(f"| rETH market price vs protocol rate, weekly snapshots (DefiLlama prices) | "
                         f"{ys['weekly_prem']} |")
        if ys["odd"]:
            lines += ["", "Check before reading the gap: " + "; ".join(ys["odd"]) + ". Weekly rates move with oracle "
                      "timing and one-off events, and a week like this also moves the monthly figure."]

    # ---- 5. context ----
    lines += ["", "## 5. Context", ""]
    if "net_since_2024" in cat:
        tot = cat.loc[TOTAL, "net_since_2024"]
        p = cat.loc[[c for c in PROTOCOL_TYPES if c in cat.index], "net_since_2024"].sum()
        lines.append(f"- From {day(panel['date'].iloc[0])} to {day(panel['date'].iloc[-1])}, rETH supply changed by "
                     f"{signed(tot)}; protocol positions account for {p / tot:.0%} of it.")
    if backing:
        lines.append(f"- Over the same period the ETH backing rETH changed by {signed(backing['reth_all'], 1)}% and "
                     f"stETH's by {signed(backing['steth_all'], 1)}%.")
    if "net_since_saturn1" in cat:
        s1d = panel.loc[panel["date"] >= pd.Timestamp(SATURN1, tz="UTC"), "date"]
        if not s1d.empty:
            lines.append(f"- From the first weekly snapshot after Saturn 1 ({day(s1d.iloc[0])}) to "
                         f"{day(panel['date'].iloc[-1])}: {signed(cat.loc[TOTAL, 'net_since_saturn1'])} rETH.")

    # ---- data, sources and checks ----
    ok = None
    if weeks is not None:
        w = weeks.copy()
        w["week_ending"] = pd.to_datetime(w["week_ending"], utc=True)
        w = w[(w["week_ending"] > start) & (w["week_ending"] <= end)]
        ok = (len(w) == int(meta["weeks"]) and (w["source"] != "MISSING").all()
              and ((w["minted"] - w["burned"] - w["supply_change"]).abs() < 1e-6).all())
    lines += ["", "## Data, sources and checks", "",
              "- **Sources:** rETH Transfer events, balances and exchange rates read from public Ethereum RPC "
              "endpoints (archive state); stETH's rate from the stETH contract; market prices from DefiLlama; "
              "contract names from Blockscout and Sourcify; protocol labels with the evidence for each in "
              f"[analysis/labels.csv]({REPO}/blob/{sha}/analysis/labels.csv)."]
    if ok:
        lines.append(f"- **Check:** for each of the {int(meta['weeks'])} weeks, mints minus burns in the Transfer events "
                     "equal the change in rETH's totalSupply read from the contract at the two snapshot blocks. "
                     "All weeks passed. Balances of listed addresses are read from archive state at both ends.")
    elif ok is not None:
        lines.append("- **Warning:** not every week in this month reconciled with the supply change. Treat the "
                     "holder figures as provisional.")
    files = ["holders_month.csv", "holders_by_category.csv", "holders_by_entity.csv", "holders_month_movers.csv",
             "holders_month_counterparties.csv", "holders_month_mint_burn.csv", "holders_month_others.csv",
             "holders_weeks.csv", "lending_panel.csv", "weekly_panel.csv"]
    lines += [f"- **Files** at commit {sha[:7]}: "
              + ", ".join(f"[{f}]({REPO}/blob/{sha}/reth_study/{f})" for f in files) + ".",
              "", "Not financial advice."]
    path = os.path.join(REPORTS_DIR, f"{month}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote {path}")
    return path


def in_sentence(name):
    """'Unlabelled contract X' -> 'an unlabelled contract X'; 'Contract X' -> 'contract X'."""
    if name.startswith("Unlabelled "):
        return "an unlabelled " + name[len("Unlabelled "):]
    if name.startswith("Contract "):
        return "contract " + name[len("Contract "):]
    return name


def pct_text(v):
    return f"{signed(v, 1)}%" if abs(v) < 10 else f"{signed(v)}%"


def prev_months(n):
    return "previous month" if n == 1 else f"previous {n} months"


def rounding_note(values, total):
    """A note when the rows, as rounded in the table, do not add up to the rounded total."""
    if sum(round(float(v)) for v in values) != round(float(total)):
        return "Rows are rounded, so they may not add up exactly to the total."
    return ""


def source_text(b):
    """The main sender of a redeemer's rETH: protocols by label, contracts by name, wallets by address only."""
    c, name, kind = b.get("source"), b.get("source_name"), b.get("source_kind")
    if not isinstance(c, str) or not c:
        return "none this month"
    share = f"{b['source_share']:.0%} from " if pd.notna(b.get("source_share")) else "from "
    if kind == "labelled" and isinstance(name, str) and name:
        return share + name
    if kind == "contract" and isinstance(name, str) and name and not name.startswith("0x"):
        return share + f"contract {name.split('->')[-1].strip()} ({etherscan(c)})"
    return share + f"{'an unlabelled contract' if kind == 'contract' else 'a wallet'} {etherscan(c)}"


def burner_name(b):
    """Burners and minters: protocols by label, contracts by name, wallets by address only."""
    a = b["address"]
    if isinstance(b.get("label"), str) and b["label"]:
        return f"{b['label']} ({etherscan(a)})"
    if b.get("kind") == "contract" and isinstance(b.get("who"), str) and b["who"] and not b["who"].startswith("0x"):
        return f"Contract {b['who'].split('->')[-1].strip()} ({etherscan(a)})"
    return f"Unlabelled {KIND_LABEL.get(b.get('kind'), 'address')} {etherscan(a)}"


if __name__ == "__main__":
    main()
