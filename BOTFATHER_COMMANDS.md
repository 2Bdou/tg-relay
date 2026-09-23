# BotFather 命令配置

在 Telegram 里找 @BotFather → 发送 `/setcommands` → 选择你的 bot，然后粘贴下面的清单。

## 直接复制这段

```
menu - 打开菜单（全部功能）
queue - 待回复队列
who - 当前对话对象
contacts - 对话卡片面板
chat - 切换对话对象
links - 查看链接
note - 备注用户
stats - 统计面板
ping - 延迟测试
id - 我的ID
about - 关于
help - 帮助
```

发送后 BotFather 会回复 `Success!`，命令菜单立即生效。

## 命令说明

| 命令 | 权限 | 说明 |
|------|------|------|
| `/menu` | 所有人 | **首选入口**。对话、封禁、链接、系统都在按钮里完成；陌生人只能看到链接和系统 |
| `/queue` | Owner | 待回复队列，看谁发了消息 |
| `/who` | Owner | 当前对话对象 |
| `/contacts` | 所有人* | 对话卡片面板：切换/删除/拉黑 |
| `/chat <序号/ID>` | Owner | 切换对话对象 |
| `/links` | 所有人 | 查看链接库（分页按钮） |
| `/note <ID> <备注>` | Owner | 给用户加备注 |
| `/stats` | Owner | 统计面板 |
| `/ping` | 所有人 | 延迟测试 |
| `/id` | 所有人 | 显示自己的 ID（不会泄露 owner） |
| `/about` | 所有人 | Bot 信息 |
| `/help` | 所有人 | 帮助 |

> *`/contacts` 在代码里是 owner 专属，陌生人调用会被静默忽略；列表里保留它是因为命令菜单是全局显示的，无法按人隐藏。不想让陌生人看到可以删掉这行。

## 权限说明

- BotFather 的命令列表**全局生效**：陌生人和 owner 看到的是同一份
- owner 专属命令（`queue/who/chat/note/stats` 等）陌生人发过来会被代码里的 `is_owner` 检查拦截，不会执行也不会转发
- 无 owner 检查的公开命令：`start/help/ping/id/about/links/linkcat/linkfind/menu`，都是只读、不泄露 owner 信息

## 精简版（可选）

如果想让菜单更干净，只保留常用的：

```
menu - 打开菜单（全部功能）
queue - 待回复队列
contacts - 对话卡片面板
links - 查看链接
help - 帮助
```

带参命令（`/note /chat /send /ban` 等）不必放进列表。打开 `/menu` 后点按钮就能完成，只有备注内容、要发送的消息、链接字段和搜索词需要打字。
