from __future__ import annotations

from typing import List

from app.config import SearchProfile
from app.domain import MatchStatus, NormalizedListing
from app.geo import distance_to_mkad_km
from app.normalization import (
    classify_electricity,
    classify_gas,
    classify_internet,
    classify_sewerage,
    classify_water,
    detect_electricity_absence,
    detect_gas,
    extract_electricity_kw,
)


def evaluate_listing(listing: NormalizedListing, profile: SearchProfile) -> NormalizedListing:
    _enrich_from_description_and_coordinates(listing)
    rejects: List[str] = []
    reviews: List[str] = list(listing.reasons)
    near_misses: List[str] = []

    combined_text = f"{listing.title} {listing.description}".lower()
    if listing.raw_payload.get("is_auction") or "аукцион" in combined_text:
        listing.raw_payload["is_auction"] = True
        listing.sale_format = "Аукцион"
        auction_reason = (
            "Аукцион: цена является стартовой и может вырасти; "
            "проверить задаток и срок подачи заявки"
        )
        if auction_reason not in reviews:
            reviews.append(auction_reason)

    if listing.raw_payload.get("outside_target_region"):
        rejects.append("Объект находится вне Минской области")

    if listing.price_usd is None:
        rejects.append("Цена в USD неизвестна — строгий лимит нельзя подтвердить")
    elif listing.price_usd > profile.discovery_max_price_usd:
        rejects.append(
            f"Цена выше широкого лимита ${profile.discovery_max_price_usd:,.0f}"
        )
    elif listing.price_usd > profile.max_price_usd:
        near_misses.append(
            f"Цена выше строгого максимума ${profile.max_price_usd:,.0f}, "
            f"но не выше ${profile.discovery_max_price_usd:,.0f}"
        )
    elif listing.price_usd < 10_000:
        reviews.append("Цена ниже $10 000 — проверить полноту объявления и скрытые риски")

    if listing.area_sotok is None:
        rejects.append("Площадь участка неизвестна — минимум 9 соток не подтверждён")
    elif not (
        profile.discovery_min_area_sotok
        <= listing.area_sotok
        <= profile.discovery_max_area_sotok
    ):
        rejects.append(
            f"Площадь {listing.area_sotok:g} сот. вне широкого диапазона "
            f"{profile.discovery_min_area_sotok:g}–{profile.discovery_max_area_sotok:g}"
        )
    elif not profile.min_area_sotok <= listing.area_sotok <= profile.max_area_sotok:
        near_misses.append(
            f"Площадь {listing.area_sotok:g} сот. вне диапазона "
            f"{profile.min_area_sotok:g}–{profile.max_area_sotok:g}"
        )

    if listing.distance_mkad_km is None:
        rejects.append("Расстояние от МКАД неизвестно — лимит 30 км не подтверждён")
    elif listing.distance_mkad_km > profile.discovery_max_distance_km:
        rejects.append(
            f"Расстояние {listing.distance_mkad_km:g} км больше "
            f"широкого лимита {profile.discovery_max_distance_km:g} км"
        )
    elif listing.distance_mkad_km > profile.max_distance_km:
        near_misses.append(
            f"Расстояние {listing.distance_mkad_km:g} км больше строгого лимита "
            f"{profile.max_distance_km:g} км"
        )

    texts = [
        listing.electricity_raw,
        listing.gas_raw,
        listing.description,
    ]
    gas_present, gas_evidence = detect_gas(texts)
    electricity_absent, electricity_evidence = detect_electricity_absence(texts)
    if gas_evidence:
        listing.evidence.setdefault("gas", gas_evidence)
    if electricity_evidence:
        listing.evidence.setdefault("electricity", electricity_evidence)

    electricity_kw = listing.electricity_kw
    communications_ok = False
    if electricity_kw is not None and electricity_kw >= profile.primary_electricity_kw:
        communications_ok = True
    elif (
        electricity_kw is not None
        and electricity_kw >= profile.secondary_electricity_kw
        and gas_present is True
    ):
        communications_ok = True
    elif electricity_absent is True and gas_present is False:
        rejects.append("Электричество и газ явно отсутствуют")
    elif electricity_kw is None:
        reviews.append("Не указана выделенная электрическая мощность")
    elif electricity_kw < profile.secondary_electricity_kw:
        reviews.append(
            f"Указано только {electricity_kw:g} кВт — требуется проверка возможности увеличения"
        )
    elif gas_present is not True:
        reviews.append(
            f"Электричество {electricity_kw:g} кВт меньше "
            f"{profile.primary_electricity_kw:g} кВт, газ по улице не подтверждён"
        )

    ownership = (listing.ownership_raw or "").lower()
    if not ownership:
        reviews.append("Право на землю не указано")
    elif "частн" in ownership and "собствен" in ownership:
        pass
    elif "пожизн" in ownership:
        reviews.append("Пожизненное наследуемое владение — проверить переход права")
    elif "аренд" in ownership:
        reviews.append("Аренда земли — проверить срок, плату и переход права")
    else:
        reviews.append(f"Право на землю требует проверки: {listing.ownership_raw}")

    internet_status, internet_evidence = classify_internet(
        [listing.internet_raw, listing.description]
    )
    if internet_status:
        listing.internet_raw = internet_status
    if internet_evidence:
        listing.evidence.setdefault("internet", internet_evidence)
    if internet_status == "Нет":
        reviews.append(
            "Интернет явно отсутствует — проверить мобильное покрытие и возможность подключения"
        )
    if listing.house_risks:
        reviews.append(f"Дом: {listing.house_risks}")

    listing.score = _score(listing, profile, communications_ok)
    if rejects:
        listing.status = MatchStatus.REJECT
        listing.reasons = rejects + near_misses + reviews
    elif near_misses:
        if len(near_misses) == 1 or listing.score >= 60:
            listing.status = MatchStatus.INTERESTING
        else:
            listing.status = MatchStatus.REJECT
        listing.reasons = near_misses + reviews
    elif reviews:
        listing.status = MatchStatus.REVIEW
        listing.reasons = reviews
    else:
        listing.status = MatchStatus.MATCH
        listing.reasons = []
    return listing


def _enrich_from_description_and_coordinates(listing: NormalizedListing) -> None:
    if (
        listing.distance_mkad_km is None
        and listing.latitude is not None
        and listing.longitude is not None
    ):
        listing.distance_mkad_km = distance_to_mkad_km(
            listing.latitude,
            listing.longitude,
        )
        listing.evidence["distance"] = (
            "По прямой до контура МКАД по координатам "
            f"{listing.latitude:.5f}, {listing.longitude:.5f}"
        )

    electricity_kw, electricity_evidence = extract_electricity_kw(
        listing.electricity_raw,
        listing.description,
    )
    if listing.electricity_kw is None and electricity_kw is not None:
        listing.electricity_kw = electricity_kw
    electricity_status, electricity_status_evidence = classify_electricity(
        [listing.description]
    )
    if not listing.electricity_raw and electricity_status:
        listing.electricity_raw = electricity_status
    if electricity_evidence or electricity_status_evidence:
        listing.evidence.setdefault(
            "electricity",
            electricity_evidence or electricity_status_evidence or "",
        )

    _fill_utility(
        listing,
        "gas_raw",
        "gas",
        classify_gas([listing.description]),
    )
    _fill_utility(
        listing,
        "water_raw",
        "water",
        classify_water([listing.description]),
    )
    _fill_utility(
        listing,
        "sewerage_raw",
        "sewerage",
        classify_sewerage([listing.description]),
    )


def _fill_utility(
    listing: NormalizedListing,
    field: str,
    evidence_key: str,
    classified: tuple,
) -> None:
    value, evidence = classified
    if not getattr(listing, field) and value:
        setattr(listing, field, value)
    if evidence:
        listing.evidence.setdefault(evidence_key, evidence)


def _score(
    listing: NormalizedListing,
    profile: SearchProfile,
    communications_ok: bool,
) -> int:
    price_score = 0
    if listing.price_usd is not None:
        if listing.price_usd <= profile.target_price_usd:
            price_score = 5
        elif listing.price_usd <= 25_000:
            price_score = 4
        elif listing.price_usd <= profile.base_budget_usd:
            price_score = 3
        elif listing.price_usd <= profile.max_price_usd:
            price_score = 2
        elif listing.price_usd <= profile.discovery_max_price_usd:
            price_score = 1

    area_score = 0
    if listing.area_sotok is not None:
        delta = abs(listing.area_sotok - profile.target_area_sotok)
        area_score = (
            5
            if delta <= 0.25
            else 4
            if delta <= 0.5
            else 3
            if delta <= 1
            else 2
            if delta <= 2
            else 1
            if delta <= 5
            else 0
        )

    distance_score = 0
    if listing.distance_mkad_km is not None:
        if listing.distance_mkad_km <= 20:
            distance_score = 5
        elif listing.distance_mkad_km <= profile.preferred_distance_km:
            distance_score = 4
        elif listing.distance_mkad_km <= profile.max_distance_km:
            distance_score = 3
        elif listing.distance_mkad_km <= profile.discovery_max_distance_km:
            distance_score = 2

    communication_score = 5 if communications_ok else 2 if listing.electricity_raw else 0

    ownership = (listing.ownership_raw or "").lower()
    if "частн" in ownership and "собствен" in ownership:
        ownership_score = 5
    elif "пожизн" in ownership:
        ownership_score = 3
    elif "аренд" in ownership:
        ownership_score = 2
    else:
        ownership_score = 1

    weighted = (
        price_score * 0.40
        + area_score * 0.20
        + distance_score * 0.15
        + communication_score * 0.20
        + ownership_score * 0.05
    )
    return round(weighted / 5 * 100)
