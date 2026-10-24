"""Who is leaving rETH?

Reads every rETH Transfer event between the study's first and last weekly blocks,
nets them by address, and reports how concentrated the outflow is: the top leavers,
the share the largest ones account for, what drove the biggest outflow weeks, and
whether each top address is a contract or a plain wallet.

Run after the notebook (it reads reth_study/blocks.csv and weekly_panel.csv):
    python analysis/holders.py
Transfers are cached in reth_study/transfers.csv, so a rerun only fetches new blocks.
"""
import json
import os
import time

import numpy as np
import pandas as pd

OUT_DIR = os.environ.get("OUT_DIR", "reth_study")
RETH = "0xae78736Cd615f374D3085123A210448E74Fc6393"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
ALCHEMY_KEY = os.environ.get("ALCHEMY_KEY", "")
PUBLIC_RPCS = [
    "https://eth.drpc.org",
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    "https://1rpc.io/eth",
    "https://rpc.payload.de",
    "https://eth.merkle.io",
]
LABELS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "labels.csv")
TOP_N = 30


DEBUG_LINES = []


def dbg(msg):
    print(msg)
    DEBUG_LINES.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")


def write_debug():
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "holders_debug.log"), "w") as f:
        f.write("\n".join(DEBUG_LINES[-400:]) + "\n")


def connect_all(test_block):
    """Every endpoint that serves Transfer logs, best first."""
    from web3 import Web3
    candidates = []
    if ALCHEMY_KEY:
        candidates.append(("Alchemy", f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"))
    candidates += [(u, u) for u in PUBLIC_RPCS]
    ok = []
    for label, url in candidates:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))
            logs = w3.eth.get_logs({"fromBlock": test_block, "toBlock": test_block + 2_000,
                                    "address": Web3.to_checksum_address(RETH), "topics": [TRANSFER_TOPIC]})
            dbg(f"Log endpoint OK: {label} ({len(logs)} logs in a 2,000-block test)")
            ok.append((label, w3))
        except Exception as e:
            dbg(f"Skipping {label}: {type(e).__name__}: {str(e)[:200]}")
    if not ok:
        raise RuntimeError("No endpoint served logs. Add the ALCHEMY_KEY secret and rerun.")
    return ok


def parse_log(lg):
    topics = lg["topics"]
    return {
        "block": int(lg["blockNumber"]),
        "tx": "0x" + bytes(lg["transactionHash"]).hex(),
        "log_index": int(lg["logIndex"]),
        "from": "0x" + bytes(topics[1])[-20:].hex(),
        "to": "0x" + bytes(topics[2])[-20:].hex(),
        "amount": int.from_bytes(bytes(lg["data"]), "big") / 1e18,
    }


def fetch_transfers(endpoints, start, end, cache_path, chunk=5_000, min_chunk=250, max_chunk=50_000):
    """Fetch Transfer logs in [start, end], resuming from the cache. Halves the block
    range when an endpoint refuses a request, grows it again after successes, and
    moves to the next endpoint when one keeps failing at the smallest range."""
    from web3 import Web3
    cols = ["block", "tx", "log_index", "from", "to", "amount"]
    state_path = cache_path + ".state.json"
    cached = pd.read_csv(cache_path) if os.path.exists(cache_path) else None
    first_block = start
    if cached is not None and os.path.exists(state_path):
        state = json.load(open(state_path))
        if state.get("start") == start:
            first_block = state["last_block"] + 1
        else:
            cached = None  # different window: start over
    address = Web3.to_checksum_address(RETH)
    rows, b, fails, ep, errors_shown = [], first_block, 0, 0, 0
    label, w3 = endpoints[ep]

    def flush(last_block):
        nonlocal cached, rows
        new = pd.DataFrame(rows, columns=cols)
        cached = new if cached is None else pd.concat([cached, new], ignore_index=True)
        cached.to_csv(cache_path, index=False)
        json.dump({"start": start, "last_block": last_block}, open(state_path, "w"))
        rows = []

    while b <= end:
        hi = min(b + chunk - 1, end)
        try:
            logs = w3.eth.get_logs({"fromBlock": b, "toBlock": hi, "address": address,
                                    "topics": [TRANSFER_TOPIC]})
        except Exception as e:
            if errors_shown < 25:
                dbg(f"{label}: blocks {b}-{hi} ({hi - b + 1}): {type(e).__name__}: {str(e)[:200]}")
                errors_shown += 1
            if chunk > min_chunk:
                chunk = max(min_chunk, chunk // 2)
                continue
            fails += 1
            if fails <= 3:
                time.sleep(3 * fails)
                continue
            ep += 1
            if ep >= len(endpoints):
                if rows:
                    flush(b - 1)
                raise RuntimeError(f"All endpoints failed at block {b}")
            label, w3 = endpoints[ep]
            dbg(f"Switching to {label} at block {b}")
            fails, chunk = 0, 2_000
            continue
        fails = 0
        rows.extend(parse_log(lg) for lg in logs)
        b = hi + 1
        chunk = min(max_chunk, int(chunk * 1.5))
        if len(rows) >= 5_000 or b > end:
            flush(hi)
            dbg(f"  transfers up to block {hi}: {len(cached):,} (range now {chunk:,} blocks, {label})")
        time.sleep(0.1)
    if cached is None:
        cached = pd.DataFrame(columns=cols)
    cached = cached.drop_duplicates(["tx", "log_index"])
    return cached[(cached["block"] >= start) & (cached["block"] <= end)]


def net_by_address(tr):
    sent = tr.groupby("from")["amount"].sum()
    received = tr.groupby("to")["amount"].sum()
    n_out = tr.groupby("from").size()
    n_in = tr.groupby("to").size()
    df = pd.DataFrame({"sent": sent, "received": received, "n_out": n_out, "n_in": n_in}).fillna(0)
    df["net"] = df["received"] - df["sent"]
    df.index.name = "address"
    return df


def concentration(net):
    leavers = net[(net.index != ZERO) & (net["net"] < 0)].sort_values("net")
    total = -leavers["net"].sum()
    out = {"leaving_addresses": int(len(leavers)), "total_decrease_reth": total}
    for k in (1, 5, 10, 20, 50):
        out[f"top{k}_share"] = float(-leavers["net"].head(k).sum() / total) if total else np.nan
    return out


def load_labels():
    if os.path.exists(LABELS_FILE):
        lab = pd.read_csv(LABELS_FILE)
        return {a.lower(): l for a, l in zip(lab["address"], lab["label"])}
    return {}


def short(a):
    return a[:6] + "..." + a[-4:]


def main():
    blocks = pd.read_csv(os.path.join(OUT_DIR, "blocks.csv"), parse_dates=["date"])
    panel = pd.read_csv(os.path.join(OUT_DIR, "weekly_panel.csv"), parse_dates=["date"])
    blocks = blocks.sort_values("block").reset_index(drop=True)
    start, end = int(blocks["block"].iloc[0]) + 1, int(blocks["block"].iloc[-1])

    endpoints = connect_all(start)
    w3 = endpoints[0][1]
    tr = fetch_transfers(endpoints, start, end, os.path.join(OUT_DIR, "transfers.csv"))
    dbg(f"Transfers in window: {len(tr):,} (blocks {start} to {end})")

    # Self-check: mints minus burns must equal the supply change in the weekly panel
    net = net_by_address(tr)
    supply_change = float(panel["reth_supply"].iloc[-1] - panel["reth_supply"].iloc[0])
    zero_net = float(net.loc[ZERO, "net"]) if ZERO in net.index else 0.0
    implied = -zero_net
    err = abs(implied - supply_change) / max(1.0, abs(supply_change))
    dbg(f"Supply change from panel {supply_change:,.1f}; from transfers {implied:,.1f} ({err:.4%})")
    if err > 0.001:
        raise RuntimeError("Transfer totals do not match the supply change: stop and review.")

    labels = load_labels()
    conc = concentration(net)
    pd.DataFrame([conc]).to_csv(os.path.join(OUT_DIR, "holders_concentration.csv"), index=False)

    top = net[(net.index != ZERO) & (net["net"] < 0)].sort_values("net").head(TOP_N).copy()
    top["share_of_total_decrease"] = -top["net"] / conc["total_decrease_reth"]
    top["is_contract"] = [len(w3.eth.get_code(w3.to_checksum_address(a))) > 0 for a in top.index]
    top["label"] = [labels.get(a.lower(), "") for a in top.index]
    top.to_csv(os.path.join(OUT_DIR, "holders_top_leavers.csv"))

    gainers = net[net.index != ZERO].sort_values("net", ascending=False).head(15).copy()
    gainers["is_contract"] = [len(w3.eth.get_code(w3.to_checksum_address(a))) > 0 for a in gainers.index]
    gainers["label"] = [labels.get(a.lower(), "") for a in gainers.index]
    gainers.to_csv(os.path.join(OUT_DIR, "holders_top_gainers.csv"))

    # Biggest outflow weeks: who drove them
    panel = panel.sort_values("date").reset_index(drop=True)
    panel["block_prev"] = panel["block"].shift(1)
    worst = panel.dropna(subset=["block_prev"]).nsmallest(5, "net_flow_eth")
    rows = []
    for _, w in worst.iterrows():
        wk = tr[(tr["block"] > w["block_prev"]) & (tr["block"] <= w["block"])]
        wn = net_by_address(wk)
        wn = wn[wn.index != ZERO].sort_values("net").head(3)
        week_out = -float(wn["net"][wn["net"] < 0].sum()) if len(wn) else 0.0
        for a, r in wn.iterrows():
            rows.append({"week_ending": w["date"].date(), "week_net_flow_eth": w["net_flow_eth"],
                         "address": a, "net_reth": r["net"], "label": labels.get(a.lower(), "")})
    pd.DataFrame(rows).to_csv(os.path.join(OUT_DIR, "holders_worst_weeks.csv"), index=False)

    # Quarterly timing of the top 10 leavers
    week_of = np.searchsorted(blocks["block"].to_numpy(), tr["block"].to_numpy(), side="left")
    week_of = np.clip(week_of, 0, len(blocks) - 1)
    quarters = pd.to_datetime(blocks["date"], utc=True).dt.tz_convert(None).dt.to_period("Q").astype(str).to_numpy()
    tr = tr.assign(quarter=quarters[week_of])
    top10 = list(top.index[:10])
    q_rows = []
    for a in top10:
        out = tr[tr["from"] == a].groupby("quarter")["amount"].sum()
        inn = tr[tr["to"] == a].groupby("quarter")["amount"].sum()
        s = inn.sub(out, fill_value=0)
        for qq, v in s.items():
            q_rows.append({"address": a, "quarter": qq, "net_reth": v})
    pd.DataFrame(q_rows).pivot_table(index="address", columns="quarter", values="net_reth", aggfunc="sum") \
        .reindex(top10).to_csv(os.path.join(OUT_DIR, "holders_top10_by_quarter.csv"))

    # Chart: top 15 leavers
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t15 = top.head(15).iloc[::-1]
    names = [(f"{l} " if l else "") + f"({short(a)})" for a, l in zip(t15.index, t15["label"])]
    colors = ["#4c72b0" if c else "#b0b0b0" for c in t15["is_contract"]]
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.barh(names, -t15["net"] / 1e3, color=colors)
    ax.set_xlabel("Net rETH that left the address since January 2024 (thousand)")
    ax.set_title(f"Top 15 addresses by net rETH decrease; top 10 = {conc['top10_share']:.0%} of the total decrease\n"
                 "(blue = contract, grey = wallet)")
    ax.grid(alpha=0.3, axis="x")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "chart_top_leavers.png"), dpi=150)

    print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in conc.items()}, indent=1))
    print(top[["net", "share_of_total_decrease", "is_contract", "label"]].head(15).round(3).to_string())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        dbg(f"FAILED: {type(e).__name__}: {e}")
        raise
    finally:
        write_debug()
