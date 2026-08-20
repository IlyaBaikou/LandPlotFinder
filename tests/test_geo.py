from app.geo import MKAD_CONTOUR, distance_to_mkad_km


def test_distance_to_mkad_is_zero_inside_ring() -> None:
    assert distance_to_mkad_km(53.9006, 27.5590) == 0


def test_distance_to_mkad_is_zero_on_contour() -> None:
    assert distance_to_mkad_km(*MKAD_CONTOUR[0]) == 0


def test_distance_to_mkad_uses_nearest_contour_segment() -> None:
    assert distance_to_mkad_km(54.002, 27.675) == 5.6
