"""
Telegram Secretary Bot — 纯同步版本 + Google Calendar
"""

import os
import logging
import re
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request as flask_request, Response, redirect
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

# ─── 环境变量 ────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
RENDER_URL       = os.environ["RENDER_URL"]
GOOGLE_CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TIMEZONE = "Asia/Kuala_Lumpur"  # 马来西亚时区
SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_FILE = "calendar_token.json"

SYSTEM_PROMPT = """你是一个私人秘书助理。你的职责是：
- 回答主人提出的任何问题
- 帮助规划、整理、分析信息
- 记住对话上下文，像真人秘书一样自然对话
- 回答简洁实用，不废话
- 默认用中文回复，除非主人用其他语言

你的主人叫 Brandon。

【提醒功能】
如果主人要你提醒他做某件事，在回复末尾加：
REMINDER|<分钟数>|<提醒内容>

例子：
主人说："30分钟后提醒我喝水"
你回复："好的！\nREMINDER|30|喝水"

【日历功能】
如果主人要查看日程，在回复末尾加：
CALENDAR_GET|<日期，格式 YYYY-MM-DD>

如果主人要创建活动，在回复末尾加：
CALENDAR_CREATE|<标题>|<日期 YYYY-MM-DD>|<开始时间 HH:MM>|<结束时间 HH:MM>

例子：
主人说："今天有什么活动？"（假设今天是 2026-06-04）
你回复："让我查一下！\nCALENDAR_GET|2026-06-04"

主人说："明天下午3点加一个跟客户开会，1小时"（假设明天是 2026-06-05）
你回复："好的，已安排！\nCALENDAR_CREATE|跟客户开会|2026-06-05|15:00|16:00"

今天的日期是：{TODAY}

如果主人没有要求提醒或日历操作，正常回答，不要加任何指令行。"""

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)

chat_sessions: dict = {}
# 存储等待 OAuth 授权的 chat_id
pending_auth: dict = {}

jobstores = {'default': SQLAlchemyJobStore(url='sqlite:///reminders.db')}
scheduler = BackgroundScheduler(jobstores=jobstores)
scheduler.start()


# ─── Telegram 工具函数 ───────────────────────────────────────
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


# ─── 提醒 ────────────────────────────────────────────────────
def send_reminder(chat_id: int, message: str):
    tg_send(chat_id, f"⏰ 提醒：{message}")


# ─── Google Calendar ─────────────────────────────────────────
def get_calendar_service():
    """获取 Google Calendar 服务，自动刷新 token"""
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        with open(TOKEN_FILE, "w") as f:
            json.dump({
                "token": creds.token,
                "refresh_token": creds.refresh_token,
            }, f)
    return build("calendar", "v3", credentials=creds)

def calendar_get_events(date_str: str) -> str:
    """查询某天的活动"""
    service = get_calendar_service()
    if not service:
        return "❌ 还没连接 Google Calendar，请先发送 /connect"
    try:
        tz = ZoneInfo(TIMEZONE)
        date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
        start = date.replace(hour=0, minute=0, second=0).isoformat()
        end   = date.replace(hour=23, minute=59, second=59).isoformat()
        result = service.events().list(
            calendarId="primary",
            timeMin=start,
            timeMax=end,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])
        if not events:
            return f"📅 {date_str} 没有活动。"
        msg = f"📅 {date_str} 的活动：\n\n"
        for e in events:
            start_time = e["start"].get("dateTime", e["start"].get("date", ""))
            if "T" in start_time:
                t = datetime.fromisoformat(start_time).strftime("%H:%M")
            else:
                t = "全天"
            msg += f"• {t} — {e.get('summary', '无标题')}\n"
        return msg
    except Exception as ex:
        logger.error(f"Calendar get error: {ex}")
        return "⚠️ 查询日历失败，请稍后再试。"

def calendar_create_event(title: str, date_str: str, start_time: str, end_time: str) -> str:
    """创建活动"""
    service = get_calendar_service()
    if not service:
        return "❌ 还没连接 Google Calendar，请先发送 /connect"
    try:
        tz = TIMEZONE
        event = {
            "summary": title,
            "start": {"dateTime": f"{date_str}T{start_time}:00", "timeZone": tz},
            "end":   {"dateTime": f"{date_str}T{end_time}:00",   "timeZone": tz},
        }
        service.events().insert(calendarId="primary", body=event).execute()
        return f"✅ 已创建活动：{title}（{date_str} {start_time}–{end_time}）"
    except Exception as ex:
        logger.error(f"Calendar create error: {ex}")
        return "⚠️ 创建活动失败，请稍后再试。"


# ─── Gemini 对话 ─────────────────────────────────────────────
def get_session(chat_id: int):
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
    prompt = SYSTEM_PROMPT.replace("{TODAY}", today)
    if chat_id not in chat_sessions:
        m = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=prompt,
        )
        chat_sessions[chat_id] = m.start_chat(history=[])
    return chat_sessions[chat_id]

def handle_text(chat_id: int, text: str):
    tg_typing(chat_id)
    try:
        session = get_session(chat_id)
        response = session.send_message(text)
        full_reply = response.text

        # 处理提醒指令
        reminder_match = re.search(r'REMINDER\|(\d+)\|(.+)', full_reply)
        if reminder_match:
            minutes = int(reminder_match.group(1))
            reminder_text = reminder_match.group(2).strip()
            full_reply = re.sub(r'\nREMINDER\|[^\n]*', '', full_reply).strip()
            run_time = datetime.now() + timedelta(minutes=minutes)
            scheduler.add_job(
                send_reminder, 'date',
                run_date=run_time,
                args=[chat_id, reminder_text],
                misfire_grace_time=300,
            )

        # 处理日历查询指令
        cal_get_match = re.search(r'CALENDAR_GET\|(\d{4}-\d{2}-\d{2})', full_reply)
        if cal_get_match:
            date_str = cal_get_match.group(1)
            full_reply = re.sub(r'\nCALENDAR_GET\|[^\n]*', '', full_reply).strip()
            cal_result = calendar_get_events(date_str)
            tg_send(chat_id, full_reply)
            tg_send(chat_id, cal_result)
            return

        # 处理日历创建指令
        cal_create_match = re.search(
            r'CALENDAR_CREATE\|([^|]+)\|(\d{4}-\d{2}-\d{2})\|(\d{2}:\d{2})\|(\d{2}:\d{2})',
            full_reply
        )
        if cal_create_match:
            title     = cal_create_match.group(1).strip()
            date_str  = cal_create_match.group(2)
            start_t   = cal_create_match.group(3)
            end_t     = cal_create_match.group(4)
            full_reply = re.sub(r'\nCALENDAR_CREATE\|[^\n]*', '', full_reply).strip()
            cal_result = calendar_create_event(title, date_str, start_t, end_t)
            tg_send(chat_id, full_reply)
            tg_send(chat_id, cal_result)
            return

        tg_send(chat_id, full_reply)

    except Exception as e:
        logger.error(f"出错: {e}")
        tg_send(chat_id, "⚠️ 出了点问题，请稍后再试。")


# ─── 指令处理 ────────────────────────────────────────────────
def handle_start(chat_id: int):
    tg_send(chat_id,
        "👋 你好，我是你的私人秘书！\n\n"
        "直接发消息给我就行，问什么都可以。\n\n"
        "📅 日历功能：\n"
        "• 今天有什么活动？\n"
        "• 明天下午3点加一个开会\n\n"
        "⏰ 提醒功能：\n"
        "• 30分钟后提醒我喝水\n\n"
        "指令：\n"
        "/connect — 连接 Google Calendar\n"
        "/clear — 清除对话记忆\n"
        "/reminders — 查看待办提醒"
    )

def handle_clear(chat_id: int):
    if chat_id in chat_sessions:
        del chat_sessions[chat_id]
    tg_send(chat_id, "✅ 对话记忆已清除！")

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

def handle_connect(chat_id: int):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"{RENDER_URL}/oauth/callback"],
            }
        },
        scopes=SCOPES,
        redirect_uri=f"{RENDER_URL}/oauth/callback",
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    pending_auth[state] = chat_id
    tg_send(chat_id,
        f"点击以下链接授权 Google Calendar：\n\n{auth_url}\n\n"
        "授权完成后我就能帮你管理日历了！"
    )


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
    elif text == "/connect":
        handle_connect(chat_id)
    elif text:
        handle_text(chat_id, text)

    return Response("ok", status=200)

@flask_app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    state = flask_request.args.get("state")
    code  = flask_request.args.get("code")
    chat_id = pending_auth.pop(state, None)

    if not chat_id:
        return "授权失败，请重新发送 /connect", 400

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"{RENDER_URL}/oauth/callback"],
            }
        },
        scopes=SCOPES,
        redirect_uri=f"{RENDER_URL}/oauth/callback",
        state=state,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    with open(TOKEN_FILE, "w") as f:
        json.dump({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
        }, f)

    tg_send(chat_id, "✅ Google Calendar 已连接！现在可以问我查日程或加活动了。")
    return "<h2>✅ 授权成功！回到 Telegram 继续使用吧。</h2>", 200

@flask_app.route("/set_webhook", methods=["GET"])
def set_webhook():
    url = f"{RENDER_URL}/webhook/{TELEGRAM_TOKEN}"
    resp = requests.post(f"{TG_API}/setWebhook", json={"url": url})
    return f"Webhook 已设置: {url} | {resp.json()}", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
