"""강남역 주변 분석에 사용하는 기존 v0 근사 미터 좌표 변환."""

import math

from .config import CENTER_LAT, CENTER_LON

CX = math.cos(math.radians(CENTER_LAT)) * 111320
CY = 110540


def to_meters(lon, lat):
    return ((lon - CENTER_LON) * CX, (lat - CENTER_LAT) * CY)


def to_lonlat(x, y):
    return (round(CENTER_LON + x / CX, 7), round(CENTER_LAT + y / CY, 7))


def distance_to_points(point, points):
    """미터 좌표계에서 가장 가까운 점까지의 거리. 데이터가 없으면 None."""
    return min((math.hypot(point[0] - x, point[1] - y) for x, y in points), default=None)


def distance_to_segment(point, segment):
    """미터 좌표계에서 폴리라인을 이루는 선분까지의 최소 거리."""
    best = 9e9
    for (x1, y1), (x2, y2) in zip(segment, segment[1:]):
        dx, dy = x2 - x1, y2 - y1
        length_squared = dx * dx + dy * dy
        t = 0 if length_squared == 0 else max(
            0, min(1, ((point[0] - x1) * dx + (point[1] - y1) * dy) / length_squared)
        )
        best = min(best, math.hypot(point[0] - (x1 + t * dx), point[1] - (y1 + t * dy)))
    return best


def distance_to_segments(point, segments):
    return min((distance_to_segment(point, segment) for segment in segments), default=None)


def distance_to_lines(point, lines, codes):
    return min(
        (distance_to_segment(point, segment) for code in codes for segment in lines.get(code, ())),
        default=None,
    )


def distance_to_area(point, area):
    """미터 좌표계에서 면까지의 거리. 면 안이면 0, 면이 없으면 None."""
    if area is None or area.is_empty:
        return None
    from shapely.geometry import Point
    return area.distance(Point(point))
