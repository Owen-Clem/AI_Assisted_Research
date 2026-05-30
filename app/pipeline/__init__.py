import json
import re


def parse_json_response(text: str):
    """Parse JSON from a model response, tolerating markdown fences and trailing text."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```.*$", "", text, flags=re.DOTALL)
    # raw_decode stops at the first complete JSON object, ignoring any trailing content
    obj, _ = json.JSONDecoder().raw_decode(text.strip())
    return obj
