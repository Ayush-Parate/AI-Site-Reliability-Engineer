"""API Gateway microservice (port 8000).

Public-facing entry point that routes orders to the orders microservice (:8001),
initializes trace IDs, and propagates them downstream.
"""

from __future__ import annotations

import os
import sys
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

SERVICE_NAME = "gateway"
PORT = 8000

ORDERS_URL = os.getenv("ORDERS_URL", "http://localhost:8001").rstrip("/")

app, metrics, chaos = create_service_app(
    service_name=SERVICE_NAME,
    port=PORT,
    description="Public API Gateway microservice",
)
logger = app.state.logger


class GatewayOrderRequest(BaseModel):
    customer_id: str = Field(default="cust_default", description="Customer identifier")
    item_id: str = Field(default="item_1", description="Item identifier to purchase")
    quantity: int = Field(default=1, ge=1, description="Quantity to purchase")


@app.post("/orders", summary="Submit a new order via the gateway")
@app.post("/order", summary="Submit a new order via the gateway (alias)")
async def submit_order(req: GatewayOrderRequest) -> dict[str, Any]:
    trace_id = get_current_trace_id()
    headers = {"X-Trace-Id": trace_id}

    logger.info(f"Gateway received order request for {req.quantity}x {req.item_id}")

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"{ORDERS_URL}/orders",
                json={
                    "customer_id": req.customer_id,
                    "item_id": req.item_id,
                    "quantity": req.quantity,
                },
                headers=headers,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.error(f"Downstream orders service unreachable at {ORDERS_URL}: {exc}")
            raise HTTPException(
                status_code=503,
                detail=f"Downstream orders service unreachable: {exc}",
            )

        if resp.status_code != 200:
            logger.error(
                f"Downstream orders service returned error {resp.status_code}: {resp.text}"
            )
            status_code = resp.status_code if resp.status_code < 500 else 502
            raise HTTPException(
                status_code=status_code,
                detail=f"Orders service error: {resp.text}",
            )

        order_data = resp.json()

    logger.info(
        f"Gateway successfully completed order {order_data.get('order_id')} (status: {order_data.get('status')})"
    )
    return order_data


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
