"""
The actual entry point: run this once at start-of-day, after you've pulled
data from Tiger. It fetches the GEX read from Tiger, loads the EOD flow you
uploaded the previous evening, assembles the reasoning input, calls the
reasoning layer, validates the output, and stores + returns it for the
render layer (dashboard/PDF) to pick up.

Trigger this from whatever your webapp uses for scheduling/manual-run
(a button, a cron job, etc.) — this file itself is framework-agnostic.
"""
from __future__ import annotations
import os
import sqlite3
from datetime import date, timedelta

from schema import ReasoningInput, MacroEvent, ReasoningOutput
import tiger_fetch
import flow_store
from reasoning_layer import run_reasoning
from render_pdf import generate_pdf

DB_PATH = "coffee_and_tea.db"
PDF_OUTPUT_DIR = "generated_pdfs"  # TODO: point this at wherever your webapp serves files from


def init_snapshot_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gex_snapshots (
            session_date TEXT PRIMARY KEY,
            gex_data_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reasoning_outputs (
            session_date TEXT PRIMARY KEY,
            output_json TEXT
        )
    """)
    conn.commit()


def get_prior_snapshot(conn: sqlite3.Connection, before_date: str):
    row = conn.execute(
        "SELECT gex_data_json FROM gex_snapshots WHERE session_date < ? ORDER BY session_date DESC LIMIT 1",
        (before_date,),
    ).fetchone()
    if not row:
        return None
    from schema import GexData
    return GexData.model_validate_json(row[0])


def save_snapshot(conn: sqlite3.Connection, session_date: str, gex_data):
    conn.execute(
        "INSERT OR REPLACE INTO gex_snapshots (session_date, gex_data_json) VALUES (?, ?)",
        (session_date, gex_data.model_dump_json()),
    )
    conn.commit()


def save_output(conn: sqlite3.Connection, session_date: str, output: ReasoningOutput):
    conn.execute(
        "INSERT OR REPLACE INTO reasoning_outputs (session_date, output_json) VALUES (?, ?)",
        (session_date, output.model_dump_json()),
    )
    conn.commit()


def fetch_macro_events_this_window(expiry: str) -> list[MacroEvent]:
    """
    TODO: wire this to an actual macro calendar source (an API, or a table
    you maintain from the weekly macro compilation). Hardcoded example
    below matches what we've been using manually in chat.
    """
    return [
        MacroEvent(
            event="FOMC decision",
            date="2026-09-16",
            time_et="14:00",
            detail="Rate decision + press conference 14:30 ET",
        )
    ]


def run_session(expiry: str, portfolio_size_usd: float) -> tuple[ReasoningOutput, str]:
    """Returns (validated output, path to the rendered PDF) — the PDF path
    is what your webapp should serve/link to, not the raw JSON."""
    conn = sqlite3.connect(DB_PATH)
    init_snapshot_table(conn)
    flow_store.init_db(conn)

    today = date.today().isoformat()

    # 1. Fetch live GEX data from Tiger
    gex_data = tiger_fetch.build_gex_data(expiry)
    save_snapshot(conn, today, gex_data)

    # 2. Load the most recently uploaded EOD flow (yesterday's, typically)
    flow_date = flow_store.get_latest_flow_date(conn)
    flow_data = flow_store.summarize_flow(flow_date, conn) if flow_date else None
    # Fail loudly if flow was expected but nothing was found — silent None
    # is exactly what produced the "no flow section" bug in the webapp output.
    if flow_date is None:
        print("WARNING: no EOD flow data found in flow_trades table — "
              "session will run without EOD Flow Context. Confirm the CSV "
              "upload actually called flow_store.save_flow_csv().")

    # 3. Prior day's GEX snapshot, for day-over-day comparison
    prior_snapshot = get_prior_snapshot(conn, today)

    # 4. Macro calendar for this expiry window
    macro_events = fetch_macro_events_this_window(expiry)
    if not macro_events:
        print(f"WARNING: no macro events returned for window ending {expiry} — "
              f"verify fetch_macro_events_this_window() date-range logic before trusting this session.")

    # 5. Assemble the reasoning input
    reasoning_input = ReasoningInput(
        session_date=today,
        expiration=expiry,
        gex_data=gex_data,
        flow_data=flow_data,
        chain_data_available=True,  # since we fetched live from Tiger
        prior_snapshot=prior_snapshot,
        portfolio_size_usd=portfolio_size_usd,
        macro_events_this_window=macro_events,
    )

    # 6. Run the reasoning layer (Anthropic API call, schema-validated)
    output = run_reasoning(reasoning_input)

    # 7. Persist for reference/debugging
    save_output(conn, today, output)

    # 8. Render the styled PDF — this is the step that was previously missing
    os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)
    pdf_path = os.path.join(PDF_OUTPUT_DIR, f"SPY_Coffee_and_Tea_{today}.pdf")
    generate_pdf(
        output,
        session_date=today,
        expiration=expiry,
        portfolio_size=portfolio_size_usd,
        spot=gex_data.spot,
        out_path=pdf_path,
    )

    conn.close()
    return output, pdf_path


if __name__ == "__main__":
    # Manual run example — replace with your actual target expiry
    result, pdf_path = run_session(expiry="2026-09-18", portfolio_size_usd=30000)
    print(f"PDF written to: {pdf_path}")
    print(result.model_dump_json(indent=2))
