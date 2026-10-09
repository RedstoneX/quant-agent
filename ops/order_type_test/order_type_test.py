"""Go-live order-type fill test: market vs limit fills per order type.

Paper fills are simulated at the NBBO, so only a LIVE run measures anything.
Needs env: ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_TRADING_URL (no default:
the caller must name paper or live explicitly), and SANDBOX_ACCOUNT_SUFFIX
(the account number must end with it).
"""

import io, json, os, time, csv, urllib.request, threading, datetime as dt, statistics as st

H = {"Content-Type": "application/json"}
T = os.environ.get("ALPACA_TRADING_URL", "")
D = "https://data.alpaca.markets"


def ott_req(m, u, b=None):
    r = urllib.request.Request(u, method=m, headers=H, data=json.dumps(b).encode() if b else None)
    try:
        with urllib.request.urlopen(r, timeout=20) as f:
            t = f.read()
            return json.loads(t) if t else {}
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:200]}


def ott_quote(s):
    q = ott_req("GET", f"{D}/v2/stocks/{s}/quotes/latest?feed=iex")["quote"]
    t = ott_req("GET", f"{D}/v2/stocks/{s}/trades/latest?feed=iex")["trade"]
    return q["bp"], q["ap"], t["p"]


def ott_now():
    return time.time()


def ott_ts(x):
    return dt.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp() if x else None


rows = []
L = threading.Lock()


def ott_submit(s, side, qty, typ, lim=None):
    b = {"symbol": s, "qty": str(qty), "side": side, "type": typ, "time_in_force": "day"}
    if lim is not None:
        b["limit_price"] = f"{lim:.2f}"
    t0 = ott_now()
    o = ott_req("POST", T + "/v2/orders", b)
    return o, t0


def ott_wait(oid, sec, cancel=False):
    end = ott_now() + sec
    o = {}
    while ott_now() < end:
        o = ott_req("GET", T + "/v2/orders/" + oid)
        if o.get("status") in ("filled", "canceled", "rejected", "expired"):
            return o
        time.sleep(1)
    if cancel:
        ott_req("DELETE", T + "/v2/orders/" + oid)
        time.sleep(1.5)
        o = ott_req("GET", T + "/v2/orders/" + oid)
    return o


def ott_rec(s, side, method, lot, qty, bid, ask, last, o, t0, lim):
    mid = (bid + ask) / 2
    fa = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
    ft = ott_ts(o.get("filled_at"))
    with L:
        rows.append(
            dict(
                sym=s,
                side=side,
                method=method,
                lot=lot,
                qty=qty,
                bid=bid,
                ask=ask,
                mid=round(mid, 4),
                last=last,
                limit=lim,
                submit=dt.datetime.utcfromtimestamp(t0).isoformat(),
                fill_time=o.get("filled_at"),
                secs=round(ft - t0, 2) if ft else None,
                fill_px=fa,
                filled_qty=o.get("filled_qty"),
                status=o.get("status", str(o.get("error"))),
                bps_vs_mid=round((fa - mid) / mid * 1e4, 2) if fa else None,
            )
        )


def ott_buy(s, lot, qty):
    bid, ask, last = ott_quote(s)
    o, t0 = ott_submit(s, "buy", qty, "market")
    if "id" not in o:
        ott_rec(s, "buy", "market", lot, qty, bid, ask, last, o, t0, None)
        return False
    o = ott_wait(o["id"], 30)
    ott_rec(s, "buy", "market", lot, qty, bid, ask, last, o, t0, None)
    return True


def ott_sell(s, m, lot, qty):
    bid, ask, last = ott_quote(s)
    mid = (bid + ask) / 2
    lim = {"a": None, "b": round(mid, 2), "c": round(bid, 2), "d": round(last * 0.995, 2)}[m]
    o, t0 = ott_submit(s, "sell", qty, "market" if m == "a" else "limit", lim)
    if "id" not in o:
        ott_rec(s, "sell", m, lot, qty, bid, ask, last, o, t0, lim)
        return
    o = ott_wait(o["id"], 60 if m == "b" else 30, cancel=True)
    ott_rec(s, "sell", m, lot, qty, bid, ask, last, o, t0, lim)


def ott_run(s):
    jobs = [(m, lot, q) for m in "abcd" for lot, q in (("whole", 1), ("frac", 0.37))]
    for m, lot, q in jobs:
        ott_buy(s, lot + m, q)
    time.sleep(2)
    th = [threading.Thread(target=sell, args=(s, m, lot, q)) for m, lot, q in jobs]
    [t.start() for t in th]
    [t.join() for t in th]


def main():
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    suffix = os.environ.get("SANDBOX_ACCOUNT_SUFFIX", "")
    if not (key and secret and suffix and T):
        raise SystemExit("ABORT set ALPACA_API_KEY, ALPACA_SECRET_KEY, SANDBOX_ACCOUNT_SUFFIX")
    H.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    a = ott_req("GET", T + "/v2/account")
    if not str(a.get("account_number", "")).endswith(suffix):
        raise SystemExit("ABORT account mismatch")
    print("account ok")
    th = [threading.Thread(target=run, args=(s,)) for s in ["AAPL", "MSFT", "NVDA", "AMD", "META"]]
    [t.start() for t in th]
    [t.join() for t in th]
    print(ott_req("DELETE", T + "/v2/positions?cancel_orders=true"))
    time.sleep(5)
    print("positions left:", len(ott_req("GET", T + "/v2/positions")))
    sio = io.StringIO()
    w = csv.DictWriter(sio, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    print("CSV_START")
    print(sio.getvalue())
    print("SUMMARY")
    for side in ("buy", "sell"):
        for m in sorted({r["method"] for r in rows if r["side"] == side}):
            rs = [r for r in rows if r["side"] == side and r["method"] == m]
            f = [r for r in rs if r["fill_px"]]
            sec = [r["secs"] for r in f if r["secs"] is not None]
            bp = [r["bps_vs_mid"] for r in f]
            print(
                side,
                m,
                "n",
                len(rs),
                "filled",
                len(f),
                "secs med",
                st.median(sec) if sec else None,
                "max",
                max(sec) if sec else None,
                "bps mean",
                round(st.mean(bp), 2) if bp else None,
                "med",
                st.median(bp) if bp else None,
            )


if __name__ == "__main__":
    main()
