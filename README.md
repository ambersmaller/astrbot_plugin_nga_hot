# astrbot_plugin_nga_hot — NGA 热帖推送插件

在 QQ 官方机器人群聊中，响应自定义指令，拉取指定 NGA 版面的热门帖子榜
（仅标题 / 发布时间 / 评论数，**不含任何链接**），以 QQ 官方 **Markdown** 消息回复。
全程无需 LLM 参与，纯指令触发。

> ⚠️ **使用范围**：插件仅针对 **QQ 官方机器人（qqofficial 适配器）群聊场景** 开发、
> 测试与验收。私聊（C2C）、QQ 频道等其他场景不做专门适配，行为不在承诺范围内。

## 功能特性

- 多版面多指令：每个版面绑定一个指令，如 `/nga` 拉取艾泽拉斯议事厅、`/dnf` 拉取 DNF 版面；
- 可配置时间窗口（「最近 N 小时」）与评论数 Top N 截取；
- QQ 官方 Markdown 消息（二级标题 + 块引用 + 有序列表 + 加粗 + 斜体），越界语法自动降级纯文本；
- 标题中的网址自动替换为 `[链接]`，消息不含任何 URL（避免 QQ 「不允许发送 URL」拦截）；
- 同版面结果缓存（默认 60 秒），防止多群同时触发重复请求 NGA；
- 全程无 LLM：不调用模型、不注册工具、不消耗额度。

## 安装

1. 将本仓库克隆/下载后放入 AstrBot 的 `data/plugins/astrbot_plugin_nga_hot/` 目录；
2. 依赖：插件依赖 `aiohttp>=3.9`（AstrBot 安装时通常已附带；缺失时 AstrBot 会自动安装 `requirements.txt`）；
3. 在 AstrBot WebUI「插件」中启用并重载插件。

要求 AstrBot **≥ 4.10.4**（`metadata.yaml` 已声明 `astrbot_version: ">=4.10.4"`）。

## 配置说明

在 WebUI「插件 → astrbot_plugin_nga_hot → 配置」中编辑：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `boards` | 模板列表 | 空 | 版面列表，每条含 `board_url`、`board_name`（可选）、`command`、`hours`、`top_n` |
| `boards[].board_url` | string | — | 版面地址，如 `https://bbs.nga.cn/thread.php?fid=7` |
| `boards[].board_name` | string | `""` | 榜单标题显示名，留空显示 `fid=数字` |
| `boards[].command` | string | — | 唤起指令名（不含 `/`），仅中文/字母/数字/下划线，1–20 字符，**不得含空格** |
| `boards[].hours` | int | `24` | 「最近 N 小时」时间窗口，`0` 表示不过滤 |
| `boards[].top_n` | int | `5` | 评论数 Top N 截取数，`0` 表示全部 |
| `nga_uid` | string(secret) | `""` | NGA Cookie `ngaPassportUid` |
| `nga_cid` | string(secret) | `""` | NGA Cookie `ngaPassportCid` |
| `request_timeout` | int | `10` | 请求 NGA 超时时间（秒） |
| `send_mode` | options | `auto` | `auto`（优先 Markdown，失败降级纯文本）/ `markdown` / `text` |
| `cache_ttl` | int | `60` | 同版面结果缓存秒数（`0` 不缓存） |
| `max_title_len` | int | `40` | 标题最大长度，超出截断加 `…` |

### ⭐ 改配置后必须「重载插件」

指令注册发生在插件加载阶段（符合 AstrBot 指令注册规范）：

- **增删版面、修改指令名**：保存配置后，在插件卡片上点 **「重载插件」**，新指令才可使用，
  并会出现在 `/help` 清单中；
- **修改 hours / top_n / send_mode 等参数**：无需重载，下次触发指令时自动生效；
- 启动日志会输出 `已注册 N 条版面指令，跳过 M 条无效配置`，非法配置（指令名含空格、
  地址无 fid、与其他指令重名等）会被跳过并告警，不影响其他版面。

### 如何获取 NGA Cookie

1. 浏览器登录 <https://bbs.nga.cn/>；
2. 开发者工具 → 应用/Cookie → 复制 `ngaPassportUid` 与 `ngaPassportCid` 的值；
3. 分别填入 `nga_uid` / `nga_cid`（WebUI 已遮罩显示，插件不会在任何日志、消息中输出 Cookie）。

Cookie 有有效期，失效后插件会提示「NGA 登录态失效，请更新 Cookie」，重新登录复制即可。

## 使用示例

群里 @机器人 发送：

```
/nga
```

机器人回复（Markdown 渲染）：

```markdown
## 🔥 艾泽拉斯议事厅 · 热帖榜

> 最近 24 小时 · 评论数 Top 5

1. **这赛季集合石大米高层尝试了查分组人…**　💬 45 · 09-16 14:00
2. **他来了他来了，他带着三个球来了**　💬 17 · 09-16 13:40

*数据来自 NGA · 更新于 09-16 14:05*
```

## 错误提示对照

| 场景 | 回复 |
| --- | --- |
| Cookie 未配置 | 请先在插件配置中填写 NGA Cookie |
| Cookie 失效 | NGA 登录态失效，请更新 Cookie |
| 网络超时/失败 | NGA 请求超时/失败，请稍后再试 |
| 版面地址配置错误 | 版面地址配置错误，应为 thread.php?fid=… |
| 时间窗内无帖 | 最近 N 小时内暂无可展示的热帖，试试调大 hours 放宽时间窗口 |
| 消息被频控 | 发送过于频繁，请稍后再试 |
| 榜单超长 | 榜单内容超过 QQ 单条消息长度限制，请在插件配置中调小 top_n 或缩短标题长度 |
| 其他未知异常 | 插件内部错误，请查看日志 |

## 技术说明

- **Markdown 发送路径（P0）**：经事件对象获取 `qqofficial` 平台底层 botpy 客户端，直接调用
  官方消息 API（`msg_type=2`，`markdown.content`，被动消息带 `msg_id`）。群聊自定义 Markdown
  已全量开放，无需申请模板；
- **降级路径**：`send_mode=auto` 时 Markdown 发送失败自动降级纯文本重发一次；
  渲染结果越界白名单语法时强制降级纯文本并记 WARN 日志；
- **Markdown 白名单**：仅使用 `## `、`> `、`1. `、`**加粗**`、`*斜体*`；表格/代码块/链接/
  图片/三级及以下标题等一律不输出；
- **合规**：仅抓取登录用户可见的版面首页列表（标题/时间/评论数），缓存 + 超时控制请求频率，
  消息不含任何 URL。

## 开发与测试

```bash
# 离线单元测试（无需 AstrBot 运行环境，抓取解析使用录制响应）
cd astrbot_plugin_nga_hot
python3 -m pytest tests/ -v
```

测试覆盖：GBK 解码与 `__T` 对象/数组两种结构解析、时间窗 + Top N 过滤与稳定排序、
Markdown 白名单校验、URL 清除、长度控制、纯文本渲染、动态指令注册校验等。

## 许可

仅供个人学习与社群使用。请遵守 NGA 用户协议与 QQ 开放平台运营规范。
