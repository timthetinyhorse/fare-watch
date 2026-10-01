import os
import sqlite3
import time
from datetime import datetime, timedelta
import requests

# ---------------------------------------------------------------------------
# Configuration & Database Setup
# ---------------------------------------------------------------------------
DUFFEL_API_KEY = os.getenv("DUFFEL_API_KEY", "duffel_test_YOUR_API_KEY")
DB_PATH = "farewatch.db"


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
            status TEXT
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
# Duffel API Scanner (Business Class)
# ---------------------------------------------------------------------------
def scan_window(origin, destination, depart_date, return_date, route_name="NCL-US"):
    """Queries Duffel for Business Class flight offers using the active v2 API version."""
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
                {"origin": origin, "destination": destination, "departure_date": depart_date},
                {"origin": "LAX", "destination": origin, "departure_date": return_date},
            ],
            "passengers": [{"type": "adult"}],
            "cabin_class": "business",  # UPDATE 1: Set cabin search to Business Class
        }
    }

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        data = response.json()

        if response.status_code != 200 and response.status_code != 201:
            error_type = data.get("errors", [{}])[0].get("type", "unknown_error")
            error_msg = data.get("errors", [{}])[0].get("message", "Request failed")
            print(f"Error scanning {depart_date}: {error_type}: {error_msg}")

            cursor.execute(
                """
                INSERT INTO searches (route_name, origin, destination, depart_date, days_out, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (route_name, origin, destination, depart_date, 14, "error"),
            )
            conn.commit()
            return

        # Insert search log entry
        cursor.execute(
            """
            INSERT INTO searches (route_name, origin, destination, depart_date, days_out, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (route_name, origin, destination, depart_date, 14, "ok"),
        )
        search_id = cursor.lastrowid

        # Parse and save flight offers
        offers = data.get("data", {}).get("offers", [])
        for offer in offers:
            owner = offer.get("owner", {})
            airline_code = owner.get("iata_code", "XX")
            airline_name = owner.get("name", "Unknown Airline")
            total_amount = float(offer.get("total_amount", 0.0))
            currency = offer.get("total_currency", "GBP")

            # Count total stops on outward leg
            outbound_slices = offer.get("slices", [])[0] if offer.get("slices") else {}
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
                    "business",  # UPDATE 2: Store booking class as business
                    stops_out,
                    1,           # UPDATE 3: Flag all_business as 1 (True)
                ),
            )

        conn.commit()
        print(f"Successfully scanned {depart_date}: Found {len(offers)} Business class offers")

    except Exception as e:
        print(f"Error scanning {depart_date}: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main Execution Entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    print("Starting Business Class Scan...")

    start_date = datetime.strptime("2027-07-01", "%Y-%m-%d")
    for i in range(5):  # Adjust range for desired number of windows
        dep = (start_date + timedelta(days=i * 2)).strftime("%Y-%m-%d")
        ret = (start_date + timedelta(days=i * 2 + 14)).strftime("%Y-%m-%d")
        print(f"Searching Newcastle to San Francisco / Los Angeles to Newcastle (Business): {dep} to {ret}...")
        scan_window("NCL", "SFO", dep, ret)
        time.sleep(1)

    print("Business class scan completed successfully. Results logged to farewatch.db.")
