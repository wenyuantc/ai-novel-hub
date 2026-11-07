# 独立场景闭环：实施与验证计划

状态：completed（工作提交89e6da9，任务已归档）。D1–D5和最终规划获用户明确“开始”批准后已按E0–E5执行；最终后端2669、前端147文件1054测试及双数据库／浏览器门禁通过，证据见verification.md。用户已授权后续合并main／推送，Git结果另行核实，不把归档等同于已部署。

## 1. 交付组织与责任边界

本次保留一个跨层任务，不创建可独立发布的子任务：统一迁移后所有正文写入口必须一起切换，否则基础层单独发布会被旧写入覆盖。以下是有依赖的执行切片，不是独立上线的功能。

Phase 2 按项目配置使用 Trellis 实现／检查子代理；主会话负责规划、上下文、跨片整合、spec、文档和最终审阅。每次派发以“Active task: .trellis/tasks/11-07-independent-scenes”开头，明确本轮拥有的文件／行为、依赖和验收，不同时把共享文件交给多个写代理。

所有写代理必须获知：不独占代码库，不回退他人编辑；发现接口需修改先与主会话协调。优先 native 注入，缺失时按角色拉取上下文并阅读 prd.md／design.md／implement.md 和相关清单。以下责任切片已按用户最终“开始”授权执行，实际派发、接口协调及修复见 execution-log.md。

### 依赖与建议所有权

| 切片 | 责任范围 | 前置 |
|---|---|---|
| E0 合同与 RED | 核对当前 head、现状基线和边界测试，冻结场景 schema／错误／投影合同。 | 最新方案获批且任务已 start |
| E1 数据与权威写入 | 新 scenes 模型／schema／迁移／服务；Chapter revision；初始化全部建章路径；结构版本、项目注册表／导入映射的核心形状。 | E0 |
| E2 规划与故事位置 | planning 查询／写入／生命周期／矩阵／支线，progression schema／解析／物化／位置 UI 合同、系列引用预检。 | E1 合同稳定 |
| E3 AI 与兼容入口 | generation／rewrite／task handler／提示词、局部和 Agent 文本写入、effects；接入 E1/E2 原语，不重复实现正文存储。 | E1、E2 |
| E4 前端闭环 | api/types、编辑器拆分、场景规划控件／网格／排序／版本／提及定位／任务状态，沿用 Ant Design。 | E2/E3 对外合同冻结；可与 E3 的不同文件有限并行 |
| E5 跨层与收尾 | 后前端全门禁、真实 PG／浏览器、故障与权限验证、README／TODO／spec，独立 check。 | E1–E4 |

迁移、注册表／remap、共享 types、generation_service、effects 等高风险文件每阶段只能一个明确所有者。下一切片若需调整上片合同，先记录差异并重跑相关门禁；若改变产品行为，返回规划而不是代理自行缩减范围。

## 2. E0：实施前合同与基线

- [x] 确认分支、未提交变更、current task 与迁移 head；保留原有两个 source-command skill 未跟踪目录，不纳入本任务。
- [x] 阅读 implement.jsonl 中本轮相关规范；复核 design 中 SourceKind／StoryPosition／SceneOut／旧接口的合同。
- [x] 核对现有后台任务、测试 DB／真实 PG 的安全边界；所有服务、FakeLLM 和浏览器数据使用隔离临时目录，不改用户作品。
- [x] 建立或记录现有质量门禁基线；已有失败保留原始证据，不能删断言／改 fixture 形状冒充通过。
- [x] 关键边界测试已覆盖：单初始场景、Unicode 光标无损、旧多场景覆盖拒绝、继承与明确清空、同章后场景设定隔离、结构版本无正文变化也可恢复；独立审查另有真实RED→GREEN证据。
- [x] 如当前 head 不再是 0038，统一更新 migration／APP_SCHEMA 的计划编号后再写代码。

## 3. E1：场景基础、无损升级和结构历史

- [x] 新增 Scene 稳定身份与活动顺序、章 content_revision、规划 scene 来源／本地角色所需结构、带版本的结构快照和 scene 锚点列。
- [x] 实际 Alembic 回填：所有旧章逐字符一个场景，空章／回收站／旧历史及所有外键子记录保留；不写只读修复逻辑。
- [x] 实现单一投影／码点区间计算、初始化和只 flush 的正文原语；外层持锁、CAS、审计、版本与提交一致。
- [x] 遍历真实建章路径：手工、from-outlines、plan 新建／复用、拆书、备份、向导／Agent；显式初始化，不靠隐式 ORM 写钩子掩盖遗漏。
- [x] 实现同章增改／重排／拆分／合并预检／退场。保留最后一个空场景；冲突元数据需作者选择，无法等价表达的保留锚点／独立规划阻止操作。
- [x] 新结构基线／记录／恢复与 D3b 旧历史提取；普通正文版本合并保留，结构操作与清空强制留边界；不回滚独立时间线／章默认。
- [x] 升级项目备份 v7／schema；登记表顺序、typed FK、覆盖模式、结构 JSON 映射；旧 v1–v6 正式逐级转换，非法夹带／投影矛盾拒绝。
- [x] 适配确实需要新不变量的直接 ORM fixture／seed，不把 GET 改成写库，也不改变旧测试的字数／原文／权限断言。

E1 门禁：新场景核心／迁移／历史测试，原 chapters／versions／outline plans／book import／project transfer／reader／export／epub 回归，注册表反射与导入拓扑测试。数据库回填和 API 新建两条路径都必须有证据。

## 4. E2：规划、继承与场景演变

- [x] PlanningUnit 的场景来源、原始覆盖状态与本地角色关联；统一有效规划解析，不在各页面／builder 各自 union。
- [x] 默认继承、按项覆盖、明确清空、恢复继承及批量操作；覆盖一类不改变其他类，不反写 ChapterCharacter。
- [x] SourceKind、矩阵场景行与章分组、标签／全类型引用、继承来源、正文统计、网格目录的分页和版本一致性。
- [x] 场景节点／交汇与独立 scene 交织轴；距离按完整轴算，和章／计划轴分开；不让每个场景重复完成整章背景规划。
- [x] StoryPosition／锚点判别联合、旧 start/end 兼容、同号章测试、新 scene start/end、孤儿和稳定顺序。
- [x] 排序、改号、退场及结构恢复后，在同事务重验全部受影响历史／当前投影；非法结构事实整体拒绝。
- [x] 扫描字段／分类按位置查询、系列投影和有效设定 UI 等所有位置消费者，更新联合类型，避免只改纯 resolver。
- [x] 规划脱离／重连、binding_epoch、retired、大纲快照、建章／重展开保留原合同；场景结构历史不能认领已改挂来源。
- [x] 系列取消采用／归属调整和真删除保护覆盖场景本地／历史引用；旧共享设定与 never／字段隐藏不旁路。

E2 门禁：新继承／scene planning／scene progressions 测试，planning 全组、progressions 全组、setting fields/categories、series projection/reference guards。用 before_flush 屏障证明新只读入口无写入。

## 5. E3：场景 AI 和所有旧正文写入

- [x] 保留原任务类型／章资源键，request 严格携带可重放目标；重试、标题／链接、Skill 快照、暂停／取消／回收站行为一致。
- [x] 单场景生成／润色／建议重写与整章顺序编排；总目标字数分配可见，不把整章目标重复用于每个场景。
- [x] 一致上下文：有效场景规划、手动角色替代、scene start、前场景衔接、正确预算与锁定范围；预览与真实调用共用。
- [x] 实际 guard_provider 保持原锁序和策略检查；场景／规划／相关时间线版本失效明确停止，不降级重试旧载荷。
- [x] 整章场景结果暂存，全部主成功后短事务统一保存；中途主失败／取消不半写；极晚取消按真实提交；批量断点仍逐章。
- [x] 原自动续写 warning／次数上限保留，限制在当前场景；整章成功只触发原章分析一次，独立场景改稿使旧分析过期。
- [x] 局部五种操作的码点／UTF-16、原文／before／revision／log 绑定；旧接口单场景桥接，跨场景预览前拒绝。
- [x] 旧 PATCH、version restore、Agent replace、effects restore 和 Core UPDATE 全面检索，禁止直接改兼容章正文的遗留旁路。
- [x] effects v6 注册场景及新签名，运行版本不倒退；旧不安全多场景效果明确拒绝，不封禁无关历史。
- [x] 默认模板、registry、builder 与旧覆盖兜底保持一致，不在 service 硬编码新提示词，不泄漏隐藏字段。

E3 门禁：捕获实际 FakeLLM messages，覆盖场景／整章／批量／自动续写／局部五类、旧自定义模板、never／隐藏字段、系列采用、未来场景与同号章；可控事件屏障验证创建／发送／提交间竞态，不能靠 sleep 猜时序。

## 6. E4：前端工作流

- [x] 建立 src/api/scenes.ts 与严格 types 镜像，统一错误解码、分页／DELETE 版本响应、target 与 snapshot 守卫；页面不得直接 fetch。
- [x] 编辑器以场景身份管理正文和摘要草稿、保存快捷键、离开／切换保护、正确 AI 目标和整章只读通读。
- [x] 提取场景导航、正文／规划覆盖、操作对话框等组件，避免再扩大单个 ChapterEditorPage。
- [x] 规划网格／矩阵场景粒度、来源标识、字段继承／清空、全类型选项与批量范围清楚；先合同后实现控件。
- [x] 章内拖动和上／下移；拆分传码点，合并预览／冲突处理；不出现跨章移动 UI 或修改 chapter_id 的 API 参数。
- [x] 新旧版本标识、结构影响预览和旧纯文本明确提取；不把多场景的旧稿“提取”写成“整章已恢复”。
- [x] 提及深链的指纹与范围解析、跨边界整章预览；任务完成刷新不覆盖当前另一 Scene 草稿。
- [x] actor／项目／章／scene／授权代次隔离，409 保留输入与拟排序，确认框随失效关闭，错误可见、可重试。
- [x] 375px／触摸、键盘、明暗主题及只读；复用现有 token 和订阅，不为排序新增重依赖，不以卸载取消后台任务。

E4 门禁：新增 API／继承控制器／场景草稿与偏移／排序与版本 UI 测试，原 tasks、planning、textOffset、reader、theme 回归；实际 lint／build／全量 Vitest。

## 7. E5：集成验收和质量门禁

### 自动化命令

以下命令分别在对应工作目录执行，不把失败后的下一个命令继续执行结果当作前一个通过。新增测试文件名在实现时可按职责调整，但 AC 映射和验证范围不能因此缺失。

| 工作目录 | 命令 | 用途 |
|---|---|---|
| backend | uv run ruff check . | 完整静态检查 |
| backend | uv run pytest tests/test_scenes.py tests/test_scenes_migration.py tests/test_scene_versions.py | 已新增的核心／实际迁移／历史 |
| backend | uv run pytest tests/test_scene_planning.py tests/test_scene_progressions.py tests/test_scene_generation.py | 已新增的规划／演变／实际 AI 消息 |
| backend | uv run pytest tests/test_chapters.py tests/test_chapter_versions.py tests/test_project_transfer.py tests/test_agent_effects.py | 旧关键入口及注册表回归 |
| backend | uv run pytest | 最终全量后端门禁 |
| frontend | npm run lint | 完整 lint |
| frontend | npm run build | TypeScript 与正式构建 |
| frontend | npm test | 全量前端测试 |
| 仓库根 | git diff --check | 已跟踪未暂存差异空白 |
| 仓库根 | git diff --cached --check | 按用户批准暂存后覆盖新增文件的空白门禁，不在规划阶段擅自暂存 |

当前未跟踪文档使用 no-index 单文件检查；退出 1 表示存在新增内容，不等于空白失败，真正错误须保留原输出并处理。

### 真实 PostgreSQL

新增 tests/test_scenes_postgresql.py，使用专用 AINOVEL_SCENES_POSTGRES_TEST_URL，严格限制本机 scenes_test／scenes_verify，并使用 UUID schema／独立连接。参考既有 test_series_postgresql 的隔离和清理方式，不把新 fixture 指向用户开发／生产库。

- [x] fresh 全迁移与 0038 存量升级，含全子记录与保留历史。
- [x] 同一 content_revision 的两个结构／正文写请求只成功一个；项目锁与 unique 约束真实生效。
- [x] 同章顺序互换不撞中间唯一约束，事务失败不残留临时 rank。
- [x] 拆并／删除／恢复中所有 FK 及 planning source 关系正确；整作品真删除完成，孤儿事件合同无假恢复。
- [x] AI 当前章最终保存与取消／规划变化／结构过期的竞争，提交后终态如实。
- [x] 外键检查、SQLite 身份高水位与 PG 序列重映射不倒退；有新作者场景数据时有损降级被拒绝。

环境缺失可明确报告“未运行”，但在本任务最终质量门禁中不能把该项标成通过或用 SQLite 替代。

### 真实浏览器矩阵

- [x] 隔离新章 → 三场景正文与摘要 → POV／标签 → 矩阵与有效预览；旧章真实升级及全引用类型组合另由SQLite／PG与契约测试覆盖，不冒充手工浏览器穷举。
- [x] 章默认变更只影响继承项，明确清空／恢复继承跨入口一致。
- [x] 拖动／按钮排序、光标拆分、相邻合并、保留锚点阻塞；刷新后检查真正服务端内容。
- [x] 场景 AI 与整章 AI、局部五类、批量、续写和中途取消，核对 actual FakeLLM 请求及原稿未半写。
- [x] 新结构历史恢复、旧纯文本预览／提取、完整备份恢复和阅读／提及跳转；TXT顺序经UI和HTTP核对，Markdown／EPUB实际下载文件与XHTML解析核对。
- [x] 双页409保存失败、撤权、只读；失败时输入保留，核对最新内容后显式重试。任意延迟响应顺序由自动化可控回归覆盖，不标为全部人工操作过。
- [x] 375px、明暗主题、Tab／按钮排序与无拖动操作；正文和网格不产生无法使用的横向溢出。

浏览器行为不能用 SSR／API 测试替代；因站点策略或环境未完成的项目单独列出，不写“已验收”。

## 8. AC 到证据的责任映射

| AC | 主要证据 |
|---|---|
| 1–3、8、12 | E1 核心与迁移、E4 UI／偏移／排序、浏览器 |
| 2、6、11 | E2 有效规划／CAS／只读、E3 真实 AI、E4 来源控件 |
| 4、10、15、19 | E1 结构历史／备份、真实双数据库、旧格式／失败原子性 |
| 5、7、9、14、17 | E2 resolver／位置、E3 FakeLLM／事件屏障／批量／续写 |
| 13、16 | 跨作品／只读／撤权、系列依赖、真实出站、Agent／effects 新旧格式 |
| 18、20 | reader/export/epub/mentions 回归、前端全门禁、真实浏览器矩阵 |

## 9. Spec、README、TODO 与独立检查

- [x] 主会话按实际实现新增 backend/scenes.md，并更新相关 index、planning、setting-progressions、project-transfer、series-settings、mention-tracking／reader 的真实跨层合同；不把设计中的未来接口当成已落地 spec。
- [x] 同步 README 的功能入口、流程、迁移／备份与限制；TODO 第 7 项只有实际验收后勾选，第 1、3 项的场景边界说明同步。
- [x] 使用 trellis-check 做最终全范围检查，重审新增表反射、核心引用、旧写入检索、真实模型输入、权限与数据流；修复后重跑受影响与完整门禁。
- [x] 记录通过、失败、修复、跳过和未验的命令／时间／日志位置；不伪造首轮全绿。
- [x] 用户已确认Phase3.4完整提交计划，并明确授权收尾、合并main及推送；执行时保留工作提交→独立归档／日志→合并／推送的顺序。

## 10. 回退点

1. E0／实施前：停留规划或撤销本任务文档即可，不触碰用户原变更。
2. 升级前：排空写作任务并保留整库备份；新迁移／旧代码不能混跑。
3. 尚未产生新场景作者数据：失败时按实际迁移的无损条件回到升级前部署，必须实测。
4. 已有新结构、规划／锚点或新历史：禁止无提示 downgrade／丢表。先保全新数据与备份，再请求作者决定回退，不凭章投影仍在宣称无损。

只有全部必需门禁和 AC 有真实证据后才报告交付完成。复选框按实际证据更新；完整命令、首轮失败与修复及20项AC证据见 verification.md。提交／归档仍需遵守后续确认。

## 最新实施批准

2026-11-07，主会话呈现最终范围、边界、风险、20 项验收和三份规划产物后，用户回复“开始”，明确批准按当前方案进入实施。此批准晚于最终规划摘要；此前单项规划确认不作为替代。
