"""
Telegram Secretary Bot — Render 版本 (Webhook + 提醒功能)
"""

import os
import logging
import asyncio
from datetime import datetime, timedelta
import re

from flask import Flask, request, Response
from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import google.generativeai as genai
from apscheduler.schedulers.background import BackgroundScheduler

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
RENDER_URL = os.environ["RENDER_URL"]

SYSTEM_PROMPT = """你是一个私人秘书助理。你的职责是：
- 回答主人提出的任何问题
- 帮助规划、整理、分析信息
- 记住对话上下文，像真人秘书一样自然对话
- 回答简洁实用，不废话
- 默认用中文回复，除非主人用其他语言

你的主人叫 Brandon。

【提醒功能】
如果主人要你提醒他做某件事，你必须在回复里包含以下格式（放在回复末尾）：
REMINDER|<分钟数>|<提醒内容>

例子：
主人说："30分钟后提醒我喝水"
你回复："好的，30分钟后我会提醒你喝水！\nREMINDER|30|喝水"

主人说："1小时后提醒我开会"
你回复："好的，1小时后提醒你开会。\nREMINDER|60|开会"

主人说："明天早上9点提醒我交报告"（假设现在是晚上10点）
你回复："好的，明天早上9点提醒你交报告。\nREMINDER|660|交报告"

如果主人没有要求提醒，正常回答，不要加 REMINDER 行。"""

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    model_name="gemini-2.5-flash",
    system_instruction=SYSTEM_PROMPT,
)

chat_sessions: dict = {}
scheduler = BackgroundScheduler()
scheduler.start()

def get_session(chat_id: int):
    if chat_id not in chat_sessions:
        chat_sessions[chat_id] = model.start_chat(history=[])
    return chat_sessions[chat_id]

def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)

def send_reminder(chat_id: int, message: str):
    """定时器触发时发提醒消息"""
    async def _send():
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=chat_id, text=f"⏰ 提醒：{message}")
    run_async(_send())


# ─── 指令处理 ────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 你好，我是你的私人秘书！\n\n"
        "直接发消息给我就行，问什么都可以。\n\n"
        "提醒例子：\n"
        "• 30分钟后提醒我喝水\n"
        "• 1小时后提醒我开会\n"
        "• 明天早上9点提醒我交报告\n\n"
        "指令：\n"
        "/start — 开始\n"
        "/clear — 清除对话记忆\n"
        "/reminders — 查看待办提醒"
    )

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in chat_sessions:
        del chat_sessions[chat_id]
    await update.message.reply_text("✅ 对话记忆已清除，重新开始吧！")

async def reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = scheduler.get_jobs()
    if not jobs:
        await update.message.reply_text("📋 目前没有待办提醒。")
        return
    msg = "📋 待办提醒：\n\n"
    for job in jobs:
        run_time = job.next_run_time.strftime("%m-%d %H:%M") if job.next_run_time else "?"
        msg += f"• {run_time} — {job.args[1]}\n"
    await update.message.reply_text(msg)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        session = get_session(chat_id)
        response = session.send_message(user_text)
        full_reply = response.text

        # 检查有没有 REMINDER 指令
        reminder_match = re.search(r'REMINDER\|(\d+)\|(.+)', full_reply)
        if reminder_match:
            minutes = int(reminder_match.group(1))
            reminder_text = reminder_match.group(2).strip()
            # 删掉 REMINDER 那行，只显示正常回复
            reply = re.sub(r'\nREMINDER\|.*', '', full_reply).strip()
            # 设定定时器
            run_time = datetime.now() + timedelta(minutes=minutes)
            scheduler.add_job(
                send_reminder,
                'date',
                run_date=run_time,
                args=[chat_id, reminder_text],
            )
            logger.info(f"已设置提醒：{minutes}分钟后 — {reminder_text}")
        else:
            reply = full_reply

    except Exception as e:
        logger.error(f"出错: {e}")
        reply = "⚠️ 出了点问题，请稍后再试。"

    await update.message.reply_text(reply)


# ─── 初始化 PTB ──────────────────────────────────────────────
ptb_app = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .updater(None)
    .build()
)

ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("clear", clear))
ptb_app.add_handler(CommandHandler("reminders", reminders))
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
