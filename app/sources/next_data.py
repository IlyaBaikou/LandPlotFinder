from __future__ import annotations

import json
import re
from typing import Any, Dict

NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def extract_next_data(html: str) -> Dict[str, Any]:
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError("Page does not contain __NEXT_DATA__")
    value = json.loads(match.group(1))
    if not isinstance(value, dict):
        raise ValueError("__NEXT_DATA__ root must be an object")
    return value
