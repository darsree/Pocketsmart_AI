"""PocketSmart AI - Member 4 extras: Forgot Password + footer info pages.

Kept in its own file so Member 2's auth.py and Member 3's routes stay untouched.
main.py only needs two lines:   import extras   /   app.include_router(extras.router)
"""
import time
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import auth

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

# ------------------------------------------------------------------ forgot / reset password
_attempts: Dict[str, List[float]] = {}   # key -> timestamps of recent tries
MAX_TRIES, WINDOW = 5, 600               # 5 tries per 10 minutes


class ResetPassword(BaseModel):
    username: str = Field(min_length=1, max_length=30)
    email: str = Field(min_length=3, max_length=120)
    new_password: str = Field(min_length=6, max_length=72)
    confirm_password: str


def _too_many(key: str) -> bool:
    now = time.time()
    recent = [t for t in _attempts.get(key, []) if now - t < WINDOW]
    recent.append(now)
    _attempts[key] = recent
    return len(recent) > MAX_TRIES


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", {})


@router.post("/reset-password")
async def reset_password(data: ResetPassword):
    """Reset a password after checking that username and registered email match."""
    if data.new_password != data.confirm_password:
        raise HTTPException(status_code=422, detail="Passwords do not match")
    key = data.username.strip().lower()
    if _too_many(key):
        raise HTTPException(status_code=429, detail="Too many attempts. Please try again in a few minutes.")
    user = auth.get_user(key)
    if not user or user.email.lower() != data.email.strip().lower():
        raise HTTPException(status_code=400, detail="Username and email do not match any account")
    with auth._users_lock:
        user.hashed_password = auth.hash_password(data.new_password)
        auth._save_users()
    session = auth.active_sessions.get(user.username)   # log out anywhere the old password was used
    if session:
        auth.end_session(session.token)
    _attempts.pop(key, None)
    return {"message": "Password updated. You can sign in now."}


# ------------------------------------------------------------------ footer info pages
PAGES = {
    "about": ("About Us", "fa-circle-info", [
        "PocketSmart AI is a GenAI-powered budget and recommendation assistant. You tell it your budget and needs, and it suggests items and services that fit.",
        "It covers three areas: home interiors, party planning and jewelry. Suggestions link to shops and services such as Amazon, Flipkart, IKEA, Swiggy, Zomato and OYO.",
        "The app is built with FastAPI, Jinja2 templates and Google's Gemini model."]),
    "team": ("Our Team", "fa-people-group", [
        "PocketSmart AI is built by a team of four students, each owning one module.",
        "Module 1 - Gemini AI setup. Module 2 - FastAPI backend and authentication. Module 3 - planner APIs and recommendation history. Module 4 - user interface."]),
    "careers": ("Careers", "fa-briefcase", [
        "PocketSmart AI is a student project, so there are no open positions right now."]),
    "contact": ("Contact Us", "fa-envelope", [
        "PocketSmart AI is a student project. For questions or feedback, please reach the project team through your course or institution."]),
    "pricing": ("Pricing", "fa-tags", [
        "PocketSmart AI is free to use. Prices shown in recommendations are AI estimates, not live shop prices, so always check the shop before buying."]),
    "blog": ("Blog", "fa-pen-nib", [
        "No posts yet. Budgeting tips and project updates will appear here."]),
    "guides": ("Guides", "fa-book-open", [
        "1. Create an account and sign in.",
        "2. Pick a planner: Home, Party or Jewelry.",
        "3. Enter your budget and what you need. For jewelry you can also upload an outfit photo.",
        "4. Review the plan, open the shopping links, and find it again later under History."]),
    "faq": ("FAQ", "fa-circle-question", [
        "Are the prices exact? No. They are AI estimates. Check the shop for the real price.",
        "Does PocketSmart buy anything for me? No. It only gives suggestions and search links.",
        "What happens to my outfit photo? It is used to match colours and style for your jewelry plan.",
        "I forgot my password. Use Forgot Password on the sign-in page."]),
    "support": ("Support", "fa-life-ring", [
        "Plan not loading? Check your internet connection and try again. If the AI is busy you may receive a standard fallback plan instead.",
        "Locked out? Use Forgot Password on the sign-in page."]),
    "privacy": ("Privacy Policy", "fa-user-shield", [
        "We store your username, email and a hashed password so you can sign in. Your plans are kept so you can see them in History.",
        "Budgets, preferences and any outfit photo you upload are sent to Google's Gemini service to generate recommendations.",
        "We do not sell your data. This is a student project, so please do not enter sensitive personal information."]),
    "terms": ("Terms of Service", "fa-file-contract", [
        "PocketSmart AI is provided as-is for learning and planning purposes.",
        "Recommendations and prices are AI-generated estimates and may be inaccurate. You are responsible for your own purchasing decisions.",
        "Links to third-party shops are for convenience; we are not affiliated with them."]),
    "cookies": ("Cookies Policy", "fa-cookie-bite", [
        "We use one cookie, set when you sign in, to keep you logged in. It is removed when you log out or it expires.",
        "We do not use advertising or tracking cookies."]),
}


def _make_page(slug: str):
    async def page(request: Request):
        title, icon, paragraphs = PAGES[slug]
        user = await auth.get_current_user(request)
        return templates.TemplateResponse(
            request, "info.html", {"user": user, "title": title, "icon": icon, "paragraphs": paragraphs})
    return page


for _slug in PAGES:
    router.add_api_route(f"/{_slug}", _make_page(_slug), methods=["GET"],
                         response_class=HTMLResponse, include_in_schema=False)