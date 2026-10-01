"""Reusable Gemini service for PocketSmart AI (text, JSON and text+image calls).

Other modules use it like this:

    from gemini_client import get_service
    svc = get_service()
    text = svc.generate_text("Suggest 3 budget sofas in India under Rs 20000")
    data = svc.generate_json(prompt)                       # returns a Python dict
    data = svc.generate_json(prompt, image="outfit.jpg")   # multimodal (Jewelry Planner)
"""
import io
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

from google import genai
from google.genai import errors, types
from PIL import Image

import config

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("pocketsmart.gemini")
ImageInput = Union[str, Path, bytes, Image.Image]

SYSTEM_INSTRUCTION = (
    "You are PocketSmart AI, a budget-aware shopping and planning assistant for the Indian market. "
    "All prices are in INR (Rs). Never exceed the user's budget. Prefer products and services "
    "available on Indian platforms (Amazon.in, Flipkart, IKEA India, Swiggy, Zomato, OYO, etc.). "
    "Be specific, practical and concise."
)


class GeminiError(RuntimeError):
    """Raised for any failure talking to Gemini, with a human-friendly message."""


class _RateLimiter:
    """Very small client-side limiter so we stay inside free-tier quotas."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.interval:
                time.sleep(self.interval - delta)
            self._last = time.monotonic()


def prepare_image(image: ImageInput) -> types.Part:
    """Validate, downscale and convert an image into a Gemini-ready Part."""
    try:
        if isinstance(image, Image.Image):
            img = image
        elif isinstance(image, (str, Path)):
            path = Path(image)
            if not path.exists():
                raise GeminiError(f"Image file not found: {path}")
            img = Image.open(path)
        elif isinstance(image, (bytes, bytearray)):
            img = Image.open(io.BytesIO(image))
        else:
            raise GeminiError(f"Unsupported image input type: {type(image).__name__}")
        img.load()
    except GeminiError:
        raise
    except Exception as exc:
        raise GeminiError(f"Could not read the image: {exc}") from exc

    img = img.convert("RGB")
    img.thumbnail((config.MAX_IMAGE_SIDE, config.MAX_IMAGE_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")


def extract_json(text: str) -> dict:
    """Parse JSON from a model reply, tolerating ```json fences or extra prose."""
    cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise GeminiError("Gemini did not return valid JSON:\n" + text[:500])


class GeminiService:
    def __init__(self) -> None:
        self.client = genai.Client(
            api_key=config.get_api_key(),
            http_options=types.HttpOptions(timeout=config.REQUEST_TIMEOUT * 1000),  # milliseconds
        )
        self.models = [config.GEMINI_MODEL] + [
            m for m in config.FALLBACK_MODELS if m != config.GEMINI_MODEL
        ]
        self.last_model_used: Optional[str] = None
        self._limiter = _RateLimiter(config.MAX_CALLS_PER_MINUTE)

    # ---- core call with retry + model fallback -------------------------------------
    def _generate(self, contents: Any, *, system_instruction: Optional[str] = None,
                  json_mode: bool = False, temperature: Optional[float] = None) -> str:
        cfg = types.GenerateContentConfig(
            system_instruction=system_instruction or SYSTEM_INSTRUCTION,
            temperature=config.TEMPERATURE if temperature is None else temperature,
            max_output_tokens=config.MAX_OUTPUT_TOKENS,
            response_mime_type="application/json" if json_mode else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        last_error: Optional[Exception] = None

        for model in self.models:
            for attempt in range(1, config.MAX_RETRIES + 1):
                self._limiter.wait()
                try:
                    resp = self.client.models.generate_content(
                        model=model, contents=contents, config=cfg
                    )
                    text = resp.text
                    if not text:
                        raise GeminiError("Gemini returned an empty reply (possibly blocked by safety filters).")
                    self.last_model_used = model
                    return text
                except errors.APIError as exc:
                    last_error = exc
                    code = getattr(exc, "code", None)
                    if code in (401, 403) or (code == 400 and "API key" in str(exc)):
                        raise GeminiError(
                            "Gemini rejected the API key. Check GOOGLE_API_KEY in .env "
                            "(create a new key at https://aistudio.google.com/apikey)."
                        ) from exc
                    if code == 404:
                        log.warning("Model %s not available, trying next fallback.", model)
                        break  # go to next model
                    if code in (429, 500, 502, 503, 504):
                        if attempt < config.MAX_RETRIES:
                            delay = 2 ** attempt
                            log.warning("Gemini %s on %s (attempt %d). Retrying in %ds.", code, model, attempt, delay)
                            time.sleep(delay)
                            continue
                        log.warning("Gemini %s on %s persists - switching to next model.", code, model)
                        break  # overloaded / quota: try the next model instead of failing
                    raise GeminiError(f"Gemini API error ({code}): {exc}") from exc
                except GeminiError:
                    raise
                except Exception as exc:  # network errors, timeouts etc.
                    last_error = exc
                    log.warning("Network/timeout error on %s (%s) - switching to next model.", model, type(exc).__name__)
                    break

        raise GeminiError(
            f"All models failed ({', '.join(self.models)}). Last error: {last_error}. "
            "Run `python list_models.py` and update GEMINI_MODEL in .env."
        )

    # ---- public helpers ------------------------------------------------------------
    def generate_text(self, prompt: str, image: Optional[ImageInput] = None, **kw) -> str:
        contents: Any = [prompt, prepare_image(image)] if image is not None else prompt
        return self._generate(contents, **kw)

    def generate_json(self, prompt: str, image: Optional[ImageInput] = None, **kw) -> dict:
        contents: Any = [prompt, prepare_image(image)] if image is not None else prompt
        return extract_json(self._generate(contents, json_mode=True, **kw))


_service: Optional[GeminiService] = None


def get_service() -> GeminiService:
    """Shared singleton - import this from FastAPI routes / services."""
    global _service
    if _service is None:
        _service = GeminiService()
    return _service