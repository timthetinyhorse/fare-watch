"""
Fare Watch scanner.

Queries the Duffel API for business class offers on each route in
config.yaml and stores every result in a local SQLite database, building
the price history the analysis relies on.

Run it once a day (see README for scheduling):
    python scanner.py
    python scanner.py --dry-run     # show what would be searched, no API calls
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
import yaml

API_URL = "https://api.duffel.com/air/offer_requests"
HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at      TEXT NOT NULL,     -- UTC timestamp of the search
    route_name      TEXT NOT NULL,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    depart_date     TEXT NOT NULL,
    return_date     TEXT,
    days_out        INTEGER NOT NULL,  -- days between search and departure
    offer_count     INTEGER NOT NULL,
    status          TEXT NOT NULL      -- ok / error message
);

CREATE TABLE IF NOT EXISTS offers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id       INTEGER NOT NULL REFERENCES searches(id),
    airline_code    TEXT,
    airline_name    TEXT,
    total_amount    REAL NOT NULL,
    currency        TEXT NOT NULL,
    booking_class   TEXT,              -- first letter of outbound fare basis
    fare_basis      TEXT,
    stops_out       INTEGER,
    duration_out    TEXT,
    all_business    INTEGER,           -- 1 if every segment is business
    carriers        TEXT               -- all marketing carriers on the trip
);

CREATE INDEX IF NOT EXISTS idx_search_route
    ON searches(origin, destination, depart_date);
CREATE INDEX IF NOT EXISTS idx_offer_search ON offers(search_id);
"""


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def departure_dates(settings, today):
    """Sample departure dates, rotating the start so daily runs cover every date."""
    step = settings["sample_every_n_days"]
    offset = today.toordinal() % step
    first = settings["days_ahead_min"] + offset
    return [today + timedelta(days=d)
            for d in range(first, settings["days_ahead_max"] + 1, step)]


def build_jobs(cfg, today):
    jobs = []
    for route in cfg["watchlist"]:
        for origin in route["origins"]:
            for dep in departure_dates(cfg["settings"], today):
                ret = None
                if route.get("trip", "return") == "return":
                    ret = dep + timedelta(days=route.get("stay_nights", 7))
                jobs.append({
                    "route_name": route["name"],
                    "origin": origin,
                    "destination": route["destination"],
                    "depart_date": dep,
                    "return_date": ret,
                })
    return jobs


def search(token, job, settings):
    slices = [{"origin": job["origin"], "destination": job["destination"],
               "departure_date": job["depart_date"].isoformat()}]
    if job["return_date"]:
        slices.append({"origin": job["destination"], "destination": job["origin"],
                       "departure_date": job["return_date"].isoformat()})

    body = {"data": {
        "slices": slices,
        "passengers": [{"type": "adult"} for _ in range(settings["adults"])],
        "cabin_class": settings["cabin_class"],
        "max_connections": settings["max_connections"],
    }}
    headers = {
        "Authorization": f"Bearer {token}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    r = requests.post(API_URL, json=body, headers=headers,
                      params={"return_offers": "true", "supplier_timeout": 20000},
                      timeout=60)
    if r.status_code == 429:
        raise RateLimited()
    if r.status_code in (401, 403):
        raise AccessDenied(r.status_code, duffel_message(r))
    if not r.ok:
        raise RuntimeError(f"{r.status_code}: {duffel_message(r)}")
    return r.json()["data"].get("offers", [])


class RateLimited(Exception):
    pass


class AccessDenied(Exception):
    pass


def duffel_message(r):
    """Pull Duffel's own explanation out of an error response."""
    try:
        e = r.json()["errors"][0]
        return f"{e.get('title', '')} - {e.get('message', '')} (code: {e.get('code', '?')})"
    except Exception:
        return r.text[:300]


def parse_offer(offer):
    out = offer["slices"][0]
    segs_out = out["segments"]
    first_pax = (segs_out[0].get("passengers") or [{}])[0]
    fare_basis = first_pax.get("fare_basis_code") or ""

    cabins, carriers = [], set()
    for sl in offer["slices"]:
        for seg in sl["segments"]:
            carriers.add((seg.get("marketing_carrier") or {}).get("iata_code", "?"))
            for p in seg.get("passengers") or []:
                cabins.append(p.get("cabin_class"))

    owner = offer.get("owner") or {}
    return {
        "airline_code": owner.get("iata_code"),
        "airline_name": owner.get("name"),
        "total_amount": float(offer["total_amount"]),
        "currency": offer["total_currency"],
        "booking_class": fare_basis[:1] or None,
        "fare_basis": fare_basis or None,
        "stops_out": len(segs_out) - 1,
        "duration_out": out.get("duration"),
        "all_business": int(bool(cabins) and all(c == "business" for c in cabins)),
        "carriers": ",".join(sorted(carriers)),
    }


def store(conn, job, today, offers, status):
    cur = conn.execute(
        """INSERT INTO searches (scanned_at, route_name, origin, destination,
               depart_date, return_date, days_out, offer_count, status)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         job["route_name"], job["origin"], job["destination"],
         job["depart_date"].isoformat(),
         job["return_date"].isoformat() if job["return_date"] else None,
         (job["depart_date"] - today).days, len(offers), status))
    sid = cur.lastrowid
    for o in offers:
        p = parse_offer(o)
        conn.execute(
            """INSERT INTO offers (search_id, airline_code, airline_name,
                   total_amount, currency, booking_class, fare_basis,
                   stops_out, duration_out, all_business, carriers)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, p["airline_code"], p["airline_name"], p["total_amount"],
             p["currency"], p["booking_class"], p["fare_basis"], p["stops_out"],
             p["duration_out"], p["all_business"], p["carriers"]))
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    settings = cfg["settings"]
    today = date.today()
    jobs = build_jobs(cfg, today)

    cap = settings["max_searches_per_run"]
    if len(jobs) > cap:
        print(f"{len(jobs)} searches planned; capping at {cap}. "
              "Trim the watchlist or raise max_searches_per_run.")
        jobs = jobs[:cap]

    if args.dry_run:
        for j in jobs:
            print(f"{j['origin']}-{j['destination']}  {j['depart_date']}"
                  f"  return {j['return_date']}")
        print(f"\n{len(jobs)} searches would run.")
        return

    token = os.environ.get("DUFFEL_TOKEN")
    if not token:
        sys.exit("Set DUFFEL_TOKEN to your Duffel access token (see README).")

    conn = connect(os.path.join(HERE, settings["database"]))
    ok = failed = total_offers = 0
    for i, job in enumerate(jobs, 1):
        label = f"[{i}/{len(jobs)}] {job['origin']}-{job['destination']} {job['depart_date']}"
        for attempt in range(3):
            try:
                offers = search(token, job, settings)
                store(conn, job, today, offers, "ok")
                ok += 1
                total_offers += len(offers)
                print(f"{label}: {len(offers)} offers")
                break
            except AccessDenied as e:
                print(f"{label}: Duffel refused access ({e.args[0]}).")
                print(f"Duffel says: {e.args[1]}")
                print("Stopping: every search would fail the same way. "
                      "See the README section 'If Duffel refuses access'.")
                sys.exit(1)
            except RateLimited:
                print(f"{label}: rate limited, waiting 60s")
                time.sleep(60)
            except Exception as e:  # keep going; log the failure
                store(conn, job, today, [], f"error: {e}"[:300])
                failed += 1
                print(f"{label}: failed ({e})")
                break
        time.sleep(settings["seconds_between_searches"])

    print(f"\nDone. {ok} searches stored, {failed} failed, {total_offers} offers saved.")


if __name__ == "__main__":
    main()
