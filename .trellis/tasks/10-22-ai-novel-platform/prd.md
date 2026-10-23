# AI 小说平台（MVP：核心创作闭环）

## Goal

构建一个多用户、自部署的 AI 辅助小说创作平台：作者登录后创建小说项目，由 AI 向导生成大纲、角色、世界观，再逐章流式生成、编辑、润色正文，最终导出成稿。功能对标 MuMuAINovel 的核心创作闭环，进阶功能后续迭代。

用户价值：把「从一个点子到可持续连载的长篇初稿」的流程工具化，省去手工维护设定与上下文拼接的成本。

## Background

- 全新仓库（greenfield），无存量业务代码。
- 参考项目 MuMuAINovel：FastAPI + React/TS + PostgreSQL 多用户自部署应用，支持 OpenAI/Gemini/Claude 与 OpenAI 兼容中转，Docker 部署。功能全集与分层见 `research/mumuainovel-reference.md`。

## Requirements

- R1 用户与登录：本地账户注册/登录/登出，JWT（httpOnly Cookie）会话；多用户数据按 user_id 隔离；注册可由环境变量开关。
- R2 AI 模型配置：支持 OpenAI 兼容（自定义 Base URL，覆盖中转/本地 LLM）、Anthropic、Gemini；按用户维护多条配置并可设默认；提供连通性测试；API Key 服务端加密存储、回显掩码。默认禁止 Base URL 指向本地/内网地址并禁止跟随重定向（防 SSRF），由部署方通过 `ALLOW_PRIVATE_AI_ENDPOINTS=true` 放行本地模型。
- R3 项目管理：创建（向导式录入书名、题材、一句话设定、写作风格）、列表、编辑、删除。
- R4 AI 向导生成：基于项目基本信息生成分卷/分章大纲、角色卡（含人物关系描述）、世界观设定；支持重新生成与手动编辑。
- R5 角色管理：角色卡增删改查。
- R6 世界观管理：设定条目增删改查。
- R7 章节创作：按大纲条目流式（SSE）生成正文；手动编辑保存；AI 润色/重写；章节有 draft/generating/done 状态与字数统计。
- R8 上下文组装：章节生成自动带入世界观 + 角色卡 + 当前卷大纲 + 最近章节正文/更早章节摘要，按 token 预算裁剪（不依赖向量检索）。
- R9 导出：整本或选中章节导出 TXT 与 Markdown。
- R10 部署：docker-compose 一键启动（应用 + PostgreSQL），.env 管理配置与密钥。

## Acceptance Criteria

- [ ] `docker compose up -d` 后可注册并登录；两个账户互相看不到对方的项目数据（R1, R10）
- [ ] 配置一条 OpenAI 兼容接口，连通性测试通过（R2）
- [ ] 通过向导创建项目，AI 生成出大纲、至少 3 个角色卡、世界观设定，且均可编辑保存（R3, R4, R5, R6）
- [ ] 选中一条大纲章节，流式生成正文（前端可见增量输出），编辑保存后可再执行润色（R7, R8）
- [ ] 导出 TXT 与 Markdown，内容包含已完成章节（R9）
- [ ] 后端 pytest 通过、前端 lint + build 通过

## Out of Scope（本期不做，后续迭代）

伏笔管理、章节关系图谱、提示词工坊、拆书、灵感模式、自定义写作风格库、职业等级体系、角色/组织卡片跨项目导入导出、LinuxDO OAuth、向量检索/Embedding（含 sentence-transformers）、EPUB 导出、移动端、计费。

## Key Decisions

- MVP 范围取核心创作闭环（用户确认，2026-10-22）。
- 技术栈跟随参考项目：FastAPI + SQLAlchemy(async) + PostgreSQL + React/TS + AntD + Zustand（用户确认，2026-10-22）。
- MVP 不引入向量检索，上下文用结构化组装（权衡见 design.md）。
- 本地账户含注册入口（env 可关闭），而非参考项目的纯环境变量账户——便于「平台」形态的多用户使用。
- 增加 `ALLOW_PRIVATE_AI_ENDPOINTS` 开关，默认关闭（用户确认，2026-10-23）。

## Constraints

- 不复制 MuMuAINovel 源码，仅功能对标自行实现（规避 GPL-3.0 传染）。
- UI 为简体中文。
