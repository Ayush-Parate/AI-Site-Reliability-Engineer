"""Payments microservice (port 8002).

Supports simulating a bad deployment when version is bumped to 2.x (v2).
"""

from __future__ import annotations

import datetime
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

# Ensure repo root is on sys.path for direct script execution
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.common.logging import get_current_trace_id
from services.common.service import create_service_app, get_service_version

SERVICE_NAME = "payments"
PORT = 8002

app, metrics, chaos = create_service_app(
    service_name=SERVICE_NAME,
    port=PORT,
    description="Payments processing microservice",
)
logger = app.state.logger


class ChargeRequest(BaseModel):
    order_id: str = Field(..., description="ID of the order being paid")
    amount: float = Field(..., gt=0.0, description="Amount to charge")
    customer_id: str = Field(default="cust_default", description="Customer ID")


def is_v2_buggy() -> bool:
    """Check if the service version represents the buggy v2 deploy."""
    v = get_service_version(SERVICE_NAME).strip().lower()
    return v.startswith("2") or v.startswith("v2")


@app.post("/charge", summary="Process a customer payment")
@app.post("/payments", summary="Process a customer payment (alias)")
async def charge_payment(req: ChargeRequest) -> dict[str, Any]:
    trace_id = get_current_trace_id()
    current_version = get_service_version(SERVICE_NAME)

    # Bad deploy simulation: v2 contains a breaking bug in payment gateway auth
    if is_v2_buggy():
        logger.error(
            f"v2 Payment connector failure for order {req.order_id}: upstream token authentication failed"
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Payment connector v2 authentication failed with upstream provider",
                "service": SERVICE_NAME,
                "version": current_version,
                "order_id": req.order_id,
            },
        )

    payment_id = f"pay_{uuid.uuid4().hex[:12]}"
    now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    logger.info(
        f"Successfully charged ${req.amount:.2f} for order {req.order_id} (payment_id={payment_id})"
    )

    return {
        "status": "charged",
        "payment_id": payment_id,
        "order_id": req.order_id,
        "amount": req.amount,
        "customer_id": req.customer_id,
        "timestamp": now_ts,
        "trace_id": trace_id,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
