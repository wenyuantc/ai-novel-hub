# G3 作品协作：浏览器验收结果（C-1 至 C-13）

场景表见 `research/targets-consistency-collab.md` C5。本批未修改任何产品代码。

## 环境

- 临时目录：`/private/var/folders/h1/jwfsdnfs4lg5p4fcbkcwwp600000gn/T/ainovel-accept-jjl9k1rk`（本批独占，`serve.py start` 新建；验收结束已 `serve.py stop`）
- 提交：`2c41c6f`；迁移头 `0064`（alembic 从空库升级）
- 前端：本批未重建，使用 `frontend/dist`（index.html 时间 2026-11-18 17:06，由第 0 步统一构建），后端经 `FRONTEND_DIST_DIR` 同源提供
- 浏览器：Google Chrome 157.0.8103.79（`channel: 'chrome'`，headless）；Playwright 1.64.0（npx 缓存）
- 视口：桌面 1280×900（本批场景均为桌面）
- 身份：每个身份一个独立浏览器上下文；种子账号见 `harness/README.md`，另为 C-1/C-2 现场注册新账号 `g3_inv_<随机后缀>`
- 另起环境：C-4 第二部分在 `ainovel-accept-9_ffxj8w` 执行（新 serve.py + seed.py，提交 `2c41c6f`、迁移头 `0064`，已停止）
- 截图：临时目录 `screenshots/g3-c*.png`；原始结果：临时目录 `result-g3-series.json`、`result-g3-admin.json`
- 执行说明：两份脚本各完整跑过多轮（中途因 API 连接中断后续跑）；脚本开头会把被改动的数据恢复（重新邀请 `qa_series`、重建「临江城」、把 `qa_admin_rw` 恢复为主作品查看者并清空其模型配置、撤销其系列身份），下文结论取自最后一次完整运行，两份脚本均 0 失败。中间出现过的失败均为脚本断言写法问题（见各场景备注），不是产品表现。

## 结论汇总

13 个场景：通过 13，失败 0，无法验证 0。

| 编号 | 身份 | 结论 | 脚本 |
|---|---|---|---|
| C-1 | qa_author | 通过 | `scenarios/g3-series.cjs` |
| C-2 | 新注册 g3_inv_* | 通过 | `scenarios/g3-series.cjs` |
| C-3 | qa_series | 通过 | `scenarios/g3-series.cjs` |
| C-4 | qa_series、qa_author | 通过 | `scenarios/g3-series.cjs`、`scenarios/g3-c4-progressions.cjs` |
| C-5 | qa_series | 通过 | `scenarios/g3-series.cjs` |
| C-6 | qa_series_editor、qa_series | 通过 | `scenarios/g3-series.cjs` |
| C-7 | qa_viewer、qa_series、qa_author | 通过 | `scenarios/g3-series.cjs` |
| C-8 | qa_admin_rw | 通过 | `scenarios/g3-admin.cjs` |
| C-9 | qa_admin_rw | 通过 | `scenarios/g3-admin.cjs` |
| C-10 | qa_admin_ro | 通过 | `scenarios/g3-admin.cjs` |
| C-11 | qa_admin_rw、qa_author | 通过 | `scenarios/g3-admin.cjs` |
| C-12 | qa_admin_rw、qa_author | 通过 | `scenarios/g3-admin.cjs` |
| C-13 | qa_admin_rw | 通过 | `scenarios/g3-admin.cjs` |

## 系列设定协作者

### C-1 作者邀请系列设定协作者 —— 通过
作者在 `/series/1` 底部面板（界面）邀请新注册账号。面板标题「系列设定协作者」；无「邀请角色」下拉、无「允许使用作者模型配置」复选框；确认框文本「邀请「g3_inv_52853」成为系列设定协作者？」；发送后成员行「g3_inv_52853 设定协作者 · 待接受」。
备注：场景表写「待处理」，界面实际状态文案是「待接受」（`MemberPanel.tsx` 的 `STATUS_LABELS`），验证意图一致，按通过记。

### C-2 受邀人接受并进入系列 —— 通过
受邀人从登录页登录，书架「待处理邀请」卡：「系列 · 剑影系列 邀请人：qa_author · 设定协作者」；界面点「接受」→「接受邀请」。之后 `/series` 列表项「剑影系列 设定协作者 2 部成员作品 · N 条共享设定」；详情页提示「设定协作者可维护共享基础；作品访问权限由作者单独授予。」。

### C-3 设定协作者的成员作品卡与页面动作 —— 通过
qa_series 打开 `/series/1`：重命名／加入作品／退出系列／删除空系列按钮均为 0 个；无「系列设定协作者」面板；成员作品卡显示「没有可查看的成员作品」，页面不含「剑影长歌」「剑影续篇」。直接访问 `/projects/1/overview` 显示「无法打开项目 项目不存在 返回作品书库」。
接口复核（均拒绝）：`GET /projects/1` 404；`GET /projects/2/chapters` 404；改名、加入作品、退出作品、删除系列 404「系列不存在」；读取／邀请系列协作者 404。

### C-4 设定协作者新建共享角色、编辑共享基础 —— 通过
第一部分（`scenarios/g3-series.cjs`，环境 `ainovel-accept-jjl9k1rk`）：qa_series 在界面「共享角色」新建「顾长风NNN」，提示「共享基础已保存」并出现在列表；编辑「沈青」简介后保存成功。作者系列页可见新条目与新简介；主作品「沈青」本书投影的 profile 随之更新，主作品角色页可见新简介。

第二部分：补核「各书本书记录的剧情变化不变」（`scenarios/g3-c4-progressions.cjs`，另起的环境 `/private/var/folders/h1/jwfsdnfs4lg5p4fcbkcwwp600000gn/T/ainovel-accept-9_ffxj8w`，同一提交与迁移头，已停止）。
- 准备：作者让续篇也采用「沈青」（`POST /series/1/entries/1/uses`）。两本书各自的真实设定变化经接口创建：先 `POST /projects/{pid}/setting-timelines`（`subject_kind=character`，本书投影 id）建时间线，再 `POST /projects/{pid}/setting-timelines/{tid}/progressions` 写入。这就是角色抽屉「字段与剧情变化」页签使用的接口。主作品写 3 条：第 5 章末 profile 追加「第5章山谷一战后左臂负伤，暂不能用剑。」、第 5 章末 current_state 替换为「在临江城养伤」、第 12 章初替换为「伤愈，随林远守城」。续篇写 2 条：第 1 章末 profile 追加「续篇开篇已任临江城守将。」、计划第 3 章初 current_state 替换为「镇守临江城」。
- 操作：qa_series 在 `/series/1` 界面「编辑共享基础 · 沈青」改简介并保存，提示「共享基础已保存」。
- 核对（作者，`GET …/progressions` 与 `GET …/effective?chapter_id=…&boundary=…`）：主作品变化记录 3→3 条，续篇 2→2 条，逐条的 id、字段、操作、值、锚点、revision、active、来源、备注完全一致。按章节回溯：主作品第 4 章初为纯新基础（无生效事件），第 5 章末与第 6 章初为「新基础 + 换行 + 第5章追加」、状态「在临江城养伤」（事件 [2,3]），第 12 章初与第 15 章末状态「伤愈，随林远守城」（事件 [2,3,4]）；续篇第 1 章初为纯新基础，第 1 章末为「新基础 + 续篇追加」（事件 [5]），计划第 3 章初状态「镇守临江城」（事件 [5,6]）。每个回溯点都等于「把编辑前结果里的旧基础替换成新基础」，状态与生效事件集合不变。
- 界面：作者分别在两本书打开「沈青」→「字段与剧情变化」页签，本书变化分别可见 3/3、2/2 条，页签内容含新基础；抽屉顶部显示「系列基础 · 本书演变」。截图在该环境的 `screenshots/g3-c4p-*.png`。

### C-5 已采用与未采用条目的删除 —— 通过
「沈青」：删除按钮禁用，旁注「已被作品采用，请系列作者处理删除。」，标签「已有作品采用」（不显示本数）。「临江城」：标签「尚未被作品采用」，删除可用；经「删除共享设定 · 临江城」对话框确认后提示「归属已更新」，列表中消失。
接口复核：qa_series 直接 `DELETE /series/1/entries/1` → 409 `series_entry_bound`；删除预检 → 409 `series_entry_bound`。

### C-6 收回指定作品 —— 通过
qa_series_editor（系列协作者兼续篇编辑者）新建未采用世界观「渡口石碑NNN」后点「收回指定作品」，目标下拉只有「剑影续篇」；检查影响后确认，提示「归属已更新」，续篇世界观列表出现该条目。把仅被主作品采用的「沈青」收回续篇的接口调用 → 409 `series_book_edit_required`。
qa_series（仅系列权限）：「收回指定作品」按钮禁用（`editableSeriesBooks` 无可选书）；接口直接收回 → 409 `series_book_edit_required`，`impact?action=move` 预检 → 409 `series_book_edit_required`。
备注：服务端拒绝码是 409 `series_book_edit_required` 而非 403/404；首轮脚本按 403/404 断言误报失败，已修正断言。属于预期的拒绝，不是缺陷。

### C-7 单书成员无系列身份；撤销系列协作者 —— 通过
qa_viewer（只有主作品查看权）：书架、作品概览、角色页均不出现「剑影系列」或「系列」字样；`GET /projects/1` 的 `series=null`；沈青 `origin={"scope":"book","series_id":null,"series_entry_id":null}`；`GET /series` 为空；`GET /series/1` 与 `/series/1/entries` 404。
撤销：qa_series 停在 `/series/1` 不刷新，作者在界面撤销其身份（「成员权限已撤销。」）。qa_series 在旧页面点「编辑共享基础」后，页面清空并提示「系列访问权限可能已变化，当前内容已清除」；刷新后不再显示系列内容，`/series` 列表不再有「剑影系列」；接口写入 → 404「系列不存在」；沈青简介未被改写。

## 管理员兼协作者

### C-8 读写管理员兼查看者 —— 通过
qa_admin_rw（管理角色 `projects.read`+`projects.edit`，主作品查看者）：书架卡片「剑影长歌 … 共享 · 查看者 … 作者模型未授权」；作品内无「管理员正在管理…」代管提示、无只读提示；在概览修改「一句话设定」保存，提示「已保存」，服务端已更新；在第 14 章正文末尾输入后 ⌘S，服务端正文包含新增文字；侧栏返回入口为「全部作品」→ `/projects`（不是作品管理）。

### C-9 AI 与作者专属入口 —— 通过
概览无「创作向导」「备份项目」「协作者」按钮，「项目智能体」按钮可见；打开面板后「新建会话」禁用、无「开始新会话」，显示只读说明（无会话时为「当前为查看者，只能查看本人已有的智能体会话…」，已有本人会话时为「此会话仅供查看 · 当前为查看者，只能查看本人会话，不能发送、确认、拒绝或撤销」）；章节页无「批量生成」；章节编辑页无「AI 助手」标签。
接口复核：章节生成 403 `project_readonly`；新建智能体会话 403 `project_readonly`；读取协作者 404；备份 404。
备注：与 2026-11-10 快照「页面没有 AI 入口」不同，最终代码按 `canViewAgent` 显示只读智能体，符合 C4 所述的预期变化。

### C-10 只读管理员兼查看者 —— 通过
qa_admin_ro（仅 `projects.read`，主作品查看者）：显示「当前为查看者，不能修改此作品」，无代管提示；「保存」与「一句话设定」均禁用。接口 `PATCH /projects/1` → 403 `permission_denied`。

### C-11 管理员被改为编辑者（有／无作者模型授权）—— 通过
作者在界面「协作者」抽屉对 qa_admin_rw「修改权限」为编辑者。
- 有作者模型授权、无本人模型：章节编辑页出现「AI 助手」，显示「未配置本人文本模型，将使用作者授权的默认文本模型」及作者费用说明；生成任务成功，出现在管理员本人任务列表、不在作者任务列表；智能体面板可新建会话并发送，收到假模型回复「好的，我已了解当前作品的情况。」，面板同时显示作者模型费用提示；会话只在本人列表中，作者列表不可见。
- 无作者模型授权、无本人模型：显示「尚未配置文本模型」，费用提示消失；接口生成 → 400「尚未配置文本模型…」。为本人添加模型配置后生成成功。

### C-12 撤销协作身份（对方页面不刷新）—— 通过
撤销前概览有「创作向导」「项目智能体」。作者经接口撤销后，管理员在未刷新的旧页面保存作品资料仍成功（管理员 `projects.edit` 的人工权限保留，符合规则）；约 15 秒前台权限轮询后，同一页面上「创作向导」「项目智能体」「导出」消失并出现「管理员正在管理「qa_author」的项目」代管提示。书架不再有「剑影长歌」；从 `/admin/projects` 进入：有代管提示，无 AI、智能体、导出入口，人工保存成功。
接口复核：章节生成、智能体会话列表、导出均 404；撤销前本人生成的任务不在任务列表，读取其输出 404。
观察（非失败）：重新被邀请为协作者后，此前本人建立的智能体会话再次可见（只读），与「只读查看本人会话」规则一致。

### C-13 管理员兼系列设定协作者对成员书做归属操作 —— 通过
qa_admin_rw 接受系列邀请后（此时已不是主作品协作者，但仍有 `projects.edit`）：在主作品采用共享条目、取消采用沈青、收回沈青到主作品、提升主作品本书角色「林远」的预检，接口均返回 409 `series_book_edit_required`。系列页「收回指定作品」对话框的目标下拉为空，显示「没有可编辑的成员作品」。管理员权限没有替代书籍编辑权。

## 发现的缺陷

无。场景表措辞与界面的差异只有 C-1「待处理」与实际「待接受」，验证意图相同。
