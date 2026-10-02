"""Module 3 tests (offline, no API key). Run from module_2/:  python test_module3.py"""
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ["USERS_DB_FILE"] = str(Path(_tmp) / "users.json")
os.environ["SECRET_KEY"] = "test-secret-key-for-unit-tests-only"

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import gemini_utils as gu  # noqa: E402
import main  # noqa: E402
from gemini_client import GeminiError  # noqa: E402


class FakeService:
    last_model_used = "fake-model"

    def __init__(self, payload=None, error=None):
        self.payload, self.error = payload, error

    def generate_json(self, prompt, image=None, **kw):
        if self.error:
            raise self.error
        return self.payload


HOME = {"budget_breakdown": [{"category": "lighting", "allocation": 1500, "items": [
    {"name": "LED Bulb", "estimated_price": 100, "quantity": 5, "search_terms": "led bulb"}]}]}
PARTY = {"budget_breakdown": [{"category": "catering", "allocation": 3000, "items": [
    {"name": "Buffet", "estimated_price": 100, "quantity": 20, "search_terms": "party catering"}]}],
    "venue_suggestions": [{"name": "Hall", "type": "banquet", "capacity": 50, "estimated_cost": 2000}]}
JEWEL = {"outfit_analysis": {"colors": ["red"], "style": "ethnic", "formality": "formal"},
         "jewelry_recommendations": [{"item_type": "necklace", "estimated_price": 2000,
                                      "search_terms": "gold necklace"}]}


def png_bytes():
    buf = BytesIO()
    Image.new("RGB", (40, 40), "red").save(buf, "PNG")
    return buf.getvalue()


def login(client, name):
    r = client.post("/register", json={"username": name, "email": f"{name}@example.com",
                                       "password": "secret123", "confirm_password": "secret123"})
    assert r.status_code == 201, r.text
    r = client.post("/token", data={"username": name, "password": "secret123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def run():
    client = TestClient(main.app)
    other = TestClient(main.app)
    login(client, "alice")
    login(other, "bob")

    # unauthenticated access is rejected
    assert TestClient(main.app).post("/home-budget", json={"total_budget": 5000}).status_code == 401

    body = {"total_budget": 5000, "num_lights": 5}
    with mock.patch.object(gu, "get_service", return_value=FakeService(HOME)):
        r = client.post("/home-budget", json=body)
        assert r.status_code == 200, r.text
        home = r.json()
        assert home["total_spent"] == 500 and home["source"] == "gemini" and home["id"]
        assert client.post("/generate-home", json=body).status_code == 200  # document alias
    assert client.post("/home-budget", json={"total_budget": 5000}).status_code == 422  # nothing requested
    assert client.post("/home-budget", json={"total_budget": -1, "num_lights": 1}).status_code == 422

    with mock.patch.object(gu, "get_service", return_value=FakeService(PARTY)):
        r = client.post("/party-budget", json={"total_budget": 5000, "party_type": "birthday", "num_guests": 20})
        assert r.status_code == 200, r.text
        assert r.json()["venue_suggestions"][0]["search_links"]["google"]
        assert client.post("/generate-party", json={"total_budget": 5000, "party_type": "birthday",
                                                    "num_guests": 20}).status_code == 200

    with mock.patch.object(gu, "get_service", return_value=FakeService(JEWEL)):
        r = client.post("/jewelry-budget", data={"total_budget": "8000", "occasion": "wedding"},
                        files={"image": ("dress.png", png_bytes(), "image/png")})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["outfit_analysis"]["colors"] == ["red"] and j["total_spent"] == 2000
        r = client.post("/generate-jewelry", data={"total_budget": "8000", "occasion": "wedding"})  # no image
        assert r.status_code == 200 and "outfit_analysis" not in r.json()
        bad = client.post("/jewelry-budget", data={"total_budget": "8000", "occasion": "wedding"},
                          files={"image": ("x.gif", b"GIF89a", "image/gif")})
        assert bad.status_code == 400
        assert client.post("/jewelry-budget", data={"total_budget": "0", "occasion": "wedding"}).status_code == 422

    # Gemini down -> fallback still returns a plan and is saved
    with mock.patch.object(gu, "get_service", return_value=FakeService(error=GeminiError("down"))):
        r = client.post("/home-budget", json=body)
        assert r.status_code == 200 and r.json()["source"] == "fallback"

    # session data was updated by the planner routes
    info = client.get("/session-info").json()["user_data"]
    assert info["last_home_budget"]["budget"] == 5000 and info["last_party_budget"]["guests"] == 20
    assert info["last_jewelry_budget"]["occasion"] == "wedding"

    # history
    h = client.get("/recommendation-history").json()["history"]
    assert len(h) == 7 and {x["type"] for x in h} == {"home", "party", "jewelry"}
    assert h == sorted(h, key=lambda x: x["timestamp"], reverse=True)
    d = client.get(f"/recommendation-details/{h[-1]['id']}")
    assert d.status_code == 200 and d.json()["full_result"]["total_budget"] == 5000
    assert client.get("/recommendation-details/nope").status_code == 404
    assert other.get("/recommendation-history").json() == {"history": []}  # users are isolated
    assert other.get(f"/recommendation-details/{h[0]['id']}").status_code == 404
    assert client.get("/history").status_code == 200
    assert TestClient(main.app).get("/history", headers={"accept": "text/html"},
                                    follow_redirects=False).status_code == 303
    # logout invalidates access
    client.post("/logout", follow_redirects=False)
    assert client.get("/recommendation-history").status_code == 401
    print("[PASS] module 3: all checks")


if __name__ == "__main__":
    run()