"""모델과 무관한 사진 관측값 계약과 판정 입력 변환. 모델 호출·원본 응답 파싱은 하지 않습니다."""

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .geo import to_meters

SCHEMA_VERSION = "photo-observations.v1"
CORE_FIELDS = {"sidewalk", "curb_blocked", "curb_line"}
BOOL_FIELDS = {"sidewalk", "curb_blocked", "construction", "occupied"}
CURB_LINES = {"white", "yellow_dashed", "yellow_solid", "yellow_double", "none"}
SOURCES = {"google", "mapillary", "own"}


class PhotoContractError(ValueError):
    pass


def _number(value, name, lower, upper):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PhotoContractError(f"{name}: 숫자가 필요합니다.")
    if not math.isfinite(value) or not lower <= value <= upper:
        raise PhotoContractError(f"{name}: {lower}~{upper} 범위가 필요합니다.")
    return value


def validate_confidence_threshold(value):
    return _number(value, "photo_min_confidence", 0, 1)


@dataclass(frozen=True)
class Observation:
    value: bool | str | None
    confidence: float | None
    evidence: str


@dataclass(frozen=True)
class PhotoAssessment:
    candidate_id: str
    coordinates: tuple[float, float]
    heading: float
    image_id: str
    image_source: str
    model: str
    observations: dict[str, Observation]

    def mismatch(self, feature, image=None):
        if self.candidate_id != feature["properties"]["id"]:
            return "후보 ID 불일치"
        p, q = to_meters(*self.coordinates), to_meters(*feature["geometry"]["coordinates"])
        if math.hypot(p[0] - q[0], p[1] - q[1]) > 1:
            return "후보 조회 좌표 불일치"
        delta = abs(self.heading - feature["properties"]["heading"]) % 360
        if min(delta, 360 - delta) > 5:
            return "사진 조회 방향 불일치"
        if self.image_source == "google" and image and self.image_id != image["pano_id"]:
            return "기존 Google 사진 ID와 불일치"
        return None

    def as_dict(self):
        return {
            "candidate_id": self.candidate_id, "coordinates": list(self.coordinates), "heading": self.heading,
            "image_id": self.image_id, "image_source": self.image_source, "model": self.model,
            "observations": {
                name: {"value": observation.value, "confidence": observation.confidence, "evidence": observation.evidence}
                for name, observation in self.observations.items()
            },
        }


def parse_assessments(payload):
    """합의된 입력만 검증합니다. 자유 텍스트나 모델별 응답을 추측해서 변환하지 않습니다."""
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "results"}:
        raise PhotoContractError("최상위 필드는 schema_version과 results여야 합니다.")
    if payload["schema_version"] != SCHEMA_VERSION or not isinstance(payload["results"], list):
        raise PhotoContractError(f"schema_version={SCHEMA_VERSION}, results 배열이 필요합니다.")
    result = {}
    fields = {"candidate_id", "coordinates", "heading", "image_id", "image_source", "model", "observations"}
    for row in payload["results"]:
        if not isinstance(row, dict) or set(row) != fields:
            raise PhotoContractError("사진 판독 행의 필드가 계약과 다릅니다.")
        for name in ("candidate_id", "image_id", "image_source", "model"):
            if not isinstance(row[name], str) or not row[name].strip():
                raise PhotoContractError(f"{name}: 비어 있지 않은 문자열이 필요합니다.")
        if row["candidate_id"] in result:
            raise PhotoContractError("중복 candidate_id는 허용하지 않습니다.")
        if row["image_source"] not in SOURCES:
            raise PhotoContractError("image_source: google/mapillary/own 중 하나여야 합니다.")
        coordinates = row["coordinates"]
        if not isinstance(coordinates, list) or len(coordinates) != 2:
            raise PhotoContractError("coordinates: [경도, 위도]가 필요합니다.")
        _number(coordinates[0], "경도", -180, 180)
        _number(coordinates[1], "위도", -90, 90)
        _number(row["heading"], "heading", 0, 360)
        raw = row["observations"]
        if not isinstance(raw, dict) or not CORE_FIELDS <= raw.keys() or raw.keys() - (BOOL_FIELDS | {"curb_line"}):
            raise PhotoContractError("observations에는 sidewalk, curb_blocked, curb_line이 필요합니다.")
        observations = {}
        for name, observation in raw.items():
            if not isinstance(observation, dict) or set(observation) != {"value", "confidence", "evidence"}:
                raise PhotoContractError(f"{name}: value/confidence/evidence가 필요합니다.")
            value, confidence = observation["value"], observation["confidence"]
            if name in BOOL_FIELDS and value is not None and not isinstance(value, bool):
                raise PhotoContractError(f"{name}.value: true/false/null이 필요합니다.")
            if name == "curb_line" and value is not None and (not isinstance(value, str) or value not in CURB_LINES):
                raise PhotoContractError("curb_line.value: 정의된 선 종류 또는 null이 필요합니다.")
            if confidence is not None:
                _number(confidence, f"{name}.confidence", 0, 1)
            if not isinstance(observation["evidence"], str):
                raise PhotoContractError(f"{name}.evidence: 문자열이 필요합니다.")
            observations[name] = Observation(value, confidence, observation["evidence"])
        result[row["candidate_id"]] = PhotoAssessment(
            row["candidate_id"], tuple(coordinates), row["heading"], row["image_id"],
            row["image_source"], row["model"], observations,
        )
    return result


def load_assessments(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise PhotoContractError("사진 관측 결과 파일을 읽을 수 없습니다.") from None
    except (ValueError, UnicodeError):
        raise PhotoContractError("사진 관측 결과 파일이 올바른 JSON이 아닙니다.") from None
    return parse_assessments(payload)


def apply_observations(assessment, feature, image, checks, fails, needs, *, min_confidence, map_sidewalk=None):
    """관측값을 기존 규칙 결과에 병합합니다. 지도 hard fail은 제거하지 않습니다.

    map_sidewalk: 1:1000 수치지형도의 보도 유무(True/False/None). 사진과 다르면 확인 필요로 둡니다.
    """
    report = assessment.as_dict()
    report.update({"schema_version": SCHEMA_VERSION, "min_confidence": min_confidence,
                   "status": "applied", "used_fields": [], "review_reasons": []})
    mismatch = assessment.mismatch(feature, image)
    if mismatch:
        report["status"] = "rejected"
        report["review_reasons"].append(mismatch)
        needs.append(f"V0 사진 대응 확인 필요: {mismatch}")
        return report

    labels = {"sidewalk": "A1 보도", "curb_blocked": "A2 연석 연결", "curb_line": "V1 사진 규제선",
              "construction": "V2 공사", "occupied": "V3 일시적 점유"}
    for name, observation in assessment.observations.items():
        item = labels[name]
        usable = observation.value is not None and observation.confidence is not None and observation.confidence >= min_confidence
        if name == "sidewalk":
            checks[:] = [check for check in checks if check["item"] != "A1 보도"]
            needs[:] = [reason for reason in needs if not reason.startswith("A1 보도")]
        if not usable:
            reason = f"{item} 사진 판독 불확실 (값·신뢰도 미제공 또는 기준 미달)"
            checks.append({"item": item, "value": "사진 판독 불확실", "threshold": f"신뢰도 {min_confidence} 이상", "pass": None, "estimated": True})
            needs.append(reason)
            report["review_reasons"].append(reason)
            continue
        value = observation.value
        report["used_fields"].append(name)
        if name == "sidewalk":
            passed, display = value, "있음" if value else "없음"
            if map_sidewalk is not None and map_sidewalk != value:
                reason = f"A1 보도 지도(1:1000 {'있음' if map_sidewalk else '없음'})와 사진 판독({display}) 불일치"
                needs.append(reason)
                report["review_reasons"].append(reason)
                passed = None
            elif not value:
                fails.append("A1 사진 판독: 보도 없음")
        elif name == "curb_blocked":
            passed, display = not value, "막힘" if value else "연결됨"
            if value:
                fails.append("A2 사진 판독: 연석 접근이 막힘")
        elif name == "curb_line":
            passed, display = value not in {"yellow_solid", "yellow_double"}, value
            if not passed:
                fails.append(f"H5 사진 판독: 정차 금지선 ({value})")
            elif any(reason.startswith("H5") for reason in fails):
                reason = "H5 지도 불가 근거와 사진 판독 불일치"
                needs.append(reason)
                report["review_reasons"].append(reason)
            elif not any("H5 버스전용차로" in reason for reason in needs):
                needs[:] = [reason for reason in needs if not reason.startswith("H5 규제선 자료 불일치")]
        else:
            passed, display = not value, "있음" if value else "없음"
            if value:
                reason = f"{item} 사진에서 확인 → 현재 상태 확인 필요"
                needs.append(reason)
                report["review_reasons"].append(reason)
                passed = None
        checks.append({"item": item, "value": f"{display} (사진 판독)", "threshold": f"신뢰도 {min_confidence} 이상", "pass": passed, "estimated": False})
    if report["review_reasons"]:
        report["status"] = "review"
    return report
