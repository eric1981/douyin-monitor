# 抖音博主监控系统

定时抓取抖音博主视频数据，追踪点赞、评论、分享、播放量等互动指标的变化趋势。

## 功能

- **博主管理** — 添加/删除/重命名监控目标，支持多种输入格式
- **数据采集** — Playwright 无头浏览器模拟滚动，拦截 API 响应收集完整视频列表
- **趋势追踪** — 每次抓取生成独立快照，对比历史数据发现涨粉/爆款
- **语音转录** — faster-whisper 离线转写视频语音为文字（简体中文）
- **Web 面板** — FastAPI + Jinja2 页面，概览仪表盘、视频列表、趋势图 (Chart.js)
- **CLI 工具** — 命令行增删查改、单次抓取、定时调度、CSV 导出
- **多格式输入** — 支持纯 sec_uid、主页链接、短链接 (v.douyin.com)、分享文案

## 技术栈

| 层 | 技术 |
|---|------|
| 数据采集 | Python 3.8+、Playwright (Chromium)、asyncio |
| 数据存储 | SQLite (WAL 模式, 外键) |
| 语音转录 | faster-whisper (large-v3-turbo)、ffmpeg、zhconv |
| Web 后端 | FastAPI、Jinja2、Uvicorn |
| Web 前端 | 原生 HTML/CSS/JS、Chart.js 4.x (CDN) |
| 配置 | YAML |

## 项目结构

```
douyin-monitor/
├── monitor.py            # CLI 入口（路由到 commands.py）
├── commands.py           # 命令实现（add/remove/list/run/report/export 等）
├── spider.py             # 抖音数据采集模块 (Playwright 滚动+API拦截)
├── douyin_spider.py      # 独立抓取脚本（可单独运行，导出 JSON）
├── db.py                 # SQLite 数据层（CRUD、快照、统计、批量查询）
├── utils.py              # URL 解析工具（sec_uid 提取、短链接重定向）
├── transcriber.py        # 语音转录模块（视频下载→音频提取→Whisper 转文本）
├── check_data.py         # JSON 数据完整性检查工具
├── test_web.py           # Web API 冒烟测试 (pytest)
├── config.yaml           # 配置文件（博主列表、调度参数）
├── requirements.txt      # Python 依赖
├── SKILL_CODE_AUDIT.md   # 代码审计工作流 Skill 文档
├── data/                 # SQLite 数据库文件
├── exports/              # CSV 导出目录
├── douyin_session/       # Playwright 持久化浏览器会话（登录态）
└── web/
    ├── app.py            # FastAPI Web 应用
    ├── static/
    │   └── style.css     # 页面样式
    └── templates/
        ├── base.html         # 布局基模板（导航栏）
        ├── dashboard.html    # 概览仪表盘
        ├── creators.html     # 博主管理（增删改查）
        ├── videos.html       # 视频列表（搜索/排序/分页）
        ├── video_detail.html # 视频详情（快照历史、转录文本）
        └── trends.html       # 趋势图（Chart.js 折线图）
```

## 数据库 Schema

### creators — 博主

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增主键 |
| name | TEXT | 博主名称（支持双击改名） |
| sec_uid | TEXT UNIQUE | 抖音用户唯一标识 |
| platform | TEXT | 平台 (默认 `douyin`) |
| enabled | INTEGER | 是否启用监控 (1/0) |
| added_at | TIMESTAMP | 添加时间 |
| last_fetched_at | TIMESTAMP | 最近一次抓取时间 |

### videos — 视频

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增主键 |
| creator_id | INTEGER FK | 关联 creators.id |
| video_id | TEXT | 抖音视频 ID |
| title | TEXT | 视频标题 |
| cover_url | TEXT | 封面图 URL |
| video_url | TEXT | 视频播放地址 |
| duration_ms | INTEGER | 时长 (毫秒) |
| create_time | INTEGER | 发布时间 (Unix 时间戳) |
| hashtags | TEXT | 标签列表 (JSON 数组) |
| first_seen_at | TIMESTAMP | 首次发现时间 |

UNIQUE(creator_id, video_id)

### snapshots — 互动快照

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增主键 |
| video_id | INTEGER FK | 关联 videos.id |
| like_count | INTEGER | 点赞数 |
| comment_count | INTEGER | 评论数 |
| share_count | INTEGER | 分享数 |
| view_count | INTEGER | 播放量 (Web 端通常为 0) |
| fetched_at | TIMESTAMP | 抓取时间 |

### transcripts — 转录文本

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增主键 |
| video_id | INTEGER FK | 关联 videos.id |
| full_text | TEXT | 完整转录文本（简体中文） |
| created_at | TIMESTAMP | 转录时间 |

每次抓取都会新增快照，通过对比相邻快照计算增长趋势。

## 采集原理

### Cookie 依赖

抓取模块不直接调用抖音 API，而是用 Playwright 打开博主主页，拦截页面自动发出的 API 请求。

```text
用户浏览器             抖音服务器
    │                      │
    ├─ GET /user/SEC_UID ─→│   (带上 Cookie)
    │←─ HTML 页面 ─────────┤
    │                      │
    ├─ 页面 JS 自动发请求 ──→│
    │  /aweme/v1/web/aweme/post/
    │  (签名/参数由页面 JS 生成)
    │←─ JSON (视频列表) ────┤   ← spider 拦截这个响应
```

抖音的 `/aweme/v1/web/aweme/post/` 接口**必须携带有效的登录 Cookie** 才会返回视频数据，未登录状态下抖音只返回公开页面的少量信息。

### 会话持久化

Playwright 使用 `launch_persistent_context` 将浏览器状态（Cookie、LocalStorage、IndexedDB）持久化到 `douyin_session/` 目录。每次抓取复用同一会话，无需重复登录。

```
douyin_session/
├── Default/
│   ├── Cookies              ← 登录 Cookie 存储在这里
│   ├── Local Storage/
│   ├── IndexedDB/
│   └── Network/
└── ...
```

### Cookie 失效与续期

抖音 Web 端 Cookie 有效期通常约 **7~30 天**，失效后抓取会返回 0 条数据。重新扫码登录即可恢复：

```bash
python monitor.py login
```

登录完成后可用以下命令验证：

```bash
# 空跑一个已知博主，看是否有数据
python monitor.py run --creator 1
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt

# 安装 Playwright 浏览器（Chromium）
python -m playwright install chromium
```

### 2. 扫码登录（只需一次）

```bash
python monitor.py login
```

弹出浏览器窗口，扫码登录抖音。会话保存在 `douyin_session/` 目录，后续无需重复登录。

### 3. 添加博主

```bash
# 方式一：sec_uid
python monitor.py add MS4wLjABAAAAdu8I0mIDajXSXZZvtY79MXGRL83G5FvMlH1hxcIteEx

# 方式二：主页链接
python monitor.py add "https://www.douyin.com/user/MS4wLjAB..."

# 方式三：分享短链
python monitor.py add "https://v.douyin.com/xxxxx/"

# 方式四：分享文案（自动提取链接）
python monitor.py add "长按复制此条消息，打开抖音搜索... https://v.douyin.com/xxxxx/"

# 指定名称
python monitor.py add <url> --name 野山坡
```

### 4. 抓取数据

```bash
# 抓取所有启用的博主
python monitor.py run

# 抓取指定博主
python monitor.py run --creator 1
```

### 5. 查看报告

```bash
# 所有博主汇总
python monitor.py report

# 指定博主
python monitor.py report --creator 1
```

### 6. 启动 Web 面板

```bash
python monitor.py web --port 8080
```

访问 `http://127.0.0.1:8080`

页面功能：
- **概览** — 各博主视频数、总时长、最高点赞、最近更新
- **博主管理** — 添加/双击改名/删除/单独抓取
- **视频列表** — 按点赞排序、标题搜索、分页浏览
- **视频详情** — 快照历史、转录文本
- **趋势图** — 点赞数变化折线图 (Chart.js)

### 7. 定时调度

```bash
python monitor.py schedule
```

默认每 4 小时 (±15 分钟随机抖动) 自动抓取一轮。配置在 `config.yaml` 中调整。

### 8. 语音转录

```bash
# 转录所有待处理的视频
python monitor.py transcribe

# 转录指定博主、限制条数、指定并行数
python monitor.py transcribe --creator 1 --limit 20 --workers 2
```

转录依赖 `faster-whisper` 和系统 `ffmpeg`。首次运行会自动下载模型（约 3GB）。

> 注意：转录需要获取视频的 m3u8 流地址，部分视频可能因签名校验失败。此时需先登录浏览器，再运行 `python monitor.py export-cookies` 导出 cookies 供转录模块使用。

### 9. 导出 CSV

```bash
# 导出所有博主
python monitor.py export

# 导出指定博主
python monitor.py export --creator 1
```

文件生成在 `exports/` 目录。

## 命令行参考

```
python monitor.py add <url或sec_uid> [--name <名称>]     添加博主
python monitor.py remove <ID或名称或sec_uid>              删除博主
python monitor.py list                                     列出所有博主
python monitor.py run [--creator <id>]                     运行抓取
python monitor.py report [--creator <id>]                  查看报告
python monitor.py schedule                                 启动定时调度
python monitor.py login                                    扫码登录
python monitor.py login --slot 1                           多槽位登录
python monitor.py export-cookies                           导出浏览器 cookies
python monitor.py transcribe [--creator <id>] [--limit N]  转录视频语音
python monitor.py export [--creator <id>]                  导出 CSV
python monitor.py web [--port <port>]                      启动 Web 面板
```

## 配置 (config.yaml)

```yaml
# 博主列表（也可通过 CLI / Web 面板管理）
creators:
  - name: "野山坡"
    sec_uid: "MS4wLjABAAAA..."
    platform: "douyin"
    enabled: true

# 调度
schedule:
  interval_minutes: 240      # 抓取间隔
  jitter_minutes: 15         # 随机抖动（反爬）
  daily_at: null             # 或设为 "03:00" 每天定时执行

# 采集参数
spider:
  max_scrolls: 80            # 最大滚动次数
  headless: true             # 无头模式
  page_load_wait: 8          # 首页加载等待 (秒)
  scroll_idle_limit: 20      # 连续无新数据的滚动上限

# 输出
output:
  export_dir: "./exports"
  auto_export_csv: true      # 每次 run 后自动导出
```

## Windows 注意事项

本项目通过 `WindowsProactorEventLoop` 隔离 Playwright 浏览器进程，避免与 Uvicorn 的事件循环冲突。无需额外配置。

## License

MIT
