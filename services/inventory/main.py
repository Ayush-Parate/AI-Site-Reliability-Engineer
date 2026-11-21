"""Inventory microservice (port 8003) backed by SQLite."""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

# Ensure repo root is on sys.path for direct script execution
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.common.logging import get_current_trace_id
from services.common.service import create_service_app

SERVICE_NAME = "inventory"
PORT = 8003
DATA_DIR = REPO_ROOT / "data"
DB_PATH = Path(os.getenv("INVENTORY_DB_PATH", str(DATA_DIR / "inventory.db")))


def get_db_connection(timeout: float = 1.0) -> sqlite3.Connection:
    """Open an SQLite database connection with a strict timeout to support DB lock chaos."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize the schema and seed default items if empty."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_db_connection(timeout=5.0) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                item_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                stock INTEGER NOT NULL,
                price REAL NOT NULL
            );
            """
        )
        # Seed initial items if table is empty
        cursor = conn.execute("SELECT COUNT(*) FROM inventory;")
        count = cursor.fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO inventory (item_id, name, stock, price) VALUES (?, ?, ?, ?);",
                [
                    ("item_1", "Widget Alpha", 100000, 19.99),
                    ("item_2", "Gadget Beta", 100000, 49.99),
                    ("item_3", "Sensor Gamma", 100000, 9.99),
                ],
            )
            conn.commit()


@asynccontextmanager
async def lifespan(app_instance: Any):
    init_db()
    yield


app, metrics, chaos = create_service_app(
    service_name=SERVICE_NAME,
    port=PORT,
    description="Inventory microservice backed by SQLite",
)
app.router.lifespan_context = lifespan
logger = app.state.logger


class ReserveRequest(BaseModel):
    item_id: str = Field(..., description="ID of the inventory item to reserve")
    quantity: int = Field(default=1, ge=1, description="Quantity to reserve")


@app.get("/items", summary="List all inventory items")
async def list_items() -> list[dict[str, Any]]:
    try:
        with get_db_connection(timeout=2.0) as conn:
            cursor = conn.execute("SELECT item_id, name, stock, price FROM inventory;")
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError as exc:
        logger.error(f"Database query failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Database operational error: {exc}")


@app.get("/items/{item_id}", summary="Get item details by ID")
async def get_item(item_id: str) -> dict[str, Any]:
    try:
        with get_db_connection(timeout=2.0) as conn:
            cursor = conn.execute("SELECT item_id, name, stock, price FROM inventory WHERE item_id = ?;", (item_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
            return dict(row)
    except sqlite3.OperationalError as exc:
        logger.error(f"Database query failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Database operational error: {exc}")


@app.post("/reserve", summary="Reserve inventory for an order")
async def reserve_stock(req: ReserveRequest) -> dict[str, Any]:
    trace_id = get_current_trace_id()
    try:
        with get_db_connection(timeout=1.0) as conn:
            cursor = conn.execute(
                "SELECT item_id, name, stock, price FROM inventory WHERE item_id = ?;",
                (req.item_id,),
            )
            row = cursor.fetchone()
            if not row:
                logger.warning(f"Item not found: {req.item_id}")
                raise HTTPException(status_code=404, detail=f"Item {req.item_id} not found")

            current_stock = row["stock"]
            price = row["price"]
            name = row["name"]

            if current_stock < req.quantity:
                logger.warning(
                    f"Insufficient stock for {req.item_id}: available={current_stock}, requested={req.quantity}"
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient stock for {req.item_id}: available={current_stock}, requested={req.quantity}",
                )

            # Deduct stock inside transaction
            conn.execute(
                "UPDATE inventory SET stock = stock - ? WHERE item_id = ?;",
                (req.quantity, req.item_id),
            )
            conn.commit()

            logger.info(
                f"Reserved {req.quantity} of {req.item_id} ({name}) at ${price:.2f}"
            )
            return {
                "status": "reserved",
                "item_id": req.item_id,
                "name": name,
                "quantity": req.quantity,
                "price": price,
                "remaining_stock": current_stock - req.quantity,
                "trace_id": trace_id,
            }

    except sqlite3.OperationalError as exc:
        logger.error(f"SQLite operational error (database locked or timeout): {exc}")
        raise HTTPException(status_code=500, detail=f"Database locked / operational error: {exc}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
