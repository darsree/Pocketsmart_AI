"""PocketSmart AI - Module 2: FastAPI application (main.py).

Activity 2.1  app init, .env, CORS, static + templates
Activity 2.2  planner page routes (/home-planner, /party-planner, /jewelry-planner)
Activity 2.3  /login, /register, /logout pages + POST /register
Activity 2.4  /token, /session-info, /session-data, startup session cleanup

Module 3 (Member 3)
Activity 3.1  POST /home-budget, /party-budget, /jewelry-budget (+ /generate-* aliases)
Activity 3.2  GET /recommendation-history, GET /recommendation-details/{id}
Activity 3.3  GET /history page (CORS + static routing are set up above)
Activity 3.4  startup cleanup (lifespan) + _main_ entry point
"""
import asyncio
import html
import json
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

import auth
from auth import (ACCESS_TOKEN_EXPIRE_MINUTES, COOKIE_NAME, COOKIE_SECURE, active_sessions,
                  authenticate_user, create_access_token, end_session, get_current_active_user,
                  get_current_user, get_token, register_user, start_session, utcnow)
from gemini_utils import (InsufficientBudgetError, get_home_recommendations_async, get_jewelry_recommendations_async,
                          get_party_recommendations_async, save_upload_file)
from models import (HomeBudgetInput, JewelryBudgetInput, PartyBudgetInput, RegisterUser, Token,
                    UserInDB)

import extras  # Member 4: forgot-password + footer info pages

load_dotenv()
log = logging.getLogger("pocketsmart.main")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
(STATIC_DIR / "uploads").mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)


# ---- startup / shutdown: background cleanup of idle sessions -------------------------------
async def _cleanup_loop() -> None:
    while True:
        try:
            auth.purge_expired_sessions()
        except Exception:
            log.exception("Session cleanup failed")
        await asyncio.sleep(300)  # every 5 minutes


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="PocketSmart: AI Budget Planner", lifespan=lifespan)

# ---- CORS: the UI is served by this same app, so only localhost origins by default ------------
_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(extras.router)  # Member 4: forgot-password + footer pages
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render(request: Request, name: str, user: Optional[UserInDB] = None, **ctx: Any):
    """Render templates/<name>; show a small placeholder page if the UI file isn't there yet."""
    if (TEMPLATES_DIR / name).exists():
        return templates.TemplateResponse(request, name, {"user": user, **ctx})
    who = f" as <b>{html.escape(user.username)}</b>" if user else ""
    return HTMLResponse(
        f"<h2>PocketSmart backend is running{who}</h2>"
        f"<p>Template <code>templates/{html.escape(name)}</code> has not been added yet.</p>"
        "<p><a href='/docs'>API docs</a> | <a href='/login'>Login</a> | <a href='/register'>Register</a> | "
        "<a href='/dashboard'>Dashboard</a> | <a href='/home-planner'>Home</a> | "
        "<a href='/party-planner'>Party</a> | <a href='/jewelry-planner'>Jewelry</a></p>"
    )


# Browser page request without a login -> send to /login instead of showing raw JSON.
@app.exception_handler(StarletteHTTPException)
async def _http_exc(request: Request, exc: StarletteHTTPException):
    if (exc.status_code == 401 and request.method == "GET"
            and "text/html" in request.headers.get("accept", "")):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return await http_exception_handler(request, exc)


# Budget too low for the request -> 422 with a readable message plus numbers the UI can use.
@app.exception_handler(InsufficientBudgetError)
async def _insufficient_budget(request: Request, exc: InsufficientBudgetError):
    return JSONResponse(status_code=422, content={
        "detail": exc.message, "error": "insufficient_budget",
        "budget": exc.budget, "minimum_budget": exc.minimum, "shortfall": exc.shortfall})


# =============================================================== public pages
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = await get_current_user(request)
    return render(request, "index.html", user)


@app.get("/health")
async def health():
    return {"status": "ok", "users": len(auth.users_db), "active_sessions": len(active_sessions)}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Serve the login page (already logged in -> dashboard)."""
    if await get_current_user(request):
        return RedirectResponse("/dashboard", status_code=status.HTTP_302_FOUND)
    return render(request, "login.html")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Serve the registration page (already logged in -> dashboard)."""
    if await get_current_user(request):
        return RedirectResponse("/dashboard", status_code=status.HTTP_302_FOUND)
    return render(request, "register.html")


_REGISTER_BODY = {"requestBody": {"required": True, "content": {"application/json": {
    "schema": RegisterUser.model_json_schema(),
    "example": {"username": "sai", "email": "sai@example.com", "password": "secret123",
                "confirm_password": "secret123", "full_name": "Sai"}}}}}


@app.post("/register", status_code=status.HTTP_201_CREATED, openapi_extra=_REGISTER_BODY)
async def register(request: Request):
    """Create an account. Accepts JSON or an HTML form:
    username, email, password, [confirm_password], [full_name]."""
    try:
        if "application/json" in request.headers.get("content-type", ""):
            payload = await request.json()
        else:
            payload = dict(await request.form())
        data = RegisterUser(**payload)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        msg = errors[0]["msg"].removeprefix("Value error, ") if errors else "Invalid input"
        return JSONResponse({"detail": errors, "message": msg}, status_code=422)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")
    user = register_user(data)
    return {"message": "Account created successfully", "username": user.username}


@app.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    """OAuth2 password login: returns the JWT and also sets it as an httponly cookie."""
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token({"sub": user.username},
                                timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    start_session(user, token)  # keeps old user_data, blacklists the previous token
    response = JSONResponse({"access_token": token, "token_type": "bearer"})
    response.set_cookie(key=COOKIE_NAME, value=token, httponly=True,
                        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60, samesite="lax",
                        secure=COOKIE_SECURE)
    return response


async def _do_logout(request: Request):
    """Blacklist the token, clear the session + cookie, go to /login."""
    token = await get_token(request)
    if token:
        end_session(token)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.post("/logout")
async def logout(request: Request):
    return await _do_logout(request)


@app.get("/logout", include_in_schema=False)  # lets a plain <a href="/logout"> link work too
async def logout_link(request: Request):
    return await _do_logout(request)


# =============================================================== protected pages
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, current_user: UserInDB = Depends(get_current_active_user)):
    return render(request, "dashboard.html", current_user)


@app.get("/home-planner", response_class=HTMLResponse)
async def home_planner(request: Request, current_user: UserInDB = Depends(get_current_active_user)):
    """Home budget planner page"""
    return render(request, "home_planner.html", current_user)


@app.get("/party-planner", response_class=HTMLResponse)
async def party_planner(request: Request, current_user: UserInDB = Depends(get_current_active_user)):
    """Party budget planner page"""
    return render(request, "party_planner.html", current_user)


@app.get("/jewelry-planner", response_class=HTMLResponse)
async def jewelry_planner(request: Request, current_user: UserInDB = Depends(get_current_active_user)):
    """Jewelry budget planner page"""
    return render(request, "jewelry_planner.html", current_user)


# =============================================================== sessions
@app.get("/session-info")
async def get_session_info(request: Request,
                           current_user: UserInDB = Depends(get_current_active_user)):
    session = active_sessions.get(current_user.username)
    if not session:
        raise HTTPException(status_code=404, detail="No active session found")
    return {
        "username": session.username,
        "login_time": session.login_time,
        "last_activity": session.last_activity,
        "session_duration": int((utcnow() - session.login_time).total_seconds() // 60),  # minutes
        "user_data": session.user_data,
    }


@app.post("/session-data")
async def update_session_data(data: Dict[str, Any], request: Request,
                              current_user: UserInDB = Depends(get_current_active_user)):
    if len(data) > 50:
        raise HTTPException(status_code=413, detail="Too many session keys (max 50)")
    session = active_sessions.get(current_user.username)
    if not session:
        raise HTTPException(status_code=404, detail="No active session found")
    session.user_data.update(data)
    session.last_activity = utcnow()
    return {"message": "Session data updated", "data": session.user_data}


# ======================================================================================
# MODULE 3 (Member 3): planner API routes + recommendation history
# ======================================================================================
MAX_HISTORY_PER_USER = 100
# username -> newest-last list of saved recommendations (in memory, like active_sessions)
user_recommendations: Dict[str, List[Dict[str, Any]]] = {}

# History is mirrored to data/history.json so it survives a server restart (like data/users.json).
HISTORY_FILE = Path(os.getenv("HISTORY_DB_FILE", str(BASE_DIR / "data" / "history.json")))
_history_lock = threading.Lock()


def _load_history() -> None:
    if not HISTORY_FILE.exists():
        return
    try:
        raw = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        for username, records in raw.items():
            user_recommendations[username] = list(records)[-MAX_HISTORY_PER_USER:]
        log.info("Loaded history for %d user(s) from %s", len(user_recommendations), HISTORY_FILE)
    except Exception as exc:  # a corrupted file must not stop the app
        log.error("Could not read %s: %s", HISTORY_FILE, exc)


def _save_history() -> None:
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(user_recommendations, default=str), encoding="utf-8")
        tmp.replace(HISTORY_FILE)
    except Exception:
        log.exception("Could not save history")


_load_history()


def _set_last(username: str, key: str, value: Dict[str, Any]) -> None:
    """Remember the latest planning request in the user's session data."""
    session = active_sessions.get(username)
    if session:
        session.user_data[key] = {"timestamp": utcnow().isoformat(), **value}


def _summarise_input(data: Dict[str, Any]) -> str:
    parts = []
    for key, val in data.items():
        if val in (None, "", False):
            continue
        parts.append(f"{key.replace('_', ' ')}: {'yes' if val is True else val}")
    return ", ".join(parts)


def _summarise_result(kind: str, result: Dict[str, Any]) -> str:
    spent, budget = result.get("total_spent", 0), result.get("total_budget", 0)
    return f"{kind.title()} plan: Rs {spent:,.0f} planned of Rs {budget:,.0f} budget"


def save_to_history(username: str, recommendation_type: str, input_data: Dict[str, Any],
                    result: Dict[str, Any]) -> str:
    """Store one recommendation for the user and return its id."""
    record = {
        "id": uuid.uuid4().hex,
        "timestamp": utcnow().isoformat(),
        "recommendation_type": recommendation_type,
        "input_summary": _summarise_input(input_data),
        "result_summary": _summarise_result(recommendation_type, result),
        "full_result": result,
    }
    with _history_lock:
        records = user_recommendations.setdefault(username, [])
        records.append(record)
        del records[:-MAX_HISTORY_PER_USER]  # keep memory bounded
        _save_history()
    return record["id"]


# ---- Activity 3.1: planner routes ----------------------------------------------------------
@app.post("/home-budget")
async def plan_home_budget(budget_input: HomeBudgetInput, request: Request,
                           current_user: UserInDB = Depends(get_current_active_user)):
    """Generate home budget recommendations."""
    _set_last(current_user.username, "last_home_budget", {
        "budget": budget_input.total_budget,
        "requirements": {"lights": budget_input.num_lights, "fans": budget_input.num_fans,
                         "furniture": budget_input.num_furniture,
                         "dining_tables": budget_input.num_dining_tables}})
    result = await get_home_recommendations_async(budget_input)
    result["id"] = save_to_history(current_user.username, "home", budget_input.model_dump(), result)
    return result


@app.post("/party-budget")
async def plan_party_budget(budget_input: PartyBudgetInput, request: Request,
                            current_user: UserInDB = Depends(get_current_active_user)):
    """Generate party budget recommendations."""
    _set_last(current_user.username, "last_party_budget", {
        "budget": budget_input.total_budget, "party_type": budget_input.party_type,
        "guests": budget_input.num_guests})
    result = await get_party_recommendations_async(budget_input)
    result["id"] = save_to_history(current_user.username, "party", budget_input.model_dump(), result)
    return result


@app.post("/jewelry-budget")
async def plan_jewelry_budget(
    request: Request,
    total_budget: float = Form(...),
    occasion: str = Form(...),
    preferences: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    current_user: UserInDB = Depends(get_current_active_user),
):
    """Generate jewelry recommendations from text, plus an optional outfit image."""
    try:
        budget_input = JewelryBudgetInput(total_budget=total_budget, occasion=occasion,
                                          preferences=preferences or None)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        raise HTTPException(status_code=422, detail=errors)

    has_image = bool(image and image.filename)
    image_path = await run_in_threadpool(save_upload_file, image) if has_image else None

    _set_last(current_user.username, "last_jewelry_budget", {
        "budget": budget_input.total_budget, "occasion": budget_input.occasion,
        "has_image": has_image})
    result = await get_jewelry_recommendations_async(budget_input, image_path)

    input_data = budget_input.model_dump()
    if has_image:
        input_data["image"] = image.filename
    result["id"] = save_to_history(current_user.username, "jewelry", input_data, result)
    return result


# the names used in the project document map to the same handlers
for _path, _handler in (("/generate-home", plan_home_budget), ("/generate-party", plan_party_budget),
                        ("/generate-jewelry", plan_jewelry_budget)):
    app.add_api_route(_path, _handler, methods=["POST"], include_in_schema=False)


# ---- Activity 3.2: history API -------------------------------------------------------------
@app.get("/recommendation-history")
async def get_recommendation_history(request: Request,
                                     current_user: UserInDB = Depends(get_current_active_user)):
    """The user's past recommendations, newest first (summaries only)."""
    records = sorted(user_recommendations.get(current_user.username, []),
                     key=lambda r: r["timestamp"], reverse=True)
    return {"history": [{"id": r["id"], "timestamp": r["timestamp"],
                         "type": r["recommendation_type"], "input": r["input_summary"],
                         "summary": r["result_summary"]} for r in records]}


@app.get("/recommendation-details/{recommendation_id}")
async def get_recommendation_details(recommendation_id: str, request: Request,
                                     current_user: UserInDB = Depends(get_current_active_user)):
    """Full details of one saved recommendation."""
    for r in user_recommendations.get(current_user.username, []):
        if r["id"] == recommendation_id:
            return {"id": r["id"], "timestamp": r["timestamp"], "type": r["recommendation_type"],
                    "input": r["input_summary"], "full_result": r["full_result"]}
    raise HTTPException(status_code=404, detail="Recommendation not found")


# ---- Activity 3.3: history page ------------------------------------------------------------
@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, current_user: UserInDB = Depends(get_current_active_user)):
    """History page to view past recommendations."""
    return render(request, "history.html", current_user)


if __name__ == "__main__":
    print("Starting PocketSmart: AI Budget Planner...")
    uvicorn.run("main:app", host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")), reload=False)