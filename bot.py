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
        #    예) "[포포투=김아인] 맨유 감독…" / "[SPORTALKOREA] 박문서 기자"
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
        await update.message.reply_text(f"시트 초기화 중 오류가 발생했습니다: {e}")
        return


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


# ───────────────── Gemini 요약 함수 ─────────────────

def summarize_with_gemini(full_text: str, max_chars: int = 400) -> str:
    """
    Gemini API를 사용해서 뉴스 기사를 서술형으로 요약한다.
    실패하면 simple_summarize로 폴백.
    """
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

    if not GEMINI_API_KEY:
        print("[GEMINI] GEMINI_API_KEY 미설정 → simple_summarize 사용")
        return simple_summarize(full_text, max_chars=max_chars)

    trimmed = full_text.strip()
    if len(trimmed) > 6000:
        trimmed = trimmed[:6000]

    prompt = (
        "다음은 스포츠 뉴스 기사 원문이다.\n"
        "전체 내용을 이해한 뒤 2~3문장으로 자연스러운 서술형 뉴스 요약을 작성해줘.\n"
        "기사 앞부분을 그대로 복사하지 말 것.\n"
        f"전체 길이는 {max_chars}자 내외.\n\n"
        "===== 기사 원문 =====\n"
        f"{trimmed}\n"
    )

    url = "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }

    try:
        resp = requests.post(
            url,
            headers=headers,
            params=params,
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()

        candidates = data.get("candidates") or []
        if not candidates:
            raise ValueError("no candidates from Gemini")

        parts = (candidates[0].get("content") or {}).get("parts") or []
        result = "".join(p.get("text", "") for p in parts).strip()

        if not result:
            raise ValueError("empty response")

        if len(result) > max_chars + 100:
            result = result[: max_chars + 100]

        return result

    except Exception as e:
        print(f"[GEMINI] 요약 실패 → fallback: {e}")
        return simple_summarize(full_text, max_chars=max_chars)

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

                    # ✅ 여기서 Gemini로 요약 (뉴스 기사 스타일, 400자 내외)
                    art["summary"] = summarize_with_gemini(clean_text, max_chars=400)

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


# ───────────────── 종목별 크롤링 명령어 ─────────────────

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

    if data == "back_main":
        await q.edit_message_reply_markup(reply_markup=build_main_inline_menu())
        return

    if data.startswith("analysis_root:"):
        _, key = data.split(":", 1)
        await q.edit_message_reply_markup(reply_markup=build_analysis_category_menu(key))
        return

    if data.startswith("analysis_cat:"):
        _, key, sport = data.split(":", 2)
        await q.edit_message_reply_markup(reply_markup=build_analysis_match_menu(key, sport))
        return

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

    if data == "news_root":
        await q.edit_message_reply_markup(reply_markup=build_news_category_menu())
        return

    if data.startswith("news_cat:"):
        sport = data.split(":", 1)[1]
        await q.edit_message_reply_markup(reply_markup=build_news_list_menu(sport))
        return

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

    app.add_handler(CommandHandler("rollover", rollover))

    # 뉴스 크롤링 명령어들
    app.add_handler(CommandHandler("crawlsoccer", crawlsoccer))             # 해외축구
    app.add_handler(CommandHandler("crawlsoccerkr", crawlsoccerkr))         # 국내축구
    app.add_handler(CommandHandler("crawlbaseball", crawlbaseball))         # KBO
    app.add_handler(CommandHandler("crawloverbaseball", crawloverbaseball)) # 해외야구
    app.add_handler(CommandHandler("crawlbasketball", crawlbasketball))     # 농구
    app.add_handler(CommandHandler("crawlvolleyball", crawlvolleyball))     # 배구

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






