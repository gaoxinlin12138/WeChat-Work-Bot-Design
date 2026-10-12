# 企业微信智能知识库与产品机器人

这是一个面向企业内部员工的企业微信机器人。系统先理解员工目标并生成受控任务计划，
再按顺序查询业务数据，最后基于真实资料合并回答。

当前已经实现：

- 企业微信官方 WebSocket 长连接接入，无需配置公网消息回调；
- 公司制度上传、分页预览、复核发布、替换、回退和逐版本回收；
- 制度关键词与向量混合检索，回答显示资料名称、具体条款和 PDF 页码；
- 按“花洒产品档案”Excel 批量导入产品，维护套装价、主体价、成本价和供应商；
- 同产品型号多规格匹配，以及产品图、主体图、配置图和产品视频发送；
- 回复话术管理、意图识别、问答测试和识别纠错记录；
- PostgreSQL + pgvector、Redis 会话、项目备份及隔离数据库测试。

当前采用单机部署：制度原文件和产品媒体保存在本地磁盘，索引与回收站清理使用 Web 进程内后台任务。尚未接入对象存储、OCR 服务、独立任务 Worker、正式数据库迁移系统或多机共享部署。

## 系统架构

```text
企业微信员工
  ↕ 官方 WebSocket 长连接
机器人进程：app.wecom.long_connection
  ├─ LangGraph 主链：规划 → 执行 → 校验 → 统一回答
  ├─ 产品/媒体 QueryPlan 计划契约 → 既有 V2 执行器
  ├─ PostgreSQL 17 + pgvector 0.8.1
  ├─ Redis 共享会话（生产推荐）
  ├─ OpenAI-compatible LLM：复杂任务规划与答案归纳
  └─ 百炼 text-embedding-v3：1024 维向量

管理员浏览器
  → FastAPI 管理台：http://127.0.0.1:8000/admin
  → 制度、产品、媒体、话术和识别记录管理
```

Web 管理台和企业微信机器人是两个独立进程，正式运行时两者都需要进程守护。机器人长连接只能启动一个相同配置的实例。

## 环境要求

- Python 3.12；
- PostgreSQL 17，并启用 `vector`、`pg_trgm` 扩展；
- Redis，生产环境必须启用；
- 可访问 DeepSeek 与阿里云百炼接口的网络；
- 企业微信机器人 ID 和 Secret。

安装依赖：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

首次创建本地数据库时可运行：

```powershell
.venv\Scripts\python.exe -m scripts.setup_postgres
```

该脚本会交互式读取数据库管理员密码，创建 `wecom_knowledge_bot` 数据库和 `wecom_bot` 专用账号，
启用 `vector`、`pg_trgm` 扩展，并把真正的 `DATABASE_URL` 写入本机 `.database.env`。
不需要手工编写表结构；应用首次连接时会幂等创建和补齐业务表。已有环境不要把初始化脚本当作日常启动或升级命令，
因为它会重设同名应用账号密码。

## 配置

复制 `.env.example` 为 `.env`，在本机填写真实配置。数据库初始化配置保存在 `.database.env`。不要提交、展示或发送这两个文件中的真实密钥。

生产环境至少应正确设置：

```dotenv
APP_ENV=production
ADMIN_API_KEY=
DATABASE_URL=
REDIS_URL=

WECOM_BOT_ID=
WECOM_BOT_SECRET=
WECOM_CORP_ID=
WECOM_SECRET=
WECOM_SALES_DEPARTMENT_ID=4

LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=

EMBEDDING_BASE_URL=
EMBEDDING_API_KEY=
EMBEDDING_MODEL=text-embedding-v3
EMBEDDING_DIMENSIONS=1024

# 复杂请求使用模型生成结构化计划；关闭后仍保留规则计划
TASK_PLANNER_ENABLED=true

# 产品/媒体生产计划阶段启用统一 QueryPlan；关闭可安全回退到原 V2 计划
UNIFIED_QUERY_PLAN_ENABLED=true

# LangGraph 是唯一业务编排器；checkpoint 可按环境独立启用
LANGGRAPH_CHECKPOINT_ENABLED=false
LANGGRAPH_CHECKPOINT_POOL_SIZE=4
```

`ADMIN_API_KEY` 用于保护管理台。远程访问管理台时必须使用 HTTPS 或受控内网入口，不要把 FastAPI、PostgreSQL 或 Redis 端口直接开放到公网。生产环境下未登录的浏览器会被引导到管理台登录页 `/admin/login`，账号固定为 `admin`，密码为本机 `.env` 中的 `ADMIN_API_KEY`；登录成功后签发 12 小时有效的 HttpOnly 会话 Cookie，侧栏底部可退出。自动化脚本改用请求头 `X-Admin-Key: <管理密钥>`，旧版 Basic 认证仍然兼容。密钥只在服务器本机查看，不要发送到聊天或写入截图。

首次建表后不要直接修改 `EMBEDDING_DIMENSIONS`。更换向量维度需要迁移字段并重新生成全部向量。

业务智能体统一由 LangGraph 的
`normalize_input -> create_plan -> execute_task -> verify_results -> finalize_reply` 主链编排，
不再保留“单意图映射单节点”的旧主链。简单请求生成一个任务；包含多个明确目标的请求最多
生成四个只读任务，按依赖顺序执行。确定性状态请求可直接处理；其余请求在模型可用时优先由模型规划，
模型只能从制度、通用资料、产品、媒体、话术和普通对话这些既有领域能力中规划任务，不能规划发布、
修改或删除操作。执行结果先检查来源、权限和完整性，再合并成最终回复；模型不可用、调用失败或计划
解析失败时自动降级为规则计划。

产品查询现在统一使用“QueryPlan 审查 + V2 执行器”链路：模型优先提出结构化意图和 ProductQuery，
模型不可用或失败时由规则计划兜底；生产阶段转换为统一 QueryPlan，完成字段、证据、权限和形状校验后，
再桥接回现有 V2 执行器。
旧的 legacy、shadow、off 产品分支已经删除，不存在两套线上答案；QueryPlan 只负责生产计划契约、
审计和回放，`ProductQuery` 是查询契约，`product.py` + `product_store.py` 是唯一实际产品执行路径。
`LANGGRAPH_CHECKPOINT_ENABLED=true` 会在 PostgreSQL 中初始化 LangGraph checkpoint 表，必须在
数据库备份和隔离环境验证完成后单独开启。企业微信 `msgid` 会作为幂等请求标识，thread id 按企业、
机器人和会话隔离并哈希保存。

真实 checkpoint 恢复验证只接受显式的非生产 `TEST_DATABASE_URL`：

```powershell
$env:TEST_DATABASE_URL="postgresql://test-user:password@localhost/test-db"
.\.venv\Scripts\python.exe scripts\check_langgraph_checkpoint.py
```

脚本不会回落到应用的 `DATABASE_URL`，未配置测试库时会直接拒绝运行。

## 启动

在 PyCharm 中右键项目根目录的 `一键启动项目.py` 并选择运行，或者在顶部运行配置中选择
`一键启动全部服务`。脚本会按需启动 Redis、后端 API（同时提供管理台前端）和企业微信长连接，
避免重复启动，并在管理台可用后自动打开浏览器。

也可使用下面的命令分别启动各进程：

启动管理台：

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端启动企业微信长连接：

```powershell
.venv\Scripts\python.exe -m app.wecom.long_connection
```

常用地址：

- 管理台：<http://127.0.0.1:8000/admin>
- 存活检查：<http://127.0.0.1:8000/health>
- 就绪检查：<http://127.0.0.1:8000/health/ready>
- 开发接口文档：<http://127.0.0.1:8000/docs>

`/health` 返回 `status=ok` 只代表 Web 进程存活。正式可用应以 `/health/ready` 返回 HTTP 200 为准；该接口会检查数据库、必需表、向量维度和模型配置，Redis 与企业微信按当前部署可为 `ok` 或 `not_configured`。模型显示 `configured` 仅代表配置完整，仍需通过真实问答验证。

长连接日志出现 `wecom_long_connection_authenticated` 后，才表示企业微信认证完成。

### Windows 持续运行

当前单机使用 Redis 8.10.1 Windows portable 包。新机器可通过 winget 安装：

```powershell
winget install --exact --id taizod1024.redis-windows-fork --source winget --scope user
```

切换生产配置并注册守护任务：

```powershell
.venv\Scripts\python.exe -m scripts.configure_production
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_runtime_tasks.ps1
```

`configure_production` 会设置 `APP_ENV=production`，并仅在管理密钥为空时生成随机密钥，不会在终端输出密钥。守护脚本创建以下 Windows 计划任务：

- `WeComKnowledgeBot-Redis`
- `WeComKnowledgeBot-API`
- `WeComKnowledgeBot-WeCom`

三个任务在当前 Windows 用户登录后启动。服务子进程退出后，守护脚本会在 5 秒后重新启动；守护脚本自身退出时，任务计划在一分钟后重启。查看状态：

```powershell
Get-ScheduledTask -TaskName 'WeComKnowledgeBot-*' |
    Select-Object TaskName, State
```

Redis 仅监听 `127.0.0.1:6379`，运行数据位于 `%LOCALAPPDATA%\WeComKnowledgeBot\redis`。它只保存带时效的会话和缓存，不替代 PostgreSQL 与项目文件备份。

每次进程启动会在 `logs` 生成带时间戳的 `api_runtime_*`、`wecom_runtime_*` 和 `redis_runtime_*` 日志。当前尚未自动清理历史日志，正式长期运行需要另设日志保留策略。

## 资料管理规则

### 公司制度

制度支持 PDF、TXT 和 Markdown。PDF 入库时会逐页记录原生文本质量、图片数量、文内页码和解析策略，清理重复页眉页脚，并将可识别的业务表格转换为带行列含义的独立检索片段。扫描件或没有可靠文本层的 PDF 会标记为需要另行处理，不会自动调用云端视觉模型。

制度更新流程：

1. 上传新文件，生成待复核版本，当前发布版继续生效；
2. 在管理台分页预览正文和页码；
3. 复核后发布新版，旧发布版自动进入制度回收站；
4. 恢复历史发布版时，仅该版本重新生效，当前发布版进入回收站；
5. 未发布草稿继续保留，删除、恢复、永久删除和到期清理都只影响所选版本。

管理台的“重新解析并切分”会从已保存原文件重建正文、表格片段和检索向量，不改变发布状态。新结果只有完整解析成功后才会原子替换旧索引；失败时保留当前可用索引。

只有 `published` 且 `is_current=true` 的制度参与员工问答。发布后应等待 `embedding_status=ready`，再进行问答验收。

### 产品与媒体

产品以 `花洒产品档案 .xlsx` 的“产品档案”工作表为准。当前共 34 项产品字段：14 项基础资料、19 项配件和备注。系统同时支持配件供应商与采购价分为相邻两列的原始档案布局。可在任意位置额外加入“件数”列。件数指当前库存，接受 0～2147483647 的整数；留空表示未登记，0 表示零库存。无件数列的表格导入后件数为空。

产品列表在价格右侧显示件数；编辑器可手动更新，建议按业务约定每半个月核对一次。保存草稿后须“发布修改”或“保存并发布”，机器人查询“库存多少”“还有多少”“还有多少件”才会使用新件数。系统不会自动推算或扣减库存，也未设置定期自动更新任务。旧产品迁移后件数保持为空，不推测历史库存。

导入后统一保存为草稿，图片和视频不再通过文件名字段匹配，请在产品编辑器中直接上传并关联。已发布产品的编辑先停留在草稿视图，机器人、搜索和媒体发送继续使用发布快照，发布时再一起切换字段、搜索正文和媒体引用。

产品型号采用规范化后的全局唯一约束：去除首尾空格后不区分大小写，草稿、已发布和回收站记录都占用型号。Excel 导入、创建和编辑都会在冲突时给出明确提示；若历史库存在重复型号，初始化会停止并列出冲突，而不会自动合并或删除。机器人仍支持完整型号和唯一尾号引用，并回答套装价、主体价、材质、档位等字段，或发送产品图、主体图、配置图和产品视频。价格统一按不含税、不含运费回答。

成本价和配件采购价属于敏感字段。机器人通过企业微信通讯录中的发送人 `userid` 与部门归属进行字段级鉴权：`WECOM_SALES_DEPARTMENT_ID` 所指部门及全部子部门成员仅可在私聊中查看；其他部门、群聊以及通讯录接口异常时均不展示。公共检索文本和向量不包含成本价、采购价。通讯录同步 Secret 只保存在本机 `.env`，可信 IP 应限制为运行机器或服务器的固定公网 IP。

### 回复话术

话术可以导入、编辑和发布。正式使用前应由业务负责人逐条复核适用场景、员工回复和禁止表达。识别纠错记录只用于后续分析，不会自动修改模型提示词或生产资料。

## 检索与回答

制度和通用知识使用 `knowledge_documents`、`knowledge_chunks`；产品、媒体和话术使用各自结构化数据表。

模型接口采用 OpenAI-compatible 协议：

- 简单请求使用单任务快速计划，复杂请求使用结构化多任务计划；
- 所有模型计划都经过枚举、步数、参数和产品型号校验后才能执行；
- 产品请求统一规划为 `operation / include_models / exclude_models / fields / filters`，模型只输出受限 JSON，Python 负责实体解析、歧义处理、权限检查和确定性数据库查询；
- 单任务产品查询与复合任务使用同一结构化计划，不再通过执行层追加排除关键词分支；模型不可生成 SQL，也不可规划产品写操作；
- 多任务结果按数据查询在前、客户话术在后的顺序执行并统一合并；
- Embedding 正常时使用向量与关键词混合检索；
- 制度向量包含章节路径和内容类型上下文，检索结果保留物理页码、文内页码、章节路径、条款号和正文/表格类型；
- Embedding 故障时降级到关键词检索；
- LLM 故障时返回可核对的原文和引用；
- 没有可靠资料时应明确说明未找到依据，不根据常识编造制度或产品信息。

制度引用面向员工显示“资料名称 + 具体条款 + PDF 页码”，不得显示内部资料序号、证据序号或检索片段编号。

### 企业微信回答格式

回答使用企业微信桌面端和移动端均可稳定显示的 Markdown 子集，不依赖管道表格或 HTML 表格：

- 先给直接结论，再补充必要细节；
- 单个对象且字段不超过三个时使用短句或键值行；
- 多个同类对象、产品清单和对比结果使用“编号 + 名称 + 键值行”；
- 每行最多四个核心字段，字段较多时换行，字段间使用全角分隔符 `｜`；
- 操作流程使用编号步骤，制度解释使用“结论、依据、注意事项”；
- 客服话术保持可直接复制的短消息，不强制转换成表格式内容；
- 空字段不输出，资料缺失时写“未登记”，不得猜测；
- 不向员工输出 JSON、代码块、内部推理或数据库字段名；
- 模型误生成的 Markdown 管道表格会在最终回复阶段自动转换为编号键值块。

## 测试与评测

基础回归不会调用真实模型：

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\ruff.exe check app tests scripts
.venv\Scripts\python.exe -m compileall -q app scripts tests
.venv\Scripts\python.exe -m scripts.evaluate_intent --mode rules
.venv\Scripts\python.exe -m scripts.evaluate_chain --mode fixture
```

数据库集成测试默认跳过。优先配置专用 `TEST_DATABASE_URL`；也可以明确启用当前数据库中的随机隔离 schema：

```powershell
$env:RUN_DATABASE_TESTS = '1'
try {
    .venv\Scripts\python.exe -m pytest
} finally {
    Remove-Item Env:RUN_DATABASE_TESTS -ErrorAction SilentlyContinue
}
```

测试只删除自己创建的 `codex_test_*` schema，不应对生产资料执行写入测试。普通 FastAPI 测试会禁用真实回收站清理、索引和 checkpoint 启动，并拒绝应用工厂连接配置中的业务库或 Redis。

调用当前 DeepSeek 的意图评测：

```powershell
.venv\Scripts\python.exe -m scripts.evaluate_intent
.venv\Scripts\python.exe -m scripts.evaluate_chain --mode model
```

固定样本评测通过不代表所有真实问题都正确。正式上线前必须使用真实已发布资料抽样复测。

当前三份真实制度的检索评测：

```powershell
.venv\Scripts\python.exe -m scripts.evaluate_policy_pdf
```

报告写入 `outputs/policy_pdf_eval_report.json`，检查真实已发布制度的 Top-5 文档、页码、内容和表格命中率，不调用答案生成模型，也不会写入聊天记录。

产品查询验收统一运行“QueryPlan 审查 + V2 执行器”链路：

```powershell
$env:RUN_READONLY_DATABASE_EVAL=1
.venv\Scripts\python.exe -m scripts.evaluate_product_fields
.venv\Scripts\python.exe -m scripts.evaluate_context_inheritance
```

这两项分别检查字段完整性、敏感字段越权和多轮上下文越界；离线回放脚本只用于评估，不会成为第二条线上回答链路。

## 备份与恢复

执行备份：

```powershell
.venv\Scripts\python.exe -m scripts.backup_project --pg-dump 'C:\Program Files\PostgreSQL\17\bin\pg_dump.exe'
```

如果 `pg_dump` 已在 PATH，可省略 `--pg-dump`。备份写入 `outputs/backups/<时间>/`，包含：

- PostgreSQL `public` schema 自定义格式归档；
- 制度原文件；
- 产品图片和视频；
- `COMPLETE.json` 完成标记。

备份不包含环境文件和密钥。没有 `COMPLETE.json` 的目录不能视为完整备份。`pg_restore --list` 只能证明归档目录可读，正式上线前还必须在隔离数据库和独立文件目录中完成完整恢复演练。

## 上线顺序

建议按以下顺序落地：

1. 使用公司专用、长期在线的单机运行；
2. 启用 Redis，设置 `APP_ENV=production` 和强管理密钥；
3. 注册 Windows 守护任务，实现用户登录启动和异常重启；
4. 导入并复核真实制度、话术、产品、图片和视频；
5. 验证制度引用、同编号产品选择以及企业微信图片/视频发送；
6. 完成隔离恢复演练和异机备份；
7. 先向 5～10 名员工灰度开放，观察识别记录和失败问题；
8. 连续稳定运行后再逐步扩大使用范围。

当前守护任务需要指定 Windows 用户完成登录后才启动；若目标服务器要求“无人登录也运行”，应改为 Windows 服务或系统级启动任务。自动备份、历史日志清理和故障告警仍需另行配置。

多实例部署前必须先处理共享文件存储、数据库迁移、后台任务去重和独立 Worker。当前本地磁盘方案不能直接作为多机部署方案。

## 项目文档

- [业务资料助手架构与工程说明](docs/业务资料助手-架构与工程说明.md)：生产链路、LangGraph、QueryPlan、查询形状、治理、评测和完整文件结构。
- [部署与运维指南](docs/部署与运维指南.md)：生产环境准备、验收、备份与恢复细则。
- [产品说明](docs/产品说明.md)：产品目标、用户、能力边界和设计原则。
- [界面设计系统](docs/界面设计系统.md)：管理台视觉、布局和组件规范。
