"""PocketSmart AI - Module 2: FastAPI application (main.py).

Activity 2.1  app init, .env, CORS, static + templates
Activity 2.2  planner page routes (/home-planner, /party-planner, /jewelry-planner)
Activity 2.3  /login, /register, /logout pages + POST /register
Activity 2.4  /token, /session-info, /session-data, startup session cleanup

Module 3 (POST /home-budget, /party-budget, /jewelry-budget, /history ...) is added
below the marked line and uses the helpers from gemini_utils.py.
"""
import asyncio
import html
import logging
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import auth
from auth import (ACCESS_TOKEN_EXPIRE_MINUTES, COOKIE_NAME, COOKIE_SECURE, active_sessions,
                  authenticate_user, create_access_token, end_session, get_current_active_user,
                  get_current_user, get_token, register_user, start_session, utcnow)
from models import RegisterUser, Token, UserInDB

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

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render(request: Request, name: str, user: UserInDB | None = None, **ctx: Any):
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
# MODULE 3 (Member 3) - add the planner POST routes and /history below this line, e.g.
#   from gemini_utils import get_home_recommendations_async, save_upload_file
#   @app.post("/home-budget") ... uses get_current_active_user + active_sessions
# ======================================================================================


if __name__ == "__main__":
    print("Starting PocketSmart: AI Budget Planner...")
    uvicorn.run("main:app", host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")), reload=False)