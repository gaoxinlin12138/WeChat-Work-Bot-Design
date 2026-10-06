# 审查修复交接 · 2026-09-24 13:45（Asia/Shanghai）

## 目标和授权

用户先要求完整审查项目，已交付 `PROJECT-AUDIT-2026-09-24.md`。随后问修复计划，接受了“先测试隔离和备份 → 三项数据一致性 → 企业微信故障降级 → 回答/管理台 → checkpoint 和验收”的计划，并明确说“好的，开始吧”。这是继续修复的授权，不要重新询问是否开始。

用户 AGENTS.md 要求长任务主动在同项目新建任务，完整交接后继续，不依赖自动压缩。这是本次新任务的原因。用户未授权发企业微信/邮件消息；不得给真实员工发测试消息。不要展示 `.env`、`.database.env` 或连接凭据。不要删除真实资料。没有 Git 仓库，不要假设有可回退提交。当前线上服务未由本任务主动重启或部署。

工作目录 `E:\Python_work\企业微信设计`，Windows PowerShell，Python `.\.venv\Scripts\python.exe`，ruff `.\.venv\Scripts\ruff.exe`，node 可直接用。网络与文件完全访问，审批策略 never，不传 sandbox_permissions。不得主动派子代理。

## 最紧急：先查清测试触达业务库的问题

虽然加了 `tests/conftest.py` 禁用 `bootstrap_langgraph_checkpoint`，**收尾只读查询发现 public.product_records 已有 published_snapshot 列**。该列是本轮才加入源码的，说明本轮某条初始化路径仍到达了工作数据库。没有主动运行生产初始化或迁移命令；原因尚未查明。正在运行的 uvicorn 进程命令未见 `--reload`，有两个 uvicorn、两个 bot 相关 Python 进程（可能为 launcher/child，不能据此认定重复服务）。不要随意停止它们。

193 项全量测试通过，但不能把“通过”当隔离完好证据。进一步测试前，应：

1. 检查所有 fixture/test 调用未替换的 `get_business_store/get_business_agent/get_rag_agent`；特别注意默认 lifespan 以外的路由、媒体缓存、fake store 缺少方法导致回退等路径。
2. 查验 isolated_database_url 的 search_path 和 initialize 的 `CREATE IF NOT EXISTS/ALTER/DROP` 是否意外解析到 public；真实单测中也可能有裸工作库连接。当前临时 schema 全部清理，不能还原数据库日志事实就不要猜结论。
3. 增加默认测试禁止工作库/Redis访问的硬保护，只对显式独立 schema 测试开放，避免仅禁几个后台函数。不得输出完整 DSN。
4. **pytest 失败输出曾自动展示 isolated_database_url fixture 的明文密码**，不要重复或引用这个值。之后数据库用例用 `--tb=short` 或更强的安全输出，并修复 fixture 表示/测试报告脱敏。告知用户实际影响时简洁如实；不要自动轮换凭据导致运行服务掉线。

已向用户明确说明发现隔离仍不完整，停止再跑测试并先备份；不要再声称工作库未变化。

备份已成功生成：`outputs/backups/20260924-134234-470888/`，这是**部分代码修改与疑似初始化之后的当前状态备份，不是修复前备份**。命令：

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m scripts.backup_project --pg-dump 'E:\software\PostgreSQL\17\bin\pg_dump.exe'
```

输出成功，有数据库 custom dump、上传文件与 COMPLETE.json。未做恢复演练。最初承诺先备份但实际修改先于备份，不要虚称执行顺序。

## 已修改的代码（全部共享工作目录内，未提交）

这些是**不完整中间实现**，不能直接部署。必须重新审查和完善，尤其产品快照。

### app/business/product_store.py

- 新增 `_PRODUCT_FIELDS`, `_published_values`（Decimal 转 float 的 JSON 安全值）, `_active_projection`。
- `set_product_status` 锁行，active 时存 `published_snapshot`（产品字段 + media_ids），draft 时保留旧快照；**尚未与搜索文本原子更新**。
- `build_product_search_text` 对 active 读取发布快照；空价格“资料中未登记”，有值格式 `.2f`。
- `get_active_product_record` 读取字段快照并按 snapshot.media_ids 过滤当前媒体。
- `get_active_product/list_active_products/search_products` 部分采用快照字段。
- 永久删除添加 `FOR UPDATE`、DELETE 状态检查；可选 `expected_deleted_at`，自动清理扫描时间戳后删除时匹配，避免恢复/重新入站被旧批次删除。

**仍有的严重缺口：**

1. `search_products` 的内容仍读取 `product_search_documents.search_text`，发布和索引不同事务；新发布/失败索引/并发异步写回都可能不一致。应一次事务保存发布快照和公开搜索文本，延迟 embedding 不覆盖新文本。上次 added DB test 只测 build_product_search_text，不测真实 search，覆盖不足。
2. `_format_product_detail` 在 `app/business/tools/product.py` 仍直接 `get_product_record` 读取成本与配件！仅 `_component_summary` 的独立 fallback 改为 getter，但 detail 传进来的 record 是草稿，会绕过发布隔离。必须统一机器人所有读路径（含成本、配件、媒体、目录标题）使用同一发布视图。
3. `media_for_product` 仍直接读取 get_product_record，未使用快照过滤；草稿媒体可能在机器人端出现。
4. 当前媒体是按 live links 过滤，旧媒体替换/删除会先删除 link/asset/实际文件，快照 media_ids 并不能保留发布中的文件。因此快照设计须把发布媒体关系作为真正引用，保护资产直到发布切换/永久删除，并与现有 orphan cleanup/backup 路径兼容。不能只补一行 getter 视为完整修复。
5. `published_snapshot` 初始化迁移 `to_jsonb(product_records)` 会带入全部元数据/自身空字段，缺 media_ids；仅初始化 active，没有覆盖 `status='deleted', deleted_from_status='active'` 的旧产品恢复。需要清晰的兼容迁移策略，**先备份、隔离演练**。
6. 草稿 pending 标识/UI尚无；active 产品已改草稿后列表仍显示 active 的新字段，用户看不出尚待发布；应显示待发布修改和正确发布按钮语义。
7. 格式金额目前 JSON 用 float，可考虑序列化为字符串或 Decimal 统一，避免金额精度损失。

### app/business/store.py

- 新建 product_records SQL 增 `published_snapshot jsonb`，`ALTER ADD IF NOT EXISTS`，active 行空快照回填 `to_jsonb(product_records)`。
- 话术初始化新增 ranked 去重，把重复的 current published 历史行转 deleted，保留最高 version/updated_at/id；再建 partial unique index `reply_scripts_one_current_published_idx`。
- `publish_script` 调整先将 sibling 转回收站，再发布目标，避免唯一约束冲突。
- `restore_script` 无 live 版本时只恢复目标 id，不恢复全 logical group。

**仍需：**发布/恢复同 logical_id 并发事务锁，当前只有唯一索引兜底，可能冲突/死锁/意外复活；ranked 启动自动改历史状态是否合适需评估，别无记录偷偷修复真实数据。补并发真实DB测试。已有后续删组/恢复编辑行为需确认新的“只恢复选中版本”与UI一致。

### app/business/tools/product.py

- `_component_summary` fallback getter尝试 `get_active_product_record`，但主要 detail 路径漏改（见上）。
- 多字段循环从命中即 return 改为收集全部 content 后 join。
- **需修**“主体多少钱”会同时匹配通用“多少钱”，意外带出套装价；敏感价格 early return 仍漏同句公开字段，按权限分别答敏感与公开。补组合问法回归。

### app/wecom/directory.py

- `_load_snapshot` 外包 try，把 httpx.HTTPError/ValueError/TypeError/KeyError 转 DirectoryAccessError；仍20秒超时，没有失败短缓存，HTTP响应也没raise_for_status/结构检查，AttributeError可能逃逸（调用方现已统一兜底）。
- known_ids 多行缩进可让 formatter整理。

### app/wecom/long_connection.py

- 私聊成本权限鉴权 `except Exception`，失败继续普通问答、敏感价格拒绝；删除未用 DirectoryAccessError import。
- 图片发送记录 failed 次数，全失败通过 send_message 通知，失败则 reply_stream；部分失败通知。
- **仍需重新设计/测**现在仍先结束“正在发送”文字，再发送媒体。全部成功仍只有旧文字；stream场景最好保持进度到结果确认，再最终说明成功/部分/失败，原帧失效用现有 fallback。无目标导致未发送分支应计失败；send_notice失败不能静默假成功。不能给真人发测试消息。
- 上传图片大小/像素与发送不一致尚未修。

### app/admin/static/app.js

- state 新加 editingProductOriginal。
- **意外回归必须立即修：替换 state 行时漏掉了原来的 `audits:[]`！** 恢复。
- 未修健康状态只加载一次、randomUUID安全上下文兼容、问答GET日志问题。

### app/admin/static/product-admin.js

- 编辑时保存隐藏 style/source_row 到 editingProductOriginal，提交保留；clearForm清空。
- 其余发布状态/媒体替换文案尚未跟快照隔离同步。
- frontend 改动不需要重设计；前轮已读 impeccable skill，若用需遵守，不要把此任务扩大为重设计。

### tests/conftest.py

- autouse fixture 新 monkeypatch main.bootstrap_langgraph_checkpoint 为 noop；**不足以隔离工作库，见顶部。**

### app/business/graph/{state,builder,reply_script}.py 和 app/business/service.py

- 将 user_id 从 BusinessGraphState/ReplyScriptGraphState 移除；BusinessGraphContext 新增必填 user_id。
- builder/reply_script 所有 user_id 改读 runtime.context.user_id，service invoke输入不带 user_id，context携带。
- 新建 checkpoint 不再主动写原始user_id；旧checkpoint记录未删除，旧数据恢复后是否被下一checkpoint继续带入需要测试。
- 文档还没改，不要声称历史数据已清理。模型输出/员工正文仍会存入checkpoint，不能说所有PII都没了。

### 测试变化

- tests/test_database_integration.py 新增2个真实DB回归：快照字段/草稿媒体过滤，话术恢复只指定版本。
- tests/test_script_edit.py 原先断言“先发布再回收”、“恢复整个逻辑组”的fake SQL测试改成新语义。
- tests/test_business_graph.py checkpoint断言从 user_id 为原值改为不存在。
- 尚未为并发清理、图片新反馈、通讯录网络降级、多字段、空价、上传限制等补完整回归；不要只改旧断言后就称覆盖充足。

## 验证结果和当前服务

- 最近 full run `RUN_DATABASE_TESTS=1 python -m pytest -q`：**193项全部通过**（原191+新增2）；结束exit 0，session 16370 已结束。
- 之前 lint因tests/test_script_edit.py长行失败，已手工换行但尚未重新跑最后一次ruff；之前业务代码ruff通过。
- Python compileall通过；两个JS node --check通过（不代表运行逻辑没问题，audits回归就漏掉了）。
- 真实模型72/72是**审查阶段原始代码结果**，修复后还没重新评测。
- 当前 `/health/ready` HTTP200，public.product_records.published_snapshot存在；所有codex_test/codex_audit随机schema剩余0。
- **没有完成部署/重启和新功能真实服务验收**；不要把原服务ready归功于修复。

## 原审查10项与剩余工作

完整证据和位置见 `PROJECT-AUDIT-2026-09-24.md` 与 `outputs/project-audit-20260924/`。

1. F01 产品并发清理误删：代码初修，缺并发回归。
2. F02 产品草稿改线上/搜索追问价不一致：**初实现不足，最高优先完善**。
3. F03 话术恢复双发布：单流程测试通过，缺并发锁与迁移验收。
4. F04 企业微信通讯录60020可信IP：尚未修外部配置；不能靠代码解决，需要到最后明确剩余用户配置事项。不要擅自更改账号敏感设置。
5. F05 通讯录超时阻断普通问答：初修，需错误类型/短失败缓存/超时/运行状态测试。
6. F06 图片全失败假成功：初修需完善，上传/发送限制未统一。
7. F07 多字段漏答：初修，组合问题及敏感字段仍需测。
8. F08 None元：已格式化，需覆盖零价/空价/搜索和详情一致。
9. F09 测试触达工作库：**未解决且发现实际新增schema字段，先硬隔离再测试**。
10. F10 checkpoint user_id：运行时上下文实现通过当前测试，需旧数据兼容、明确历史保留方案及文档。

用户接受的计划还包含无关引用、管理台健康刷新与隐藏字段保留、备份恢复和实际验收。无关引用（不存在的检测报告拒答却引用考勤）还没改，目录短失败缓存/健康状态还没改。

## 建议新任务执行顺序

1. 读本交接、审查报告、当前相关代码（小范围，避免重复巨量输出），立即恢复 app.js audits:[]。封堵并查清测试数据库触达工作库路径，确保不再泄露DSN。
2. 已有当前状态备份，验证dump可读并为接下来的代码留明确基线（不要声称是原始版本）。设计完整发布快照与媒体引用方案，补原子发布和统一机器人读路径。
3. 补真实数据库状态/并发回归，在隔离schema里验证；修话术锁与清理锁。完善其余已授权修复。
4. 确认每个变更确实覆盖审查触发条件，适度测试，完成完整suite、模型/fixture评测。失败时安全日志，禁止凭据展示。
5. 更新修复报告/部署与隐私说明，按实际授权与风险安排运行服务切换（不要在未验证时重启）。如需操作账号可信IP或真实发消息，说明具体待办；代码与reviewable结果先完成，不提前卡在确认。
6. 前轮 IAB访问 http://127.0.0.1:8000/admin 被 net::ERR_BLOCKED_BY_CLIENT，不要绕过工具限制。可以在新任务依工具允许检查是否现可访问，但不要把源码/语法检查说成真实浏览器验收。

## 沟通与已告知用户

最后更新已如实告知：193测试通过但发现真实schema变化；停止测试、当前状态已备份、服务200；先查清隔离，再继续功能修复。此前一条过早称第一批“已落地”，已纠正为未完成、不能部署。保持中文，短更新，不要再次大段重述审查或重复要求用户授权。
