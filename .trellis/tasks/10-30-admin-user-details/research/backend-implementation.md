# 后端实施边界

日期：2026-11-01。用户已批准最终方案，当前任务 in_progress。

## 修改前边界

- 最小缺口：User 没有成功登录时间，也没有单用户安全元数据/分生命周期资源统计读取接口。
- 行为归属：登录记录属于 auth_service 的成功认证提交边界；聚合属于 admin_service；路由只做 AdminUser 依赖和 schema 转发。
- 文件范围：User 字段、0027 后新增原位迁移、auth_service、admin schema/service/route，以及相关后端测试。父会话与前端代理拥有其他文件；不覆盖其工作。
- 明确不做：普通 UserOut/原管理员列表字段变化、账号写 API 新增、私人配置/密钥/正文披露、配额、全站 UI、任务/作品授权重构。
- 局部复用：现有每作品 chapter_stats_subquery；现有创建账号原语；只有成功认证记录使用窄持久化 helper，避免把管理员创建或改密续签标记为登录。
- 兼容证明：原账号/认证/生命周期回归保留；新增精确字段集合、单 SQL 投影、迁移子行完整性及并发会话版本测试。实施结果待后续补充，不预填通过。

## 安全存储边界

原有 AuditOperationError/DashboardUnavailableError 均是业务专属异常，不适合认证复用。主会话认可新增窄 AuthenticationPersistenceError：仅新认证写入路径失败时回滚、返回固定 500，不将带 SQL/哈希参数的原异常交给全局日志；日志只记异常类型。

## 已实施

- `backend/app/models/entities.py`：User nullable `last_login_at`。
- `backend/alembic/versions/0028_user_last_login.py`：父版本 0027，原位 ADD/DROP COLUMN，无历史回填、不重建 users。
- `backend/app/services/auth_service.py`：注册专属创建/提交、登录条件 UPDATE（ID/启用/已验哈希/版本）、CASE 时间不倒退、显式保持 updated_at，RETURNING 的已核验快照在 commit 后直接用于 Cookie，不 refresh 成更新的会话版本。
- 同文件 `_authentication_storage_failure` 只用于注册/登录的新持久化边界：提交或注册 flush 失败回滚，固定安全 500，日志只有异常类型；管理员创建/bootstrap/改密等原边界不改。
- `backend/app/schemas/admin.py`：新增 AdminResourceUsage、AdminUserUsage、AdminUserDetail；原 UserOut/AdminUserOut 字段不变。
- `backend/app/services/admin_service.py::get_user_detail`：一次 SQL 的安全列投影、每作品章节预聚合及三类生命周期条件聚合，零资源 coalesce 0；不查正文/版本/配置/凭据，顶层 project_count 保留全生命周期语义。
- `backend/app/api/v1/admin.py`：新增 AdminUser 保护的详情 GET，路由无 ORM 查询。
- `backend/tests/test_admin_user_details.py`：17 个实际行为场景，含真实受控并发、精确字段集合、单 SQL 投影、生命周期操作往返、读无写入、认证失败/版本快照和故障脱敏。
- `backend/tests/test_migrations.py`：0027→0028→0027 存量账号、作品、章节、用户私有配置完整性；NULL/default、foreign_key_check 和 RESTRICT 保留。

## 未改动边界

未修改原有账号操作写 API、全局错误框架、JWT/Cookie、作品授权、普通响应、项目备份、前端/规范/任务规划；未提交 Git。前端与父会话的并行修改未回滚或覆盖。
