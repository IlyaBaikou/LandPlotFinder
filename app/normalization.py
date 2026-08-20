from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

KW_PATTERNS = [
    re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?:квт|kw)\b", re.IGNORECASE),
    re.compile(
        r"(?:мощност\w*|выделен\w*|электричеств\w*)[^\d]{0,25}"
        r"(?P<value>\d+(?:[.,]\d+)?)\s*(?:квт|kw)\b",
        re.IGNORECASE,
    ),
]
DISTANCE_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*км[^\n.]{0,30}(?:от\s+)?мкад",
    re.IGNORECASE,
)
DIMENSIONS_RE = re.compile(
    r"(?P<a>\d+(?:[.,]\d+)?)\s*[xх×*]\s*(?P<b>\d+(?:[.,]\d+)?)\s*(?:м\b|метр)",
    re.IGNORECASE,
)


def plain_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return " ".join(BeautifulSoup(value, "html.parser").get_text(" ").split())


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", ""))


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d,.\-]", "", str(value)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_electricity_kw(*texts: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    combined = " ".join(text for text in texts if text)
    if not combined:
        return None, None
    matches = []
    for pattern in KW_PATTERNS:
        for match in pattern.finditer(combined):
            value = parse_float(match.group("value"))
            if value is not None and 1 <= value <= 500:
                matches.append((value, _evidence(combined, match.start(), match.end())))
    if not matches:
        return None, None
    return max(matches, key=lambda item: item[0])


def extract_distance_km(*texts: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    combined = " ".join(text for text in texts if text)
    match = DISTANCE_RE.search(combined)
    if not match:
        return None, None
    return parse_float(match.group("value")), _evidence(combined, match.start(), match.end())


def extract_dimensions(
    *texts: Optional[str],
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    combined = " ".join(text for text in texts if text)
    match = DIMENSIONS_RE.search(combined)
    if not match:
        return None, None, None
    a = parse_float(match.group("a"))
    b = parse_float(match.group("b"))
    if not a or not b or a > 500 or b > 500:
        return None, None, None
    return min(a, b), max(a, b), _evidence(combined, match.start(), match.end())


def detect_gas(texts: Iterable[Optional[str]]) -> Tuple[Optional[bool], Optional[str]]:
    status, evidence = classify_gas(texts)
    if status == "Нет":
        return False, evidence
    if status:
        return True, evidence
    return None, None


def classify_gas(texts: Iterable[Optional[str]]) -> Tuple[Optional[str], Optional[str]]:
    combined = " ".join(text for text in texts if text)
    if not combined:
        return None, None
    patterns = [
        (
            "Нет",
            [
                r"газ[а]?\s+нет",
                r"без\s+газа",
                r"газ\s+отсутств",
                r"газ\s+не\s+проведен",
            ],
        ),
        (
            "На участке / в доме",
            [
                r"газ[^\n.]{0,35}(?:на\s+участке|в\s+доме|подвед\w*)",
                r"(?:на\s+участке|в\s+доме)[^\n.]{0,25}газ",
            ],
        ),
        (
            "По улице / рядом",
            [
                r"газ[^\n.]{0,35}(?:по\s+улице|по\s+границе|рядом|вдоль)",
                r"(?:по\s+улице|по\s+границе|рядом)[^\n.]{0,25}газ",
            ],
        ),
        (
            "Есть, расположение не указано",
            [r"газоснабжение", r"газ\s+(?:есть|имеется)"],
        ),
    ]
    for label, label_patterns in patterns:
        for pattern in label_patterns:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return label, _evidence(combined, match.start(), match.end())
    return None, None


def classify_electricity(
    texts: Iterable[Optional[str]],
) -> Tuple[Optional[str], Optional[str]]:
    return _classify_utility(
        texts,
        [
            (
                "Нет",
                [
                    r"электричеств\w*\s+нет",
                    r"без\s+электричества",
                    r"свет\w*\s+нет",
                ],
            ),
            (
                "На участке / подключено",
                [
                    r"(?:электричество|свет)[^\n.]{0,40}(?:на\s+участке|в\s+доме|подвед\w*|подключ\w*)",
                    r"(?:на\s+участке|в\s+доме)[^\n.]{0,25}(?:электричество|свет)",
                ],
            ),
            (
                "По улице / рядом",
                [
                    r"(?:электричество|свет)[^\n.]{0,40}(?:по\s+улице|по\s+границе|по\s+меже|рядом)",
                ],
            ),
            (
                "Есть, мощность не указана",
                [
                    r"электричество\s+(?:есть|имеется)",
                    r"свет\s+(?:есть|имеется)",
                ],
            ),
        ],
    )


def classify_water(texts: Iterable[Optional[str]]) -> Tuple[Optional[str], Optional[str]]:
    return _classify_utility(
        texts,
        [
            ("Нет", [r"вод\w*\s+нет", r"без\s+воды"]),
            ("Центральная", [r"централ\w*[^\n.]{0,20}вод", r"водопровод"]),
            ("Скважина", [r"скважин"]),
            ("Колодец", [r"колодец", r"колодц"]),
            ("Сезонная", [r"сезонн\w*[^\n.]{0,20}вод", r"вода[^\n.]{0,20}сезонн"]),
        ],
    )


def classify_sewerage(
    texts: Iterable[Optional[str]],
) -> Tuple[Optional[str], Optional[str]]:
    return _classify_utility(
        texts,
        [
            ("Нет", [r"канализац\w*\s+нет", r"без\s+канализац"]),
            ("Центральная", [r"централ\w*[^\n.]{0,25}канализац"]),
            ("Септик", [r"септик"]),
            (
                "Местная / автономная",
                [
                    r"местн\w*[^\n.]{0,20}канализац",
                    r"автономн\w*[^\n.]{0,20}канализац",
                ],
            ),
        ],
    )


def _classify_utility(
    texts: Iterable[Optional[str]],
    labels_and_patterns: Iterable[Tuple[str, Iterable[str]]],
) -> Tuple[Optional[str], Optional[str]]:
    combined = " ".join(text for text in texts if text)
    if not combined:
        return None, None
    for label, patterns in labels_and_patterns:
        for pattern in patterns:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return label, _evidence(combined, match.start(), match.end())
    return None, None


def detect_electricity_absence(
    texts: Iterable[Optional[str]],
) -> Tuple[Optional[bool], Optional[str]]:
    combined = " ".join(text for text in texts if text)
    if not combined:
        return None, None
    patterns = [
        r"электричеств\w*\s+нет",
        r"без\s+электричества",
        r"свет\w*\s+нет",
        r"электричеств\w*\s+отсутств",
    ]
    for pattern in patterns:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            return True, _evidence(combined, match.start(), match.end())
    return False, None


def classify_internet(
    texts: Iterable[Optional[str]],
) -> Tuple[Optional[str], Optional[str]]:
    combined = " ".join(text for text in texts if text)
    if not combined:
        return None, None

    patterns = [
        (
            "Нет",
            [
                r"интернет[а]?\s+нет",
                r"без\s+интернета",
                r"интернет\s+отсутств",
                r"интернет\s+не\s+подключ",
            ],
        ),
        (
            "Оптика / проводной",
            [
                r"оптоволок",
                r"\bоптик[аи]\b",
                r"\bgpon\b",
                r"проводн\w*\s+интернет",
            ],
        ),
        (
            "Мобильный 4G/LTE",
            [
                r"\b4g\b",
                r"\blte\b",
                r"\b5g\b",
                r"мобильн\w*\s+интернет",
            ],
        ),
        (
            "Есть, тип не указан",
            [
                r"\bинтернет\b",
                r"\bwi[\s-]?fi\b",
                r"\bвай[\s-]?фай\b",
            ],
        ),
    ]
    for label, label_patterns in patterns:
        for pattern in label_patterns:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return label, _evidence(combined, match.start(), match.end())
    return None, None


def normalized_ownership(*texts: Optional[str]) -> Optional[str]:
    combined = " ".join(text for text in texts if text).lower()
    if not combined:
        return None
    if "частн" in combined and "собствен" in combined:
        return "частная собственность"
    if "пожизн" in combined and "наслед" in combined:
        return "пожизненное наследуемое владение"
    if "аренд" in combined:
        return "аренда"
    if "временн" in combined and "пользован" in combined:
        return "временное пользование"
    return None


def content_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence(text: str, start: int, end: int, radius: int = 80) -> str:
    snippet = text[max(0, start - radius) : min(len(text), end + radius)]
    return " ".join(snippet.split())
