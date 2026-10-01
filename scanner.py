import os
import sqlite3
import time
from datetime import datetime, timedelta
import requests

# ---------------------------------------------------------------------------
# Configuration & Database Setup
# ---------------------------------------------------------------------------
DUFFEL_API_KEY = os.getenv("DUFFEL_API_KEY", "duffel_test_YOUR_API_KEY")
DB_PATH = "fares.db"


def init_db():
    """Ensure database schema includes all required fields."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            route_name TEXT,
            origin TEXT,
            destination TEXT,
            depart_date TEXT,
            days_out INTEGER,
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
            stops_out INTEGER,
            all_business INTEGER,
            FOREIGN KEY (search_id) REFERENCES searches(id)
        )
    """
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Duffel API Scanner (Open-Jaw Business Class)
# ---------------------------------------------------------------------------
def scan_window(
    origin="NCL",
    outbound_dest="SFO",
    return_orig="LAX",
    depart_date="",
    return_date="",
    route_name="NCL-SFO/LAX-NCL",
    top_limit=250,
):
    """Queries Duffel for Open-Jaw Business Class flight offers and saves top N cheapest."""
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
                    "destination": outbound_dest,
                    "departure_date": depart_date,
                },
                {
                    "origin": return_orig,
                    "destination": origin,
                    "departure_date": return_date,
                },
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
            error_type = data.get("errors", [{}])[0].get("type", "unknown_error")
            error_msg = data.get("errors", [{}])[0].get("message", "Request failed")
            print(f"Error scanning {depart_date}: {error_type} - {error_msg}")

            cursor.execute(
                """
                INSERT INTO searches (scanned_at, route_name, origin, destination, depart_date, days_out, status, offer_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    now_str,
                    route_name,
                    origin,
                    outbound_dest,
                    depart_date,
                    14,
                    "error",
                    0,
                ),
            )
            conn.commit()
            return

        offers = data.get("data", {}).get("offers", [])
        total_found = len(offers)

        # Sort all offers by price ascending and slice the top N cheapest
        sorted_offers = sorted(
            offers, key=lambda x: float(x.get("total_amount", float("inf")))
        )[:top_limit]

        # Insert search log entry recording the count stored
        cursor.execute(
            """
            INSERT INTO searches (scanned_at, route_name, origin, destination, depart_date, days_out, status, offer_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                now_str,
                route_name,
                origin,
                outbound_dest,
                depart_date,
                14,
                "ok",
                len(sorted_offers),
            ),
        )
        search_id = cursor.lastrowid

        # Save top 250 lowest offers
        for offer in sorted_offers:
            owner = offer.get("owner", {})
            airline_code = owner.get("iata_code", "XX")
            airline_name = owner.get("name", "Unknown Airline")
            total_amount = float(offer.get("total_amount", 0.0))
            currency = offer.get("total_currency", "GBP")

            outbound_slices = (
                offer.get("slices", [])[0] if offer.get("slices") else {}
            )
            segments = outbound_slices.get("segments", [])
            stops_out = max(0, len(segments) - 1)

            cursor.execute(
                """
                INSERT INTO offers (
                    search_id, airline_code, airline_name, total_amount, currency,
                    booking_class, stops_out, all_business
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    search_id,
                    airline_code,
                    airline_name,
                    total_amount,
                    currency,
                    "business",
                    stops_out,
                    1,
                ),
            )

        conn.commit()
        print(
            f"Scanned NCL->SFO ({depart_date}) & LAX->NCL ({return_date}): Total found {total_found}, saved top {len(sorted_offers)} cheapest"
        )

    except Exception as e:
        print(f"Error scanning {depart_date}: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main Execution Loop (July & August 2027)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    print(
        "Starting Open-Jaw Business Class Scan (NCL -> SFO / LAX -> NCL) for July & August 2027..."
    )

    start_date = datetime.strptime("2027-07-01", "%Y-%m-%d")
    end_date = datetime.strptime("2027-08-31", "%Y-%m-%d")

    current_dep = start_date
    while current_dep <= end_date:
        dep = current_dep.strftime("%Y-%m-%d")
        ret = (current_dep + timedelta(days=14)).strftime("%Y-%m-%d")

        scan_window(
            origin="NCL",
            outbound_dest="SFO",
            return_orig="LAX",
            depart_date=dep,
            return_date=ret,
            top_limit=250,
        )

        current_dep += timedelta(days=3)
        time.sleep(1)

    print("Business class scan completed successfully. Results saved to fares.db.")
