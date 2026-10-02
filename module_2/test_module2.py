import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ["USERS_DB_FILE"] = str(Path(_tmp) / "users.json")  # never touch real users
os.environ["SECRET_KEY"] = "test-secret-key-for-unit-tests-only"

from fastapi import HTTPException, UploadFile  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import gemini_utils as gu  # noqa: E402
from gemini_client import GeminiError  # noqa: E402
from main import app  # noqa: E402
from models import HomeBudgetInput, JewelryBudgetInput, PartyBudgetInput  # noqa: E402


class FakeService:
    """Stands in for Gemini so tests are free, fast and deterministic."""
    last_model_used = "fake-model"

    def _init_(self, *payloads, error=None):
        self.payloads, self.error, self.calls = list(payloads), error, []

    def generate_json(self, prompt, image=None, **kw):
        self.calls.append((prompt, image))
        if self.error:
            raise self.error
        return self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]


HOME_RAW = {"total_budget": 1, "remaining_budget": 99999, "budget_breakdown": [
    {"category": "lighting", "allocation": 1500, "items": [
        {"name": "LED Bulb", "description": "d", "estimated_price": "Rs 100", "quantity": 5, "search_terms": "led bulb warm white"}]},
    {"category": "ceiling_fans", "allocation": 2000, "items": [
        {"name": "Havells Fan", "estimated_price": 500, "quantity": 4, "search_terms": "havells ceiling fan"}]}],
    "additional_suggestions": ["Look for sales"]}


def home_input(budget=5000):
    return HomeBudgetInput(total_budget=budget, num_lights=5, num_fans=4, has_kitchen=True)


def test_home_math_and_links():
    with mock.patch.object(gu, "get_service", return_value=FakeService(HOME_RAW)):
        r = gu.get_home_recommendations(home_input())
    assert r["total_spent"] == 2500 and r["remaining_budget"] == 2500, r
    assert r["source"] == "gemini" and r["model"] == "fake-model"
    link = r["budget_breakdown"][0]["items"][0]["shopping_links"]
    assert link["amazon"] == "https://www.amazon.in/s?k=led+bulb+warm+white"
    assert set(link) == {"amazon", "flipkart", "ikea", "myntra", "ajio"}
    assert r["calculation_table"][0]["percentage_of_budget"] == 10.0


def test_over_budget_retry_uses_cheaper_plan():
    cheap = {"budget_breakdown": [{"category": "lighting", "items": [
        {"name": "Bulb", "estimated_price": 100, "quantity": 5, "search_terms": "bulb"}]}]}
    expensive = {"budget_breakdown": [{"category": "lighting", "items": [
        {"name": "Chandelier", "estimated_price": 9000, "quantity": 1, "search_terms": "chandelier"}]}]}
    fake = FakeService(expensive, cheap)
    with mock.patch.object(gu, "get_service", return_value=fake):
        r = gu.get_home_recommendations(home_input())
    assert len(fake.calls) == 2 and r["total_spent"] == 500 and "budget_warning" not in r


def test_still_over_budget_adds_warning():
    expensive = {"budget_breakdown": [{"category": "lighting", "items": [
        {"name": "Chandelier", "estimated_price": 9000, "quantity": 1, "search_terms": "chandelier"}]}]}
    with mock.patch.object(gu, "get_service", return_value=FakeService(expensive)):
        r = gu.get_home_recommendations(home_input())
    assert "budget_warning" in r and r["remaining_budget"] < 0


def test_fallback_when_gemini_fails():
    fake = FakeService(error=GeminiError("All models failed"))
    with mock.patch.object(gu, "get_service", return_value=fake):
        home = gu.get_home_recommendations(home_input())
        party = gu.get_party_recommendations(PartyBudgetInput(total_budget=5000, party_type="Birthday", num_guests=10))
        jew = gu.get_jewelry_recommendations(JewelryBudgetInput(total_budget=5000, occasion="Wedding"))
    for r in (home, party, jew):
        assert r["source"] == "fallback" and r["notice"] and r["total_spent"] <= 5000, r
        assert r["remaining_budget"] >= 0
    assert party["venue_suggestions"] and "calculation_table_inr" in party


def test_bad_api_key_is_not_hidden():
    fake = FakeService(error=GeminiError("Gemini rejected the API key. Check GOOGLE_API_KEY"))
    with mock.patch.object(gu, "get_service", return_value=fake):
        try:
            gu.get_home_recommendations(home_input())
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 500


def test_party_links_by_category():
    raw = {"budget_breakdown": [
        {"category": "Catering", "items": [{"name": "Biryani", "estimated_price": 200, "quantity": 10, "search_terms": "biryani"}]},
        {"category": "Decor", "items": [{"name": "Balloons", "estimated_price": 300, "quantity": 1, "search_terms": "balloons"}]},
        {"category": "Entertainment", "items": [{"name": "DJ", "estimated_price": 1000, "quantity": 0, "search_terms": "dj"}]}],
        "venue_suggestions": [{"name": "Hall A", "type": "Hall", "capacity": 50, "estimated_cost": 1500, "search_terms": "banquet hall chennai"}]}
    with mock.patch.object(gu, "get_service", return_value=FakeService(raw)):
        r = gu.get_party_recommendations(PartyBudgetInput(total_budget=5000, party_type="Birthday", num_guests=10))
    cats = {c["category"]: c for c in r["budget_breakdown"]}
    assert set(cats["Catering"]["items"][0]["shopping_links"]) == {"justdial", "sulekha", "wedmegood", "google"}
    assert "meesho" in cats["Decor"]["items"][0]["shopping_links"]
    assert "bookmyshow" in cats["Entertainment"]["items"][0]["shopping_links"]
    assert r["total_spent"] == 2000 + 300 + 1000  # quantity 0 treated as 1
    assert "oyorooms" in r["venue_suggestions"][0]["search_links"]


def test_jewelry_with_image():
    img = Path(_tmp) / "outfit.png"
    Image.new("RGB", (200, 200), (30, 60, 200)).save(img)
    raw = {"outfit_analysis": {"colors": ["blue"], "style": "casual", "formality": "informal"},
           "jewelry_recommendations": [{"item_type": "bracelet", "description": "d", "style": "casual",
                                        "estimated_price": 500, "search_terms": "silver bracelet"}]}
    fake = FakeService(raw)
    with mock.patch.object(gu, "get_service", return_value=fake):
        r = gu.get_jewelry_recommendations(JewelryBudgetInput(total_budget=5000, occasion="Birthday"), str(img))
    assert fake.calls[0][1] == str(img)
    assert r["outfit_analysis"]["colors"] == ["blue"] and r["remaining_budget"] == 4500
    assert "tanishq" in r["jewelry_recommendations"][0]["shopping_links"]
    try:
        gu.get_jewelry_recommendations(JewelryBudgetInput(total_budget=5000, occasion="Birthday"), "missing.png")
        raise AssertionError("expected HTTPException")
    except HTTPException as e:
        assert e.status_code == 400


def test_input_validation():
    for bad in (dict(total_budget=0), dict(total_budget=-5), dict()):
        try:
            HomeBudgetInput(**bad)
            raise AssertionError(f"should reject {bad}")
        except ValueError:
            pass


def test_save_upload_file():
    gu.UPLOAD_DIR = Path(_tmp) / "uploads"
    buf = BytesIO()
    Image.new("RGB", (20, 20), "red").save(buf, "PNG")
    buf.seek(0)
    path = gu.save_upload_file(UploadFile(file=buf, filename="dress.png"))
    assert Path(path).exists()
    for name, data in (("evil.exe", b"MZ"), ("fake.png", b"not an image")):
        try:
            gu.save_upload_file(UploadFile(file=BytesIO(data), filename=name))
            raise AssertionError("expected rejection")
        except HTTPException as e:
            assert e.status_code == 400
    assert len(list(gu.UPLOAD_DIR.iterdir())) == 1  # rejected files cleaned up


def test_auth_and_session_flow():
    with TestClient(app) as c:
        assert c.get("/health").json()["status"] == "ok"
        # protected page, browser -> redirect to login; API client -> 401
        r = c.get("/dashboard", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.get("/session-info").status_code == 401

        body = {"username": "Sai", "email": "sai@example.com", "password": "secret123", "confirm_password": "secret123"}
        assert c.post("/register", json={**body, "confirm_password": "nope"}).status_code == 422
        assert c.post("/register", json={**body, "password": "abc", "confirm_password": "abc"}).status_code == 422
        assert c.post("/register", json=body).status_code == 201
        assert c.post("/register", json=body).status_code == 409
        assert c.post("/register", data={"username": "form_user", "email": "f@example.com", "password": "secret123"}).status_code == 201

        assert c.post("/token", data={"username": "sai", "password": "wrong"}).status_code == 401
        r = c.post("/token", data={"username": "SAI", "password": "secret123"})
        assert r.status_code == 200 and "access_token" in r.cookies
        token = r.json()["access_token"]

        assert c.get("/login", follow_redirects=False).status_code == 302  # already logged in
        for page in ("/dashboard", "/home-planner", "/party-planner", "/jewelry-planner"):
            assert c.get(page).status_code == 200, page
        info = c.get("/session-info").json()
        assert info["username"] == "sai"
        r = c.post("/session-data", json={"last_home_budget": {"budget": 5000}})
        assert r.json()["data"]["last_home_budget"]["budget"] == 5000

        # re-login keeps session data and kills the old token
        token2 = c.post("/token", data={"username": "sai", "password": "secret123"}).json()["access_token"]
        c.cookies.clear()
        assert c.get("/session-info", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        r = c.get("/session-info", headers={"Authorization": f"Bearer {token2}"})
        assert r.status_code == 200 and "last_home_budget" in r.json()["user_data"]

        r = c.post("/logout", headers={"Authorization": f"Bearer {token2}"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.get("/session-info", headers={"Authorization": f"Bearer {token2}"}).status_code == 401

    # users survive a restart
    import auth
    auth.users_db.clear()
    auth.load_users()
    assert "sai" in auth.users_db and auth.users_db["sai"].hashed_password != "secret123"


# ---------------------------------------------------------------- negative / insufficient budgets
def test_negative_zero_and_invalid_budgets_rejected_with_clear_message():
    cases = {-5: "negative", 0: "zero", float("nan"): "valid", float("inf"): "valid", 2e9: "too large"}
    for model, extra in ((HomeBudgetInput, dict(num_lights=1)),
                         (PartyBudgetInput, dict(party_type="Birthday", num_guests=5)),
                         (JewelryBudgetInput, dict(occasion="Wedding"))):
        for bad, word in cases.items():
            try:
                model(total_budget=bad, **extra)
                raise AssertionError(f"{model._name_} accepted {bad}")
            except ValueError as e:
                assert word in str(e), (model._name_, bad, str(e))


def test_insufficient_budget_blocked_before_calling_gemini():
    fake = FakeService(HOME_RAW)
    with mock.patch.object(gu, "get_service", return_value=fake):
        for call in (
            lambda: gu.get_home_recommendations(HomeBudgetInput(total_budget=500, num_lights=5, num_fans=4)),
            lambda: gu.get_party_recommendations(PartyBudgetInput(total_budget=500, party_type="Wedding", num_guests=50)),
            lambda: gu.get_jewelry_recommendations(JewelryBudgetInput(total_budget=100, occasion="Wedding")),
        ):
            try:
                call()
                raise AssertionError("expected InsufficientBudgetError")
            except gu.InsufficientBudgetError as e:
                assert e.minimum > e.budget and e.shortfall > 0 and "not enough" in e.message
    assert fake.calls == []  # no API call wasted


def test_party_at_home_has_no_venue_minimum_and_unticked_needs_are_skipped():
    # 10 guests at home, catering only: min = 10 x 100 = 1000
    fake = FakeService(HOME_RAW)
    with mock.patch.object(gu, "get_service", return_value=fake):
        gu.get_party_recommendations(PartyBudgetInput(
            total_budget=1200, party_type="Birthday", num_guests=10, venue_type="Home",
            needs_decoration=False, needs_entertainment=False))
        try:
            gu.get_party_recommendations(PartyBudgetInput(
                total_budget=1200, party_type="Birthday", num_guests=10, venue_type="Banquet Hall",
                needs_decoration=False, needs_entertainment=False))
            raise AssertionError("banquet hall should add a venue minimum")
        except gu.InsufficientBudgetError:
            pass


def test_tight_budget_is_flagged_but_still_planned():
    raw = {"budget_breakdown": [{"category": "lighting", "items": [
        {"name": "Bulb", "estimated_price": 100, "quantity": 5, "search_terms": "led bulb"}]}]}
    with mock.patch.object(gu, "get_service", return_value=FakeService(raw)):
        r = gu.get_home_recommendations(HomeBudgetInput(total_budget=600, num_lights=5))  # min 500
    assert r["budget_status"] == "tight" and "tight" in r["additional_suggestions"][0].lower()


def test_over_budget_after_retry_reports_shortfall():
    expensive = {"budget_breakdown": [{"category": "lighting", "items": [
        {"name": "Chandelier", "estimated_price": 9000, "quantity": 1, "search_terms": "chandelier"}]}]}
    with mock.patch.object(gu, "get_service", return_value=FakeService(expensive)):
        r = gu.get_home_recommendations(HomeBudgetInput(total_budget=5000, num_lights=5))
    assert r["budget_status"] == "over" and r["shortfall"] == 4000 and "4,000" in r["budget_warning"]


def test_budget_errors_over_http():
    with TestClient(app) as c:
        c.post("/register", json={"username": "budgeter", "email": "b@example.com", "password": "secret123"})
        assert c.post("/token", data={"username": "budgeter", "password": "secret123"}).status_code == 200
        # negative / zero -> 422 with readable per-field message
        for bad in (-100, 0):
            r = c.post("/party-budget", json={"total_budget": bad, "party_type": "Birthday", "num_guests": 5})
            assert r.status_code == 422 and "greater than 0" in r.json()["detail"][0]["msg"], r.text
        r = c.post("/jewelry-budget", data={"total_budget": "-20", "occasion": "Wedding"})
        assert r.status_code == 422 and "negative" in r.json()["detail"][0]["msg"], r.text
        # too low -> 422 with message + numbers, and nothing saved to history
        before = len(c.get("/recommendation-history").json().get("history", []))
        r = c.post("/party-budget", json={"total_budget": 300, "party_type": "Wedding", "num_guests": 100})
        j = r.json()
        assert r.status_code == 422 and j["error"] == "insufficient_budget", r.text
        assert j["minimum_budget"] > 300 and j["shortfall"] == j["minimum_budget"] - 300 and "not enough" in j["detail"]
        assert len(c.get("/recommendation-history").json().get("history", [])) == before


def run_live():
    print("\n--- LIVE Gemini test ---")
    for name, fn in (
        ("home", lambda: gu.get_home_recommendations(HomeBudgetInput(
            total_budget=50000, num_lights=5, num_fans=2, num_furniture=2, num_dining_tables=1, has_living_room=True))),
        ("party", lambda: gu.get_party_recommendations(PartyBudgetInput(
            total_budget=20000, party_type="Birthday", num_guests=20, venue_type="Home"))),
        ("jewelry", lambda: gu.get_jewelry_recommendations(JewelryBudgetInput(
            total_budget=15000, occasion="Wedding", preferences="gold, traditional"))),
    ):
        r = fn()
        print(f"[{name}] source={r['source']} model={r.get('model')} spent={r['total_spent']} "
              f"remaining={r['remaining_budget']} {r.get('notice', '')}")


if _name_ == "_main_":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
        except Exception as exc:
            failed += 1
            import traceback
            print(f"[FAIL] {name}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed.")
    if "--live" in sys.argv and not failed:
        run_live()
    sys.exit(1 if failed else 0)