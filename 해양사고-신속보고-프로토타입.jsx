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
  refPoints: [
    "추자도등대, 33-57.5N, 126-18.1E",
    "제주항북방파제, 33-31.5N, 126-32.7E",
    "한림항방파제, 33-25.0N, 126-15.5E",
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
async function fetchWeather(locText, cfg) {
  // 기상청 직접 호출은 브라우저 CORS·HTML오류 페이지 문제가 있어 backend.py /weather 경유로 조회한다.
  // (백엔드가 .env의 KMA_KEY로 서버측에서 호출 → CSV 정상 수신·파싱)
  const base = (cfg.proxy || "http://localhost:8000").replace(/\/$/, "");
  // 사고 좌표를 함께 보내 백엔드가 '가장 가까운 부이'를 고르게 한다 (지명 매칭 실패 시 엉뚱한 부이 방지)
  // 좌표가 없으면 기준점 목록으로 좌표를 추정해 앵커로 사용한다.
  const ll = extractLatLon(locText) || geocodeFromRefs(locText, cfg.refPoints);
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

    // ── 기상청 해상관측 ──
    let wx = null, wSrc = "live";
    try {
      wx = await fetchWeather(parsed.사고위치 || "", cfg);
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
    // ── 기준점 상대위치 자동 계산 ──
    let 상대위치 = "";
    const ll = extractLatLon(parsed.사고위치 || "");
    if (ll && ll.lat != null && ll.lon != null) {
      상대위치 = relPosition(ll.lat, ll.lon, cfg.refPoints) || "";
      if (상대위치) setMsgs((m) => [...m, { who: "api", text: `기준점 상대위치 자동 계산 → ${상대위치}`, live: true }]);
    }
    const total = (parseInt(parsed.여객 || 0) + parseInt(parsed.승무원 || 0)) || "";
    setReport({ ...parsed, 상대위치, 합계: total, vessel, wx, route, 발생일시: `${new Date().toLocaleDateString("ko-KR")} ${now()}` });
    setMsgs((m) => [...m, { who: "bot", text: "1차(속보) 보고서 조안을 작성했습니다. 내용 확인 후 [발송] 버튼을 눌러주세요.", action: true }]);
    setBusy(false);
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
