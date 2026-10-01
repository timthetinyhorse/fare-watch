import datetime
import os
import sqlite3
import sys
import time
from duffel_api import Duffel
import yaml

# --- Load Configuration ---
CONFIG_FILE = "config.yaml"
if not os.path.exists(CONFIG_FILE):
    print(f"Error: Configuration file '{CONFIG_FILE}' not found.")
    sys.exit(1)

with open(CONFIG_FILE, "r") as f:
    config = yaml.safe_load(f)

# Initialize Duffel API Client
api_key = os.getenv("DUFFEL_API_KEY")
if not api_key:
    print("Error: DUFFEL_API_KEY environment variable is not set.")
    sys.exit(1)

duffel = Duffel(access_token=api_key)

# --- Database Setup ---
db_path = config["settings"].get("database", "farewatch.db")
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Ensure target tables exist
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    origin TEXT,
    destination TEXT,
    return_origin TEXT,
    return_destination TEXT
)
"""
)

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
)

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    route_id INTEGER,
    airline TEXT,
    total_amount REAL,
    currency TEXT,
    total_amount_gbp REAL,
    outbound_date TEXT,
    return_date TEXT,
    transfers INTEGER,
    FOREIGN KEY(scan_id) REFERENCES scans(id),
    FOREIGN KEY(route_id) REFERENCES routes(id)
)
"""
)

# Create/Update Unified View for Easy DB Reading
cursor.execute(
    """
CREATE VIEW IF NOT EXISTS v_full_fare_history AS
SELECT 
    scans.created_at AS scan_timestamp,
    routes.name AS route_name,
    routes.origin,
    routes.destination,
    routes.return_origin,
    routes.return_destination,
    offers.airline,
    offers.total_amount_gbp AS price_gbp,
    offers.outbound_date,
    offers.return_date,
    offers.transfers
FROM offers
JOIN scans ON offers.scan_id = scans.id
JOIN routes ON offers.route_id = routes.id
ORDER BY scans.created_at DESC;
"""
)
conn.commit()

# Record current scan session
cursor.execute("INSERT INTO scans DEFAULT VALUES")
scan_id = cursor.lastrowid
conn.commit()

# --- Date Range Generator (July & August) ---
search_months = config["settings"].get("search_months", ["2027-07", "2027-08"])
departure_dates = []

for month_str in search_months:
    year, month = map(int, month_str.split("-"))
    # Start from beginning of month or tomorrow if scanning current month
    start_day = 1
    today = datetime.date.today()
    if year == today.year and month == today.month:
        start_day = today.day + 1

    # Get number of days in target month
    if month == 12:
        next_month = datetime.date(year + 1, 1, 1)
    else:
        next_month = datetime.date(year, month + 1, 1)
    last_day = (next_month - datetime.timedelta(days=1)).day

    for day in range(start_day, last_day + 1, 2):  # Scan every 2 days to manage API rate limits
        departure_dates.append(datetime.date(year, month, day))

stay_days = config["settings"].get("return_stay_days", 14)
max_connections = config["settings"].get("max_connections", 1)
fx_rates = config.get("fx_to_gbp", {"GBP": 1.0, "USD": 0.78, "EUR": 0.85})

# --- Execute Scans ---
print(f"Starting Scan ID {scan_id} for {len(departure_dates)} target departure windows...")

for route in config["routes"]:
    # Upsert route entry
    cursor.execute(
        """
        INSERT INTO routes (name, origin, destination, return_origin, return_destination)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            origin=excluded.origin,
            destination=excluded.destination,
            return_origin=excluded.return_origin,
            return_destination=excluded.return_destination
    """,
        (
            route["name"],
            route["origin"],
            route["destination"],
            route.get("return_origin", route["destination"]),
            route.get("return_destination", route["origin"]),
        ),
    )
    conn.commit()

    cursor.execute("SELECT id FROM routes WHERE name = ?", (route["name"],))
    route_id = cursor.fetchone()[0]

    ret_origin = route.get("return_origin", route["destination"])
    ret_dest = route.get("return_destination", route["origin"])

    for dep_date in departure_dates:
        ret_date = dep_date + datetime.timedelta(days=stay_days)
        dep_str = dep_date.strftime("%Y-%m-%d")
        ret_str = ret_date.strftime("%Y-%m-%d")

        print(f"Searching {route['name']}: {dep_str} to {ret_str}...")

        try:
            # Send Open-Jaw Business Class Request to Duffel API
            offer_request = (
                duffel.offer_requests.create()
                .passenger([{"type": "adult"}])
                .cabin_class(config["settings"]["cabins"][0])
                .slices(
                    [
                        {
                            "origin": route["origin"],
                            "destination": route["destination"],
                            "departure_date": dep_str,
                        },
                        {
                            "origin": ret_origin,
                            "destination": ret_dest,
                            "departure_date": ret_str,
                        },
                    ]
                )
                .execute()
            )

            # Retrieve generated offers
            offers = duffel.offers.list(offer_request.id)

            for offer in offers:
                # Calculate maximum transfers/connections across both legs
                max_transfers = 0
                for s in offer.slices:
                    num_segments = len(s.segments)
                    transfers = num_segments - 1
                    if transfers > max_transfers:
                        max_transfers = transfers

                # Enforce max connection constraint
                if max_transfers > max_connections:
                    continue

                amount = float(offer.total_amount)
                currency = offer.total_currency
                gbp_rate = fx_rates.get(currency, 1.0)
                amount_gbp = round(amount * gbp_rate, 2)
                airline = offer.owner.name

                cursor.execute(
                    """
                    INSERT INTO offers 
                    (scan_id, route_id, airline, total_amount, currency, total_amount_gbp, outbound_date, return_date, transfers)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        scan_id,
                        route_id,
                        airline,
                        amount,
                        currency,
                        amount_gbp,
                        dep_str,
                        ret_str,
                        max_transfers,
                    ),
                )

            conn.commit()
            time.sleep(0.5)  # Respect API rate limits

        except Exception as e:
            print(f"Error scanning {dep_str}: {str(e)}")
            continue

conn.close()
print("Scan completed successfully. Results logged to farewatch.db.")
