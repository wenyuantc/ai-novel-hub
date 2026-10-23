# 实施计划：AI 小说平台 MVP

## 执行顺序

里程碑 A — 可登录的骨架
1. 仓库骨架：backend/、frontend/、docker-compose.yml、.env.example、README
2. 后端基础：FastAPI 入口、配置加载、async SQLAlchemy、Alembic 初始化、/healthz
3. 认证：users 表 + 注册/登录/登出/me，JWT httpOnly Cookie，bcrypt，REGISTRATION_ENABLED 开关
4. 前端骨架：Vite+React+TS+AntD+Zustand+Router，登录/注册页，会话守卫

里程碑 B — 项目与设定
5. projects CRUD（后端 + 前端项目列表/创建）
6. ai_configs CRUD + 连通性测试（后端 + 前端模型配置页，Key 掩码回显）
7. world / characters / outlines CRUD（后端 + 工作区三个管理面板）

里程碑 C — AI 生成主链路
8. Provider 抽象 + openai_compat/anthropic/gemini 实现 + 单测（mock 流）
9. 提示词模板 + 向导生成（大纲/角色/世界观，SSE）+ 前端向导流程
10. 章节生成/润色（上下文组装 + SSE）+ 章节编辑器（流式渲染、编辑保存）

里程碑 D — 出口与收尾
11. 导出 TXT/Markdown（后端拼装 + 前端入口）
12. docker-compose 联调、README 部署文档
13. 全量验证，逐条核对 prd.md 验收标准

每个里程碑结束时保持系统可运行（compose 冒烟一次）。

## 验证命令

- 后端：`cd backend && pytest`
- 前端：`cd frontend && npm run lint && npm run build`
- 冒烟：`docker compose up -d` → 注册/登录 → 向导建项目 → 生成一章 → 导出

## 风险点与回滚

- SSE 可能被反代/压缩中间件缓冲成一次性返回：compose/反代配置须关闭 buffering；前端按增量渲染验证
- API Key 加密主密钥缺失时启动 fail-fast，防止明文落库
- 多用户隔离回归：service 层统一 user_id 过滤，检查阶段专项核对越权路径
- Alembic 每步迁移可 downgrade；出问题按里程碑回退

## task.py start 前检查

- prd.md / design.md / implement.md 三件套齐备
- implement.jsonl 与 check.jsonl 均有真实条目
- 用户已在规划摘要后明确批准进入实现
