import json
import os
import re


def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(
            f"Required environment variable {key!r} is not set. "
            "Copy .env.example to .env and fill in all values."
        )
    return val


def parse_json_response(text: str):
    """Parse JSON from a model response, tolerating markdown fences and trailing text."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```.*$", "", text, flags=re.DOTALL)
    # raw_decode stops at the first complete JSON object, ignoring any trailing content
    obj, _ = json.JSONDecoder().raw_decode(text.strip())
    return obj
