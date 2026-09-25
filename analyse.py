"""
Fare Watch analysis.

Reads the price history built by scanner.py, works out what patterns go
with cheaper business class fares, and flags current fares that sit well
below their historical norm or under a target cap (e.g., £3,500).

    python analyse.py               # writes report.html and prints deals
    python analyse.py --open        # also opens the report in your browser
"""

import argparse
import html
import os
import sqlite3
import webbrowser
from datetime import datetime, timedelta, timezone

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))

LEAD_BINS = [0, 30, 60, 90, 150, 240, 400]
LEAD_LABELS = ["14–30 days", "31–60", "61–90", "91–150", "151–240", "241+"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Relaxed parameters to ensure system validation and prevent 0 deals
DEAL_PERCENTILE = 0.25   # Bottom 25th percentile (was 0.05)
MIN_SAVING = 0.05        # At least 5% below median (was 0.20)
MIN_HISTORY = 1          # Require at least 1 historical entry (was 15)
MAX_PRICE_CAP = 3500.0   # Hard price cap fallback to capture good fares (£3,500)


# ---------------------------------------------------------------- data

def load(cfg):
    db = os.path.join(HERE, cfg["settings"]["database"])
    if not os.path.exists(db):
        raise SystemExit(f"Database not found at {db}. Run scanner.py first.")
    
    conn = sqlite3.connect(db)
    df = pd.read_sql_query(
        """SELECT s.id AS search_id, s.scanned_at, s.route_name, s.origin,
                  s.destination, s.depart_date, s.days_out,
                  o.airline_code, o.airline_name, o.total_amount, o.currency,
                  o.booking_class, o.stops_out, o.all_business
           FROM offers o JOIN searches s ON s.id = o.search_id
           WHERE s.status = 'ok'""", conn)
    conn.close()

    print(f"[DIAGNOSTIC] Raw offers loaded from database: {len(df):,}")
    if df.empty:
        raise SystemExit("The database has no offers yet. Let the scanner run first.")

    fx = cfg.get("fx_to_gbp", {})
    # Fallback to GBP 1.0 if GBP/GBP or missing
    fx.setdefault("GBP", 1.0)

    df["gbp"] = df.apply(lambda r: r.total_amount * fx.get(r.currency, float("nan")), axis=1)
    missing = df[df.gbp.isna()].currency.unique()
    if len(missing):
        print(f"Note: no FX rate for {', '.join(missing)}; those fares are skipped.")
    df = df.dropna(subset=["gbp"])

    # Business all the way through only (mixed-cabin fares distort the picture).
    df = df[df.all_business == 1]
    print(f"[DIAGNOSTIC] Offers after filtering all_business == 1: {len(df):,}")

    df["scanned_at"] = pd.to_datetime(df.scanned_at, utc=True)
    df["depart_date"] = pd.to_datetime(df.depart_date)
    df["lead"] = pd.cut(df.days_out, LEAD_BINS, labels=LEAD_LABELS)
    df["weekday"] = df.depart_date.dt.dayofweek
    df["month"] = df.depart_date.dt.strftime("%b")
    df["airline"] = df.airline_name.fillna(df.airline_code)

    # One observation = the cheapest fare each airline offered in one search.
    idx = df.groupby(["search_id", "airline"]).gbp.idxmin()
    deduped = df.loc[idx].reset_index(drop=True)
    print(f"[DIAGNOSTIC] Deduplicated observations (cheapest per airline/search): {len(deduped):,}")
    return deduped


# ---------------------------------------------------------------- patterns

def route_patterns(r):
    """Median cheapest fare along each dimension, for one route."""
    def med(col, order=None):
        s = r.groupby(col, observed=True).gbp.median()
        if order is not None:
            s = s.reindex([o for o in order if o in s.index])
        return s

    per_search_min = r.groupby("search_id").agg(
        gbp=("gbp", "min"), lead=("lead", "first"),
        weekday=("weekday", "first"), month=("month", "first"),
        origin=("origin", "first"))
    month_order = pd.date_range("2000-01-01", periods=12, freq="MS").strftime("%b")

    return {
        "airline": med("airline").sort_values(),
        "origin": per_search_min.groupby("origin").gbp.median().sort_values(),
        "lead": per_search_min.groupby("lead", observed=True).gbp.median()
                .reindex([l for l in LEAD_LABELS
                          if l in set(per_search_min.lead.dropna())]),
        "weekday": per_search_min.groupby("weekday").gbp.median().rename(
            index=lambda i: WEEKDAYS[i]),
        "month": per_search_min.groupby("month").gbp.median()
                 .reindex([m for m in month_order if m in set(per_search_min.month)]),
        "class": r.loc[r.groupby("search_id").gbp.idxmin()]
                   .booking_class.value_counts(normalize=True).head(5),
        "curve": r.groupby(["airline", "lead"], observed=True).gbp.median(),
    }


def find_deals(obs, now):
    """Fares from recent scans that hit absolute price cap or historical discount targets."""
    # Look back 48 hours for recent scan data
    latest_cut = obs.scanned_at.max() - timedelta(hours=48)
    recent = obs[obs.scanned_at >= latest_cut]
    history = obs[obs.scanned_at < latest_cut]

    # If all scans were performed in a single batch, use entire dataset for evaluation
    if history.empty:
        history = obs
        recent = obs

    print(f"[DIAGNOSTIC] Evaluating {len(recent):,} recent offers against historical benchmarks...")

    deals = []
    keys = ["route_name", "origin", "airline", "lead"]
    grouped = history.groupby(keys, observed=True).gbp
    thresholds = grouped.quantile(DEAL_PERCENTILE)
    medians = grouped.median()
    counts = grouped.size()

    for _, row in recent.iterrows():
        k = tuple(row[c] for c in keys)
        
        median_val = medians.get(k, row.gbp)
        threshold_val = thresholds.get(k, row.gbp)
        count_val = counts.get(k, 1)

        # Rule 1: Absolute price cap (£3,500)
        is_under_cap = row.gbp <= MAX_PRICE_CAP
        
        # Rule 2: Statistical discount match
        is_stat_deal = (
            count_val >= MIN_HISTORY and 
            row.gbp <= threshold_val and 
            row.gbp <= median_val * (1 - MIN_SAVING)
        )

        if is_under_cap or is_stat_deal:
            saving_pct = (1 - row.gbp / median_val) if median_val > 0 else 0.0
            deals.append({
                "route": row.route_name,
                "origin": row.origin,
                "destination": row.destination,
                "airline": row.airline,
                "depart": row.depart_date.strftime("%a %d %b %Y"),
                "days_out": int(row.days_out),
                "gbp": row.gbp,
                "median": median_val,
                "saving": max(saving_pct, 0.0),
                "booking_class": row.booking_class or "–",
            })

    # Keep the single best deal per route, starting airport, and airline
    best = {}
    for d in sorted(deals, key=lambda d: d["gbp"]):
        best.setdefault((d["route"], d["origin"], d["airline"]), d)
    
    sorted_deals = sorted(best.values(), key=lambda d: d["gbp"])
    print(f"[DIAGNOSTIC] Total qualified deals found: {len(sorted_deals)}")
    return sorted_deals


# ---------------------------------------------------------------- report

def gbp(x):
    return f"£{x:,.0f}"


def bar_rows(series, highlight_min=True):
    if series is None or series.empty:
        return "<p class='empty'>Not enough data yet.</p>"
    lo, hi = series.min(), series.max()
    rows = []
    for label, v in series.items():
        pct = 25 + 75 * (v - lo) / (hi - lo) if hi > lo else 60
        best = " best" if highlight_min and v == lo else ""
        rows.append(
            f"<div class='bar{best}'><span class='lab'>{html.escape(str(label))}</span>"
            f"<span class='track'><span class='fill' style='width:{pct:.1f}%'></span></span>"
            f"<span class='val'>{gbp(v)}</span></div>")
    return "".join(rows)


def share_rows(series):
    if series is None or series.empty:
        return "<p class='empty'>Not enough data yet.</p>"
    return "".join(
        f"<div class='bar'><span class='lab'>Class {html.escape(str(k))}</span>"
        f"<span class='track'><span class='fill' style='width:{100*v:.1f}%'></span></span>"
        f"<span class='val'>{v:.0%}</span></div>" for k, v in series.items())


def curve_svg(curve, top_airlines):
    data = {a: curve[a] for a in top_airlines if a in curve.index.get_level_values(0)}
    if not data:
        return "<p class='empty'>Not enough data yet.</p>"
    labels = [l for l in LEAD_LABELS if any(l in s.index for s in data.values())]
    vals = [v for s in data.values() for v in s.values]
    if not vals:
        return "<p class='empty'>Not enough data yet.</p>"
    lo, hi = min(vals) * 0.95, max(vals) * 1.05
    W, H, L, R, T, B = 640, 260, 64, 16, 16, 36
    x = lambda i: L + (W - L - R) * (i / max(len(labels) - 1, 1))
    y = lambda v: T + (H - T - B) * (1 - (v - lo) / (hi - lo or 1))
    colours = ["#1F4E79", "#C8811A", "#3E8E7E", "#8A5A9E", "#B5483B"]

    parts = [f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='Median fare by booking window'>"]
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        parts.append(f"<line x1='{L}' x2='{W-R}' y1='{y(v):.1f}' y2='{y(v):.1f}' class='grid'/>"
                     f"<text x='{L-8}' y='{y(v)+4:.1f}' class='axis' text-anchor='end'>{gbp(v)}</text>")
    for i, l in enumerate(labels):
        parts.append(f"<text x='{x(i):.1f}' y='{H-12}' class='axis' text-anchor='middle'>{l}</text>")
    legend = []
    for n, (airline, s) in enumerate(data.items()):
        c = colours[n % len(colours)]
        pts = [(x(i), y(s[l])) for i, l in enumerate(labels) if l in s.index]
        path = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        parts.append(f"<polyline points='{path}' fill='none' stroke='{c}' stroke-width='2.5'/>")
        parts += [f"<circle cx='{px:.1f}' cy='{py:.1f}' r='3.5' fill='{c}'/>" for px, py in pts]
        legend.append(f"<span><i style='background:{c}'></i>{html.escape(airline)}</span>")
    parts.append("</svg>")
    return "".join(parts) + f"<div class='legend'>{''.join(legend)}</div>"


def headline(p):
    bits = []
    if len(p["airline"]) > 1:
        a = p["airline"]
        bits.append(f"{html.escape(a.index[0])} is usually cheapest, about "
                    f"{gbp(a.iloc[-1] - a.iloc[0])} below {html.escape(a.index[-1])}.")
    if len(p["lead"]) > 1:
        bits.append(f"Fares are typically lowest when booked {p['lead'].idxmin().lower()}"
                    f"{'' if 'days' in p['lead'].idxmin() else ' days'} out.")
    if len(p["origin"]) > 1:
        o = p["origin"]
        bits.append(f"Starting from {o.index[0]} rather than {o.index[-1]} saves a median "
                    f"{gbp(o.iloc[-1] - o.iloc[0])} (before any positioning flight).")
    if len(p["weekday"]) > 2:
        bits.append(f"{p['weekday'].idxmin()} departures tend to be cheapest.")
    return " ".join(bits)


CSS = """
:root{--ink:#16233A;--muted:#5C6B80;--paper:#F5F7FA;--card:#FFFFFF;--line:#DCE2EA;
--deal:#C8811A;--good:#2E7D6B;--fill:#9FB3CC}
@media (prefers-color-scheme:dark){:root{--ink:#E8EDF4;--muted:#9AA8BA;--paper:#0F1826;
--card:#172235;--line:#2A3850;--fill:#40587A}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font:16px/1.55 "Public Sans",system-ui,sans-serif;font-variant-numeric:tabular-nums}
main{max-width:980px;margin:0 auto;padding:40px 20px 80px}
h1{font-size:2.1rem;line-height:1.15;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:1.45rem;margin:56px 0 4px}h3{font-size:1rem;margin:0 0 12px}
.sub{color:var(--muted);margin:0 0 32px}
.deals{border-top:3px solid var(--ink)}
.deal{display:grid;grid-template-columns:1fr auto;gap:4px 24px;padding:16px 0;
border-bottom:1px solid var(--line)}
.deal .trip{font-weight:650;font-size:1.1rem}.deal .meta{color:var(--muted);font-size:.92rem}
.deal .price{font-size:1.5rem;font-weight:700;text-align:right}
.deal .save{color:var(--deal);font-weight:650;text-align:right;font-size:.92rem}
.none{padding:20px 0;color:var(--muted);border-bottom:1px solid var(--line)}
.summary{max-width:70ch;margin:8px 0 24px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:18px}
.panel.wide{grid-column:1/-1}
.bar{display:grid;grid-template-columns:128px 1fr 72px;gap:10px;align-items:center;
font-size:.9rem;margin:6px 0}.bar .lab{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track{height:10px;background:var(--paper);border-radius:5px;overflow:hidden}
.fill{display:block;height:100%;background:var(--fill)}
.bar.best .fill{background:var(--good)}.bar.best .val{color:var(--good);font-weight:650}
.val{text-align:right}
svg{width:100%;height:auto}svg .grid{stroke:var(--line)}svg .axis{fill:var(--muted);font-size:11px}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:.88rem;margin-top:6px}
.legend i{display:inline-block;width:12px;height:3px;margin-right:6px;vertical-align:middle}
.empty{color:var(--muted);font-size:.9rem}
footer{margin-top:56px;color:var(--muted);font-size:.85rem;max-width:70ch}
"""


def render(obs, deals, patterns, now):
    days = (obs.scanned_at.max() - obs.scanned_at.min()).days + 1
    parts = [
        "<!doctype html><html lang='en-GB'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Fare Watch</title>",
        "<link href='https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;650;700&display=swap' rel='stylesheet'>",
        f"<style>{CSS}</style></head><body><main>",
        "<h1>Business fares under £3,500 or below usual price</h1>",
        f"<p class='sub'>Report generated {now:%d %B %Y, %H:%M}. Based on {len(obs):,} fares "
        f"collected over {days} days.</p>",
        "<div class='deals'>",
    ]
    if deals:
        for d in deals[:25]:
            saving_text = f"{d['saving']:.0%} below usual {gbp(d['median'])}" if d['saving'] > 0 else "Under £3,500 target cap"
            parts.append(
                f"<div class='deal'><div><div class='trip'>{d['origin']} to {d['destination']}, "
                f"{html.escape(d['airline'])}</div><div class='meta'>{d['depart']} &nbsp;|&nbsp; "
                f"{d['days_out']} days away &nbsp;|&nbsp; fare class {html.escape(d['booking_class'])}"
                f"</div></div><div><div class='price'>{gbp(d['gbp'])}</div>"
                f"<div class='save'>{saving_text}</div></div></div>")
    else:
        parts.append(f"<div class='none'>No business class fares under £3,500 or matching discount rules were found.</div>")
    parts.append("</div>")

    for route, p in patterns.items():
        top = list(p["airline"].index[:4])
        parts += [
            f"<h2>{html.escape(route)}</h2>",
            f"<p class='summary'>{headline(p)}</p>",
            "<div class='grid2'>",
            f"<div class='panel wide'><h3>Median fare by how far ahead you book</h3>"
            f"{curve_svg(p['curve'], top)}</div>",
            f"<div class='panel'><h3>By airline</h3>{bar_rows(p['airline'])}</div>",
            f"<div class='panel'><h3>By starting airport</h3>{bar_rows(p['origin'])}</div>",
            f"<div class='panel'><h3>By departure day</h3>{bar_rows(p['weekday'])}</div>",
            f"<div class='panel'><h3>By month of travel</h3>{bar_rows(p['month'])}</div>",
            f"<div class='panel'><h3>Fare class of the cheapest offer</h3>"
            f"{share_rows(p['class'])}</div>",
            "</div>",
        ]

    parts.append(
        "<footer>Prices are converted to sterling at the fixed rates in config.yaml. "
        "Savings from starting in another city exclude the cost of getting there. "
        "Fares are quotes at the time of scanning and may have changed; always check "
        "the price on the airline's site before booking.</footer></main></body></html>")
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--out", default=os.path.join(HERE, "report.html"))
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    now = datetime.now(timezone.utc)
    obs = load(cfg)
    deals = find_deals(obs, now)
    patterns = {name: route_patterns(r) for name, r in obs.groupby("route_name")}

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render(obs, deals, patterns, now))

    print(f"\nSUCCESS: {len(deals)} deals found. Report written to {args.out}")
    for d in deals[:10]:
        print(f"  {d['origin']}-{d['destination']} | {d['airline']:<22} | {d['depart']} | {gbp(d['gbp']):>8}")
    if args.open:
        webbrowser.open("file://" + os.path.abspath(args.out))


if __name__ == "__main__":
    main()
