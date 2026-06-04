"""
Telegram Secretary Bot — Render 版本 (Webhook)
"""

import os
import logging
import asyncio
from flask import Flask, request, Response
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import google.generativeai as genai

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
RENDER_URL = os.environ["RENDER_URL"]

SYSTEM_PROMPT = """你是一个私人秘书助理。你的职责是：
- 回答主人提出的任何问题
- 帮助规划、整理、分析信息
- 记住对话上下文，像真人秘书一样自然对话
- 回答简洁实用，不废话
- 默认用中文回复，除非主人用其他语言

你的主人叫 Brandon。"""

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    model_name="gemini-3.1-flash-lite",
    system_instruction=SYSTEM_PROMPT,
)

chat_sessions: dict = {}

def get_session(chat_id: int):
    if chat_id not in chat_sessions:
        chat_sessions[chat_id] = model.start_chat(history=[])
    return chat_sessions[chat_id]

def run_async(coro):
    """在任何线程里安全地跑 async 函数"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 你好，我是你的私人秘书！\n\n"
        "直接发消息给我就行，问什么都可以。\n\n"
        "指令：\n"
        "/start — 开始\n"
        "/clear — 清除对话记忆\n"
        "/help — 帮助"
    )

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in chat_sessions:
        del chat_sessions[chat_id]
    await update.message.reply_text("✅ 对话记忆已清除，重新开始吧！")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 使用方法：\n\n"
        "• 直接发消息 → 我会回答\n"
        "• /clear → 清除记忆，重新开始\n"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        session = get_session(chat_id)
        response = session.send_message(user_text)
        reply = response.text
    except Exception as e:
        logger.error(f"Gemini 出错: {e}")
        reply = "⚠️ 出了点问题，请稍后再试。"
    await update.message.reply_text(reply)


# ─── 初始化 PTB Application ──────────────────────────────────
ptb_app = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .updater(None)
    .build()
)

ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("clear", clear))
ptb_app.add_handler(CommandHandler("help", help_command))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

run_async(ptb_app.initialize())


# ─── Flask ───────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200

@flask_app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, ptb_app.bot)
    run_async(ptb_app.process_update(update))
    return Response("ok", status=200)

@flask_app.route("/set_webhook", methods=["GET"])
def set_webhook():
    url = f"{RENDER_URL}/webhook/{TELEGRAM_TOKEN}"
    run_async(ptb_app.bot.set_webhook(url=url))
    return f"Webhook 已设置到: {url}", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
