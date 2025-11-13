import os
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ───────────────── 기본 설정 ─────────────────
TOKEN = os.getenv("BOT_TOKEN")
APP_URL = (os.getenv("APP_URL") or "").strip()
CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()  # 예: @sportpicck 또는 -100xxxxxxxxxxxx

# 채널/미리보기 공통으로 사용할 설명 텍스트
MENU_CAPTION = (
    "📌 스포츠 정보&분석 공유방 메뉴 안내\n\n"
    "1️⃣ 실시간 무료 중계 - GOAT-TV 라이브 중계 바로가기\n"
    "2️⃣ 오늘 경기 분석픽 - 종목별로 오늘 경기 분석을 확인하세요\n"
    "3️⃣ 금일 스포츠 정보 - 주요 이슈 & 뉴스 요약 정리\n\n"
    "아래 버튼을 눌러 원하는 메뉴를 선택하세요 👇"
)

# ───────────────── 분석/뉴스 데이터 (예시) ─────────────────

ANALYSIS_DATA = {
    "축구": [
        {
            "id": "soccer_1",
            "title": "EPL - 아스널 vs 토트넘",
            "summary": "아스널은 홈에서 공격 전개가 매끄럽고, 토트넘은 역습이 위협적인 매치업. "
                       "중원 장악 여부가 승부를 가를 가능성이 크다."
        },
        {
            "id": "soccer_2",
            "title": "라리가 - 바르셀로나 vs 레알 마드리드",
            "summary": "양 팀 모두 측면 공격이 날카롭고, 슈팅 수 싸움이 중요해 보이는 경기."
        },
    ],
    "농구": [
        {
            "id": "basket_1",
            "title": "NBA - 11.14 클리블랜드 vs 토론토",
            "summary": """📌 클리블랜드 vs 토론토 분석 요약

✔️ 팀 분위기 & 최근 흐름
클리블랜드: 최근 6경기 5승. 주전 결장에도 마이애미 원정 설욕 성공. 홈 3연승 포함 4승 1패로 안정감.
토론토: 브루클린전 승리로 연패 차단. 리바운드 우위는 좋았지만 원정 3연전 마지막 경기로 체력 부담 가능.

✔️ 상대 전적
최근 5경기 클리블랜드 4승 1패 우세.
가장 최근 홈 맞대결에서는 클리블랜드 패배.

✔️ 부상자
클리블랜드 캐벌리어스:
- 대리어스 갈랜드(G) 11월 13일 복귀 예정
- 제일런 타이슨(G) 11월 13일 복귀 예정
- 에반 모블리(C) 11월 13일 복귀 예정
- 도노반 미첼(G) 11월 13일 복귀 예정
- 맥스 스트러스(G) 12월 1일 복귀 예정

토론토 랩터스:
- 샌드로 마무켈라슈빌리(F) 당일 결정
- 오차이 아바지(G) 당일 결정
- 콜린 머레이 보일스(F) 당일 결정

🔥 추천픽
✅ 일반승: 클리블랜드 승
✅ 핸디캡: 클리블랜드 -7.5 승
✅ 언오버: 240.5 오버
✅ 추세: 홈 강세 + 주전 복귀로 클리블랜드 기대치 상승""",
        },
        {
            "id": "basket_2",
            "title": "NBA - 11.14 피닉스 vs 인디애나",
            "summary": """📌 피닉스 vs 인디애나 분석 요약

✔️ 팀 분위기 & 최근 흐름
피닉스: 4연승 + 최근 7경기 6승 1패 흐름. 부커·굿윈·그레이슨 앨런이 꾸준히 득점하며 경기 주도.
인디애나: 5연패 + 원정 6연패. 시아캄이 분전했지만 핵심 전력 부재로 공격·수비 모두 붕괴.

✔️ 상대 전적
최근 맞대결에서 피닉스가 108-126 패배.
최근 5경기 2승 3패.

✔️ 부상자
피닉스 선즈:
- 제일런 그린(G) 12월 18일 복귀 예정

인디애나 페이서스:
- 캠 존스(G) 12월 3일 복귀 예정
- 베네딕트 마서린(G) 11월 15일 복귀 예정
- 조니 퍼피(G) 11월 15일 복귀 예정
- 퀸튼 잭슨(G) 11월 15일 복귀 예정
- 오비 토핀(F) 2월 2일 복귀 예정

🔥 추천픽
✅ 일반승: 피닉스 승
✅ 핸디캡: 피닉스 -4.5 승
✅ 언오버: 230.5 오버
✅ 추세: 홈 강세 + 인디애나 전력 붕괴""",
        },
                {
            "id": "basket_3",
            "title": "NBA - 11.14 유타 vs 애틀랜",
            "summary": """📌 유타 vs 애틀랜타 분석 요약

✔️ 팀 분위기 & 최근 흐름
유타: 인디애나전 152득점 폭발로 3연패 탈출. 마카넨 35득점, 루키 베일리·미하일류크 활약으로 공격력 상승. 콜리어의 11어시스트로 볼 전개 안정. 홈 3승2패로 흐름 괜찮음.
애틀랜타: 트레이 영 없이도 3연승. 새크라멘토전 133-100 완승 포함 원정 2연승. 제일런 존슨 중심으로 7명이 두 자릿수 득점하며 고른 전력. 다만 백투백 원정으로 체력 부담 존재.

✔️ 상대 전적
최근 맞대결에서 유타가 134-147 패배
최근 5경기 1승 4패로 유타 열세

✔️ 부상자
유타 재즈:
테일러 헨드릭스(F) 11월16일 복귀 예정
카일 앤더슨(F) 11월16일 복귀 예정
조지스 니앙(F) 11월18일 복귀 예정
워커 케슬러(C) 시즌 아웃

애틀랜타 호크스:
니콜라 두리시치(F) 11월13일 복귀 예정
트레이 영(G) 11월30일 복귀 예정

🔥 추천픽

✅ 일반승: 유타 승
✅ 핸디캡: 유타 +1.5 승
✅ 언오버: 233.5 오버
✅ 추세: 홈 공격력 상승 + 애틀랜타 백투백 체력 부담""",
        },
    ],

    "야구": [
        {
            "id": "base_1",
            "title": "KBO - LG 트윈스 vs 롯데 자이언츠",
            "summary": "선발 투수의 컨디션 차이가 큰 경기. 초반 실점 관리가 중요하다."
        },
    ],
    "배구": [
        {
            "id": "vball_1",
            "title": "V-리그 - 대한항공 vs 현대캐피탈",
            "summary": "서브와 리시브 싸움이 강조되는 매치업. 블로킹 싸움에서도 차이가 날 수 있다."
        },
    ],
}

NEWS_ITEMS = [
    {
        "id": "news_1",
        "title": "손흥민, 리그 15호 골 폭발",
        "summary": "손흥민이 리그 15호 골을 기록하며 팀의 승리를 이끌었다. "
                   "최근 5경기 연속 공격포인트로 폼이 절정에 이르렀다는 평가."
    },
    {
        "id": "news_2",
        "title": "NBA 파이널 1차전 리뷰",
        "summary": "양 팀 모두 수비 집중력이 높았던 경기. 클러치 타임 3점슛 한 방이 승패를 가르며 "
                   "파이널다운 긴장감이 이어졌다."
    },
]

# ───────────────── 키보드/메뉴 구성 ─────────────────

def build_reply_keyboard() -> ReplyKeyboardMarkup:
    """봇 1:1 테스트용 간단 하단 키보드"""
    menu = [
        ["메뉴 미리보기", "도움말"],
    ]
    return ReplyKeyboardMarkup(menu, resize_keyboard=True)


def build_main_inline_menu() -> InlineKeyboardMarkup:
    """메인 인라인 메뉴 (채널/미리보기 공통)"""
    buttons = [
        [InlineKeyboardButton("실시간 무료 중계", url="https://goat-tv.com")],
        [InlineKeyboardButton("오늘 경기 분석픽", callback_data="analysis_root")],
        [InlineKeyboardButton("금일 스포츠 정보", callback_data="news_root")],
    ]
    return InlineKeyboardMarkup(buttons)


def build_analysis_category_menu() -> InlineKeyboardMarkup:
    """오늘 경기 분석픽 → 종목 선택 메뉴"""
    buttons = [
        [InlineKeyboardButton("축구", callback_data="analysis_cat:축구")],
        [InlineKeyboardButton("농구", callback_data="analysis_cat:농구")],
        [InlineKeyboardButton("야구", callback_data="analysis_cat:야구")],
        [InlineKeyboardButton("배구", callback_data="analysis_cat:배구")],
        [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)


def build_analysis_match_menu(sport: str) -> InlineKeyboardMarkup:
    """종목 선택 후 → 해당 종목 경기 리스트 메뉴"""
    items = ANALYSIS_DATA.get(sport, [])
    buttons = []
    for item in items:
        cb = f"match:{sport}:{item['id']}"
        buttons.append([InlineKeyboardButton(item["title"], callback_data=cb)])
    buttons.append([InlineKeyboardButton("◀ 종목 선택으로", callback_data="analysis_root")])
    buttons.append([InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def build_news_list_menu() -> InlineKeyboardMarkup:
    """금일 스포츠 정보 → 뉴스 제목 리스트 메뉴"""
    buttons = []
    for idx, item in enumerate(NEWS_ITEMS):
        cb = f"news_item:{idx}"
        buttons.append([InlineKeyboardButton(item["title"], callback_data=cb)])
    buttons.append([InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

# ───────────────── 공통: 메인 메뉴 보내는 함수 ─────────────────

async def send_main_menu(chat_id: int | str, context: ContextTypes.DEFAULT_TYPE, preview: bool = False):
    """
    채널/DM 공통으로 '텍스트 + 메인 메뉴 버튼' 전송.
    (이미지는 사용하지 않음)
    """
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=MENU_CAPTION,
        reply_markup=build_main_inline_menu(),
    )
    return msg

# ───────────────── 핸들러들 ─────────────────

# 1) /start – DM에서 채널과 동일한 레이아웃 미리보기
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # 안내 + 하단 테스트 키보드
    await update.message.reply_text(
        "스포츠봇입니다.\n"
        "아래에는 채널에 올라갈 메뉴와 동일한 레이아웃 미리보기를 보여줄게.\n"
        "실제 채널 배포는 /publish 명령으로 진행하면 돼.",
        reply_markup=build_reply_keyboard(),
    )

    # 채널과 똑같은 텍스트 + 메인 메뉴 미리보기
    await send_main_menu(chat_id, context, preview=True)


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
    if data == "analysis_root":
        await q.edit_message_reply_markup(reply_markup=build_analysis_category_menu())
        return

    # 분석픽 – 종목 선택
    if data.startswith("analysis_cat:"):
        sport = data.split(":", 1)[1]
        await q.edit_message_reply_markup(reply_markup=build_analysis_match_menu(sport))
        return

    # ✅ 분석픽 – 개별 경기 선택 → 채팅창에 분석글 메시지로 보내기
    if data.startswith("match:"):
        _, sport, match_id = data.split(":", 2)
        items = ANALYSIS_DATA.get(sport, [])

        title = "선택한 경기"
        summary = "해당 경기 분석을 찾을 수 없습니다."

        for item in items:
            if item["id"] == match_id:
                title = item["title"]
                summary = item["summary"]
                break

        text = f"📌 경기 분석 – {title}\n\n{summary}"

        # 분석 글 아래에 버튼 2개 달기
        buttons = [
            [InlineKeyboardButton("📝 분석글 더 보기", callback_data="analysis_root")],
            [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
        ]

        await q.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return


    # 금일 스포츠 정보 루트: 뉴스 리스트
    if data == "news_root":
        await q.edit_message_reply_markup(reply_markup=build_news_list_menu())
        return

    # ✅ 뉴스 제목 클릭 → 채팅창에 요약 메시지로 보내기
    if data.startswith("news_item:"):
        try:
            idx = int(data.split(":", 1)[1])
            item = NEWS_ITEMS[idx]
            title = item["title"]
            summary = item["summary"]
        except Exception:
            title = "뉴스 정보 없음"
            summary = "해당 뉴스 정보를 찾을 수 없습니다."

        text = f"📰 뉴스 요약 – {title}\n\n{summary}"

        buttons = [
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
    app = ApplicationBuilder().token(TOKEN).build()

    # 1:1 테스트용
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # 채널 메뉴용
    app.add_handler(CommandHandler("publish", publish))
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






