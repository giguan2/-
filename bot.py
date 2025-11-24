import os
import json
import time
import re
import requests
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from datetime import datetime, timedelta

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

# ───────────────── 구글 시트 연동 설정 ─────────────────

# GOOGLE_SERVICE_KEY  : 서비스계정 JSON 전체 (Render 환경변수)
# SPREADSHEET_ID      : 구글시트 ID (환경변수)
# SHEET_TODAY_NAME    : 오늘 탭 이름 (기본값 "today")
# SHEET_TOMORROW_NAME : 내일 탭 이름 (기본값 "tomorrow")

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
    아주 단순한 요약: 문장을 잘라서 앞에서부터 max_len까지 자르는 방식.
    """
    text = text.replace("\n", " ").strip()
    # 문장 단위로 대충 자르기 (한국어라 대충 마침표/다/요 기준)
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
    # 너무 짧으면 그냥 원문 한 번 더 잘라줌
    if not result:
        result = text[:max_len]
    return result


def clean_daum_body_text(text: str) -> str:
    """
    다음 뉴스 본문에서 '음성으로 듣기', 번역/요약 UI 텍스트를 최대한 제거.
    """
    if not text:
        return ""

    # 1단계: 번역/요약/언어선택 블록이 시작되기 전까지만 사용
    cut_keywords = [
        "번역 설정",                      # 번역 설정 ...
        "번역 beta",                     # 번역 beta ...
        "Translated by",                # Translated by kakao ...
        "Now in translation",           # Now in translation ...
        "요약본이 자동요약",             # 요약 안내문
        "기사 제목과 주요 문장을 기반으로 자동요약한 결과입니다",
    ]
    cut_pos = None
    for kw in cut_keywords:
        idx = text.find(kw)
        if idx != -1:
            if cut_pos is None or idx < cut_pos:
                cut_pos = idx

    if cut_pos is not None:
        text = text[:cut_pos]

    # 2단계: 줄 단위로 나눈 뒤 언어 목록 등 불필요한 줄 제거
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
        if any(b in l for b in blacklist):
            continue
        clean_lines.append(l)

    return " ".join(clean_lines)

def crawl_naver_soccer(max_count: int = 5) -> list[dict]:
    """
    (다음 스포츠 해외축구) 최신 뉴스 일부를 크롤링해서
    [ {title, summary, url}, ... ] 리스트로 반환
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    articles: list[dict] = []

    # ── 1) 다음 harmony JSON API에서 해외축구 리스트 가져오기 ──
    base_url = "https://sports.daum.net/media-api/harmony/contents.json"

    # 한국 시간 기준 오늘 날짜
    today_kst = get_kst_now().date()
    ymd = today_kst.strftime("%Y%m%d")
    create_dt = f"{ymd}000000~{ymd}235959"

    # discoveryTag[0] 값 (해외축구 카테고리 ID: 100032)
    discovery_tag_value = json.dumps(
        {
            "group": "media",
            "key": "defaultCategoryId3",
            "value": "100032",
        },
        ensure_ascii=False,
    )

    params = {
        "page": 0,
        "consumerType": "HARMONY",
        "status": "SERVICE",
        "createDt": create_dt,
        "size": max_count if max_count > 0 else 5,
        "discoveryTag[0]": discovery_tag_value,
    }

    try:
        resp = requests.get(base_url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[CRAWLER] 다음 harmony API 요청 실패: {e}")
        return articles

    try:
        data = resp.json()
    except Exception as e:
        print(f"[CRAWLER] JSON 파싱 실패: {e}")
        return articles

    # contents 리스트 찾기 (구조 변화에 대비한 방어 코드)
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
        print(
            "[CRAWLER] JSON에서 contents 리스트를 찾지 못했습니다.",
            f"type={type(data)}, keys={list(data.keys()) if isinstance(data, dict) else 'N/A'}",
        )
        return articles

    # ── 2) JSON에서 제목 + 링크 추출 ──
    link_items: list[dict] = []
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

        # 상대경로면 절대경로로 변환
        if url.startswith("/"):
            url = urljoin("https://sports.daum.net", url)

        link_items.append({"title": title, "url": url})

        if len(link_items) >= max_count:
            break

    if not link_items:
        print("[CRAWLER] 제목/URL 추출 실패 (contents 구조 변경 가능성)")
        return articles

    # ── 3) 각 기사 페이지에서 본문 긁고 요약 ──
    for it in link_items:
        link = it["url"]
        title = it["title"]

        try:
            resp2 = requests.get(link, headers=headers, timeout=10)
            resp2.raise_for_status()
            s2 = BeautifulSoup(resp2.text, "html.parser")

            body_el = (
                s2.select_one("div#harmonyContainer")
                or s2.select_one("div#mArticle div#harmonyContainer")
                or s2.select_one("div#mArticle")
                or s2.find("article")
                or s2.body
            )

            if not body_el:
                print(f"[CRAWLER] 본문 태그 못 찾음: {link}")
                continue

            raw_body_text = body_el.get_text("\n", strip=True)
            clean_body_text = clean_daum_body_text(raw_body_text)
            summary = summarize_text(clean_body_text, max_len=400)

            articles.append(
                {
                    "title": title,
                    "summary": summary,
                    "url": link,
                }
            )

        except Exception as e:
            print(f"[CRAWLER] 기사 파싱 실패 ({link}): {e}")
            continue

    return articles


def _load_analysis_sheet(sh, sheet_name: str) -> dict:
    """
    구글시트에서 한 탭(today / tomorrow)을 읽어서
    { sport: [ {id,title,summary}, ... ] } 구조로 변환

    시트 컬럼 구조 (1행 헤더 기준):
    A열: sport   (예: 축구/농구/야구/배구)
    B열: id      (bot에서 쓸 고유 id, 비워두면 자동 생성)
    C열: title   (버튼에 보이는 제목)
    D열: summary (분석 본문)
    """
    try:
        ws = sh.worksheet(sheet_name)
    except Exception as e:
        print(f"[GSHEET] 시트 '{sheet_name}' 열기 실패: {e}")
        return {}

    rows = ws.get_all_values()
    if not rows:
        return {}

    # 헤더 파싱 (첫 행)
    header = rows[0]
    # 기본 인덱스
    idx_sport = 0
    idx_id = 1
    idx_title = 2
    idx_summary = 3

    def safe_index(name, default):
        try:
            return header.index(name)
        except ValueError:
            return default

    # 헤더에 'sport', 'id', 'title', 'summary' 글자가 있으면 그 위치 사용
    idx_sport = safe_index("sport", idx_sport)
    idx_id = safe_index("id", idx_id)
    idx_title = safe_index("title", idx_title)
    idx_summary = safe_index("summary", idx_summary)

    data: dict[str, list[dict]] = {}

    for row in rows[1:]:  # 데이터 행
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

        # id 없으면 자동 생성
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


NEWS_DATA = {}


def _load_news_sheet(sh, sheet_name: str) -> dict:
    """
    구글시트에서 뉴스 탭을 읽어서
    {
        sport: [ {id,title,summary}, ... ]
    } 구조로 변환

    시트 컬럼 구조 (1행 헤더 기준):
    A열: sport
    B열: id
    C열: title
    D열: summary
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

    # 기본 index
    idx_sport = 0
    idx_id = 1
    idx_title = 2
    idx_summary = 3

    # 헤더 기반 동적 매칭
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

        # id 비어 있으면 자동 생성
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


def build_analysis_match_menu(key: str, sport: str) -> InlineKeyboardMarkup:
    """종목 선택 후 → 해당 종목 경기 리스트 메뉴"""
    items = ANALYSIS_DATA_MAP.get(key, {}).get(sport, [])
    buttons = []
    for item in items:
        cb = f"match:{key}:{sport}:{item['id']}"
        buttons.append([InlineKeyboardButton(item["title"], callback_data=cb)])

    buttons.append([InlineKeyboardButton("◀ 종목 선택으로", callback_data=f"analysis_root:{key}")])
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

    # 오늘 분석 버튼
    if mode == "today":
        await update.message.reply_text(
            f"{today_str} 경기 분석픽 메뉴입니다. 종목을 선택하세요 👇",
            reply_markup=build_analysis_category_menu("today"),
        )
        return

    # 내일 분석 버튼
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

    # 그 외: DM에서 전체 레이아웃 미리보기
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

    # 기존 고정 메시지 해제 (선택)
    try:
        await context.bot.unpin_all_chat_messages(CHANNEL_ID)
    except Exception:
        pass

    # 채널에 DM과 동일한 메뉴 전송
    msg = await send_main_menu(CHANNEL_ID, context, preview=False)

    # 방금 보낸 메뉴 메시지 상단 고정
    await context.bot.pin_chat_message(
        chat_id=CHANNEL_ID,
        message_id=msg.message_id,
        disable_notification=True,
    )

    await update.message.reply_text("채널에 메뉴를 올리고 상단에 고정했습니다 ✅")


# 5) /syncsheet – 구글시트에서 분석 데이터 다시 로딩
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

            # tomorrow 탭 전체 데이터 가져오기
            rows = ws_tomorrow.get_all_values()

            if rows:
                # 1-1) today 탭을 tomorrow 내용으로 통째로 덮어쓰기
                ws_today.clear()
                ws_today.update("A1", rows)

                # 1-2) tomorrow 탭은 헤더만 남기고 비우기
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


async def fetch_daum_worldsoccer_json(client: httpx.AsyncClient) -> list[dict]:
    """
    다음 스포츠 해외축구 뉴스 JSON 리스트를 가져온다.
    Daum 내부 harmony API 사용.
    """
    base_url = "https://sports.daum.net/media-api/harmony/contents.json"

    today_kst = get_kst_now().date()
    ymd = today_kst.strftime("%Y%m%d")
    create_dt = f"{ymd}000000~{ymd}235959"

    discovery_tag_value = json.dumps({
        "group": "media",
        "key": "defaultCategoryId3",
        "value": "100032",      # 해외축구 카테고리 ID
    }, ensure_ascii=False)

    params = {
        "page": 0,
        "consumerType": "HARMONY",
        "status": "SERVICE",
        "createDt": create_dt,
        "size": 20,
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
        print("[CRAWL][DAUM] JSON 구조를 파악하지 못했습니다. 최상위 키:", list(data.keys()) if isinstance(data, dict) else type(data))
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


async def crawlsoccer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 관리자만 사용
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    await update.message.reply_text("다음스포츠 해외축구 뉴스를 크롤링합니다. 잠시만 기다려 주세요...")

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        ) as client:

            contents = await fetch_daum_worldsoccer_json(client)

            if not contents:
                await update.message.reply_text("해외축구 JSON 데이터에서 기사를 찾지 못했습니다.")
                return

            articles = []

            # 2) JSON에서 제목 + 기사 URL 추출
            for item in contents:
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

                if len(articles) >= 10:
                    break

            if not articles:
                await update.message.reply_text("JSON은 받았지만, 제목/URL 정보를 찾지 못했습니다.")
                return

            # 3) 각 기사 페이지 들어가서 본문 크롤링 + 요약
            for art in articles:
                try:
                    r2 = await client.get(art["link"], timeout=10.0)
                    r2.raise_for_status()
                    s2 = BeautifulSoup(r2.text, "html.parser")

                    body_el = (
                        s2.select_one("div#harmonyContainer")
                        or s2.select_one("div#mArticle div#harmonyContainer")
                        or s2.select_one("div#mArticle")
                        or s2.find("article")
                        or s2.body
                    )

                    if body_el:
                        body_text = body_el.get_text("\n", strip=True)
                    else:
                        body_text = ""

                    clean_text = clean_daum_body_text(body_text)
                    art["summary"] = simple_summarize(clean_text, max_chars=400)

                except Exception as e:
                    print(f"[CRAWL][DAUM] 기사 파싱 실패 ({art['link']}): {e}")
                    art["summary"] = "(본문 크롤링 실패)"

    except Exception as e:
        await update.message.reply_text(f"요청 오류가 발생했습니다: {e}")
        return

    # 4) 구글 시트 저장
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
            "축구",          # sport
            "",             # id (비워두면 나중에 자동 생성)
            art["title"],   # title
            art["summary"], # summary
        ])

    try:
        ws.append_rows(rows_to_append, value_input_option="RAW")
    except Exception as e:
        await update.message.reply_text(f"시트 쓰기 오류: {e}")
        return

    await update.message.reply_text(
        f"다음스포츠 해외축구 뉴스 {len(rows_to_append)}건을 저장했습니다.\n"
        "/syncsheet 로 텔레그램 메뉴를 갱신할 수 있습니다."
    )


# 4) 인라인 버튼 콜백 처리 (분석/뉴스 팝업)
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()  # 기본 로딩표시 제거

    # 메인 메뉴로 돌아가기
    if data == "back_main":
        await q.edit_message_reply_markup(reply_markup=build_main_inline_menu())
        return

    # 분석픽 루트: 종목 리스트
    if data.startswith("analysis_root:"):
        _, key = data.split(":", 1)          # today / tomorrow
        await q.edit_message_reply_markup(reply_markup=build_analysis_category_menu(key))
        return

    # 분석픽 – 종목 선택
    if data.startswith("analysis_cat:"):
        _, key, sport = data.split(":", 2)
        await q.edit_message_reply_markup(reply_markup=build_analysis_match_menu(key, sport))
        return

    # 분석픽 – 개별 경기 선택 → 채팅창에 분석글 전송
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

    # 스포츠 뉴스 요약 루트: 뉴스 종목 선택
    if data == "news_root":
        await q.edit_message_reply_markup(reply_markup=build_news_category_menu())
        return

    # 뉴스 – 종목 선택
    if data.startswith("news_cat:"):
        sport = data.split(":", 1)[1]
        await q.edit_message_reply_markup(reply_markup=build_news_list_menu(sport))
        return

    # 뉴스 제목 클릭 → 채팅창에 요약 메시지로 보내기
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


# ───────────────── 실행부 ─────────────────

def main():
    # 서버 시작할 때 한 번 시트에서 데이터 읽어오기
    reload_analysis_from_sheet()
    reload_news_from_sheet()

    app = ApplicationBuilder().token(TOKEN).build()

    # 1:1 테스트용
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # 채널 메뉴용
    app.add_handler(CommandHandler("publish", publish))

    # 구글시트 수동 새로고침
    app.add_handler(CommandHandler("syncsheet", syncsheet))

    # 🔹 오늘 ← 내일 복사용 롤오버 명령
    app.add_handler(CommandHandler("rollover", rollover))

    # 🔹 해외축구 뉴스 크롤링 명령
    app.add_handler(CommandHandler("crawlsoccer", crawlsoccer))

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

