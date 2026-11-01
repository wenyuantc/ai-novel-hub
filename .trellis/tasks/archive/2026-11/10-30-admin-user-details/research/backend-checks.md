# 后端验证记录

日期：2026-11-01（Asia/Shanghai）。测试仅隔离 tmp_path SQLite、FakeLLM，未操作已有 8000 服务/真实数据库、未联网/安装依赖。

## 工具与命令

工作目录 `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/backend`，使用仓库现成 `.venv/bin/python -m pytest` / `-m ruff`，等价调用已安装项目工具，不改依赖或权限。所有命令设置 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`，避免 Alembic 中文 ini 的 locale 问题。

## 已观察结果

1. RED：`... .venv/bin/python -m pytest tests/test_admin_user_details.py -x -q`，首个新增详情权限测试失败，实际 GET 405 而非要求的 401；证明原先没有详情读接口。此为预期开发 RED，不计作完成门禁。
2. 首轮 GREEN：`... .venv/bin/python -m pytest tests/test_admin_user_details.py tests/test_migrations.py::test_0028_nullable_login_time_preserves_users_and_children -q`，14 passed in 14.60s。
3. 扩展回归：`... .venv/bin/python -m pytest tests/test_auth.py tests/test_admin.py tests/test_admin_user_details.py tests/test_migrations.py -q`，77 passed in 42.43s。
4. 最终新增详情/登录场景：`... .venv/bin/python -m pytest tests/test_admin_user_details.py -q`，17 passed in 17.81s（含后续补强的注册 flush 故障与真实并发迟到请求）。
5. `... .venv/bin/python -m ruff check .`，All checks passed。
6. 首轮全套执行期间补强了认证存储故障边界，为避免旧 import 快照冒称最终代码通过，主动发送 Ctrl-C 停止：255 passed in 143.56s 后 KeyboardInterrupt/exit 2。这是有意中断，不是产品失败、不算全套通过。
7. 最终源码全套：`... .venv/bin/python -m pytest -q`，实际摘要为 **5 failed, 1345 passed, 1 warning in 629.86s (0:10:29)**。不能记作全套通过。
   - 4 个既有 MCP 集成测试均在 `tests/mcp_fakes.py:87` 绑定 `127.0.0.1:0` 时被沙箱拒绝（PermissionError）；未进入真实协议业务断言。
   - 1 个既有关系生成测试 `test_generate_marks_duplicate_self_unselected_existing_and_invalid_fields` 在 SSE 的 10 秒等待超时。
   - 摘要后测试进程清理未退出，继续等待后由本代理 Ctrl-C 结束（退出码 1）；不把清理中断当作新的产品失败，也不隐去上述五项原始失败。
8. 不改源码、原超时限制或测试断言，定向复跑关系生成失败项：`... .venv/bin/python -m pytest tests/test_relations_generate.py::test_generate_marks_duplicate_self_unselected_existing_and_invalid_fields -q`，**1 passed in 1.60s**。
9. 通过工具审批解除本地 bind 沙箱限制后，定向执行：`... .venv/bin/python -m pytest tests/test_mcp_plugins.py::test_connect_time_ssrf_check_sends_no_request tests/test_mcp_plugins.py::test_redirect_not_followed tests/test_mcp_plugins.py::test_fastmcp_integration_list_and_call -q`，**4 passed in 4.08s**。仅本地随机端口的合成 FakeMCP，无真实模型/DNS调用。
10. 上述五项复跑通过不冒称同一轮全套全绿。已与主会话协调，最终 check 代理负责获批的沙箱外完整全套；本代理不再启动重复全套。

## 关键证据

- 对详情 SQL 的执行事件只捕获一次 SELECT；SQL 无 password_hash/token_version、章节正文和历史版本。
- 当前正文、空章节、空作品、其他 owner、回收站、过期待清理及删除中都用独立源数据断言；真实软删/恢复/purge 后统计随生命周期变化。
- bcrypt 之后用 Event 阻塞登录，另一个请求/事务禁用、重置或自助改密后，旧登录分别 403/401，无 Set-Cookie、时间不更新。
- 在登录 commit 已完成后、响应前另开事务重置密码，Cookie 仍携已验证的旧版本，后续 /auth/me 401，未升级签发新版本。
- 受控并发先暂停旧时间 UPDATE，更新的登录先提交，再释放旧请求；两个返回与最终库均保留较新时间。
- commit 和注册 flush 注入包含密码哈希的 SQLAlchemyError：固定 500、无 Set-Cookie，目标时间不变/新账号不存在，日志不含 SQL 私有标记或哈希。
- 迁移存量用户无历史默认回填，原作品、章节、私有配置、密码哈希及 RESTRICT/外键校验在往返后保留。

## 验证边界

没有 PostgreSQL 实例实测，没有浏览器 UI 验收（由主会话/前端检查负责）。不把 SQLite 测试声称为 PostgreSQL 通过。最终全范围检查和规范更新仍由主会话安排。

## 交付时源码 SHA-256

以下为定向复跑结束后的实际文件字节；全套之后未编辑这些源码。

```json
{
  "backend/app/models/entities.py": "7340e366b0a880e334d004b93766fb2ecdd7d66f6bae56a85994a133807f82e0",
  "backend/alembic/versions/0028_user_last_login.py": "30858f69d58a3be7413deb65a18fcce6da47b2a3ef03bd65e33d1c85c85dd268",
  "backend/app/services/auth_service.py": "756a3a455dfe8309c07fc3e5d4a40e8c46a044436e1906f8257d280eeb3d6e6c",
  "backend/app/schemas/admin.py": "9d834a99bf019cf5403a450f1229d575f33bee43defb98f4d7408e3a1c0779f8",
  "backend/app/services/admin_service.py": "690a32c5316675b509811fc64e3b22707bc9139a3a61eafc5956f8d23d6c7770",
  "backend/app/api/v1/admin.py": "f9518f67a2c62b3023af265d9554a0228ba474789f10f263d370b4981b0caa50",
  "backend/tests/test_admin_user_details.py": "52accb8c8f478a2dfffd515c0d0ad888043731687ec84a83e12b428e10cc4446",
  "backend/tests/test_migrations.py": "5cd1fc2229c749b23a73fcd7fda3ae590a9c2b0db1f4ddade91dc13e006f88a9"
}
```
