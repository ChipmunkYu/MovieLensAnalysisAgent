import json

from backend.config import rules_file


def load_rules() -> dict:
    with rules_file().open(encoding="utf-8") as stream:
        return json.load(stream)


def public_rules() -> dict:
    raw = load_rules()
    return {"version": raw["version"], "name": raw["name"], "rules": raw["rules"], "raw": raw}
