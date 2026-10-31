# 管理员审计：后端实施报告

日期：2026-10-31。仅修改后端产品、后端测试及本报告；未操作真实数据库，未 stage/commit/switch，未覆盖前端、规划文档或启动前的个人配置改动。

## 实施范围

新增：
- `backend/app/models/audit.py`：实例级 `admin_audit_logs`，独立 ID/操作 UUID、无业务外键、用户名/目标/归属快照；SQLite AUTOINCREMENT。
- `backend/alembic/versions/0023_admin_audit_logs.py`：已核实原 head=0022；仅新增审计表及时间、操作人时间、目标时间索引，不回填历史。
- `backend/app/schemas/audit.py`：动作/结果/目标枚举、有界且拒绝未知字段的摘要、安全标量变更、版本关联、日期及清理契约。
- `backend/app/services/audit_writer.py`：显式操作上下文，成功审计与业务同事务；回滚后仅用 primitive 快照写失败；操作 UUID 去重、不确定提交查询；提交后异常不误记失败。
- `backend/app/services/audit_service.py`、`backend/app/api/v1/admin_audit.py`：查询/详情及受保护日期清理。
- `backend/tests/test_admin_audit_logs.py`：30 个测试案例（含参数化）。

局部修改：
- `models/__init__.py`、`api/v1/__init__.py` 注册新模型/路由。
- `admin_service.py`、`api/v1/admin.py`：创建/更新/重置密码/删除均保留真实 actor；`auth_service.py` 拆不提交创建原语，原注册/bootstrap 包装仍提交。
- `project_service.py`、`chapter_service.py`、`cast_service.py`、`chapter_version_service.py`：仅管理员维护他人作品的登记动作；不放宽 owned helper、不替换 actor、不扩大模型/任务/私有资源权限。
- `version_service.py`：精确识别 chapter_id/seq 唯一竞争的业务 flush 原语；审计 flush 不在业务冲突翻译边界内。原十分钟合并、裁剪、标星、恢复规则不改。
- `skill_service.py`、`api/v1/skills.py`：仅全局 Skill 管理动作；导入复用不双记；保留路由原 2MB 大小守卫及上传中间件，超限全局导入在安全拒绝服务中记失败；不保存文件名或内容。
- `workshop_service.py`：发布/审核/分类标签/上下架/删除；管理员提交自动通过与官方发布复用单条事件；不改变原 item/submission 锁顺序。
- `tests/test_migrations.py`：固定 `0022→0023→0022`、无外键、删用户保留历史、UUID 唯一、清空后 ID 不复用；原 head 全链路仍测试。

## 关键保障

- 摘要只存字段名、安全布尔/数字/状态、正文前后字数或字符数、参考资料数、删除级联数量、有界 ID；不保存正文、任意备注、密码/长度/哈希、凭据、请求体或原始异常。
- 关联业务版本只保存 project/chapter/version ID、seq 和 SHA-256。详情核对关联关系并返回 available/changed/missing，不返回正文，不把版本设为永久保留。
- `AuditOperationError` 把审计边界中的未知故障转为固定中文 500；只记录 operation_id/committed 的安全运维信息，原 SQLAlchemy 异常及 SQL 参数不会继续交给全局 traceback logger。提交后响应故障同样安全转换但不写失败事件。
- 清理预览 JWT 使用独立 purpose，绑定 actor、UTC 半开范围、高水位、预计数、随机 nonce，十分钟有效；执行必须 `confirm` 真正为 JSON true，验证当前管理员，并先用唯一 operation_id 占 nonce。
- 删除始终排除 `audit.cleanup`，包括失败清理事件；删除与成功留痕同事务。预览之后更大 ID 不受影响；并发/重复令牌 409；已有并行清理可使实际数量低于预计。

## 最终接口契约

单一来源为 `backend/app/schemas/audit.py`；已同步主会话和前端代理。

前缀 `/api/v1/admin/audit-logs`，全部 `AdminUser`：
- `GET ""`：`actor_user_id?`, `actor_q?`≤64（历史快照，字面量大小写不敏感子串）、`target_type?`, `target_id?`, `action?`, `result?`, `from_at?/to_at?`（成对 aware 时间，UTC 半开区间）、`page=1`, `page_size=20`（1–100）。返回 `{items: AuditLogBrief[], total}`，按 occurred_at DESC/id DESC。
- `GET /{log_id}`：`AuditLogDetail` = brief + `summary`, `version_ref`, `version_status: available|changed|missing|null`；不存在/已清理 404。
- `POST /cleanup-preview`：`{from_at,to_at}` → `{from_at,to_at,count,protected_count,expires_at,preview_token}`。
- `POST /cleanup`：`{preview_token,confirm:true}` → `{deleted_count,audit_log_id}`。
- 清理错误除统一中文 `detail` 外附 `code`：400 `invalid_preview`、403 `preview_actor_mismatch`、409 `preview_replayed`。参数/确认非法 422，不写审计。
- brief 显式字段：id/operation_id/occurred_at、actor_user_id/actor_username、action/result、target_type/target_id/target_label、owner_user_id/owner_username、project_id/project_title/chapter_id、error_code/error_message；不包含 summary/正文。
- SafeState 包含项目 `planning/writing/completed`，与章节/工坊状态完整兼容。

## 验证记录

命令均在 `backend/`，使用已安装 `.venv` 的 Python 3.12.8 与 `/Users/wenyuan/.local/bin/uv run --offline`，不访问网络。测试使用 pytest tmp_path SQLite、FakeLLM、fake_dns。

已执行：
1. 初版目标业务（admin/auth/admin_projects/chapters/chapter_versions/cast_context/skills/workshop）**120 passed**。同期迁移首跑 **21 failed**：环境 `LC_ALL=C` 导致 Alembic 中文 ini 被 ASCII 解码；没有修改/跳过迁移，后续均明确 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`。
2. 审计+迁移阶段回归 **43 passed**，补并发/白名单/slug 冲突后 **47 passed**。
3. 摘要完善后目标回归 **169 passed in 123.17s**（此时尚未加入最终版本竞争/系统日志安全修复）。
4. 最终新增版本业务 flush/版本原语竞争与 SQL 参数日志脱敏 **3 passed, 27 deselected in 2.55s**。故障注入最初因漏 import/误选已自动 flush 的章节对象失败；已修正为实际脏版本行，未削弱产品断言。
5. `uv run --offline ruff check .`：最终修复后 **All checks passed**；`git diff --check -- backend` 通过。
6. 初版全量 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run --offline pytest`：**1095 passed, 1 warning in 581.56s**，最终 exit 0。唯一 warning 为既有 Gemini SDK generate_images 弃用警告。该进程在汇总后延迟退出，后已自然结束；尝试向旧 PID 发送 INT 时已不存在，未实际中断任何进程。此套件在最终核心修复前载入，不能代替最新修复证据。
7. **最终代码目标回归 exit 0：172 passed in 166.78s**。命令：`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run --offline pytest tests/test_admin_audit_logs.py tests/test_migrations.py tests/test_admin.py tests/test_auth.py tests/test_admin_projects.py tests/test_chapters.py tests/test_chapter_versions.py tests/test_cast_context.py tests/test_skills.py tests/test_workshop.py -q`；包含审计 30、迁移 22、既有目标业务 120。
8. 最终代码的独立全量门禁由主会话派发的 `audit_check` 运行（预期收集 1100），最终结果以其报告为准；本实施代理不提前声称该检查完成。

## 交接、限制和风险

- 后端产品所有权已明确移交 `audit_check` 独立审查/修复；之后本实施代理只更新本报告和运行测试。主会话应在最终核心修复后重启 QA 服务复测，旧进程已载入旧模块。
- 独立检查指出的版本预 flush 丢失 409、原异常进入全局 SQL 参数日志两项已修正并有专门回归；仍以其最终全量审查结果为准。
- 未连接 PostgreSQL 做真实并发集成测试；SQLite 并发相同 nonce 的 200/409 与单条受保护结果已测试，PG 路径依赖唯一约束和原有行锁。
- 数据库不可用/进程崩溃仍不能保证失败审计落库；不承诺独立可靠投递或数据库管理员防篡改。
- 不回填上线前事件；部署必须先升级 0023。显式降级会删除审计，应先备份；应用回滚可保留审计表。
- 未扩大上传大小限制、未访问真实生产数据；曾被自动审核拒绝的移除重复路由大小校验方案已放弃，最终保留原守卫，采用更保守的安全拒绝服务。
