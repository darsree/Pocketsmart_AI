"""Central configuration for PocketSmart AI - Module 1 (Gemini AI Initialization).

Every other module (FastAPI backend, planners, UI) should import settings from here
instead of reading environment variables on its own.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def get_api_key() -> str:
    """Return the Gemini API key from .env (GOOGLE_API_KEY or GEMINI_API_KEY)."""
    key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key or key.strip().lower().startswith("your_"):
        raise ValueError(
            "No Gemini API key found. Copy .env.example to .env and set "
            "GOOGLE_API_KEY=<your key> (get one at https://aistudio.google.com/apikey)."
        )
    return key.strip()


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.7-flash,gemini-3.5-flash,gemini-2.5-flash",
    ).split(",")
    if m.strip()
]

TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.4"))
MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "8192"))
MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
MAX_CALLS_PER_MINUTE = int(os.getenv("GEMINI_MAX_CALLS_PER_MINUTE", "10"))
REQUEST_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "45"))  # per request, so a call can never hang forever
MAX_IMAGE_SIDE = 1024  # images are downscaled to this many pixels before upload