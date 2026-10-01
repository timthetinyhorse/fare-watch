import os
import sqlite3
import time
from datetime import datetime, timedelta
import requests

DUFFEL_API_KEY = os.getenv("DUFFEL_API_KEY", "duffel_test_YOUR_API_KEY")
DB_PATH = "fares.db"


def init_db():
    """Setup database schema to support individual leg tracking and paired routes."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            leg_type TEXT,          -- 'outbound' or 'return'
            origin TEXT,
            destination TEXT,
            depart_date TEXT,
            status TEXT,
            offer_count INTEGER DEFAULT 0
        )
    """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_id INTEGER,
            airline_code TEXT,
            airline_name TEXT,
            total_amount REAL,
            currency TEXT,
            booking_class TEXT,
            stops INTEGER,
            FOREIGN KEY (search_id) REFERENCES searches(id)
        )
    """
    )

    conn.commit()
    conn.close()


def fetch_one_way_leg(
    origin, destination, depart_date, leg_type="outbound", top_limit=100
):
    """Executes a single one-way flight search on Duffel."""
    url = "https://api.duffel.com/air/offer_requests"

    headers = {
        "Authorization": f"Bearer {DUFFEL_API_KEY}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    payload = {
        "data": {
            "slices": [
                {
                    "origin": origin,
                    "destination": destination,
                    "departure_date": depart_date,
                }
            ],
            "passengers": [{"type": "adult"}],
            "cabin_class": "business",
        }
    }

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        data = response.json()

        if response.status_code not in (200, 201):
            print(f"Error scanning {origin}->{destination} on {depart_date}")
            cursor.execute(
                """
                INSERT INTO searches (scanned_at, leg_type, origin, destination, depart_date, status, offer_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    now_str,
                    leg_type,
                    origin,
                    destination,
                    depart_date,
                    "error",
                    0,
                ),
            )
            conn.commit()
            return

        offers = data.get("data", {}).get("offers", [])

        # Sort by lowest total fare and limit
        sorted_offers = sorted(
            offers, key=lambda x: float(x.get("total_amount", float("inf")))
        )[:top_limit]

        cursor.execute(
            """
            INSERT INTO searches (scanned_at, leg_type, origin, destination, depart_date, status, offer_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                now_str,
                leg_type,
                origin,
                destination,
                depart_date,
                "ok",
                len(sorted_offers),
            ),
        )
        search_id = cursor.lastrowid

        for offer in sorted_offers:
            owner = offer.get("owner", {})
            airline_code = owner.get("iata_code", "XX")
            airline_name = owner.get("name", "Unknown Airline")
            total_amount = float(offer.get("total_amount", 0.0))
            currency = offer.get("total_currency", "GBP")

            slices = offer.get("slices", [])
            segments = slices[0].get("segments", []) if slices else []
            stops = max(0, len(segments) - 1)

            cursor.execute(
                """
                INSERT INTO offers (
                    search_id, airline_code, airline_name, total_amount, currency,
                    booking_class, stops
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    search_id,
                    airline_code,
                    airline_name,
                    total_amount,
                    currency,
                    "business",
                    stops,
                ),
            )

        conn.commit()
        print(
            f"Scanned {leg_type.upper()} {origin}->{destination} ({depart_date}): Saved top {len(sorted_offers)} offers"
        )

    except Exception as e:
        print(f"Exception during {origin}->{destination}: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("Starting One-Way Leg Scan for Open-Jaw Route (July & August 2027)...")

    start_date = datetime.strptime("2027-07-01", "%Y-%m-%d")
    end_date = datetime.strptime("2027-08-31", "%Y-%m-%d")

    current_dep = start_date
    while current_dep <= end_date:
        outbound_date = current_dep.strftime("%Y-%m-%d")
        return_date = (current_dep + timedelta(days=14)).strftime("%Y-%m-%d")

        # 1. Fetch Outbound Leg (NCL -> SFO)
        fetch_one_way_leg(
            "NCL", "SFO", outbound_date, leg_type="outbound", top_limit=100
        )
        time.sleep(0.5)

        # 2. Fetch Return Leg (LAX -> NCL)
        fetch_one_way_leg(
            "LAX", "NCL", return_date, leg_type="return", top_limit=100
        )
        time.sleep(0.5)

        current_dep += timedelta(days=3)

    print("Individual leg scan complete.")
