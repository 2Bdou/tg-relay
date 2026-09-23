#!/usr/bin/env python3
"""
TG 双向匿名中继机器人 v3.2.0
- 防封版：随机延迟 + 双轨限流
- Flask 内嵌 HTTP 健康检查服务器
- Polling / Webhook 双模式自适应
- 多对话支持 + SQLite 持久化
- 全配置环境变量驱动
"""

import os
import sys
import time
import json
import random
import logging
import sqlite3
import threading
from types import SimpleNamespace as _SNS

from flask import Flask, request, jsonify
from waitress import serve
import telebot
from telebot import types

# 自动加载 .env 文件（如果存在）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# ============================================================
# 配置（全部从环境变量读取）
# ============================================================
TOKEN = os.getenv("TG_BOT_TOKEN") or ""
OWNER_ID = int(os.getenv("TG_OWNER_ID") or "0")
PORT = int(os.getenv("TG_PORT", "8080"))
WEBHOOK_BASE = os.getenv("TG_WEBHOOK_URL", "").rstrip("/")
LOG_LEVEL = os.getenv("TG_LOG_LEVEL", "INFO").upper()
WELCOME_OWNER = os.getenv("TG_WELCOME_OWNER", "")
WELCOME_STRANGER = os.getenv("TG_WELCOME_STRANGER", "")
OWNER_CONTACT = os.getenv("TG_OWNER_CONTACT", "")
RATE_LIMIT = int(os.getenv("TG_RATE_LIMIT", "5"))
RATE_WINDOW = int(os.getenv("TG_RATE_WINDOW", "30"))
OWNER_RATE_LIMIT = int(os.getenv("TG_OWNER_RATE_LIMIT", "8"))
MSG_HEADER = os.getenv("TG_MSG_HEADER", "")
MSG_FOOTER = os.getenv("TG_MSG_FOOTER", "")
ADMIN_TOKEN = os.getenv("TG_ADMIN_TOKEN", "")
VERSION = "3.2.0"

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tg-relay")

# ============================================================
# 启动自检
# ============================================================
if not TOKEN or not OWNER_ID:
    logger.error("TG_BOT_TOKEN 或 TG_OWNER_ID 未正确设置")
    sys.exit(1)

_token_masked = TOKEN[:6] + "..." + TOKEN[-4:] if len(TOKEN) > 10 else "***"
logger.info("配置: owner_id=%s, port=%s, log_level=%s, mode=%s",
            OWNER_ID, PORT, LOG_LEVEL, "webhook" if WEBHOOK_BASE else "polling")
logger.info("Token: %s", _token_masked)

START_TIME = time.time()

# ============================================================
# 数据目录和数据库
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("TG_DATA_DIR", SCRIPT_DIR)
DB_FILE = os.path.join(DATA_DIR, "relay.db")

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            stranger_id INTEGER PRIMARY KEY,
            first_name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            note TEXT DEFAULT '',
            last_message_time INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stranger_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            content_type TEXT DEFAULT 'text',
            content TEXT DEFAULT '',
            owner_msg_id INTEGER DEFAULT 0,
            timestamp INTEGER NOT NULL,
            FOREIGN KEY (stranger_id) REFERENCES conversations(stranger_id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_stranger ON messages(stranger_id, timestamp);
    """)
    conn.commit()
    conn.close()

init_db()

# ============================================================
# 速率限制（加强版）
# ============================================================
rate_limit_data = {}      # stranger: user_id -> [timestamps]
owner_rate_data = []      # owner: [timestamps]

def check_rate_limit(user_id):
    if RATE_LIMIT <= 0:
        return True
    now = time.time()
    cutoff = now - RATE_WINDOW
    if user_id not in rate_limit_data:
        rate_limit_data[user_id] = []
    rate_limit_data[user_id] = [t for t in rate_limit_data[user_id] if t > cutoff]
    if len(rate_limit_data[user_id]) >= RATE_LIMIT:
        return False
    rate_limit_data[user_id].append(now)
    return True

def check_owner_rate_limit():
    if OWNER_RATE_LIMIT <= 0:
        return True
    now = time.time()
    cutoff = now - RATE_WINDOW * 2
    global owner_rate_data
    owner_rate_data = [t for t in owner_rate_data if t > cutoff]
    if len(owner_rate_data) >= OWNER_RATE_LIMIT:
        return False
    owner_rate_data.append(now)
    return True

def random_delay(min_sec=0.8, max_sec=3.0):
    time.sleep(random.uniform(min_sec, max_sec))

def block_user(stranger_id):
    conn = get_db()
    conn.execute("UPDATE conversations SET is_blocked = 1 WHERE stranger_id = ?", (stranger_id,))
    conn.commit()
    conn.close()

def unblock_user(stranger_id):
    conn = get_db()
    conn.execute("UPDATE conversations SET is_blocked = 0 WHERE stranger_id = ?", (stranger_id,))
    conn.commit()
    conn.close()

def get_blocked_users():
    conn = get_db()
    rows = conn.execute("SELECT * FROM conversations WHERE is_blocked = 1").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def export_history(stranger_id):
    conv = get_conversation(stranger_id)
    msgs = get_history(stranger_id, limit=1000)
    if not conv:
        return None
    name = conv["first_name"] or "未知"
    username = f" (@{conv['username']})" if conv["username"] else ""
    lines = [f"对话记录: {name}{username} (ID: {stranger_id})", "=" * 40]
    for m in reversed(msgs):
        arrow = "<-" if m["direction"] == "from_stranger" else "->"
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["timestamp"]))
        content = m["content"] or f"[{m['content_type']}]"
        lines.append(f"[{ts}] {arrow} {content}")
    return "\n".join(lines)

# ============================================================
# 对话状态（内存 + DB）
# ============================================================
forwarded_msg_map = {}  # owner_side_msg_id -> stranger_id
active_conversation = None  # 当前活跃的 stranger_id
conversation_lock = threading.Lock()

def upsert_conversation(stranger_id, first_name="", username=""):
    conn = get_db()
    conn.execute("""
        INSERT INTO conversations (stranger_id, first_name, username, last_message_time)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(stranger_id) DO UPDATE SET
            first_name = COALESCE(NULLIF(?, ''), first_name),
            username = COALESCE(NULLIF(?, ''), username),
            last_message_time = ?,
            message_count = message_count + 1
    """, (stranger_id, first_name, username, int(time.time()),
          first_name, username, int(time.time())))
    conn.commit()
    conn.close()

def get_conversations():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM conversations WHERE is_blocked = 0 ORDER BY last_message_time DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_conversation(stranger_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM conversations WHERE stranger_id = ?", (stranger_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def set_note(stranger_id, note):
    conn = get_db()
    conn.execute("UPDATE conversations SET note = ? WHERE stranger_id = ?", (note, stranger_id))
    conn.commit()
    conn.close()

def log_message(stranger_id, direction, content_type, content, owner_msg_id=0):
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (stranger_id, direction, content_type, content, owner_msg_id, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (stranger_id, direction, content_type, content, owner_msg_id, int(time.time()))
    )
    conn.commit()
    conn.close()

def get_history(stranger_id, limit=50, offset=0):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE stranger_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (stranger_id, limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def count_messages(stranger_id):
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE stranger_id = ?", (stranger_id,)
    ).fetchone()[0]
    conn.close()
    return n

def get_stats():
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    today_start = int(time.time()) - (int(time.time()) % 86400)
    today_messages = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE timestamp >= ?", (today_start,)
    ).fetchone()[0]
    conn.close()
    return {
        "total_users": total_users,
        "total_messages": total_messages,
        "today_messages": today_messages,
    }

def get_all_conversations():
    """全量对话列表（含封禁用户），供卡片面板/删除使用"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM conversations ORDER BY last_message_time DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_conversation(stranger_id):
    """彻底删除一个对话对象及其全部消息记录（不可恢复）"""
    global active_conversation
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE stranger_id = ?", (stranger_id,))
    conn.execute("DELETE FROM conversations WHERE stranger_id = ?", (stranger_id,))
    conn.commit()
    conn.close()
    with conversation_lock:
        if active_conversation == stranger_id:
            active_conversation = None
        stale = [k for k, v in forwarded_msg_map.items() if v == stranger_id]
        for k in stale:
            forwarded_msg_map.pop(k, None)

# ============================================================
# 对话卡片面板（/contacts）
# ============================================================
CARD_PER_PAGE = 5

def format_relative_time(ts):
    if not ts:
        return "无"
    diff = int(time.time()) - ts
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{diff // 60} 分钟前"
    if diff < 86400:
        return f"{diff // 3600} 小时前"
    if diff < 86400 * 7:
        return f"{diff // 86400} 天前"
    return time.strftime("%m-%d", time.localtime(ts))

def build_contacts_keyboard(page=0):
    convos = get_all_conversations()
    total_pages = max(1, (len(convos) + CARD_PER_PAGE - 1) // CARD_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * CARD_PER_PAGE
    items = convos[start:start + CARD_PER_PAGE]
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    for c in items:
        sid = c["stranger_id"]
        ban_label = "✅ 解封" if c["is_blocked"] else "🚫 拉黑"
        keyboard.add(
            types.InlineKeyboardButton("💬 对话", callback_data=f"card_switch_{sid}_{page}"),
            types.InlineKeyboardButton("🗑 删除", callback_data=f"card_del_{sid}_{page}"),
            types.InlineKeyboardButton(ban_label, callback_data=f"card_ban_{sid}_{page}"),
        )
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️", callback_data=f"card_page_{page - 1}"))
    nav.append(types.InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"card_none_{page}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("▶️", callback_data=f"card_page_{page + 1}"))
    keyboard.add(*nav)
    keyboard.add(types.InlineKeyboardButton("🏠 返回菜单", callback_data="m|cat|dialog"))
    return keyboard

def render_contacts_page(page=0):
    convos = get_all_conversations()
    total_pages = max(1, (len(convos) + CARD_PER_PAGE - 1) // CARD_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * CARD_PER_PAGE
    items = convos[start:start + CARD_PER_PAGE]
    if not items:
        return "📭 暂无对话对象。\n\n发送 /menu，在「对话」里管理。"
    lines = [f"👥 对话卡片（共 {len(convos)} 人）\n"]
    for c in items:
        name = c["first_name"] or "未知"
        username = f" @{c['username']}" if c["username"] else ""
        note = f"\n🏷 备注：{c['note']}" if c["note"] else ""
        blocked = " 🔒已拉黑" if c["is_blocked"] else ""
        active = " ⬅当前" if c["stranger_id"] == active_conversation else ""
        last = format_relative_time(c["last_message_time"])
        count = c["message_count"]
        lines.append(
            f"👤 {name}{username}{blocked}{active}\n"
            f"🆔 {c['stranger_id']} ｜ ⏱ {last} ｜ 💬 {count}条{note}"
        )
        lines.append("───")
    return "\n".join(lines)

def is_owner(user_id):
    return user_id == OWNER_ID

# ============================================================
# Bot 实例
# ============================================================
bot = telebot.TeleBot(TOKEN, threaded=False)

# ============================================================
# Flask 健康检查服务器
# ============================================================
flask_app = Flask(__name__)

@flask_app.route("/health", methods=["GET"])
def health():
    mode = "webhook" if WEBHOOK_BASE else "polling"
    stats = get_stats()
    convos = get_conversations()
    return jsonify({
        "status": "ok",
        "version": VERSION,
        "mode": mode,
        "uptime": int(time.time() - START_TIME),
        "active_conversations": len(convos),
        "total_users": stats["total_users"],
        "total_messages": stats["total_messages"],
        "today_messages": stats["today_messages"],
    })

if WEBHOOK_BASE:
    @flask_app.route("/webhook", methods=["POST"])
    def webhook():
        if request.headers.get("content-type") == "application/json":
            json_string = request.get_data().decode("utf-8")
            update = types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return ""
        return "Bad Request", 400

@flask_app.route("/admin")
def admin_panel():
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        return "Unauthorized", 403
    stats = get_stats()
    convos = get_conversations()
    uptime_sec = int(time.time() - START_TIME)
    hours = uptime_sec // 3600
    mins = (uptime_sec % 3600) // 60
    convos_html = ""
    for c in convos[:20]:
        name = c["first_name"] or "未知"
        username = f" @{c['username']}" if c["username"] else ""
        note = f" [{c['note']}]" if c["note"] else ""
        convos_html += f"<tr><td>{c['stranger_id']}</td><td>{name}{username}{note}</td><td>{c['message_count']}</td></tr>"
    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>TG Relay Bot v{VERSION}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{font-family:system-ui,sans-serif;max-width:800px;margin:20px auto;padding:0 16px;background:#111;color:#eee}}
.card{{background:#1a1a1a;border-radius:8px;padding:16px;margin:12px 0}}
h1,h2{{color:#4fc3f7}} .stat{{font-size:24px;font-weight:bold;color:#81c784}}
table{{width:100%;border-collapse:collapse}} th,td{{padding:8px;text-align:left;border-bottom:1px solid #333}}
th{{color:#4fc3f7}} .bar{{display:flex;gap:16px;flex-wrap:wrap}}
</style></head><body>
<h1>🤖 TG Relay Bot <small>v{VERSION}</small></h1>
<div class="bar">
<div class="card"><div>运行时间</div><div class="stat">{hours}h {mins}m</div></div>
<div class="card"><div>总用户</div><div class="stat">{stats['total_users']}</div></div>
<div class="card"><div>总消息</div><div class="stat">{stats['total_messages']}</div></div>
<div class="card"><div>今日消息</div><div class="stat">{stats['today_messages']}</div></div>
<div class="card"><div>活跃对话</div><div class="stat">{len(convos)}</div></div>
<div class="card"><div>模式</div><div class="stat">{'Webhook' if WEBHOOK_BASE else 'Polling'}</div></div>
</div>
<h2>活跃对话 TOP 20</h2>
<table><tr><th>ID</th><th>用户</th><th>消息数</th></tr>{convos_html}</table>
<p style="color:#666;margin-top:20px">TG Relay Bot - 消息中继代理</p>
</body></html>"""

# ============================================================
# 链接管理
# ============================================================
LINKS_FILE = os.path.join(DATA_DIR, "links.json")

DEFAULT_LINKS = [
    {"name": "GitHub", "url": "https://github.com", "category": "开发"},
    {"name": "StackOverflow", "url": "https://stackoverflow.com", "category": "开发"},
    {"name": "Python 文档", "url": "https://docs.python.org/zh-cn/", "category": "学习"},
    {"name": "Telegram 开发", "url": "https://core.telegram.org/bots", "category": "开发"},
    {"name": "维基百科", "url": "https://zh.wikipedia.org", "category": "常用"},
]

def load_links():
    if os.path.exists(LINKS_FILE):
        try:
            with open(LINKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.warning("加载 links.json 失败，使用默认链接")
    return DEFAULT_LINKS

def save_links(links):
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(links, f, ensure_ascii=False, indent=2)

def get_links_by_category(category=None):
    links = load_links()
    if category:
        return [link for link in links if link.get("category") == category]
    return links

def add_link(name, url, category="常用"):
    links = load_links()
    if any(link["name"] == name for link in links):
        return False, "链接已存在"
    links.append({"name": name, "url": url, "category": category})
    save_links(links)
    return True, f"✅ 已添加链接：{name}"

def delete_link(name):
    links = load_links()
    original_count = len(links)
    links = [link for link in links if link["name"] != name]
    if len(links) == original_count:
        return False, "链接不存在"
    save_links(links)
    return True, f"✅ 已删除链接：{name}"

def edit_link(old_name, new_name=None, new_url=None, new_category=None):
    links = load_links()
    for link in links:
        if link["name"] == old_name:
            if new_name:
                link["name"] = new_name
            if new_url:
                link["url"] = new_url
            if new_category:
                link["category"] = new_category
            save_links(links)
            return True, f"✅ 已更新链接：{link['name']}"
    return False, "链接不存在"

def find_links(keyword):
    keyword_lower = keyword.lower()
    links = load_links()
    results = []
    for link in links:
        if keyword_lower in link["name"].lower() or keyword_lower in link["url"].lower() or keyword_lower in link.get("category", "").lower():
            results.append(link)
    return results

# ============================================================
# 命令处理
# ============================================================
@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    if is_owner(message.from_user.id):
        if WELCOME_OWNER:
            bot.reply_to(message, WELCOME_OWNER)
        else:
            bot.reply_to(message,
                "👋 你是 owner。陌生人的消息会转发到这里。\n"
                "回复我转发的消息，就会回到那个人。\n"
                "直接发消息，则发给当前对话对象。\n\n"
                "/menu — 打开菜单：切换、回复、备注、记录、导出、封禁、链接都在里面")
    else:
        if WELCOME_STRANGER:
            bot.reply_to(message, WELCOME_STRANGER)
        else:
            bot.reply_to(message,
                "👋 你好！你的消息会匿名转发给 bot 主人。\n"
                "/menu — 查看链接、测延迟、获取自己的 ID")

@bot.message_handler(commands=["ping"])
def handle_ping(message):
    latency_ms = int((time.time() - message.date) * 1000)
    bot.reply_to(message, f"🏓 Pong!\n响应: {latency_ms}ms")

@bot.message_handler(commands=["id"])
def handle_id(message):
    user = message.from_user
    username = f" (@{user.username})" if user.username else ""
    bot.reply_to(message, f"🆔 {user.first_name or '未知'}{username}\n"
                 f"ID: `{user.id}`", parse_mode="Markdown")

@bot.message_handler(commands=["about"])
def handle_about(message):
    uptime_sec = int(time.time() - START_TIME)
    hours = uptime_sec // 3600
    mins = (uptime_sec % 3600) // 60
    secs = uptime_sec % 60
    mode = "Webhook" if WEBHOOK_BASE else "Polling"
    contact = OWNER_CONTACT or "(未设置)"
    convos = get_conversations()
    bot.reply_to(message,
        f"🤖 TG 中继机器人 v{VERSION}\n"
        f"模式: {mode}\n"
        f"运行时间: {hours}h {mins}m {secs}s\n"
        f"活跃对话: {len(convos)}\n"
        f"联系方式: {contact}")

@bot.message_handler(commands=["stats"])
def handle_stats(message):
    if not is_owner(message.from_user.id):
        return
    stats = get_stats()
    convos = get_conversations()
    top_users = sorted(convos, key=lambda c: c["message_count"], reverse=True)[:5]
    top_text = ""
    for i, c in enumerate(top_users, 1):
        name = c["first_name"] or "未知"
        top_text += f"  {i}. {name} ({c['message_count']}条)\n"
    bot.reply_to(message,
        f"📊 统计面板\n\n"
        f"总用户: {stats['total_users']}\n"
        f"总消息: {stats['total_messages']}\n"
        f"今日消息: {stats['today_messages']}\n"
        f"活跃对话: {len(convos)}\n"
        f"速率限制: {'关闭' if RATE_LIMIT <= 0 else f'{RATE_LIMIT}条/{RATE_WINDOW}s'}\n"
        f"\nTop 5 活跃用户:\n{top_text or '  (暂无)'}")

@bot.message_handler(commands=["who"])
def handle_who(message):
    if not is_owner(message.from_user.id):
        return
    global active_conversation
    if active_conversation:
        conv = get_conversation(active_conversation)
        if conv:
            name = conv["first_name"] or "未知"
            username = f" (@{conv['username']})" if conv["username"] else ""
            note = f"\n备注: {conv['note']}" if conv["note"] else ""
            count = conv["message_count"]
            bot.reply_to(message,
                f"当前对话: {name}{username}\n"
                f"ID: {active_conversation}\n"
                f"消息数: {count}{note}")
            return
    bot.reply_to(message, "当前没有活跃对话。陌生人发消息会自动设为活跃。\n"
                 "使用 /queue 查看所有对话，/chat <序号> 切换。")

@bot.message_handler(commands=["queue"])
def handle_queue(message):
    if not is_owner(message.from_user.id):
        return
    convos = get_conversations()
    if not convos:
        bot.reply_to(message, "📭 当前没有待回复的对话。")
        return
    result = f"📋 待回复队列（共 {len(convos)} 人）\n\n"
    for i, c in enumerate(convos, 1):
        name = c["first_name"] or "未知"
        username = f" (@{c['username']})" if c["username"] else ""
        note = f" 📝{c['note']}" if c["note"] else ""
        active = " ⬅ 当前" if c["stranger_id"] == active_conversation else ""
        count = c["message_count"]
        result += f"{i}. {name}{username} [{count}条]{note}{active}\n"
    result += "\n回复此消息或使用 /chat <序号> 切换对话对象"
    bot.reply_to(message, result)

@bot.message_handler(commands=["chat"])
def handle_chat(message):
    if not is_owner(message.from_user.id):
        return
    global active_conversation
    if len(message.text.split()) < 2:
        convos = get_conversations()
        if not convos:
            bot.reply_to(message, "📭 没有对话可切换。")
            return
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        for i, c in enumerate(convos[:10], 1):
            name = c["first_name"] or "未知"
            username = f" @{c['username']}" if c["username"] else ""
            label = f"{i}. {name}{username}"[:40]
            buttons.append(types.InlineKeyboardButton(
                label, callback_data=f"chat_{c['stranger_id']}"))
        keyboard.add(*buttons)
        bot.reply_to(message, "选择要切换的对话对象：", reply_markup=keyboard)
        return
    target = message.text.split(" ", 1)[1].strip()
    if target.isdigit():
        convos = get_conversations()
        index = int(target) - 1
        if 0 <= index < len(convos):
            active_conversation = convos[index]["stranger_id"]
            name = convos[index]["first_name"] or "未知"
            bot.reply_to(message, f"✅ 已切换到: {name} (ID: {active_conversation})")
        else:
            bot.reply_to(message, f"❌ 序号 {target} 超出范围（共 {len(convos)} 人）")
    else:
        try:
            sid = int(target)
        except ValueError:
            bot.reply_to(message, "❌ 请输入有效序号或用户 ID")
            return
        conv = get_conversation(sid)
        if conv:
            active_conversation = sid
            bot.reply_to(message, f"✅ 已切换到: {conv['first_name'] or '未知'} (ID: {sid})")
        else:
            bot.reply_to(message, f"❌ 未找到用户 ID: {sid}")

@bot.message_handler(commands=["contacts"])
def handle_contacts(message):
    if not is_owner(message.from_user.id):
        return
    page = 0
    parts = message.text.split()
    if len(parts) > 1 and parts[1].isdigit():
        page = max(0, int(parts[1]) - 1)
    bot.reply_to(message, render_contacts_page(page),
                 reply_markup=build_contacts_keyboard(page))

# ============================================================
# 命令菜单（/menu）
# ============================================================
# 屏幕回调统一用 m| 开头，竖线分隔，避免类别名里的下划线把参数拆坏。
# 只有备注、消息正文、链接名称/网址/自定义类别、搜索词需要打字，
# 每一步都有「取消」。除了「添加链接时选类别」，任何菜单点击都会先清掉未完成输入。

pending_input = {}
last_link_query = {}
PENDING_TIMEOUT = 300
PEOPLE_PER_PAGE = 5

OWNER_ONLY_CATS = {"dialog", "ban"}
OWNER_ONLY_ITEMS = {"people", "queue", "who", "banned", "add", "manage", "stats"}

MENU_CATS = [
    ("dialog", "💬 对话", [
        ("people", "📇 对话列表"),
        ("queue", "📋 待回复队列"),
        ("who", "👤 当前对象"),
    ]),
    ("ban", "🚫 封禁", [
        ("banned", "📃 封禁列表"),
    ]),
    ("link", "🔗 链接", [
        ("browse", "🔗 浏览全部"),
        ("cats", "📁 按类别"),
        ("find", "🔍 搜索"),
        ("add", "➕ 添加链接"),
        ("manage", "✏️ 管理链接"),
    ]),
    ("sys", "📊 系统", [
        ("stats", "📊 统计面板"),
        ("about", "🤖 关于"),
        ("ping", "🏓 延迟测试"),
        ("id", "🆔 我的ID"),
        ("help", "❓ 帮助"),
    ]),
]

CMD_CB = {
    "people": "m|people|0",
    "queue": "m|queue",
    "who": "m|who",
    "banned": "m|banned|0",
    "browse": "m|links|-1|0",
    "cats": "m|links|-2|0",
    "find": "m|find",
    "add": "m|add",
    "manage": "m|lmgr|0",
    "stats": "m|exec|stats",
    "about": "m|exec|about",
    "ping": "m|exec|ping",
    "id": "m|exec|id",
    "help": "m|exec|help",
}

MENU_CAT_HELP = {
    "dialog": "点开一个人，就可以切换当前对话、发消息、改备注、看记录、导出、封禁或删除。",
    "ban": "这里只列出已封禁的人。点进去可以解封；要封禁，从对话列表进入对方的操作面板。",
    "link": "浏览、按类别查看和搜索都可以直接点。添加和修改是分步完成的，不用再记分号格式。",
    "sys": "统计、关于、延迟、你的 ID 和帮助。",
}


def set_pending(user_id, flow, **fields):
    pending_input[user_id] = {"flow": flow, "ts": time.time(), **fields}


def clear_pending(user_id):
    pending_input.pop(user_id, None)


def cleanup_pending():
    now = time.time()
    stale = [uid for uid, item in pending_input.items() if now - item.get("ts", 0) > PENDING_TIMEOUT]
    for uid in stale:
        pending_input.pop(uid, None)


def _clean(value, limit=40):
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _valid_url(url):
    return (
        isinstance(url, str)
        and url.startswith(("http://", "https://"))
        and " " not in url
        and len(url) <= 2000
    )


def _arg_int(parts, index, default=None):
    try:
        return int(parts[index])
    except (IndexError, ValueError):
        return default


def _page_slice(items, page, per_page):
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    page = max(0, min(int(page or 0), total_pages - 1))
    start = page * per_page
    return items[start:start + per_page], page, total_pages, start


def btn(text, data=None, url=None):
    label = _clean(text, 60) or "…"
    if url:
        return types.InlineKeyboardButton(label, url=url)
    return types.InlineKeyboardButton(label, callback_data=data)


def safe_url_button(text, url):
    if _valid_url(url):
        return btn(text, url=url)
    return None


def kb_rows(rows):
    keyboard = types.InlineKeyboardMarkup()
    for row in rows:
        buttons = [item for item in row if item is not None]
        if buttons:
            keyboard.row(*buttons)
    return keyboard


def nav_row(page, total_pages, cb_for_page):
    row = []
    if page > 0:
        row.append(btn("◀️", cb_for_page(page - 1)))
    row.append(btn(f"{page + 1}/{total_pages}", "m|noop"))
    if page < total_pages - 1:
        row.append(btn("▶️", cb_for_page(page + 1)))
    return row


def back_home(back_data):
    return [btn("◀️ 返回", back_data), btn("🏠 主菜单", "m|home")]


def link_categories():
    return sorted({(link.get("category") or "常用") for link in load_links()})


def _person_label(conv):
    name = _clean(conv.get("first_name") or "未知", 14) or "未知"
    if conv.get("is_blocked"):
        name += " 🔒"
    if conv.get("stranger_id") == active_conversation:
        name += " ⬅"
    return name


def _conv_name(conv):
    if not conv:
        return "未知"
    return _clean(conv.get("first_name") or "未知", 40) or "未知"


def _menu_public(screen, parts):
    if screen in ("home", "noop", "links", "find", "sres"):
        return True
    if screen == "cat" and len(parts) > 2 and parts[2] in ("link", "sys"):
        return True
    if screen == "exec" and len(parts) > 2 and parts[2] in ("ping", "id", "about", "help"):
        return True
    return False


def show_screen(chat_id, msg_id, text, keyboard):
    if not text:
        text = "…"
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    if not chat_id:
        return
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=keyboard)
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
            logger.warning("刷新菜单失败: %s", e)
    try:
        bot.send_message(chat_id, text, reply_markup=keyboard)
    except Exception as e:
        logger.warning("发送菜单失败: %s", e)


def build_menu_keyboard(cat=None, is_owner_user=True):
    if cat is None:
        rows = []
        for key, label, _cmds in MENU_CATS:
            if not is_owner_user and key in OWNER_ONLY_CATS:
                continue
            rows.append([btn(label, f"m|cat|{key}")])
        return kb_rows(rows)
    rows = []
    for key, _label, cmds in MENU_CATS:
        if key != cat:
            continue
        for cmd, cmd_label in cmds:
            if not is_owner_user and cmd in OWNER_ONLY_ITEMS:
                continue
            rows.append([btn(cmd_label, CMD_CB[cmd])])
    rows.append([btn("◀️ 返回", "m|home")])
    return kb_rows(rows)


def render_menu(cat=None):
    if cat is None:
        return (
            "🤖 TG Relay 菜单\n\n"
            "所有功能都从这里完成：先选分类，再点操作。\n"
            "需要填写的内容（备注、消息、链接、搜索词）直接发送下一条文字，"
            "每一步都可以点「取消」。"
        )
    for key, label, cmds in MENU_CATS:
        if key == cat:
            lines = [label, "", MENU_CAT_HELP.get(key, ""), ""]
            for _cmd, cmd_label in cmds:
                lines.append(f"• {cmd_label}")
            return "\n".join(lines)
    return render_menu(None)


def render_stats_text():
    stats = get_stats()
    convos = get_conversations()
    top_users = sorted(convos, key=lambda c: c["message_count"], reverse=True)[:5]
    top_text = ""
    for i, c in enumerate(top_users, 1):
        top_text += f"  {i}. {_clean(c['first_name'] or '未知', 24)} ({c['message_count']}条)\n"
    return (
        f"📊 统计面板\n\n"
        f"总用户: {stats['total_users']}\n"
        f"总消息: {stats['total_messages']}\n"
        f"今日消息: {stats['today_messages']}\n"
        f"活跃对话: {len(convos)}\n"
        f"速率限制: {'关闭' if RATE_LIMIT <= 0 else f'{RATE_LIMIT}条/{RATE_WINDOW}s'}\n"
        f"\nTop 5 活跃用户:\n{top_text or '  (暂无)'}"
    )


def render_about_text():
    uptime_sec = int(time.time() - START_TIME)
    hours = uptime_sec // 3600
    mins = (uptime_sec % 3600) // 60
    secs = uptime_sec % 60
    mode = "Webhook" if WEBHOOK_BASE else "Polling"
    contact = OWNER_CONTACT or "(未设置)"
    return (
        f"🤖 TG 中继机器人 v{VERSION}\n"
        f"模式: {mode}\n"
        f"运行时间: {hours}h {mins}m {secs}s\n"
        f"活跃对话: {len(get_conversations())}\n"
        f"联系方式: {contact}"
    )


def render_ping_text():
    uptime_sec = int(time.time() - START_TIME)
    return f"🏓 Pong! Bot 在线\n已运行 {uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m"


def render_id_text(user):
    username = f" (@{user.username})" if getattr(user, "username", None) else ""
    name = getattr(user, "first_name", None) or "未知"
    return f"🆔 {name}{username}\nID: {user.id}"


def render_help_text(owner):
    if owner:
        return (
            "❓ 帮助\n\n"
            "只用 /menu 就能完成全部操作：\n"
            "• 对话 — 点开某人：切换、发消息、备注、记录、导出、封禁、删除\n"
            "• 封禁 — 查看已封禁的人并解封\n"
            "• 链接 — 浏览、搜索、分步添加、修改、删除\n"
            "• 系统 — 统计、关于、延迟、ID\n\n"
            "回复我转发来的消息，会直接回给那个人。\n"
            "没有在回复某条转发时，你发出的文字、图片、文件和语音会发给「当前对象」。\n"
            "菜单正在等你输入时，下一条文字只用于当前步骤。点「取消」可退出。"
        )
    return (
        "❓ 帮助\n\n"
        "直接发文字、图片或文件即可匿名转达。\n"
        "/menu 可以查看链接、测延迟、看自己的 ID。"
    )


def sys_back_keyboard():
    return kb_rows([back_home("m|cat|sys")])


def render_people_page(page, only_blocked=False):
    convos = get_all_conversations()
    if only_blocked:
        convos = [c for c in convos if c.get("is_blocked")]
    items, page, total_pages, _start = _page_slice(convos, page, PEOPLE_PER_PAGE)
    if only_blocked:
        title = f"🚫 封禁列表（共 {len(convos)} 人）"
        empty = "📭 没有被封禁的用户。\n\n打开「对话列表」，点进某个人就可以封禁。"
    else:
        title = f"📇 对话列表（共 {len(convos)} 人）"
        empty = "📭 暂无对话对象。\n\n陌生人发来消息后会出现在这里。"
    if not convos:
        return empty, page, total_pages, []
    lines = [title, "点名字进入操作面板：切换、发消息、备注、记录、导出、封禁、删除。", ""]
    for conv in items:
        name = _clean(conv.get("first_name") or "未知", 24) or "未知"
        username = f" @{_clean(conv.get('username'), 24)}" if conv.get("username") else ""
        note = f"\n🏷 {_clean(conv.get('note'), 40)}" if conv.get("note") else ""
        flags = ""
        if conv.get("is_blocked"):
            flags += " 🔒已封禁"
        if conv["stranger_id"] == active_conversation:
            flags += " ⬅当前"
        last = format_relative_time(conv["last_message_time"])
        lines.append(
            f"👤 {name}{username}{flags}\n"
            f"🆔 {conv['stranger_id']} ｜ ⏱ {last} ｜ 💬 {conv['message_count']}条{note}"
        )
        lines.append("───")
    return "\n".join(lines), page, total_pages, items


def build_people_keyboard(items, page, total_pages, prefix):
    rows = [[btn(_person_label(conv), f"m|user|{conv['stranger_id']}|{page}")] for conv in items]
    rows.append(nav_row(page, total_pages, lambda p, prefix=prefix: f"{prefix}|{p}"))
    back = "m|cat|ban" if prefix == "m|banned" else "m|cat|dialog"
    rows.append(back_home(back))
    return kb_rows(rows)


def show_people(chat_id, msg_id, page, only_blocked=False):
    text, page, total_pages, items = render_people_page(page, only_blocked)
    prefix = "m|banned" if only_blocked else "m|people"
    show_screen(chat_id, msg_id, text, build_people_keyboard(items, page, total_pages, prefix))


def render_user_panel(sid):
    conv = get_conversation(sid)
    if not conv:
        return None
    name = _conv_name(conv)
    username = f" @{_clean(conv.get('username'), 32)}" if conv.get("username") else ""
    note = f"\n🏷 备注：{_clean(conv.get('note'), 80)}" if conv.get("note") else "\n🏷 备注：无"
    state = "\n🔒 状态：已封禁" if conv.get("is_blocked") else "\n🟢 状态：正常"
    active = "\n⬅ 这是当前对话对象" if sid == active_conversation else ""
    last = format_relative_time(conv.get("last_message_time"))
    return (
        f"👤 {name}{username}\n"
        f"🆔 {sid}\n"
        f"⏱ 最近：{last} ｜ 💬 {conv['message_count']} 条"
        f"{note}{state}{active}\n\n"
        "选择操作："
    )


def user_panel_keyboard(sid, page, blocked):
    ban_label = "✅ 解封" if blocked else "🚫 封禁"
    ban_act = "unban" if blocked else "ban"
    return kb_rows([
        [btn("💬 设为当前", f"m|do|chat|{sid}|{page}"), btn("✉️ 发消息", f"m|do|send|{sid}|{page}")],
        [btn("🏷 备注", f"m|do|note|{sid}|{page}"), btn("🧹 清除备注", f"m|do|nclear|{sid}|{page}")],
        [btn("📜 记录", f"m|hist|{sid}|0|{page}"), btn("📤 导出", f"m|do|exp|{sid}|{page}")],
        [btn(ban_label, f"m|do|{ban_act}|{sid}|{page}"), btn("🗑 删除", f"m|do|del|{sid}|{page}")],
        back_home(f"m|people|{page}"),
    ])


def show_user(chat_id, msg_id, sid, page, banner=""):
    panel = render_user_panel(sid)
    if not panel:
        show_screen(chat_id, msg_id, "这个对话已经不存在。", kb_rows([
            [btn("📇 对话列表", f"m|people|{page}")],
            back_home("m|cat|dialog"),
        ]))
        return
    conv = get_conversation(sid)
    text = f"{banner}\n\n{panel}" if banner else panel
    show_screen(
        chat_id, msg_id, text,
        user_panel_keyboard(sid, page, bool(conv and conv.get("is_blocked"))),
    )


def render_queue_screen():
    convos = get_conversations()
    if not convos:
        return "📭 当前没有待回复的对话。", []
    lines = [f"📋 待回复队列（共 {len(convos)} 人）", "点名字打开操作面板。", ""]
    show = convos[:8]
    for i, conv in enumerate(show, 1):
        name = _conv_name(conv)
        username = f" (@{_clean(conv.get('username'), 24)})" if conv.get("username") else ""
        note = f" 🏷{_clean(conv.get('note'), 20)}" if conv.get("note") else ""
        active = " ⬅当前" if conv["stranger_id"] == active_conversation else ""
        lines.append(f"{i}. {name}{username} [{conv['message_count']}条]{note}{active}")
    if len(convos) > len(show):
        lines.append(f"\n还有 {len(convos) - len(show)} 人，请用对话列表翻页。")
    return "\n".join(lines), show


def render_history_screen(sid, hpage, retpage):
    conv = get_conversation(sid)
    if not conv:
        return None
    name = _conv_name(conv)
    total = count_messages(sid)
    per = 8
    total_pages = max(1, (total + per - 1) // per) if total else 1
    hpage = max(0, min(int(hpage or 0), total_pages - 1))
    msgs = get_history(sid, limit=per, offset=hpage * per) if total else []
    if not msgs:
        text = f"📜 {name} (ID: {sid})\n📭 暂无消息记录。"
    else:
        lines = [f"📜 {name} (ID: {sid}) 第 {hpage + 1}/{total_pages} 页", ""]
        for item in reversed(msgs):
            arrow = "⬅" if item["direction"] == "from_stranger" else "➡"
            ts = time.strftime("%m-%d %H:%M", time.localtime(item["timestamp"]))
            content = _clean(item.get("content") or "", 80)
            if not content:
                content = f"[{item.get('content_type') or 'message'}]"
            lines.append(f"{arrow} [{ts}] {content}")
        text = "\n".join(lines)
    rows = [nav_row(
        hpage, total_pages,
        lambda p, sid=sid, retpage=retpage: f"m|hist|{sid}|{p}|{retpage}",
    )]
    rows.append(back_home(f"m|user|{sid}|{retpage}"))
    return text, kb_rows(rows)


def render_links_screen(cat_idx, page):
    links = load_links()
    cats = link_categories()
    title = "🔗 全部链接"
    if cat_idx >= 0:
        if cat_idx >= len(cats):
            return "这个类别已经不存在。", kb_rows([back_home("m|cat|link")])
        cat = cats[cat_idx]
        links = [link for link in links if (link.get("category") or "常用") == cat]
        title = f"📁 {cat}"
    items, page, total_pages, start = _page_slice(links, page, 6)
    if not links:
        text = f"{title}\n\n还没有链接。"
    else:
        text = f"{title}（共 {len(links)} 条）\n点按钮打开。"
    rows = []
    for i, link in enumerate(items, start + 1):
        button = safe_url_button(f"{i}. {link.get('name') or '链接'}", link.get("url") or "")
        if button:
            rows.append([button])
        else:
            rows.append([btn(f"{i}. {_clean(link.get('name') or '链接', 24)}（链接无效）", "m|noop")])
    rows.append(nav_row(page, total_pages, lambda p, cat_idx=cat_idx: f"m|links|{cat_idx}|{p}"))
    back = "m|links|-2|0" if cat_idx >= 0 else "m|cat|link"
    rows.append(back_home(back))
    return text, kb_rows(rows)


def render_cat_picker(page):
    cats = link_categories()
    items, page, total_pages, start = _page_slice(cats, page, 8)
    rows = [[btn(f"📁 {cat}", f"m|links|{start + i}|0")] for i, cat in enumerate(items)]
    text = f"📁 选择类别（共 {len(cats)} 个）" if cats else "还没有类别。先添加一条链接。"
    rows.append(nav_row(page, total_pages, lambda p: f"m|links|-2|{p}"))
    rows.append(back_home("m|cat|link"))
    return text, kb_rows(rows)


def render_link_manager(page):
    links = load_links()
    items, page, total_pages, start = _page_slice(links, page, 8)
    rows = []
    for i, link in enumerate(items):
        idx = start + i
        rows.append([btn(f"{idx + 1}. {link.get('name') or '链接'}", f"m|link|{idx}|{page}")])
    text = "✏️ 管理链接\n点一条进行修改或删除。" if links else "还没有链接。可以先添加。"
    rows.append(nav_row(page, total_pages, lambda p: f"m|lmgr|{p}"))
    rows.append([btn("➕ 添加", "m|add"), btn("◀️ 返回", "m|cat|link")])
    rows.append([btn("🏠 主菜单", "m|home")])
    return text, kb_rows(rows)


def render_link_detail(idx, page):
    links = load_links()
    if idx < 0 or idx >= len(links):
        return None
    link = links[idx]
    text = (
        f"🔗 {_clean(link.get('name') or '链接', 80)}\n"
        f"{link.get('url') or ''}\n"
        f"类别：{_clean(link.get('category') or '常用', 40)}\n\n"
        "要修改哪一项？"
    )
    rows = []
    open_btn = safe_url_button("🌐 打开链接", link.get("url") or "")
    if open_btn:
        rows.append([open_btn])
    rows.append([
        btn("✏️ 改名称", f"m|ledit|{idx}|n|{page}"),
        btn("✏️ 改网址", f"m|ledit|{idx}|u|{page}"),
    ])
    rows.append([
        btn("📁 改类别", f"m|ledit|{idx}|c|{page}"),
        btn("🗑 删除", f"m|ldel|{idx}|{page}"),
    ])
    rows.append(back_home(f"m|lmgr|{page}"))
    return text, kb_rows(rows)


def render_link_cat_edit(idx, page):
    cats = link_categories()
    rows = []
    row = []
    for i, cat in enumerate(cats):
        row.append(btn(cat, f"m|setcat|{idx}|{i}|{page}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("✏️ 自定义类别", f"m|ledit|{idx}|x|{page}")])
    rows.append([btn("◀️ 返回", f"m|link|{idx}|{page}")])
    return "选择新类别，或自己输入一个：", kb_rows(rows)


def render_search(user_id, page):
    keyword = last_link_query.get(user_id, "")
    back = kb_rows([[btn("🔍 搜索", "m|find")], back_home("m|cat|link")])
    if not keyword:
        return "请重新搜索。", back
    results = find_links(keyword)
    items, page, total_pages, start = _page_slice(results, page, 6)
    shown = _clean(keyword, 40)
    text = f"🔍 「{shown}」共 {len(results)} 条\n点按钮打开。" if results else f"🔍 没有找到「{shown}」。"
    rows = []
    for i, link in enumerate(items, start + 1):
        button = safe_url_button(f"{i}. {link.get('name') or '链接'}", link.get("url") or "")
        rows.append([button or btn(f"{i}. {_clean(link.get('name') or '链接', 24)}", "m|noop")])
    rows.append(nav_row(page, total_pages, lambda p: f"m|sres|{p}"))
    rows.append([btn("🔍 再搜", "m|find")])
    rows.append(back_home("m|cat|link"))
    return text, kb_rows(rows)


def linkadd_category_keyboard():
    cats = link_categories()
    rows = []
    row = []
    for i, cat in enumerate(cats):
        row.append(btn(cat, f"m|addcat|{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if "常用" not in cats:
        rows.append([btn("常用（默认）", "m|addcat|-1")])
    rows.append([btn("✏️ 自定义类别", "m|addcat|-2")])
    rows.append([btn("❌ 取消", "m|cat|link")])
    return kb_rows(rows)


def cancel_kb(back_data):
    return kb_rows([[btn("❌ 取消", back_data), btn("🏠 主菜单", "m|home")]])


def note_prompt_kb(sid, page):
    return kb_rows([
        [btn("🧹 清除备注", f"m|do|nclear|{sid}|{page}")],
        [btn("❌ 取消", f"m|user|{sid}|{page}"), btn("🏠 主菜单", "m|home")],
    ])


def finish_linkadd(user_id, pending, category, message=None):
    draft = pending.get("draft") or {}
    name = (draft.get("name") or "").strip()
    url = (draft.get("url") or "").strip()
    chat_id = pending.get("chat_id")
    msg_id = pending.get("msg_id")
    clear_pending(user_id)
    keyboard = build_menu_keyboard("link", True)
    if not name or not url:
        show_screen(chat_id, msg_id, "添加已失效，请从菜单重新开始。", keyboard)
        return "添加已失效，请从菜单重新开始。"
    _ok, msg = add_link(name, url, category or "常用")
    show_screen(chat_id, msg_id, msg, keyboard)
    if message is not None:
        bot.reply_to(message, msg)
    return msg


def _send_chunks(chat_id, text):
    step = 3500
    for i in range(0, len(text), step):
        bot.send_message(chat_id, text[i:i + step])


def _show_home(chat_id, msg_id, owner):
    show_screen(chat_id, msg_id, render_menu(None), build_menu_keyboard(None, owner))


def _show_cat(chat_id, msg_id, cat, owner):
    known = {key for key, _label, _cmds in MENU_CATS}
    if cat not in known or (not owner and cat in OWNER_ONLY_CATS):
        _show_home(chat_id, msg_id, owner)
        return
    show_screen(chat_id, msg_id, render_menu(cat), build_menu_keyboard(cat, owner))


def _ack(call, text=None):
    try:
        if text:
            bot.answer_callback_query(call.id, text)
        else:
            bot.answer_callback_query(call.id)
    except Exception as e:
        logger.debug("应答菜单回调失败: %s", e)


@bot.message_handler(commands=["menu"])
def handle_menu(message):
    clear_pending(message.from_user.id)
    owner = is_owner(message.from_user.id)
    bot.reply_to(
        message, render_menu(),
        reply_markup=build_menu_keyboard(is_owner_user=owner),
    )


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("m|"))
def callback_menu(call):
    try:
        _menu_dispatch(call)
    except Exception as e:
        logger.exception("菜单处理失败: %s", getattr(call, "data", ""))
        try:
            bot.answer_callback_query(call.id, "操作失败，请重试")
        except Exception:
            pass
        logger.warning("菜单异常: %s", e)


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith(("menu_", "pick_")))
def callback_legacy_menu(call):
    owner = is_owner(call.from_user.id) if call.from_user else False
    _ack(call, "菜单已更新")
    if not call.message:
        return
    show_screen(
        call.message.chat.id,
        call.message.message_id,
        "菜单已更新。请用下面的按钮继续，旧按钮不再使用。\n重新发送 /menu 也可以。",
        build_menu_keyboard(is_owner_user=owner),
    )


def _menu_dispatch(call):
    global active_conversation
    if not call.message:
        _ack(call)
        return
    parts = (call.data or "").split("|")
    if len(parts) < 2 or parts[0] != "m":
        _ack(call)
        return
    screen = parts[1]
    user_id = call.from_user.id
    owner = is_owner(user_id)
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if screen != "addcat":
        clear_pending(user_id)
    if not owner and not _menu_public(screen, parts):
        _ack(call, "❌ 仅限 owner 使用")
        return

    if screen == "noop":
        _ack(call)
        return
    if screen == "home":
        _show_home(chat_id, msg_id, owner)
        _ack(call)
        return
    if screen == "cat":
        _show_cat(chat_id, msg_id, parts[2] if len(parts) > 2 else "", owner)
        _ack(call)
        return
    if screen == "people":
        show_people(chat_id, msg_id, _arg_int(parts, 2, 0) or 0)
        _ack(call)
        return
    if screen == "banned":
        show_people(chat_id, msg_id, _arg_int(parts, 2, 0) or 0, only_blocked=True)
        _ack(call)
        return
    if screen == "user":
        sid = _arg_int(parts, 2)
        page = _arg_int(parts, 3, 0) or 0
        if sid is None:
            _ack(call, "参数错误")
            return
        show_user(chat_id, msg_id, sid, page)
        _ack(call)
        return
    if screen == "queue":
        text, show = render_queue_screen()
        rows = [[btn(_person_label(conv), f"m|user|{conv['stranger_id']}|0")] for conv in show]
        rows.append([btn("📇 全部对话", "m|people|0")])
        rows.append(back_home("m|cat|dialog"))
        show_screen(chat_id, msg_id, text, kb_rows(rows))
        _ack(call)
        return
    if screen == "who":
        if active_conversation and get_conversation(active_conversation):
            show_user(chat_id, msg_id, active_conversation, 0)
        else:
            show_screen(
                chat_id, msg_id,
                "当前没有活跃对话。\n陌生人发来消息后会自动成为当前对象，也可以从对话列表指定。",
                kb_rows([[btn("📇 对话列表", "m|people|0")], back_home("m|cat|dialog")]),
            )
        _ack(call)
        return
    if screen == "hist":
        sid = _arg_int(parts, 2)
        hpage = _arg_int(parts, 3, 0) or 0
        retpage = _arg_int(parts, 4, 0) or 0
        if sid is None:
            _ack(call, "参数错误")
            return
        rendered = render_history_screen(sid, hpage, retpage)
        if not rendered:
            show_user(chat_id, msg_id, sid, retpage)
            _ack(call, "用户不存在")
            return
        show_screen(chat_id, msg_id, rendered[0], rendered[1])
        _ack(call)
        return
    if screen == "links":
        cat_idx = _arg_int(parts, 2, -1)
        page = _arg_int(parts, 3, 0) or 0
        if cat_idx is None:
            cat_idx = -1
        if cat_idx == -2:
            text, keyboard = render_cat_picker(page)
        else:
            text, keyboard = render_links_screen(cat_idx, page)
        show_screen(chat_id, msg_id, text, keyboard)
        _ack(call)
        return
    if screen == "sres":
        text, keyboard = render_search(user_id, _arg_int(parts, 2, 0) or 0)
        show_screen(chat_id, msg_id, text, keyboard)
        _ack(call)
        return
    if screen == "find":
        set_pending(user_id, "linkfind", chat_id=chat_id, msg_id=msg_id)
        show_screen(chat_id, msg_id, "🔍 搜索链接\n\n请直接发送关键词。", cancel_kb("m|cat|link"))
        _ack(call, "请发送关键词")
        return
    if screen == "add":
        set_pending(user_id, "linkadd", step="name", draft={}, chat_id=chat_id, msg_id=msg_id)
        show_screen(chat_id, msg_id, "➕ 添加链接\n\n第 1 步：请发送链接名称。", cancel_kb("m|cat|link"))
        _ack(call, "请发送名称")
        return
    if screen == "addcat":
        _menu_addcat(call, parts, user_id, chat_id, msg_id)
        return
    if screen == "lmgr":
        text, keyboard = render_link_manager(_arg_int(parts, 2, 0) or 0)
        show_screen(chat_id, msg_id, text, keyboard)
        _ack(call)
        return
    if screen == "link":
        idx = _arg_int(parts, 2)
        page = _arg_int(parts, 3, 0) or 0
        rendered = render_link_detail(idx if idx is not None else -1, page)
        if not rendered:
            text, keyboard = render_link_manager(page)
            show_screen(chat_id, msg_id, "链接不存在或已被删除。\n\n" + text, keyboard)
            _ack(call, "链接不存在")
            return
        show_screen(chat_id, msg_id, rendered[0], rendered[1])
        _ack(call)
        return
    if screen == "ledit":
        _menu_ledit(call, parts, user_id, chat_id, msg_id)
        return
    if screen == "setcat":
        _menu_setcat(call, parts, chat_id, msg_id)
        return
    if screen == "ldel":
        _menu_ldel(call, parts, chat_id, msg_id)
        return
    if screen == "ldelok":
        _menu_ldelok(call, parts, chat_id, msg_id)
        return
    if screen == "exec":
        _menu_exec(call, parts, owner, chat_id, msg_id)
        return
    if screen == "do":
        _menu_do(call, parts, chat_id, msg_id, user_id)
        return
    _ack(call, "未知操作")


def _menu_addcat(call, parts, user_id, chat_id, msg_id):
    pending = pending_input.get(user_id)
    if not pending or pending.get("flow") != "linkadd":
        _show_cat(chat_id, msg_id, "link", True)
        _ack(call, "添加已过期，请重新开始")
        return
    choice = _arg_int(parts, 2)
    if choice is None:
        _ack(call, "参数错误")
        return
    if choice == -2:
        pending["step"] = "cat"
        pending["ts"] = time.time()
        show_screen(chat_id, msg_id, "请发送自定义类别名称。", cancel_kb("m|cat|link"))
        _ack(call, "请发送类别")
        return
    if choice == -1:
        category = "常用"
    else:
        cats = link_categories()
        if choice < 0 or choice >= len(cats):
            _ack(call, "类别不存在")
            return
        category = cats[choice]
    msg = finish_linkadd(user_id, pending, category)
    _ack(call, "已添加" if str(msg).startswith("✅") else "未能添加")


def _menu_ledit(call, parts, user_id, chat_id, msg_id):
    idx = _arg_int(parts, 2)
    field = parts[3] if len(parts) > 3 else ""
    page = _arg_int(parts, 4, 0) or 0
    links = load_links()
    if idx is None or idx < 0 or idx >= len(links):
        _ack(call, "链接不存在")
        return
    if field == "c":
        text, keyboard = render_link_cat_edit(idx, page)
        show_screen(chat_id, msg_id, text, keyboard)
        _ack(call)
        return
    prompts = {
        "n": "请发送新的链接名称。",
        "u": "请发送新的 URL（以 http:// 或 https:// 开头）。",
        "x": "请发送新的类别名称。",
    }
    if field not in prompts:
        _ack(call, "未知操作")
        return
    set_pending(
        user_id, "linkedit",
        idx=idx, field=field, page=page,
        expect_name=links[idx].get("name") or "",
        chat_id=chat_id, msg_id=msg_id,
    )
    show_screen(
        chat_id, msg_id,
        f"✏️ {_clean(links[idx].get('name') or '链接', 40)}\n\n{prompts[field]}",
        cancel_kb(f"m|link|{idx}|{page}"),
    )
    _ack(call, "请发送新内容")


def _menu_setcat(call, parts, chat_id, msg_id):
    idx = _arg_int(parts, 2)
    cat_idx = _arg_int(parts, 3)
    page = _arg_int(parts, 4, 0) or 0
    links = load_links()
    cats = link_categories()
    if idx is None or cat_idx is None or idx < 0 or idx >= len(links) or cat_idx < 0 or cat_idx >= len(cats):
        _ack(call, "链接或类别不存在")
        return
    links[idx]["category"] = cats[cat_idx]
    save_links(links)
    rendered = render_link_detail(idx, page)
    if rendered:
        show_screen(chat_id, msg_id, "✅ 类别已更新。\n\n" + rendered[0], rendered[1])
    _ack(call, "已更新类别")


def _menu_ldel(call, parts, chat_id, msg_id):
    idx = _arg_int(parts, 2)
    page = _arg_int(parts, 3, 0) or 0
    links = load_links()
    if idx is None or idx < 0 or idx >= len(links):
        _ack(call, "链接不存在")
        return
    link = links[idx]
    keyboard = kb_rows([
        [btn("✅ 确认删除", f"m|ldelok|{idx}|{page}")],
        [btn("❌ 取消", f"m|link|{idx}|{page}")],
    ])
    show_screen(
        chat_id, msg_id,
        f"⚠️ 确认删除链接：{_clean(link.get('name') or '链接', 40)}？\n{link.get('url') or ''}",
        keyboard,
    )
    _ack(call, "请再次确认")


def _menu_ldelok(call, parts, chat_id, msg_id):
    idx = _arg_int(parts, 2)
    page = _arg_int(parts, 3, 0) or 0
    links = load_links()
    if idx is None or idx < 0 or idx >= len(links):
        _ack(call, "链接不存在")
        return
    name = links[idx].get("name") or "链接"
    links.pop(idx)
    save_links(links)
    logger.info("菜单删除链接: %s", name)
    text, keyboard = render_link_manager(page)
    show_screen(chat_id, msg_id, f"✅ 已删除链接：{name}\n\n{text}", keyboard)
    _ack(call, "已删除")


def _menu_exec(call, parts, owner, chat_id, msg_id):
    cmd = parts[2] if len(parts) > 2 else ""
    keyboard = sys_back_keyboard()
    if cmd == "stats":
        if not owner:
            _ack(call, "❌ 仅限 owner 使用")
            return
        show_screen(chat_id, msg_id, render_stats_text(), keyboard)
    elif cmd == "about":
        show_screen(chat_id, msg_id, render_about_text(), keyboard)
    elif cmd == "ping":
        show_screen(chat_id, msg_id, render_ping_text(), keyboard)
    elif cmd == "id":
        show_screen(chat_id, msg_id, render_id_text(call.from_user), keyboard)
    elif cmd == "help":
        show_screen(chat_id, msg_id, render_help_text(owner), keyboard)
    else:
        _ack(call, "未知操作")
        return
    _ack(call)


def _menu_do(call, parts, chat_id, msg_id, user_id):
    global active_conversation
    act = parts[2] if len(parts) > 2 else ""
    sid = _arg_int(parts, 3)
    page = _arg_int(parts, 4, 0) or 0
    if sid is None:
        _ack(call, "参数错误")
        return
    conv = get_conversation(sid)
    if act == "delok":
        name = _conv_name(conv)
        if conv:
            delete_conversation(sid)
            logger.info("菜单删除对话对象: %s (%s)", name, sid)
        show_people(chat_id, msg_id, page)
        _ack(call, f"🗑 已删除: {name}" if conv else "对话不存在")
        return
    if not conv:
        show_people(chat_id, msg_id, page)
        _ack(call, "用户不存在")
        return
    name = _conv_name(conv)
    if act == "chat":
        active_conversation = sid
        show_user(chat_id, msg_id, sid, page, banner=f"✅ 已切换到 {name}")
        _ack(call, f"✅ 已切换到: {name}")
        return
    if act == "note":
        set_pending(user_id, "note", sid=sid, page=page, chat_id=chat_id, msg_id=msg_id)
        show_screen(
            chat_id, msg_id,
            f"🏷 给 {name} 写备注\n\n请直接发送备注内容。",
            note_prompt_kb(sid, page),
        )
        _ack(call, "请发送备注")
        return
    if act == "nclear":
        set_note(sid, "")
        show_user(chat_id, msg_id, sid, page, banner="✅ 备注已清除")
        _ack(call, "已清除备注")
        return
    if act == "send":
        set_pending(user_id, "send", sid=sid, page=page, chat_id=chat_id, msg_id=msg_id)
        show_screen(
            chat_id, msg_id,
            f"✉️ 发给 {name} (ID: {sid})\n\n请直接发送文字、图片或文件。下一条内容只会发给这个人。",
            cancel_kb(f"m|user|{sid}|{page}"),
        )
        _ack(call, "请发送内容")
        return
    if act == "ban":
        block_user(sid)
        logger.info("菜单封禁用户: %s (%s)", sid, name)
        show_user(chat_id, msg_id, sid, page, banner=f"🚫 已封禁 {name}")
        _ack(call, f"🚫 已封禁: {name}")
        return
    if act == "unban":
        unblock_user(sid)
        logger.info("菜单解封用户: %s (%s)", sid, name)
        show_user(chat_id, msg_id, sid, page, banner=f"✅ 已解封 {name}")
        _ack(call, f"✅ 已解封: {name}")
        return
    if act == "del":
        keyboard = kb_rows([
            [btn("✅ 确认删除", f"m|do|delok|{sid}|{page}")],
            [btn("❌ 取消", f"m|user|{sid}|{page}")],
        ])
        show_screen(
            chat_id, msg_id,
            f"⚠️ 确认删除 {name} (ID: {sid}) 的全部记录？\n此操作不可恢复。",
            keyboard,
        )
        _ack(call, "请再次确认")
        return
    if act == "exp":
        text = export_history(sid)
        if not text:
            show_user(chat_id, msg_id, sid, page, banner="没有可导出的记录")
            _ack(call, "无记录")
            return
        try:
            _send_chunks(chat_id, text)
        except Exception as e:
            logger.warning("菜单导出失败: %s", e)
            _ack(call, "导出失败")
            return
        show_user(chat_id, msg_id, sid, page, banner="✅ 导出内容已发到对话下方")
        _ack(call, "已导出")
        return
    _ack(call, "未知操作")


def consume_menu_input(message):
    """菜单正在等待文字时消费这条消息。返回 True 表示不要再转发。"""
    global active_conversation
    user = getattr(message, "from_user", None)
    if user is None:
        return False
    user_id = user.id
    pending = pending_input.get(user_id)
    if not pending:
        return False
    if time.time() - pending.get("ts", 0) > PENDING_TIMEOUT:
        clear_pending(user_id)
        bot.reply_to(message, "这一步已超时取消。请重新打开 /menu。这条消息没有转发。")
        return True
    if getattr(message, "reply_to_message", None):
        clear_pending(user_id)
        return False
    text = (getattr(message, "text", None) or "").strip()
    if text.startswith("/"):
        clear_pending(user_id)
        return False
    if not text:
        if pending.get("flow") == "send":
            return _consume_send_media(message, user_id, pending)
        bot.reply_to(message, "这一步需要文字。请发送文字，或点菜单里的「取消」。")
        return True

    flow = pending.get("flow")
    if flow == "note":
        return _consume_note(message, user_id, pending, text)
    if flow == "send":
        return _consume_send(message, user_id, pending, text)
    if flow == "linkfind":
        return _consume_linkfind(message, user_id, pending, text)
    if flow == "linkadd":
        return _consume_linkadd(message, user_id, pending, text)
    if flow == "linkedit":
        return _consume_linkedit(message, user_id, pending, text)
    clear_pending(user_id)
    bot.reply_to(message, "上一步已失效。请重新打开 /menu。这条消息没有转发。")
    return True


def _consume_note(message, user_id, pending, text):
    sid = pending.get("sid")
    page = pending.get("page") or 0
    if not get_conversation(sid):
        clear_pending(user_id)
        bot.reply_to(message, "用户不存在，备注没有保存。")
        return True
    note = text[:200]
    set_note(sid, note)
    clear_pending(user_id)
    bot.reply_to(message, f"✅ 已更新备注：{note}")
    show_user(pending.get("chat_id"), pending.get("msg_id"), sid, page, banner="✅ 备注已更新")
    return True


def _consume_send(message, user_id, pending, text):
    global active_conversation
    sid = pending.get("sid")
    page = pending.get("page") or 0
    if len(text) > 4000:
        bot.reply_to(message, "内容太长，请分成几条发送。")
        return True
    if not get_conversation(sid):
        clear_pending(user_id)
        bot.reply_to(message, "用户不存在，消息没有发送。")
        return True
    if not check_owner_rate_limit():
        bot.reply_to(message, "发送太频繁，请稍等后再发一次。")
        return True
    random_delay(1.0, 2.8)
    try:
        bot.send_message(sid, text)
    except Exception as e:
        logger.warning("菜单发送失败: %s", e)
        bot.reply_to(message, f"❌ 发送失败：{e}\n可以再发一次，或点取消。")
        return True
    active_conversation = sid
    upsert_conversation(sid)
    log_message(sid, "to_stranger", "text", text[:500])
    conv = get_conversation(sid)
    name = _conv_name(conv)
    clear_pending(user_id)
    bot.reply_to(message, f"✅ 已发送给 {name} (ID: {sid})")
    show_user(pending.get("chat_id"), pending.get("msg_id"), sid, page, banner="✅ 已发送")
    return True


def _consume_send_media(message, user_id, pending):
    """菜单「发消息」时，图片、文件等非文字内容按原样复制给对方。"""
    global active_conversation
    sid = pending.get("sid")
    page = pending.get("page") or 0
    kind = message_kind(message)
    if kind not in RELAY_CONTENT_TYPES or kind == "text":
        bot.reply_to(message, "这一步需要文字、图片或文件。请重新发送，或点菜单里的「取消」。")
        return True
    if not get_conversation(sid):
        clear_pending(user_id)
        bot.reply_to(message, "用户不存在，内容没有发送。")
        return True
    if not check_owner_rate_limit():
        bot.reply_to(message, "发送太频繁，请稍等后再发一次。")
        return True
    random_delay(1.0, 2.8)
    try:
        bot.copy_message(
            chat_id=sid,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as e:
        logger.warning("菜单发送媒体失败: %s", e)
        bot.reply_to(message, f"❌ 发送失败：{e}\n可以再发一次，或点取消。")
        return True
    active_conversation = sid
    upsert_conversation(sid)
    log_message(sid, "to_stranger", kind, message_preview(message))
    name = _conv_name(get_conversation(sid))
    clear_pending(user_id)
    bot.reply_to(message, f"✅ 已把{_kind_label(kind)}发给 {name} (ID: {sid})")
    show_user(pending.get("chat_id"), pending.get("msg_id"), sid, page, banner="✅ 已发送")
    return True


def _consume_linkfind(message, user_id, pending, text):
    keyword = text[:64]
    last_link_query[user_id] = keyword
    chat_id = pending.get("chat_id")
    msg_id = pending.get("msg_id")
    clear_pending(user_id)
    body, keyboard = render_search(user_id, 0)
    show_screen(chat_id, msg_id, body, keyboard)
    bot.reply_to(message, body)
    return True


def _consume_linkadd(message, user_id, pending, text):
    step = pending.get("step")
    draft = pending.setdefault("draft", {})
    chat_id = pending.get("chat_id")
    msg_id = pending.get("msg_id")
    if step == "name":
        name = text[:64]
        if any(link.get("name") == name for link in load_links()):
            bot.reply_to(message, "已有同名链接，请换一个名称。")
            return True
        draft["name"] = name
        pending["step"] = "url"
        pending["ts"] = time.time()
        show_screen(
            chat_id, msg_id,
            f"名称：{name}\n\n第 2 步：请发送 URL（以 http:// 或 https:// 开头）。",
            cancel_kb("m|cat|link"),
        )
        bot.reply_to(message, "已记录名称，请继续发送 URL。")
        return True
    if step == "url":
        if not _valid_url(text):
            bot.reply_to(message, "URL 需要以 http:// 或 https:// 开头，且不能有空格。")
            return True
        draft["url"] = text
        pending["step"] = "cat"
        pending["ts"] = time.time()
        show_screen(
            chat_id, msg_id,
            f"名称：{draft.get('name')}\nURL：{text}\n\n第 3 步：选择类别。",
            linkadd_category_keyboard(),
        )
        bot.reply_to(message, "已记录 URL，请在菜单里选择类别。也可以直接发送类别名称。")
        return True
    if step == "cat":
        finish_linkadd(user_id, pending, text[:32] or "常用", message)
        return True
    clear_pending(user_id)
    bot.reply_to(message, "添加步骤已失效，请从菜单重新开始。")
    return True


def _consume_linkedit(message, user_id, pending, text):
    idx = pending.get("idx")
    field = pending.get("field")
    page = pending.get("page") or 0
    links = load_links()
    if not isinstance(idx, int) or idx < 0 or idx >= len(links):
        clear_pending(user_id)
        bot.reply_to(message, "链接不存在或已变化，请从菜单重新选择。")
        return True
    if (links[idx].get("name") or "") != (pending.get("expect_name") or ""):
        clear_pending(user_id)
        bot.reply_to(message, "链接列表已变化，请从菜单重新选择。")
        return True
    if field == "n":
        name = text[:64]
        if any(i != idx and link.get("name") == name for i, link in enumerate(links)):
            bot.reply_to(message, "已有同名链接，请换一个名称。")
            return True
        links[idx]["name"] = name
    elif field == "u":
        if not _valid_url(text):
            bot.reply_to(message, "URL 需要以 http:// 或 https:// 开头，且不能有空格。")
            return True
        links[idx]["url"] = text
    elif field == "x":
        links[idx]["category"] = text[:32] or "常用"
    else:
        clear_pending(user_id)
        bot.reply_to(message, "这一步已失效，请从菜单重新选择。")
        return True
    save_links(links)
    clear_pending(user_id)
    bot.reply_to(message, "✅ 已更新")
    rendered = render_link_detail(idx, page)
    if rendered:
        show_screen(pending.get("chat_id"), pending.get("msg_id"), "✅ 已更新。\n\n" + rendered[0], rendered[1])
    return True

@bot.message_handler(commands=["note"])
def handle_note(message):
    if not is_owner(message.from_user.id):
        return
    parts = message.text.split(" ", 2)
    if len(parts) < 3:
        bot.reply_to(message, "⚠️ 用法：/note <user_id> <备注内容>")
        return
    try:
        sid = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ 无效的用户 ID")
        return
    note_text = parts[2].strip()
    if not note_text:
        bot.reply_to(message, "❌ 备注不能为空")
        return
    set_note(sid, note_text)
    bot.reply_to(message, f"✅ 已为用户 {sid} 添加备注: {note_text}")

@bot.message_handler(commands=["history"])
def handle_history(message):
    if not is_owner(message.from_user.id):
        return
    parts = message.text.split(" ", 1)
    sid = active_conversation
    if len(parts) > 1:
        target = parts[1].strip()
        if target.isdigit():
            convos = get_conversations()
            index = int(target) - 1
            if 0 <= index < len(convos):
                sid = convos[index]["stranger_id"]
            else:
                try:
                    sid = int(target)
                except ValueError:
                    pass
    if not sid:
        bot.reply_to(message, "❌ 没有可查看的对话。先等陌生人发消息吧。")
        return
    msgs = get_history(sid, limit=10)
    if not msgs:
        bot.reply_to(message, "📭 暂无消息记录。")
        return
    conv = get_conversation(sid)
    name = conv["first_name"] if conv else "未知"
    result = f"📜 {name} (ID: {sid}) 最近消息:\n\n"
    for m in reversed(msgs):
        arrow = "⬅" if m["direction"] == "from_stranger" else "➡"
        ts = time.strftime("%m-%d %H:%M", time.localtime(m["timestamp"]))
        content = m["content"][:50] + ("..." if len(m.get("content", "")) > 50 else "")
        if not content:
            content = f"[{m['content_type']}]"
        result += f"{arrow} [{ts}] {content}\n"
    bot.reply_to(message, result)

@bot.message_handler(commands=["ban"])
def handle_ban(message):
    if not is_owner(message.from_user.id):
        return
    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ 用法：/ban <user_id>\n示例：/ban 123456789")
        return
    try:
        sid = int(parts[1].strip())
    except ValueError:
        bot.reply_to(message, "❌ 无效的用户 ID")
        return
    block_user(sid)
    name = get_conversation(sid)
    name_str = (name["first_name"] if name else "未知") or "未知"
    bot.reply_to(message, f"🚫 已封禁: {name_str} (ID: {sid})")
    logger.info("封禁用户: %s (%s)", sid, name_str)

@bot.message_handler(commands=["unban"])
def handle_unban(message):
    if not is_owner(message.from_user.id):
        return
    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ 用法：/unban <user_id>\n示例：/unban 123456789")
        return
    try:
        sid = int(parts[1].strip())
    except ValueError:
        bot.reply_to(message, "❌ 无效的用户 ID")
        return
    unblock_user(sid)
    bot.reply_to(message, f"✅ 已解封用户 ID: {sid}")

@bot.message_handler(commands=["banlist"])
def handle_banlist(message):
    if not is_owner(message.from_user.id):
        return
    blocked = get_blocked_users()
    if not blocked:
        bot.reply_to(message, "📭 没有被封禁的用户。")
        return
    result = "🚫 封禁列表:\n\n"
    for b in blocked:
        name = b["first_name"] or "未知"
        username = f" (@{b['username']})" if b["username"] else ""
        result += f"  • {name}{username} (ID: {b['stranger_id']})\n"
    bot.reply_to(message, result)

@bot.message_handler(commands=["del"])
def handle_del(message):
    if not is_owner(message.from_user.id):
        return
    parts = message.text.split(" ", 2)
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ 用法：/del <ID/序号> [confirm]\n示例：/del 123456789")
        return
    target = parts[1].strip()
    confirm = len(parts) > 2 and parts[2].strip().lower() == "confirm"

    # 解析目标：优先序号（全量列表含封禁），否则按 ID
    convos = get_all_conversations()
    sid = None
    if target.isdigit():
        index = int(target) - 1
        if 0 <= index < len(convos):
            sid = convos[index]["stranger_id"]
        else:
            sid = int(target)
    if sid is None:
        try:
            sid = int(target)
        except ValueError:
            bot.reply_to(message, "❌ 请输入有效序号或用户 ID")
            return

    conv = get_conversation(sid)
    if not conv:
        bot.reply_to(message, f"❌ 未找到对话对象: {target}")
        return

    name = (conv["first_name"] or "未知") or "未知"
    if not confirm:
        bot.reply_to(
            message,
            f"⚠️ 确认删除 {name} (ID: {sid}) 的全部记录？\n"
            f"此操作不可恢复，将同时清空其消息历史。\n\n"
            f"确认请回复：/del {target} confirm"
        )
        return

    delete_conversation(sid)
    logger.info("删除对话对象: %s (%s)", name, sid)
    bot.reply_to(message, f"🗑 已删除 {name} (ID: {sid}) 及其全部记录。")

@bot.message_handler(commands=["send"])
def handle_send(message):
    if not is_owner(message.from_user.id):
        return
    parts = message.text.split(" ", 2)
    if len(parts) < 3:
        bot.reply_to(message, "⚠️ 用法：/send <user_id> <消息内容>")
        return
    try:
        sid = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ 无效的用户 ID")
        return
    text = parts[2]
    try:
        bot.send_message(sid, text)
        upsert_conversation(sid)
        log_message(sid, "to_stranger", "text", text[:500])
        bot.reply_to(message, f"✅ 已发送给 ID: {sid}")
    except Exception as e:
        bot.reply_to(message, f"❌ 发送失败：{e}")

@bot.message_handler(commands=["export"])
def handle_export(message):
    if not is_owner(message.from_user.id):
        return
    parts = message.text.split(" ", 1)
    target_sid = None
    if len(parts) > 1:
        arg = parts[1].strip()
        if arg.isdigit():
            convos = get_conversations()
            index = int(arg) - 1
            if 0 <= index < len(convos):
                target_sid = convos[index]["stranger_id"]
            else:
                try:
                    target_sid = int(arg)
                except ValueError:
                    pass
    if not target_sid:
        target_sid = active_conversation
    if not target_sid:
        bot.reply_to(message, "❌ 没有可导出的对话。请指定用户 ID 或序号。")
        return
    text = export_history(target_sid)
    if not text:
        bot.reply_to(message, "❌ 未找到该用户的对话记录。")
        return
    if len(text) > 4000:
        text = text[:4000] + "\n...(已截断)"
    bot.reply_to(message, text)

@bot.message_handler(commands=["linkadd"])
def handle_linkadd(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "❌ 仅限 owner 使用")
        return
    if len(message.text.split()) < 2:
        bot.reply_to(message,
            "⚠️ 用法：\n"
            "  /linkadd <name>;<url>              （默认类别：常用）\n"
            "  /linkadd <name>;<url>;<category>   （指定类别）\n\n"
            "示例：\n"
            "  /linkadd VSCode;https://code.visualstudio.com\n"
            "  /linkadd Python;https://www.python.org;学习")
        return
    rest = message.text[len("/linkadd") + 1:].strip()
    parts = rest.split(";", 2)
    if len(parts) < 2:
        bot.reply_to(message, "❌ 格式错误，请使用分号分隔")
        return
    name = parts[0].strip()
    url = parts[1].strip()
    category = parts[2].strip() if len(parts) > 2 else "常用"
    if not name or not url:
        bot.reply_to(message, "❌ 链接名和 URL 不能为空")
        return
    success, msg = add_link(name, url, category)
    bot.reply_to(message, msg)

@bot.message_handler(commands=["linkdel"])
def handle_linkdel(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "❌ 仅限 owner 使用")
        return
    if len(message.text.split()) < 2:
        bot.reply_to(message,
            "⚠️ 用法：\n"
            "  /linkdel <序号>        （删除指定序号的链接）\n"
            "  /linkdel <链接名>      （删除指定名称的链接）\n\n"
            "示例：\n"
            "  /linkdel 1              # 删除第 1 个链接\n"
            "  /linkdel VSCode        # 删除名为 VSCode 的链接")
        return
    target = message.text.split(" ", 1)[1].strip()
    if target.isdigit():
        index = int(target) - 1
        links = load_links()
        if index < 0 or index >= len(links):
            bot.reply_to(message, f"❌ 序号 {target} 超出范围（共 {len(links)} 个链接）")
            return
        deleted_name = links[index]["name"]
        links.pop(index)
        save_links(links)
        bot.reply_to(message, f"✅ 已删除链接：{deleted_name}")
        return
    success, msg = delete_link(target)
    bot.reply_to(message, msg)

def build_links_keyboard(page=0, category=None):
    links = get_links_by_category(category)
    per_page = 6
    total_pages = (len(links) + per_page - 1) // per_page
    start = page * per_page
    page_links = links[start:start + per_page]
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for i, link in enumerate(page_links, start + 1):
        button = safe_url_button(f"{i}. {link.get('name') or '链接'}", link.get("url") or "")
        if button:
            keyboard.add(button)
        else:
            keyboard.add(types.InlineKeyboardButton(
                f"{i}. {_clean(link.get('name') or '链接', 24)}（链接无效）",
                callback_data="m|noop",
            ))
    nav_row = []
    if page > 0:
        nav_row.append(types.InlineKeyboardButton(
            "◀ 上一页", callback_data=f"links_{page-1}_{category or ''}"))
    if page < total_pages - 1:
        nav_row.append(types.InlineKeyboardButton(
            "下一页 ▶", callback_data=f"links_{page+1}_{category or ''}"))
    if nav_row:
        keyboard.row(*nav_row)
    return keyboard

@bot.message_handler(commands=["links"])
def handle_links(message):
    category = None
    if len(message.text.split()) > 1:
        category = message.text.split(" ", 1)[1]
    links = get_links_by_category(category)
    if not links:
        bot.reply_to(message, "❌ 没有可用链接" if category else "❌ 没有链接")
        return
    keyboard = build_links_keyboard(0, category)
    bot.reply_to(message, f"🔗 可用链接（共 {len(links)} 条）", reply_markup=keyboard)

@bot.message_handler(commands=["linkcat"])
def handle_linkcat(message):
    if len(message.text.split()) < 2:
        bot.reply_to(message, "⚠️ 用法：/linkcat <类别>\n\n示例：/linkcat 开发")
        return
    category = message.text.split(" ", 1)[1]
    links = get_links_by_category(category)
    if not links:
        bot.reply_to(message, f"❌ 类别 '{category}' 中没有链接")
        return
    keyboard = build_links_keyboard(0, category)
    bot.reply_to(message, f"📁 类别：{category}（共 {len(links)} 条）", reply_markup=keyboard)

@bot.message_handler(commands=["linkedit"])
def handle_linkedit(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "❌ 仅限 owner 使用")
        return
    if len(message.text.split()) < 2:
        bot.reply_to(message,
            "⚠️ 用法：\n"
            "  /linkedit <旧名称>;<新名称>;<新URL>;<新类别>\n"
            "可以只改部分字段（留空表示不修改）\n\n"
            "示例：\n"
            "  /linkedit GitHub;;https://github.com/new;\n"
            "  /linkedit GitHub;NewName;;")
        return
    rest = message.text[len("/linkedit") + 1:].strip()
    parts = rest.split(";", 3)
    if len(parts) < 1 or not parts[0].strip():
        bot.reply_to(message, "❌ 必须指定要修改的链接名称")
        return
    old_name = parts[0].strip()
    new_name = parts[1].strip() if len(parts) > 1 else ""
    new_url = parts[2].strip() if len(parts) > 2 else ""
    new_category = parts[3].strip() if len(parts) > 3 else ""
    if not new_name and not new_url and not new_category:
        bot.reply_to(message, "❌ 至少需要指定一个新值")
        return
    success, msg = edit_link(old_name,
        new_name=new_name or None,
        new_url=new_url or None,
        new_category=new_category or None)
    bot.reply_to(message, msg)

@bot.message_handler(commands=["linkfind"])
def handle_linkfind(message):
    if len(message.text.split()) < 2:
        bot.reply_to(message, "⚠️ 用法：/linkfind <关键词>\n\n示例：/linkfind python")
        return
    keyword = message.text.split(" ", 1)[1].strip()
    results = find_links(keyword)
    if not results:
        bot.reply_to(message, f"❌ 未找到包含 '{keyword}' 的链接")
        return
    shown = results[:10]
    result = f"🔍 搜索 '{keyword}'（共 {len(results)} 条）\n\n"
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for i, link in enumerate(shown, 1):
        result += f"  {i}. {link['name']} — {link.get('category') or '常用'}\n"
        button = safe_url_button(f"{i}. {link['name']}", link.get("url") or "")
        if button:
            keyboard.add(button)
    if len(results) > len(shown):
        result += f"\n只显示前 {len(shown)} 条，打开 /menu 可以继续翻页搜索。"
    bot.reply_to(message, result, reply_markup=keyboard if keyboard.keyboard else None)

# ============================================================
# InlineKeyboard 回调
# ============================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("chat_"))
def callback_chat(call):
    global active_conversation
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ 仅限 owner")
        return
    sid = int(call.data.split("_", 1)[1])
    conv = get_conversation(sid)
    if conv:
        active_conversation = sid
        name = conv["first_name"] or "未知"
        bot.answer_callback_query(call.id, f"✅ 已切换到: {name}")
        bot.edit_message_text(
            f"✅ 当前对话: {name} (ID: {sid})",
            call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "❌ 用户不存在")

@bot.callback_query_handler(func=lambda call: call.data.startswith("card_"))
def callback_card(call):
    global active_conversation
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ 仅限 owner")
        return
    parts = call.data.split("_")
    action = parts[1]
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if action == "none":
        page = int(parts[2]) if len(parts) > 2 and str(parts[2]).isdigit() else 0
        bot.answer_callback_query(call.id, f"当前第 {page + 1} 页")
        return
    if action == "page":
        page = int(parts[2])
        bot.edit_message_text(render_contacts_page(page), chat_id, msg_id,
                              reply_markup=build_contacts_keyboard(page))
        bot.answer_callback_query(call.id)
        return

    sid = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    conv = get_conversation(sid)
    name = (conv["first_name"] if conv else "未知") or "未知"

    if action == "switch":
        if not conv:
            bot.answer_callback_query(call.id, "❌ 用户不存在")
            return
        active_conversation = sid
        bot.answer_callback_query(call.id, f"✅ 已切换到: {name}")
        bot.edit_message_text(render_contacts_page(page), chat_id, msg_id,
                              reply_markup=build_contacts_keyboard(page))
    elif action == "del":
        if not conv:
            bot.answer_callback_query(call.id, "❌ 用户不存在")
            return
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ 确认删除", callback_data=f"card_delok_{sid}_{page}"),
            types.InlineKeyboardButton("❌ 取消", callback_data=f"card_page_{page}"),
        )
        bot.edit_message_text(
            f"⚠️ 确认删除 {name} (ID: {sid}) 的全部记录？\n"
            f"此操作不可恢复，将同时清空其消息历史。",
            chat_id, msg_id, reply_markup=kb)
        bot.answer_callback_query(call.id, "⚠️ 请再次确认")
    elif action == "delok":
        delete_conversation(sid)
        logger.info("卡片删除对话对象: %s (%s)", name, sid)
        bot.answer_callback_query(call.id, f"🗑 已删除: {name}")
        bot.edit_message_text(
            f"🗑 已删除 {name} (ID: {sid}) 及其全部记录。\n\n{render_contacts_page(page)}",
            chat_id, msg_id, reply_markup=build_contacts_keyboard(page))
    elif action == "ban":
        if not conv:
            bot.answer_callback_query(call.id, "❌ 用户不存在")
            return
        if conv.get("is_blocked"):
            unblock_user(sid)
            bot.answer_callback_query(call.id, f"✅ 已解封: {name}")
        else:
            block_user(sid)
            bot.answer_callback_query(call.id, f"🚫 已拉黑: {name}")
        bot.edit_message_text(render_contacts_page(page), chat_id, msg_id,
                              reply_markup=build_contacts_keyboard(page))

@bot.callback_query_handler(func=lambda call: call.data.startswith("links_"))
def callback_links(call):
    parts = call.data.split("_")
    try:
        page = int(parts[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id)
        return
    category = "_".join(parts[2:]).strip() or None
    keyboard = build_links_keyboard(page, category)
    links = get_links_by_category(category)
    label = f"🔗 可用链接（共 {len(links)} 条）"
    if category:
        label = f"📁 类别：{category}（共 {len(links)} 条）"
    bot.edit_message_text(label, call.message.chat.id, call.message.message_id,
                          reply_markup=keyboard)
    bot.answer_callback_query(call.id)

# ============================================================
# 核心消息路由（多对话）
# 文字以外的图片、文件、语音等也走这里，靠 copy_message 原样转发。
# ============================================================
RELAY_CONTENT_TYPES = [
    "text", "photo", "document", "animation",
    "video", "video_note", "voice", "audio", "sticker",
]
_KIND_LABELS = {
    "photo": "图片",
    "document": "文件",
    "animation": "动图",
    "video": "视频",
    "video_note": "视频",
    "voice": "语音",
    "audio": "音频",
    "sticker": "贴纸",
}
_media_group_seen = {}


def message_kind(message):
    if getattr(message, "text", None):
        return "text"
    return getattr(message, "content_type", None) or "text"


def message_preview(message):
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "")[:500]


def _kind_label(kind):
    return _KIND_LABELS.get(kind, "内容")


def should_announce_media_group(user_id, message):
    """同一组相册只发一次来源提示，每张图仍然单独转发。"""
    group_id = getattr(message, "media_group_id", None)
    if not group_id:
        return True
    now = time.time()
    stale = [key for key, ts in _media_group_seen.items() if now - ts > 120]
    for key in stale:
        _media_group_seen.pop(key, None)
    key = (user_id, str(group_id))
    if key in _media_group_seen:
        return False
    _media_group_seen[key] = now
    return True


@bot.message_handler(func=lambda m: True, content_types=RELAY_CONTENT_TYPES)
def handle_all(message):
    global active_conversation
    user_id = message.from_user.id

    # 菜单向导进行中时，下一条文字只完成当前步骤，不转发出去。
    if consume_menu_input(message):
        return

    # ==================== Owner 回复被转发的消息 ====================
    if is_owner(user_id) and message.reply_to_message:
        replied_msg_id = message.reply_to_message.message_id
        with conversation_lock:
            target_id = forwarded_msg_map.get(replied_msg_id)
        if not target_id:
            bot.reply_to(message, "⚠️ 找不到回复目标。该消息可能不是通过我转发的。\n"
                         "发送 /menu，在对话列表里指定对象后再发。")
            return

        if not check_owner_rate_limit():
            bot.reply_to(message, "⚠️ 发送太频繁，请稍等。")
            return

        random_delay(1.0, 2.8)
        try:
            sent = bot.copy_message(
                chat_id=target_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            with conversation_lock:
                active_conversation = target_id
            upsert_conversation(target_id)
            log_message(target_id, "to_stranger", message_kind(message), message_preview(message))
            logger.info("回复: owner -> %s", target_id)
        except Exception as e:
            logger.warning("回复转发失败: %s", e)
            bot.reply_to(message, f"❌ 发送失败：{e}")
        return

    # ==================== 陌生人发消息 → 转发给 owner ====================
    if not is_owner(user_id):
        sender_name = message.from_user.first_name or "未知"
        sender_username = message.from_user.username
        sender_id = message.from_user.id

        # 封禁检查
        conv = get_conversation(sender_id)
        if conv and conv.get("is_blocked"):
            return

        # 速率限制
        if not check_rate_limit(sender_id):
            logger.info("陌生人速率限制: %s (%s)", sender_name, sender_id)
            return

        upsert_conversation(sender_id, sender_name, sender_username or "")

        convos = get_conversations()
        queue_pos = None
        for i, c in enumerate(convos, 1):
            if c["stranger_id"] == sender_id:
                queue_pos = i
                break
        with conversation_lock:
            if not active_conversation:
                active_conversation = sender_id

        announced = should_announce_media_group(sender_id, message)
        if announced:
            if MSG_HEADER:
                header = MSG_HEADER.replace("{name}", sender_name)
                if sender_username:
                    header = header.replace("{username}", sender_username)
                else:
                    header = header.replace(" (@{username})", "").replace("@{username}", "")
                header = header.replace("{id}", str(sender_id))
                header = header.replace("{queue}", str(queue_pos))
                header = header.replace("{total}", str(len(convos)))
            else:
                header = f"📩 来自：{sender_name}"
                if sender_username:
                    header += f" (@{sender_username})"
                header += f"\nID: `{sender_id}`"
                header += f"\n队列: #{queue_pos}/{len(convos)}  "
                header += "⬅ 当前" if active_conversation == sender_id else ""
            bot.send_message(OWNER_ID, header, parse_mode="Markdown")
        random_delay(0.5, 1.8)
        forwarded = bot.copy_message(
            chat_id=OWNER_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        with conversation_lock:
            forwarded_msg_map[forwarded.message_id] = sender_id
        if announced and MSG_FOOTER:
            footer = MSG_FOOTER.replace("{name}", sender_name).replace("{id}", str(sender_id))
            bot.send_message(OWNER_ID, footer, parse_mode="Markdown")

        log_message(sender_id, "from_stranger", message_kind(message), message_preview(message),
                    owner_msg_id=forwarded.message_id)
        logger.info("转发: %s (%s) -> owner, 队列 #%s", sender_name, sender_id, queue_pos)
        return

    # ==================== Owner 直接发消息 → 发给当前活跃对话 ====================
    if active_conversation:
        if not check_owner_rate_limit():
            bot.reply_to(message, "⚠️ 发送太频繁，请稍等。")
            return
        random_delay(1.2, 3.0)
        try:
            sent = bot.copy_message(
                chat_id=active_conversation,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            log_message(active_conversation, "to_stranger", message_kind(message), message_preview(message))
            conv = get_conversation(active_conversation)
            name = (conv["first_name"] if conv else "未知") or "未知"
            bot.reply_to(message,
                f"✅ 已发送给 {name} "
                f"(ID: {active_conversation})")
        except Exception as e:
            logger.warning("发送失败: %s", e)
            bot.reply_to(message, f"❌ 发送失败：{e}")
    else:
        bot.reply_to(message, "💡 回复我转发的消息即可回复对方。\n"
                     "也可以发送 /menu，打开对话列表后指定对象。")


# ============================================================
# 主入口
# ============================================================
def _install_command_pending_reset():
    """斜杠命令会取消菜单里未完成的输入，避免下一条普通消息被当成备注或链接。"""
    for handler in getattr(bot, "message_handlers", None) or []:
        fn = handler.get("function")
        if not fn or getattr(fn, "_clears_pending", False):
            continue

        def _make(fn):
            def wrapped(message, *args, **kwargs):
                user = getattr(message, "from_user", None)
                text = getattr(message, "text", None) or ""
                if user is not None and text.startswith("/"):
                    clear_pending(user.id)
                return fn(message, *args, **kwargs)
            wrapped._clears_pending = True
            return wrapped

        handler["function"] = _make(fn)


_install_command_pending_reset()


if __name__ == "__main__":
    logger.info("🤖 TG 中继机器人 v%s 启动中...", VERSION)

    # 启动自检：验证 Token
    try:
        me = bot.get_me()
        logger.info("Bot 身份: @%s (ID: %s)", me.username, me.id)
    except Exception as e:
        logger.error("Token 验证失败: %s", e)
        sys.exit(1)

    # 注册命令菜单
    try:
        bot.set_my_commands([
            types.BotCommand("menu", "打开菜单（全部功能）"),
            types.BotCommand("start", "欢迎信息"),
            types.BotCommand("help", "帮助"),
            types.BotCommand("ping", "检查延迟"),
            types.BotCommand("id", "获取你的 ID"),
            types.BotCommand("about", "机器人信息"),
            types.BotCommand("stats", "统计面板(Owner)"),
            types.BotCommand("who", "当前对话对象"),
            types.BotCommand("queue", "待回复队列"),
            types.BotCommand("chat", "切换对话"),
            types.BotCommand("send", "主动发消息(Owner)"),
            types.BotCommand("note", "备注用户(Owner)"),
            types.BotCommand("history", "消息记录(Owner)"),
            types.BotCommand("export", "导出对话(Owner)"),
            types.BotCommand("ban", "封禁用户(Owner)"),
            types.BotCommand("unban", "解封用户(Owner)"),
            types.BotCommand("banlist", "封禁列表(Owner)"),
            types.BotCommand("del", "删除对话对象(Owner)"),
            types.BotCommand("contacts", "对话卡片面板(Owner)"),
            types.BotCommand("links", "可用链接"),
            types.BotCommand("linkcat", "按类别查看链接"),
            types.BotCommand("linkfind", "搜索链接"),
            types.BotCommand("linkadd", "添加链接(Owner)"),
            types.BotCommand("linkedit", "修改链接(Owner)"),
            types.BotCommand("linkdel", "删除链接(Owner)"),
        ])
        logger.info("命令菜单已注册")
    except Exception as e:
        logger.warning("命令菜单注册失败: %s", e)

    # 启动自检：验证 Owner ID
    try:
        bot.get_chat(OWNER_ID)
        logger.info("Owner ID 验证通过: %s", OWNER_ID)
    except Exception as e:
        logger.error("Owner ID 验证失败: %s", e)
        sys.exit(1)

    # 确保 polling 前没有残留 webhook
    if not WEBHOOK_BASE:
        try:
            bot.remove_webhook()
        except Exception:
            pass

    if WEBHOOK_BASE:
        logger.info("Webhook 模式")
        webhook_url = WEBHOOK_BASE.rstrip("/") + "/webhook"
        try:
            bot.remove_webhook()
            bot.set_webhook(url=webhook_url)
            logger.info("Webhook 已设置: %s", webhook_url)
        except Exception as e:
            logger.error("Webhook 设置失败: %s", e)
            sys.exit(1)
        serve(flask_app, host="0.0.0.0", port=PORT)
    else:
        logger.info("Polling 模式，健康检查端口: %s", PORT)
        flask_thread = threading.Thread(
            target=lambda: serve(flask_app, host="0.0.0.0", port=PORT),
            daemon=True,
        )
        flask_thread.start()
        logger.info("Flask 已启动: http://0.0.0.0:%s/health", PORT)
        bot.infinity_polling()
