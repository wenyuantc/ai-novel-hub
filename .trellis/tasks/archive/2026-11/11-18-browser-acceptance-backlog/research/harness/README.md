# 浏览器验收环境（TODOS 第 19 项）

隔离环境：临时 SQLite（alembic 迁移到 head）＋ FastAPI 同源提供 `frontend/dist` 生产包＋本地 OpenAI 兼容假模型，全部绑定 `127.0.0.1` 随机端口。运行数据（库、备份、日志、`state.json`、`fixtures.json`、`requests.jsonl`、截图）只写在系统临时目录，不进仓库。

| 文件 | 作用 |
|---|---|
| `serve.py` | 组装环境变量、迁移、后台启动后端与假模型；`start` / `status` / `stop` |
| `fake_model.py` | 假模型（标准库实现）：按提示词分流、一次性脚本、延迟、调用记录 |
| `seed.py` | 经 HTTP 接口幂等建立账号、管理角色、模型配置、协作关系与夹具，写 `fixtures.json` |
| `lib.cjs` | Playwright 公共辅助：读环境、按身份建独立上下文并登录、视口、截图、假模型控制、结果记录 |
| `scenarios/smoke.cjs` | 冒烟（AC5） |

## 确切命令

在仓库根目录执行（`H` 为本目录）：

```bash
H=.trellis/tasks/11-18-browser-acceptance-backlog/research/harness

# 1. 重建生产包（每次验收前；修了前端也要重建）
(cd frontend && npm run build)

# 2. 启动（不给 --dir 时新建 mktemp 目录；就绪后打印 state.json 摘要，记下其中的 dir）
python3 $H/serve.py start
#   可选：--dir <已有空目录>  --auto-analyze（打开 AUTO_ANALYZE_ENABLED）

# 3. 种子（可重复执行）
backend/.venv/bin/python $H/seed.py --dir <dir>

# 4. 场景
QA_DIR=<dir> node $H/scenarios/smoke.cjs          # HEADED=1 时有界面运行

# 5. 状态 / 停止
python3 $H/serve.py status --dir <dir>
python3 $H/serve.py stop --dir <dir>
```

`serve.py` 本身用任意 Python 3 运行，后端、迁移与假模型一律用 `backend/.venv/bin/python`（需先 `cd backend && uv sync`）。后端工作目录是临时目录、`PYTHONPATH` 指向 `backend/`，因此不会读到产品 `.env`。

## 环境变量

`serve.py` 为后端进程设置：`DATABASE_URL`（`<dir>/qa.db`）、`JWT_SECRET`（随机）、`ENCRYPTION_KEY`（随机 Fernet）、`BACKUP_DIR`（`<dir>/backups`）、`FRONTEND_DIST_DIR`（仓库 `frontend/dist`）、`REGISTRATION_ENABLED=true`、`COOKIE_SECURE=false`、`ALLOW_PRIVATE_AI_ENDPOINTS=true`、`AUTO_ANALYZE_ENABLED`（默认 false）、`ADMIN_USERNAME=qa_super`、`ADMIN_PASSWORD`（随机，只存 `state.json`）、`PYTHONPATH`。

场景脚本读取：`QA_DIR`（必需）、`PLAYWRIGHT_PATH`（默认 npx 缓存中的 1.64.0）、`HEADED`。

## 账号（seed.py）

测试口令是 `seed.py` 中的 `QA_PASSWORD`，只在隔离库有效；超级管理员 `qa_super` 由启动引导，口令见 `state.json`。

| 账号 | 身份 |
|---|---|
| `qa_author` | 主作品「剑影长歌」、续篇「剑影续篇」与系列「剑影系列」的作者；有默认模型配置「本地假模型」 |
| `qa_editor` | 主作品编辑者，`allow_owner_ai=true`，本人无模型 |
| `qa_viewer` | 主作品查看者 |
| `qa_series` | 仅系列设定协作者 |
| `qa_series_editor` | 系列设定协作者，兼续篇编辑者 |
| `qa_admin_rw` | 管理角色「QA 作品读写」（`projects.read`+`projects.edit`），主作品查看者 |
| `qa_admin_ro` | 管理角色「QA 作品只读」（仅 `projects.read`），主作品查看者 |

夹具：主作品 15 章（第 5 章「佩剑被毁」并已分析，自动回写 1 条角色状态与 5 条剧情记忆；第 12 章「拔出佩剑」；第 3 章 3 个场景，首场景带 1 个分区块）；角色林远／苏晴／老周（带标签）；3 个按标签匹配的自定义分类；3 篇笔记（固定+收藏、仅收藏、无标记）；Skill `qa-tone`（下拉「基调」默认轻松、必填「重点人物」、触发词「基调」）；系列共享角色「沈青」（主作品已采用）与世界观「临江城」（未采用）。具体 id 见 `<dir>/fixtures.json`。主作品未跑过一致性检查，修改影响页签为空。

## 假模型

默认按 `backend/app/ai/prompts/formats.py` 里锁定的输出结构分流（`fake_model.py` 的 `MARKERS`）：章节分析、一致性抽取、比对、影响判定、向导（大纲／角色／世界观／职业）、角色／组织／职业生成、关系解析与生成、大纲续写与展开、风格提取、灵感、拆书两步、智能体摘要；带 `tools` 的请求视为智能体；其余（章节生成、续写、润色、改写、场景与区块）返回一段正文。

一致性相关的规则：抽取按句识别「佩剑／玉佩／法器N」被毁（断、毁、碎）、使用（拔出、取出、握着、挥动）、完好；「老周／阿青」身亡、现身。比对把同一主体「已毁／身亡」与更晚章节的「仍在使用／在世」报为冲突；影响判定把同主体的前后事实配为 changed，其余为 removed／added。

控制接口（`http://127.0.0.1:<model_port>/_control/...`，`lib.cjs` 有封装）：

```bash
# 智能体下一轮调用一个工具（之后无脚本时回一段文本）
curl -s -X POST localhost:<model_port>/_control/script -d '{"match":"tools","type":"tool_calls","tool_calls":[{"name":"get_project_overview","arguments":{}}]}'
# 下一次章节正文请求失败 503
curl -s -X POST localhost:<model_port>/_control/script -d '{"match":"text","type":"error","status":503}'
# 只给事实抽取加 3 秒首字节延迟；全 0 关闭
curl -s -X POST localhost:<model_port>/_control/delay -d '{"first_byte":3,"per_chunk":0,"kinds":["extract"]}'
# 调用记录（完整请求体在 <dir>/requests.jsonl）
curl -s 'localhost:<model_port>/_control/calls?kind=agent'
```

## 已知限制

- 假模型不理解对话，只能验证结构、送达与受理；分析与抽取规则只覆盖夹具里的写法。
- 脚本是全局队列：智能体确认后自动续跑的轮次也会消费 `match:"tools"` 的脚本，每一步前要清空并只压入这一步需要的脚本。
- 端口在启动时取空闲端口后再绑定，极小概率被抢占，重启即可。
- 冒烟用独立的「冒烟作品-时间戳」，不改验收夹具；但会在作者账号下留下这本书。
- 项目智能体输入框是 AntD `AutoComplete`，可访问角色为 `combobox` 而不是 `textbox`：用 `getByRole('combobox', { name: '发给项目智能体的消息' })` 定位（`textarea[aria-label=...]` 亦可）。这不是可访问性缺陷，见 `../verification-g7-g4.md` 的 D-extra。
- 窄屏是视口模拟，不是真机。
