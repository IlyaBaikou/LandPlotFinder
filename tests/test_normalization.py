from app.normalization import (
    canonical_url,
    classify_electricity,
    classify_gas,
    classify_internet,
    classify_sewerage,
    classify_water,
    detect_gas,
    extract_dimensions,
    extract_distance_km,
    extract_electricity_kw,
)


def test_extract_electricity_power_ignores_voltage() -> None:
    assert extract_electricity_kw("трёхфазное 380 В")[0] is None
    assert extract_electricity_kw("выделенная мощность 21,5 кВт")[0] == 21.5


def test_extract_distance_to_mkad() -> None:
    distance, evidence = extract_distance_km("Участок расположен в 31,5 км от МКАД")

    assert distance == 31.5
    assert evidence


def test_extract_dimensions() -> None:
    facade, depth, _ = extract_dimensions("Размер участка 25 x 40 метров")

    assert (facade, depth) == (25, 40)


def test_gas_detection_prefers_explicit_absence() -> None:
    present, _ = detect_gas(["газ по улице", "на участке газа нет"])

    assert present is False


def test_classifies_utility_location_from_description() -> None:
    assert classify_electricity(["Электричество 15 кВт подведено на участок"])[0] == (
        "На участке / подключено"
    )
    assert classify_gas(["Газ проходит по улице"])[0] == "По улице / рядом"
    assert classify_water(["На участке скважина"])[0] == "Скважина"
    assert classify_sewerage(["Установлен септик"])[0] == "Септик"


def test_classifies_internet_types_and_absence() -> None:
    assert classify_internet(["Подведено оптоволокно GPON"])[0] == (
        "Оптика / проводной"
    )
    assert classify_internet(["Уверенный мобильный интернет 4G"])[0] == (
        "Мобильный 4G/LTE"
    )
    assert classify_internet(["Интернета нет"])[0] == "Нет"
    assert classify_internet(["Связь не описана"])[0] is None


def test_canonical_url_drops_tracking_query_and_fragment() -> None:
    assert (
        canonical_url("HTTPS://EXAMPLE.TEST/path?utm_source=x#map")
        == "https://example.test/path"
    )
