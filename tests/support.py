"""테스트 공통: MapData와 후보 Feature로 바로 판정 (준비 → 판정을 한 번에)."""

from pudo.measure import measure
from pudo.rules import evaluate


def judge(feature, data, *, thresholds=None, rules_off=(), photos=None, photo_min_confidence=0.8):
    m = measure(feature, data)
    photo = (photos or {}).get(m["id"])
    return evaluate(m, thresholds=thresholds, rules_off=rules_off, photo=photo, photo_min_confidence=photo_min_confidence)
