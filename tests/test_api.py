"""The HTTP service: auth, a run with approvals, a denial, records, reset."""

import time

import pytest
from fastapi.testclient import TestClient

import sawn_api

KEY = {"X-API-Key": "officer-dev-key"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = sawn_api.create_app(workdir=tmp_path_factory.mktemp("api"), offline=True, mcp="inproc",
                              trace_file=tmp_path_factory.mktemp("traces") / "t.jsonl")
    with TestClient(app) as c:
        yield c


def drive(client, run_id, approve=True, limit=200):
    seen = []
    for _ in range(limit):
        v = client.get(f"/api/runs/{run_id}", headers=KEY).json()
        if v["pending"]:
            seen.append(v["pending"])
            client.post(f"/api/runs/{run_id}/decision", json={"approve": approve}, headers=KEY)
        elif v["status"] in ("done", "error"):
            return v, seen
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_health_and_auth(client):
    h = client.get("/healthz").json()
    assert h["ok"] and h["offline"] and set(h["mcp_tools"]) == {"search_policies", "read_policy"}
    assert client.get("/api/inbox").status_code == 401
    assert client.get("/api/inbox", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/").status_code == 200


def test_run_with_approvals(client):
    run_id = client.post("/api/runs", json={"inbox_id": "REQ-001"}, headers=KEY).json()["run_id"]
    v, pending = drive(client, run_id)
    assert [p["tool"] for p in pending] == ["delete_data", "deliver_reply"]
    assert pending[0]["preview"]["plan"]["keep"][0]["keep_until"] == "2030-01-15"
    assert v["delivery"]["to"] == "fahad.harbi@mail.example" and v["delivery"]["status"] == "sent"
    out = client.get("/api/records/outbox", headers=KEY).json()
    assert out[0]["to_addr"] == "fahad.harbi@mail.example"
    inbox = {r["id"]: r for r in client.get("/api/inbox", headers=KEY).json()}
    assert inbox["REQ-001"]["status"] == "done" and inbox["REQ-001"]["run_id"] == run_id


def test_denial_changes_nothing(client):
    run_id = client.post("/api/runs", json={"inbox_id": "REQ-003"}, headers=KEY).json()["run_id"]
    v, pending = drive(client, run_id, approve=False)
    assert [p["tool"] for p in pending] == ["withdraw_consent"] and v["delivery"] is None
    consents = client.get("/api/records/customers", headers=KEY).json()
    assert next(c for c in consents if c["customer_id"] == "CU-1005")["status"] == "active"


def test_records_are_masked_and_reset_works(client):
    customers = client.get("/api/records/customers", headers=KEY).json()
    assert all(c["national_id"] is None or c["national_id"].startswith("******") for c in customers)
    assert client.get("/api/records/unknown", headers=KEY).status_code == 404
    assert client.post("/api/runs", json={}, headers=KEY).status_code == 422
    assert client.post("/api/reset", headers=KEY).json() == {"ok": True}
    assert client.get("/api/records/outbox", headers=KEY).json() == []
    assert client.get("/api/runs", headers=KEY).json() == []
