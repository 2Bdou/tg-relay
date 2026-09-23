# TG 双向匿名中继机器人 v3.2.0

> 保护聊天隐私的 Telegram 中间层。陌生人只能通过 Bot 联系你，双方互不知道真实身份。

## 核心功能

- **匿名中继**：陌生人 DM Bot → 转发 Owner；Owner 回复 → 回传陌生人
- **多对话支持**：同时跟踪多位陌生人，`forwarded_msg_map` 按回复消息自动路由
- **SQLite 持久化**：对话状态、消息历史、封禁列表重启不丢失
- **Flask 健康检查**：内置 `GET /health` 端点，适配免费容器面板
- **Polling / Webhook 双模式**：设 `TG_WEBHOOK_URL` 自动切换
- **交互菜单**：`/menu` 完成对话、封禁、链接和系统操作。只有备注、消息、链接字段和搜索词需要打字，并且可以取消
- **速率限制**：可配置每用户消息频率限制
- **黑名单系统**：`/ban` `/unban` `/banlist`
- **消息模板**：转发格式可自定义（`{name}` `{username}` `{id}` 等占位符）
- **Web 管理面板**：`/admin?token=xxx` 查看统计和活跃对话

## 文件结构

```
tg-relay/
├── app.py          # 主程序
├── app.py               # Pterodactyl 入口（同 app.py）
├── requirements.txt     # Python 依赖
├── .env.example         # 环境变量模板
├── tg-relay.service     # systemd 服务文件
├── Dockerfile           # Docker 构建
├── docker-compose.yml   # Docker Compose
├── .dockerignore
├── relay.db             # SQLite 数据库（自动生成）
├── links.json           # 链接数据（自动生成）
├── test_tg_relay.py     # 模拟测试脚本
├── agent.md             # 功能路线图
└── README.md
```

## 快速开始

### 推荐：VPS 一键菜单部署（Docker）

```bash
curl -sSfL https://raw.githubusercontent.com/alivedou/tg-relay/main/tg-relay.sh -o tg-relay.sh && chmod +x tg-relay.sh && ./tg-relay.sh
```

脚本菜单：`1 项目配置`（Token / Owner / 对外端口）→ `2 安装部署`（默认镜像 `ghcr.io/alivedou/tg-relay:latest`）。  
数据目录默认 `/opt/tg-relay`，可用 `TG_HOME=/path ./tg-relay.sh` 覆盖。

### 方式一：Pterodactyl 面板（推荐免费容器）

1. 上传 `app.py` 和 `requirements.txt` 到面板
2. 设置环境变量：
   - `TG_BOT_TOKEN` — 从 [@BotFather](https://t.me/BotFather) 获取
   - `TG_OWNER_ID` — 从 [@userinfobot](https://t.me/userinfobot) 获取
3. 面板自动安装依赖并启动
4. Bot 通过 `/health` 端点（端口 8080）通过健康检查

### 方式二：VPS + systemd

```bash
mkdir /opt/tg-relay && cd /opt/tg-relay
cp .env.example .env && nano .env   # 填写 TG_BOT_TOKEN 和 TG_OWNER_ID
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp tg-relay.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable tg-relay --now
```

### 方式三：Docker（VPS）

两种路径：**本机构建** 或 **拉取 GitHub Actions 推到 GHCR 的镜像**。

#### 菜单脚本 `tg-relay.sh`（推荐）

```bash
curl -sSfL https://raw.githubusercontent.com/alivedou/tg-relay/main/tg-relay.sh -o tg-relay.sh && chmod +x tg-relay.sh && ./tg-relay.sh
# 或仓库内: chmod +x tg-relay.sh && ./tg-relay.sh
# 自定义目录: TG_HOME=/opt/tg-relay ./tg-relay.sh
```

菜单：`1 项目配置` → 填 `TG_BOT_TOKEN` / `TG_OWNER_ID` / **对外端口 HOST_PORT** → `2 安装部署`（纯 `docker run`）。  
数据默认 `/opt/tg-relay/data`，配置 `/opt/tg-relay/.env`。

#### 0. VPS 前置

```bash
# 装 Docker（Debian/Ubuntu 示例）
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# 可选：当前用户免 sudo
usermod -aG docker $USER
# 重新登录后生效
```

准备目录与配置：

```bash
mkdir -p /opt/tg-relay/data && cd /opt/tg-relay
# 把仓库里的 docker-compose.yml、.env.example 拷过来
# 或 git clone 后 cd 进项目根目录
cp .env.example .env
nano .env   # 至少填 TG_BOT_TOKEN、TG_OWNER_ID
```

`.env` 最小示例：

```env
TG_BOT_TOKEN=123456:ABC-DEF...
TG_OWNER_ID=你的数字ID
TG_PORT=8080
# 可选管理面板
# TG_ADMIN_TOKEN=随机长字符串
# Webhook 模式（有公网 HTTPS 域名时再开）
# TG_WEBHOOK_URL=https://relay.example.com
```

数据卷：`./data` 挂到容器 `/app/data`，持久化 `relay.db`、`links.json`。

---

#### A. 本机 `docker compose` 构建（最简单）

项目根目录执行：

```bash
cd /opt/tg-relay   # 含 Dockerfile、docker-compose.yml、.env
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100
```

常用运维：

```bash
docker compose restart
docker compose pull          # 仅当 compose 用 image: 时有效
docker compose up -d --build # 代码改后重建
docker compose down          # 停容器，保留 ./data
docker compose down -v       # 慎用：会删匿名卷（本项目主要靠 bind ./data）
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/health
# 或
docker inspect --format='{{.State.Health.Status}}' tg-relay-bot
```

---

#### B. 拉取 GitHub 生成的镜像（GHCR）

仓库已带 `.github/workflows/docker-build.yml`：手动触发后推送到  
`ghcr.io/<owner>/<repo>:<tag>`（默认 `latest`）。

**1）GitHub 侧**

1. 代码推到 GitHub
2. Actions → **Docker Build** → Run workflow → tag 填 `latest`（或版本号）
3. 等绿色通过
4. 仓库 → Packages / 右侧 package → 镜像设置：
   - 若 VPS **不登录**就要拉：Package 可见性改 **Public**
   - 若保持 Private：VPS 用 PAT 登录（见下）

**2）VPS 拉镜像（公开包）**

```bash
# 确认镜像名：把 OWNER/REPO 换成你的，如 alice/tg-relay
export IMAGE=ghcr.io/OWNER/REPO:latest
docker pull $IMAGE
```

**3）VPS 拉镜像（私有包）**

```bash
# GitHub → Settings → Developer settings → Personal access tokens
# 经典 PAT 勾选 read:packages（写包另需 write:packages）
echo YOUR_GITHUB_PAT | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
docker pull ghcr.io/OWNER/REPO:latest
```

**4）用拉取的镜像跑（推荐改 compose）**

把 `docker-compose.yml` 里 `build: .` 换成镜像，例如：

```yaml
version: "3.8"

services:
  tg-relay:
    image: ghcr.io/OWNER/REPO:latest   # 改成你的
    # build: .                         # 本机构建时用这个，与 image 二选一
    container_name: tg-relay-bot
    restart: unless-stopped
    ports:
      - "${TG_PORT:-8080}:8080"
    environment:
      - TG_BOT_TOKEN=${TG_BOT_TOKEN}
      - TG_OWNER_ID=${TG_OWNER_ID}
      - TG_PORT=8080
      - TG_LOG_LEVEL=${TG_LOG_LEVEL:-INFO}
      - TG_WEBHOOK_URL=${TG_WEBHOOK_URL:-}
      - TG_WELCOME_OWNER=${TG_WELCOME_OWNER:-}
      - TG_WELCOME_STRANGER=${TG_WELCOME_STRANGER:-}
      - TG_OWNER_CONTACT=${TG_OWNER_CONTACT:-}
      - TG_RATE_LIMIT=${TG_RATE_LIMIT:-0}
      - TG_RATE_WINDOW=${TG_RATE_WINDOW:-10}
      - TG_MSG_HEADER=${TG_MSG_HEADER:-}
      - TG_MSG_FOOTER=${TG_MSG_FOOTER:-}
      - TG_ADMIN_TOKEN=${TG_ADMIN_TOKEN:-}
      - TG_DATA_DIR=/app/data
    volumes:
      - ./data:/app/data
    env_file:
      - .env
```

然后：

```bash
cd /opt/tg-relay
mkdir -p data
docker compose up -d
docker compose logs -f --tail=100
```

**5）手动部署：纯 `docker run`（不写 compose）**

一条命令拉起，环境变量直接 `-e` 传入：

```bash
mkdir -p /opt/tg-relay/data

docker run -d \
  --name tg-relay-bot \
  --restart unless-stopped \
  -p 8080:8080 \
  -e TG_BOT_TOKEN=你的token \
  -e TG_OWNER_ID=你的数字ID \
  -e TG_PORT=8080 \
  -e TG_DATA_DIR=/app/data \
  -e TG_LOG_LEVEL=INFO \
  -e TG_ADMIN_TOKEN=可选管理token \
  -v /opt/tg-relay/data:/app/data \
  ghcr.io/OWNER/REPO:latest
```

说明：

| 参数 | 作用 |
|------|------|
| `-p 8080:8080` | 宿主机 8080 → 容器 8080；宿主机端口占用则改左边，如 `-p 8089:8080` |
| `-e TG_BOT_TOKEN` / `TG_OWNER_ID` | **必填** |
| `-e TG_DATA_DIR=/app/data` | 与下面数据卷路径一致 |
| `-v /opt/tg-relay/data:/app/data` | 持久化 `relay.db`、`links.json` |
| `ghcr.io/OWNER/REPO:latest` | 换成你的 GHCR 镜像名 |

可选环境变量按需追加 `-e`，例如：

```bash
  -e TG_WEBHOOK_URL=https://relay.example.com \
  -e TG_WELCOME_STRANGER=你好，消息会匿名转发 \
  -e TG_RATE_LIMIT=5 \
  -e TG_RATE_WINDOW=30 \
```

也可用 `.env` 文件代替一长串 `-e`：

```bash
mkdir -p /opt/tg-relay/data
# 先写好 /opt/tg-relay/.env（至少 TG_BOT_TOKEN、TG_OWNER_ID）

docker run -d \
  --name tg-relay-bot \
  --restart unless-stopped \
  -p 8080:8080 \
  --env-file /opt/tg-relay/.env \
  -e TG_PORT=8080 \
  -e TG_DATA_DIR=/app/data \
  -v /opt/tg-relay/data:/app/data \
  ghcr.io/OWNER/REPO:latest
```

本机构建镜像时，把最后的镜像名换成本地 tag：

```bash
docker build -t tg-relay:local /path/to/tg-relay
# run 时镜像改为 tg-relay:local
```

运维：

```bash
docker logs -f --tail=100 tg-relay-bot
curl -s http://127.0.0.1:8080/health
docker restart tg-relay-bot
docker stop tg-relay-bot && docker rm tg-relay-bot   # 删容器，数据仍在 /opt/tg-relay/data
```

升级镜像：

```bash
docker pull ghcr.io/OWNER/REPO:latest
docker stop tg-relay-bot && docker rm tg-relay-bot
# 再执行上面同一条 docker run（-v 数据目录不变）
```

**5）升级已部署镜像**

```bash
cd /opt/tg-relay
docker compose pull
docker compose up -d
# 或
docker pull ghcr.io/OWNER/REPO:latest
docker compose up -d
```

---

#### C. 端口、防火墙、Webhook

- **Polling（默认）**：不必对公网开 `8080`；`/health` 可只本机或内网探活。
- **Webhook**：必须公网 HTTPS 打到容器的 `/webhook`。

Nginx 反代示例：

```nginx
server {
    listen 443 ssl http2;
    server_name relay.example.com;
    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`.env` 设：

```env
TG_WEBHOOK_URL=https://relay.example.com
```

改完后：

```bash
docker compose up -d
# 确认 Bot 日志出现 webhook 模式
docker compose logs --tail=50
```

防火墙（仅当需要外网直连 8080 时）：

```bash
# ufw 示例
ufw allow 8080/tcp
ufw reload
```

---

#### D. 排错清单

| 现象 | 处理 |
|------|------|
| 容器秒退 | `docker compose logs`；查 `TG_BOT_TOKEN` / `TG_OWNER_ID` 是否空 |
| `docker pull` 403 | Package 未 Public，或未 `docker login ghcr.io` |
| 镜像名 404 | 核对 `ghcr.io/owner/repo` 大小写；owner/repo 与 GitHub 一致 |
| 数据丢了 | 确认挂了 `./data:/app/data`，且 `.env` 有 `TG_DATA_DIR=/app/data`（compose 已写） |
| 健康检查失败 | `curl 127.0.0.1:8080/health`；看端口映射是否 `${TG_PORT}:8080` |
| Webhook 收不到 | 域名 HTTPS、反代路径、`TG_WEBHOOK_URL` 无尾斜杠问题、Telegram 能否访问你域名 |

备份数据：

```bash
cp -a /opt/tg-relay/data /opt/tg-relay/data.bak.$(date +%F)
```

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|:--:|--------|------|
| `TG_BOT_TOKEN` | ✅ | - | Bot Token |
| `TG_OWNER_ID` | ✅ | - | 主人的 Telegram 用户 ID |
| `TG_PORT` | | `8080` | Flask 端口 |
| `TG_WEBHOOK_URL` | | - | 设置后走 Webhook 模式 |
| `TG_LOG_LEVEL` | | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `TG_WELCOME_OWNER` | | - | Owner 的 /start 自定义内容 |
| `TG_WELCOME_STRANGER` | | - | 陌生人的 /start 自定义内容 |
| `TG_OWNER_CONTACT` | | - | /about 中显示的联系方式 |
| `TG_RATE_LIMIT` | | `0` | 每窗口最大消息数（默认5） |
| `TG_RATE_WINDOW` | | `10` | 速率限制窗口（秒） |
| `TG_MSG_HEADER` | | `默认格式` | 转发消息头部模板 |
| `TG_MSG_FOOTER` | | - | 转发消息尾部模板 |
| `TG_ADMIN_TOKEN` | | - | 管理面板访问 Token |

### 消息模板占位符

```
{name}      — 发送者名称
{username}  — 发送者用户名
{id}        — 发送者 ID
{queue}     — 队列位置
{total}     — 队列总数
```

示例：`TG_MSG_HEADER=📩 {name} (@{username})\nID: {id}\n队列: #{queue}/{total}`

## 交互菜单

发送 `/menu`，按分类点按钮即可完成操作，不必再记 `/note`、`/send`、`/linkedit` 这类带参数的写法。

- **对话**：打开列表，点一个人。可以设为当前对话、发消息、写备注、看记录、导出、封禁、删除。
- **封禁**：查看已封禁的人并解封。封禁入口在每个人的操作面板里。
- **链接**：浏览、按类别查看、搜索、分步添加、修改名称/网址/类别、删除。
- **系统**：统计、关于、延迟、ID、帮助。

陌生人只能看到链接和系统。菜单提示输入时，下一条文字只用于当前步骤；点「取消」或发送任意斜杠命令都会退出。回复某条转发消息时，仍然直接回给那个人。

## 全部命令

| 命令 | 说明 | 权限 |
|------|------|------|
| `/menu` | 交互菜单，完成对话、封禁、链接和系统操作 | 所有人（陌生人仅链接和系统） |
| `/start` | 欢迎信息 | 所有人 |
| `/help` | 帮助 | 所有人 |
| `/ping` | 检查延迟 | 所有人 |
| `/id` | 获取自己的 ID | 所有人 |
| `/about` | Bot 信息 | 所有人 |
| `/links` | 查看链接（分页按钮） | 所有人 |
| `/linkcat <类别>` | 按类别查看 | 所有人 |
| `/linkfind <关键词>` | 搜索链接 | 所有人 |
| `/who` | 当前对话对象 | Owner |
| `/queue` | 待回复队列 | Owner |
| `/chat <序号/ID>` | 切换对话对象 | Owner |
| `/send <ID> <消息>` | 主动发消息 | Owner |
| `/note <ID> <备注>` | 用户备注 | Owner |
| `/history [ID]` | 消息记录 | Owner |
| `/export [ID]` | 导出对话 | Owner |
| `/ban <ID>` | 封禁用户 | Owner |
| `/unban <ID>` | 解封用户 | Owner |
| `/banlist` | 封禁列表 | Owner |
| `/stats` | 统计面板 | Owner |
| `/linkadd <n>;<u>;<c>` | 添加链接 | Owner |
| `/linkedit <旧>;<新>;<URL>;<类>` | 修改链接 | Owner |
| `/linkdel <序号/名称>` | 删除链接 | Owner |

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查，返回状态 JSON |
| `/webhook` | POST | Telegram Webhook 接收（Webhook 模式） |
| `/admin?token=xxx` | GET | Web 管理面板（需 TG_ADMIN_TOKEN） |

## 注意事项

- 多对话自动路由：回复被转发的消息时，Bot 自动识别目标用户
- 封禁用户的消息**静默丢弃**，对方不会收到任何提示
- SQLite 数据库 `relay.db` 和 `links.json` 需确保可写
- Webhook 模式下需配置反向代理（Nginx/Caddy）指向 `/webhook`

## License

MIT
