"""Orders microservice (port 8001).

Coordinates order processing by calling inventory (:8003) and payments (:8002),
propagating trace IDs downstream.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

# Ensure repo root is on sys.path for direct script execution
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.common.logging import get_current_trace_id
from services.common.service import create_service_app

SERVICE_NAME = "orders"
PORT = 8001

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8003").rstrip("/")
PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://localhost:8002").rstrip("/")

app, metrics, chaos = create_service_app(
    service_name=SERVICE_NAME,
    port=PORT,
    description="Orders coordination microservice",
)
logger = app.state.logger


class CreateOrderRequest(BaseModel):
    customer_id: str = Field(default="cust_default", description="Customer ID")
    item_id: str = Field(default="item_1", description="Item ID to purchase")
    quantity: int = Field(default=1, ge=1, description="Quantity to purchase")


@app.post("/orders", summary="Create and fulfill an order")
@app.post("/order", summary="Create and fulfill an order (alias)")
async def create_order(req: CreateOrderRequest) -> dict[str, Any]:
    trace_id = get_current_trace_id()
    order_id = f"ord_{uuid.uuid4().hex[:12]}"
    headers = {"X-Trace-Id": trace_id}

    logger.info(f"Starting order processing {order_id} for {req.quantity}x {req.item_id}")

    # 1. Step 1: Reserve inventory
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            inv_resp = await client.post(
                f"{INVENTORY_URL}/reserve",
                json={"item_id": req.item_id, "quantity": req.quantity},
                headers=headers,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.error(f"Downstream inventory service unreachable at {INVENTORY_URL}: {exc}")
            raise HTTPException(
                status_code=503,
                detail=f"Downstream inventory service unreachable: {exc}",
            )

        if inv_resp.status_code != 200:
            logger.error(
                f"Inventory reservation failed for {req.item_id} (status {inv_resp.status_code}): {inv_resp.text}"
            )
            # Forward appropriate 4xx or 502 status
            status_code = inv_resp.status_code if inv_resp.status_code < 500 else 502
            raise HTTPException(
                status_code=status_code,
                detail=f"Inventory error: {inv_resp.text}",
            )

        inv_data = inv_resp.json()
        item_price = float(inv_data.get("price", 19.99))
        total_amount = round(item_price * req.quantity, 2)

        # 2. Step 2: Process payment
        try:
            pay_resp = await client.post(
                f"{PAYMENTS_URL}/charge",
                json={
                    "order_id": order_id,
                    "amount": total_amount,
                    "customer_id": req.customer_id,
                },
                headers=headers,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.error(f"Downstream payments service unreachable at {PAYMENTS_URL}: {exc}")
            raise HTTPException(
                status_code=503,
                detail=f"Downstream payments service unreachable: {exc}",
            )

        if pay_resp.status_code != 200:
            logger.error(
                f"Payment processing failed for order {order_id} (status {pay_resp.status_code}): {pay_resp.text}"
            )
            status_code = pay_resp.status_code if pay_resp.status_code < 500 else 502
            raise HTTPException(
                status_code=status_code,
                detail=f"Payments error: {pay_resp.text}",
            )

        pay_data = pay_resp.json()
        payment_id = pay_data.get("payment_id")

    logger.info(
        f"Order {order_id} completed successfully (payment_id={payment_id}, total=${total_amount:.2f})"
    )

    return {
        "order_id": order_id,
        "status": "confirmed",
        "customer_id": req.customer_id,
        "item_id": req.item_id,
        "quantity": req.quantity,
        "total_amount": total_amount,
        "payment_id": payment_id,
        "trace_id": trace_id,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
