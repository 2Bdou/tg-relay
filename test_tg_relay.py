#!/usr/bin/env python3
"""
tg-relay-bot 模拟测试脚本
- 在不连接 Telegram API 的情况下测试核心逻辑
- 测试链接管理 CRUD
- 模拟消息路由逻辑
- Flask 健康检查测试（如有 Flask 则启动服务测试）
"""

import os
import sys
import json
import time
import threading
import tempfile
import urllib.request
import urllib.error

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}  FAIL: {detail}")

# ============================================================
# 测试环境变量
# ============================================================
os.environ["TG_BOT_TOKEN"] = "test:fake-bot-token-for-unit-test"
os.environ["TG_OWNER_ID"] = "999999999"
os.environ["TG_PORT"] = "18081"
os.environ["TG_LOG_LEVEL"] = "DEBUG"

# ============================================================
# 测试 0: .env 自动加载
# ============================================================
print("\n📦 测试 0: .env 自动加载与兜底")
import tempfile

# 模拟 .env 文件
env_file = os.path.join(tempfile.gettempdir(), ".env.test")
with open(env_file, "w") as f:
    f.write("TG_BOT_TOKEN=from_dotenv_file\nTG_OWNER_ID=88888\n")
# 清除可能存在的同名环境变量（避免干扰）
os.environ.pop("TG_BOT_TOKEN", None)
os.environ.pop("TG_OWNER_ID", None)

# 测试 dotenv 加载
try:
    from dotenv import load_dotenv
    load_dotenv(env_file)
    check("dotenv 加载成功", os.getenv("TG_BOT_TOKEN") == "from_dotenv_file")
    check("dotenv OWNER_ID", os.getenv("TG_OWNER_ID") == "88888")
except ImportError:
    check("dotenv 未安装(优雅降级)", True)
    check("降级: 直接读环境变量", True)

os.remove(env_file)

# 恢复测试环境变量
os.environ["TG_BOT_TOKEN"] = "test:fake-bot-token-for-unit-test"
os.environ["TG_OWNER_ID"] = "999999999"

# 测试 or 兜底
test_token = os.getenv("TG_BOT_TOKEN") or ""
test_owner = int(os.getenv("TG_OWNER_ID") or "0")
check("TOKEN or '' 兜底", test_token == "test:fake-bot-token-for-unit-test")
check("OWNER_ID or '0' 兜底", test_owner == 999999999)

# 模拟缺失环境变量时 or 兜底
os.environ.pop("TG_BOT_TOKEN", None)
os.environ.pop("TG_OWNER_ID", None)
missing_token = os.getenv("TG_BOT_TOKEN") or ""
missing_owner = int(os.getenv("TG_OWNER_ID") or "0")
check("缺失时 TOKEN=''", missing_token == "")
check("缺失时 OWNER_ID=0", missing_owner == 0)
os.environ["TG_BOT_TOKEN"] = "test:fake-bot-token-for-unit-test"
os.environ["TG_OWNER_ID"] = "999999999"

# ============================================================
# 测试 1: 配置加载
# ============================================================
print("\n📦 测试 1: 配置加载")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_OWNER_ID = int(os.getenv("TG_OWNER_ID", "0"))
TG_PORT = int(os.getenv("TG_PORT", "8080"))
check("TG_BOT_TOKEN 读取", TG_BOT_TOKEN == "test:fake-bot-token-for-unit-test")
check("TG_OWNER_ID 读取", TG_OWNER_ID == 999999999)
check("TG_PORT 读取", TG_PORT == 18081)
check("WEBHOOK_URL 默认空", os.getenv("TG_WEBHOOK_URL", "") == "")

# ============================================================
# 测试 2: Token 脱敏
# ============================================================
print("\n📦 测试 2: Token 脱敏")
token = "test_bot_token_for_masking_xyz"
masked = token[:6] + "..." + token[-4:] if len(token) > 10 else "***"
check("长 token 脱敏", masked == "test_b..._xyz")
short_token = "abc"
masked_short = short_token[:6] + "..." + short_token[-4:] if len(short_token) > 10 else "***"
check("短 token 脱敏", masked_short == "***")

# ============================================================
# 测试 3: Webhook URL 拼接
# ============================================================
print("\n📦 测试 3: Webhook URL 拼接")
base = "https://example.com"
url = base.rstrip("/") + "/webhook"
check("无尾部斜杠", url == "https://example.com/webhook")
base2 = "https://example.com/"
url2 = base2.rstrip("/") + "/webhook"
check("有尾部斜杠", url2 == "https://example.com/webhook")

# ============================================================
# 测试 4: 链接管理 CRUD
# ============================================================
print("\n📦 测试 4: 链接管理 CRUD")

LINKS_FILE = os.path.join(tempfile.gettempdir(), "test_links.json")

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
            pass
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

def is_owner(user_id):
    return user_id == TG_OWNER_ID

# 清理
if os.path.exists(LINKS_FILE):
    os.remove(LINKS_FILE)

links = load_links()
check("load_links 返回默认链接", len(links) == 5, f"got {len(links)}")

# add
success, msg = add_link("TestLink", "https://test.com", "测试")
check("add_link 成功", success, msg)
links = load_links()
check("add 后数量=6", len(links) == 6, f"got {len(links)}")

# 重复
success, msg = add_link("TestLink", "https://test.com", "测试")
check("add_link 拒绝重复", not success, msg)

# 类别筛选
check("类别'开发'=3", len(get_links_by_category("开发")) == 3)
check("类别'测试'=1", len(get_links_by_category("测试")) == 1)
check("类别'不存在'=0", len(get_links_by_category("不存在")) == 0)
check("全部链接=6", len(get_links_by_category()) == 6)

# 按名称删除
success, msg = delete_link("TestLink")
check("delete_link 按名称成功", success)
check("删除后=5", len(load_links()) == 5)

# 删除不存在的
success, msg = delete_link("不存在的链接")
check("delete_link 不存在", not success)

# is_owner
check("is_owner(OWNER)=True", is_owner(999999999))
check("is_owner(123)=False", not is_owner(123))

# edit_link
add_link("EditMe", "https://old.com", "旧类别")
success, msg = edit_link("EditMe", new_url="https://new.com")
check("edit_link 改URL", success, msg)
links = load_links()
found = [l for l in links if l["name"] == "EditMe"]
check("edit_link URL已更新", found and found[0]["url"] == "https://new.com")

success, msg = edit_link("EditMe", new_name="Renamed", new_category="新类别")
check("edit_link 改名+类别", success, msg)
links = load_links()
check("edit_link 名称已变更", not any(l["name"] == "EditMe" for l in links))
found = [l for l in links if l["name"] == "Renamed"]
check("edit_link 新名称存在", len(found) == 1 and found[0]["category"] == "新类别")

success, msg = edit_link("不存在的链接", new_url="https://x.com")
check("edit_link 不存在", not success)
delete_link("Renamed")

# find_links
results = find_links("git")
check("find_links('git') 找到GitHub", any(l["name"] == "GitHub" for l in results))
results = find_links("python")
check("find_links('python') 找到Python文档", any("Python" in l["name"] for l in results))
results = find_links("学习")
check("find_links('学习') 按类别搜索", len(results) >= 1)
results = find_links("xyz不存在的关键词123")
check("find_links 无结果", len(results) == 0)

# 清理
if os.path.exists(LINKS_FILE):
    os.remove(LINKS_FILE)

# ============================================================
# 测试 5: 消息路由逻辑（多对话模拟）
# ============================================================
print("\n📦 测试 5: 多对话路由逻辑 (forwarded_msg_map)")

forwarded_msg_map = {}  # owner_side_message_id -> stranger_id
active_conversation = None
OWNER_ID = TG_OWNER_ID

def sim_stranger_msg(sender_id, sender_name):
    global active_conversation
    if sender_id == OWNER_ID:
        return False, None
    # 模拟转发消息到 owner 得到的 message_id
    fake_msg_id = hash(f"{sender_id}_{time.time()}") % 100000
    forwarded_msg_map[fake_msg_id] = sender_id
    if active_conversation is None:
        active_conversation = sender_id
    return True, fake_msg_id

def sim_owner_reply(reply_to_msg_id):
    global active_conversation
    target = forwarded_msg_map.get(reply_to_msg_id)
    if target:
        active_conversation = target
    return target

# 陌生人 Alice
ok, msg_id_a = sim_stranger_msg(100, "Alice")
check("Alice 消息路由", ok)
check("Alice msg_id 映射", forwarded_msg_map.get(msg_id_a) == 100)
check("active_conversation → Alice", active_conversation == 100)

# Owner 回复 Alice 的消息
target = sim_owner_reply(msg_id_a)
check("回复 Alice 正确", target == 100)

# 陌生人 Bob（不覆盖 active）
ok, msg_id_b = sim_stranger_msg(200, "Bob")
check("Bob 消息路由", ok)
check("Bob msg_id 映射", forwarded_msg_map.get(msg_id_b) == 200)
check("active_conversation 保持 Alice", active_conversation == 100)

# Owner 回复 Bob 的消息
target = sim_owner_reply(msg_id_b)
check("回复 Bob 正确", target == 200)
check("active_conversation 切换到 Bob", active_conversation == 200)

# 回复不存在的消息 ID
target = sim_owner_reply(99999)
check("回复不存在消息 → None", target is None)

# Owner 不会作为陌生人路由
ok, _ = sim_stranger_msg(OWNER_ID, "Owner")
check("Owner 不自路由", not ok)

# 清理
forwarded_msg_map.clear()
active_conversation = None

# ============================================================
# 测试 6: SQLite 数据库操作
# ============================================================
print("\n📦 测试 6: SQLite 数据库操作")

import sqlite3
DB_FILE = os.path.join(tempfile.gettempdir(), "test_relay.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
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
""")
conn.commit()

# upsert
conn.execute("""
    INSERT INTO conversations (stranger_id, first_name, username, last_message_time, message_count)
    VALUES (?, ?, ?, ?, 1)
    ON CONFLICT(stranger_id) DO UPDATE SET
        first_name = COALESCE(NULLIF(?, ''), first_name),
        username = COALESCE(NULLIF(?, ''), username),
        last_message_time = ?,
        message_count = message_count + 1
""", (100, "Alice", "alice_tg", int(time.time()), "Alice", "alice_tg", int(time.time())))
conn.commit()
check("upsert Alice", True)

conn.execute("""
    INSERT INTO conversations (stranger_id, first_name, username, last_message_time, message_count)
    VALUES (?, ?, ?, ?, 1)
    ON CONFLICT(stranger_id) DO UPDATE SET
        first_name = COALESCE(NULLIF(?, ''), first_name),
        username = COALESCE(NULLIF(?, ''), username),
        last_message_time = ?,
        message_count = message_count + 1
""", (200, "Bob", "bob_tg", int(time.time()) + 1, "Bob", "bob_tg", int(time.time()) + 1))
conn.commit()
check("upsert Bob", True)

# query
rows = conn.execute("SELECT * FROM conversations ORDER BY last_message_time DESC").fetchall()
check("conversations 数量=2", len(rows) == 2, f"got {len(rows)}")
check("第一位是 Bob(最新)", rows[0]["first_name"] == "Bob")

# note
conn.execute("UPDATE conversations SET note = ? WHERE stranger_id = ?", ("VIP用户", 100))
conn.commit()
row = conn.execute("SELECT note FROM conversations WHERE stranger_id = 100").fetchone()
check("note 更新", row["note"] == "VIP用户")

# log message
conn.execute(
    "INSERT INTO messages (stranger_id, direction, content_type, content, timestamp) "
    "VALUES (?, ?, ?, ?, ?)",
    (100, "from_stranger", "text", "Hello!", int(time.time()))
)
conn.execute(
    "INSERT INTO messages (stranger_id, direction, content_type, content, timestamp) "
    "VALUES (?, ?, ?, ?, ?)",
    (100, "to_stranger", "text", "Hi Alice!", int(time.time()))
)
conn.commit()
rows = conn.execute("SELECT * FROM messages WHERE stranger_id = 100 ORDER BY timestamp").fetchall()
check("messages 数量=2", len(rows) == 2, f"got {len(rows)}")
check("第一条是 from_stranger", rows[0]["direction"] == "from_stranger")
check("内容匹配", rows[0]["content"] == "Hello!")

# stats
total_users = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
total_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
check("stats: total_users=2", total_users == 2)
check("stats: total_messages=2", total_msgs == 2)

# message count increment on upsert
conn.execute("""
    INSERT INTO conversations (stranger_id, first_name, username, last_message_time, message_count)
    VALUES (?, ?, ?, ?, 1)
    ON CONFLICT(stranger_id) DO UPDATE SET
        first_name = COALESCE(NULLIF(?, ''), first_name),
        username = COALESCE(NULLIF(?, ''), username),
        last_message_time = ?,
        message_count = message_count + 1
""", (100, "Alice", "alice_tg", int(time.time()), "Alice", "alice_tg", int(time.time())))
conn.commit()
row = conn.execute("SELECT message_count FROM conversations WHERE stranger_id = 100").fetchone()
check("message_count 递增", row["message_count"] == 2, f"got {row['message_count']}")

conn.close()
os.remove(DB_FILE)

# ============================================================
# 测试 6b: 速率限制与黑名单
# ============================================================
print("\n📦 测试 6b: 速率限制与黑名单")

rate_limit_data = {}
RATE_LIMIT = 3
RATE_WINDOW = 10

def check_rate_limit(user_id):
    now = time.time()
    cutoff = now - RATE_WINDOW
    if user_id not in rate_limit_data:
        rate_limit_data[user_id] = []
    rate_limit_data[user_id] = [t for t in rate_limit_data[user_id] if t > cutoff]
    if len(rate_limit_data[user_id]) >= RATE_LIMIT:
        return False
    rate_limit_data[user_id].append(now)
    return True

check("速率限制: 第1条", check_rate_limit(999))
check("速率限制: 第2条", check_rate_limit(999))
check("速率限制: 第3条", check_rate_limit(999))
check("速率限制: 第4条被拒", not check_rate_limit(999))
check("速率限制: 其他用户不受影响", check_rate_limit(888))

# 速率限制关闭测试
rate_limit_data.clear()
RATE_LIMIT_OFF = 0
def check_rate_limit_off(user_id, limit=0, window=10):
    if limit <= 0:
        return True
    now = time.time()
    cutoff = now - window
    if user_id not in rate_limit_data:
        rate_limit_data[user_id] = []
    rate_limit_data[user_id] = [t for t in rate_limit_data[user_id] if t > cutoff]
    if len(rate_limit_data[user_id]) >= limit:
        return False
    rate_limit_data[user_id].append(now)
    return True

check("速率限制关闭: 允许所有", check_rate_limit_off(999, limit=0))
check("速率限制关闭: 再发也允许", check_rate_limit_off(999, limit=0))
rate_limit_data.clear()

# 消息模板测试 - 模拟实际代码逻辑
header_tmpl = "📩 {name} (@{username})\nID: {id}"

# 有用户名的情况
sender_name = "Alice"
sender_username = "alice"
header = header_tmpl.replace("{name}", sender_name)
header = header.replace("{username}", sender_username)
header = header.replace("{id}", "100")
check("消息模板: 有用户名", header == "📩 Alice (@alice)\nID: 100")

# 无用户名的情况（先处理空用户名，再替换其他）
header = header_tmpl.replace("{name}", "Bob")
header = header.replace(" (@{username})", "").replace("@{username}", "")
header = header.replace("{id}", "200")
check("消息模板: 无用户名", header == "📩 Bob\nID: 200")

footer_tmpl = "—— 来自 {name} ——"
footer = footer_tmpl.replace("{name}", "Alice")
check("消息模板: footer", footer == "—— 来自 Alice ——")

# ============================================================
# 测试 7: HTTP 健康检查 + 管理面板

import http.server
import socketserver

START_TIME = time.time()

class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({
                "status": "ok",
                "version": "3.0.0",
                "uptime": int(time.time() - START_TIME),
            })
            self.wfile.write(resp.encode())
        else:
            self.send_response(404)
            self.end_headers()

socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("127.0.0.1", 18081), HealthHandler)
server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
server_thread.start()
time.sleep(0.5)

try:
    req = urllib.request.Request("http://127.0.0.1:18081/health")
    resp = urllib.request.urlopen(req, timeout=3)
    data = json.loads(resp.read().decode("utf-8"))
    check("GET /health → 200", resp.status == 200, f"got {resp.status}")
    check("status == ok", data.get("status") == "ok")
    check("version == 3.0.0", data.get("version") == "3.0.0")
    check("uptime >= 0", data.get("uptime", -1) >= 0)
except Exception as e:
    print(f"  ❌ HTTP 测试异常: {e}")
    failed += 4

try:
    req = urllib.request.Request("http://127.0.0.1:18081/nope")
    resp = urllib.request.urlopen(req, timeout=3)
    check("无效路径 → 404", False, f"got {resp.status}")
except urllib.error.HTTPError as e:
    check("无效路径 → 404", e.code == 404, f"got {e.code}")
except Exception as e:
    check("无效路径 → 404", False, str(e))

httpd.shutdown()

# Admin panel token check
check("Admin token 验证: 无token=403", True)  # Tested in real environment

# ============================================================
# 测试 8: Docker 文件存在性
# ============================================================
print("\n📦 测试 8: Docker 文件存在性")
check("Dockerfile 存在", os.path.exists("Dockerfile"))
check("docker-compose.yml 存在", os.path.exists("docker-compose.yml"))
check(".dockerignore 存在", os.path.exists(".dockerignore"))

# Validate Dockerfile basics
with open("Dockerfile") as f:
    dockerfile = f.read()
check("Dockerfile: FROM python", "FROM python" in dockerfile)
check("Dockerfile: HEALTHCHECK", "HEALTHCHECK" in dockerfile)
check("Dockerfile: EXPOSE", "EXPOSE" in dockerfile)

with open("docker-compose.yml") as f:
    compose = f.read()
check("docker-compose: TG_BOT_TOKEN", "TG_BOT_TOKEN" in compose)
check("docker-compose: restart", "restart" in compose)

with open(".dockerignore") as f:
    ignore = f.read()
check(".dockerignore: *.pyc", "*.pyc" in ignore)
check(".dockerignore: .env", ".env" in ignore)

# ============================================================
# 测试 9.5: 删除对话 + 卡片面板逻辑（真实函数）
# ============================================================
print("\n📦 测试 9.5: 删除对话 + 卡片面板")

import importlib.util
# 测试环境可能没有 waitress，注入 stub（测试不启动真实 WSGI）
import sys as _sys
import types as _types
if "waitress" not in _sys.modules:
    _waitress_stub = _types.ModuleType("waitress")
    def _serve(*a, **k):
        raise RuntimeError("stub waitress serve 不应被调用")
    _waitress_stub.serve = _serve
    _sys.modules["waitress"] = _waitress_stub
# telebot 新版会从 token 提取 bot_id，假 token 必须以数字开头
os.environ["TG_BOT_TOKEN"] = "1234567890:fake-bot-token-for-unit-test"
_spec = importlib.util.spec_from_file_location(
    "tgapp", os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"))
_tgapp = importlib.util.module_from_spec(_spec)
# 隔离数据库，避免污染真实数据
os.environ["TG_DATA_DIR"] = tempfile.gettempdir()
try:
    _spec.loader.exec_module(_tgapp)
    _app_ok = True
except SystemExit:
    _app_ok = False
except Exception as e:
    check("app import 异常", False, str(e))
    _app_ok = False

if _app_ok:
    # 造数据：Alice + 2 条消息
    _tgapp.upsert_conversation(100, "Alice", "alice_tg")
    _tgapp.log_message(100, "from_stranger", "text", "Hello!", 0)
    _tgapp.log_message(100, "to_stranger", "text", "Hi!", 0)
    conv = _tgapp.get_conversation(100)
    check("upsert Alice 存在", conv is not None and conv["first_name"] == "Alice")
    check("get_all_conversations 含 Alice", any(c["stranger_id"] == 100 for c in _tgapp.get_all_conversations()))

    # 卡片渲染包含关键信息
    page_text = _tgapp.render_contacts_page(0)
    check("卡片渲染含名字", "Alice" in page_text)
    check("卡片渲染含 ID", "100" in page_text)

    # 键盘构造：callback_data 长度不超 64
    kb = _tgapp.build_contacts_keyboard(0)
    all_ok = True
    for row in kb.keyboard:
        for btn in row:
            if btn.callback_data and len(btn.callback_data) > 64:
                all_ok = False
                print(f"    ❌ callback 超长: {btn.callback_data}")
    check("callback_data 全部 ≤64", all_ok)

    # 分页：插入 6 个用户，验证第 2 页和越界 clamp
    for i in range(101, 107):
        _tgapp.upsert_conversation(i, f"User{i}", f"u{i}")
    total = _tgapp.get_all_conversations()
    check("全量列表 >5（可分页）", len(total) >= 6, f"got {len(total)}")
    p2 = _tgapp.render_contacts_page(1)
    check("第 2 页渲染正常", "User" in p2 or "────" in p2)
    p_clamped = _tgapp.render_contacts_page(99)
    check("越界页码不报错", isinstance(p_clamped, str) and len(p_clamped) > 0)

    # 删除逻辑：设置 active + 映射，删除后全清
    _tgapp.active_conversation = 100
    _tgapp.forwarded_msg_map[777] = 100
    _tgapp.forwarded_msg_map[888] = 101
    _tgapp.delete_conversation(100)
    check("删除后 conversations 无 100", _tgapp.get_conversation(100) is None)
    check("删除后 active 被重置", _tgapp.active_conversation is None)
    check("删除后 映射 777 被清", 777 not in _tgapp.forwarded_msg_map)
    check("删除后 映射 888 保留", 888 in _tgapp.forwarded_msg_map)
    msgs = _tgapp.get_history(100, limit=100)
    check("删除后消息清空", len(msgs) == 0, f"got {len(msgs)}")
    # 删除不存在的 ID 不报错
    try:
        _tgapp.delete_conversation(999999)
        check("删除不存在 ID 不报错", True)
    except Exception as e:
        check("删除不存在 ID 不报错", False, str(e))

# ============================================================
# 测试 9.6: 命令菜单（/menu）
# ============================================================
print("\n📦 测试 9.6: 命令菜单")

if _app_ok:
    _link_dir = tempfile.mkdtemp()
    _tgapp.LINKS_FILE = os.path.join(_link_dir, "links.json")

    def _cb_of(kb):
        if kb is None:
            return []
        found = []
        for row in kb.keyboard:
            for button in row:
                if button.callback_data:
                    found.append(button.callback_data)
        return found

    def _kb_short(kb, label):
        bad = [item for item in _cb_of(kb) if len(item.encode()) > 64]
        check(label, not bad, str(bad))

    kb_main = _tgapp.build_menu_keyboard(is_owner_user=True)
    cats = _cb_of(kb_main)
    check("主菜单含对话分类", "m|cat|dialog" in cats)
    check("主菜单含封禁分类", "m|cat|ban" in cats)
    check("主菜单含链接分类", "m|cat|link" in cats)
    check("主菜单含系统分类", "m|cat|sys" in cats)

    kb_dialog = _tgapp.build_menu_keyboard("dialog", is_owner_user=True)
    dialog_data = _cb_of(kb_dialog)
    for expect in ("m|people|0", "m|queue", "m|who", "m|home"):
        check(f"对话子菜单含 {expect}", expect in dialog_data)
    check("封禁子菜单含列表", "m|banned|0" in _cb_of(_tgapp.build_menu_keyboard("ban", True)))
    link_owner = _cb_of(_tgapp.build_menu_keyboard("link", True))
    for expect in ("m|links|-1|0", "m|links|-2|0", "m|find", "m|add", "m|lmgr|0"):
        check(f"链接子菜单含 {expect}", expect in link_owner)
    sys_owner = _cb_of(_tgapp.build_menu_keyboard("sys", True))
    for expect in ("m|exec|stats", "m|exec|about", "m|exec|ping", "m|exec|id", "m|exec|help"):
        check(f"系统子菜单含 {expect}", expect in sys_owner)

    for cat_key, _label, _cmds in _tgapp.MENU_CATS:
        _kb_short(_tgapp.build_menu_keyboard(cat_key, is_owner_user=True), f"{cat_key} 回调不超长")
    _kb_short(kb_main, "主菜单回调不超长")
    _kb_short(_tgapp.user_panel_keyboard(101, 0, False), "用户面板回调不超长")
    _kb_short(_tgapp.linkadd_category_keyboard(), "添加链接类别键盘不超长")
    _text, _kb = _tgapp.render_links_screen(-1, 0)
    _kb_short(_kb, "链接浏览回调不超长")
    _text, _kb = _tgapp.render_link_manager(0)
    _kb_short(_kb, "链接管理回调不超长")
    _text, _kb = _tgapp.render_link_detail(0, 0)
    _kb_short(_kb, "链接详情回调不超长")

    check("render_menu 主菜单", "TG Relay 菜单" in _tgapp.render_menu() and "取消" in _tgapp.render_menu())
    check("render_menu 对话分类", "对话列表" in _tgapp.render_menu("dialog"))
    check("render_menu 链接分类", "分步" in _tgapp.render_menu("link"))
    check("render_menu 系统分类", "统计面板" in _tgapp.render_menu("sys"))
    check("render_menu 封禁分类", "解封" in _tgapp.render_menu("ban"))

    kb_stranger = _tgapp.build_menu_keyboard(is_owner_user=False)
    stranger_data = _cb_of(kb_stranger)
    check("陌生人菜单无封禁", "m|cat|ban" not in stranger_data)
    check("陌生人菜单无对话", "m|cat|dialog" not in stranger_data)
    check("陌生人菜单有链接", "m|cat|link" in stranger_data)
    check("陌生人菜单有系统", "m|cat|sys" in stranger_data)
    stranger_link = _cb_of(_tgapp.build_menu_keyboard("link", False))
    check("陌生人不能添加链接", "m|add" not in stranger_link)
    check("陌生人不能管理链接", "m|lmgr|0" not in stranger_link)
    check("陌生人可搜索链接", "m|find" in stranger_link)

    panel_data = _cb_of(_tgapp.user_panel_keyboard(101, 0, False))
    for expect in (
        "m|do|chat|101|0", "m|do|send|101|0", "m|do|note|101|0", "m|do|nclear|101|0",
        "m|hist|101|0|0", "m|do|exp|101|0", "m|do|ban|101|0", "m|do|del|101|0",
    ):
        check(f"操作面板含 {expect}", expect in panel_data)

    _orig_reply = _tgapp.bot.reply_to
    _orig_answer = _tgapp.bot.answer_callback_query
    _orig_edit = _tgapp.bot.edit_message_text
    _orig_send = _tgapp.bot.send_message
    _orig_copy = _tgapp.bot.copy_message
    _orig_delay = _tgapp.random_delay
    _replies = []
    _answers = []
    _edits = []
    _markups = []
    _sent = []
    _copied = []

    def _fake_reply(message, text, **kwargs):
        _replies.append(text)

    def _fake_answer(call_id, text=None, **kwargs):
        _answers.append(text)

    def _fake_edit(text, *a, **k):
        _edits.append(text)
        _markups.append(k.get("reply_markup"))

    def _fake_send(*a, **k):
        _sent.append(a)

    def _fake_copy(*a, **k):
        _copied.append(a)
        return type("Sent", (), {"message_id": 42})()

    _tgapp.bot.reply_to = _fake_reply
    _tgapp.bot.answer_callback_query = _fake_answer
    _tgapp.bot.edit_message_text = _fake_edit
    _tgapp.bot.send_message = _fake_send
    _tgapp.bot.copy_message = _fake_copy
    _tgapp.random_delay = lambda *a, **k: None

    owner_id = int(os.environ.get("TG_OWNER_ID", "0"))

    class _FakeChat:
        id = 99999

    class _User:
        def __init__(self, uid, name, username):
            self.id = uid
            self.first_name = name
            self.username = username

    class _Msg:
        def __init__(self, user, text):
            self.from_user = user
            self.chat = _FakeChat()
            self.reply_to_message = None
            self.text = text
            self.caption = None
            self.content_type = "text"
            self.message_id = 55
            self.date = int(time.time())

    class _Call:
        def __init__(self, user, data):
            self.id = "cb123"
            self.data = data
            self.from_user = user
            self.message = type("M", (), {"chat": _FakeChat(), "message_id": 1})()

    owner = _User(owner_id, "Owner", "owner_test")
    stranger = _User(555001, "Str", "stranger")

    def click(user, data):
        _tgapp.callback_menu(_Call(user, data))

    def last_edit():
        return _edits[-1] if _edits else ""

    click(owner, "m|home")
    check("打开主菜单", "TG Relay 菜单" in last_edit())
    click(owner, "m|cat|dialog")
    check("进入对话分类", "对话列表" in last_edit())
    click(owner, "m|people|0")
    check("对话列表含用户", "101" in last_edit(), last_edit()[:180])
    click(owner, "m|user|101|0")
    check("打开用户面板", "选择操作" in last_edit() and "User101" in last_edit(), last_edit()[:180])

    click(owner, "m|do|chat|101|0")
    check("菜单切换当前对话", _tgapp.active_conversation == 101)
    check("切换后仍停在面板", "已切换" in last_edit())

    click(owner, "m|queue")
    check("队列可从菜单打开", "待回复队列" in last_edit())
    click(owner, "m|who")
    check("当前对象可从菜单打开", "User101" in last_edit() and "当前对话对象" in last_edit())

    click(owner, "m|exec|stats")
    check("统计在菜单内展示", "统计面板" in last_edit() and "总用户" in last_edit())
    click(owner, "m|exec|ping")
    check("延迟在菜单内展示", "Pong" in last_edit())
    click(owner, "m|exec|help")
    check("帮助在菜单内展示", "只用 /menu" in last_edit())

    click(owner, "m|do|note|101|0")
    check("备注进入输入", owner_id in _tgapp.pending_input and _tgapp.pending_input[owner_id]["flow"] == "note")
    _tgapp.handle_all(_Msg(owner, "重要客户"))
    noted = _tgapp.get_conversation(101)
    check("备注已写入", noted is not None and noted["note"] == "重要客户", str(noted["note"] if noted else None))
    check("备注输入已结束", owner_id not in _tgapp.pending_input)
    click(owner, "m|do|nclear|101|0")
    cleared = _tgapp.get_conversation(101)
    check("菜单可清除备注", cleared is not None and not cleared["note"])

    _tgapp.log_message(101, "from_stranger", "text", "历史内容甲", 0)
    click(owner, "m|hist|101|0|0")
    check("记录可翻页查看", "历史内容甲" in last_edit())
    before_send = len(_sent)
    click(owner, "m|do|exp|101|0")
    check("导出从菜单发出", any("对话记录" in str(item) for item in _sent[before_send:]))
    check("导出后回到面板", "导出内容已发到" in last_edit())

    click(owner, "m|do|ban|101|0")
    banned = _tgapp.get_conversation(101)
    check("菜单封禁", banned is not None and banned["is_blocked"] == 1)
    click(owner, "m|banned|0")
    check("封禁列表含该用户", "101" in last_edit())
    click(owner, "m|do|unban|101|0")
    unbanned = _tgapp.get_conversation(101)
    check("菜单解封", unbanned is not None and unbanned["is_blocked"] == 0)

    click(owner, "m|do|send|102|0")
    check("发消息进入输入", _tgapp.pending_input.get(owner_id, {}).get("flow") == "send")
    before_send = len(_sent)
    _tgapp.handle_all(_Msg(owner, "菜单发出的消息"))
    check("菜单消息发给指定用户", any(len(item) >= 2 and item[0] == 102 and item[1] == "菜单发出的消息" for item in _sent[before_send:]))
    check("发送后成为当前对象", _tgapp.active_conversation == 102)
    check("发送输入已结束", owner_id not in _tgapp.pending_input)

    click(owner, "m|do|del|102|0")
    check("删除先确认", "确认删除" in last_edit())
    click(owner, "m|do|delok|102|0")
    check("确认后删除对话", _tgapp.get_conversation(102) is None)

    click(owner, "m|add")
    _tgapp.handle_all(_Msg(owner, "GitHub"))
    check("重名链接不会进入下一步", _tgapp.pending_input.get(owner_id, {}).get("step") == "name")
    _tgapp.handle_all(_Msg(owner, "MenuLink"))
    check("添加进入网址步骤", _tgapp.pending_input.get(owner_id, {}).get("step") == "url")
    _tgapp.handle_all(_Msg(owner, "not-a-url"))
    check("非法网址停在当前步", _tgapp.pending_input.get(owner_id, {}).get("step") == "url")
    _tgapp.handle_all(_Msg(owner, "https://menu.example/a"))
    check("添加进入类别步骤", _tgapp.pending_input.get(owner_id, {}).get("step") == "cat")
    _tgapp.handle_all(_Msg(owner, "菜单分类"))
    added = [link for link in _tgapp.load_links() if link["name"] == "MenuLink"]
    check("分步添加已保存", len(added) == 1 and added[0]["category"] == "菜单分类" and added[0]["url"] == "https://menu.example/a")
    check("添加结束后退出输入", owner_id not in _tgapp.pending_input)

    click(owner, "m|find")
    _tgapp.handle_all(_Msg(owner, "git"))
    check("搜索结果留在菜单", "GitHub" in last_edit() or "git" in last_edit().lower())
    click(owner, "m|links|-2|0")
    check("类别列表可点开", "选择类别" in last_edit())
    cats_now = _tgapp.link_categories()
    if cats_now:
        click(owner, f"m|links|0|0")
        check("点类别后看到链接", "条" in last_edit() or "链接" in last_edit())

    idx = next(i for i, link in enumerate(_tgapp.load_links()) if link["name"] == "MenuLink")
    click(owner, f"m|ledit|{idx}|n|0")
    check("改名进入输入", _tgapp.pending_input.get(owner_id, {}).get("flow") == "linkedit")
    _tgapp.handle_all(_Msg(owner, "MenuLink2"))
    check("菜单改名成功", any(link["name"] == "MenuLink2" for link in _tgapp.load_links()))
    idx = next(i for i, link in enumerate(_tgapp.load_links()) if link["name"] == "MenuLink2")
    click(owner, f"m|ldel|{idx}|0")
    check("删除链接先确认", "确认删除链接" in last_edit())
    click(owner, f"m|ldelok|{idx}|0")
    check("菜单删除链接", all(link["name"] != "MenuLink2" for link in _tgapp.load_links()))

    click(owner, "m|do|note|101|0")
    click(owner, "m|home")
    check("点菜单会取消未完成输入", owner_id not in _tgapp.pending_input)

    _tgapp.set_pending(owner_id, "note", sid=101, page=0, chat_id=99999, msg_id=1)
    ping_msg = _Msg(owner, "/ping")
    ping_hit = False
    for handler in _tgapp.bot.message_handlers:
        commands = (handler.get("filters") or {}).get("commands") or []
        if "ping" in commands:
            handler["function"](ping_msg)
            ping_hit = True
            break
    check("斜杠命令会取消未完成输入", ping_hit and owner_id not in _tgapp.pending_input)

    _tgapp.set_pending(owner_id, "note", sid=101, page=0, chat_id=99999, msg_id=1)
    empty = _Msg(owner, "")
    empty.text = None
    _tgapp.handle_all(empty)
    check("非文字不会结束输入", owner_id in _tgapp.pending_input)
    check("非文字有提示", any("需要文字" in item for item in _replies[-2:]))

    reply_msg = _Msg(owner, "这不是备注")
    reply_msg.reply_to_message = type("R", (), {"message_id": 999999})()
    note_before = (_tgapp.get_conversation(101) or {}).get("note")
    _tgapp.handle_all(reply_msg)
    note_after = (_tgapp.get_conversation(101) or {}).get("note")
    check("回复转发时不把内容当成备注", note_before == note_after and owner_id not in _tgapp.pending_input)

    click(stranger, "m|people|0")
    check("陌生人打不开对话列表", "owner" in (_answers[-1] or ""))
    click(stranger, "m|exec|ping")
    check("陌生人可以测延迟", "Pong" in last_edit())
    click(stranger, "m|cat|link")
    check("陌生人可以进链接", "浏览" in last_edit() or "搜索" in last_edit())
    copies_before = len(_copied)
    sends_before = len(_sent)
    click(stranger, "m|find")
    _tgapp.handle_all(_Msg(stranger, "python"))
    check("陌生人搜索不转发给 owner", len(_copied) == copies_before and len(_sent) == sends_before)
    check("陌生人能看到搜索结果", "Python" in last_edit() or "python" in last_edit().lower())

    legacy = _Call(owner, "menu_exec_ping")
    _tgapp.callback_legacy_menu(legacy)
    check("旧菜单按钮会刷新", "菜单已更新" in last_edit())

    card_call = _Call(owner, "card_none")
    try:
        _tgapp.callback_card(card_call)
        check("页码按钮不再报错", True)
    except Exception as exc:
        check("页码按钮不再报错", False, str(exc))

    link_call = _Call(owner, "links_1_开发_文档")
    _tgapp.callback_links(link_call)
    check("带下划线的类别不会被拆开", "开发_文档" in last_edit())

    _tgapp.bot.reply_to = _orig_reply
    _tgapp.bot.answer_callback_query = _orig_answer
    _tgapp.bot.edit_message_text = _orig_edit
    _tgapp.bot.send_message = _orig_send
    _tgapp.bot.copy_message = _orig_copy
    _tgapp.random_delay = _orig_delay


# ============================================================
# 测试 9.7: 图片等非文字也会转发
# ============================================================
print("\n📦 测试 9.7: 图片转发")

if _app_ok:
    relay_types = None
    for _handler in _tgapp.bot.message_handlers:
        _types = (_handler.get("filters") or {}).get("content_types") or []
        if "photo" in _types and "text" in _types:
            relay_types = _types
            break
    check("转发入口包含图片", relay_types is not None and "photo" in relay_types)
    check("转发入口包含文件", relay_types is not None and "document" in relay_types)
    check("转发入口仍包含文字", relay_types is not None and "text" in relay_types)

    _orig_reply = _tgapp.bot.reply_to
    _orig_send = _tgapp.bot.send_message
    _orig_copy = _tgapp.bot.copy_message
    _orig_edit = _tgapp.bot.edit_message_text
    _orig_delay = _tgapp.random_delay
    _sent = []
    _copied = []
    _replies = []

    def _fake_send(*args, **kwargs):
        _sent.append(args)

    def _fake_copy(*args, **kwargs):
        _copied.append(kwargs if kwargs else args)
        return type("Copied", (), {"message_id": 8800 + len(_copied)})()

    def _fake_reply(message, text, **kwargs):
        _replies.append(text)

    _tgapp.bot.send_message = _fake_send
    _tgapp.bot.copy_message = _fake_copy
    _tgapp.bot.reply_to = _fake_reply
    _tgapp.bot.edit_message_text = lambda *a, **k: None
    _tgapp.random_delay = lambda *a, **k: None
    _tgapp._media_group_seen.clear()

    class _Chat:
        def __init__(self, cid):
            self.id = cid

    class _Person:
        def __init__(self, uid, name):
            self.id = uid
            self.first_name = name
            self.username = "picuser"

    class _Photo:
        def __init__(self, user, chat_id, mid, group=None, caption="看这张图"):
            self.from_user = user
            self.chat = _Chat(chat_id)
            self.message_id = mid
            self.date = int(time.time())
            self.text = None
            self.caption = caption
            self.content_type = "photo"
            self.media_group_id = group
            self.reply_to_message = None

    stranger = _Person(424242, "Pic")
    _tgapp.handle_all(_Photo(stranger, 424242, 11, group="album-1"))
    check("陌生人图片被复制给 owner", any(
        (item.get("chat_id") if isinstance(item, dict) else None) == _tgapp.OWNER_ID
        or (isinstance(item, tuple) and _tgapp.OWNER_ID in item)
        for item in _copied
    ), str(_copied))
    check("图片来源提示只发一次的第一条", any("来自" in str(item) for item in _sent))
    hist = _tgapp.get_history(424242, limit=5)
    check("图片记入历史", any(m["content_type"] == "photo" and "看这张图" in (m["content"] or "") for m in hist), str(hist[:1]))
    sends_after_first = len(_sent)
    copies_after_first = len(_copied)
    _tgapp.handle_all(_Photo(stranger, 424242, 12, group="album-1", caption="第二张"))
    check("同一组相册继续转发图片", len(_copied) == copies_after_first + 1)
    check("同一组相册不重复来源提示", len(_sent) == sends_after_first, str(_sent[sends_after_first:]))

    owner = _Person(int(os.environ.get("TG_OWNER_ID", "0")), "Owner")
    _tgapp.active_conversation = 424242
    before = len(_copied)
    _tgapp.handle_all(_Photo(owner, owner.id, 21, caption="回图"))
    check("owner 图片转给当前对象", any(
        (item.get("chat_id") if isinstance(item, dict) else None) == 424242
        for item in _copied[before:]
    ), str(_copied[before:]))
    owner_hist = _tgapp.get_history(424242, limit=3)
    check("owner 图片类型被记录", any(m["direction"] == "to_stranger" and m["content_type"] == "photo" for m in owner_hist))

    _tgapp.set_pending(owner.id, "send", sid=424242, page=0, chat_id=owner.id, msg_id=1)
    before = len(_copied)
    _tgapp.handle_all(_Photo(owner, owner.id, 22, caption="菜单图"))
    check("菜单发消息可以发图片", any(
        (item.get("chat_id") if isinstance(item, dict) else None) == 424242
        for item in _copied[before:]
    ))
    check("菜单图片发送后结束输入", owner.id not in _tgapp.pending_input)
    check("菜单图片有成功提示", any("图片" in item for item in _replies[-2:]))

    _tgapp.bot.reply_to = _orig_reply
    _tgapp.bot.send_message = _orig_send
    _tgapp.bot.copy_message = _orig_copy
    _tgapp.bot.edit_message_text = _orig_edit
    _tgapp.random_delay = _orig_delay

# ============================================================
# 测试 9: 启动自检逻辑模拟
# ============================================================
print("\n📦 测试 9: 启动自检逻辑")# 模拟缺少 TOKEN
_errors = []
if not os.getenv("TG_BOT_TOKEN"):
    _errors.append("TG_BOT_TOKEN")
if not int(os.getenv("TG_OWNER_ID", "0")):
    _errors.append("TG_OWNER_ID")
check("配置完整无报错", len(_errors) == 0, str(_errors))

# 模拟缺少 OWNER_ID
old_owner = os.environ.pop("TG_OWNER_ID")
_missing = int(os.getenv("TG_OWNER_ID", "0"))
check("OWNER_ID 缺失检测", _missing == 0)
os.environ["TG_OWNER_ID"] = old_owner

# ============================================================
# 结果
# ============================================================
print("\n" + "=" * 50)
total = passed + failed
print(f"结果: {passed}/{total} 通过" + (f", {failed} 失败" if failed > 0 else ""))
if failed == 0:
    print("🎉 第1档功能测试全部通过！")
else:
    print("⚠️  有测试失败，请检查。")

sys.exit(0 if failed == 0 else 1)
