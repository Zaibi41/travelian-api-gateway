"""Flight pricing rules (migration 0041) through the API: Super Admin only; create, list, edit, switch off and delete;
one rule per supplier, source and airline; a saved rule reaches the next search at once (the cache is cleared)."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.routes import flight_pricing as routes
from app.core.field_visibility import ALL, BackOfficeUser, require_super_admin
from app.services import flight_pricing_rules as pricing_rules, public_booking

ACTOR = "11111111-1111-4111-8111-111111111111"
ADMIN = BackOfficeUser(ACTOR, "super_admin", ALL)


def api(viewer=ADMIN):
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    if viewer is None:
        def deny():
            raise HTTPException(403, "Super Admin only.")
        app.dependency_overrides[require_super_admin] = deny
    else:
        app.dependency_overrides[require_super_admin] = lambda: viewer
    return TestClient(app)


@pytest.fixture
def db(database):
    connection, metadata, _ = database
    if not connection.execute(text("SELECT 1 FROM users WHERE id=:id"), {"id": ACTOR}).first():
        connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))
    pricing_rules.clear_cache()
    yield connection
    pricing_rules.clear_cache()


def test_create_list_edit_switch_off_and_delete(db):
    client = api()
    public_booking._flight_cache[("LHE", "KHI")] = (10**9, "cached search")
    public_booking._fares_by_date[("LHE", "KHI", "2026-12-05")] = (10**9, 1_000_000, "PKR")
    created = client.post("/v1/admin/flight-pricing/rules", json={"supplier": "sooperfare", "source": "pk", "airline_code": "pk", "amount_minor": -10_000})
    assert created.status_code == 201, created.text
    rule = created.json()
    assert (rule["supplier"], rule["source"], rule["airline_code"], rule["amount_minor"], rule["active"]) == ("sooperfare", "PK", "PK", -10_000, True)
    client.post("/v1/admin/flight-pricing/rules", json={"supplier": "southair", "amount_minor": 50_000})
    listed = client.get("/v1/admin/flight-pricing/rules").json()["rules"]
    assert public_booking._flight_cache == {} and public_booking._fares_by_date == {}  # the website's cached prices are dropped
    assert [(r["supplier"], r["amount_minor"]) for r in listed] == [("sooperfare", -10_000), ("southair", 50_000)]
    # The next search uses it at once.
    assert pricing_rules.adjustment("sooperfare", "PK", "PK", "PKR") == -10_000

    edited = client.put(f"/v1/admin/flight-pricing/rules/{rule['id']}", json={"supplier": "sooperfare", "source": "PK", "airline_code": "PK",
                                                                               "amount_minor": -20_000, "active": False})
    assert edited.status_code == 200 and edited.json()["amount_minor"] == -20_000 and edited.json()["active"] is False
    assert pricing_rules.adjustment("sooperfare", "PK", "PK", "PKR") == 0  # switched off
    assert db.scalar(text("SELECT updated_by_user_id FROM flight_pricing_rules WHERE id = :id"), {"id": rule["id"]}) == ACTOR

    deleted = client.delete(f"/v1/admin/flight-pricing/rules/{rule['id']}")
    assert deleted.status_code == 200 and deleted.json() == {"id": rule["id"], "deleted": True}
    assert client.delete(f"/v1/admin/flight-pricing/rules/{rule['id']}").status_code == 404
    assert [r["supplier"] for r in client.get("/v1/admin/flight-pricing/rules").json()["rules"]] == ["southair"]


def test_one_rule_per_supplier_source_and_airline(db):
    client = api()
    body = {"supplier": "sooperfare", "airline_code": "PK", "amount_minor": -10_000}
    assert client.post("/v1/admin/flight-pricing/rules", json=body).status_code == 201
    duplicate = client.post("/v1/admin/flight-pricing/rules", json={**body, "amount_minor": -5_000})
    assert duplicate.status_code == 409 and duplicate.headers["X-Error-Code"] == "duplicate_rule"
    # Another source, or any airline, is another rule.
    assert client.post("/v1/admin/flight-pricing/rules", json={**body, "source": "PK"}).status_code == 201
    assert client.post("/v1/admin/flight-pricing/rules", json={**body, "airline_code": None}).status_code == 201
    other = client.post("/v1/admin/flight-pricing/rules", json={"supplier": "kyte", "amount_minor": 10_000}).json()
    clash = client.put(f"/v1/admin/flight-pricing/rules/{other['id']}", json=body)
    assert clash.status_code == 409


@pytest.mark.parametrize("body", [
    {"supplier": "southair", "amount_minor": 0},
    {"supplier": "southair", "amount_minor": -10_050},
    {"supplier": "southair", "source": "PK", "amount_minor": -10_000},
    {"supplier": "nope", "amount_minor": -10_000},
])
def test_invalid_rules_are_refused_and_nothing_is_saved(db, body):
    assert api().post("/v1/admin/flight-pricing/rules", json=body).status_code == 422
    assert db.scalar(text("SELECT count(*) FROM flight_pricing_rules")) == 0


def test_super_admin_only(db):
    assert api(None).get("/v1/admin/flight-pricing/rules").status_code == 403
    assert api(None).get("/v1/admin/flight-pricing/suppliers").status_code == 403
    assert api(None).post("/v1/admin/flight-pricing/rules", json={"supplier": "southair", "amount_minor": -10_000}).status_code == 403
    assert db.scalar(text("SELECT count(*) FROM flight_pricing_rules")) == 0


def test_suppliers_list_names_sooperfares_sources(db):
    suppliers = api().get("/v1/admin/flight-pricing/suppliers").json()["suppliers"]
    assert [s["code"] for s in suppliers] == ["southair", "sooperfare", "kyte"]
    sooperfare = suppliers[1]
    assert {s["code"] for s in sooperfare["sources"]} == {"PK", "PA", "PF", "ONEAPI", "Z8", "SABRE"}
    assert suppliers[0]["sources"] == [] and suppliers[2]["sources"] == []
