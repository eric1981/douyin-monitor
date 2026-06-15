# Skill: 项目代码审计与优化工作流

## 适用场景
对一个已有项目做全面代码审计，发现安全性/性能/架构问题，排出优先级，逐项修复并提交。

## 工作流程

### Phase 1: 扫描审计（只读）
1. 用 `git status` 确认仓库状态
2. 用 `find . -type f -not -path './.git/*' ... | sort` 列出所有源文件
3. 逐文件 Read，按以下维度扫描：
   - 安全性：SQL 注入、命令注入、硬编码密钥、路径遍历
   - 正确性：线程安全、资源泄漏、错误处理缺失
   - 性能：N+1 查询、阻塞 I/O、不必要的重复计算
   - 架构：职责混乱、代码重复、循环依赖、动态 import
   - 工程化：测试覆盖、.gitignore 完整性、临时文件清理
4. 输出编号清单，按 高/中/低 优先级分组

### Phase 2: 规划
- 对用户陈述「共 N 项，按优先级分 3 批」
- 每项一句话说明问题和修复方向
- 获得用户确认后进入执行

### Phase 3: 执行
1. `git checkout -b optimize/<name>` 创建分支
2. 按优先级顺序逐项修复
3. 每项修复后验证：
   - `python -c "import <module>"` 确保无语法错误
   - 检查函数签名是否匹配调用方
4. 重大重构（如文件拆分）先创建新文件再删旧文件，确保不丢代码

### Phase 4: 收尾
- 删除残留临时文件（如 `$null`）
- 更新 `.gitignore`
- `git add -A && git commit -m "chore: ..."` 提交
- 分支验证无误后合并回 main

## 常见修复模式

### N+1 查询 → 批量查询
```python
# Before: 循环查 DB
for c in creators:
    stats = get_stats(c["id"])

# After: 一次 JOIN 查询
all_stats = get_all_stats()  # GROUP BY creator_id
for c in creators:
    stats = all_stats.get(c["id"])
```

### 代码重复 → 抽取共享函数
```python
# 两处完全相同的入库逻辑 → db.py
def ingest_crawl_results(creator_id, videos, profile) -> int:
    for v in videos:
        db_id = upsert_video(creator_id, v)
        add_snapshot(db_id, v)
    if profile:
        update_creator_profile(creator_id, profile)
    update_last_fetched(creator_id)
    return len(videos)
```

### 大杂烩文件 → 命令拆分
```
monitor.py (620行 CLI 入口 + 全部实现)
  → monitor.py (68行 入口 + main())
  → commands.py (520行 命令实现，顶层 import)
```

### 网络请求 → 添加重试
```python
async def fetch(self, ..., max_retries=3):
    for attempt in range(max_retries):
        try:
            return await self._fetch_attempt(...)
        except Exception as e:
            delay = (2 ** attempt) + random.uniform(0.5, 2.0)
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
```

### DB 线程安全
```python
conn.execute("PRAGMA busy_timeout=5000")  # 等待而非立即报错
```

### INSERT + SELECT → RETURNING
```python
# Before: INSERT + SELECT (2次查询)
db.execute("INSERT INTO ...")
row = db.execute("SELECT id FROM ... WHERE ...").fetchone()

# After: RETURNING (1次查询, SQLite 3.35+)
row = db.execute("INSERT INTO ... RETURNING id").fetchone()
```

## 检查清单
- [ ] `git status` 干净或目标明确
- [ ] 所有 import 在模块顶层，无动态 import
- [ ] 无 f-string 拼接 SQL（即使白名单也避免）
- [ ] 数据库连接有 `busy_timeout`
- [ ] 共享逻辑已抽取到公共模块
- [ ] `.gitignore` 覆盖所有生成文件/缓存目录
- [ ] 无残留临时文件
- [ ] 所有模块可独立 import 无报错
- [ ] 删除 dead code 而非注释掉
