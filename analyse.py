import argparse
import os
import sqlite3
import pandas as pd


def migrate_and_connect(db_path="fares.db"):
    """Connects to SQLite database and ensures required columns exist."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(offers)")
    columns = [row[1] for row in cursor.fetchall()]

    required_columns = {
        "airline_code": "TEXT DEFAULT 'XX'",
        "airline_name": "TEXT DEFAULT 'Unknown'",
        "booking_class": "TEXT DEFAULT 'business'",
        "stops_out": "INTEGER DEFAULT 0",
        "all_business": "INTEGER DEFAULT 1",
    }

    for col_name, col_type in required_columns.items():
        if col_name not in columns and len(columns) > 0:
            cursor.execute(f"ALTER TABLE offers ADD COLUMN {col_name} {col_type}")

    conn.commit()
    return conn


def load_raw_offers(db_path="fares.db"):
    """Fetches every individual offer from the database without aggregation."""
    conn = migrate_and_connect(db_path)

    query = """
        SELECT 
            s.depart_date AS "Outbound Date",
            s.origin AS "Origin",
            s.destination AS "Destination",
            o.airline_name AS "Airline",
            o.airline_code AS "Code",
            o.total_amount AS "Price",
            o.currency AS "Currency",
            o.booking_class AS "Cabin",
            o.stops_out AS "Stops",
            s.scanned_at AS "Scanned At"
        FROM offers o 
        JOIN searches s ON s.id = o.search_id
        WHERE s.status = 'ok'
        ORDER BY o.total_amount ASC
    """

    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def generate_html_report(df, output_path="docs/index.html"):
    """Outputs a complete deal-by-deal table to index.html."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    if df.empty:
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Business Class Deals</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 40px; background: #f9f9f9; color: #333; }
                .card { background: white; padding: 24px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Business Class Flight Deals</h1>
                <p>No valid offers found in <code>fares.db</code> yet. Run <code>python scanner.py</code> to fetch latest fares.</p>
            </div>
        </body>
        </html>
        """
    else:
        # Format prices to two decimal places
        df["Price"] = df.apply(lambda row: f"{row['Price']:.2f} {row['Currency']}", axis=1)
        df_display = df.drop(columns=["Currency"])

        table_html = df_display.to_html(index=False, classes="deals-table")

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Full Business Class Deal List</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                    margin: 0;
                    padding: 30px;
                    background-color: #f4f6f8;
                    color: #1a1a1a;
                }}
                .container {{
                    max-width: 1200px;
                    margin: 0 auto;
                    background: #ffffff;
                    padding: 30px;
                    border-radius: 10px;
                    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
                }}
                h1 {{
                    margin-top: 0;
                    color: #0f172a;
                    font-size: 28px;
                }}
                .meta {{
                    color: #64748b;
                    margin-bottom: 24px;
                    font-size: 14px;
                }}
                table.deals-table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 10px;
                    font-size: 15px;
                }}
                table.deals-table th {{
                    background-color: #0f172a;
                    color: #ffffff;
                    text-align: left;
                    padding: 12px 16px;
                    font-weight: 600;
                }}
                table.deals-table td {{
                    padding: 12px 16px;
                    border-bottom: 1px solid #e2e8f0;
                }}
                table.deals-table tr:hover {{
                    background-color: #f8fafc;
                }}
                table.deals-table tr:nth-child(even) {{
                    background-color: #f1f5f9;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Business Class Deals Found</h1>
                <div class="meta">
                    <strong>Total deals cataloged:</strong> {len(df)} | Sorted by price (lowest first)
                </div>
                {table_html}
            </div>
        </body>
        </html>
        """

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Report written to {output_path} with {len(df)} raw deals.")


def main():
    parser = argparse.ArgumentParser(description="Generate raw deal list from fares.db.")
    parser.add_argument("--out", default="docs/index.html", help="Output path for HTML report")
    args = parser.parse_args()

    df = load_raw_offers()
    generate_html_report(df, args.out)


if __name__ == "__main__":
    main()
