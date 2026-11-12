"""테스트 공통: MapData와 후보 Feature로 바로 판정 (준비 → 판정을 한 번에)."""

from pudo.measure import measure
from pudo.rules import evaluate


def judge(feature, data, *, thresholds=None, rules_off=(), photos=None, photo_min_confidence=0.8):
    m = measure(feature, data)
    photo = (photos or {}).get(m["id"])
    return evaluate(m, thresholds=thresholds, rules_off=rules_off, photo=photo, photo_min_confidence=photo_min_confidence)


def write_map1000(folder, reach=200):
    """1:1000 수치지형도 축소판: 동서 '가로로'(도로폭 20m, 양쪽 보도 3m, 차로수 2)와
    남북 '세로길'(도로폭 8m, 보도 없음, 차로수 1)이 판정 중심 (0,0)에서 만남. reach = 도로 반 길이(m)."""
    import shapefile
    from pyproj import Transformer
    from pudo.config import CENTER_LAT, CENTER_LON
    x0, y0 = Transformer.from_crs(4326, 5186, always_xy=True).transform(CENTER_LON, CENTER_LAT)

    def at(x, y):   # 판정 중심 기준 미터 → EPSG:5186 (테스트 범위에서는 축척 차이 무시)
        return [x0 + x, y0 + y]

    sheet = folder / "000000001"
    sheet.mkdir(parents=True)
    r = reach
    with shapefile.Writer(str(sheet / "N1L_A0020000"), shapeType=shapefile.POLYLINE, encoding="cp949") as w:
        for name in ("UFID", "도로명", "일방통행", "명칭"):
            w.field(name, "C", 40)
        w.field("차로수", "N", 4)
        w.field("도로폭", "N", 8, 2)
        for ufid, name, points, lanes, width in (
                ("L1", "가로로", [at(-r, 0), at(0, 0)], 2, 20), ("L2", "가로로", [at(0, 0), at(r, 0)], 2, 20),
                ("L3", "세로길", [at(0, -r), at(0, 0)], 1, 8), ("L4", "세로길", [at(0, 0), at(0, r)], 1, 8)):
            w.line([points])
            w.record(UFID=ufid, 도로명=name, 일방통행="양방통행", 명칭="", 차로수=lanes, 도로폭=width)

    def rect(x1, y1, x2, y2):
        return [(x1, y1), (x1, y2), (x2, y2), (x2, y1), (x1, y1)]   # 시계 방향 (shp 바깥 고리)

    for layer, rings in (("N1A_A0010000", [("B1", rect(-r, -10, r, 10), 0), ("B2", rect(-4, -r, 4, r), 0)]),
                         ("N1A_A0033320", [("S1", rect(-r, 7, -4, 10), 3), ("S2", rect(4, 7, r, 10), 3),
                                           ("S3", rect(-r, -10, -4, -7), 3), ("S4", rect(4, -10, r, -7), 3)])):
        with shapefile.Writer(str(sheet / layer), shapeType=shapefile.POLYGON, encoding="cp949") as w:
            w.field("UFID", "C", 40)
            w.field("폭", "N", 8, 2)
            for ufid, ring, width in rings:
                w.poly([[at(x, y) for x, y in ring]])
                w.record(UFID=ufid, 폭=width)
