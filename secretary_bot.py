"""
Telegram Secretary Bot — 纯同步版本 (Flask + requests)
彻底避免 asyncio 冲突
"""

import os
import logging
import re
from datetime import datetime, timedelta

import requests
from flask import Flask, request as flask_request, Response
import google.generativeai as genai
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
RENDER_URL = os.environ["RENDER_URL"]
TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

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

jobstores = {'default': SQLAlchemyJobStore(url='sqlite:///reminders.db')}
scheduler = BackgroundScheduler(jobstores=jobstores)
scheduler.start()


# ─── Telegram 工具函数（纯同步）────────────────────────────
def tg_send(chat_id: int, text: str):
    requests.post(f"{TG_API}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
    })

def tg_typing(chat_id: int):
    requests.post(f"{TG_API}/sendChatAction", json={
        "chat_id": chat_id,
        "action": "typing",
    })


# ─── 提醒发送函数 ────────────────────────────────────────────
def send_reminder(chat_id: int, message: str):
    tg_send(chat_id, f"⏰ 提醒：{message}")


# ─── 消息处理 ────────────────────────────────────────────────
def get_session(chat_id: int):
    if chat_id not in chat_sessions:
        chat_sessions[chat_id] = model.start_chat(history=[])
    return chat_sessions[chat_id]

def handle_start(chat_id: int):
    tg_send(chat_id,
        "👋 你好，我是你的私人秘书！\n\n"
        "直接发消息给我就行，问什么都可以。\n\n"
        "提醒例子：\n"
        "• 30分钟后提醒我喝水\n"
        "• 1小时后提醒我开会\n\n"
        "指令：\n"
        "/start — 开始\n"
        "/clear — 清除对话记忆\n"
        "/reminders — 查看待办提醒"
    )

def handle_clear(chat_id: int):
    if chat_id in chat_sessions:
        del chat_sessions[chat_id]
    tg_send(chat_id, "✅ 对话记忆已清除，重新开始吧！")

def handle_reminders(chat_id: int):
    jobs = scheduler.get_jobs()
    if not jobs:
        tg_send(chat_id, "📋 目前没有待办提醒。")
        return
    msg = "📋 待办提醒：\n\n"
    for job in jobs:
        run_time = job.next_run_time.strftime("%m-%d %H:%M") if job.next_run_time else "?"
        msg += f"• {run_time} — {job.args[1]}\n"
    tg_send(chat_id, msg)

def handle_text(chat_id: int, text: str):
    tg_typing(chat_id)
    try:
        session = get_session(chat_id)
        response = session.send_message(text)
        full_reply = response.text

        reminder_match = re.search(r'REMINDER\|(\d+)\|(.+)', full_reply)
        if reminder_match:
            minutes = int(reminder_match.group(1))
            reminder_text = reminder_match.group(2).strip()
            reply = re.sub(r'\nREMINDER\|.*', '', full_reply).strip()
            run_time = datetime.now() + timedelta(minutes=minutes)
            scheduler.add_job(
                send_reminder,
                'date',
                run_date=run_time,
                args=[chat_id, reminder_text],
                misfire_grace_time=300,
            )
            logger.info(f"已设置提醒：{minutes}分钟后 — {reminder_text}")
        else:
            reply = full_reply

    except Exception as e:
        logger.error(f"出错: {e}")
        reply = "⚠️ 出了点问题，请稍后再试。"

    tg_send(chat_id, reply)


# ─── Flask ───────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200

@flask_app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    data = flask_request.get_json(force=True)
    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return Response("ok", status=200)

    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")

    if text == "/start":
        handle_start(chat_id)
    elif text == "/clear":
        handle_clear(chat_id)
    elif text == "/reminders":
        handle_reminders(chat_id)
    elif text:
        handle_text(chat_id, text)

    return Response("ok", status=200)

@flask_app.route("/set_webhook", methods=["GET"])
def set_webhook():
    url = f"{RENDER_URL}/webhook/{TELEGRAM_TOKEN}"
    resp = requests.post(f"{TG_API}/setWebhook", json={"url": url})
    return f"Webhook 已设置到: {url} | 结果: {resp.json()}", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
