"""PocketSmart AI - Module 2: recommendation engine (gemini_utils.py).

Sits on top of Member 1's work (gemini_client.get_service() + prompts.py) and adds:
  * input validation / prompt orchestration for Home, Party and Jewelry planners
  * math we do NOT trust the model with: totals, remaining budget, calculation tables
  * one automatic retry when the plan goes over budget
  * shopping-link generation (Amazon, Flipkart, IKEA, Swiggy, Zomato, OYO, ...)
  * fallback recommendations when Gemini is unavailable or returns nothing usable
  * save_upload_file() for the outfit image (used by the Jewelry route)

Public API (all return plain dicts that can be sent straight to the frontend):
    get_home_recommendations(HomeBudgetInput)
    get_party_recommendations(PartyBudgetInput)
    get_jewelry_recommendations(JewelryBudgetInput, image_path=None)
    *_async versions of the three above (use these inside `async def` routes)
"""
import logging
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus


# Member 1's files (config.py, gemini_client.py, prompts.py) live one folder up from module_2/.
# Adding that folder to the import path lets this module find them. Harmless if they are alongside.
sys.path.append(str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.concurrency import run_in_threadpool

from gemini_client import GeminiError, get_service
from models import HomeBudgetInput, JewelryBudgetInput, PartyBudgetInput
from prompts import home_prompt, jewelry_prompt, party_prompt

log = logging.getLogger("pocketsmart.utils")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

# ------------------------------------------------------------------ shopping platforms
SEARCH_URLS: Dict[str, str] = {
    "amazon": "https://www.amazon.in/s?k={q}",
    "flipkart": "https://www.flipkart.com/search?q={q}",
    "ikea": "https://www.ikea.com/in/en/search/?q={q}",
    "myntra": "https://www.myntra.com/search?q={q}",
    "ajio": "https://www.ajio.com/search/?text={q}",
    "meesho": "https://www.meesho.com/search?q={q}",
    "bigbasket": "https://www.bigbasket.com/ps/?q={q}",
    "swiggy": "https://www.swiggy.com/search?query={q}",
    "zomato": "https://www.zomato.com/search?q={q}",
    "bookmyshow": "https://in.bookmyshow.com/search?q={q}",
    "google": "https://www.google.com/search?q={q}",
    "booking": "https://www.booking.com/search.html?ss={q}",
    "makemytrip": "https://www.makemytrip.com/hotels/hotel-listing/?searchText={q}",
    "oyorooms": "https://www.oyorooms.com/search/?location={q}",
    "nobroker": "https://www.nobroker.in/property/search?searchTerm={q}",
    "bluestone": "https://www.bluestone.com/search.html?query={q}",
    "tanishq": "https://www.tanishq.co.in/search?q={q}",
    "caratlane": "https://www.caratlane.com/search?q={q}",
    "melorra": "https://www.melorra.com/search?q={q}",
}

# Same five buttons the Home results page shows. Edit this list to change them.
HOME_PLATFORMS = ["amazon", "flipkart", "ikea", "myntra", "ajio"]
JEWELRY_PLATFORMS = ["amazon", "flipkart", "bluestone", "tanishq", "caratlane", "melorra", "meesho"]
VENUE_PLATFORMS = ["google", "booking", "makemytrip", "oyorooms", "nobroker"]
PARTY_DEFAULT_PLATFORMS = ["amazon", "flipkart", "google"]
PARTY_CATEGORY_PLATFORMS: Dict[str, List[str]] = {
    "venue": VENUE_PLATFORMS,
    "catering": ["swiggy", "zomato"],
    "food": ["swiggy", "zomato", "bigbasket", "amazon", "flipkart"],
    "drinks": ["swiggy", "zomato", "bigbasket", "amazon", "flipkart"],
    "decoration": ["amazon", "flipkart", "meesho", "myntra"],
    "entertainment": ["bookmyshow", "amazon", "flipkart"],
    "gifts": ["amazon", "flipkart", "myntra", "meesho"],
    "return_gifts": ["amazon", "flipkart", "myntra", "meesho"],
    "photography": ["google", "amazon", "flipkart"],
    "music": ["amazon", "flipkart", "bookmyshow"],
    "games": ["amazon", "flipkart"],
    "accessories": ["amazon", "flipkart", "myntra", "meesho"],
    "transportation": ["makemytrip", "google"],
}


def build_links(platforms: List[str], search_terms: str) -> Dict[str, str]:
    q = quote_plus(search_terms.strip())
    return {p: SEARCH_URLS[p].replace("{q}", q) for p in platforms if p in SEARCH_URLS}


def _platforms_for_category(category: str) -> List[str]:
    c = re.sub(r"[^a-z]+", "_", category.lower()).strip("_")
    if c in PARTY_CATEGORY_PLATFORMS:
        return PARTY_CATEGORY_PLATFORMS[c]
    if len(c) >= 3:
        for key, platforms in PARTY_CATEGORY_PLATFORMS.items():
            if key in c or c in key:
                return platforms
    return PARTY_DEFAULT_PLATFORMS


# ------------------------------------------------------------------ cleaning / math
def _num(value: Any, default: float = 0.0) -> float:
    """Tolerant number parser: 1200, '1200', 'Rs 1,200.50' all work."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"\d[\d,]*\.?\d*", value)
        if m:
            try:
                return float(m.group().replace(",", ""))
            except ValueError:
                pass
    return default


def _clean_items(items: Any) -> List[Dict[str, Any]]:
    cleaned = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or it.get("item_type") or "").strip()
        if not name:
            continue
        cleaned.append({
            **it,
            "name": name,
            "description": str(it.get("description") or "").strip(),
            "estimated_price": round(max(_num(it.get("estimated_price")), 0.0), 2),
            "quantity": max(int(_num(it.get("quantity"), 1)), 1),
            "search_terms": str(it.get("search_terms") or name).strip(),
        })
    return cleaned


def _str_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def _finalize_plan(raw: Dict[str, Any], total_budget: float, table_key: str) -> Dict[str, Any]:
    """Re-compute every number from the items so the UI never shows wrong totals."""
    breakdown, table = [], []
    spent = allocated = 0.0
    for cat in raw.get("budget_breakdown") or []:
        if not isinstance(cat, dict):
            continue
        items = _clean_items(cat.get("items"))
        if not items:
            continue
        cost = round(sum(i["estimated_price"] * i["quantity"] for i in items), 2)
        alloc = round(_num(cat.get("allocation")) or cost, 2)
        name = str(cat.get("category") or "misc").strip()
        breakdown.append({"category": name, "allocation": alloc, "items": items})
        table.append({
            "category": name,
            "items_count": len(items),
            "total_cost": cost,
            "percentage_of_budget": round(cost / total_budget * 100, 2),
        })
        spent += cost
        allocated += alloc
    spent = round(spent, 2)
    return {
        "total_budget": round(total_budget, 2),
        "budget_breakdown": breakdown,
        table_key: table,
        "total_spent": spent,
        "total_allocated": round(allocated, 2),
        "remaining_budget": round(total_budget - spent, 2),
        "additional_suggestions": _str_list(raw.get("additional_suggestions")),
    }


# ------------------------------------------------------------------ Gemini orchestration
def _generate_with_checks(prompt: str, total_budget: float,
                          finalize: Callable[[Dict[str, Any]], Dict[str, Any]],
                          has_items: Callable[[Dict[str, Any]], bool],
                          image: Optional[str] = None) -> Dict[str, Any]:
    """Call Gemini, finalize, retry once if over budget. Raises GeminiError if unusable."""
    svc = get_service()
    result = finalize(svc.generate_json(prompt, image=image))
    if not has_items(result):
        raise GeminiError("Gemini returned no usable items.")

    if result["total_spent"] > total_budget * 1.01:
        log.warning("Plan costs %.0f but budget is %.0f - asking Gemini for a cheaper plan.",
                    result["total_spent"], total_budget)
        retry_prompt = (
            prompt + f"\n\nIMPORTANT: your previous plan cost Rs {result['total_spent']:.0f}, which is MORE than the "
            f"budget of Rs {total_budget:.0f}. Return a cheaper plan with a total of at most "
            f"Rs {total_budget * 0.95:.0f}. Remember total = sum of estimated_price x quantity."
        )
        try:
            second = finalize(svc.generate_json(retry_prompt, image=image))
            if has_items(second) and second["total_spent"] < result["total_spent"]:
                result = second
        except GeminiError as exc:
            log.warning("Budget-correction retry failed: %s", exc)
        if result["total_spent"] > total_budget * 1.01:
            warn = (f"Estimated total (Rs {result['total_spent']:.0f}) is above your budget. "
                    "Consider reducing quantities or choosing cheaper options.")
            result["budget_warning"] = warn
            result["additional_suggestions"] = [warn] + result.get("additional_suggestions", [])

    result["source"] = "gemini"
    result["model"] = svc.last_model_used
    return result


def _run(prompt: str, total_budget: float, finalize, has_items, fallback, image: Optional[str] = None):
    try:
        return _generate_with_checks(prompt, total_budget, finalize, has_items, image)
    except ValueError as exc:  # no API key configured -> configuration problem, not a fallback case
        raise HTTPException(status_code=500, detail=str(exc))
    except GeminiError as exc:
        if "API key" in str(exc):
            raise HTTPException(status_code=500, detail=str(exc))
        log.error("Gemini unavailable, using fallback plan: %s", exc)
        result = fallback()
        result["source"] = "fallback"
        result["notice"] = "The AI service is unavailable right now, so standard budget estimates are shown."
        return result


# ================================================================== HOME PLANNER
def _home_has_items(r: Dict[str, Any]) -> bool:
    return bool(r.get("budget_breakdown"))


def _finalize_home(raw: Dict[str, Any], total_budget: float) -> Dict[str, Any]:
    result = _finalize_plan(raw, total_budget, "calculation_table")
    for cat in result["budget_breakdown"]:
        for item in cat["items"]:
            item["shopping_links"] = build_links(HOME_PLATFORMS, item["search_terms"])
    return result


def _fallback_home(b: HomeBudgetInput) -> Dict[str, Any]:
    spec = [  # category, qty, budget weight, item name, search terms
        ("lighting", b.num_lights, 0.20, "LED light fixture (warm white)", "led ceiling light warm white"),
        ("ceiling_fans", b.num_fans, 0.25, "5-star BEE ceiling fan", "ceiling fan 1200mm 5 star"),
        ("furniture", b.num_furniture, 0.35, "Engineered-wood furniture piece", "engineered wood furniture"),
        ("dining_tables", b.num_dining_tables, 0.20, "4-seater dining table", "4 seater dining table"),
    ]
    active = [s for s in spec if s[1] > 0] or [
        ("lighting", 1, 0.4, spec[0][3], spec[0][4]), ("furniture", 1, 0.6, spec[2][3], spec[2][4])]
    wsum = sum(s[2] for s in active)
    breakdown = []
    for cat, qty, w, name, terms in active:
        alloc = b.total_budget * 0.9 * w / wsum
        breakdown.append({"category": cat, "allocation": alloc, "items": [{
            "name": name, "description": "Standard budget-friendly option (estimate).",
            "estimated_price": alloc / qty, "quantity": qty, "search_terms": terms}]})
    raw = {"budget_breakdown": breakdown, "additional_suggestions": [
        "Compare prices across Amazon, Flipkart and IKEA before buying.",
        "Look out for festival-season sales and bank offers.",
        "Prioritise essentials first and postpone non-essential purchases."]}
    return _finalize_home(raw, b.total_budget)


def get_home_recommendations(budget_input: HomeBudgetInput) -> Dict[str, Any]:
    """Home interior plan within budget (INR) with shopping links per item."""
    prompt = home_prompt(
        budget_input.total_budget, budget_input.num_lights, budget_input.num_fans,
        budget_input.num_furniture, budget_input.num_dining_tables,
        rooms=budget_input.rooms, additional=budget_input.additional_requirements)
    tb = budget_input.total_budget
    return _run(prompt, tb, lambda raw: _finalize_home(raw, tb), _home_has_items,
                lambda: _fallback_home(budget_input))


# ================================================================== PARTY PLANNER
def _finalize_party(raw: Dict[str, Any], total_budget: float) -> Dict[str, Any]:
    result = _finalize_plan(raw, total_budget, "calculation_table_inr")
    for cat in result["budget_breakdown"]:
        platforms = _platforms_for_category(cat["category"])
        for item in cat["items"]:
            item["shopping_links"] = build_links(platforms, item["search_terms"])
    venues = []
    for v in raw.get("venue_suggestions") or []:
        if not isinstance(v, dict) or not str(v.get("name") or "").strip():
            continue
        name = str(v["name"]).strip()
        terms = str(v.get("search_terms") or name).strip()
        venues.append({
            "name": name,
            "type": str(v.get("type") or "").strip(),
            "capacity": int(_num(v.get("capacity"))),
            "estimated_cost": round(_num(v.get("estimated_cost")), 2),
            "location": str(v.get("location") or "").strip(),
            "search_terms": terms,
            "search_links": build_links(VENUE_PLATFORMS, terms),
        })
    result["venue_suggestions"] = venues
    return result


def _fallback_party(b: PartyBudgetInput) -> Dict[str, Any]:
    at_home = "home" in (b.venue_type or "").lower()
    shares = {"venue": 0.0 if at_home else 0.15, "catering": 0.45 if b.needs_catering else 0.0,
              "decoration": 0.20 if b.needs_decoration else 0.0,
              "entertainment": 0.15 if b.needs_entertainment else 0.0, "contingency": 0.10}
    wsum = sum(shares.values())
    kind = b.party_type.lower()
    templates = {
        "venue": ("Banquet / community hall hire", f"{kind} party hall", 1),
        "catering": (f"Catering for {b.num_guests} guests", f"{kind} party catering", b.num_guests),
        "decoration": ("Theme decoration kit", f"{kind} party decoration", 1),
        "entertainment": ("DJ / music and games", "party games and speaker", 1),
        "contingency": ("Contingency buffer", "party supplies", 1),
    }
    breakdown = []
    for cat, share in shares.items():
        if share <= 0:
            continue
        alloc = b.total_budget * 0.98 * share / wsum
        name, terms, qty = templates[cat]
        breakdown.append({"category": cat, "allocation": alloc, "items": [{
            "name": name, "description": "Standard estimate for your budget.",
            "estimated_price": alloc / qty, "quantity": qty, "search_terms": terms}]})
    venue = (b.venue_type or "Community hall / banquet hall").strip()
    raw = {"budget_breakdown": breakdown,
           "venue_suggestions": [{"name": venue, "type": venue, "capacity": b.num_guests,
                                  "estimated_cost": shares["venue"] * b.total_budget,
                                  "search_terms": f"{venue} for {b.num_guests} guests"}],
           "additional_suggestions": [
               "Order catering at least 3 days ahead for better rates.",
               "Homemade decorations can cut costs significantly.",
               "Keep the contingency buffer for last-minute expenses."]}
    return _finalize_party(raw, b.total_budget)


def get_party_recommendations(budget_input: PartyBudgetInput) -> Dict[str, Any]:
    """Party plan split across catering / decoration / entertainment (+ venue, contingency)."""
    prompt = party_prompt(
        budget_input.total_budget, budget_input.num_guests, budget_input.party_type,
        budget_input.venue_type, budget_input.needs_catering, budget_input.needs_decoration,
        budget_input.needs_entertainment, budget_input.additional_requirements)
    tb = budget_input.total_budget
    return _run(prompt, tb, lambda raw: _finalize_party(raw, tb), _home_has_items,
                lambda: _fallback_party(budget_input))


# ================================================================== JEWELRY PLANNER
def _finalize_jewelry(raw: Dict[str, Any], total_budget: float, has_image: bool) -> Dict[str, Any]:
    items = _clean_items(raw.get("jewelry_recommendations"))
    spent = 0.0
    for it in items:
        it["item_type"] = str(it.get("item_type") or it["name"]).strip()
        it["style"] = str(it.get("style") or "").strip()
        it["shopping_links"] = build_links(JEWELRY_PLATFORMS, it["search_terms"])
        spent += it["estimated_price"]  # one piece of each item
    spent = round(spent, 2)
    result: Dict[str, Any] = {
        "total_budget": round(total_budget, 2),
        "jewelry_recommendations": items,
        "total_spent": spent,
        "remaining_budget": round(total_budget - spent, 2),
        "styling_tips": _str_list(raw.get("styling_tips")),
    }
    if has_image:
        oa = raw.get("outfit_analysis") if isinstance(raw.get("outfit_analysis"), dict) else {}
        result["outfit_analysis"] = {
            "colors": _str_list(oa.get("colors")),
            "style": str(oa.get("style") or "").strip(),
            "formality": str(oa.get("formality") or "").strip(),
        }
    return result


def _fallback_jewelry(b: JewelryBudgetInput, has_image: bool) -> Dict[str, Any]:
    style = b.preferences or "elegant"
    parts = [("necklace", 0.35), ("earrings", 0.25), ("bracelet", 0.20), ("ring", 0.15)]
    items = [{"item_type": t, "name": t.title(), "style": style,
              "description": f"A {style} {t} suitable for a {b.occasion}.",
              "estimated_price": b.total_budget * w,
              "search_terms": f"{style} {t} for women {b.occasion}"} for t, w in parts]
    raw = {"jewelry_recommendations": items, "styling_tips": [
        "Pick one statement piece and keep the rest minimal.",
        "Match metal tones (gold / silver / rose gold) across all pieces.",
        "Let the neckline of the outfit decide between necklace and earrings."]}
    return _finalize_jewelry(raw, b.total_budget, has_image)


def get_jewelry_recommendations(budget_input: JewelryBudgetInput,
                                image_path: Optional[str] = None) -> Dict[str, Any]:
    """Jewelry suggestions for an occasion; pass image_path to match an outfit photo."""
    if image_path:
        try:
            with Image.open(image_path) as im:
                im.verify()
        except Exception:
            raise HTTPException(status_code=400, detail="The uploaded outfit image could not be read.")
    has_image = bool(image_path)
    prompt = jewelry_prompt(budget_input.total_budget, budget_input.occasion,
                            budget_input.preferences, has_image=has_image)
    tb = budget_input.total_budget
    return _run(prompt, tb, lambda raw: _finalize_jewelry(raw, tb, has_image),
                lambda r: bool(r.get("jewelry_recommendations")),
                lambda: _fallback_jewelry(budget_input, has_image),
                image=image_path)


# ------------------------------------------------------------------ async wrappers
# The Gemini call takes seconds. Inside `async def` routes use these so the server stays responsive.
async def get_home_recommendations_async(b: HomeBudgetInput):
    return await run_in_threadpool(get_home_recommendations, b)


async def get_party_recommendations_async(b: PartyBudgetInput):
    return await run_in_threadpool(get_party_recommendations, b)


async def get_jewelry_recommendations_async(b: JewelryBudgetInput, image_path: Optional[str] = None):
    return await run_in_threadpool(get_jewelry_recommendations, b, image_path)


# ------------------------------------------------------------------ image upload helper
def save_upload_file(upload: UploadFile) -> str:
    """Validate + store an uploaded outfit image in static/uploads/, return its path."""
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise HTTPException(status_code=400, detail="Only JPG, PNG or WEBP images are allowed.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / f"{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}{ext}"
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Image is too large (max 5 MB).")
                out.write(chunk)
        with Image.open(dest) as im:
            im.verify()
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The uploaded file is not a valid image.")
    return str(dest)