import { useState, useEffect, useRef } from "react";

// ─────────────────────────────────────────────
// 해양사고 신속 보고 시스템 — 실 API 연동 프로토타입
// · 선박 제원: KOMSA '여객선 제원 정보' (공공데이터포털, psnshpNm 필터)
// · 기상정보: 기상청 API허브 해상관측 sea_obs (부이·파고부이·등표)
// · 자연어 파싱: Claude API
// · 브라우저 CORS 차단 시 모의 데이터로 자동 대체(상태 표시)
// ─────────────────────────────────────────────

const DEFAULT_CONFIG = {
  komsaUrl: "https://apis.data.go.kr/B554035/psnshp-spec-v2/get-psnshp-spec-v2",
  ferryRouteUrl: "https://apis.data.go.kr/B554035/ferry-route-info-v4/get-ferry-route-info-v4",
  komsaKey: "",
  kmaKey: "",
  anthropicKey: "",
  geminiKey: "",
  proxy: "",
  // 기준점 목록: "이름, 위도, 경도" 한 줄에 하나 — 도-분 표기(35-34.3N) 권장, 도-분-초·십진수도 인식
  // KOMSA 연안여객선 기항지 공식 API(port-call-info) 좌표 + 동해안·외해 등대 보충. 섬명은 정식명(○○도)로 표기. 백엔드와 동기화.
  refPoints: [
    "부산, 35-06.1N, 129-02.6E",
    "제주(제주시), 33-31.5N, 126-32.4E",
    "인천, 37-27.3N, 126-35.9E",
    "소청도, 37-46.6N, 124-44.8E",
    "대청도, 37-49.6N, 124-42.9E",
    "백령도, 37-57.3N, 124-44.2E",
    "덕적, 37-13.6N, 126-09.4E",
    "자월도, 37-14.6N, 126-19.1E",
    "대이작도, 37-10.7N, 126-14.9E",
    "승봉도, 37-10.2N, 126-17.4E",
    "하리, 37-43.7N, 126-17.2E",
    "미법도, 37-43.5N, 126-16.2E",
    "서검도, 37-43.9N, 126-14.2E",
    "문갑도, 37-10.3N, 126-06.8E",
    "굴업도, 37-11.3N, 125-59.2E",
    "백아도, 37-04.9N, 125-57.9E",
    "울도, 37-02.1N, 125-59.7E",
    "지도, 37-04.0N, 126-00.7E",
    "선수, 37-38.4N, 126-23.1E",
    "볼음도, 37-39.9N, 126-12.7E",
    "아차도, 37-39.6N, 126-14.1E",
    "주문(살곶이), 37-37.9N, 126-15.7E",
    "소연평도, 37-36.8N, 125-42.5E",
    "대연평도, 37-39.4N, 125-42.8E",
    "대부도, 37-17.9N, 126-34.4E",
    "삼목, 37-30.0N, 126-27.1E",
    "신도(옹진군), 37-30.8N, 126-26.4E",
    "장봉도, 37-31.8N, 126-23.1E",
    "풍도, 37-06.7N, 126-23.7E",
    "육도(안산시 단원구), 37-05.8N, 126-27.3E",
    "화흥포, 34-18.3N, 126-40.7E",
    "동천, 34-12.2N, 126-37.5E",
    "소안도, 34-10.8N, 126-38.1E",
    "완도, 34-19.0N, 126-45.6E",
    "청산, 34-10.9N, 126-51.2E",
    "모황도, 34-17.3N, 126-53.8E",
    "생일, 34-18.4N, 126-59.6E",
    "덕우도, 34-14.9N, 127-00.8E",
    "황제도, 34-11.4N, 127-04.5E",
    "소모도, 34-13.8N, 126-46.3E",
    "모도(모서), 34-12.1N, 126-45.1E",
    "모도(모동), 34-12.0N, 126-46.2E",
    "장도, 34-12.3N, 126-50.9E",
    "여서도, 33-59.3N, 126-55.3E",
    "땅끝, 34-17.9N, 126-31.8E",
    "흑일도, 34-17.0N, 126-33.6E",
    "산양, 34-13.6N, 126-34.6E",
    "횡간도, 34-14.1N, 126-36.6E",
    "넙도, 34-12.1N, 126-31.0E",
    "서넙도, 34-11.5N, 126-29.1E",
    "이목, 34-10.5N, 126-34.4E",
    "당사(완도군), 34-06.3N, 126-35.9E",
    "노력도, 34-27.9N, 126-58.0E",
    "가학, 34-27.3N, 127-01.3E",
    "남성, 34-19.0N, 126-36.4E",
    "동화도, 34-17.5N, 126-36.5E",
    "백일도, 34-17.7N, 126-35.5E",
    "마삭도, 34-14.6N, 126-34.1E",
    "일정, 34-21.7N, 126-59.3E",
    "당목, 34-22.7N, 126-56.8E",
    "서성, 34-20.3N, 126-59.7E",
    "화전, 34-21.1N, 127-01.0E",
    "목포, 34-46.9N, 126-23.1E",
    "비금‧도초, 34-43.0N, 125-56.1E",
    "흑산, 34-41.1N, 125-26.5E",
    "홍도, 34-41.0N, 125-11.6E",
    "다물도(다촌), 34-44.1N, 125-26.8E",
    "상태도, 34-26.1N, 125-17.1E",
    "하태도, 34-23.7N, 125-17.8E",
    "가거도, 34-03.0N, 125-07.7E",
    "만재도, 34-12.6N, 125-28.3E",
    "시하도, 34-41.9N, 126-14.6E",
    "마진도, 34-37.6N, 126-12.3E",
    "백야도, 34-36.8N, 126-10.8E",
    "율도(진도), 34-34.6N, 126-11.8E",
    "평사, 34-34.6N, 126-09.0E",
    "쉬미, 34-30.3N, 126-12.0E",
    "저도, 34-30.4N, 126-10.0E",
    "광대도, 34-31.8N, 126-06.2E",
    "송도(하태도), 34-31.2N, 126-05.7E",
    "혈도(가사), 34-30.9N, 126-05.2E",
    "양덕도, 34-29.7N, 126-06.3E",
    "주지도, 34-29.2N, 126-05.2E",
    "가사, 34-28.2N, 126-03.2E",
    "소성남도, 34-24.0N, 126-02.2E",
    "성남도, 34-23.7N, 126-02.7E",
    "옥도(조도), 34-21.0N, 126-01.1E",
    "내병도, 34-22.6N, 125-58.2E",
    "외병도, 34-22.5N, 125-56.6E",
    "눌옥도, 34-20.8N, 125-57.5E",
    "갈목도, 34-18.3N, 125-56.9E",
    "진목도, 34-18.6N, 125-57.7E",
    "창유, 34-18.4N, 126-03.2E",
    "율목, 34-19.3N, 126-01.2E",
    "라베, 34-18.6N, 126-00.8E",
    "관사도, 34-18.5N, 125-58.7E",
    "소마도(모도), 34-18.1N, 125-59.0E",
    "모도, 34-17.4N, 125-59.9E",
    "대마도, 34-16.3N, 125-59.9E",
    "관매도, 34-14.4N, 126-02.7E",
    "동거차도, 34-14.5N, 125-56.4E",
    "서거차도, 34-15.1N, 125-55.0E",
    "복호, 34-42.0N, 126-10.1E",
    "북강, 34-40.1N, 126-09.8E",
    "웅곡, 34-36.5N, 126-02.3E",
    "옥도(하의), 34-41.0N, 126-03.9E",
    "장병도, 34-39.2N, 126-03.2E",
    "자라도, 34-41.5N, 126-10.2E",
    "상태서리, 34-36.2N, 126-04.0E",
    "축강, 34-37.8N, 126-11.2E",
    "상태동리, 34-35.4N, 126-06.7E",
    "진도, 34-22.5N, 126-08.1E",
    "슬도, 34-15.7N, 126-09.1E",
    "독거도, 34-15.4N, 126-10.8E",
    "탄항(진도군), 34-14.5N, 126-10.4E",
    "혈도(진도), 34-13.5N, 126-09.7E",
    "청등도, 34-14.9N, 126-04.5E",
    "죽항도, 34-16.1N, 126-06.1E",
    "상하죽도, 34-15.0N, 125-55.4E",
    "곽도, 34-11.9N, 125-51.5E",
    "맹골도, 34-13.0N, 125-51.2E",
    "죽도(맹골), 34-13.2N, 125-50.8E",
    "각흘, 34-15.4N, 126-03.2E",
    "달리도, 34-46.7N, 126-19.8E",
    "장좌도, 34-47.4N, 126-20.1E",
    "율도(목포), 34-47.7N, 126-19.2E",
    "외달도, 34-47.0N, 126-17.9E",
    "막금도, 34-37.3N, 126-07.6E",
    "기도, 34-38.1N, 126-05.2E",
    "부소, 34-41.5N, 126-08.8E",
    "두리, 34-42.9N, 126-07.1E",
    "반월도, 34-42.4N, 126-05.6E",
    "문병도, 34-40.1N, 126-02.4E",
    "개도, 34-38.2N, 126-00.7E",
    "하의(당두), 34-36.9N, 126-00.8E",
    "대야도, 34-38.4N, 125-58.2E",
    "신도(신안군), 34-36.1N, 125-58.7E",
    "계마, 35-23.4N, 126-24.3E",
    "대석만도, 35-22.3N, 126-03.3E",
    "안마도, 35-20.7N, 126-01.1E",
    "우이1구, 34-37.2N, 125-51.4E",
    "동소우이도, 34-36.6N, 125-52.5E",
    "우이(예리), 34-36.1N, 125-50.8E",
    "우이2구, 34-36.3N, 125-49.5E",
    "목포(북항), 34-48.3N, 126-21.9E",
    "가산, 34-45.7N, 125-59.9E",
    "수치도, 34-44.7N, 126-00.7E",
    "남강, 34-48.2N, 126-07.2E",
    "읍동, 34-45.6N, 126-08.1E",
    "사치, 34-45.3N, 126-03.7E",
    "송공, 34-50.9N, 126-13.6E",
    "당사(신안군), 34-53.4N, 126-11.3E",
    "소악도, 34-55.1N, 126-12.1E",
    "매화(청돌), 34-55.1N, 126-13.1E",
    "대기점도, 34-56.6N, 126-12.8E",
    "병풍(나리), 34-57.3N, 126-13.0E",
    "향화도, 35-10.1N, 126-21.6E",
    "상낙월도, 35-12.0N, 126-08.7E",
    "진리(신안군), 35-04.9N, 126-07.3E",
    "점암, 35-05.4N, 126-09.4E",
    "봉리, 35-06.5N, 126-12.2E",
    "어의도, 35-07.8N, 126-11.3E",
    "목섬, 35-04.7N, 126-02.5E",
    "재원도, 35-05.0N, 126-01.9E",
    "송도(지도), 35-02.5N, 126-12.2E",
    "병풍(보기), 34-59.1N, 126-12.9E",
    "선도, 34-58.5N, 126-16.2E",
    "가룡, 34-55.3N, 126-18.3E",
    "매화(기섬), 34-54.8N, 126-15.3E",
    "마산도, 34-57.2N, 126-15.0E",
    "신월, 34-57.6N, 126-17.8E",
    "고이도, 34-57.6N, 126-17.4E",
    "사옥도(지신개), 35-01.5N, 126-10.1E",
    "증도, 34-56.8N, 126-07.4E",
    "자은, 34-55.1N, 126-05.5E",
    "송이도, 35-16.3N, 126-09.1E",
    "도초(시목), 34-40.1N, 125-57.0E",
    "상추자도, 33-57.7N, 126-17.9E",
    "우수영, 34-35.3N, 126-18.6E",
    "하추자도, 33-56.6N, 126-19.7E",
    "모슬포, 33-12.6N, 126-15.5E",
    "마라도 살레덕, 33-07.3N, 126-16.2E",
    "가파도, 33-10.5N, 126-16.3E",
    "산이수동, 33-12.4N, 126-17.5E",
    "여수, 34-44.3N, 127-44.0E",
    "나로, 34-27.9N, 127-27.2E",
    "손죽도, 34-17.4N, 127-21.7E",
    "대동, 34-14.5N, 127-14.6E",
    "거문, 34-01.7N, 127-18.5E",
    "여천, 34-33.1N, 127-45.1E",
    "유송, 34-32.1N, 127-45.8E",
    "우학, 34-30.5N, 127-46.3E",
    "안도, 34-29.4N, 127-48.2E",
    "서고지, 34-28.6N, 127-47.8E",
    "역포, 34-27.2N, 127-48.2E",
    "제도, 34-35.6N, 127-39.6E",
    "개도(화산), 34-35.0N, 127-40.0E",
    "자봉도, 34-35.3N, 127-41.1E",
    "송고, 34-33.0N, 127-43.7E",
    "함구미, 34-32.3N, 127-42.6E",
    "백야도, 34-37.2N, 127-38.5E",
    "하화도, 34-35.7N, 127-37.1E",
    "사도, 34-35.6N, 127-33.4E",
    "낭도, 34-36.2N, 127-32.3E",
    "상화도, 34-35.8N, 127-36.3E",
    "여석, 34-34.9N, 127-39.0E",
    "모전, 34-34.6N, 127-38.6E",
    "둔병도, 34-37.4N, 127-32.2E",
    "소거문도, 34-17.1N, 127-23.3E",
    "평도, 34-14.7N, 127-26.8E",
    "광도, 34-15.8N, 127-31.8E",
    "엑스포, 34-45.2N, 127-45.3E",
    "신기, 34-35.9N, 127-44.6E",
    "화태(마족), 34-35.1N, 127-44.3E",
    "직포, 34-30.5N, 127-44.3E",
    "군산, 35-58.7N, 126-37.9E",
    "장자도, 35-48.6N, 126-24.0E",
    "관리도, 35-49.1N, 126-22.5E",
    "방축도, 35-50.9N, 126-22.7E",
    "명도, 35-50.9N, 126-21.0E",
    "말도, 35-51.2N, 126-19.3E",
    "연도(군산시), 36-04.9N, 126-26.7E",
    "어청도, 36-07.1N, 125-59.0E",
    "개야도, 36-01.9N, 126-33.4E",
    "격포, 35-37.2N, 126-28.2E",
    "위도, 35-37.1N, 126-18.1E",
    "식도, 35-37.4N, 126-17.4E",
    "하왕등도, 35-38.4N, 126-07.1E",
    "상왕등도, 35-39.5N, 126-06.7E",
    "대천, 36-19.7N, 126-30.7E",
    "삽시도, 36-19.7N, 126-21.8E",
    "장고도, 36-24.0N, 126-21.3E",
    "고대도, 36-23.4N, 126-22.3E",
    "영목, 36-24.0N, 126-25.7E",
    "저두, 36-21.8N, 126-27.4E",
    "효자도, 36-22.7N, 126-26.4E",
    "선촌, 36-23.0N, 126-26.1E",
    "안흥신항, 36-40.9N, 126-08.0E",
    "가의(북항), 36-40.7N, 126-04.1E",
    "구도, 36-49.6N, 126-19.4E",
    "고파도, 36-54.8N, 126-20.4E",
    "호도, 36-18.2N, 126-15.9E",
    "녹도, 36-16.7N, 126-16.3E",
    "외연도, 36-13.4N, 126-04.8E",
    "도비도, 37-01.0N, 126-27.6E",
    "소난지도, 37-02.0N, 126-27.3E",
    "대난지도, 37-03.2N, 126-27.0E",
    "대난지도(해수욕장), 37-02.6N, 126-25.2E",
    "오천, 36-26.4N, 126-31.3E",
    "월도, 36-24.5N, 126-28.2E",
    "육도(보령시), 36-24.6N, 126-27.3E",
    "추도(보령시), 36-24.3N, 126-26.3E",
    "통영, 34-50.3N, 128-25.2E",
    "욕지도, 34-38.0N, 128-16.0E",
    "연화도, 34-39.0N, 128-21.1E",
    "우도, 34-39.3N, 128-20.7E",
    "한목, 34-45.5N, 128-18.1E",
    "추도(미조), 34-45.4N, 128-17.3E",
    "비진내, 34-44.0N, 128-27.6E",
    "비진외, 34-43.1N, 128-27.5E",
    "소매물도, 34-37.8N, 128-32.9E",
    "대항, 34-38.5N, 128-34.2E",
    "당금, 34-38.9N, 128-34.5E",
    "문어포, 34-47.8N, 128-27.8E",
    "제승당, 34-47.9N, 128-28.4E",
    "의항, 34-47.4N, 128-28.0E",
    "한산(관암), 34-48.9N, 128-28.1E",
    "가오치, 34-54.5N, 128-18.9E",
    "사량, 34-50.6N, 128-13.5E",
    "두미북구, 34-42.6N, 128-10.9E",
    "두미남구, 34-41.9N, 128-12.0E",
    "산등, 34-40.6N, 128-13.9E",
    "탄항(통영시), 34-40.4N, 128-15.3E",
    "하노대도, 34-40.1N, 128-15.1E",
    "삼천포, 34-55.4N, 128-05.2E",
    "삼덕, 34-47.7N, 128-23.0E",
    "저구, 34-43.9N, 128-36.3E",
    "용초, 34-44.7N, 128-28.9E",
    "호두, 34-44.4N, 128-30.2E",
    "죽도, 34-44.1N, 128-31.8E",
    "진두, 34-46.0N, 128-30.5E",
    "동좌, 34-48.0N, 128-30.5E",
    "서좌, 34-47.7N, 128-29.9E",
    "비산도, 34-48.7N, 128-29.8E",
    "화도, 34-49.7N, 128-28.6E",
    "국도, 34-32.8N, 128-26.6E",
    "중화, 34-47.4N, 128-23.3E",
    "미수, 34-49.6N, 128-23.8E",
    "포항, 36-03.1N, 129-22.7E",
    "울릉, 37-28.8N, 130-54.7E",
    "울릉(저동)(울릉군), 37-29.8N, 130-54.6E",
    "울릉(사동), 37-27.7N, 130-52.7E",
    "영일만신항, 36-05.8N, 129-26.4E",
    "후포, 36-40.7N, 129-27.7E",
    "독도(도착)(울릉군), 37-14.4N, 131-52.0E",
    "묵호, 37-33.0N, 129-06.8E",
    "강릉, 37-46.4N, 128-57.2E",
    "녹동(고흥군), 34-31.4N, 127-08.6E",
    "우두, 34-27.0N, 127-05.9E",
    "거문(서도), 34-03.2N, 127-17.8E",
    "금진, 34-29.5N, 127-07.4E",
    "금당(울포)(완도군), 34-25.5N, 127-04.5E",
    "신도(완도군), 34-23.3N, 127-03.0E",
    "충도, 34-22.8N, 127-04.4E",
    "동송, 34-21.5N, 127-03.8E",
    "연홍도, 34-27.6N, 127-05.6E",
    "도장, 34-22.1N, 127-00.6E",
    "신지(동고), 34-20.6N, 126-53.5E",
    "성산포, 33-28.4N, 126-56.1E",
    "초도(의성), 34-13.4N, 127-15.2E",
    "고사, 34-34.3N, 126-09.0E",
    "횡도, 35-20.1N, 125-59.8E",
    "후장구도, 34-11.9N, 126-29.5E",
    "하낙월도, 35-11.5N, 126-07.9E",
    "소기점도, 34-55.7N, 126-12.5E",
    "마안도, 34-12.5N, 126-30.9E",
    "대각시도, 35-11.0N, 126-12.7E",
    "규포, 34-37.0N, 127-33.1E",
    "삼천포(구항), 34-55.6N, 128-03.9E",
    "동도(거문), 34-02.8N, 127-18.6E",
    "연도(여수시), 34-25.7N, 127-47.7E",
    "추도(여수시), 34-35.6N, 127-34.0E",
    "노대도, 34-46.4N, 126-02.3E",
    "금평, 34-50.5N, 128-13.0E",
    "부산항북방파제, 35-03.5N, 129-04.5E",
    "오륙도등대, 35-05.5N, 129-07.5E",
    "가덕도등대, 35-00.1N, 128-49.7E",
    "격렬비열도등대, 36-36.5N, 125-32.7E",
    "팔미도등대, 37-21.2N, 126-31.0E",
    "간절곶등대, 35-21.5N, 129-22.3E",
    "호미곶등대, 36-04.6N, 129-34.1E",
    "울산항, 35-29.5N, 129-23.0E",
    "속초항, 38-12.5N, 128-35.7E",
    "동해항, 37-29.5N, 129-08.5E",
    "주문진항, 37-54.0N, 128-50.0E",
    "죽변항, 37-03.5N, 129-25.0E",
  ].join("\n"),
};

// ── 위경도 → 기준점 상대위치(방위 ○○방 △해리) 계산 ──
// "33-58-12N" 같은 도-분-초 또는 "33.97" 십진수를 십진 도로 변환
function parseCoord(str) {
  const s = String(str).trim();
  const dms = s.match(/^(\d{1,3})[-–\s](\d{1,2})[-–\s](\d{1,2}(?:\.\d+)?)\s*([NSEW])?$/i);
  if (dms) {
    let v = (+dms[1]) + (+dms[2]) / 60 + (+dms[3]) / 3600;
    if (/[SW]/i.test(dms[4] || "")) v = -v;
    return v;
  }
  const dm = s.match(/^(\d{1,3})[-–\s](\d{1,2}(?:\.\d+)?)\s*([NSEW])?$/i); // 도-분 형식
  if (dm) {
    let v = (+dm[1]) + (+dm[2]) / 60;
    if (/[SW]/i.test(dm[3] || "")) v = -v;
    return v;
  }
  const dec = parseFloat(s);
  return isNaN(dec) ? null : dec;
}
// 사고위치 문자열에서 위·경도 한 쌍 추출
function extractLatLon(posText) {
  const m = String(posText).match(/(\d{1,3}(?:[-–]\d{1,2}){1,2}(?:\.\d+)?)\s*N[,，]?\s*(\d{1,3}(?:[-–]\d{1,2}){1,2}(?:\.\d+)?)\s*E/i);
  if (m) return { lat: parseCoord(m[1] + "N"), lon: parseCoord(m[2] + "E") };
  // N/E 없는 도-분 좌표(예: "35-32.6 129-32.7")
  const dm = String(posText).match(/(\d{1,3})[-–](\d{1,2}(?:\.\d+)?)\s*[,，]?\s+(\d{2,3})[-–](\d{1,2}(?:\.\d+)?)/);
  if (dm) return { lat: +dm[1] + +dm[2] / 60, lon: +dm[3] + +dm[4] / 60 };
  const d = String(posText).match(/(\d{2}\.\d+)[,，\s]+(\d{3}\.\d+)/);
  if (d) return { lat: +d[1], lon: +d[2] };
  return null;
}
// 십진 위·경도 → 도-분 표기 "33-31.5N, 126-32.6E" (실시간 위치 표시용)
function fmtDM(lat, lon) {
  const one = (v, pos, neg) => { const h = v >= 0 ? pos : neg; v = Math.abs(v); const dg = Math.floor(v); return `${dg}-${((v - dg) * 60).toFixed(1)}${h}`; };
  return `${one(lat, "N", "S")}, ${one(lon, "E", "W")}`;
}
const DIR8 = ["북방", "북동방", "동방", "남동방", "남방", "남서방", "서방", "북서방"];
// 하버사인 거리(해리) + 초기 방위각
function relPosition(lat, lon, refText) {
  const refs = String(refText).split("\n").map((l) => l.split(",").map((x) => x.trim())).filter((a) => a.length >= 3)
    .map(([name, la, lo]) => ({ name, lat: parseCoord(la), lon: parseCoord(lo) }))
    .filter((r) => r.lat != null && r.lon != null);
  if (!refs.length) return null;
  const R = 3440.065; // 지구 반경(해리)
  const rad = (d) => (d * Math.PI) / 180;
  let best = null;
  for (const r of refs) {
    const dLat = rad(lat - r.lat), dLon = rad(lon - r.lon);
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(rad(r.lat)) * Math.cos(rad(lat)) * Math.sin(dLon / 2) ** 2;
    const distNM = 2 * R * Math.asin(Math.sqrt(a));
    // 기준점→사고지점 초기 방위각
    const y = Math.sin(dLon) * Math.cos(rad(lat));
    const x = Math.cos(rad(r.lat)) * Math.sin(rad(lat)) - Math.sin(rad(r.lat)) * Math.cos(rad(lat)) * Math.cos(dLon);
    const brg = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
    if (!best || distNM < best.distNM) best = { name: r.name, distNM, brg };
  }
  const dir = DIR8[Math.round(best.brg / 45) % 8];
  const dist = best.distNM < 10 ? best.distNM.toFixed(1) : Math.round(best.distNM);
  return `${best.name} ${dir} 약 ${dist}마일(방위 ${Math.round(best.brg)}°)`;
}

// ── 모의 데이터(API 실패 시 대체) ──
const VESSEL_DB = {
  "섬사랑3호": { 총톤수: "199톤", 여객정원: "104명", 선종: "연안여객선", 항로: "제주-추자" },
  "섬사랑12호": { 총톤수: "152톤", 여객정원: "92명", 선종: "연안여객선", 항로: "목포-도초" },
  "퀸제누비아2호": { 총톤수: "26,546톤", 여객정원: "1,010명", 선종: "카페리", 항로: "목포-제주" },
};
function mockWeather() {
  const r = (a, b, d = 0) => (Math.random() * (b - a) + a).toFixed(d);
  return { 지점: "모의 관측점", 풍향: ["북동", "북서", "남동", "남서"][Math.floor(Math.random() * 4)], 풍속: `${r(6, 14)}m/s`, 파고: `${r(0.8, 2.4, 1)}m`, 수온: `${r(16, 21)}℃` };
}

const EXAMPLE = "섬사랑12호가 위치 33-58.2N, 126-18.7E 제주 추자도 북동방 약 2해리 해상에서 운항 중 부유물(폐그물)이 프로펠러에 감겨 자력 항해 불가. 여객 28명·승무원 4명 승선.";

// ── 자연어 파싱(규칙 기반 — Claude API 실패 시 대체) ──
function ruleParse(text) {
  // "호"로 끝나는 선명 우선 → 쉼표 앞 단어 → 문장 맨 앞 토큰(좌표·숫자 앞) 순으로 인식
  let ship = (text.match(/([가-힣A-Za-z0-9]+호)/) || [])[1] || "";
  if (!ship) ship = (text.match(/^([가-힣A-Za-z0-9]+)(?=\s*,)/) || [])[1] || "";
  if (!ship) {
    // 예: "오션비스타제주 35-32.6 …" → "오션비스타제주" ("호"·쉼표 없이 공백/좌표가 뒤따르는 경우)
    const head = (text.trim().match(/^([가-힣A-Za-z][가-힣A-Za-z0-9]*)/) || [])[1] || "";
    if (head.length >= 2) ship = head.replace(/(에서|에게|으로|에|가|이|은|는|와|과|을|를|로)$/, "");
  }
  // 도-분(33-58.2N), 도-분-초(33-58-12N) 인식. N/E가 없으면 N/E를 보완해 정규화
  let pos = (text.match(/(\d{1,3}[-–]\d{1,2}(?:[-–]\d{1,2})?(?:\.\d+)?\s*N[,，]?\s*\d{1,3}[-–]\d{1,2}(?:[-–]\d{1,2})?(?:\.\d+)?\s*E)/i) || [])[1] || "";
  if (!pos) {
    // N/E 없이 공백 구분된 도-분 좌표(예: "35-32.6 129-32.7") → "35-32.6N 129-32.7E"로 정규화
    const m = text.match(/(\d{1,3}[-–]\d{1,2}(?:\.\d+)?)\s*[,，]?\s+(\d{2,3}[-–]\d{1,2}(?:\.\d+)?)/);
    if (m) pos = `${m[1]}N ${m[2]}E`;
  }
  const area = (text.match(/([가-힣]+\s*(?:북동방|남동방|북서방|남서방|동방|서방|남방|북방|인근|부근)[^,.\n]*)/) || [])[1] || "";
  const pax = (text.match(/여객\s*(\d+)\s*명/) || [])[1] || "";
  const crew = (text.match(/승무원\s*(\d+)\s*명/) || [])[1] || "";
  let summary = "";
  if (/부유물|폐그물|감김|감겨/.test(text)) summary = "부유물(폐그물) 프로펠러 감김으로 자력 항해 불가";
  else if (/이물질/.test(text)) summary = `${(text.match(/(좌현|우현|중앙)?\s*추진기/) || ["추진기"])[0].trim()} 이물질 걸림으로 자력 항해 불가`;
  else if (/좌초/.test(text)) summary = "좌초 발생";
  else if (/충돌/.test(text)) summary = "충돌 발생";
  else if (/화재/.test(text)) summary = "화재 발생";
  else if (/기관|엔진/.test(text)) summary = "기관 고장으로 자력 항해 불가";
  else if (/정선|표류/.test(text)) summary = "자력 항해 불가 (정선·표류)";
  return { 선박명: ship, 사고위치: [pos, area].filter(Boolean).join(" / "), 여객: pax, 승무원: crew, 사고개요: summary || text.slice(0, 60) };
}

// 파싱 프롬프트(Claude·Gemini 공통)
const PARSE_INSTRUCTION = (text) => `다음은 여객선 해양사고 보고자의 자유 입력입니다. 핵심 정보를 추출해 JSON으로만 응답하세요. 마크다운·설명 없이 순수 JSON만 출력합니다.\n키: 선박명("호"까지 포함), 사고위치(좌표·지명 포함), 여객(숫자만), 승무원(숫자만), 사고개요(한 문장).\n값을 알 수 없으면 "".\n\n입력: ${text}`;

async function aiParse(text, anthropicKey) {
  if (!anthropicKey) throw new Error("anthropicKey 미설정");
  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": anthropicKey,
      "anthropic-version": "2023-06-01",
      "anthropic-dangerous-direct-browser-access": "true",
    },
    body: JSON.stringify({
      model: "claude-haiku-4-5-20251001",
      max_tokens: 512,
      messages: [{
        role: "user",
        content: PARSE_INSTRUCTION(text),
      }],
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  const raw = data.content.map((i) => (i.type === "text" ? i.text : "")).join("");
  return JSON.parse(raw.replace(/```json|```/g, "").trim());
}

// ── Google Gemini 파싱 (브라우저 직접 호출) ──
async function geminiParse(text, geminiKey, model = "gemini-2.5-flash") {
  if (!geminiKey) throw new Error("geminiKey 미설정");
  const res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${encodeURIComponent(geminiKey)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      contents: [{ parts: [{ text: PARSE_INSTRUCTION(text) }] }],
      generationConfig: { responseMimeType: "application/json", maxOutputTokens: 512 },
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  const raw = (data.candidates?.[0]?.content?.parts || []).map((p) => p.text || "").join("");
  return JSON.parse(raw.replace(/```json|```/g, "").trim());
}

// ── 백엔드 응답 공통 파싱: HTML 등 비-JSON 응답을 명확한 오류로 변환 ──
async function backendJson(r, label) {
  if (!r.ok) throw new Error(`${label} HTTP ${r.status}`);
  const txt = await r.text();
  let d;
  try { d = JSON.parse(txt); }
  catch { throw new Error(`${label}: 백엔드 미연결 — JSON이 아닌 응답을 받았습니다(프론트 서버 응답일 수 있음). backend.py 실행 여부와 ⚙설정의 백엔드 주소(http://localhost:8000)를 확인하세요`); }
  if (d && d.error) throw new Error(d.error);
  return d;
}

// ── 백엔드 /parse 경유 (서버 .env 키 사용 — 보안 권장). 미실행·미설정 시 웹 직접 호출로 폴백 ──
async function backendParse(text, cfg) {
  const base = (cfg.proxy || "http://localhost:8000").replace(/\/$/, "");
  const r = await fetch(`${base}/parse`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return backendJson(r, "파싱");
}

// ── KOMSA 여객선 제원 조회 (backend.py /vessel 경유 — 브라우저 CORS 회피) ──
async function fetchVessel(name, cfg) {
  const base = (cfg.proxy || "http://localhost:8000").replace(/\/$/, "");
  const r = await fetch(`${base}/vessel?name=${encodeURIComponent(name)}`);
  return backendJson(r, "제원");
}

// ── KOMSA 운항항로 조회 (backend.py /route 경유) ──
async function fetchRoute(name, cfg) {
  const base = (cfg.proxy || "http://localhost:8000").replace(/\/$/, "");
  const r = await fetch(`${base}/route?name=${encodeURIComponent(name)}`);
  return backendJson(r, "운항항로");
}

// ── GICOMS VMS 실시간 위치 조회 (backend.py /vessel_position 경유) ──
// 신고문에 좌표가 없을 때 선명만으로 현재위치(AIS)를 가져온다. 미설정/미수신 시 backendJson이 예외 → 호출부에서 graceful 처리.
async function fetchVesselPosition(name, cfg) {
  const base = (cfg.proxy || "http://localhost:8000").replace(/\/$/, "");
  const r = await fetch(`${base}/vessel_position?name=${encodeURIComponent(name)}`);
  return backendJson(r, "실시간위치");
}

// ── 기상청 API허브 해상관측(sea_obs) 조회 ──
const DIRS = ["북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동", "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서"];
function tmString(offsetHours = 0) {
  const d = new Date(Date.now() + offsetHours * 3600 * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}${p(d.getHours())}00`;
}
// 지명만 입력된 경우 ⚙기준점 목록(refPoints)에서 좌표를 추정 — 최근접 부이 계산용 앵커
function geocodeFromRefs(locText, refText) {
  const refs = String(refText).split("\n").map((l) => l.split(",").map((x) => x.trim()))
    .filter((a) => a.length >= 3)
    .map(([name, la, lo]) => ({ name, lat: parseCoord(la), lon: parseCoord(lo) }))
    .filter((r) => r.lat != null && r.lon != null);
  for (const tok of String(locText).match(/[가-힣]{2,}/g) || []) {
    const hit = refs.find((r) => r.name.includes(tok.slice(0, 2)));
    if (hit) return { lat: hit.lat, lon: hit.lon };
  }
  return null;
}
async function fetchWeather(locText, cfg, llOverride) {
  // 기상청 직접 호출은 브라우저 CORS·HTML오류 페이지 문제가 있어 backend.py /weather 경유로 조회한다.
  // (백엔드가 .env의 KMA_KEY로 서버측에서 호출 → CSV 정상 수신·파싱)
  const base = (cfg.proxy || "http://localhost:8000").replace(/\/$/, "");
  // 사고 좌표를 함께 보내 백엔드가 '가장 가까운 부이'를 고르게 한다 (지명 매칭 실패 시 엉뚱한 부이 방지)
  // 우선순위: VMS 실시간 좌표(llOverride) → 신고문 좌표 → 기준점 목록 추정.
  const ll = (llOverride && llOverride.lat != null ? llOverride : null) || extractLatLon(locText) || geocodeFromRefs(locText, cfg.refPoints);
  const geo = ll && ll.lat != null && ll.lon != null ? `&lat=${ll.lat}&lon=${ll.lon}` : "";
  const r = await fetch(`${base}/weather?loc=${encodeURIComponent(locText)}${geo}`);
  return backendJson(r, "기상");
}
function parseSeaObs(text) {
  const num = (s) => { const n = parseFloat(s); return isNaN(n) || n <= -9 ? null : n; };
  return String(text).split("\n")
    .filter((l) => l.trim() && !l.trim().startsWith("#"))
    .map((l) => l.split(",").map((c) => c.trim()))
    .filter((t) => t.length >= 11)
    .map((t) => ({ tp: t[0], name: t[3], tm: t[1], wh: num(t[6]), wd: num(t[7]), ws: num(t[8]), tw: num(t[10]) }));
}

const now = () => new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

export default function App() {
  const [cfg, setCfg] = useState(() => {
    try {
      const saved = localStorage.getItem("sar_cfg");
      return saved ? { ...DEFAULT_CONFIG, ...JSON.parse(saved) } : DEFAULT_CONFIG;
    } catch { return DEFAULT_CONFIG; }
  });
  const [showCfg, setShowCfg] = useState(false);
  const [step, setStep] = useState(1);
  const [msgs, setMsgs] = useState([
    { who: "bot", text: "해양사고 신속 보고 챗봇입니다. 선박명·위치·승선인원·사고 내용 등 핵심 정보를 한 번에 입력해 주세요. 선박 제원(KOMSA)과 기상정보(기상청 API허브)를 실시간 자동 연계합니다." },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState(null);
  const [src, setSrc] = useState({ vessel: "", wx: "", route: "" }); // live | mock
  const [startAt, setStartAt] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const [sentFirst, setSentFirst] = useState(null);
  const [confirmFirst, setConfirmFirst] = useState(false);
  const [extra, setExtra] = useState({ 경위: "", 피해: "", 조치: "" });
  const [reviewed, setReviewed] = useState(false);
  const [sentFinal, setSentFinal] = useState(null);
  const [vesselList, setVesselList] = useState([]); // KOMSA 전체 선박(자동완성용)
  const [center, setCenter] = useState("운항관리센터"); // 보고 센터(정식 보고서 머리글)
  const [hwpxBusy, setHwpxBusy] = useState(false);
  const chatEnd = useRef(null);

  useEffect(() => { try { localStorage.setItem("sar_cfg", JSON.stringify(cfg)); } catch {} }, [cfg]);
  useEffect(() => { chatEnd.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs]);
  useEffect(() => {
    if (!startAt || sentFinal) return;
    const t = setInterval(() => setElapsed(Math.floor((Date.now() - startAt) / 1000)), 1000);
    return () => clearInterval(t);
  }, [startAt, sentFinal]);
  // 선명 자동완성용 KOMSA 전체 목록 로드 (마운트 시 + 보고서 진입 시 목록이 비어 있으면 재시도)
  const loadVessels = async () => {
    try {
      const base = (cfg.proxy || "http://localhost:8000").replace(/\/$/, "");
      const d = await backendJson(await fetch(`${base}/vessels`), "선박목록");
      setVesselList(d.items || []);
    } catch { setVesselList([]); }
  };
  useEffect(() => { loadVessels(); }, [cfg.proxy]);
  useEffect(() => { if (report && vesselList.length === 0) loadVessels(); }, [report]); // 백엔드를 늦게 켠 경우 자동 복구
  const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

  const SRC_BADGE = { live: { txt: "실데이터", bg: "#E6F4EC", bd: "#1B7F4E", fg: "#1B7F4E" }, mock: { txt: "모의(연결 실패)", bg: "#FDECEA", bd: "#C03221", fg: "#C03221" } };
  const Badge = ({ kind }) => kind ? <span style={{ fontSize: 10, fontWeight: 800, padding: "2px 6px", borderRadius: 4, marginLeft: 6, background: SRC_BADGE[kind].bg, border: `1px solid ${SRC_BADGE[kind].bd}`, color: SRC_BADGE[kind].fg }}>{SRC_BADGE[kind].txt}</span> : null;

  async function submit(textArg) {
    const text = (textArg ?? input).trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    setStartAt(Date.now());
    setMsgs((m) => [...m, { who: "user", text }]);

    // 파싱 우선순위: 백엔드(.env 키) → Gemini(웹) → Claude(웹) → 규칙(모두 실패 시)
    let parsed; let via = "규칙";
    const aiChain = [
      { name: "백엔드", run: () => backendParse(text, cfg) },
      cfg.geminiKey && { name: "Gemini(웹)", run: () => geminiParse(text, cfg.geminiKey) },
      cfg.anthropicKey && { name: "Claude(웹)", run: () => aiParse(text, cfg.anthropicKey) },
    ].filter(Boolean);
    for (const ai of aiChain) {
      try { parsed = await ai.run(); via = ai.name; break; } catch { /* 다음 후보로 폴백 */ }
    }
    if (!parsed || (!parsed.선박명 && !parsed.사고개요)) { parsed = ruleParse(text); via = "규칙"; }
    setMsgs((m) => [...m, { who: "bot", text: `입력 내용을 확인했습니다. (${via} 기반 자동 추출) 공공데이터 API를 호출합니다.` }]);

    // ── KOMSA 제원 ──
    let vessel = null, vSrc = "live";
    if (!parsed.선박명) {
      vSrc = "mock";
      setMsgs((m) => [...m, { who: "api", text: "입력에서 선박명을 찾지 못해 KOMSA 제원 조회를 건너뜁니다. 선박명을 포함해 다시 입력하거나 수기로 보완해 주세요.", live: false }]);
    } else {
      try {
        vessel = await fetchVessel(parsed.선박명, cfg);
        setMsgs((m) => [...m, { who: "api", text: `KOMSA 여객선 제원 API → ${parsed.선박명}: ${[vessel.총톤수 && `총톤수 ${vessel.총톤수}`, vessel.여객정원 && `여객정원 ${vessel.여객정원}`, vessel.선종 && `선종 ${vessel.선종}`, vessel.항로 && `항로 ${vessel.항로}`].filter(Boolean).join(", ") || "항목 매핑 확인 필요(원본 수신됨)"}`, live: true }]);
      } catch (e) {
        vessel = VESSEL_DB[parsed.선박명] || null; vSrc = "mock";
        setMsgs((m) => [...m, { who: "api", text: `KOMSA API 연결 실패(${e.message}) → ${vessel ? "모의 제원으로 대체" : "제원 미확보, 수기 보완 필요"}. 브라우저 CORS 차단일 수 있으니 ⚙설정에서 프록시 주소를 지정해 보세요.`, live: false }]);
      }
    }

    // ── GICOMS VMS 실시간 위치 (신고문에 좌표가 없으면 선명으로 현재위치 조회) ──
    let vpos = null;
    if (!extractLatLon(parsed.사고위치 || "") && parsed.선박명) {
      try {
        vpos = await fetchVesselPosition(parsed.선박명, cfg);
        if (vpos && vpos.위도 != null) {
          const spd = vpos.속력_kn != null ? `, ${vpos.속력_kn}kn` : "";
          setMsgs((m) => [...m, { who: "api", text: `GICOMS VMS 실시간 위치 → ${parsed.선박명}(${vpos.선박명 || ""}): ${fmtDM(vpos.위도, vpos.경도)}${spd} (수신 ${vpos.수신시각 || "-"})`, live: true }]);
        }
      } catch (e) {
        vpos = null;
        setMsgs((m) => [...m, { who: "api", text: `GICOMS VMS 실시간 위치 조회 불가(${e.message}) — 좌표 없이 진행`, live: false }]);
      }
    }
    const vll = vpos && vpos.위도 != null ? { lat: vpos.위도, lon: vpos.경도 } : null;

    // ── 기상청 해상관측 ──
    let wx = null, wSrc = "live";
    try {
      wx = await fetchWeather(parsed.사고위치 || "", cfg, vll);
      const awsTxt = wx.AWS ? ` · 인근 ${wx.AWS.지점}: 풍향 ${wx.AWS.풍향}, 풍속 ${wx.AWS.풍속}, 기온 ${wx.AWS.기온}` : "";
      setMsgs((m) => [...m, { who: "api", text: `기상청 해상관측 API → ${wx.지점}: 풍향 ${wx.풍향}, 풍속 ${wx.풍속}, 파고 ${wx.파고}${wx.파고출처 ? `(${wx.파고출처})` : ""}, 수온 ${wx.수온} (관측 ${wx.관측시각})${awsTxt}`, live: true }]);
    } catch (e) {
      wx = mockWeather(); wSrc = "mock";
      setMsgs((m) => [...m, { who: "api", text: `기상청 API 연결 실패(${e.message}) → 모의 기상으로 대체. ⚙설정에서 프록시 주소를 지정해 보세요.`, live: false }]);
    }

    // ── KOMSA 운항항로 ──
    let route = null, rSrc = "live";
    if (!parsed.선박명) {
      rSrc = "mock";
    } else {
      try {
        route = await fetchRoute(parsed.선박명, cfg);
        setMsgs((m) => [...m, { who: "api", text: `KOMSA 운항항로 API → ${parsed.선박명}: ${[route.면허항로 && `면허항로 ${route.면허항로}`, route.운항항로 && `운항항로 ${route.운항항로}`, route.운항상태 && `상태 ${route.운항상태}`, route.출발시각 && `출발 ${route.출발시각}`].filter(Boolean).join(" · ")}`, live: true }]);
      } catch (e) {
        rSrc = "mock";
        setMsgs((m) => [...m, { who: "api", text: `KOMSA 운항항로 API 연결 실패(${e.message}) → 항로 정보 수기 보완 필요`, live: false }]);
      }
    }

    setSrc({ vessel: vSrc, wx: wSrc, route: rSrc });
    // ── 기준점 상대위치 자동 계산 (신고문 좌표 우선, 없으면 VMS 실시간 좌표) ──
    // backend /relpos가 가장 가까운 기항지(항구) 기준으로 '○○항' 표기로 계산. 실패 시 클라이언트 폴백.
    let 상대위치 = "";
    const ll = extractLatLon(parsed.사고위치 || "") || vll;
    if (ll && ll.lat != null && ll.lon != null) {
      try {
        const base = (cfg.proxy || "http://localhost:8000").replace(/\/$/, "");
        const rp = await fetch(`${base}/relpos?lat=${ll.lat}&lon=${ll.lon}`);
        if (rp.ok) {
          const j = await rp.json();
          const dm = j.거리 < 10 ? j.거리.toFixed(1) : Math.round(j.거리);
          상대위치 = `${j.name} ${j.dir8}방 약 ${dm}마일(방위 ${j.방위}°)`;
        }
      } catch (e) { /* 백엔드 미연결 → 폴백 */ }
      if (!상대위치) 상대위치 = relPosition(ll.lat, ll.lon, cfg.refPoints) || "";
      if (상대위치) setMsgs((m) => [...m, { who: "api", text: `기준점 상대위치 자동 계산 → ${상대위치}`, live: true }]);
    }
    // 신고문에 위치가 없고 AIS로 현위치를 얻었으면 사고위치 칸에 좌표를 표기
    if (!(parsed.사고위치 || "").trim() && vll) {
      parsed.사고위치 = `${fmtDM(vll.lat, vll.lon)} (실시간 AIS · 수신 ${vpos.수신시각 || "-"})`;
    }
    const total = (parseInt(parsed.여객 || 0) + parseInt(parsed.승무원 || 0)) || "";
    setReport({ ...parsed, 원문: text, 상대위치, 합계: total, vessel, wx, route, 발생일시: `${new Date().toLocaleDateString("ko-KR")} ${now()}` });
    setMsgs((m) => [...m, { who: "bot", text: "1차(속보) 보고서 조안을 작성했습니다. 내용 확인 후 [발송] 버튼을 눌러주세요.", action: true }]);
    setBusy(false);
  }

  // ── 정식 해양사고 보고서(hwpx) 생성·다운로드 (backend.py /report/hwpx 경유) ──
  async function downloadHwpx() {
    if (!report || hwpxBusy) return;
    setHwpxBusy(true);
    const base = (cfg.proxy || "http://localhost:8000").replace(/\/$/, "");
    try {
      const res = await fetch(`${base}/report/hwpx`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ utterance: report.원문 || "", center, extra }),
      });
      if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try { const j = await res.json(); msg = j.error || msg; } catch { /* 비JSON 응답 */ }
        throw new Error(msg);
      }
      const blob = await res.blob();
      const date = new Date();
      const ymd = `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, "0")}${String(date.getDate()).padStart(2, "0")}`;
      const fname = `${report.선박명 || "해양사고"}_해양사고보고서_${ymd}.hwpx`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = fname;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      setMsgs((m) => [...m, { who: "bot", text: `정식 해양사고 보고서(hwpx)를 생성해 다운로드했습니다 → ${fname} (한글에서 열어 검토·보완 후 본부 보고)` }]);
    } catch (e) {
      setMsgs((m) => [...m, { who: "bot", text: `보고서(hwpx) 생성 실패: ${e.message} — backend.py 실행 여부와 pyhwpxlib 설치(pip install -r requirements.txt)를 확인하세요.` }]);
    } finally {
      setHwpxBusy(false);
    }
  }

  const S = styles;
  const steps = [
    { n: 1, label: "챗봇 입력", done: !!report },
    { n: 2, label: "1차 속보", done: !!sentFirst },
    { n: 3, label: "최종 보고", done: !!sentFinal },
  ];

  // 승선인원 셀: MTIS 점검표가 있으면 여객(성인/소아/유아)·선원·실승선 상세, 없으면 기본 표기
  const manifestCell = (r) => {
    if (!r.mtis) return `여객 ${r.여객 || "?"}명, 승무원 ${r.승무원 || "?"}명 (계 ${r.합계 || "?"}명)`;
    const m = r.mtis;
    return (
      <span>
        여객 <b>{m.여객}</b>명 <span style={{ color: "#5A6B80" }}>(성인 {m.대인} · 소아 {m.소인} · 유아 {m.유아})</span>, 선원 <b>{m.승무원}</b>명{m.임시승선자 ? `, 임시승선자 ${m.임시승선자}명` : ""} <b>(실승선 계 {m.실제승선인원}명)</b>
        <span style={{ fontSize: 10, fontWeight: 800, padding: "2px 6px", borderRadius: 4, marginLeft: 6, background: "#E6F4EC", border: "1px solid #1B7F4E", color: "#1B7F4E" }}>MTIS 출항전점검표</span>
      </span>
    );
  };
  const cargoCell = (m) => `실제 적재 ${m.화물적재중량} M/T${m.차량 ? ` · 차량 ${m.차량}대` : ""}`;

  // 운항항로 셀: "(항로) 출항시각 출항지출항" 형식 (MTIS 점검표 우선, 없으면 KOMSA 항로)
  const routeCell = (r) => {
    const m = r.mtis || {}, rt = r.route || {};
    const name = m.항로 || rt.운항항로 || rt.면허항로 || "";
    const hhmm = (t) => { t = String(t || ""); return t.length === 4 ? `${t.slice(0, 2)}:${t.slice(2)}` : t; };
    const time = m.출항시간 ? hhmm(m.출항시간) : (rt.출발시각 ? hhmm(rt.출발시각) : "");
    const dep = name.split(/[-~∼]/)[0].trim();
    return [name && `(${name})`, time, dep && `${dep}출항`].filter(Boolean).join(" ");
  };

  return (
    <div style={S.app}>
      <header style={S.header}>
        <div style={S.headerLeft}>
          <div style={S.badge}>실연동</div>
          <div>
            <div style={S.title}>해양사고 신속 보고 시스템</div>
            <div style={S.subtitle}>제주운항관리센터 · KOMSA·기상청 API 실시간 연계</div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <div style={S.timerBox}>
            <span style={S.timerLabel}>보고 소요시간</span>
            <span style={{ ...S.timerVal, color: sentFinal ? "#1B7F4E" : elapsed > 300 ? "#C03221" : "#0B2545" }}>
              {startAt ? fmt(sentFinal ? Math.floor((sentFinal.t - startAt) / 1000) : elapsed) : "--:--"}
            </span>
            <span style={S.timerHint}>목표 5분</span>
          </div>
          <button style={S.gearBtn} onClick={() => setShowCfg(!showCfg)}>⚙</button>
        </div>
      </header>

      {showCfg && (
        <div style={S.cfgPanel}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <div style={S.cfgTitle}>API 연동 설정</div>
            <button style={{ border: "1.5px solid #0B2545", background: "#0B2545", color: "#fff", borderRadius: 6, padding: "5px 18px", fontSize: 13, fontWeight: 800, cursor: "pointer" }}
              onClick={() => setShowCfg(false)}>닫기</button>
          </div>
          {[
            ["komsaUrl", "KOMSA 제원 요청주소 (여객선 제원 정보 조회)"],
            ["ferryRouteUrl", "KOMSA 운항항로 요청주소 (여객선 운항상태 정보 조회)"],
            ["komsaKey", "KOMSA 인증키 (serviceKey) — 두 KOMSA API 공통 사용"],
            ["kmaKey", "기상청 API허브 인증키 (authKey) — apihub.kma.go.kr 재발급 필요"],
            ["geminiKey", "Google Gemini API 키 (선택 — 입력 파싱에 우선 사용)"],
            ["anthropicKey", "Anthropic(Claude) API 키 (선택 — Gemini 실패 시 대체)"],
            ["proxy", "프록시 주소 (CORS 차단 시 필수 — proxy.py 실행 후 http://localhost:8000 입력)"],
          ].map(([k, label]) => (
            <div key={k} style={{ marginBottom: 8 }}>
              <div style={S.formLabel}>{label}</div>
              <input style={{ ...S.textarea, fontFamily: "monospace", fontSize: 12 }} value={cfg[k]} onChange={(e) => setCfg({ ...cfg, [k]: e.target.value })} />
            </div>
          ))}
          <div style={{ marginBottom: 8 }}>
            <div style={S.formLabel}>위치 기준점 목록 — 한 줄에 하나: 이름, 위도, 경도 (도-분 표기 35-34.3N / 129-52.1E 권장)</div>
            <textarea style={{ ...S.textarea, fontFamily: "monospace", fontSize: 12 }} rows={4}
              value={cfg.refPoints} onChange={(e) => setCfg({ ...cfg, refPoints: e.target.value })} />
          </div>
          <div style={{ fontSize: 11, color: "#8295AB", lineHeight: 1.6, marginTop: 4 }}>
            ※ 설정은 브라우저에 자동 저장됩니다. 기상청 API 직접 호출은 CORS 차단 → proxy.py 실행 후 프록시 주소 입력 필요.
          </div>
        </div>
      )}

      <nav style={S.rail}>
        {steps.map((s, i) => (
          <div key={s.n} style={S.railItem}>
            <button onClick={() => { if (s.n === 1 || report) setStep(s.n); }}
              style={{ ...S.railBtn, ...(step === s.n ? S.railBtnActive : {}), ...(s.done ? S.railBtnDone : {}) }}>
              <span style={S.railNum}>{s.done ? "✓" : s.n}</span>{s.label}
            </button>
            {i < steps.length - 1 && <div style={S.railLine} />}
          </div>
        ))}
      </nav>

      <main style={S.main}>
        {step === 1 && (
          <section style={S.panel}>
            <div style={S.panelHead}>① 보고자가 SNS 챗봇에 핵심 정보만 입력</div>
            <div style={S.chat}>
              {msgs.map((m, i) => (
                <div key={i} style={{ display: "flex", justifyContent: m.who === "user" ? "flex-end" : "flex-start" }}>
                  <div style={m.who === "user" ? S.bubbleUser : m.who === "api" ? { ...S.bubbleApi, ...(m.live === false ? S.bubbleApiFail : {}) } : S.bubbleBot}>
                    {m.who === "api" && <span style={{ ...S.apiTag, color: m.live === false ? "#C03221" : "#B07400" }}>{m.live === false ? "API 연결 실패 — 대체 데이터" : "API 실시간 연계"}</span>}
                    {m.text}
                    {m.action && report && <button style={S.primaryBtn} onClick={() => setStep(2)}>1차 보고서 확인하기 →</button>}
                  </div>
                </div>
              ))}
              {busy && <div style={S.bubbleBot}>분석·조회 중…</div>}
              <div ref={chatEnd} />
            </div>
            <div style={S.inputRow}>
              <textarea style={S.textarea} rows={2}
                placeholder="예: 섬사랑12호, 추자도 북동방 2해리, 여객 28명 승무원 4명, 부유물 프로펠러 감김"
                value={input} onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }} />
              <button style={S.sendBtn} onClick={() => submit()} disabled={busy}>입력</button>
            </div>
            <button style={S.exampleBtn} onClick={() => submit(EXAMPLE)} disabled={busy}>가상 사례로 시연하기 (섬사랑12호 부유물 감김)</button>
          </section>
        )}

        {step === 2 && report && (
          <section style={S.panel}>
            <div style={S.panelHead}>② 1차(속보) 보고서 자동 작성 → 확인 후 운항상황센터 신속 전파</div>
            <table style={S.table}>
              <tbody>
                <Row k="발생일시" v={report.발생일시} />
                <Row k="선박명" v={<VesselPicker value={report.선박명} vessel={report.vessel} src={src.vessel} list={vesselList}
                  onPick={(x) => { setReport((r) => ({ ...r, 선박명: x.선박명, vessel: x.vessel })); setSrc((s) => ({ ...s, vessel: x.src })); }} />} />
                <Row k="사고위치" v={<span>{report.사고위치 || "확인 중"}{report.상대위치 && <span style={{ display: "block", fontWeight: 700, color: "#0B2545" }}>※ {report.상대위치} <span style={{ fontSize: 11, color: "#8295AB", fontWeight: 400 }}>(기준점 자동 계산)</span></span>}</span>} />
                <Row k="승선인원" v={manifestCell(report)} />
                <Row k="실승선 조회" v={<MtisPredep vessel={report.vessel} cfg={cfg} onFill={(d) => setReport((r) => ({ ...r, 여객: d.여객, 승무원: d.승무원, 합계: (d.여객 || 0) + (d.승무원 || 0), mtis: d }))} />} />
                {report.mtis && <Row k="화물적재" v={cargoCell(report.mtis)} />}
                <Row k="사고개요" v={report.사고개요} />
                {(report.route || report.mtis) && <Row k="운항항로" v={<span>{routeCell(report)}{report.mtis ? <span style={{ fontSize: 10, fontWeight: 800, padding: "2px 6px", borderRadius: 4, marginLeft: 6, background: "#E6F4EC", border: "1px solid #1B7F4E", color: "#1B7F4E" }}>MTIS 출항전점검표</span> : <Badge kind={src.route} />}</span>} />}
                <Row k="기상상황" v={<span>{`${report.wx.지점 || ""} 풍향 ${report.wx.풍향}, 풍속 ${report.wx.풍속}, 파고 ${report.wx.파고}${report.wx.파고출처 ? `(${report.wx.파고출처})` : ""}${report.wx.수온 ? `, 수온 ${report.wx.수온}` : ""}${report.wx.AWS ? ` / 인근 ${report.wx.AWS.지점} 풍향 ${report.wx.AWS.풍향}, 풍속 ${report.wx.AWS.풍속}, 기온 ${report.wx.AWS.기온}` : ""}`}<Badge kind={src.wx} /></span>} />
                <Row k="조치사항" v="해경 및 해사안전 감독관 보고, 여객 안내방송 및 승객 구명조끼 착용 후 선내 대기 중" />
              </tbody>
            </table>
            {!sentFirst ? (
              <div style={S.sendArea}>
                <label style={S.checkRow}>
                  <input type="checkbox" checked={confirmFirst} onChange={(e) => setConfirmFirst(e.target.checked)} />
                  보고자 본인이 내용을 확인했습니다 (오발송 방지 — 확인 필수)
                </label>
                <button style={{ ...S.primaryBtnLg, opacity: confirmFirst ? 1 : 0.4 }} disabled={!confirmFirst}
                  onClick={() => { setSentFirst(now()); setStep(3); }}>
                  [발송] 운항상황센터 전파
                </button>
              </div>
            ) : (
              <div style={S.sentBanner}>✓ {sentFirst} 운항상황센터 전파 완료 · 모바일(SNS) 해경 등 관계기관 동시 통보</div>
            )}
          </section>
        )}

        {step === 3 && report && (
          <section style={S.panel}>
            {sentFirst && <div style={S.sentBanner}>✓ 1차 속보 {sentFirst} 전파 완료 — 골든타임 확보, 아래 정식 보고서 보완 후 본부 보고</div>}
            <div style={S.panelHead}>③ 해양사고 보고서(최종·규정 서식) 자동 작성 → 운항관리자 검토·확인 후 본부 정식 보고</div>
            <table style={S.table}>
              <tbody>
                <Row k="보고구분" v="최종 보고 (규정 서식)" />
                <Row k="자동 반영" v="1차 입력 정보 + API 연계 데이터(제원·기상) 자동 채움" />
                <Row k="발생일시" v={report.발생일시} />
                <Row k="선박명" v={report.vessel ? `${report.선박명} / ${report.vessel.선종 || "—"} / ${report.vessel.총톤수 || "—"}${report.vessel.선사 ? ` / ${report.vessel.선사}` : ""}` : report.선박명} />
                <Row k="사고위치" v={<span>{report.사고위치 || "—"}{report.상대위치 && <span style={{ display: "block", fontWeight: 700 }}>※ {report.상대위치}</span>}</span>} />
                <Row k="승선인원" v={manifestCell(report)} />
                {report.mtis && <Row k="화물적재" v={cargoCell(report.mtis)} />}
                <Row k="사고개요" v={report.사고개요} />
                {(report.route || report.mtis) && <Row k="운항항로" v={routeCell(report)} />}
              </tbody>
            </table>
            <div style={S.formGrid}>
              {[["경위", "사고 경위 (추가 기재)"], ["피해", "피해 상황"], ["조치", "후속 조치 계획"]].map(([key, label]) => (
                <div key={key}>
                  <div style={S.formLabel}>{label}</div>
                  <textarea style={S.textarea} rows={2} value={extra[key]} disabled={!!sentFinal}
                    onChange={(e) => setExtra({ ...extra, [key]: e.target.value })}
                    placeholder={key === "경위" ? "예: 추자항 출항 후 10분경 프로펠러 이상 진동 감지…" : key === "피해" ? "예: 인명피해 없음, 추진기 손상 여부 점검 예정" : "예: 예인선 도착 후 추자항 예인, 정밀 점검 실시"} />
                </div>
              ))}
            </div>
            <div style={{ marginTop: 12, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ ...S.formLabel, marginBottom: 0 }}>보고 센터</span>
              <input style={{ ...S.textarea, width: 220, padding: "8px 10px" }} value={center} disabled={!!sentFinal}
                onChange={(e) => setCenter(e.target.value)} placeholder="예: 여수운항관리센터" />
              <button style={{ ...S.primaryBtnLg, background: "#0B5394", padding: "10px 16px", opacity: hwpxBusy ? 0.5 : 1 }}
                disabled={hwpxBusy} onClick={downloadHwpx}>
                {hwpxBusy ? "보고서 생성 중…" : "📄 정식 보고서(hwpx) 다운로드"}
              </button>
              <span style={{ fontSize: 12, color: "#5A6B80" }}>공폼 서식 · 회사 선박마스터·기상·제원 자동 반영 · 한글에서 보완</span>
            </div>
            {!sentFinal ? (
              <div style={S.sendArea}>
                <label style={S.checkRow}>
                  <input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} />
                  현장 운항관리자가 내용을 검토·확인했습니다
                </label>
                <button style={{ ...S.primaryBtnLg, opacity: reviewed ? 1 : 0.4 }} disabled={!reviewed}
                  onClick={() => setSentFinal({ at: now(), t: Date.now() })}>
                  [발송] 본부 정식 보고
                </button>
              </div>
            ) : (
              <div style={{ ...S.sentBanner, background: "#E6F4EC", borderColor: "#1B7F4E" }}>
                ✓ {sentFinal.at} 본부 정식 보고 완료 · 총 보고 소요시간 {fmt(Math.floor((sentFinal.t - startAt) / 1000))} (기존 평균 25분 → 목표 5분 이내)
              </div>
            )}
          </section>
        )}
      </main>

      <footer style={S.footer}>
        제원(KOMSA)·기상(기상청 API허브) 실연동 — 연결 실패 시 모의 데이터로 대체되며 배지로 구분 표시됩니다. 자연어 추출은 Claude API.
      </footer>
    </div>
  );
}

function Row({ k, v }) {
  return (
    <tr>
      <th style={styles.th}>{k}</th>
      <td style={styles.td}>{v}</td>
    </tr>
  );
}

// 선명 입력칸 + KOMSA 전체목록 자동완성 (2글자 이상 입력 → 목록 필터 → 선택 시 제원 자동 채움)
function VesselPicker({ value, vessel, src, list, onPick }) {
  const [q, setQ] = useState(value || "");
  const [open, setOpen] = useState(false);
  useEffect(() => { setQ(value || ""); }, [value]);
  const norm = (s) => String(s).replace(/\s/g, "");
  const matches = q.trim().length >= 2
    ? list.filter((v) => norm(v.선박명).includes(norm(q))).slice(0, 10)
    : [];
  const spec = vessel
    ? [vessel.총톤수 && `총톤수 ${vessel.총톤수}`, vessel.여객정원 && `여객정원 ${vessel.여객정원}`, vessel.선종, vessel.선사].filter(Boolean).join(" · ")
    : "";
  return (
    <div style={{ position: "relative" }}>
      <input
        style={{ ...styles.textarea, width: "100%" }}
        value={q}
        placeholder={list.length ? `선박명 2글자 이상 입력 → 목록에서 선택 (KOMSA ${list.length}척)` : "선박 목록 로딩 실패 — backend.py /vessels 확인"}
        onChange={(e) => { setQ(e.target.value); setOpen(true); onPick({ 선박명: e.target.value, vessel: null, src: "" }); }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
      />
      {open && matches.length > 0 && (
        <div style={styles.acBox}>
          {matches.map((v, i) => (
            <div key={i} style={styles.acItem}
              onMouseDown={() => { onPick({ 선박명: v.선박명, vessel: v, src: "live" }); setQ(v.선박명); setOpen(false); }}>
              <b>{v.선박명}</b>
              <span style={{ color: "#5A6B80", fontSize: 12, marginLeft: 6 }}>{[v.총톤수, v.여객정원, v.선종].filter(Boolean).join(" · ")}</span>
            </div>
          ))}
        </div>
      )}
      {open && q.trim().length >= 2 && matches.length === 0 && (
        <div style={styles.acBox}>
          <div style={{ ...styles.acItem, color: "#8295AB" }}>
            {list.length > 0
              ? "일치하는 선박이 없습니다 (KOMSA 미등록)"
              : "선박 목록을 불러오지 못했습니다 — backend.py 실행 및 ⚙백엔드 주소(http://localhost:8000) 확인"}
          </div>
        </div>
      )}
      {spec && (
        <div style={{ marginTop: 6, fontSize: 13 }}>
          {spec}
          <span style={{ fontSize: 10, fontWeight: 800, padding: "2px 6px", borderRadius: 4, marginLeft: 6, background: src === "live" ? "#E6F4EC" : "#FDECEA", border: `1px solid ${src === "live" ? "#1B7F4E" : "#C03221"}`, color: src === "live" ? "#1B7F4E" : "#C03221" }}>
            {src === "live" ? "실데이터" : "모의(연결 실패)"}
          </span>
        </div>
      )}
    </div>
  );
}

// MTIS 출항전 안전점검표 → 작성 시각 기준 '가장 최근' 점검표의 실제 승선인원/화물 자동 조회
// (로그인 불필요, 백엔드 /predep 경유 — 선박코드만으로 최신 항차 자동 선택)
function MtisPredep({ vessel, cfg, onFill }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const code = vessel && vessel.선박코드;
  const fmtDate = (s) => (s && s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}` : s);
  const fmtTime = (s) => (s && s.length === 4 ? `${s.slice(0, 2)}:${s.slice(2)}` : s);
  const run = async () => {
    if (!code) { setMsg({ ok: false, text: "선박을 자동완성에서 먼저 선택하세요 (선박코드 필요)" }); return; }
    setBusy(true); setMsg(null);
    try {
      const base = (cfg.proxy || "http://localhost:8000").replace(/\/$/, "");
      const url = `${base}/predep?psnshpCd=${encodeURIComponent(code)}&name=${encodeURIComponent(vessel.선박명 || "")}`;
      const d = await backendJson(await fetch(url), "MTIS점검표");
      onFill(d);
      setMsg({ ok: true, text: `최근 점검표(${fmtDate(d.출항일)} ${fmtTime(d.출항시간)} · ${d.항로}) → 여객 ${d.여객}명(성인 ${d.대인}·소아 ${d.소인}·유아 ${d.유아}) / 선원 ${d.승무원}명 / 실승선 ${d.실제승선인원}명 / 화물 ${d.화물적재중량}M/T` });
    } catch (e) { setMsg({ ok: false, text: `조회 실패: ${e.message}` }); }
    setBusy(false);
  };
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
      <button style={{ ...styles.sendBtn, padding: "7px 14px", fontSize: 12 }} onClick={run} disabled={busy}>{busy ? "조회중…" : "MTIS 최근 출항전점검표 조회"}</button>
      <span style={{ fontSize: 11, color: "#8295AB" }}>작성 시각 기준 가장 최근 점검표의 실제 승선인원을 자동 반영</span>
      {msg && <span style={{ fontSize: 12, color: msg.ok ? "#1B7F4E" : "#C03221", flexBasis: "100%", lineHeight: 1.5 }}>{msg.text}</span>}
    </div>
  );
}

const styles = {
  app: { minHeight: "100vh", background: "#EDF1F5", fontFamily: "'Apple SD Gothic Neo','Malgun Gothic','Noto Sans KR',sans-serif", color: "#16263B", display: "flex", flexDirection: "column" },
  header: { background: "#0B2545", color: "#fff", padding: "14px 18px", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" },
  headerLeft: { display: "flex", alignItems: "center", gap: 12 },
  badge: { background: "#F5A623", color: "#0B2545", fontWeight: 800, fontSize: 12, padding: "4px 8px", borderRadius: 4, letterSpacing: 1 },
  title: { fontSize: 18, fontWeight: 800, letterSpacing: -0.3 },
  subtitle: { fontSize: 12, opacity: 0.75, marginTop: 2 },
  timerBox: { display: "flex", alignItems: "baseline", gap: 8, background: "#fff", borderRadius: 8, padding: "8px 14px" },
  timerLabel: { fontSize: 11, color: "#5A6B80", fontWeight: 700 },
  timerVal: { fontFamily: "'SF Mono','Consolas',monospace", fontSize: 22, fontWeight: 800 },
  timerHint: { fontSize: 11, color: "#8295AB" },
  gearBtn: { border: "none", background: "rgba(255,255,255,.15)", color: "#fff", borderRadius: 8, width: 38, height: 38, fontSize: 17, cursor: "pointer" },
  cfgPanel: { background: "#fff", borderBottom: "1px solid #D8E1EA", padding: "14px 18px" },
  cfgTitle: { fontSize: 13, fontWeight: 800, color: "#0B2545", marginBottom: 10 },
  rail: { display: "flex", alignItems: "center", padding: "14px 18px 0", flexWrap: "wrap" },
  railItem: { display: "flex", alignItems: "center" },
  railBtn: { display: "flex", alignItems: "center", gap: 8, border: "1.5px solid #C5D1DE", background: "#fff", borderRadius: 999, padding: "8px 16px", fontSize: 13, fontWeight: 700, color: "#5A6B80", cursor: "pointer" },
  railBtnActive: { borderColor: "#0B2545", color: "#0B2545", boxShadow: "0 2px 8px rgba(11,37,69,.12)" },
  railBtnDone: { background: "#E6F4EC", borderColor: "#1B7F4E", color: "#1B7F4E" },
  railNum: { width: 20, height: 20, borderRadius: "50%", background: "currentColor", color: "#fff", display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: 11, fontWeight: 800 },
  railLine: { width: 28, height: 2, background: "#C5D1DE", margin: "0 4px" },
  main: { flex: 1, padding: 18, maxWidth: 880, width: "100%", margin: "0 auto", boxSizing: "border-box" },
  panel: { background: "#fff", borderRadius: 12, border: "1px solid #D8E1EA", padding: 18, boxShadow: "0 1px 4px rgba(11,37,69,.06)" },
  panelHead: { fontSize: 15, fontWeight: 800, color: "#0B2545", paddingBottom: 12, borderBottom: "2px solid #0B2545", marginBottom: 14 },
  chat: { display: "flex", flexDirection: "column", gap: 10, maxHeight: 380, overflowY: "auto", padding: "4px 2px", marginBottom: 12 },
  bubbleBot: { background: "#F0F4F8", borderRadius: "4px 14px 14px 14px", padding: "10px 14px", fontSize: 14, maxWidth: "88%", lineHeight: 1.6 },
  bubbleUser: { background: "#0B2545", color: "#fff", borderRadius: "14px 4px 14px 14px", padding: "10px 14px", fontSize: 14, maxWidth: "88%", lineHeight: 1.6 },
  bubbleApi: { background: "#FFF7E8", border: "1px solid #F5A623", borderRadius: 10, padding: "10px 14px", fontSize: 13, maxWidth: "88%", lineHeight: 1.6, fontFamily: "'SF Mono','Consolas',monospace" },
  bubbleApiFail: { background: "#FDECEA", borderColor: "#C03221" },
  apiTag: { display: "block", fontSize: 10, fontWeight: 800, letterSpacing: 1, marginBottom: 4, fontFamily: "inherit" },
  inputRow: { display: "flex", gap: 8 },
  textarea: { flex: 1, width: "100%", boxSizing: "border-box", border: "1.5px solid #C5D1DE", borderRadius: 8, padding: "10px 12px", fontSize: 14, fontFamily: "inherit", resize: "vertical", lineHeight: 1.5 },
  sendBtn: { border: "none", background: "#1B6CB0", color: "#fff", borderRadius: 8, padding: "0 18px", fontSize: 14, fontWeight: 800, cursor: "pointer" },
  exampleBtn: { marginTop: 10, width: "100%", border: "1.5px dashed #1B6CB0", background: "#F2F8FD", color: "#1B6CB0", borderRadius: 8, padding: "10px", fontSize: 13, fontWeight: 700, cursor: "pointer" },
  primaryBtn: { display: "block", marginTop: 10, border: "none", background: "#1B6CB0", color: "#fff", borderRadius: 8, padding: "9px 14px", fontSize: 13, fontWeight: 800, cursor: "pointer" },
  primaryBtnLg: { border: "none", background: "#0B2545", color: "#fff", borderRadius: 8, padding: "12px 22px", fontSize: 15, fontWeight: 800, cursor: "pointer" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 14, marginBottom: 14 },
  th: { width: 110, background: "#DCE7F2", border: "1px solid #B9C9D9", padding: "9px 10px", fontWeight: 800, color: "#0B2545", textAlign: "left", verticalAlign: "top" },
  td: { border: "1px solid #B9C9D9", padding: "9px 12px", lineHeight: 1.6 },
  sendArea: { display: "flex", flexDirection: "column", gap: 10, alignItems: "flex-start", marginTop: 6 },
  checkRow: { display: "flex", alignItems: "center", gap: 8, fontSize: 13, fontWeight: 600, color: "#3D5168" },
  sentBanner: { background: "#FFF7E8", border: "1.5px solid #F5A623", borderRadius: 8, padding: "11px 14px", fontSize: 13, fontWeight: 700, color: "#0B2545", marginBottom: 14 },
  formGrid: { display: "grid", gap: 12, marginBottom: 14 },
  formLabel: { fontSize: 12, fontWeight: 800, color: "#3D5168", marginBottom: 5 },
  acBox: { position: "absolute", top: "100%", left: 0, right: 0, zIndex: 30, background: "#fff", border: "1px solid #B9C9D9", borderRadius: 8, marginTop: 2, maxHeight: 260, overflowY: "auto", boxShadow: "0 6px 18px rgba(11,37,69,.18)" },
  acItem: { padding: "8px 12px", fontSize: 14, cursor: "pointer", borderBottom: "1px solid #EEF2F6", lineHeight: 1.4 },
  footer: { textAlign: "center", fontSize: 11, color: "#8295AB", padding: "10px 16px 18px", lineHeight: 1.5 },
};
