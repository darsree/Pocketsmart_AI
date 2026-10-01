"""Lists the Gemini models your API key can actually use (Activity 1.1 / 1.2)."""
from google import genai
import config


def main() -> None:
    client = genai.Client(api_key=config.get_api_key())
    print("Models available to your key that support generateContent:\n")
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        name = m.name.replace("models/", "")
        if "gemini" in name and (not actions or "generateContent" in actions):
            print(" -", name)
    print("\nPut your choice in .env as GEMINI_MODEL=<name>")


if __name__ == "__main__":
    main()
