# 后端实施与验证证据

## 实施范围

- 新增管理员项目分页查询：名称/用户名文字搜索、所属用户/状态过滤、稳定排序与章节聚合。
- 项目详情/更新/手工封面响应带可空 owner 摘要；跨用户 reading=null。
- 新增人工 get_manageable_project / get_manageable_chapter，严格 get_owned_project / get_owned_chapter / get_project_chapter 保持原有语义。
- 人工世界观/人物/大纲路由显式启用管理权限；组织、关系、职业、伏笔、细纲、版本、封面、已有KB、手工记忆与目录按设计接入。
- 跨用户 style/default_style_id/style_id 与 auto_analyze 修改拒绝；章节生成中管理员不能复位/覆盖。记忆人工保存不排队所有者向量任务。
- 未新增依赖/迁移；模型、任务、日志、导入导出、阅读进度和账户资源仍严格归属。

## 已执行

1. 初始 `uv run pytest tests/test_admin_projects.py -q`：3 failed，原因分别为缺失管理列表和管理员无跨用户内容权限，证明测试先于实现能捕获行为缺口。
2. 核心实现后相同命令：3 passed。
3. 扩展测试运行 `uv run pytest tests/test_admin_projects.py tests/test_projects_and_story.py::test_cross_user_isolation -q`：首轮发现测试误用了 PlanBuildResult.created 字段；按真实 schema 改为 chapters 后，新功能8项全部通过，原普通用户隔离测试亦通过。
4. `uv run ruff check .`：All checks passed。
5. 全量 `uv run pytest`：已完成1069项测试并输出 `21 failed, 1048 passed, 1 warning in 398.66s`。21项失败全部为 test_migrations.py：宿主 LC_ALL=C.UTF-8 在macOS不存在，Alembic以 locale 编码读取含中文 alembic.ini 时回退ASCII，抛 UnicodeDecodeError。没有修改迁移/测试来绕过。
6. 系统 `locale -a` 确认支持 en_US.UTF-8；使用 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_migrations.py -q`：`21 passed in 12.44s`。
7. 最后 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check .`：All checks passed。
8. 已交回后端所有权给最终 trellis-check 代理；按主会话协调，不启动第二次全量，最终审核修复后由 checker 统一在有效UTF-8 locale下执行完整测试。原全量后续清理完成，进程正常结束并返回 exit_code=1；该命令因上述21项环境编码失败而非全绿。

## 新增行为验收

- 管理列表401/403、过滤/分页/统计、特殊搜索字符、个人书架仍只含本人项目。
- 管理员修改他人资料/正文/各人工设定，所有者可见；管理员降级立即失去权限，第三名普通用户仍404。
- 人工章节字数/版本、细纲建章/选角、关系历史/图谱、组织成员/职业挂接/伏笔、封面和Reader目录。
- KB已有内容维护、记忆CRUD与关键词块更新，配置所有者embedding模型后仍无新增任务与模型调用。
- AI、生成日志、私有风格/模型配置、阅读进度请求拒绝，风格绑定保留，生成中编辑/复位冲突。
- 跨项目子资源混用、不存在项目、删除级联仅影响目标项目。

## 交付边界

尚未执行主会话浏览器验收或Trellis审核，不将它们标为已通过。没有执行提交或推送。
