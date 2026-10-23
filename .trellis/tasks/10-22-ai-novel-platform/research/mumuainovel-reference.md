# 参考项目调研：MuMuAINovel

- 来源: https://github.com/xiamuceer-j/MuMuAINovel （README，抓取日期 2026-10-22）
- 许可证: **GPL-3.0** —— 直接复用其代码则衍生作品必须以 GPL-3.0 开源；仅参考功能、自行实现不受约束。

## 功能清单

核心特性：
- 多 AI 模型：OpenAI、Gemini、Claude，各有独立 API Key 与 Base URL 配置；支持任意 OpenAI 兼容中转；可放行本地/内网 LLM（Ollama、llama.cpp，`ALLOW_PRIVATE_AI_ENDPOINTS`）
- 智能向导：输入基本信息后 AI 自动生成大纲、角色和世界观
- 角色管理：人物关系、组织架构可视化
- 章节编辑：创建、编辑、重新生成和润色
- 世界观设定：构建完整故事背景
- 登录体系：LinuxDO OAuth（自动建号）+ 本地账户（环境变量配置用户名/密码）
- PostgreSQL 多用户数据隔离
- Docker 一键部署（compose / Docker Hub 镜像；最低 2 核 2GB，无需 GPU）

进阶功能（README TODO 中标记已完成）：
- 灵感模式（创作点子生成）
- 自定义写作风格
- 数据导入导出
- Prompt 可视化调整界面
- 章节字数限制设置
- 思维链与章节关系图谱
- 根据分析一键重写
- 职业等级体系（修仙境界、魔法等级等多体系）
- 角色/组织卡片跨项目导入导出
- 伏笔管理（追踪未回收线索、伏笔时间线）
- 提示词工坊（社区 Prompt 分享）
- 拆书功能（一键拆书）

## 技术栈

- 后端: FastAPI 0.109.0、SQLAlchemy、Python 3.12
- 前端: React 18.3、TypeScript、Ant Design、Zustand、Vite
- 数据库: PostgreSQL 18（asyncpg，连接池可调）
- 其他: sentence-transformers 多语言 Embedding 模型（约 400MB，用于语义检索类能力）

## 核心创作工作流

1. 登录（本地账户或 LinuxDO OAuth）
2. 创建项目（向导模式）
3. AI 生成：输入基本信息 → 自动产出大纲和角色
4. 编辑完善：管理角色关系、生成/编辑/润色章节

## 部署

- Docker Compose（.env 配置 → `docker-compose up -d` → 8000 端口）
- Docker Hub 镜像 `mumujie/mumuainovel:latest`（内置模型文件）
- 源码开发：后端 venv + uvicorn，前端 npm，自行准备 PostgreSQL 与 Embedding 模型
