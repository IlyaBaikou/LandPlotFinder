from urllib.parse import parse_qs, urlsplit

from app.integrations.trips import (
    TripPoint,
    build_trip_routes,
    google_maps_route_url,
    parse_coordinates,
)


def point(index: int, latitude: float, longitude: float) -> TripPoint:
    return TripPoint(
        external_id=f"realt:{index}",
        status="Изучаем",
        source="Realt",
        listing_url=f"https://example.test/{index}",
        district="Минский",
        place=f"Локация {index}",
        price="$20,000",
        area="10",
        distance="20",
        latitude=latitude,
        longitude=longitude,
        rating="80",
        location_score="70",
    )


def test_builds_mobile_routes_with_at_most_four_objects() -> None:
    points = [
        point(1, 54.0, 27.5),
        point(2, 54.1, 27.6),
        point(3, 53.8, 27.8),
        point(4, 53.7, 27.4),
        point(5, 53.9, 27.9),
    ]

    routes = build_trip_routes(points)

    assert [len(route) for route in routes] == [4, 1]
    assert {item.external_id for route in routes for item in route} == {
        item.external_id for item in points
    }


def test_google_maps_route_uses_three_waypoints_and_destination() -> None:
    points = [
        point(1, 54.0, 27.5),
        point(2, 54.1, 27.6),
        point(3, 53.8, 27.8),
        point(4, 53.7, 27.4),
    ]

    query = parse_qs(urlsplit(google_maps_route_url(points)).query)

    assert query["api"] == ["1"]
    assert query["destination"] == [points[-1].coordinates]
    assert query["travelmode"] == ["driving"]
    assert query["waypoints"] == [
        "|".join(item.coordinates for item in points[:-1])
    ]


def test_parses_only_valid_coordinates() -> None:
    assert parse_coordinates("53.9, 27.5") == (53.9, 27.5)
    assert parse_coordinates("нет координат") is None
    assert parse_coordinates("153.9, 27.5") is None
