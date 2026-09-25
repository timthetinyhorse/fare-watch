"""
Generates six months of realistic sample fares so you can see the report
before your own history has built up. Writes to demo.db, not fares.db.

    python demo_data.py
    python analyse.py --config demo_config.yaml --open
"""

import os
import random
import shutil
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

import yaml

from scanner import SCHEMA, build_jobs, load_config

HERE = os.path.dirname(os.path.abspath(__file__))
random.seed(7)

# Rough, invented baselines (GBP, return, business) purely for illustration.
BASE = {"JFK": 3400, "DXB": 2600, "SIN": 4300}
AIRLINES = {
    "JFK": [("BA", "British Airways", 1.00), ("VS", "Virgin Atlantic", 0.95),
            ("AA", "American Airlines", 0.90), ("EI", "Aer Lingus", 0.78)],
    "DXB": [("EK", "Emirates", 1.00), ("BA", "British Airways", 0.97),
            ("QR", "Qatar Airways", 0.88), ("TK", "Turkish Airlines", 0.74)],
    "SIN": [("SQ", "Singapore Airlines", 1.00), ("BA", "British Airways", 0.96),
            ("QR", "Qatar Airways", 0.84), ("EK", "Emirates", 0.86)],
}
ORIGIN_FACTOR = {"LHR": 1.0, "NCL": 0.97, "DUB": 0.72, "OSL": 0.68}
FX = {"LHR": ("GBP", 1.0), "NCL": ("GBP", 1.0), "DUB": ("EUR", 0.85), "OSL": ("NOK", 0.071)}
MONTH = {1: .88, 2: .9, 3: .97, 4: 1.0, 5: 1.02, 6: 1.08, 7: 1.12, 8: 1.05,
         9: 1.0, 10: .98, 11: .9, 12: 1.1}
WEEKDAY = [1.02, .94, .93, 1.0, 1.08, 1.03, 1.06]


def lead_factor(days):
    # Business fares: dear close in, a sweet spot around 2-5 months, firm far out.
    if days < 30: return 1.35
    if days < 60: return 1.12
    if days < 90: return 1.0
    if days < 150: return 0.9
    if days < 240: return 0.95
    return 1.02


def main():
    cfg = load_config(os.path.join(HERE, "config.yaml"))
    cfg["settings"]["database"] = "demo.db"
    with open(os.path.join(HERE, "demo_config.yaml"), "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

    db = os.path.join(HERE, "demo.db")
    if os.path.exists(db):
        os.remove(db)
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)

    start = date.today() - timedelta(days=180)
    for n in range(181):
        day = start + timedelta(days=n)
        scanned = datetime.combine(day, time(6, 0), tzinfo=timezone.utc).isoformat()
        for job in build_jobs(cfg, day):
            dest, origin = job["destination"], job["origin"]
            days_out = (job["depart_date"] - day).days
            cur = conn.execute(
                "INSERT INTO searches (scanned_at, route_name, origin, destination, depart_date,"
                " return_date, days_out, offer_count, status) VALUES (?,?,?,?,?,?,?,?,'ok')",
                (scanned, job["route_name"], origin, dest, job["depart_date"].isoformat(),
                 job["return_date"].isoformat(), days_out, len(AIRLINES[dest])))
            sid = cur.lastrowid
            for code, name, af in AIRLINES[dest]:
                if origin != "LHR" and random.random() < 0.3:
                    continue  # not every airline serves every origin
                sale = 0.75 if random.random() < 0.04 else 1.0   # occasional sale fares
                price_gbp = (BASE[dest] * af * ORIGIN_FACTOR[origin] * lead_factor(days_out)
                             * MONTH[job["depart_date"].month]
                             * WEEKDAY[job["depart_date"].weekday()]
                             * sale * random.gauss(1, 0.07))
                ccy, rate = FX[origin]
                cls = "I" if sale < 1 or price_gbp < BASE[dest] * 0.85 else random.choice("DCJ")
                conn.execute(
                    "INSERT INTO offers (search_id, airline_code, airline_name, total_amount,"
                    " currency, booking_class, fare_basis, stops_out, duration_out, all_business,"
                    " carriers) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                    (sid, code, name, round(price_gbp / rate, 2), ccy, cls, cls + "RT",
                     0 if origin == "LHR" else 1, None, code))
    conn.commit()
    conn.close()
    print("Sample data written to demo.db. Now run:\n"
          "  python analyse.py --config demo_config.yaml --open")


if __name__ == "__main__":
    main()
