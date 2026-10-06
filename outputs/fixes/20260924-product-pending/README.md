# 已发布产品误显示“有修改待发布”

2026-09-24，用户反馈 LTA-822 发布后仍显示待发布。

## 原因

只读请求当前 `/business/products` 验证：草稿与发布快照的业务内容、媒体 ID 均一致，仅 `set_price`、`body_price`、`cost_price` 在 API 中分别是 Decimal 序列化字符串与 JSONB 数字。前端直接比较 `JSON.stringify`，把相同金额误判为差异。不是发布失败，也不需要重新发布。

## 修改

- `app/admin/static/product-admin.js`：仅金额字段按数值比较（含配件单价）；嵌套对象忽略属性顺序，仍保留数组顺序。空值与零不同，型号、供应商等文本保持原样比较。
- `app/admin/static/index.html`：产品脚本版本改为 `2026092403`，刷新页面会请求新版本。
- `tests/frontend/product_admin_status.test.cjs`：用合成数据和本地 DOM/API 替身执行真实产品列表脚本，验证状态标记与操作文字。
- 原脚本、页面副本在 `before/`。本次未写入或重新发布真实产品，未改机器人业务权限，未重启服务。

## 验证

- 修改前 6 个前端回归用例中 4 个复现失败；修改后 6/6 通过。
- 覆盖：金额类型相同含义、实际价格变动、空值与零、配件属性顺序与实际变动、文本标识、媒体增删，以及发布后清除标记。
- `node --check app/admin/static/product-admin.js` 通过；`pytest -q --tb=short tests/test_admin.py` 8/8 通过。
- 对当前真实接口数据执行修正后的比较函数：LTA-822 `pending=false`。
- 实际 `/admin` 已引用新脚本版本；HTTP 返回脚本与磁盘文件 SHA-256 一致。
- 执行了一次界面静态扫描，原始结果在 `detector.json`。扫描器无法解析带查询参数的根相对样式链接，样式相关结论不完整；没有因该扫描改动其他界面样式。
- 内置浏览器访问管理台仍被 `net::ERR_BLOCKED_BY_CLIENT` 阻止，未绕过限制；没有完成真实浏览器视觉验收。用户刷新页面后可确认实际显示。
