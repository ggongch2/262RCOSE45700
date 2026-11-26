import json
import unittest

import support  # noqa: F401
from support import assessment, localization_json

from pudo3.photo_decision import decide, undecidable
from pudo3.vlm_policy import parse_assessment, parse_localization

LOC = parse_localization(localization_json())
VAGUE = {"조건 미충족", "접근성 부족", "고정 시설 문제", "AI 판독 결과 불가"}


def judge(**overrides):
    return decide(LOC, parse_assessment(json.dumps(assessment(**overrides), ensure_ascii=False)))


class DecisionTest(unittest.TestCase):
    def test_clear_case_is_possible(self):
        self.assertEqual(judge()["verdict"], "가능")

    def test_property_boundary_fence_not_a_reason(self):
        """7. property_boundary 펜스는 불가 사유가 아님."""
        for value in (True, False):
            result = judge(fixed_obstruction={"value": value, "type": "permanent_fence", "relation": "property_boundary",
                                              "opening_at_target": None, "evidence": "보도 뒤 건물 앞 펜스"})
            self.assertEqual(result["verdict"], "가능", result)
            self.assertFalse(result["fail_reasons"])
            self.assertTrue(any("대지 경계" in n["note"] for n in result["notes"]))

    def test_background_and_inside_sidewalk_not_reasons(self):
        for relation in ("background", "inside_sidewalk"):
            result = judge(fixed_obstruction={"value": True, "type": "planter", "relation": relation,
                                              "opening_at_target": None, "evidence": "화단"})
            self.assertEqual(result["verdict"], "가능")

    def test_curb_edge_railing_without_opening_is_impossible(self):
        """8. curb_edge 금속 난간 + 개구부 없음은 즉시 불가."""
        result = judge(fixed_obstruction={"value": True, "type": "metal_railing", "relation": "curb_edge",
                                          "opening_at_target": False, "evidence": "연석 위 금속 난간이 이어짐"})
        self.assertEqual(result["verdict"], "불가")
        self.assertEqual(result["reason"], "불가: 표시된 연석과 보도 사이에 금속 난간이 있고 정면 개구부가 없음")
        self.assertEqual(result["fail_reasons"][0]["evidence"], "연석 위 금속 난간이 이어짐")

    def test_impossible_even_when_other_fields_unknown(self):
        result = judge(fixed_obstruction={"value": True, "type": "wall", "relation": "curb_edge",
                                          "opening_at_target": False, "evidence": "벽"},
                       sidewalk={"value": None, "evidence": "가림"})
        self.assertEqual(result["verdict"], "불가")
        self.assertTrue(result["unverified"])

    def test_curb_edge_railing_with_opening_is_not_rejected(self):
        """9. curb_edge 난간 + 정면 개구부 있음은 난간만으로 불가 처리하지 않음."""
        result = judge(fixed_obstruction={"value": True, "type": "metal_railing", "relation": "curb_edge",
                                          "opening_at_target": True, "evidence": "난간 사이 개구부"})
        self.assertEqual(result["verdict"], "가능")
        self.assertTrue(any("개구부" in n["note"] for n in result["notes"]))

    def test_curb_edge_railing_opening_unknown_is_undecidable(self):
        result = judge(fixed_obstruction={"value": True, "type": "metal_railing", "relation": "curb_edge",
                                          "opening_at_target": None, "evidence": "난간"})
        self.assertEqual(result["verdict"], "판단 불가")
        self.assertIn("개구부", result["reason"])

    def test_relation_unknown_is_undecidable(self):
        result = judge(fixed_obstruction={"value": True, "type": "permanent_fence", "relation": "unknown",
                                          "opening_at_target": None, "evidence": "펜스 위치 불명"})
        self.assertEqual(result["verdict"], "판단 불가")
        self.assertIn("대지 경계인지 구분하지 못함", result["reason"])

    def test_transient_objects_are_not_permanent(self):
        """10. 차량·사람·이동식 콘을 영구 차단물로 처리하지 않음."""
        for kind in ("vehicle", "car", "person", "truck", "motorcycle", "traffic_cone", "cone", "barricade"):
            result = judge(fixed_obstruction={"value": True, "type": kind, "relation": "curb_edge",
                                              "opening_at_target": False, "evidence": f"{kind} 있음"})
            self.assertNotEqual(result["verdict"], "불가", kind)
            self.assertFalse([r for r in result["fail_reasons"] if r["field"] == "fixed_obstruction"], kind)
        cone = judge(fixed_obstruction={"value": True, "type": "temporary_facility", "relation": "curb_edge",
                                        "opening_at_target": False, "evidence": "이동식 콘"})
        self.assertEqual(cone["verdict"], "가능")
        self.assertEqual(cone["scene_conditions"][0]["label"], "촬영 당시 임시 시설")
        car = judge(fixed_obstruction={"value": True, "type": "parked_car", "relation": "curb_edge",
                                       "opening_at_target": False, "evidence": "주차 차량"})
        self.assertTrue(any(c["field"] == "temporary_occupancy" for c in car["scene_conditions"]))

    def test_vehicle_entrance_overlap_is_impossible(self):
        """11. 주차장 차량 출입구 직접 겹침은 불가."""
        result = judge(vehicle_entrance={"value": True, "evidence": "주차장 입구 경사로"})
        self.assertEqual(result["verdict"], "불가")
        self.assertEqual(result["reason"], "불가: 표시된 위치가 주차장·차고 차량 출입구와 직접 겹침")

    def test_yellow_double_is_impossible(self):
        """12. 황색 복선은 불가."""
        result = judge(curb_line={"value": "yellow_double", "evidence": "황색 복선"})
        self.assertEqual(result["verdict"], "불가")
        self.assertIn("황색 복선", result["reason"])

    def test_yellow_solid_without_exception_is_impossible(self):
        """13. 황색 실선이며 허용 조건 미확인은 불가."""
        for exception in (False, None):
            result = judge(curb_line={"value": "yellow_solid", "evidence": "황색 실선"},
                           stop_exception={"value": exception, "evidence": ""})
            self.assertEqual(result["verdict"], "불가")
            self.assertIn("황색 실선", result["reason"])
            self.assertIn("조건부 정차 허용이 확인되지 않음", result["reason"])
        result = judge(curb_line={"value": "yellow_solid", "evidence": "황색 실선"},
                       stop_exception={"value": True, "evidence": "09-18시 정차 허용"})
        self.assertEqual(result["verdict"], "판단 불가")

    def test_no_sidewalk_is_impossible(self):
        result = judge(sidewalk={"value": False, "evidence": "도로 옆이 바로 벽"})
        self.assertEqual(result["reason"], "불가: 후보 바로 앞에 사람이 설 수 있는 보도가 없음")

    def test_reasons_are_specific(self):
        """14. 불가·판단 불가 이유가 구체적으로 출력됨."""
        cases = [
            judge(sidewalk={"value": False, "evidence": "x"}),
            judge(fixed_obstruction={"value": True, "type": "planter", "relation": "curb_edge",
                                     "opening_at_target": False, "evidence": "x"}),
            judge(vehicle_entrance={"value": True, "evidence": "x"}),
            judge(curb_line={"value": "yellow_double", "evidence": "x"}),
            judge(sidewalk={"value": None, "evidence": "가림"}),
            judge(sidewalk={"value": None, "evidence": "가림"}, vehicle_entrance={"value": None, "evidence": "가림"}),
            decide(parse_localization(json.dumps({"sidewalk_status": "occluded", "evidence": "가림"})), None),
            undecidable("assessment", "2차 조건 판독 JSON 검증 실패: sidewalk.value"),
        ]
        for result in cases:
            self.assertIn(result["verdict"], {"불가", "판단 불가"})
            self.assertTrue(result["reason"].startswith(result["verdict"] + ": "), result["reason"])
            self.assertNotIn(result["reason"].split(": ", 1)[1].strip(), VAGUE)
            self.assertGreater(len(result["reason"]), 15)
            if result["verdict"] == "판단 불가":
                self.assertTrue(result["unverified"])
                for item in result["unverified"]:
                    self.assertTrue(item["reason"] and item["label"])
        two = cases[5]
        self.assertEqual(len(two["unverified"]), 2)
        self.assertIn("보도 유무", two["reason"])
        self.assertIn("차량 출입구", two["reason"])

    def test_invisible_is_not_none(self):
        """보이지 않는 규제선은 '없음'이 아니라 null → 판단 불가 (GIS가 규제선을 판정한 경우 제외)."""
        a = parse_assessment(json.dumps(assessment(curb_line={"value": None, "evidence": "차량이 가림"})))
        self.assertEqual(decide(LOC, a)["verdict"], "판단 불가")
        self.assertEqual(decide(LOC, a, curb_line_required=False)["verdict"], "가능")


if __name__ == "__main__":
    unittest.main()
