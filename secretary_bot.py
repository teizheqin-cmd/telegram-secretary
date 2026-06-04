"""
Telegram Secretary Bot — 纯同步版本 + Google Calendar (持久化 token)
"""

import os
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request as flask_request, Response
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

# ─── 环境变量 ────────────────────────────────────────────────
TELEGRAM_TOKEN       = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY       = os.environ["GEMINI_API_KEY"]
RENDER_URL           = os.environ["RENDER_URL"]
GOOGLE_CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
# 第一次授权后填入，之后永久有效
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

TG_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TIMEZONE = "Asia/Kuala_Lumpur"
SCOPES   = ["https://www.googleapis.com/auth/calendar"]

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
CALENDAR_GET|<日期 YYYY-MM-DD>

如果主人要创建活动，在回复末尾加：
CALENDAR_CREATE|<标题>|<日期 YYYY-MM-DD>|<开始时间 HH:MM>|<结束时间 HH:MM>

今天的日期是：{TODAY}

如果主人没有要求提醒或日历操作，正常回答，不要加任何指令行。"""

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
chat_sessions: dict = {}
pending_auth: dict = {}

jobstores = {'default': SQLAlchemyJobStore(url='sqlite:///reminders.db')}
scheduler = BackgroundScheduler(jobstores=jobstores)
scheduler.start()


# ─── Telegram ────────────────────────────────────────────────
def tg_send(chat_id: int, text: str):
    requests.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text})

def tg_typing(chat_id: int):
    requests.post(f"{TG_API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})


# ─── 提醒 ────────────────────────────────────────────────────
def send_reminder(chat_id: int, message: str):
    tg_send(chat_id, f"⏰ 提醒：{message}")


# ─── Google Calendar ─────────────────────────────────────────
def get_calendar_service():
    refresh_token = GOOGLE_REFRESH_TOKEN
    if not refresh_token:
        return None
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    creds.refresh(GoogleRequest())
    return build("calendar", "v3", credentials=creds)

def calendar_get_events(date_str: str) -> str:
    service = get_calendar_service()
    if not service:
        return "❌ 还没连接 Google Calendar，请先发送 /connect"
    try:
        tz   = ZoneInfo(TIMEZONE)
        date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
        start = date.replace(hour=0, minute=0, second=0).isoformat()
        end   = date.replace(hour=23, minute=59, second=59).isoformat()
        result = service.events().list(
            calendarId="primary", timeMin=start, timeMax=end,
            singleEvents=True, orderBy="startTime",
        ).execute()
        events = result.get("items", [])
        if not events:
            return f"📅 {date_str} 没有活动。"
        msg = f"📅 {date_str} 的活动：\n\n"
        for e in events:
            s = e["start"].get("dateTime", e["start"].get("date", ""))
            t = datetime.fromisoformat(s).strftime("%H:%M") if "T" in s else "全天"
            msg += f"• {t} — {e.get('summary', '无标题')}\n"
        return msg
    except Exception as ex:
        logger.error(f"Calendar get error: {ex}")
        return "⚠️ 查询日历失败，请稍后再试。"

def calendar_create_event(title: str, date_str: str, start_time: str, end_time: str) -> str:
    service = get_calendar_service()
    if not service:
        return "❌ 还没连接 Google Calendar，请先发送 /connect"
    try:
        event = {
            "summary": title,
            "start": {"dateTime": f"{date_str}T{start_time}:00", "timeZone": TIMEZONE},
            "end":   {"dateTime": f"{date_str}T{end_time}:00",   "timeZone": TIMEZONE},
        }
        service.events().insert(calendarId="primary", body=event).execute()
        return f"✅ 已创建：{title}（{date_str} {start_time}–{end_time}）"
    except Exception as ex:
        logger.error(f"Calendar create error: {ex}")
        return "⚠️ 创建活动失败，请稍后再试。"


# ─── Gemini ──────────────────────────────────────────────────
def get_session(chat_id: int):
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
    prompt = SYSTEM_PROMPT.replace("{TODAY}", today)
    if chat_id not in chat_sessions:
        m = genai.GenerativeModel(model_name="gemini-2.5-flash", system_instruction=prompt)
        chat_sessions[chat_id] = m.start_chat(history=[])
    return chat_sessions[chat_id]

def handle_text(chat_id: int, text: str):
    tg_typing(chat_id)
    try:
        session = get_session(chat_id)
        response = session.send_message(text)
        full_reply = response.text

        # 提醒
        m = re.search(r'REMINDER\|(\d+)\|(.+)', full_reply)
        if m:
            minutes = int(m.group(1))
            reminder_text = m.group(2).strip()
            full_reply = re.sub(r'\nREMINDER\|[^\n]*', '', full_reply).strip()
            scheduler.add_job(send_reminder, 'date',
                run_date=datetime.now() + timedelta(minutes=minutes),
                args=[chat_id, reminder_text], misfire_grace_time=300)

        # 查日历
        m = re.search(r'CALENDAR_GET\|(\d{4}-\d{2}-\d{2})', full_reply)
        if m:
            date_str = m.group(1)
            full_reply = re.sub(r'\nCALENDAR_GET\|[^\n]*', '', full_reply).strip()
            tg_send(chat_id, full_reply)
            tg_send(chat_id, calendar_get_events(date_str))
            return

        # 创建活动
        m = re.search(r'CALENDAR_CREATE\|([^|]+)\|(\d{4}-\d{2}-\d{2})\|(\d{2}:\d{2})\|(\d{2}:\d{2})', full_reply)
        if m:
            title, date_str, start_t, end_t = m.group(1).strip(), m.group(2), m.group(3), m.group(4)
            full_reply = re.sub(r'\nCALENDAR_CREATE\|[^\n]*', '', full_reply).strip()
            tg_send(chat_id, full_reply)
            tg_send(chat_id, calendar_create_event(title, date_str, start_t, end_t))
            return

        tg_send(chat_id, full_reply)

    except Exception as e:
        logger.error(f"出错: {e}")
        tg_send(chat_id, "⚠️ 出了点问题，请稍后再试。")


# ─── 指令处理 ────────────────────────────────────────────────
def handle_start(chat_id: int):
    connected = "✅ 已连接" if GOOGLE_REFRESH_TOKEN else "❌ 未连接（发送 /connect）"
    tg_send(chat_id,
        f"👋 你好，我是你的私人秘书！\n\n"
        f"Google Calendar：{connected}\n\n"
        "直接发消息给我就行，例如：\n"
        "• 今天有什么活动？\n"
        "• 明天下午3点加一个开会\n"
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
        {"web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"{RENDER_URL}/oauth/callback"],
        }},
        scopes=SCOPES,
        redirect_uri=f"{RENDER_URL}/oauth/callback",
    )
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    pending_auth[state] = chat_id
    tg_send(chat_id, f"点击以下链接授权 Google Calendar：\n\n{auth_url}")


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
    if   text == "/start":    handle_start(chat_id)
    elif text == "/clear":    handle_clear(chat_id)
    elif text == "/reminders": handle_reminders(chat_id)
    elif text == "/connect":  handle_connect(chat_id)
    elif text:                handle_text(chat_id, text)
    return Response("ok", status=200)

@flask_app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    state   = flask_request.args.get("state")
    code    = flask_request.args.get("code")
    chat_id = pending_auth.pop(state, None)
    if not chat_id:
        return "授权失败，请重新发送 /connect", 400

    flow = Flow.from_client_config(
        {"web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"{RENDER_URL}/oauth/callback"],
        }},
        scopes=SCOPES,
        redirect_uri=f"{RENDER_URL}/oauth/callback",
        state=state,
    )
    flow.fetch_token(code=code)
    refresh_token = flow.credentials.refresh_token
    logger.info(f"=== REFRESH TOKEN: {refresh_token} ===")

    if refresh_token:
        tg_send(chat_id,
            f"✅ 授权成功！\n\n"
            f"请去 Render → Environment 添加：\n\n"
            f"GOOGLE_REFRESH_TOKEN = {refresh_token}\n\n"
            f"保存后永久有效，不需要再重新连接！"
        )
    else:
        # Google 没有返回 refresh token，让用户重新撤销再授权
        tg_send(chat_id,
            "⚠️ 授权成功但未拿到 refresh token。\n\n"
            "请去以下链接撤销授权后重新发送 /connect：\n"
            "https://myaccount.google.com/permissions"
        )
    return "<h2>✅ 授权成功！回到 Telegram 查看下一步。</h2>", 200

@flask_app.route("/set_webhook", methods=["GET"])
def set_webhook():
    url = f"{RENDER_URL}/webhook/{TELEGRAM_TOKEN}"
    resp = requests.post(f"{TG_API}/setWebhook", json={"url": url})
    return f"Webhook 已设置: {url} | {resp.json()}", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
