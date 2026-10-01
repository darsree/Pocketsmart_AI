# PocketSmart AI - Module 1: Gemini AI Setup & Initialization

Files:
- `config.py` - loads .env, model name, limits
- `gemini_client.py` - reusable Gemini service (text, JSON, image+text, retries, model fallback, rate limit)
- `prompts.py` - Home / Party / Jewelry prompt templates
- `list_models.py` - shows models your key can use
- `make_sample_image.py` - makes a test outfit image
- `test_connection.py` - validates everything

Quick start (Windows):
    python -m venv venv
    venv\Scripts\activate
    pip install -r requirements.txt
    copy .env.example .env      (then edit .env and paste your key)
    python list_models.py
    python test_connection.py

macOS/Linux: `source venv/bin/activate` and `cp .env.example .env`.
