# 全范围质量检查（Phase 2.2）

日期：2026-10-30。范围覆盖本任务后端、前端、迁移、环境变量、Compose、README、规范与任务契约；任务前已有的 `pelican-bicycle.html` 未修改、未纳入。

已补读 check.jsonl 全部引用、PRD、design、implement、implementation-contracts、新 open-api 规范及两层质量检查条目，并逐实际调用路径核对独立鉴权、逐资源归属与授权、密钥恢复和脱敏、生命周期、SQL 分页、领域派生、专属 schema、安全定义与 root_path，以及前端局部明文、异步失效、统一客户端和响应式主题。

## 已修复

- `backend/app/services/api_key_service.py`：认证原先用 `rate_window != window` 重置计数。受控暂停旧分钟请求、先提交新分钟请求后再放行旧请求，能倒退窗口并重新获得新分钟额度；limit=1 时三个请求均返回 200。
- 修复为 SQL 原子条件只在窗口前进时重置，迟到请求扣减当前已存窗口；`last_used_at` 使用 SQL CASE 保持非递减；429 的 `Retry-After` 使用实际受限窗口与提交后的当前时间。仍保留既有固定 UTC 分钟窗口设计、配置阈值及到期复核。
- `backend/tests/test_api_keys.py`：增加两组通过真实 HTTP/临时数据库的受控并发回归。limit=1 验证迟到请求被限流，limit=2 验证迟到请求消耗现存额度；同时检查窗口、计数、调用时间与 Retry-After。新增测试在修复前均失败，修复后通过；未使用长 sleep、跳过测试或放宽阈值。

## 最终验证

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| backend | `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check .` | exit 0，全部通过 |
| backend 定向回归 | `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_api_keys.py tests/test_open_api.py -q` | exit 0，41 passed，28.25s |
| backend 最终完整回归 | `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest` | exit 0，1061 passed，380.72s；既有 Gemini SDK 弃用警告 1 条 |
| frontend | `npm run lint` | exit 0 |
| frontend 类型与构建 | `npm run build` | exit 0；包含 `tsc -b`；最大 chunk 1146.11kB，无包体积警告 |
| frontend | `npm test` | exit 0，57 个文件、444 项测试通过；Node localStorage 实验性警告不影响结果 |

前端门禁完成后没有更改前端产品文件；后端最终完整回归在限流修复后单独运行，未与修复前全套并跑。主会话同期完成浏览器验收：创建、显隐、隐藏状态复制、刷新后再次显示、Swagger 授权读取、未授权 404，以及桌面/375px 移动端的明暗主题和无横向溢出；管理页控制台无 warn/error。JSON schema 的实际 GET 与 Swagger 加载正常，IAB 下载事件等待超时仅记录为浏览器工具观察限制。

## 未修复与剩余观察

- 未发现剩余需修复的本任务产品问题，未更改公共接口或扩大功能范围。限流边界与通用规范窄例外已由主会话同步到规范。
- 首次完整回归出现一次旧 `test_projects_and_story.py::test_cross_user_isolation` 的知识库 `kb.index` 任务 5s 轮询超时，结果为 1058 passed / 1 failed，发生在隔离断言前。该用例原样单独复跑为 1 passed in 1.33s，最终修复后完整回归也通过；没有稳定复现根因，因此未改动该旧测试或索引产品逻辑，未延长等待时间。
- 修复前第二次全套在 817 passed 时按主会话要求主动停止（exit 2），避免与最终门禁争用资源；不将其作为完整通过证据。
- 未执行 Git mutation 或提交。
