import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
import requests

DUFFEL_API_KEY = os.getenv("DUFFEL_API_KEY", "duffel_test_YOUR_API_KEY")
DB_PATH = "fares.db"
OUTPUT_HTML = "docs/index.html"


def init_db():
    """Reset and create database schema for individual leg tracking."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            leg_type TEXT,
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
            stops INTEGER,
            FOREIGN KEY (search_id) REFERENCES searches(id)
        )
    """
    )

    conn.commit()
    conn.close()


def fetch_one_way_leg(
    origin, destination, depart_date, leg_type="outbound", top_limit=50
):
    """Fetch one-way offers from Duffel API and store in SQLite."""
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
            print(
                f"Error scanning {leg_type} {origin}->{destination} on {depart_date}: {data.get('errors')}"
            )
            return

        offers = data.get("data", {}).get("offers", [])
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
                INSERT INTO offers (search_id, airline_code, airline_name, total_amount, currency, stops)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    search_id,
                    airline_code,
                    airline_name,
                    total_amount,
                    currency,
                    stops,
                ),
            )

        conn.commit()
        print(
            f"Saved {len(sorted_offers)} offers for {leg_type.upper()} {origin}->{destination} ({depart_date})"
        )

    except Exception as e:
        print(f"Exception during {origin}->{destination}: {e}")
    finally:
        conn.close()


def generate_html_report():
    """Query paired legs from SQLite and build docs/index.html."""
    os.makedirs("docs", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Match outbound + return legs by airline to build total trip price
    cursor.execute(
        """
        SELECT 
            s_out.depart_date AS outbound_date,
            s_in.depart_date AS return_date,
            o_out.airline_name,
            o_out.airline_code,
            ROUND(o_out.total_amount + o_in.total_amount, 2) AS total_price,
            o_out.currency,
            o_out.stops AS out_stops,
            o_in.stops AS in_stops
        FROM offers o_out
        JOIN searches s_out ON o_out.search_id = s_out.id AND s_out.leg_type = 'outbound'
        JOIN searches s_in ON s_in.leg_type = 'return' 
            AND s_in.depart_date = DATE(s_out.depart_date, '+14 days')
        JOIN offers o_in ON o_in.search_id = s_in.id 
            AND o_in.airline_code = o_out.airline_code
        ORDER BY total_price ASC
        LIMIT 250
    """
    )
    rows = cursor.fetchall()
    conn.close()

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Fare Watch - Business Class Scan</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 40px; background: #f4f6f8; color: #333; }}
        h1 {{ color: #111; }}
        .meta {{ margin-bottom: 20px; color: #666; font-size: 0.9em; }}
        table {{ width: 100%; border-collapse: collapse; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }}
        th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #0066cc; color: #fff; font-weight: 600; }}
        tr:hover {{ background: #f9fbfd; }}
        .price {{ font-weight: bold; color: #2e7d32; }}
        .badge {{ background: #e0e0e0; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; }}
    </style>
</head>
<body>
    <h1>Fare Watch: Business Class Deals</h1>
    <div class="meta">Last Scanned: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} | Route: NCL &#8594; SFO / LAX &#8594; NCL</div>
    <table>
        <thead>
            <tr>
                <th>Airline</th>
                <th>Outbound (NCL-SFO)</th>
                <th>Return (LAX-NCL)</th>
                <th>Stops (Out / Return)</th>
                <th>Combined Total</th>
            </tr>
        </thead>
        <tbody>
"""

    for row in rows:
        out_date, in_date, airline, code, price, currency, out_stops, in_stops = (
            row
        )
        html_content += f"""
            <tr>
                <td><strong>{airline}</strong> <span class="badge">{code}</span></td>
                <td>{out_date}</td>
                <td>{in_date}</td>
                <td>{out_stops} stop(s) / {in_stops} stop(s)</td>
                <td class="price">{currency} {price:,.2f}</td>
            </tr>"""

    html_content += """
        </tbody>
    </table>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Successfully generated {OUTPUT_HTML}")


if __name__ == "__main__":
    init_db()
    print("Starting one-way leg scan for open-jaw route...")

    start_date = datetime.strptime("2027-07-01", "%Y-%m-%d")
    end_date = datetime.strptime("2027-08-31", "%Y-%m-%d")

    current_dep = start_date
    while current_dep <= end_date:
        outbound_date = current_dep.strftime("%Y-%m-%d")
        return_date = (current_dep + timedelta(days=14)).strftime("%Y-%m-%d")

        # Fetch individual legs
        fetch_one_way_leg(
            "NCL", "SFO", outbound_date, leg_type="outbound", top_limit=50
        )
        time.sleep(0.3)
        fetch_one_way_leg(
            "LAX", "NCL", return_date, leg_type="return", top_limit=50
        )
        time.sleep(0.3)

        current_dep += timedelta(days=3)

    generate_html_report()
