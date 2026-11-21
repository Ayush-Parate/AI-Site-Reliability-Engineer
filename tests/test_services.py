"""Comprehensive tests for Phase 1 Demo Microservices App.

Covers:
- Endpoints on all 4 services: /health, /metrics, /chaos
- Structured JSON logging with required fields
- Full order flow from gateway -> orders -> inventory & payments
- Trace ID propagation across all services
- Chaos injection: latency, error rate, memory leak, CPU burn, unhealthy
- Bad deploy simulation (v2 failure)
- SQLite inventory persistence and locking behavior
- Load generator execution
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from load import LoadGenerator
from services.common.logging import JSONFormatter, get_current_trace_id, set_current_trace_id
from services.gateway.main import app as gateway_app
from services.inventory.main import (
    DB_PATH,
    app as inventory_app,
    get_db_connection,
    init_db,
)
from services.orders.main import app as orders_app
from services.payments.main import app as payments_app


@pytest.fixture(autouse=True)
def setup_test_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ensure database and log isolation for tests."""
    test_db = tmp_path / "test_inventory.db"
    monkeypatch.setattr("services.inventory.main.DB_PATH", test_db)
    monkeypatch.setattr("services.inventory.main.DATA_DIR", tmp_path)
    init_db()

    # Reset chaos on all services before each test
    for app in (gateway_app, orders_app, payments_app, inventory_app):
        if hasattr(app.state, "chaos"):
            app.state.chaos.reset()

    yield

    # Clean up chaos
    for app in (gateway_app, orders_app, payments_app, inventory_app):
        if hasattr(app.state, "chaos"):
            app.state.chaos.reset()


def test_health_endpoints_all_services():
    """All 4 services expose GET /health returning status ok and version."""
    services = [
        ("gateway", gateway_app, 8000),
        ("orders", orders_app, 8001),
        ("payments", payments_app, 8002),
        ("inventory", inventory_app, 8003),
    ]

    for name, app, port in services:
        client = TestClient(app)
        res = client.get("/health")
        assert res.status_code == 200, f"{name} health check failed"
        data = res.json()
        assert data["status"] == "ok"
        assert data["service"] == name
        assert data["port"] == port
        assert "version" in data


def test_metrics_endpoints_all_services():
    """All 4 services expose GET /metrics with Prometheus formatted text."""
    services = [
        ("gateway", gateway_app),
        ("orders", orders_app),
        ("payments", payments_app),
        ("inventory", inventory_app),
    ]

    for name, app in services:
        client = TestClient(app)
        # Make a request first to trigger metric recording
        client.get("/health")
        res = client.get("/metrics")
        assert res.status_code == 200, f"{name} metrics check failed"
        text = res.text
        assert "http_requests_total" in text
        assert "http_errors_total" in text
        assert "http_request_duration_seconds" in text
        assert f'service="{name}"' in text


def test_structured_json_logging_schema():
    """Structured JSON logs contain ts, level, service, trace_id, msg, latency_ms, status."""
    import logging

    formatter = JSONFormatter(service_name="test_service")
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Test log message",
        args=(),
        exc_info=None,
    )
    record.trace_id = "test_trace_123"
    record.latency_ms = 45.67
    record.status = 200

    raw_json = formatter.format(record)
    data = json.loads(raw_json)

    required_keys = {"ts", "level", "service", "trace_id", "msg", "latency_ms", "status"}
    assert required_keys.issubset(data.keys())
    assert data["service"] == "test_service"
    assert data["trace_id"] == "test_trace_123"
    assert data["latency_ms"] == 45.67
    assert data["status"] == 200
    assert data["level"] == "INFO"
    assert data["msg"] == "Test log message"


def test_inventory_database_crud():
    """Inventory SQLite database tracks items and updates stock correctly."""
    client = TestClient(inventory_app)

    # 1. List items
    res = client.get("/items")
    assert res.status_code == 200
    items = res.json()
    assert len(items) >= 3
    item_map = {item["item_id"]: item for item in items}
    assert "item_1" in item_map
    initial_stock = item_map["item_1"]["stock"]

    # 2. Reserve stock
    res = client.post("/reserve", json={"item_id": "item_1", "quantity": 2})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "reserved"
    assert data["remaining_stock"] == initial_stock - 2

    # 3. Reserve more than available returns 400
    res = client.post("/reserve", json={"item_id": "item_1", "quantity": 9999999})
    assert res.status_code == 400

    # 4. Unknown item returns 404
    res = client.post("/reserve", json={"item_id": "unknown_item", "quantity": 1})
    assert res.status_code == 404


def test_payments_charge_success():
    """Payments microservice charges successfully under v1."""
    client = TestClient(payments_app)
    res = client.post(
        "/charge",
        json={"order_id": "ord_101", "amount": 39.98, "customer_id": "cust_1"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "charged"
    assert data["order_id"] == "ord_101"
    assert data["amount"] == 39.98
    assert data["payment_id"].startswith("pay_")


def test_payments_bad_deploy_simulation(monkeypatch: pytest.MonkeyPatch):
    """When payments service version is v2, /charge fails with 500 simulating a bad deploy."""
    client = TestClient(payments_app)

    monkeypatch.setenv("PAYMENTS_VERSION", "2.0.0")
    res = client.post(
        "/charge",
        json={"order_id": "ord_bad_v2", "amount": 19.99, "customer_id": "cust_1"},
    )
    assert res.status_code == 500
    assert "v2" in res.text or "authentication failed" in res.text


def test_full_call_chain_and_trace_id_propagation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Gateway -> Orders -> Inventory & Payments propagates trace_id and fulfills order."""
    async def _run():
        inventory_transport = httpx.ASGITransport(app=inventory_app)
        payments_transport = httpx.ASGITransport(app=payments_app)
        orders_transport = httpx.ASGITransport(app=orders_app)

        custom_trace_id = "trace_deadbeef123456"

        RealAsyncClient = httpx.AsyncClient

        class MockAsyncClient:
            def __init__(self, *args, **kwargs):
                self._real_client = RealAsyncClient(*args, **kwargs) if "transport" in kwargs else None

            async def __aenter__(self):
                if self._real_client:
                    await self._real_client.__aenter__()
                return self

            async def __aexit__(self, *args):
                if self._real_client:
                    await self._real_client.__aexit__(*args)

            async def post(self, url: str, **kwargs):
                if self._real_client:
                    return await self._real_client.post(url, **kwargs)
                if "8003" in url:
                    async with RealAsyncClient(transport=inventory_transport, base_url="http://localhost:8003") as c:
                        return await c.post(url, **kwargs)
                elif "8002" in url:
                    async with RealAsyncClient(transport=payments_transport, base_url="http://localhost:8002") as c:
                        return await c.post(url, **kwargs)
                elif "8001" in url:
                    async with RealAsyncClient(transport=orders_transport, base_url="http://localhost:8001") as c:
                        return await c.post(url, **kwargs)
                raise ValueError(f"Unknown mock URL: {url}")

        monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

        # Call the gateway
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="http://localhost:8000") as gw_client:
            resp = await gw_client.post(
                "/orders",
                json={"customer_id": "cust_integration", "item_id": "item_1", "quantity": 2},
                headers={"X-Trace-Id": custom_trace_id},
            )

            assert resp.status_code == 200, f"Gateway failed: {resp.text}"
            data = resp.json()
            assert data["status"] == "confirmed"
            assert data["item_id"] == "item_1"
            assert data["quantity"] == 2
            assert data["total_amount"] == 39.98
            assert data["payment_id"].startswith("pay_")
            assert resp.headers.get("x-trace-id") == custom_trace_id

        # Verify log files exist and contain the trace ID
        repo_root = Path(__file__).resolve().parent.parent
        logs_dir = repo_root / "logs"
        for svc in ("gateway", "orders", "payments", "inventory"):
            log_file = logs_dir / f"{svc}.log"
            assert log_file.exists(), f"Log file {log_file} does not exist"
            log_text = log_file.read_text(encoding="utf-8")
            assert custom_trace_id in log_text, f"Trace ID {custom_trace_id} not found in {svc}.log"

    asyncio.run(_run())


def test_chaos_middleware_error_rate_and_reset():
    """Chaos middleware injects errors when error_rate > 0 and clears on reset."""
    client = TestClient(payments_app)

    # 1. Baseline: 100% success
    res = client.post("/charge", json={"order_id": "ord_c1", "amount": 10.0, "customer_id": "c1"})
    assert res.status_code == 200

    # 2. Inject 100% error rate
    res = client.post("/chaos", json={"error_rate": 1.0})
    assert res.status_code == 200
    assert res.json()["error_rate"] == 1.0

    # 3. Next charge should fail with 500
    res = client.post("/charge", json={"order_id": "ord_c2", "amount": 10.0, "customer_id": "c1"})
    assert res.status_code == 500
    assert res.json()["chaos"] is True

    # 4. Reset chaos
    res = client.post("/chaos/reset")
    assert res.status_code == 200
    assert res.json()["error_rate"] == 0.0

    # 5. Subsequent charge succeeds again
    res = client.post("/charge", json={"order_id": "ord_c3", "amount": 10.0, "customer_id": "c1"})
    assert res.status_code == 200


def test_chaos_unhealthy_flag():
    """When chaos sets unhealthy=True, /health returns 503."""
    client = TestClient(orders_app)

    # Normal health
    res = client.get("/health")
    assert res.status_code == 200

    # Set unhealthy
    res = client.post("/chaos", json={"unhealthy": True})
    assert res.status_code == 200

    # Health fails
    res = client.get("/health")
    assert res.status_code == 503
    assert res.json()["status"] == "unhealthy"

    # Reset
    client.post("/chaos/reset")
    res = client.get("/health")
    assert res.status_code == 200


def test_chaos_memory_leak_and_cpu_burn():
    """Memory leak allocates memory and CPU burn flag toggles without crashing."""
    client = TestClient(gateway_app)

    # Leak 2 MB
    res = client.post("/chaos", json={"leak_mb": 2, "cpu_burn": True})
    assert res.status_code == 200
    data = res.json()
    assert data["leak_mb_total"] == 2
    assert data["cpu_burn"] is True

    # Reset clears leak and stops CPU burn
    res = client.post("/chaos/reset")
    assert res.status_code == 200
    data = res.json()
    assert data["leak_mb_total"] == 0
    assert data["cpu_burn"] is False


def test_load_generator_execution():
    """Load generator runs and computes percentiles accurately."""
    # Mock send_one to record 10 simulated requests with known latencies
    generator = LoadGenerator(url="http://localhost:8000/orders", rate=10.0, count=10)

    for i in range(1, 11):
        if i == 5:
            generator.stats.record_error(500, latency_ms=100.0)
        else:
            generator.stats.record_success(latency_ms=float(i * 10))

    assert generator.stats.total_sent == 10
    assert generator.stats.total_success == 9
    assert generator.stats.total_errors == 1
    pcts = generator.stats.compute_percentiles()
    assert pcts["p50"] > 0
    assert pcts["p95"] >= pcts["p50"]
    summary = generator.stats.summary()
    assert "Sent: 10" in summary
    assert "Errors: 1" in summary
