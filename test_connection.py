"""Module 1 validation (Activity 1.3): API key, model, text-only, JSON and image+text calls.

Usage:
    python test_connection.py                 # uses an auto-generated sample outfit image
    python test_connection.py --image my.jpg  # use your own image
"""
import argparse
import sys
import traceback

import config
from gemini_client import GeminiError, get_service
from make_sample_image import make_sample_outfit
from prompts import jewelry_prompt

results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((name, True))
        print(f"[PASS] {name}" + (f"\n       {detail}" if detail else ""))
    except (GeminiError, ValueError) as exc:
        results.append((name, False))
        print(f"[FAIL] {name}\n       {exc}")
    except Exception:
        results.append((name, False))
        print(f"[FAIL] {name}")
        traceback.print_exc()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", help="Path to an outfit image (optional)")
    args = parser.parse_args()

    print(f"Configured model: {config.GEMINI_MODEL}\n")
    svc = {}

    def t_key():
        svc["s"] = get_service()
        return "API key loaded from .env"

    def t_text():
        out = svc["s"].generate_text("In one sentence, why is budgeting useful? Reply in plain text.")
        return f"model={svc['s'].last_model_used} | reply: {out.strip()[:150]}"

    def t_json():
        data = svc["s"].generate_json(
            'Return JSON {"city": "...", "currency": "..."} for Chennai, India.'
        )
        assert "city" in data and "currency" in data, f"unexpected JSON: {data}"
        return f"parsed JSON: {data}"

    def t_image():
        path = args.image or make_sample_outfit()
        data = svc["s"].generate_json(
            jewelry_prompt(5000, "Birthday", "minimal, silver", has_image=True), image=path
        )
        assert data.get("jewelry_recommendations"), "no jewelry_recommendations in reply"
        colors = data.get("outfit_analysis", {}).get("colors")
        first = data["jewelry_recommendations"][0]
        return f"outfit colours detected: {colors} | first item: {first.get('item_type')} (Rs {first.get('estimated_price')})"

    check("1. API key + client initialisation", t_key)
    if results[-1][1]:
        check("2. Text-only prompt", t_text)
        check("3. Structured JSON output", t_json)
        check("4. Multimodal (image + text) prompt", t_image)

    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} checks passed.")
    return 0 if passed == len(results) and len(results) == 4 else 1


if __name__ == "__main__":
    sys.exit(main())
