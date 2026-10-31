# 当前契约与变更边界调研

调研日期：2026-10-31。仅依据本仓库，无新依赖或外部服务。

## 身份、权限与事务

- `backend/app/core/deps.py:71/:84` 验证真实 Cookie/Bearer、启用状态和 token_version，再判断管理员；没有超级管理员。不得从请求体接受 actor/owner。
- `backend/app/core/deps.py:25` 只提供请求级 AsyncSession，不自动 commit。服务自行提交，不能假设路由结束有统一事务。
- `backend/app/services/admin_service.py:87/:110` 创建/重置尚无 actor，更新/删除已有 actor；需补齐真实操作人。
- `backend/app/services/auth_service.py:17` 共用 create_user 自行提交。管理员创建需拆出不提交的原语，注册/bootstrap 保留原包装的行为。
- `backend/app/services/project_service.py:31` 的 manageable 允许当前启用管理员，owned 仍严格；更新/删除位于 `:232/:258`。不改认证身份、不放宽 owned helper。
- `backend/app/core/errors.py:61` 集中处理 AppError/422/500；不得直接把任意 detail/extra、SQL 参数或请求体序列化入审计。

## 章节与版本

- `backend/app/services/chapter_service.py:78/:106/:146/:195` 为手工创建、从大纲批量建章、更新、删除，各自提交。
- `chapter_service.py:21` 的 commit_versioned 将版本竞争映射为 409，接审计不能丢失原语义。
- `backend/app/services/version_service.py:91` 的手动版本会在十分钟窗口原地合并，空白/同文可不生成；`:48` 裁剪旧版本。不能另造全文快照或把所有关联版本设为永久保留。
- `backend/app/services/chapter_version_service.py:61` 恢复正文并生成 restore 版本，同文件有版本备注/标星/删除；`cast_service.py:146` 手工选角独立提交。
- `rewrite_service.py` 预览/应用保持严格 owner helper，不在本期跨用户人工审计接入清单。

## 共享资源及复用入口

- `backend/app/services/skill_service.py:167/:191/:206/:217/:264` 共用全局和私有 CRUD/参考资料/内置恢复，须按已验证的 scope 决定采集。
- `skill_service.py:394` 导入调用 create_skill；内外不可各记一次。global 导入的业务解析失败仅记录安全类别。
- `backend/app/services/workshop_service.py:266` 管理员提交自动通过，官方发布路由复用 submit；只记一次发布。
- `workshop_service.py:345/:356/:372/:388/:399` 为审核、上下架、元数据和删除；保持已有 item/submission 锁顺序。
- 点赞、下载、本人导入和私有 Skill 复制均不属于本期。

## 数据、迁移与界面

- 当前 head 为 `backend/alembic/versions/0022_api_keys.py:10`；拟新增 0023，实现前重查占用。新模型拆文件后在 `app/models/__init__.py` 导出。
- `backend/tests/test_migrations.py:13` 展示固定 revision 往返，`:85` 验证 head 全链路；本迁移测试固定 0023→0022，不用 head→-1 代替。
- 审计是实例级表，目标/用户/版本 ID 用无外键快照，不被级联删除、不进项目备份；SQLite 使用 AUTOINCREMENT 防清理后 ID 重用。
- `frontend/src/pages/AdminProjectsPage.tsx:14` 提供管理表格/筛选/分页/错误重试参照。`hooks/useLoader.ts:15` 的 loader 需 useCallback，只接纳当前 effect 响应。
- `frontend/src/router.tsx:59/:64` 有 RequireAdmin 管理路由；新审计页同样保护且懒加载。
- 管理菜单和导航位于 `frontend/src/components/AppLayout.tsx`；不是 StudioNavigation 的普通全局资源组。同步页面名、桌面/移动入口及面包屑。
- 传输类型统一在 `frontend/src/api/types.ts`，新客户端走统一 api，不能在组件 fetch。
- `frontend/src/utils/time.ts:4/:9` 统一 UTC 解析/本地显示；日期选择转成半开 UTC 区间，明确本地时区。
- `components/chapter/VersionDrawer.tsx` 是带恢复/编辑功能的抽屉，不直接嵌入只读审计详情；按需使用既有 versionApi.get 查看业务版本。

## 最小变更边界

缺口是范围内业务写入缺少独立、可检索、同事务且保留真实 actor 的事实记录，以及日期清理缺少受保护留痕。

只接入 PRD 覆盖表所列服务，不使用全局 ORM hook/middleware、请求 body 采集或替换所有 session 扩大范围。其他人工内容模块可在后续通过明确动作登记继续接入。

已有未提交的 `.codex/config.toml`、`.gitignore`、两个 `source-command-trellis-*` skill 目录与任务无关，不覆盖、回滚或混入后续功能提交。

## 上下文注入截断的兜底读取

task.py validate 已确认清单路径有效，但 database-guidelines、error-handling、skills-mcp、component-guidelines、type-safety 超过单文件注入上限，且总量可能使后续条目只保留索引。此文件在清单中优先，不能把“已可信注入”等同于“已读到全文”。

实现/检查代理必须按职责补读原规范的相关段落：
- backend/database-guidelines.md：事务、查询模式、Migration、外键规则、章节版本写入规则。
- backend/error-handling.md：错误类型、错误处理模式、API 错误响应。
- backend/skills-mcp.md：Skill 可见性/写权限/上限与 SKILL.md/zip 导入导出；不用拉取 MCP 无关后半部。
- frontend/component-guidelines.md：页面模式、用户管理页、Props/样式/无障碍、主题与配色、响应式。
- frontend/type-safety.md：类型组织、PATCH 语义、时间戳、校验、API 客户端契约。

上述名称相对 .trellis/spec/；先按标题搜索当前行号再定向读取，避免行号因其他变更漂移，也不以本调研摘要替代原规范。
