import os
import json
import time
import re
import requests
import httpx
import math
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from openai import OpenAI

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from datetime import datetime, timedelta, date

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ───────────────── 기본 설정 ─────────────────
TOKEN = os.getenv("BOT_TOKEN")
APP_URL = (os.getenv("APP_URL") or "").strip()
CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()  # 예: @채널아이디 또는 -100xxxxxxxxxxxx

# 🔴 여기만 네 봇 유저네임으로 수정하면 됨 (@ 빼고)
BOT_USERNAME = "castlive_bot"  # 예: @castlive_bot 이라면 "castlive_bot"

# 🔹 Gemini API 키 (환경변수에 설정)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# 🔹 관리자 ID 목록 (쉼표로 여러 명 가능) 예: "123456789,987654321"
_admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [
    int(x.strip())
    for x in _admin_ids_raw.split(",")
    if x.strip().isdigit()
]


def is_admin(update: Update) -> bool:
    """이 명령어를 누가 호출했는지 확인해서, 관리자면 True 리턴"""
    if not ADMIN_IDS:
        # ADMIN_IDS를 안 넣었으면 그냥 모두 허용 (테스트용)
        return True
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


# ───────────────── 날짜 헬퍼 ─────────────────

def get_kst_now() -> datetime:
    """한국 시간 기준 현재 시각 (UTC+9)"""
    return datetime.utcnow() + timedelta(hours=9)


def get_date_labels():
    """
    오늘 / 내일 날짜를 'M.DD' 형식으로 돌려줌
    예: ( '11.14', '11.15' )
    """
    now_kst = get_kst_now().date()
    today = now_kst
    tomorrow = now_kst + timedelta(days=1)

    today_str = f"{today.month}.{today.day:02d}"
    tomorrow_str = f"{tomorrow.month}.{tomorrow.day:02d}"
    return today_str, tomorrow_str

def get_tomorrow_mmdd_str() -> str:
    """
    mazgtv 테이블의 '11-28 (금) 02:45' 같은 날짜에서
    앞부분 'MM-DD' 와 비교하기 위한 내일 날짜 문자열 생성 (예: '11-28')
    """
    tomorrow = get_kst_now().date() + timedelta(days=1)
    return f"{tomorrow.month:02d}-{tomorrow.day:02d}"

def get_tomorrow_keywords():
    """
    해외분석 리스트에서 '내일 경기'만 필터링하기 위한 키워드 세트 생성.
    - '내일'
    - 11.28 / 11-28 / 11/28 같은 여러 날짜 포맷
    """
    tomorrow = get_kst_now().date() + timedelta(days=1)
    m = tomorrow.month
    d = tomorrow.day

    md_dot_1 = f"{m}.{d}"
    md_dot_2 = f"{m}.{d:02d}"
    md_dash_1 = f"{m}-{d}"
    md_dash_2 = f"{m}-{d:02d}"
    md_slash_1 = f"{m}/{d}"
    md_slash_2 = f"{m}/{d:02d}"

    return {
        "내일",
        md_dot_1, md_dot_2,
        md_dash_1, md_dash_2,
        md_slash_1, md_slash_2,
    }


def get_menu_caption() -> str:
    """메인 메뉴 설명 텍스트 (오늘/내일 날짜 자동 반영)"""
    today_str, tomorrow_str = get_date_labels()
    return (
        "📌 스포츠 정보&분석 공유방 메뉴 안내\n\n"
        "1️⃣ 실시간 무료 중계 - GOAT-TV 라이브 중계 바로가기\n"
        f"2️⃣ {today_str} 경기 분석픽 - 종목별로 {today_str} 경기 분석을 확인하세요\n"
        f"3️⃣ {tomorrow_str} 경기 분석픽 - 종목별로 {tomorrow_str} 경기 분석을 확인하세요\n"
        "4️⃣ 스포츠 뉴스 요약 - 주요 이슈 & 뉴스 요약 정리\n\n"
        "아래 버튼을 눌러 원하는 메뉴를 선택하세요 👇"
    )


# ───────────────── 분석/뉴스 데이터 (예시) ─────────────────

ANALYSIS_TODAY = {
    "축구": [],
    "농구": [],
    "야구": [],
    "배구": [],
}
ANALYSIS_TOMORROW = {
    "축구": [],
    "농구": [],
    "야구": [],
    "배구": [],
}

ANALYSIS_DATA_MAP = {
    "today": ANALYSIS_TODAY,
    "tomorrow": ANALYSIS_TOMORROW,
}

# ───────────────── 다음 스포츠 카테고리 ID 설정 ─────────────────
# DevTools > Network 에서 harmony contents.json 요청 확인 후
# defaultCategoryId3 의 value 를 환경변수에 세팅.
DAUM_CATEGORY_IDS = {
    # 해외축구
    "world_soccer": os.getenv("DAUM_CAT_WORLD_SOCCER", "100032"),

    # 국내축구 (K리그)
    "soccer_kleague": os.getenv("DAUM_CAT_SOCCER_KLEAGUE", "1027"),

    # 국내야구 (KBO)
    "baseball_kbo": os.getenv("DAUM_CAT_BASEBALL_KBO", "1028"),

    # 해외야구 (MLB)
    "baseball_world": os.getenv("DAUM_CAT_BASEBALL_WORLD", "1015"),

    # 농구
    "basketball": os.getenv("DAUM_CAT_BASKETBALL", "1029"),

    # 배구
    "volleyball": os.getenv("DAUM_CAT_VOLLEYBALL", "100033"),
}


# ───────────────── 구글 시트 연동 설정 ─────────────────

_gs_client = None  # gspread 클라이언트 캐시용


def get_gs_client():
    """환경변수에서 서비스계정 JSON 읽어서 gspread 클라이언트 생성"""
    global _gs_client
    if _gs_client is not None:
        return _gs_client

    key_raw = os.getenv("GOOGLE_SERVICE_KEY")
    if not key_raw:
        print("[GSHEET] GOOGLE_SERVICE_KEY 환경변수가 없습니다. 시트 연동 건너뜀.")
        return None

    try:
        key_data = json.loads(key_raw)
    except Exception as e:
        print(f"[GSHEET] GOOGLE_SERVICE_KEY JSON 파싱 오류: {e}")
        return None

    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
    _gs_client = gspread.authorize(creds)
    print("[GSHEET] gspread 인증 완료")
    return _gs_client


def summarize_text(text: str, max_len: int = 400) -> str:
    """
    (예전용) 아주 단순한 요약: 문장을 잘라서 앞에서부터 max_len까지 자르는 방식.
    """
    text = text.replace("\n", " ").strip()
    sentences = re.split(r'(?<=[\.!?다요])\s+', text)
    result = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if not result:
            candidate = s
        else:
            candidate = result + " " + s
        if len(candidate) > max_len:
            break
        result = candidate
    if not result:
        result = text[:max_len]
    return result


def clean_daum_body_text(text: str) -> str:
    """
    다음 뉴스 본문에서 번역/요약 UI, 언어 목록, 기자 크레딧/사진 설명 등
    불필요한 문장을 최대한 제거하고 기사 본문만 남긴다.
    """
    if not text:
        return ""

    # 1단계: 줄 단위로 나누고, 빈 줄 제거
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    blacklist = [
        "음성으로 듣기",
        "음성 재생",
        "음성재생 설정",
        "번역 설정",
        "번역 beta",
        "Translated by",
        "전체 맥락을 이해하기 위해서는 본문 보기를 권장합니다.",
        "요약문이므로 일부 내용이 생략될 수 있습니다.",
        "요약본이 자동요약 기사 제목과 주요 문장을 기반으로 자동요약한 결과입니다",
        "기사 제목과 주요 문장을 기반으로 자동요약한 결과입니다",
        # 언어 목록 키워드
        "한국어 - English",
        "한국어 - 영어",
        "English",
        "日本語",
        "简体中文",
        "Deutsch",
        "Русский",
        "Español",
        "العربية",
        "bahasa Indonesia",
        "ภาษาไทย",
        "Türkçe",
    ]

    clean_lines = []
    for l in lines:
        # 1) 공통 블랙리스트
        if any(b in l for b in blacklist):
            continue

        # 2) 사진/기사 크레딧 한 줄 통째로 날리기
        if re.match(r"^\[[^]]{2,60}\]\s*[^ ]{1,20}\s*(기자|통신원|특파원)?\s*$", l):
            continue

        clean_lines.append(l)

    text = " ".join(clean_lines)

    # 3단계: 본문 안에 끼어 있는 크레딧 패턴 제거
    text = re.sub(
        r"\[[^]]{2,60}(일보|뉴스|코리아|KOREA|포포투|베스트 일레븐)[^]]*?\]\s*[^ ]{1,20}\s*(기자|통신원|특파원)?",
        "",
        text,
    )
    text = re.sub(
        r"\[[^]]{2,60}\]\s*[^ ]{1,20}\s*(기자|통신원|특파원)",
        "",
        text,
    )

    # 4단계: "요약보기 자동요약" 꼬리 제거
    text = re.sub(r"요약보기\s*자동요약.*$", "", text)

    # 5단계: 공백 정리
    text = re.sub(r"\s{2,}", " ", text).strip()

    return text


def remove_title_prefix(title: str, body: str) -> str:
    """
    본문이 제목으로 시작하면 그 부분을 잘라낸다.
    (제목이 그대로 summary 에 반복되는 현상 완화용)
    """
    if not title or not body:
        return body

    t = title.strip().strip('\"“”')
    b = body.strip()

    candidates = [
        t,
        f'"{t}"',
        f"“{t}”",
    ]

    for cand in candidates:
        if b.startswith(cand):
            return b[len(cand):].lstrip(" -–:·,\"'")

    return b

def parse_maz_overseas_row(tr) -> dict | None:
    """
    mazgtv 해외분석 테이블의 <tr> 하나에서
    리그명 / 홈팀 / 원정팀 / 킥오프 시간 / 상세 링크를 추출한다.
    """
    tds = tr.find_all("td")
    if len(tds) < 3:
        return None

    # 홈팀
    home_parts = list(tds[0].stripped_strings)
    home_team = home_parts[0] if home_parts else ""

    # 가운데: [리그, VS, 날짜/시간] 구조라고 가정
    center_parts = list(tds[1].stripped_strings)
    league = center_parts[0] if center_parts else ""
    kickoff = center_parts[-1] if center_parts else ""

    # 원정팀
    away_parts = list(tds[2].stripped_strings)
    away_team = away_parts[0] if away_parts else ""

    # 상세 링크 (tr 안에 있는 첫 번째 <a href>)
    a = tr.select_one("a[href]") or tr.find("a", href=True)
    url = a["href"].strip() if a and a.get("href") else ""

    return {
        "league": league.strip(),
        "home": home_team.strip(),
        "away": away_team.strip(),
        "kickoff": kickoff.strip(),
        "url": url,
    }

def _load_analysis_sheet(sh, sheet_name: str) -> dict:
    """
    구글시트에서 한 탭(today / tomorrow)을 읽어서
    { sport: [ {id,title,summary}, ... ] } 구조로 변환
    """
    try:
        ws = sh.worksheet(sheet_name)
    except Exception as e:
        print(f"[GSHEET] 시트 '{sheet_name}' 열기 실패: {e}")
        return {}

    rows = ws.get_all_values()
    if not rows:
        return {}

    header = rows[0]
    idx_sport = 0
    idx_id = 1
    idx_title = 2
    idx_summary = 3

    def safe_index(name, default):
        try:
            return header.index(name)
        except ValueError:
            return default

    idx_sport = safe_index("sport", idx_sport)
    idx_id = safe_index("id", idx_id)
    idx_title = safe_index("title", idx_title)
    idx_summary = safe_index("summary", idx_summary)

    data: dict[str, list[dict]] = {}

    for row in rows[1:]:
        if len(row) <= idx_title:
            continue

        sport = (row[idx_sport] if len(row) > idx_sport else "").strip()
        if not sport:
            continue

        item_id = (row[idx_id] if len(row) > idx_id else "").strip()
        title = (row[idx_title] if len(row) > idx_title else "").strip()
        summary = (row[idx_summary] if len(row) > idx_summary else "").strip()

        if not title:
            continue

        if not item_id:
            cur_len = len(data.get(sport, []))
            item_id = f"{sport}_{cur_len + 1}"

        entry = {
            "id": item_id,
            "title": title,
            "summary": summary,
        }
        data.setdefault(sport, []).append(entry)

    return data


def reload_analysis_from_sheet():
    """
    구글시트에서 today / tomorrow 탭을 읽어서
    ANALYSIS_TODAY / ANALYSIS_TOMORROW / ANALYSIS_DATA_MAP 갱신
    """
    global ANALYSIS_TODAY, ANALYSIS_TOMORROW, ANALYSIS_DATA_MAP

    client = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not client or not spreadsheet_id:
        print("[GSHEET] 시트 클라이언트 또는 SPREADSHEET_ID 없음 → 기존 하드코딩 데이터 사용")
        return

    try:
        sh = client.open_by_key(spreadsheet_id)
    except Exception as e:
        print(f"[GSHEET] 스프레드시트 열기 실패: {e}")
        return

    sheet_today_name = os.getenv("SHEET_TODAY_NAME", "today")
    sheet_tomorrow_name = os.getenv("SHEET_TOMORROW_NAME", "tomorrow")

    print(f"[GSHEET] '{sheet_today_name}' / '{sheet_tomorrow_name}' 탭에서 분석 데이터 로딩 시도")

    try:
        today_data = _load_analysis_sheet(sh, sheet_today_name)
        tomorrow_data = _load_analysis_sheet(sh, sheet_tomorrow_name)
    except Exception as e:
        print(f"[GSHEET] 시트 데이터 로딩 중 오류: {e}")
        return

    ANALYSIS_TODAY = today_data
    ANALYSIS_TOMORROW = tomorrow_data

    ANALYSIS_DATA_MAP = {
        "today": ANALYSIS_TODAY,
        "tomorrow": ANALYSIS_TOMORROW,
    }

    print("[GSHEET] ANALYSIS_TODAY / ANALYSIS_TOMORROW 갱신 완료")

def append_analysis_rows(day_key: str, rows: list[list[str]]) -> bool:
    """
    분석 데이터를 today / tomorrow 탭에 추가하는 공용 함수.
    rows: [ [sport, "", title, summary], ... ]
    """
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        print("[GSHEET][ANALYSIS] 설정 없음 → 저장 불가")
        return False

    sheet_today_name = os.getenv("SHEET_TODAY_NAME", "today")
    sheet_tomorrow_name = os.getenv("SHEET_TOMORROW_NAME", "tomorrow")
    sheet_name = sheet_today_name if day_key == "today" else sheet_tomorrow_name

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name)
    except Exception as e:
        print(f"[GSHEET][ANALYSIS] 시트 '{sheet_name}' 열기 실패: {e}")
        return False

    try:
        ws.append_rows(rows, value_input_option="RAW")
        print(f"[GSHEET][ANALYSIS] {sheet_name} 에 {len(rows)}건 추가")
        return True
    except Exception as e:
        print(f"[GSHEET][ANALYSIS] append_rows 오류: {e}")
        return False

def _get_ws_by_name(sh, name: str):
    try:
        return sh.worksheet(name)
    except Exception:
        return None

def get_site_export_ws():
    """
    site_export 탭 워크시트 반환.
    없으면 생성 시도(권한/환경에 따라 실패 가능).
    """
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not (client_gs and spreadsheet_id):
        return None

    sheet_name = os.getenv("SHEET_SITE_EXPORT_NAME", "site_export")

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = _get_ws_by_name(sh, sheet_name)
        if ws:
            return ws

        # 없으면 생성 시도
        ws = sh.add_worksheet(title=sheet_name, rows=2000, cols=10)
        # 헤더 세팅
        ws.update("A1", [[
            "day", "sport", "src_id", "title", "body", "creatadAt"
        ]])
        return ws

    except Exception as e:
        print(f"[GSHEET][SITE_EXPORT] 워크시트 준비 실패: {e}")
        return None


def get_existing_site_src_ids(day_value: str | None = None) -> set[str]:
    """
    site_export 탭에서 이미 저장된 src_id 목록을 읽어 중복 저장 방지.
    day_value를 주면 해당 day만 필터링해서 읽는다.
    """
    ws = get_site_export_ws()
    if not ws:
        return set()

    try:
        values = ws.get_all_values()
        if not values or len(values) < 2:
            return set()

        header = values[0]
        idx_day = header.index("day") if "day" in header else 0
        idx_src = header.index("src_id") if "src_id" in header else 2

        out = set()
        for r in values[1:]:
            if len(r) <= idx_src:
                continue
            rid = (r[idx_src] or "").strip()
            if not rid:
                continue
            if day_value:
                dv = (r[idx_day] or "").strip() if len(r) > idx_day else ""
                if dv != day_value:
                    continue
            out.add(rid)

        return out

    except Exception as e:
        print(f"[GSHEET][SITE_EXPORT] existing src_id 로드 실패: {e}")
        return set()


def append_site_export_rows(rows: list[list[str]]) -> bool:
    """
    site_export 탭에 rows를 append한다.
    rows 포맷: [day, sport, src_id, title, body, creatadAt]
    """
    ws = get_site_export_ws()
    if not ws:
        print("[GSHEET][SITE_EXPORT] 워크시트 없음 → 저장 불가")
        return False

    try:
        ws.append_rows(rows, value_input_option="RAW")
        print(f"[GSHEET][SITE_EXPORT] {len(rows)}건 추가")
        return True
    except Exception as e:
        print(f"[GSHEET][SITE_EXPORT] append_rows 실패: {e}")
        return False

# ───────────────── site_export 저장 ─────────────────

SITE_EXPORT_SHEET_NAME = os.getenv("SHEET_SITE_EXPORT_NAME", "site_export")
SITE_EXPORT_HEADER = ["day", "sport", "src_id", "title", "body", "creatadAt"]  # 헤더 오타 포함 그대로

def _ensure_header(ws, header: list[str]) -> None:
    """시트가 비어있거나 헤더가 없으면 헤더를 1행에 깔아준다."""
    try:
        values = ws.get_all_values()
        if not values:
            ws.update("A1", [header])
            return
        first = values[0]
        if [c.strip() for c in first] != header:
            # 헤더가 다르면 강제로 교체하진 않고, 없는 경우만 깔기
            # (원하면 여기서 강제 교체로 바꿀 수 있음)
            pass
    except Exception as e:
        print(f"[GSHEET][SITE_EXPORT] 헤더 확인 실패: {e}")

def append_site_export_rows(rows: list[list[str]]) -> bool:
    """
    site_export 탭에 rows를 append.
    rows: [ [day, sport, src_id, title, body, createdAt], ... ]
    """
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not (client_gs and spreadsheet_id):
        print("[GSHEET][SITE_EXPORT] 설정 없음 → 저장 불가")
        return False

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(SITE_EXPORT_SHEET_NAME)
        _ensure_header(ws, SITE_EXPORT_HEADER)
    except Exception as e:
        print(f"[GSHEET][SITE_EXPORT] 시트 '{SITE_EXPORT_SHEET_NAME}' 열기 실패: {e}")
        return False

    try:
        ws.append_rows(rows, value_input_option="RAW")
        print(f"[GSHEET][SITE_EXPORT] {SITE_EXPORT_SHEET_NAME} 에 {len(rows)}건 추가")
        return True
    except Exception as e:
        print(f"[GSHEET][SITE_EXPORT] append_rows 오류: {e}")
        return False

def get_existing_site_src_ids(day_str: str) -> set[str]:
    """site_export 탭에서 day가 같은 행들의 src_id를 set으로 가져와 중복 저장 방지."""
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not (client_gs and spreadsheet_id):
        return set()

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(SITE_EXPORT_SHEET_NAME)
        values = ws.get_all_values()
        if not values or len(values) < 2:
            return set()

        header = values[0]
        idx_day = header.index("day") if "day" in header else 0
        idx_src = header.index("src_id") if "src_id" in header else 2

        out = set()
        for r in values[1:]:
            if len(r) <= max(idx_day, idx_src):
                continue
            if (r[idx_day] or "").strip() == day_str:
                sid = (r[idx_src] or "").strip()
                if sid:
                    out.add(sid)
        return out
    except Exception as e:
        print(f"[GSHEET][SITE_EXPORT] 기존 src_id 로딩 실패: {e}")
        return set()

def get_existing_analysis_ids(day_key: str) -> set[str]:
    """
    today / tomorrow 시트에서 이미 저장된 id 값들을 set으로 가져온다.
    (중복 크롤링 방지용)
    """
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        return set()

    sheet_today_name = os.getenv("SHEET_TODAY_NAME", "today")
    sheet_tomorrow_name = os.getenv("SHEET_TOMORROW_NAME", "tomorrow")
    sheet_name = sheet_today_name if day_key == "today" else sheet_tomorrow_name

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name)
    except Exception:
        return set()

    rows = ws.get_all_values()
    if not rows:
        return set()

    header = rows[0]

    def safe_index(name, default):
        try:
            return header.index(name)
        except ValueError:
            return default

    idx_sport = safe_index("sport", 0)
    idx_id = safe_index("id", 1)

    existing: set[str] = set()
    for row in rows[1:]:
        if len(row) <= idx_id:
            continue
        row_id = (row[idx_id] if len(row) > idx_id else "").strip()
        if row_id:
            existing.add(row_id)

    return existing

NEWS_DATA = {}


def _load_news_sheet(sh, sheet_name: str) -> dict:
    """
    구글시트에서 뉴스 탭을 읽어서
    {
        sport: [ {id,title,summary}, ... ]
    } 구조로 변환
    """
    try:
        ws = sh.worksheet(sheet_name)
    except Exception as e:
        print(f"[GSHEET] 뉴스 시트 '{sheet_name}' 열기 실패: {e}")
        return {}

    rows = ws.get_all_values()
    if not rows:
        return {}

    header = rows[0]

    idx_sport = 0
    idx_id = 1
    idx_title = 2
    idx_summary = 3

    def safe_index(name, default):
        try:
            return header.index(name)
        except ValueError:
            return default

    idx_sport = safe_index("sport", idx_sport)
    idx_id = safe_index("id", idx_id)
    idx_title = safe_index("title", idx_title)
    idx_summary = safe_index("summary", idx_summary)

    data: dict[str, list[dict]] = {}

    for row in rows[1:]:
        if len(row) <= idx_title:
            continue

        sport = (row[idx_sport] if len(row) > idx_sport else "").strip()
        if not sport:
            continue

        item_id = (row[idx_id] if len(row) > idx_id else "").strip()
        title = (row[idx_title] if len(row) > idx_title else "").strip()
        summary = (row[idx_summary] if len(row) > idx_summary else "").strip()

        if not title:
            continue

        if not item_id:
            cur_len = len(data.get(sport, []))
            item_id = f"{sport}_news_{cur_len + 1}"

        entry = {
            "id": item_id,
            "title": title,
            "summary": summary,
        }
        data.setdefault(sport, []).append(entry)

    return data


def reload_news_from_sheet():
    """구글시트에서 뉴스 탭을 읽어서 NEWS_DATA 갱신"""
    global NEWS_DATA
    client = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not client or not spreadsheet_id:
        print("[GSHEET] 뉴스용 SPREADSHEET 연동 실패 → 기존 하드코딩 NEWS_DATA 사용.")
        return

    try:
        sh = client.open_by_key(spreadsheet_id)
    except Exception as e:
        print(f"[GSHEET] 뉴스 스프레드시트 열기 실패: {e}")
        return

    sheet_news_name = os.getenv("SHEET_NEWS_NAME", "news")
    print(f"[GSHEET] '{sheet_news_name}' 탭에서 뉴스 데이터 로딩 시도")

    try:
        news_data = _load_news_sheet(sh, sheet_news_name)
    except Exception as e:
        print(f"[GSHEET] 뉴스 시트 데이터 로딩 중 오류: {e}")
        return

    NEWS_DATA = news_data
    print("[GSHEET] NEWS_DATA 갱신 완료")


# ───────────────── 키보드/메뉴 구성 ─────────────────

def build_reply_keyboard() -> ReplyKeyboardMarkup:
    """봇 1:1 테스트용 간단 하단 키보드"""
    menu = [
        ["메뉴 미리보기", "도움말"],
    ]
    return ReplyKeyboardMarkup(menu, resize_keyboard=True)


def build_main_inline_menu() -> InlineKeyboardMarkup:
    """
    메인 인라인 메뉴 (채널/미리보기 공통)
    채널에서는 이 버튼을 눌러 각자 봇 DM으로 이동하게 함.
    """
    today_str, tomorrow_str = get_date_labels()

    buttons = [
        [InlineKeyboardButton("실시간 무료 중계", url="https://goat-tv.com")],
        [
            InlineKeyboardButton(
                f"{today_str} 경기 분석픽",
                url=f"https://t.me/{BOT_USERNAME}?start=today",
            )
        ],
        [
            InlineKeyboardButton(
                f"{tomorrow_str} 경기 분석픽",
                url=f"https://t.me/{BOT_USERNAME}?start=tomorrow",
            )
        ],
        [
            InlineKeyboardButton(
                "스포츠 뉴스 요약",
                url=f"https://t.me/{BOT_USERNAME}?start=news",
            )
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_analysis_category_menu(key: str) -> InlineKeyboardMarkup:
    # key = "today" or "tomorrow"
    buttons = [
        [InlineKeyboardButton("⚽️축구⚽️", callback_data=f"analysis_cat:{key}:축구")],
        [InlineKeyboardButton("🏀농구🏀", callback_data=f"analysis_cat:{key}:농구")],
        [InlineKeyboardButton("⚾️야구⚾️", callback_data=f"analysis_cat:{key}:야구")],
        [InlineKeyboardButton("🏐배구🏐", callback_data=f"analysis_cat:{key}:배구")],
        [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)

def build_soccer_subcategory_menu(key: str) -> InlineKeyboardMarkup:
    """
    축구 선택 후 나오는 2단계 메뉴:
    해외축구 / K리그 / J리그
    key = "today" 또는 "tomorrow"
    """
    buttons = [
        [InlineKeyboardButton("해외축구", callback_data=f"soccer_cat:{key}:해외축구")],
        [InlineKeyboardButton("K리그", callback_data=f"soccer_cat:{key}:K리그")],
        [InlineKeyboardButton("J리그", callback_data=f"soccer_cat:{key}:J리그")],
        [InlineKeyboardButton("◀ 종목 선택으로", callback_data=f"analysis_root:{key}")],
        [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)

def build_basketball_subcategory_menu(key: str) -> InlineKeyboardMarkup:
    """
    농구 선택 후 나오는 2단계 메뉴:
    NBA / KBL
    key = "today" 또는 "tomorrow"
    """
    buttons = [
        [InlineKeyboardButton("NBA", callback_data=f"basket_cat:{key}:NBA")],
        [InlineKeyboardButton("KBL", callback_data=f"basket_cat:{key}:KBL")],
        [InlineKeyboardButton("◀ 종목 선택으로", callback_data=f"analysis_root:{key}")],
        [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)

def build_baseball_subcategory_menu(key: str) -> InlineKeyboardMarkup:
    """
    야구 선택 시 나오는 하위 카테고리 메뉴:
    - 해외야구
    - KBO
    - NPB
    """
    buttons = [
        [InlineKeyboardButton("⚾ 해외야구", callback_data=f"baseball_cat:{key}:해외야구")],
        [InlineKeyboardButton("⚾ KBO", callback_data=f"baseball_cat:{key}:KBO")],
        [InlineKeyboardButton("⚾ NPB", callback_data=f"baseball_cat:{key}:NPB")],
        [InlineKeyboardButton("◀ 종목 선택으로", callback_data=f"analysis_root:{key}")],
        [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)

def build_volleyball_subcategory_menu(key: str) -> InlineKeyboardMarkup:
    """
    배구 선택 시 나오는 하위 카테고리 메뉴
    (현재는 V리그만 있지만, 나중에 해외배구 등을 늘릴 수 있음)
    """
    buttons = [
        [InlineKeyboardButton("V리그", callback_data=f"volley_cat:{key}:V리그")],
        [InlineKeyboardButton("◀ 종목 선택으로", callback_data=f"analysis_root:{key}")],
        [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)


def build_analysis_match_menu(key: str, sport: str, page: int = 1) -> InlineKeyboardMarkup:
    """종목 선택 후 → 해당 종목 경기 리스트 메뉴 (10개씩 페이지 나누기)"""
    items = ANALYSIS_DATA_MAP.get(key, {}).get(sport, [])
    per_page = 10

    if page < 1:
        page = 1

    total = len(items)
    total_pages = max(1, math.ceil(total / per_page))

    if page > total_pages:
        page = total_pages

    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]

    buttons: list[list[InlineKeyboardButton]] = []

    # 현재 페이지의 경기들만 버튼으로
    for item in page_items:
        cb = f"match:{key}:{sport}:{item['id']}"
        buttons.append([InlineKeyboardButton(item["title"], callback_data=cb)])

    # 페이지 이동 버튼 (이전 / 현재페이지 / 다음)
    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []

        if page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    "◀ 이전",
                    callback_data=f"match_page:{key}:{sport}:{page-1}",
                )
            )

        nav_row.append(
            InlineKeyboardButton(
                f"{page}/{total_pages}",
                callback_data="noop",  # 눌러도 아무 동작 안 하는 용도
            )
        )

        if page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    "다음 ▶",
                    callback_data=f"match_page:{key}:{sport}:{page+1}",
                )
            )

        buttons.append(nav_row)

    # 공통 하단 버튼
    buttons.append(
        [InlineKeyboardButton("◀ 종목 선택으로", callback_data=f"analysis_root:{key}")]
    )
    buttons.append([InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")])

    return InlineKeyboardMarkup(buttons)

def build_news_category_menu() -> InlineKeyboardMarkup:
    """스포츠 뉴스 요약 → 종목 선택 메뉴"""
    buttons = [
        [InlineKeyboardButton("⚽️축구 뉴스⚽️", callback_data="news_cat:축구")],
        [InlineKeyboardButton("🏀농구 뉴스🏀", callback_data="news_cat:농구")],
        [InlineKeyboardButton("⚾️야구 뉴스⚾️", callback_data="news_cat:야구")],
        [InlineKeyboardButton("🏐배구 뉴스🏐", callback_data="news_cat:배구")],
        [InlineKeyboardButton("기타종목 뉴스", callback_data="news_cat:기타종")],
        [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)


def build_news_list_menu(sport: str) -> InlineKeyboardMarkup:
    """종목 선택 후 → 해당 종목 뉴스 제목 리스트 메뉴"""
    items = NEWS_DATA.get(sport, [])
    buttons = []
    for item in items:
        cb = f"news_item:{sport}:{item['id']}"
        buttons.append([InlineKeyboardButton(item["title"], callback_data=cb)])

    buttons.append([InlineKeyboardButton("◀ 종목 선택으로", callback_data="news_root")])
    buttons.append([InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


# ───────────────── 공통: 메인 메뉴 보내는 함수 ─────────────────

async def send_main_menu(chat_id: int | str, context: ContextTypes.DEFAULT_TYPE, preview: bool = False):
    """
    채널/DM 공통으로 '텍스트 + 메인 메뉴 버튼' 전송.
    """
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=get_menu_caption(),
        reply_markup=build_main_inline_menu(),
    )
    return msg


# ───────────────── 핸들러들 ─────────────────

# 1) /start – DM에서 채널과 동일한 레이아웃 or 바로 메뉴 진입
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    mode = args[0] if args else None

    today_str, tomorrow_str = get_date_labels()

    if mode == "today":
        await update.message.reply_text(
            f"{today_str} 경기 분석픽 메뉴입니다. 종목을 선택하세요 👇",
            reply_markup=build_analysis_category_menu("today"),
        )
        return

    if mode == "tomorrow":
        await update.message.reply_text(
            f"{tomorrow_str} 경기 분석픽 메뉴입니다. 종목을 선택하세요 👇",
            reply_markup=build_analysis_category_menu("tomorrow"),
        )
        return

    if mode == "news":
        await update.message.reply_text(
            "스포츠 뉴스 요약입니다. 종목을 선택하세요 👇",
            reply_markup=build_news_category_menu(),
        )
        return

    await update.message.reply_text(
        "스포츠봇입니다.\n"
        "아래에는 채널에 올라갈 메뉴와 동일한 레이아웃 미리보기를 보여줄게.\n"
        "실제 채널 배포는 /publish 명령으로 진행하면 돼.",
        reply_markup=build_reply_keyboard(),
    )

    await send_main_menu(chat_id, context, preview=True)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"당신의 텔레그램 ID: {uid}")


# 2) DM 텍스트 처리 – 간단 테스트용
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if "메뉴 미리보기" in text:
        await start(update, context)
    elif "도움말" in text:
        await update.message.reply_text(
            "/start : 메뉴 미리보기\n"
            "/publish : 채널에 메뉴 전송 + 상단 고정"
        )
    else:
        await update.message.reply_text("메뉴 미리보기는 /start 또는 '메뉴 미리보기' 버튼을 눌러주세요.")


# 3) /publish – 채널로 메뉴 보내고 상단 고정
async def publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    if not CHANNEL_ID:
        await update.message.reply_text("CHANNEL_ID가 비어 있습니다. Render 환경변수에 CHANNEL_ID를 설정하세요.")
        return

    try:
        await context.bot.unpin_all_chat_messages(CHANNEL_ID)
    except Exception:
        pass

    msg = await send_main_menu(CHANNEL_ID, context, preview=False)

    await context.bot.pin_chat_message(
        chat_id=CHANNEL_ID,
        message_id=msg.message_id,
        disable_notification=True,
    )

    await update.message.reply_text("채널에 메뉴를 올리고 상단에 고정했습니다 ✅")


# 5) /syncsheet – 구글시트에서 분석/뉴스 데이터 다시 로딩
async def syncsheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    try:
        reload_analysis_from_sheet()
        reload_news_from_sheet()
        await update.message.reply_text("구글시트에서 분석 데이터를 다시 불러왔습니다 ✅")
    except Exception as e:
        await update.message.reply_text(f"구글시트 로딩 중 오류가 발생했습니다: {e}")


# 🔹 /newsclean – news 시트 초기화 (헤더만 남기기)
async def newsclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        await update.message.reply_text(
            "구글시트 설정(GOOGLE_SERVICE_KEY 또는 SPREADSHEET_ID)이 없어 시트를 초기화할 수 없습니다."
        )
        return

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(os.getenv("SHEET_NEWS_NAME", "news"))
    except Exception as e:
        await update.message.reply_text(f"뉴스 시트를 열지 못했습니다: {e}")
        return

    try:
        rows = ws.get_all_values()
        if rows:
            header = rows[0]
        else:
            header = ["sport", "id", "title", "summary"]

        ws.clear()
        ws.update("A1", [header])

        await update.message.reply_text("뉴스 시트를 초기화했습니다. (헤더만 남겨둠) ✅")

    except Exception as e:
        await update.message.reply_text(f"시트 초기화 중 오류: {e}")
        return

# 🔹 /allclean – today / tomorrow / news 시트 전체 초기화
async def allclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        await update.message.reply_text(
            "구글시트 설정(GOOGLE_SERVICE_KEY 또는 SPREADSHEET_ID)이 없어 시트를 초기화할 수 없습니다."
        )
        return

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
    except Exception as e:
        await update.message.reply_text(f"스프레드시트를 열지 못했습니다: {e}")
        return

    sheet_today_name = os.getenv("SHEET_TODAY_NAME", "today")
    sheet_tomorrow_name = os.getenv("SHEET_TOMORROW_NAME", "tomorrow")
    sheet_news_name = os.getenv("SHEET_NEWS_NAME", "news")

    sheet_configs = [
        (sheet_today_name, "today 분석"),
        (sheet_tomorrow_name, "tomorrow 분석"),
        (sheet_news_name, "news 뉴스"),
    ]

    errors: list[str] = []

    for sheet_name, desc in sheet_configs:
        try:
            ws = sh.worksheet(sheet_name)
        except Exception as e:
            errors.append(f"{desc} 시트를 열지 못했습니다: {e}")
            continue

        try:
            rows = ws.get_all_values()
            if rows:
                header = rows[0]
            else:
                # today / tomorrow / news 모두 같은 형식 사용
                header = ["sport", "id", "title", "summary"]

            ws.clear()
            ws.update("A1", [header])
        except Exception as e:
            errors.append(f"{desc} 시트 초기화 중 오류: {e}")

    # 메모리 데이터도 함께 리셋
    reload_analysis_from_sheet()
    reload_news_from_sheet()

    if errors:
        msg = (
            "일부 시트를 초기화하지 못했습니다.\n\n"
            + "\n".join(errors)
        )
    else:
        msg = "today / tomorrow / news 시트를 모두 초기화했습니다. (헤더만 남겨둠) ✅"

    await update.message.reply_text(msg)

async def _analysis_clean_by_sports(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    sports_to_clear: set[str] | None,
    label: str,
):
    """
    tomorrow 시트에서 sport 컬럼 기준으로 특정 종목만 지우거나,
    sports_to_clear 가 None 이면 전체(헤더 제외) 삭제.
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        await update.message.reply_text(
            "구글시트 설정(GOOGLE_SERVICE_KEY 또는 SPREADSHEET_ID)이 없어 시트를 초기화할 수 없습니다."
        )
        return

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(os.getenv("SHEET_TOMORROW_NAME", "tomorrow"))
    except Exception as e:
        await update.message.reply_text(f"tomorrow 시트를 열지 못했습니다: {e}")
        return

    try:
        rows = ws.get_all_values()
    except Exception as e:
        await update.message.reply_text(f"시트 읽기 오류: {e}")
        return

    # 데이터가 아예 없으면 헤더만 복구
    if not rows:
        header = ["sport", "id", "title", "summary"]
        try:
            ws.clear()
            ws.update("A1", [header])
        except Exception as e:
            await update.message.reply_text(f"시트 초기화 중 오류: {e}")
            return
        reload_analysis_from_sheet()
        await update.message.reply_text(f"tomorrow 시트를 초기화했습니다. ({label})")
        return

    header = rows[0]
    data_rows = rows[1:]

    # sport 컬럼 인덱스 찾기 (기본은 0)
    try:
        idx_sport = header.index("sport")
    except ValueError:
        idx_sport = 0

    kept_rows = [header]
    deleted_count = 0

    if sports_to_clear is None:
        # 전체 삭제 (헤더만 남김)
        deleted_count = len(data_rows)
    else:
        # 해당 종목만 제외하고 유지
        for row in data_rows:
            sport_val = row[idx_sport] if len(row) > idx_sport else ""
            if sport_val in sports_to_clear:
                deleted_count += 1
                continue
            kept_rows.append(row)

    try:
        ws.clear()
        ws.update("A1", kept_rows)
    except Exception as e:
        await update.message.reply_text(f"시트 쓰기 오류: {e}")
        return

    reload_analysis_from_sheet()

    if sports_to_clear is None:
        await update.message.reply_text(
            f"tomorrow 시트의 분석 데이터를 전체 초기화했습니다. (삭제된 행: {deleted_count}개)"
        )
    else:
        await update.message.reply_text(
            f"tomorrow 시트에서 {label} 분석 데이터만 초기화했습니다. (삭제된 행: {deleted_count}개)"
        )

# ⚽ 축구 계열(해외축구 / K리그 / J리그)만 삭제
async def soccerclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sports = {"해외축구", "K리그", "J리그"}
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=sports,
        label="축구(해외축구/K리그/J리그)",
    )


# ⚾ 야구 계열(해외야구 / KBO / NPB)만 삭제
async def baseballclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sports = {"해외야구", "KBO", "NPB"}
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=sports,
        label="야구(해외야구/KBO/NPB)",
    )


# 🏀 농구만 삭제
async def basketclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 농구 전체: 예전 '농구' + 새 라벨 'NBA', 'KBL'
    sports = {"농구", "NBA", "KBL"}
    await _clean_tomorrow_sheet(
        update,
        context,
        sports_to_clear=sports,
        label="농구(NBA/KBL)",
    )


# 🏐 배구만 삭제
async def volleyclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sports = {"배구", "v리그"}
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=sports,
        label="배구/v리그",
    )


# 기타 종목만 삭제 (기타 / 기타종 / 기타종목)
async def etcclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sports = {"기타", "기타종", "기타종목"}
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=sports,
        label="기타 종목",
    )


# tomorrow 시트 전체 분석 데이터 삭제 (헤더만 남김)
async def analysisclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=None,
        label="전체 분석",
    )

# 🔹 4) /rollover – 내일 분석 → 오늘 분석으로 복사
async def rollover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    client = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if client and spreadsheet_id:
        try:
            sh = client.open_by_key(spreadsheet_id)

            sheet_today_name = os.getenv("SHEET_TODAY_NAME", "today")
            sheet_tomorrow_name = os.getenv("SHEET_TOMORROW_NAME", "tomorrow")

            ws_today = sh.worksheet(sheet_today_name)
            ws_tomorrow = sh.worksheet(sheet_tomorrow_name)

            rows = ws_tomorrow.get_all_values()

            if rows:
                ws_today.clear()
                ws_today.update("A1", rows)

                header = rows[0]
                ws_tomorrow.clear()
                ws_tomorrow.update("A1", [header])
            else:
                print("[GSHEET] tomorrow 탭에 데이터가 없어 시트 롤오버는 생략합니다.")

        except Exception as e:
            print(f"[GSHEET] 롤오버 중 시트 복사 실패: {e}")

    else:
        print("[GSHEET] 클라이언트 또는 SPREADSHEET_ID 없음 → 시트 롤오버는 건너뜀.")

    reload_analysis_from_sheet()

    await update.message.reply_text(
        "✅ 롤오버 완료!\n"
        "구글시트 'tomorrow' 탭 내용을 'today' 탭으로 복사했고,\n"
        "'tomorrow' 탭은 헤더만 남기고 초기화했어.\n\n"
        "이제 오늘 경기 분석은 'today' 탭에서, 내일 경기는 'tomorrow' 탭에서 작성하면 돼."
    )


def simple_summarize(text: str, max_chars: int = 400) -> str:
    """
    아주 단순 요약: 문장 사이 공백 정리 후,
    max_chars 안쪽에서 '다.' 기준으로 잘라서 반환.
    (Gemini 오류 시 fallback 용)
    """
    if not text:
        return ""

    text = re.sub(r"\s+", " ", text).strip()

    if len(text) <= max_chars:
        return text

    cut = text.rfind("다.", 0, max_chars)
    if cut != -1:
        return text[: cut + 2]

    return text[:max_chars] + "..."

# 🔹 OpenAI 클라이언트 (요약용)
_openai_client = None

def get_openai_client():
    """
    OPENAI_API_KEY 환경변수 기반으로 OpenAI 클라이언트를 초기화해서 돌려준다.
    키가 없으면 None을 리턴하고, 에러 시 simple_summarize 폴백을 사용한다.
    """
    global _openai_client
    if _openai_client is not None:
        return _openai_client

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("[OPENAI] OPENAI_API_KEY 미설정 → simple_summarize 폴백 사용")
        return None

    try:
        _openai_client = OpenAI(api_key=api_key)
        print("[OPENAI] OpenAI 클라이언트 초기화 완료")
    except Exception as e:
        print(f"[OPENAI] 클라이언트 초기화 실패: {e}")
        _openai_client = None
    return _openai_client

# 🔹 mazgtv 홍보 문구/해시태그 공통 제거용 패턴
MAZ_REMOVE_PATTERNS = [
    # 기본 홍보 문구
    r"실시간\s*스포츠중계",
    r"스포츠\s*중계",
    r"스포츠\s*분석",
    r"스포츠\s*정보",
    r"라이브\s*스포츠중계",
    r"실시간\s*무료\s*중계",
    r"무료\s*중계",
    r"무료\s*스포츠중계",

    # 사이트/브랜드명
    r"마징가티비",
    r"마징가\s*티비",
    r"마징가TV",
    r"마징가\s*TV",
    r"마징가\s*티브이",
    r"마징가\s*티비\s*바로가기",

    # 배너/유도 문구
    r"배너\s*문의",
    r"배너",
    r"링크\s*클릭",
    r"바로가기",
    r"스포츠중계\s*바로가기",

    # 해시태그
    r"#\S+",

    # 날짜/제목 라인 (예: 11월 28일 프리뷰, 11월 28일 경기 분석)
    r"11월\s*\d{1,2}\s*[^\n]{0,30}",
    r"\d{1,2}월\s*\d{1,2}일\s*[^\n]{0,30}",

    # 제목 패턴 (중계 / 분석)
    r"[가-힣A-Za-z0-9 ]+ 중계",
    r"[가-힣A-Za-z0-9 ]+ 분석",
    r"[가-힣A-Za-z0-9 ]+ 프리뷰",

    # 섹션 제목들
    r"프리뷰",
    r"핵심\s*포인트",
    r"핵심\s*포인트\s*정리",
    r"승부\s*예측",
    r"베팅\s*강도",
    r"마무리\s*코멘트",
    r"마무리\s*정리",

    # 사이트로 유도하는 꼬리 문구
    r"에서\s*확인하세요[^\n]*",

    # 픽 라인 (승/무/패, 핸디, 언더오버)
    r"\[승/무/패\][^\n]+",
    r"\[핸디\][^\n]+",
    r"\[언더오버\][^\n]+",
    r"승패\s*추천[^\n]*",
    r"추천\s*픽[^\n]*",

    # 이모지/아이콘류
    r"✅",
    r"⭕",
    r"⚠️",
    r"⭐+",
    r"🔥",
    r"👉",
]

def clean_maz_text(text: str) -> str:
    """
    mazgtv 원문/요약에서 홍보 문구, 해시태그 등을 제거하고
    공백을 정리해서 돌려준다.
    """
    if not text:
        return ""
    for pattern in MAZ_REMOVE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def extract_mmdd_from_kickoff(kickoff: str) -> tuple[int | None, int | None]:
    """
    '11-28 (금) 02:45' 같은 문자열에서 (month, day)를 뽑는다.
    다른 포맷(예: '11월 28일 02:45')도 대비해서 정규식 두 개를 시도.
    """
    if not kickoff:
        return (None, None)

    text = kickoff.strip()

    # 1) 11-28 (금) 02:45 형태
    m = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", text)
    if not m:
        # 2) 11월 28일 (금) 02:45 형태
        m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)

    if not m:
        return (None, None)

    try:
        month = int(m.group(1))
        day = int(m.group(2))
        return (month, day)
    except ValueError:
        return (None, None)

def ensure_team_line_breaks(body: str, home_team: str, away_team: str) -> str:
    """
    요약 본문에서 '홈팀: ... 원정팀:' 이 한 줄에 붙어 있을 때
    홈팀 블록 / 원정팀 블록 / 🎯 픽 사이에 빈 줄을 강제로 넣어 준다.
    """
    if not body:
        return body

    body = body.replace("\r\n", "\n")

    # 홈팀: ... 원정팀: 이 한 줄에 붙어 있으면 강제 분리
    if home_team and away_team:
        pattern = rf"({re.escape(home_team)}:[^\n]+)\s+({re.escape(away_team)}:)"
        body = re.sub(pattern, r"\1\n\n\2", body)

    # 원정팀: ... 🎯 픽 붙어 있으면 분리
    if away_team:
        pattern2 = rf"({re.escape(away_team)}:[^\n]+)\s+🎯\s*픽"
        body = re.sub(pattern2, r"\1\n\n🎯 픽", body)

    # 🎯 픽 라인을 항상 단독 줄로
    body = re.sub(r"\s*🎯\s*픽\s*", "\n\n🎯 픽\n", body)

    # 여러 공백 정리
    body = re.sub(r"[ \t]+", " ", body)
    return body.strip()


def _postprocess_analysis_body(body: str, home_label: str, away_label: str) -> str:
    """
    - 팀별 블록 사이 줄바꿈 강제
    - 🎯 픽 아래는 '➡' 로 시작하는 3줄만 남기기
    """
    body = ensure_team_line_breaks(body, home_label, away_label)

    if "🎯 픽" in body:
        head, tail = body.split("🎯 픽", 1)
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]

        # ➡ 로 시작하는 줄만 골라서 최대 3줄
        picks = [ln for ln in lines if ln.startswith("➡")]
        picks = picks[:3]

        if picks:
            tail_norm = "🎯 픽\n" + "\n".join(picks)
            body = head.rstrip() + "\n\n" + tail_norm
        else:
            # 픽이 이상하게 나오면 그냥 잘라버림
            body = head.rstrip()

    return body.strip()


def summarize_analysis_with_gemini(
    full_text: str,
    *,
    league: str = "해외축구",
    home_team: str = "",
    away_team: str = "",
    max_chars: int = 900,
) -> tuple[str, str]:
    """
    👉 이제는 OpenAI(gpt-4.1-mini)를 사용해서
       '제목 + 팀별 요약 + 🎯 픽' 형식으로 경기 분석을 생성한다.
    """
    client_oa = get_openai_client()

    # 기본 제목
    if home_team and away_team:
        base_title = f"[{league}] {home_team} vs {away_team} 경기 분석"
    else:
        base_title = f"[{league}] 해외축구 경기 분석"

    home_label = home_team or "홈팀"
    away_label = away_team or "원정팀"

    # 원문 정리
    full_text_clean = clean_maz_text(full_text or "").strip()
    if len(full_text_clean) > 7000:
        full_text_clean = full_text_clean[:7000]

    # OpenAI 키 없으면 간단 폴백
    if not client_oa:
        core = simple_summarize(full_text_clean, max_chars=max_chars)
        body = (
            f"{home_label}:\n{core}\n\n"
            "🎯 픽\n"
            "➡️ 경기 흐름 참고용 텍스트입니다.\n"
            "➡️ 실제 베팅 전 라인·부상 정보를 반드시 다시 확인해야 합니다.\n"
            "➡️ 세부 추천픽은 별도 분석이 필요합니다."
        )
        return (base_title or "[경기 분석]", body)

    # ── 프롬프트 ──
    prompt = f"""
다음은 해외축구 경기 분석 원문이다.
전체 내용을 이해한 뒤, 아래에 제시한 ‘엄격한 형식’ 그대로 작성하라.
원문 문장을 그대로 복사하지 말고 반드시 재작성하고, 형식에서 벗어나는 텍스트는 절대 출력하지 마라.

출력 형식은 아래를 정확히 지켜라:

제목: [리그] 홈팀 vs 원정팀 경기 분석
요약:
{home_label}:
- 문장1
- 문장2
(문장 수는 2~3개, 반드시 줄바꿈으로 구분)

{away_label}:
- 문장1
- 문장2
(문장 수는 2~3개)

🎯 픽
➡️ 홈팀/원정팀 승 관련 1줄
➡️ 핸디 관련 1줄
➡️ 오버/언더 관련 1줄

❗ 절대 금지:
- 픽 섹션에 설명문 추가 금지
- 픽을 3줄 초과하거나 3줄보다 적게 쓰는 것 금지
- {home_label}/{away_label} 블록 사이 줄바꿈 누락 금지
- 팀 이름 없이 분석 시작 금지
- 🎯 픽 위에 불필요한 텍스트 출력 금지
- 형식과 다른 여분 문장 출력 금지

아래는 리그/팀 정보다.
리그: {league}
홈팀: {home_label}
원정팀: {away_label}

===== 경기 분석 원문 =====
{full_text_clean}
""".strip()

    try:
        resp = client_oa.chat.completions.create(
            model=os.getenv("OPENAI_MODEL_ANALYSIS", "gpt-4.1-mini"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "너는 축구 경기 분석을 요약해서 정리하는 한국어 전문가다. "
                        "문장은 간결하고 직설적으로 쓰고, 형식을 반드시 지킨다."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_completion_tokens=700,
        )
        text_out = (resp.choices[0].message.content or "").strip()
        if not text_out:
            raise ValueError("empty response from OpenAI (analysis)")

        # 제목 / 요약 분리
        m_title = re.search(r"제목\s*[:：]\s*(.+)", text_out)
        m_body = re.search(r"요약\s*[:：]\s*(.+)", text_out, flags=re.S)

        new_title = (m_title.group(1).strip() if m_title else "").strip()
        body = (m_body.group(1).strip() if m_body else text_out).strip()

        if not new_title:
            new_title = base_title or "[경기 분석]"

        # 제목이 본문에 또 반복되면 잘라내기
        body = remove_title_prefix(new_title, body)

        # 형식 강제 후처리 (팀별 줄바꿈 + 픽 3줄)
        body = _postprocess_analysis_body(body, home_label, away_label)

        if len(body) > max_chars + 200:
            body = body[: max_chars + 200]

        return (new_title, body)

    except Exception as e:
        print(f"[OPENAI][ANALYSIS] 실패 → simple_summarize 폴백: {e}")
        core = simple_summarize(full_text_clean, max_chars=max_chars)
        body = (
            f"{home_label}:\n{core}\n\n"
            "🎯 픽\n"
            "➡️ 경기 흐름 참고용 텍스트입니다.\n"
            "➡️ 실제 베팅 전 라인·부상 정보를 반드시 다시 확인해야 합니다.\n"
            "➡️ 세부 추천픽은 별도 분석이 필요합니다."
        )
        return (base_title or "[경기 분석]", body)
        
def rewrite_for_site_openai(
    full_text: str,
    *,
    league: str,
    home_team: str,
    away_team: str,
    max_chars: int = 4500,
) -> tuple[str, str]:
    """
    사이트 게시용: 원문(full_text) 기반 서술형 재작성.
    - 허구/추측 금지
    - 원문과 어긋나는 정보 추가 금지
    - '스포츠분석', '고트티비' 키워드 자연스럽게 1~2회 삽입
    """
    text = (full_text or "").strip()
    if not text or len(text) < 200:
        raise ValueError("원문이 너무 짧음(사이트용 생성 스킵)")

    client_oa = get_openai_client()
    if not client_oa:
        raise ValueError("OPENAI_API_KEY 없음(사이트용 생성 스킵)")

    base_title = f"[{league}] {home_team} vs {away_team} 경기 분석".strip()

    # 너무 길면 컷
    if len(text) > 9000:
        text = text[:9000]

    prompt = f"""
다음은 스포츠 경기 분석 원문이다.
원문을 기반으로만 한국어로 자연스럽게 재작성하라.
절대로 원문에 없는 내용을 추가/추측/단정하지 마라.

요구사항:
- 제목 1개 + 본문(서술형)만 작성
- 본문은 6~14문단 내에서 자연스럽게 구성(줄바꿈 유지)
- 팀 전력/핵심 포인트/경기 흐름 전망 중심으로 정리
- '스포츠분석' 키워드를 본문에 1~2회 자연스럽게 포함
- '고트티비' 키워드를 본문에 1회 자연스럽게 포함
- 베팅 픽/배당/확률/승부 단정은 쓰지 말고, 가능성/흐름 중심으로만
- 원문 문장을 그대로 복사하지 말 것(재작성)

출력 형식(반드시):
제목: ...
본문:
... (여기부터 본문)

리그: {league}
홈팀: {home_team}
원정팀: {away_team}

===== 원문 =====
{text}
""".strip()

    resp = client_oa.chat.completions.create(
        model=os.getenv("OPENAI_MODEL_SITE", "gpt-4.1-mini"),
        messages=[
            {"role": "system", "content": "너는 스포츠 경기 분석 원문을 기반으로 재작성하는 한국어 에디터다. 허구를 절대 추가하지 않는다."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.35,
        max_completion_tokens=1200,
    )

    out = (resp.choices[0].message.content or "").strip()
    if not out:
        raise ValueError("site rewrite empty")

    # 파싱
    title = base_title
    body = out

    m1 = re.search(r"제목\s*[:：]\s*(.+)", out)
    m2 = re.search(r"본문\s*[:：]\s*(.+)", out, flags=re.S)
    if m1:
        title = m1.group(1).strip()
    if m2:
        body = m2.group(1).strip()

    # 길이 제한
    if len(body) > max_chars:
        body = body[:max_chars].rstrip()

    return title, body

    except Exception as e:
        print(f"[OPENAI][SITE] 실패 → simple_summarize 폴백: {e}")
        body = simple_summarize(text, max_chars=min(max_chars, 1200))
        return (base_title, body)

# ───────────────── 뉴스용 Gemini 요약 함수 ─────────────────

def summarize_with_gemini(full_text: str, orig_title: str = "", max_chars: int = 400) -> tuple[str, str]:
    """
    뉴스 기사용 요약 함수.
    이제 OpenAI(gpt-4.1-mini)를 사용해서
    '제목: ... / 요약: ...' 형식으로 리라이팅한다.
    """
    client_oa = get_openai_client()
    trimmed = (full_text or "").strip()
    if len(trimmed) > 6000:
        trimmed = trimmed[:6000]

    # 키 없으면 폴백
    if not client_oa:
        print("[OPENAI][NEWS] 클라이언트 없음 → simple_summarize 사용")
        fb_summary = simple_summarize(trimmed, max_chars=max_chars)
        fb_summary = clean_maz_text(fb_summary)
        return (orig_title or "[제목 없음]", fb_summary)

    prompt = (
        "다음은 스포츠 뉴스 기사 원문과 기존 제목이다.\n"
        "전체 내용을 이해한 뒤, 새로운 한국어 뉴스 헤드라인 1개와 2~3문장짜리 요약을 작성해줘.\n"
        "기사 앞부분을 그대로 복사하지 말 것.\n"
        f"요약 길이는 공백 포함 {max_chars}자 내외.\n"
        "\n"
        "반드시 아래 형식으로만 출력해:\n"
        "제목: (여기에 새 제목)\n"
        "요약: (여기에 요약문)\n"
        "그 외의 문장은 출력하지 마.\n"
        "\n"
        "===== 기존 제목 =====\n"
        f"{orig_title}\n"
        "\n"
        "===== 기사 원문 =====\n"
        f"{trimmed}\n"
    )

    try:
        resp = client_oa.chat.completions.create(
            model=os.getenv("OPENAI_MODEL_NEWS", "gpt-4.1-mini"),
            messages=[
                {
                    "role": "system",
                    "content": "너는 스포츠 뉴스를 간결하게 요약하는 한국어 기자다. "
                               "형식을 정확히 지키고, 중복 표현은 줄인다.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_completion_tokens=450,
        )
        text_out = (resp.choices[0].message.content or "").strip()
        if not text_out:
            raise ValueError("empty response from OpenAI (news)")

        new_title = ""
        summary = ""
        for line in text_out.splitlines():
            line = line.strip()
            if line.startswith("제목:"):
                new_title = line[len("제목:"):].strip(" ：:")
            elif line.startswith("요약:"):
                summary = line[len("요약:"):].strip(" ：:")

        if not summary:
            summary = text_out

        if len(summary) > max_chars + 100:
            summary = summary[: max_chars + 100]

        if not new_title:
            new_title = orig_title or "[제목 없음]"

        summary = clean_maz_text(summary)
        return (new_title, summary)

    except Exception as e:
        print(f"[OPENAI][NEWS] 요약 실패 → simple_summarize로 폴백: {e}")
        fb_summary = simple_summarize(trimmed, max_chars=max_chars)
        fb_summary = clean_maz_text(fb_summary)
        return (orig_title or "[제목 없음]", fb_summary)

def extract_main_text_from_html(soup: BeautifulSoup) -> str:
    """
    mazgtv 분석 상세 페이지에서 본문 텍스트를 최대한 잘 뽑아서 리턴.
    HTML 구조를 정확히 모를 때를 대비해서 여러 후보 셀렉터를 시도하고,
    그래도 없으면 body 전체 텍스트를 사용.
    """
    # 광고/스크립트 제거
    for bad in soup.select("script, style, noscript"):
        try:
            bad.decompose()
        except Exception:
            pass

    candidates = [
        "div.ql-editor",      # 에디터 본문일 때 자주 쓰는 클래스
        "div.v-card__text",   # vuetify 카드 본문
        "div.article-body",
        "div.view-cont",
        "div#content",
        "article",
        "main",
    ]

    for sel in candidates:
        el = soup.select_one(sel)
        if not el:
            continue
        text = el.get_text("\n", strip=True)
        if len(text) >= 200:   # 너무 짧으면 본문이 아닐 가능성
            return re.sub(r"\s+", " ", text).strip()

    # 후보들에서 못 찾으면 bodyFallback
    body = soup.body or soup
    text = body.get_text("\n", strip=True)
    return re.sub(r"\s+", " ", text).strip()

# ───────────────── Daum harmony API 공통 함수 ─────────────────

async def fetch_daum_news_json(client: httpx.AsyncClient, category_id: str, size: int = 20) -> list[dict]:
    """
    다음 스포츠 harmony API에서 특정 카테고리 ID의 뉴스 JSON 리스트를 가져온다.
    (해외축구, KBO, 해외야구, 농구, 배구 공통)
    """
    if not category_id:
        return []

    base_url = "https://sports.daum.net/media-api/harmony/contents.json"

    today_kst = get_kst_now().date()
    ymd = today_kst.strftime("%Y%m%d")
    create_dt = f"{ymd}000000~{ymd}235959"

    discovery_tag_value = json.dumps({
        "group": "media",
        "key": "defaultCategoryId3",
        "value": str(category_id),
    }, ensure_ascii=False)

    params = {
        "page": 0,
        "consumerType": "HARMONY",
        "status": "SERVICE",
        "createDt": create_dt,
        "size": size,
        "discoveryTag[0]": discovery_tag_value,
    }

    r = await client.get(base_url, params=params, timeout=10.0)
    r.raise_for_status()
    data = r.json()

    contents = None
    if isinstance(data, dict):
        contents = data.get("contents")
        if contents is None:
            inner = data.get("data") or data.get("result") or data.get("body")
            if isinstance(inner, dict):
                contents = inner.get("contents") or inner.get("list") or inner.get("items")
    elif isinstance(data, list):
        contents = data

    if not contents:
        print("[CRAWL][DAUM] JSON 구조를 파악하지 못했습니다. 최상위 키:",
              list(data.keys()) if isinstance(data, dict) else type(data))
        return []

    return contents


async def fetch_article_body(client: httpx.AsyncClient, url: str) -> str:
    """
    (예전 네이버용) 뉴스 상세 페이지에서 본문 텍스트만 추출.
    현재는 사용하지 않지만 남겨둠.
    """
    try:
        r = await client.get(url, timeout=10.0, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        print(f"[CRAWL][ARTICLE] 요청 실패: {url} / {e}")
        return ""

    soup = BeautifulSoup(r.text, "html.parser")

    body = soup.select_one("#newsEndContents")
    if body:
        return body.get_text("\n", strip=True)

    body = soup.select_one("#newsEndBody")
    if body:
        return body.get_text("\n", strip=True)

    body = soup.select_one("#dic_area")
    if body:
        return body.get_text("\n", strip=True)

    print(f"[CRAWL][ARTICLE] 본문 셀렉터 매치 실패: {url}")
    return ""


async def crawl_daum_news_common(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    category_id: str,
    sport_label: str,
    max_articles: int = 10,
):
    """
    Daum harmony API + HTML 본문을 이용해 뉴스 크롤링 후
    구글시트 news 탭에 저장하는 공통 함수.
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    if not category_id:
        await update.message.reply_text(
            f"{sport_label} 카테고리 ID가 설정되어 있지 않습니다.\n"
            "코드 상단 DAUM_CATEGORY_IDS 또는 환경변수를 확인해 주세요."
        )
        return

    await update.message.reply_text(
        f"다음스포츠 {sport_label} 뉴스를 크롤링합니다. 잠시만 기다려 주세요..."
    )

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        ) as client:
            contents = await fetch_daum_news_json(client, category_id, size=max_articles)

            if not contents:
                await update.message.reply_text(f"{sport_label} JSON 데이터에서 기사를 찾지 못했습니다.")
                return

            articles: list[dict] = []

            # 1) JSON에서 제목 + 기사 URL 추출
            for item in contents:
                if not isinstance(item, dict):
                    continue

                title = (
                    item.get("title")
                    or item.get("contentTitle")
                    or item.get("headline")
                    or item.get("name")
                )

                url = (
                    item.get("contentUrl")
                    or item.get("permalink")
                    or item.get("url")
                    or item.get("link")
                )

                if not title or not url:
                    continue

                title = str(title).strip()
                url = str(url).strip()

                if url.startswith("/"):

                    url = urljoin("https://sports.daum.net", url)

                articles.append({"title": title, "link": url})

                if len(articles) >= max_articles:
                    break

            if not articles:
                await update.message.reply_text(
                    f"JSON은 받았지만, {sport_label} 제목/URL 정보를 찾지 못했습니다."
                )
                return

            # 2) 각 기사 페이지 들어가서 본문 크롤링 + 요약
            for art in articles:
                try:
                    r2 = await client.get(art["link"], timeout=10.0)
                    r2.raise_for_status()
                    s2 = BeautifulSoup(r2.text, "html.parser")

                    body_el = (
                        s2.select_one("div#harmonyContainer")
                        or s2.select_one("section#article-view-content-div")
                        or s2.select_one("div.article_view")
                        or s2.select_one("div#mArticle")
                        or s2.find("article")
                        or s2.body
                    )

                    raw_body = ""
                    if body_el:
                        # 이미지 설명 캡션 제거
                        try:
                            for cap in body_el.select(
                                "figcaption, .txt_caption, .photo_desc, .caption, "
                                "em.photo_desc, span.caption, p.caption"
                            ):
                                try:
                                    cap.extract()
                                except Exception:
                                    pass
                        except Exception:
                            # select가 안 되는 경우는 그냥 무시
                            pass

                        raw_body = body_el.get_text("\n", strip=True)
                    
                    clean_text = clean_daum_body_text(raw_body)
                    clean_text = remove_title_prefix(art["title"], clean_text)
                    
                    # ✅ Gemini로 "새 제목 + 요약" 생성 (400자 내외)
                    new_title, new_summary = summarize_with_gemini(
                        clean_text,
                        orig_title=art["title"],
                        max_chars=400,
                    )

                    art["title"] = new_title
                    art["summary"] = new_summary

                except Exception as e:
                    print(f"[CRAWL][DAUM] 기사 파싱 실패 ({art['link']}): {e}")
                    # 크롤링 실패 시에도 최소한 뭔가 넣어두기
                    art["summary"] = "(본문 크롤링 실패)"

    except Exception as e:
        await update.message.reply_text(f"요청 오류가 발생했습니다: {e}")
        return

    # 3) 구글 시트 저장
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        await update.message.reply_text(
            "구글시트 설정(GOOGLE_SERVICE_KEY 또는 SPREADSHEET_ID)이 없어 시트에 저장하지 못했습니다."
        )
        return

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(os.getenv("SHEET_NEWS_NAME", "news"))
    except Exception as e:
        await update.message.reply_text(f"뉴스 시트를 열지 못했습니다: {e}")
        return

    rows_to_append = []
    for art in articles:
        rows_to_append.append([
            sport_label,      # sport
            "",               # id
            art["title"],     # title
            art["summary"],   # summary
        ])

    try:
        ws.append_rows(rows_to_append, value_input_option="RAW")
    except Exception as e:
        await update.message.reply_text(f"시트 쓰기 오류: {e}")
        return

    await update.message.reply_text(
        f"다음스포츠 {sport_label} 뉴스 {len(rows_to_append)}건을 저장했습니다.\n"
        "/syncsheet 로 텔레그램 메뉴를 갱신할 수 있습니다."
    )

# ───────────────── mazgtv 분석 공통 (내일 경기 → today/tomorrow 시트, JSON/API 버전) ─────────────────

MAZ_LIST_API = "https://mazgtv1.com/api/board/list"
# 상세 API 실제 경로에 맞게 여기만 수정하면 됨
MAZ_DETAIL_API_TEMPLATE = "https://mazgtv1.com/api/board/{board_id}"


def _parse_game_start_date(game_start_at: str) -> date | None:
    """
    '2025-11-28T05:00:00' 같은 문자열에서 날짜(date)만 뽑는다.
    """
    if not game_start_at:
        return None
    try:
        # 뒤에 타임존이 붙어 있어도 앞 19자리까지만 잘라서 파싱
        s = game_start_at[:19]
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return dt.date()
    except Exception:
        return None

from datetime import date  # 파일 위쪽에 이미 있을 수도 있음

def detect_game_date_from_item(item: dict, target_date: date) -> date | None:
    """
    mazgtv 리스트 JSON 한 건(item) 전체를 훑으면서
    target_date 와 '같은 날짜'가 들어있는지 찾는다.

    아래 패턴들 중 하나라도 target_date 와 같으면 target_date 를 리턴, 
    하나도 없으면 None:
    - YYYY-MM-DD
    - MM-DD
    - M월 D일 / MM월 DD일
    """

    def _iter_values(x):
        if isinstance(x, dict):
            for v in x.values():
                yield from _iter_values(v)
        elif isinstance(x, list):
            for v in x:
                yield from _iter_values(v)
        else:
            yield x

    texts = [v for v in _iter_values(item) if isinstance(v, str)]

    ty = target_date.year

    # 1) YYYY-MM-DD 패턴들 중에서 target_date 와 같은 날짜가 있는지
    for text in texts:
        for yy, mm, dd in re.findall(r"(\d{4})-(\d{2})-(\d{2})", text):
            try:
                dt = date(int(yy), int(mm), int(dd))
            except ValueError:
                continue
            if dt == target_date:
                return dt

    # 2) MM-DD (예: 12-03)
    for text in texts:
        for mm, dd in re.findall(r"(\d{1,2})-(\d{1,2})", text):
            try:
                dt = date(ty, int(mm), int(dd))
            except ValueError:
                continue
            if dt == target_date:
                return dt

    # 3) '12월 3일' / '12 월 03 일' 패턴
    for text in texts:
        for mm, dd in re.findall(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text):
            try:
                dt = date(ty, int(mm), int(dd))
            except ValueError:
                continue
            if dt == target_date:
                return dt

    return None

def classify_basketball_volleyball_sport(league: str) -> str:
    """
    mazgtv leagueName 기준으로 ANALYSIS 시트 sport 값을 결정한다.
    - NBA      → "NBA"
    - KBL      → "KBL"
    - WKBL     → "WKBL"
    - V-리그   → "V리그"
    - 그 외 배구 관련 → "배구"
    - 그 외 농구 관련 → "농구"
    """
    if not league:
        return "농구"

    upper = league.upper()

    # NBA
    if "NBA" in upper:
        return "NBA"

    # 국내 농구
    if "KBL" in upper:
        return "KBL"
    if "WKBL" in upper:
        return "WKBL"

    # 배구 (V리그/해외배구 포함)
    if any(x in upper for x in ["V-리그", "V리그", "V-LEAGUE", "VOLLEY", "배구"]):
        # 국내 V리그 표시를 조금 더 명확히 하고 싶으면 여기 분리
        if "V" in upper or "V-LEAGUE" in upper:
            return "V리그"
        return "배구"

    # 나머지는 대충 농구로 묶기
    if any(x in upper for x in ["BASKET", "농구"]):
        return "농구"

    # 정말 정보가 없으면 농구로
    return "농구"

async def crawl_maz_analysis_common(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    base_url: str,
    sport_label: str,
    league_default: str,
    day_key: str = "tomorrow",
    max_pages: int = 5,
    board_type: int = 2,
    category: int = 1,
    target_ymd: str | None = None,
    export_site: bool = False,
):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    # ✅ 날짜 기준 설정 (today/tomorrow)
    if target_ymd is None:
        base_date = get_kst_now().date()
        if day_key == "tomorrow":
            base_date += timedelta(days=1)
        target_ymd = base_date.strftime("%Y-%m-%d")

    target_date = datetime.strptime(target_ymd, "%Y-%m-%d").date()

    await update.message.reply_text(
        f"mazgtv {sport_label} 분석 페이지에서 {target_ymd} 경기 분석글을 가져옵니다. 잠시만 기다려 주세요..."
    )

    rows_to_append: list[list[str]] = []

    # ✅ 중복 방지: 이미 today/tomorrow 시트에 있는 src_id 모으기
    existing_ids = get_existing_analysis_ids(day_key)

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        ) as client:

            for page in range(1, max_pages + 1):
                list_url = (
                    f"{MAZ_LIST_API}"
                    f"?page={page}&perpage=20"
                    f"&boardType={board_type}&category={category}"
                    f"&sort=b.game_start_at+DESC,+b.created_at+DESC"
                )

                r = await client.get(list_url, timeout=10.0)
                r.raise_for_status()

                try:
                    data = r.json()
                except Exception as e:
                    print(f"[MAZ][LIST] JSON 파싱 실패(page={page}): {e}")
                    print("  응답 일부:", r.text[:200])
                    continue

                if isinstance(data, dict):
                    items = (
                        data.get("rows")
                        or (data.get("data") or {}).get("rows")
                        or data.get("list")
                        or data.get("items")
                    )
                else:
                    items = data

                if not isinstance(items, list) or not items:
                    print(f"[MAZ][LIST] page={page} 항목 없음 → 반복 종료")
                    break

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    board_id = item.get("id")
                    if not board_id:
                        continue

                    row_id = f"maz_{board_id}"

                    # ✅ 중복 스킵
                    if row_id in existing_ids:
                        print(f"[MAZ][SKIP_DUP] already exists in sheet: {row_id}")
                        continue

                    game_start_at = (
                        item.get("gameStartAt")
                        or item.get("game_start_at")
                        or ""
                    )
                    game_start_at = str(game_start_at).strip()

                    game_start_at_text = str(item.get("gameStartAtText") or "").strip()
                    print(
                        f"[MAZ][DEBUG] page={page} id={board_id} "
                        f"gameStartAt='{game_start_at}' gameStartAtText='{game_start_at_text}'"
                    )

                    # 1) gameStartAt로 날짜 파싱
                    item_date = _parse_game_start_date(game_start_at)

                    # 2) 실패하면 item 전체에서 날짜 패턴 탐색 (연도 보정용)
                    if not item_date:
                        item_date = detect_game_date_from_item(item, target_year=target_date.year)

                    print(f"[MAZ][DEBUG_DATE] page={page} id={board_id} item_date={item_date}")

                    if not item_date:
                        continue

                    # ✅ 날짜 필터링
                    # - 축구/농구/배구: target_date와 정확히 일치만
                    # - 야구: (혹시 주간 카드로 들어오는 경우) 일치가 아니면 같은 주(0~6일)까지 허용
                    if sport_label == "야구":
                        if item_date != target_date:
                            delta_days = (target_date - item_date).days
                            if delta_days < 0 or delta_days >= 7:
                                continue
                    else:
                        if item_date != target_date:
                            continue

                    league = item.get("leagueName") or league_default
                    home = item.get("homeTeamName") or ""
                    away = item.get("awayTeamName") or ""

                    detail_url = MAZ_DETAIL_API_TEMPLATE.format(board_id=board_id)
                    try:
                        r2 = await client.get(detail_url, timeout=10.0)
                        r2.raise_for_status()
                        detail = r2.json()
                    except Exception as e:
                        print(f"[MAZ][DETAIL] id={board_id} 요청 실패: {e}")
                        continue

                    content_html = detail.get("content") or ""
                    if not str(content_html).strip():
                        print(f"[MAZ][DETAIL] id={board_id} content 없음")
                        continue

                    soup = BeautifulSoup(content_html, "html.parser")
                    try:
                        for bad in soup.select("script, style, .ad, .banner"):
                            bad.decompose()
                    except Exception:
                        pass

                    full_text = soup.get_text("\n", strip=True)
                    full_text = clean_maz_text(full_text)
                    if not full_text:
                        print(f"[MAZ][DETAIL] id={board_id} 본문 텍스트 없음")
                        continue

                    new_title, new_body = summarize_analysis_with_gemini(
                        full_text,
                        league=league,
                        home_team=home,
                        away_team=away,
                        max_chars=900,
                    )

                    # ✅ sport 세부 분류
                    row_sport = sport_label

                    if sport_label == "축구":
                        if "K리그" in league:
                            row_sport = "K리그"
                        elif "J리그" in league:
                            row_sport = "J리그"
                        else:
                            row_sport = "해외축구"

                    elif sport_label == "야구":
                        upper_league = (league or "").upper()
                        if "KBO" in upper_league:
                            row_sport = "KBO"
                        elif "NPB" in upper_league:
                            row_sport = "NPB"
                        elif "MLB" in upper_league:
                            row_sport = "해외야구"
                        else:
                            row_sport = "해외야구"

                    elif sport_label in ("농구", "농구/배구"):
                        row_sport = classify_basketball_volleyball_sport(league or "")

                    rows_to_append.append([row_sport, row_id, new_title, new_body])

    except Exception as e:
        # ✅ 여기 except는 try와 같은 들여쓰기 레벨이어야 함
        await update.message.reply_text(f"요청 오류가 발생했습니다: {e}")
        return

    if not rows_to_append:
        await update.message.reply_text(
            f"mazgtv {sport_label} 분석에서 {target_ymd} 경기 분석글을 찾지 못했습니다."
        )
        return

    ok = append_analysis_rows(day_key, rows_to_append)
    if not ok:
        await update.message.reply_text("구글시트에 분석 데이터를 저장하지 못했습니다.")
        return

    reload_analysis_from_sheet()

    await update.message.reply_text(
        f"mazgtv {sport_label} 분석에서 {target_ymd} 경기 분석 {len(rows_to_append)}건을 "
        f"'{day_key}' 시트에 저장했습니다.\n"
        "텔레그램에서 경기 분석픽 메뉴를 열어 확인해보세요."
    )

# ───────────────── 종목별 (Daum 뉴스) 크롤링 명령어 ─────────────────

# 해외축구
async def crawlsoccer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("world_soccer")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="축구",
        max_articles=5,
    )


# 국내축구 (K리그 등, 5개)
async def crawlsoccerkr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("soccer_kleague")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="축구",   # 해외/국내를 한 카테고리에 묶어서 보여주기
        max_articles=5,
    )


# KBO 야구
async def crawlbaseball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("baseball_kbo")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="야구",
        max_articles=5,
    )


# 해외야구 (MLB 등)
async def crawloverbaseball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("baseball_world")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="야구",  # 필요하면 '해외야구'로 분리해서도 가능
        max_articles=5,
    )


# 농구
async def crawlbasketball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("basketball")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="농구",
        max_articles=10,
    )


# 배구
async def crawlvolleyball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("volleyball")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="배구",
        max_articles=10,
    )

# 4) 인라인 버튼 콜백 처리 (분석/뉴스 팝업)
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    # 아무 동작 안 하는 더미
    if data == "noop":
        return

    # 메인 메뉴로
    if data == "back_main":
        await q.edit_message_reply_markup(reply_markup=build_main_inline_menu())
        return

    # 축구 하위 카테고리 (해외축구 / K리그 / J리그)
    if data.startswith("soccer_cat:"):
        _, key, subsport = data.split(":", 2)
        # subsport: "해외축구", "K리그", "J리그"
        await q.edit_message_reply_markup(
            reply_markup=build_analysis_match_menu(key, subsport, page=1)
        )
        return

    # 야구 하위 카테고리 (해외야구 / KBO / NPB)
    if data.startswith("baseball_cat:"):
        _, key, subsport = data.split(":", 2)
        # subsport: "해외야구", "KBO", "NPB"
        await q.edit_message_reply_markup(
            reply_markup=build_analysis_match_menu(key, subsport, page=1)
        )
        return

        # 농구 하위 카테고리 (NBA / KBL)
    if data.startswith("basket_cat:"):
        _, key, subsport = data.split(":", 2)
        # subsport: "NBA", "KBL"
        await q.edit_message_reply_markup(
            reply_markup=build_analysis_match_menu(key, subsport, page=1)
        )
        return

    # 배구 하위 카테고리 (V리그)
    if data.startswith("volley_cat:"):
        _, key, subsport = data.split(":", 2)  # subsport == "V리그"
        await q.edit_message_reply_markup(
            reply_markup=build_analysis_match_menu(key, subsport, page=1)
        )
        return
  
    # 종목 선택으로 돌아가기
    if data.startswith("analysis_root:"):
        _, key = data.split(":", 1)
        await q.edit_message_reply_markup(reply_markup=build_analysis_category_menu(key))
        return

    # 종목 선택 (축구/농구/야구/배구)
    if data.startswith("analysis_cat:"):
        _, key, sport = data.split(":", 2)

        # ⚽ 축구 → 해외축구 / K리그 / J리그 하위 메뉴
        if sport == "축구":
            await q.edit_message_reply_markup(
                reply_markup=build_soccer_subcategory_menu(key)
            )
            return

        # ⚾ 야구 → 해외야구 / KBO / NPB 하위 메뉴
        if sport == "야구":
            await q.edit_message_reply_markup(
                reply_markup=build_baseball_subcategory_menu(key)
            )
            return

        # 🏀 농구 → NBA / KBL 하위 메뉴
        if sport == "농구":
            await q.edit_message_reply_markup(
                reply_markup=build_basketball_subcategory_menu(key)
            )
            return

        # 🏐 배구 → V리그 하위 메뉴
        if sport == "배구":
            await q.edit_message_reply_markup(
                reply_markup=build_volleyball_subcategory_menu(key)
            )
            return        

        # 그 외 종목(배구 등)은 바로 경기 리스트 1페이지
        await q.edit_message_reply_markup(
            reply_markup=build_analysis_match_menu(key, sport, page=1)
        )
        return
        
    # 경기 리스트 페이지 이동 (이전/다음)
    if data.startswith("match_page:"):
        _, key, sport, page_str = data.split(":", 3)
        try:
            page = int(page_str)
        except ValueError:
            page = 1

        await q.edit_message_reply_markup(
            reply_markup=build_analysis_match_menu(key, sport, page=page)
        )
        return

    # 개별 경기 선택
    if data.startswith("match:"):
        _, key, sport, match_id = data.split(":", 3)
        items = ANALYSIS_DATA_MAP.get(key, {}).get(sport, [])

        title = "선택한 경기"
        summary = "해당 경기 분석을 찾을 수 없습니다."

        for item in items:
            if item["id"] == match_id:
                title = item["title"]
                summary = item["summary"]
                break

        text = f"📌 경기 분석 – {title}\n\n{summary}"

        buttons = [
            [InlineKeyboardButton("📺 스포츠 무료 중계", url="https://goat-tv.com")],
            [InlineKeyboardButton("📝 분석글 더 보기", callback_data=f"analysis_root:{key}")],
            [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
        ]

        await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    # 뉴스 루트
    if data == "news_root":
        await q.edit_message_reply_markup(reply_markup=build_news_category_menu())
        return

    # 뉴스 종목 선택
    if data.startswith("news_cat:"):
        sport = data.split(":", 1)[1]
        await q.edit_message_reply_markup(reply_markup=build_news_list_menu(sport))
        return

    # 뉴스 아이템 선택
    if data.startswith("news_item:"):
        try:
            _, sport, news_id = data.split(":", 2)
            items = NEWS_DATA.get(sport, [])
            title = "뉴스 정보 없음"
            summary = "해당 뉴스 정보를 찾을 수 없습니다."

            for item in items:
                if item["id"] == news_id:
                    title = item["title"]
                    summary = item["summary"]
                    break
        except Exception:
            title = "뉴스 정보 없음"
            summary = "해당 뉴스 정보를 찾을 수 없습니다."

        text = f"📰 뉴스 요약 – {title}\n\n{summary}"

        buttons = [
            [InlineKeyboardButton("📺 스포츠무료중계", url="https://goat-tv.com")],
            [InlineKeyboardButton("📰 다른 뉴스 보기", callback_data="news_root")],
            [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
        ]

        await q.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

async def crawlmazsoccer_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) 해외축구
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/overseas",
        sport_label="축구",
        league_default="해외축구",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,
        category=1,
        export_site=True,   # ✅ 추가
    )

    # 2) K리그/J리그(asia)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/asia",
        sport_label="축구",
        league_default="K리그/J리그",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,
        category=2,
        export_site=True,   # ✅ 추가
    )

    await update.message.reply_text("⚽ 텔레그램용 + 사이트용(내일) 분석 크롤링을 모두 저장했습니다.")


# 야구(MLB · KBO · NPB) 분석 (내일 경기 → tomorrow 시트)
async def crawlmazbaseball_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    mazgtv 야구(MLB / KBO / NPB) 내일 경기 분석을 크롤링해서
    'tomorrow' 시트에 저장한다. 축구용과 동일한 구조.
    """
    # 해외야구(MLB)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/mlb",
        sport_label="야구",
        league_default="해외야구",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,
        category=3,
    )

    # KBO + NPB
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/baseball",
        sport_label="야구",
        league_default="KBO/NPB",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,
        category=4,
    )

    await update.message.reply_text(
        "⚾ 야구(MLB · KBO · NPB) 내일 경기 분석 크롤링 명령을 모두 실행했습니다."
    )

# 🔹 NBA + 국내 농구/배구 (내일 경기) 크롤링
async def bvcrawl_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    mazgtv 농구/배구 분석:
    - NBA 분석:    https://mazgtv1.com/analyze/nba
    - 국내 농구/배구: https://mazgtv1.com/analyze/volleyball
    두 곳에서 '내일 경기' 분석글을 크롤링해서 tomorrow 시트에 저장한다.
    """

    # 1) NBA (해외 농구)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/nba",
        sport_label="농구",          # 시트에는 NBA/KBL/WKBL 등으로 나뉨
        league_default="NBA",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,                # ⚠️ 실제 boardType 값으로 수정 필요
        category=5,                  # ⚠️ 실제 category 값으로 수정 필요
        # target_ymd=None → 자동으로 '내일' 날짜 사용
    )

    # 2) 국내 농구 + 배구 (KBL / WKBL / V리그 등)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/volleyball",
        sport_label="농구/배구",     # 분류 함수에서 KBL/WKBL/V리그/배구 등으로 세분화
        league_default="국내농구/배구",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,                # ⚠️ 실제 boardType 값으로 수정 필요
        category=7,                  # ⚠️ 실제 category 값으로 수정 필요
    )

    await update.message.reply_text(
        "NBA + 국내 농구/배구(내일 경기) 분석 크롤링을 모두 실행했습니다.\n"
        "/syncsheet 로 텔레그램 메뉴 데이터를 갱신할 수 있습니다."
    )

async def crawlmazsoccer_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    mazgtv 해외축구 + K리그/J리그 분석 중
    '오늘 날짜' 경기를 크롤링해서 today 시트에 저장.
    """

    # 1) 해외축구 탭
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/overseas",
        sport_label="축구",          # 안에서 '해외축구/K리그/J리그'로 다시 분류됨
        league_default="해외축구",
        day_key="today",            # ✅ today
        max_pages=5,
        board_type=2,
        category=1,                 # 해외축구
    )

    # 2) K리그 / J리그 탭
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/asia",
        sport_label="축구",
        league_default="K리그/J리그",
        day_key="today",            # ✅ today
        max_pages=5,
        board_type=2,
        category=2,                 # K리그/J리그
    )

    await update.message.reply_text(
        "⚽ 해외축구 + K리그/J리그 오늘 경기 분석 크롤링을 모두 실행했습니다."
    )

async def crawlmazbaseball_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    mazgtv 야구 분석(MLB + KBO + NPB) 중
    '오늘 날짜' 경기를 크롤링해서 today 시트에 저장.
    """

    # 1) 해외야구 (MLB)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/mlb",
        sport_label="야구",          # 시트에서는 해외야구/KBO/NPB로 분리됨
        league_default="해외야구",
        day_key="today",            # 🔴 오늘
        max_pages=5,
        board_type=2,               # 기존 /crawlmazbaseball_tomorrow 와 동일
        category=3,                 # MLB 쪽 category 값 (지금 쓰는 값 그대로)
    )

    # 2) KBO + NPB
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/baseball",
        sport_label="야구",
        league_default="KBO/NPB",
        day_key="today",            # 🔴 오늘
        max_pages=5,
        board_type=2,               # 동일 boardType
        category=4,                 # KBO/NPB 쪽 category 값 (지금 쓰는 값 그대로)
    )

    await update.message.reply_text(
        "⚾ mazgtv 야구(MLB · KBO · NPB) '오늘 경기' 분석 크롤링을 완료했습니다.\n"
        "today 시트에서 내용을 확인할 수 있습니다."
    )

# 🔹 NBA + 국내 농구/배구 (오늘 경기) 크롤링
async def bvcrawl_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    mazgtv 농구/배구 분석:
    - NBA 분석:    https://mazgtv1.com/analyze/nba
    - 국내 농구/배구: https://mazgtv1.com/analyze/volleyball
    두 곳에서 '오늘 경기' 분석글을 크롤링해서 today 시트에 저장한다.
    """

    # 1) NBA (해외 농구)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/nba",
        sport_label="농구",
        league_default="NBA",
        day_key="today",             # ✅ 오늘
        max_pages=5,
        board_type=2,                # 👉 tomorrow와 동일 값 유지
        category=5,
    )

    # 2) 국내 농구 + 배구 (KBL / WKBL / V리그 등)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/volleyball",
        sport_label="농구/배구",
        league_default="국내농구/배구",
        day_key="today",             # ✅ 오늘
        max_pages=5,
        board_type=2,                # 👉 tomorrow와 동일 값 유지
        category=7,
    )

    await update.message.reply_text(
        "NBA + 국내 농구/배구(오늘 경기) 분석 크롤링을 모두 실행했습니다.\n"
        "today 시트에서 내용을 확인할 수 있습니다."
    )


# ───────────────── 실행부 ─────────────────

def main():
    reload_analysis_from_sheet()
    reload_news_from_sheet()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_handler(CommandHandler("publish", publish))
    app.add_handler(CommandHandler("syncsheet", syncsheet))
    # 뉴스 시트 전체 초기화
    app.add_handler(CommandHandler("newsclean", newsclean))
    # today / tomorrow / news 전체 초기화
    app.add_handler(CommandHandler("allclean", allclean))    

    # 분석 시트 부분 초기화 명령어들 (모두 tomorrow 시트 기준)
    app.add_handler(CommandHandler("soccerclean", soccerclean))
    app.add_handler(CommandHandler("baseballclean", baseballclean))
    app.add_handler(CommandHandler("basketclean", basketclean))
    app.add_handler(CommandHandler("volleyclean", volleyclean))
    app.add_handler(CommandHandler("etcclean", etcclean))
    app.add_handler(CommandHandler("analysisclean", analysisclean))

    app.add_handler(CommandHandler("rollover", rollover))

    # 뉴스 크롤링 명령어들 (Daum)
    app.add_handler(CommandHandler("crawlsoccer", crawlsoccer))             # 해외축구
    app.add_handler(CommandHandler("crawlsoccerkr", crawlsoccerkr))         # 국내축구
    app.add_handler(CommandHandler("crawlbaseball", crawlbaseball))         # KBO
    app.add_handler(CommandHandler("crawloverbaseball", crawloverbaseball)) # 해외야구
    app.add_handler(CommandHandler("crawlbasketball", crawlbasketball))     # 농구
    app.add_handler(CommandHandler("crawlvolleyball", crawlvolleyball))     # 배구

    # mazgtv 해외축구 분석 (오늘 / 내일 경기 → today / tomorrow 시트)
    app.add_handler(CommandHandler("crawlmazsoccer_today", crawlmazsoccer_today))
    app.add_handler(CommandHandler("crawlmazsoccer_tomorrow", crawlmazsoccer_tomorrow))

    # mazgtv 야구 분석 (오늘 / 내일)
    app.add_handler(CommandHandler("crawlmazbaseball_today", crawlmazbaseball_today))
    app.add_handler(CommandHandler("crawlmazbaseball_tomorrow", crawlmazbaseball_tomorrow))

    # mazgtv 농구 + 배구 분석 (오늘 / 내일)
    app.add_handler(CommandHandler("bvcrawl_today", bvcrawl_today))
    app.add_handler(CommandHandler("bvcrawl_tomorrow", bvcrawl_tomorrow))




    app.add_handler(CallbackQueryHandler(on_callback))

    port = int(os.environ.get("PORT", "10000"))
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN,
        webhook_url=f"{APP_URL}/{TOKEN}",
    )


if __name__ == "__main__":
    main()
















