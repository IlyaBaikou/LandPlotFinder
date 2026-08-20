from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional


def rotating_detail_ids(
    source_ids: List[str],
    limit: int,
    *,
    slot: Optional[int] = None,
) -> List[str]:
    if not source_ids or limit <= 0:
        return []
    effective_slot = (
        slot
        if slot is not None
        else int(datetime.now(timezone.utc).timestamp() // (6 * 60 * 60))
    )
    count = min(limit, len(source_ids))
    start = (effective_slot * count) % len(source_ids)
    return [source_ids[(start + offset) % len(source_ids)] for offset in range(count)]
