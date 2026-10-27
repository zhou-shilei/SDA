"""Project-local switch for reusable inference results, separate from model weights."""

import json
import os
from pathlib import Path


POLICY_PATH = Path(__file__).resolve().parents[2] / ".studio/cache_policy.json"


def cache_enabled():
    value = os.environ.get("SDA_CACHE_ENABLED")
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if not POLICY_PATH.is_file():
        return True
    try:
        return json.loads(POLICY_PATH.read_text(encoding="utf-8")).get("enabled") is True
    except (OSError, ValueError, AttributeError):
        return False
