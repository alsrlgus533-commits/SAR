import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["HEALTH_CHECK"] = "0"
os.environ["VMS_WARM"] = "0"

import backend


class ReportConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.gemini = backend.GEMINI_KEY
        self.anthropic = backend.ANTHROPIC_KEY
        backend.GEMINI_KEY = ""
        backend.ANTHROPIC_KEY = ""

    def tearDown(self):
        backend.GEMINI_KEY = self.gemini
        backend.ANTHROPIC_KEY = self.anthropic

    def test_extracts_actual_accident_datetime(self):
        dt = backend._extract_accident_datetime("2026년 7월 13일 14시 20분경 기관 고장")
        self.assertEqual(dt.strftime("%Y-%m-%d %H:%M"), "2026-07-13 14:20")

        base = datetime(2026, 7, 13, 18, 0, tzinfo=timezone(timedelta(hours=9)))
        dt = backend._extract_accident_datetime("오늘 09:05경 사고", now=base)
        self.assertEqual(dt.strftime("%Y-%m-%d %H:%M"), "2026-07-13 09:05")

    def test_accident_cause_is_limited_to_official_names_without_list_number(self):
        self.assertEqual(len(backend._ACCIDENT_CAUSE_LABELS), 23)
        self.assertEqual(len(set(backend._ACCIDENT_CAUSE_LABELS)), 23)
        self.assertTrue(all(not re.match(r"^\d", x) for x in backend._ACCIDENT_CAUSE_LABELS))
        self.assertEqual(backend._accident_cause("17. 해상 부유물"), "해상 부유물")
        self.assertEqual(backend._accident_cause("12 기관계통 고장으로 추정"), "기관계통 고장")
        self.assertEqual(backend._accident_cause("", "폐그물 프로펠러 감김", ""), "해상 부유물")
        self.assertEqual(backend._accident_cause("임의 설명문"), "기타")

    def test_llm_classification_numbers_and_explanations_are_normalized(self):
        fallback = backend._infer_fallback("엔진 고장", "주기관 정지")
        merged = backend._merge_infer({
            "사고종류": "9. 기관손상 - 주기관 고장",
            "추정원인": "12번 기관계통 고장으로 판단됨",
        }, fallback)
        self.assertEqual(merged["사고종류"], "기관손상")
        self.assertEqual(merged["추정원인"], "기관계통 고장")

    def test_missing_fields_and_formats_are_rejected(self):
        confirmed = {key: "값" for key in backend._REPORT_REQUIRED}
        confirmed.update({"사고일시": "2026-07-13 14:20", "항로": "목포-제주", "출항시각": "13:40"})
        self.assertEqual(backend._missing_report_fields(confirmed), [])

        confirmed["사고위치"] = ""
        confirmed["항로"] = "목포 제주"
        missing = backend._missing_report_fields(confirmed)
        self.assertIn("사고 위치", missing)
        self.assertIn("운항 항로 형식(예: 목포-제주)", missing)

    def test_hwpx_endpoint_rejects_unconfirmed_request(self):
        client = backend.app.test_client()
        response = client.post("/report/hwpx", json={"utterance": "시험호 기관 고장"})
        self.assertEqual(response.status_code, 422)
        body = response.get_json()
        self.assertEqual(body["error"], "필수정보를 확인·입력해 주세요.")
        self.assertIn("사고 일시", body["missing"])

    def test_kakao_asks_for_each_missing_field(self):
        uid = "confirmation-test-user"
        backend._SESSIONS[uid] = {
            "utterance": "시험 원문", "confirmed": {},
            "pending_fields": ["사고일시", "항로"], "mode": "confirm_report_field",
        }
        try:
            response = backend.app.test_client().post("/kakao", json={
                "userRequest": {"utterance": "2026-07-13 14:20", "user": {"id": uid}}
            })
            self.assertEqual(response.status_code, 200)
            text = response.get_json()["template"]["outputs"][0]["simpleText"]["text"]
            self.assertIn("운항 항로", text)
            self.assertEqual(backend._SESSIONS[uid]["confirmed"]["사고일시"], "2026-07-13T14:20")
        finally:
            backend._SESSIONS.pop(uid, None)

    def test_kakao_formal_report_preparation_is_immediately_deferred(self):
        uid = "formal-report-callback-test"
        backend._SESSIONS[uid] = {"utterance": "시험호 기관 고장", "report": "1차 보고서", "mode": None}
        try:
            with patch.object(backend, "_prepare_report_confirmation") as prepare, \
                 patch.object(backend.threading, "Thread") as thread_cls:
                response = backend.app.test_client().post("/kakao", json={
                    "userRequest": {
                        "utterance": "정식 보고서", "callbackUrl": "https://callback.invalid/test",
                        "user": {"id": uid},
                    }
                })
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["useCallback"])
            prepare.assert_not_called()
            thread_cls.assert_called_once()
            thread_cls.return_value.start.assert_called_once()
        finally:
            backend._SESSIONS.pop(uid, None)

    def test_formal_report_reuses_all_values_shown_in_first_report(self):
        first_report = """🚨 해양사고 1차(속보) — 자동작성

▶ 선박: 한일골드스텔라 (카페리 · 15,195톤 · 정원 948명)
▶ 발생: 2026-07-13 14:20
▶ 출항시각: 13:40 (여수-제주)
▶ 위치: 여수항 북동방 약 2마일
▶ 승선: 여객 120명(성인 110·소아 8·유아 2), 선원 20명 (실승선 계 140명)
▶ 개요: 주기관 고장으로 자력 항해 불가
▶ 조치사항: 해경 보고
"""
        empty_parse = {"사고일시": "", "선박명": "", "사고위치": "", "여객": "",
                       "승무원": "", "사고개요": ""}
        with patch.object(backend, "_parse_nl", return_value=empty_parse) as parse_nl, \
             patch.object(backend, "_vessel_lookup") as vessel_lookup, \
             patch.object(backend, "_route_lookup") as route_lookup:
            confirmed = backend._prepare_report_confirmation("최초 신고문", first_report)

        self.assertEqual(backend._pending_report_keys(confirmed), [])
        self.assertEqual(confirmed["선박명"], "한일골드스텔라")
        self.assertEqual(confirmed["사고일시"], "2026-07-13T14:20")
        self.assertEqual(confirmed["항로"], "여수-제주")
        self.assertEqual(confirmed["출항시각"], "13:40")
        self.assertEqual(confirmed["여객"], "120")
        self.assertEqual(confirmed["승무원"], "20")
        self.assertEqual(confirmed["사고위치"], "여수항 북동방 약 2마일")
        self.assertEqual(confirmed["사고개요"], "주기관 고장으로 자력 항해 불가")
        parse_nl.assert_not_called()
        vessel_lookup.assert_not_called()
        route_lookup.assert_not_called()

    def test_formal_report_reparses_only_when_first_report_lacks_source_fields(self):
        first_report = """🚨 해양사고 1차(속보) — 자동작성

▶ 선박: 시험호
▶ 발생: 2026-07-13 14:20
▶ 위치: 추자항 북동방 2해리
▶ 승선: 여객 28명, 선원 4명
"""
        parsed = {
            "사고일시": "", "선박명": "", "사고위치": "",
            "여객": "", "승무원": "", "사고개요": "기관 고장으로 자력 항해 불가",
        }
        with patch.object(backend, "_parse_nl", return_value=parsed) as parse_nl, \
             patch.object(backend, "_vessel_lookup", return_value=None), \
             patch.object(backend, "_route_lookup", return_value=None):
            confirmed = backend._prepare_report_confirmation("시험호 기관 고장", first_report)

        parse_nl.assert_called_once_with("시험호 기관 고장")
        self.assertEqual(confirmed["사고개요"], "기관 고장으로 자력 항해 불가")

    def test_formal_report_skips_reparse_when_only_lookup_fields_are_missing(self):
        first_report = """🚨 해양사고 1차(속보) — 자동작성

▶ 선박: 시험호
▶ 발생: 2026-07-13 14:20
▶ 위치: 추자항 북동방 2해리
▶ 승선: 여객 28명, 선원 4명
▶ 개요: 기관 고장으로 자력 항해 불가
"""
        with patch.object(backend, "_parse_nl") as parse_nl, \
             patch.object(backend, "_vessel_lookup", return_value={"선박코드": "TEST"}), \
             patch.object(backend, "_route_lookup", return_value={"운항항로": "목포-제주", "출발시각": "1340"}), \
             patch.object(backend, "_predep_lookup", return_value=None):
            confirmed = backend._prepare_report_confirmation("시험호 기관 고장", first_report)

        parse_nl.assert_not_called()
        self.assertEqual(confirmed["항로"], "목포-제주")
        self.assertEqual(confirmed["출항시각"], "13:40")

    def test_first_kakao_message_received_time_overrides_text_time(self):
        parsed = {"사고일시": "2000-01-01 01:01", "선박명": "시험호", "사고위치": "",
                  "여객": "1", "승무원": "1", "사고개요": "기관 고장"}
        with patch.object(backend, "_parse_nl", return_value=parsed), \
             patch.object(backend, "_vessel_lookup", return_value=None), \
             patch.object(backend, "_route_lookup", return_value=None), \
             patch.object(backend, "_vms_position_safe", return_value=None), \
             patch.object(backend, "_weather_lookup", return_value={"error": "test"}), \
             patch.object(backend, "_pax_lookup", return_value=None):
            report = backend._build_report_text("2000년 사고", "2026-07-13 15:42")
        self.assertIn("▶ 발생: 2026-07-13 15:42", report)
        self.assertNotIn("▶ 발생: 2000-01-01 01:01", report)

    def test_new_kakao_accident_captures_received_time_before_callback(self):
        uid = "received-time-test"
        backend._SESSIONS.pop(uid, None)
        try:
            with patch.object(backend, "_health_down_critical", return_value=[]), \
                 patch.object(backend.threading, "Thread") as thread_cls:
                response = backend.app.test_client().post("/kakao", json={
                    "userRequest": {
                        "utterance": "시험호 기관 고장", "callbackUrl": "https://callback.invalid/test",
                        "user": {"id": uid},
                    }
                })
            self.assertTrue(response.get_json()["useCallback"])
            callback_args = thread_cls.call_args.kwargs["args"]
            self.assertRegex(callback_args[4], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
            self.assertEqual(backend._SESSIONS[uid]["accident_at"], callback_args[4])
        finally:
            backend._SESSIONS.pop(uid, None)

    def test_narrative_rejects_missing_information_instead_of_omitting_it(self):
        with self.assertRaisesRegex(ValueError, "운항 항로"):
            backend._summary_narrative({
                "date": "2026. 7. 13.(월)", "vtype": "여객선", "ship": "시험호",
                "manifest": "", "route": "", "dep": "", "acc_time": "14:20",
                "spot": "추자항 북동방 2해리", "summary": "기관 고장",
            })

    def test_confirmed_values_override_reparse_and_external_data(self):
        inferred = {
            "사고종류": "기관손상", "추정원인": "확인 중", "인명피해": "없음",
            "오염피해": "없음", "선박피해": "확인 중", "지연시간": "확인 중",
            "조치사항": ["확인 중"], "조치계획": ["확인 중"],
        }
        confirmed = {
            "사고일시": "2026-07-13T14:20", "선박명": "확정호",
            "사고위치": "추자항 북동방 2해리", "항로": "목포-제주",
            "출항시각": "13:40", "여객": "28", "승무원": "4",
            "사고개요": "기관 고장으로 자력 항해 불가",
        }
        reparsed = ({"사고일시": "", "선박명": "오인식호", "사고위치": "",
                     "여객": "99", "승무원": "9", "사고개요": "다른 내용"}, inferred)
        vessel = {"선박코드": "X", "선종": "여객선", "총톤수": "100톤", "선사": "시험선사"}
        mtis = {"항로": "부산-대마", "출항시간": "0800", "여객": "77", "승무원": "7"}

        with patch.object(backend, "_parse_and_infer", return_value=reparsed), \
             patch.object(backend, "_vessel_lookup", return_value=vessel), \
             patch.object(backend, "_route_lookup", return_value={"운항항로": "인천-백령", "출발시각": "0900"}), \
             patch.object(backend, "_predep_lookup", return_value=mtis), \
             patch.object(backend, "_vessel_master", return_value={}), \
             patch.object(backend, "_vms_position_safe", return_value=None), \
             patch.object(backend, "_weather_lookup", return_value=None), \
             patch.object(backend, "_pax_lookup", return_value={"여객": 66, "승무원": 6}), \
             patch.object(backend, "_komsa_vessel_photo", return_value=""), \
             patch.object(backend, "_rel_position", return_value=""):
            data = backend._build_report_data("원문", {}, "제주운항관리센터", confirmed)

        self.assertEqual(data["선명"], "확정호")
        self.assertEqual(data["승무정원"], "4")
        self.assertIn("목포-제주", data["사고개요"])
        self.assertIn("2026. 7. 13.", data["사고개요"])
        self.assertIn("14:20경", data["사고개요"])
        self.assertIn("여객 28명", data["사고개요"])
        self.assertNotIn("○○", data["사고개요"])
        self.assertIn(data["사고종류"], backend._ACCIDENT_TYPE_LABELS)
        self.assertIn(data["추정원인"], backend._ACCIDENT_CAUSE_LABELS)
        self.assertFalse(data["추정원인"][0].isdigit())

    def test_confirmed_data_composes_valid_hwpx(self):
        data = {
            "사고종류": "기관손상", "기준일시": "2026년 07월 13일 14:30",
            "보고센터": "제주운항관리센터",
            "사고개요": "2026. 7. 13.(월) 목포-제주 항로를 운항중인 여객선 시험호가 14:20경 기관 고장 사고 발생",
            "현지기상": "풍향(북), 풍속(3m/s), 파고(0.5m), 시정(양호)",
            "선명": "시험호", "총톤수": "100톤", "선종": "여객선", "승무정원": "4",
            "소유자": "시험선사", "선박번호": "TEST-1", "화물": "없음",
            "선적항": "제주", "국적": "대한민국", "검사기관": "한국해양교통안전공단",
            "보험현황": "가입", "사진경로": "", "인명피해": "없음", "오염피해": "없음",
            "선박피해": "확인 중", "지연시간": "확인 중", "추정원인": "확인 중",
            "조치사항": ["해경 보고"], "조치계획": ["정밀 점검"], "작성일자": "2026. 7. 13.",
        }
        blob = backend._compose_report_hwpx(data)
        self.assertTrue(blob.startswith(b"PK"))
        self.assertGreater(len(blob), 1000)


if __name__ == "__main__":
    unittest.main()
