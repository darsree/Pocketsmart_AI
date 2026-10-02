"""PocketSmart AI - Module 2: authentication + session handling (auth.py).

Covers Activity 2.1 (settings), 2.3 (register / login / logout) and 2.4 (/token, sessions).
Users are kept in memory and mirrored to data/users.json so accounts survive a restart.
Active sessions and the token blacklist are in memory only.
"""
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Set

import bcrypt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

from models import RegisterUser, UserInDB, UserSession

load_dotenv()
log = logging.getLogger("pocketsmart.auth")

BASE_DIR = Path(__file__).resolve().parent

# ---- settings -----------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY or SECRET_KEY == "your_secret_key":
    SECRET_KEY = secrets.token_hex(32)
    log.warning("SECRET_KEY not set in .env - using a random one. Logins will reset on every restart.")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
SESSION_IDLE_SECONDS = ACCESS_TOKEN_EXPIRE_MINUTES * 60
COOKIE_NAME = "access_token"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"  # set true behind HTTPS

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)  # enables Swagger "Authorize"

# ---- in-memory stores -----------------------------------------------------------------
users_db: Dict[str, UserInDB] = {}
active_sessions: Dict[str, UserSession] = {}
blacklisted_tokens: Set[str] = set()

USERS_FILE = Path(os.getenv("USERS_DB_FILE", str(BASE_DIR / "data" / "users.json")))
_users_lock = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- user persistence -----------------------------------------------------------------
def load_users() -> None:
    if not USERS_FILE.exists():
        return
    try:
        raw = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        for username, data in raw.items():
            users_db[username] = UserInDB(**data)
        log.info("Loaded %d user(s) from %s", len(users_db), USERS_FILE)
    except Exception as exc:  # corrupted file must not stop the app
        log.error("Could not read %s: %s", USERS_FILE, exc)


def _save_users() -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {name: u.model_dump() for name, u in users_db.items()}
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(USERS_FILE)


# ---- passwords ------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    raw = plain.encode("utf-8")
    if len(raw) > 72:  # bcrypt limit
        raise HTTPException(status_code=400, detail="Password is too long (max 72 bytes)")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except ValueError:
        return False


_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt()).decode("utf-8")


def get_user(username: str) -> Optional[UserInDB]:
    return users_db.get((username or "").strip().lower())


def register_user(data: RegisterUser) -> UserInDB:
    username = data.username.lower()
    email = str(data.email).lower()
    with _users_lock:
        if username in users_db:
            raise HTTPException(status_code=409, detail="Username already exists")
        if any(u.email.lower() == email for u in users_db.values()):
            raise HTTPException(status_code=409, detail="Email already registered")
        user = UserInDB(
            username=username,
            email=email,
            full_name=data.full_name,
            hashed_password=hash_password(data.password),
            created_at=utcnow().isoformat(),
        )
        users_db[username] = user
        _save_users()
    return user


def authenticate_user(username: str, password: str) -> Optional[UserInDB]:
    user = get_user(username)
    if not user:
        verify_password(password, _DUMMY_HASH)  # keeps timing similar for unknown users
        return None
    if not verify_password(password, user.hashed_password) or user.disabled:
        return None
    return user


# ---- tokens ---------------------------------------------------------------------------
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    payload = data.copy()
    payload["exp"] = utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    payload["jti"] = secrets.token_hex(8)  # unique id: two logins in the same second must differ
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


async def get_token(request: Request) -> Optional[str]:
    """Token from the httponly cookie first, then the Authorization header."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    if token and token.lower().startswith("bearer "):
        token = token[7:]
    return token.strip() if token else None


async def get_current_user(request: Request, token: Optional[str] = None) -> Optional[UserInDB]:
    """Return the user for a valid, non-blacklisted token, otherwise None (never raises)."""
    token = token or await get_token(request)
    if not token or token in blacklisted_tokens:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    user = users_db.get(payload.get("sub", ""))
    if not user or user.disabled:
        return None
    return user


async def get_current_active_user(
    request: Request, _swagger_token: Optional[str] = Depends(oauth2_scheme)
) -> UserInDB:
    """FastAPI dependency for protected routes (401 if not logged in)."""
    token = await get_token(request) or _swagger_token
    user = await get_current_user(request, token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    now = utcnow()
    session = active_sessions.get(user.username)
    if session is None or session.token != token:
        # valid token but no session (e.g. cleaned up) -> recreate, keeping any old data
        active_sessions[user.username] = UserSession(
            username=user.username, login_time=now, last_activity=now, token=token,
            user_data=session.user_data if session else {},
        )
    else:
        session.last_activity = now
    return user


# ---- sessions -------------------------------------------------------------------------
def start_session(user: UserInDB, token: str) -> UserSession:
    """Create/replace the user's session; the previous token (if any) is blacklisted."""
    now = utcnow()
    existing = active_sessions.get(user.username)
    user_data = existing.user_data if existing else {}
    if existing:
        blacklisted_tokens.add(existing.token)
    session = UserSession(username=user.username, login_time=now, last_activity=now,
                          token=token, user_data=user_data)
    active_sessions[user.username] = session
    return session


def end_session(token: str) -> None:
    blacklisted_tokens.add(token)
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM],
                             options={"verify_exp": False})
        active_sessions.pop(payload.get("sub", ""), None)
    except JWTError:
        pass


def purge_expired_sessions() -> int:
    """Drop idle sessions and blacklist entries whose token has expired anyway."""
    now = utcnow()
    expired = [u for u, s in active_sessions.items()
               if (now - s.last_activity).total_seconds() > SESSION_IDLE_SECONDS]
    for username in expired:
        log.info("Removing expired session for %s", username)
        active_sessions.pop(username, None)
    for tok in list(blacklisted_tokens):
        try:
            jwt.decode(tok, SECRET_KEY, algorithms=[ALGORITHM])
        except JWTError:  # expired or invalid -> no need to remember it
            blacklisted_tokens.discard(tok)
    return len(expired)


load_users()