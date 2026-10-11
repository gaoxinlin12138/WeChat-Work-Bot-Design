# LangGraph 智能体架构

## 当前状态

- 2026-09-24 已在随机临时 schema 中完成真实集成回归；当前全套回归为 194 项（含数据库用例），
  临时 schema 已由测试夹具清理。测试默认禁止应用工厂连接配置中的业务库和 Redis。
- Checkpoint 改造已完成代码和隔离回归，但本次任务没有重启或部署现有服务；是否启用生产开关、
  当前服务 readiness 与现网日志必须按部署后的实际结果单独确认。
- 已引入 `langgraph>=1.2.12,<2.0`。
- 2026-09-26 已将旧的单意图固定分流主链替换为
  `normalize_input -> create_plan -> execute_task -> verify_results -> finalize_reply`。
  简单请求为单任务计划；复合请求最多四个任务，并在同一个执行节点中循环调用既有领域工具。
- 任务规划模型只输出结构化只读任务。产品任务使用 `operation / include_models / exclude_models / fields / filters`；Python 负责 Schema 校验、数据库驱动的型号解析、歧义澄清、权限和执行边界校验；
- 简单产品请求与复合请求共用同一产品任务协议，执行层不再为排除条件维护独立关键词实现；模型不能生成 SQL，也不能安排写库、发布、删除或修改；
  模型或格式异常时退回规则计划，不执行自由函数名。
- 图状态只保存可序列化数据；Agent、流式回调和权限上下文通过 Runtime Context 传入。
- `BusinessAgent.answer()` 的公开契约保持不变。
- 已加入 PostgreSQL Checkpointer 资源池和独立开关，默认关闭；开启时 LangGraph 自动初始化
  checkpoint 表。现有 Redis 仍是跨消息业务会话的唯一状态来源。
- 旧 `_answer_legacy()`、`_answer_decision()`、`route_intent -> apply_business_guards`、
  产品查询 legacy/shadow/off 分支和固定领域节点主链均已删除；LangGraph 任务规划链与
  产品 V2 结构化查询链是当前唯一业务路径。
- 多轮客户话术已经拆为 `load_session -> collect_fields -> generate_reply` 子图；缺失字段时
  子图在 `collect_fields` 后结束并等待员工下一条消息，当前由 Redis 恢复会话。
- 企业微信 `msgid` 会先经过现有数据库领取去重，再以哈希形式进入图状态和意图审计；审计表
  对请求指纹设置部分唯一索引，checkpoint 重试不会重复写入同一次请求的审计。
- Redis 中的最近意图上下文也按请求指纹去重，收尾节点重放不会重复追加同一轮对话。
- thread id 使用企业、机器人和会话三级作用域并哈希。新写入的图状态不再单独保存原始 `user_id`；
  既有 checkpoint 不会由启动自动清理，历史 checkpoint、消息正文和模型输出仍可能包含员工会话内容，
  应按实际保留策略在隔离环境验证后制定清理或过期方案。
- 已增加图实例重建后的 checkpoint 恢复测试，以及只接受显式 `TEST_DATABASE_URL` 的 PostgreSQL
  恢复脚本 `scripts/check_langgraph_checkpoint.py`。未配置隔离库时脚本拒绝运行，不会读取生产地址。

## 后续工作

1. 扩充复合任务评测语料，覆盖跨制度/产品/媒体/话术组合以及中间结果不足的场景。
2. 部署前在备份和隔离环境验证 checkpoint 旧数据兼容、保留期与清理脚本。
3. 部署后观察 checkpoint 表增长、连接池、回答延迟和错误日志。
4. 由授权人员验证一轮企业微信真实多轮话术、产品、媒体和知识库问答。
5. 稳定运行后明确 Redis 与图状态的长期边界。

## 上线原则

- 每一阶段先运行测试和离线评测语料。
- 图节点中的外部写入必须使用请求幂等键。
- 未验证多 worker、服务重启和流式回复前，不开启生产开关。
