from __future__ import annotations

from typing import Any, Iterable, Optional

from app.normalization import plain_text


def join_house_condition(*values: Any) -> Optional[str]:
    parts = []
    seen = set()
    for value in values:
        text = plain_text(str(value)) if value not in (None, "") else ""
        if not text or text.isdigit() or text.lower() in seen:
            continue
        seen.add(text.lower())
        parts.append(text)
    return "; ".join(parts) or None


def detect_house_risks(texts: Iterable[Optional[str]]) -> Optional[str]:
    combined = " ".join(value for value in texts if value).lower().replace("ё", "е")
    if not combined:
        return None

    risks = []
    rules = [
        (
            ("под снос", "требует сноса", "на снос"),
            "Возможен или прямо указан снос дома",
        ),
        (
            ("ветхий", "аварийн", "непригоден для проживания"),
            "Ветхое или аварийное состояние",
        ),
        (
            (
                "без документов",
                "не зарегистрирован",
                "самовольн",
                "не узаконен",
            ),
            "Проверить документы и регистрацию строения",
        ),
        (
            ("незавершен", "незавершенное", "законсервирован"),
            "Незавершённое или законсервированное строение",
        ),
        (
            ("капитальный ремонт", "требует ремонта", "под реконструкцию"),
            "Нужен существенный ремонт или реконструкция",
        ),
    ]
    for needles, label in rules:
        if any(needle in combined for needle in needles):
            risks.append(label)
    return "; ".join(risks) or None
