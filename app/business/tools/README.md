# 内部业务工具边界

这里的“工具”是代码内部的领域门面，不直接作为任意 function tool 暴露。任务规划模型只能输出
受枚举约束的领域任务，LangGraph 执行器再把任务映射到这些门面。

## 目录

- `knowledge.py`：制度、通用资料和 RAG 回答入口。
- `product.py`：产品、型号、规格、价格和配件入口。
- `media.py`：产品图片/视频查询入口；企业微信传输仍由 `app/wecom/` 负责。
- `reply_script.py`：客户沟通场景、字段收集和话术生成入口。
- `permission.py`：敏感字段权限边界的占位门面；实际通讯录校验仍由 `app/wecom/directory.py` 完成。
- `audit.py`：意图上下文和审计记录入口。

## 调用规则

`BusinessAgent` 只做状态恢复和对外入口；`create_plan -> execute_task -> verify_results` 是唯一
业务主链。领域工具不直接暴露 HTTP 路由、不负责管理台渲染，也不允许模型绕过权限直接执行
发送或管理操作。

产品、媒体、话术和知识库的实际实现分别位于对应模块；`service.py` 只保留状态恢复、审计和
对外入口。各模块通过 mixin 复用 `BusinessAgent` 的 store/会话状态，不改变现有企业微信接口。
