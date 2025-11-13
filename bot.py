import os
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu = [
        ["라이브 보기", "뉴스"],
        ["분석 보기", "고객센터"]
    ]
    await update.message.reply_text(
        "원하시는 메뉴를 선택하세요:",
        reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True)
    )

    buttons = [
        [InlineKeyboardButton("라이브 보기", url="https://example.com/live")],
        [InlineKeyboardButton("뉴스", callback_data="news")],
        [InlineKeyboardButton("분석", callback_data="analysis")]
    ]
    await update.message.reply_text("빠른 메뉴:", reply_markup=InlineKeyboardMarkup(buttons))

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "라이브" in text:
        await update.message.reply_text("라이브 보기: https://example.com/live")
    elif "뉴스" in text:
        await update.message.reply_text("뉴스 보기: https://example.com/news")
    elif "분석" in text:
        await update.message.reply_text("분석 보기: https://example.com/analysis")
    elif "고객센터" in text:
        await update.message.reply_text("문의: @your_admin")
    else:
        await update.message.reply_text("메뉴에서 선택해주세요. (/start 입력으로 다시 보기)")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "news":
        await q.edit_message_text("최신 뉴스: https://example.com/news")
    elif q.data == "analysis":
        await q.edit_message_text("분석 모음: https://example.com/analysis")

async def set_webhook(app):
    await app.bot.set_webhook(url=f"{APP_URL}/{TOKEN}")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.post_init = set_webhook

    port = int(os.environ.get("PORT", "10000"))
    app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN,webhook_url=f"{APP_URL}/{TOKEN}".strip())

# --- 기존 코드 (on_callback, set_webhook, main 등) ---

# 여기부터 추가
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler
import os

CHANNEL_ID = os.getenv("CHANNEL_ID")  # 채널 ID 불러오기

async def publish(update, context):
    buttons = [
        [InlineKeyboardButton("📺 라이브 보기", url="https://example.com/live")],
        [InlineKeyboardButton("📰 뉴스", url="https://example.com/news")],
        [InlineKeyboardButton("🎯 분석", url="https://example.com/analysis")]
    ]
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text="빠른 메뉴:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    await update.message.reply_text("채널로 메뉴를 보냈어요 ✅")


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(CommandHandler("publish", publish))  # ← 이 줄이 새로 추가된 부분

    port = int(os.environ.get("PORT", "10000"))
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN,
        webhook_url=f"{APP_URL}/{TOKEN}".strip()
    )


if __name__ == "__main__":
    main()

