from __future__ import annotations

import math
from typing import Iterable, Tuple

# A simplified centerline of Minsk's MKAD derived from OpenStreetMap road
# geometry (ways formerly tagged ref=М9). © OpenStreetMap contributors,
# ODbL: https://www.openstreetmap.org/copyright. Points are ordered clockwise
# and intentionally kept local so scheduled scans do not depend on a map API.
MKAD_CONTOUR: Tuple[Tuple[float, float], ...] = (
    (53.9057502, 27.6876773),
    (53.9106755, 27.6856532),
    (53.9163275, 27.6833362),
    (53.9233373, 27.6802547),
    (53.9298236, 27.6774971),
    (53.9361892, 27.6751111),
    (53.9423620, 27.6722564),
    (53.9466109, 27.6641214),
    (53.9498088, 27.6551788),
    (53.9544385, 27.6421935),
    (53.9579505, 27.6320203),
    (53.9608076, 27.6243060),
    (53.9639541, 27.6148382),
    (53.9673692, 27.6056072),
    (53.9702359, 27.5952371),
    (53.9713704, 27.5864864),
    (53.9711721, 27.5757319),
    (53.9707067, 27.5649467),
    (53.9701423, 27.5544499),
    (53.9696566, 27.5434065),
    (53.9693180, 27.5323010),
    (53.9687935, 27.5235506),
    (53.9683349, 27.5118635),
    (53.9679304, 27.4995831),
    (53.9673257, 27.4855720),
    (53.9659669, 27.4712284),
    (53.9627741, 27.4624168),
    (53.9576978, 27.4547576),
    (53.9511869, 27.4459762),
    (53.9455277, 27.4387378),
    (53.9399789, 27.4311043),
    (53.9319765, 27.4227808),
    (53.9263741, 27.4182267),
    (53.9196217, 27.4139099),
    (53.9115346, 27.4100814),
    (53.9049839, 27.4074961),
    (53.8966664, 27.4095402),
    (53.8894350, 27.4129023),
    (53.8815660, 27.4167311),
    (53.8758036, 27.4194638),
    (53.8699220, 27.4224954),
    (53.8589809, 27.4279654),
    (53.8529663, 27.4328599),
    (53.8476419, 27.4407930),
    (53.8440537, 27.4541945),
    (53.8427096, 27.4685789),
    (53.8413000, 27.4798056),
    (53.8394557, 27.4956554),
    (53.8386907, 27.5027560),
    (53.8375889, 27.5141824),
    (53.8363847, 27.5252792),
    (53.8353734, 27.5341551),
    (53.8341734, 27.5436985),
    (53.8331656, 27.5541040),
    (53.8329153, 27.5643431),
    (53.8330007, 27.5737364),
    (53.8332678, 27.5862634),
    (53.8333545, 27.5945718),
    (53.8333124, 27.6063028),
    (53.8334370, 27.6199936),
    (53.8336480, 27.6313470),
    (53.8341037, 27.6450655),
    (53.8371342, 27.6567984),
    (53.8428807, 27.6651873),
    (53.8494816, 27.6718570),
    (53.8555311, 27.6775232),
    (53.8622467, 27.6844615),
    (53.8684770, 27.6903483),
    (53.8749797, 27.6949043),
    (53.8822168, 27.6967630),
    (53.8894725, 27.6942358),
    (53.8969123, 27.6910963),
)


def distance_to_mkad_km(latitude: float, longitude: float) -> float:
    """Straight-line distance to MKAD; points inside the ring return zero."""
    if _point_in_polygon(latitude, longitude, MKAD_CONTOUR):
        return 0.0
    distances = (
        _distance_to_segment_km(latitude, longitude, start, end)
        for start, end in _closed_segments(MKAD_CONTOUR)
    )
    return round(min(distances), 1)


def _closed_segments(
    contour: Tuple[Tuple[float, float], ...],
) -> Iterable[Tuple[Tuple[float, float], Tuple[float, float]]]:
    for index, start in enumerate(contour):
        yield start, contour[(index + 1) % len(contour)]


def _point_in_polygon(
    latitude: float,
    longitude: float,
    contour: Tuple[Tuple[float, float], ...],
) -> bool:
    inside = False
    previous_latitude, previous_longitude = contour[-1]
    for current_latitude, current_longitude in contour:
        crosses = (current_latitude > latitude) != (previous_latitude > latitude)
        if crosses:
            crossing_longitude = (
                (previous_longitude - current_longitude)
                * (latitude - current_latitude)
                / (previous_latitude - current_latitude)
                + current_longitude
            )
            if longitude < crossing_longitude:
                inside = not inside
        previous_latitude, previous_longitude = current_latitude, current_longitude
    return inside


def _distance_to_segment_km(
    latitude: float,
    longitude: float,
    start: Tuple[float, float],
    end: Tuple[float, float],
) -> float:
    start_x, start_y = _local_xy_km(latitude, longitude, *start)
    end_x, end_y = _local_xy_km(latitude, longitude, *end)
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared == 0:
        return math.hypot(start_x, start_y)
    projection = max(
        0.0,
        min(1.0, -(start_x * delta_x + start_y * delta_y) / length_squared),
    )
    closest_x = start_x + projection * delta_x
    closest_y = start_y + projection * delta_y
    return math.hypot(closest_x, closest_y)


def _local_xy_km(
    origin_latitude: float,
    origin_longitude: float,
    latitude: float,
    longitude: float,
) -> Tuple[float, float]:
    mean_latitude = math.radians((origin_latitude + latitude) / 2)
    x = (longitude - origin_longitude) * 111.32 * math.cos(mean_latitude)
    y = (latitude - origin_latitude) * 111.32
    return x, y
