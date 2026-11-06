# 类型化设定字段：前端独立检查与修复

日期：2026-11-05。审阅角色：已派发的 `trellis-check`。活动任务：`11-05-typed-setting-fields`。

本报告覆盖字段子任务的全部前端已跟踪差异与新增文件，包含最早的规划选择器抽取，不只覆盖最后一片。实现者全部释放文件后完成自修与最终门禁。本报告不宣布整个字段子任务完成，也不替代后端、真实 PostgreSQL 或主会话的完整浏览器验收。

## 检查范围与依据

已加载保存的原生 hook 全文、check/implement manifests、字段 PRD/design/implement，以及父任务 implementation-contracts、planning-gate、fields-categories-ui 研究。以冻结设计为准，不把研究中的标签目录等备选当作字段实现要求。

对照 frontend 质量、组件、状态、类型、Hook 规范，以及 setting-fields/setting-progressions 跨层契约。只读比对了后端字段、卡片和事件 schema 与实际读路由；未修改后端、用户作品、任务状态、提交或 `pelican-cycling.html`。

| 范围 | 检查结果 |
| --- | --- |
| API/DTO/审计 | settingFields、progressions、cards 路径和 snake_case 参数与服务端相符；共享值请求为判别联合；null 清空、PATCH 省略、fields/timeline CAS 分开；审计两个字段标签同步。 |
| EntrySelect/规划抽取 | 通用控件通过注入目录分页；规划仍独立保留 relationship 的 timeline 身份及历史标记；字段引用仅四实体，失败不清空受控选择。 |
| 配置页/router/nav | 懒加载字段配置页、共享桌面/移动导航、适用类型筛选、定义/选项排序和停用；值和配置分开保存。 |
| 配置 controller | 同 actor 授权变化取消请求并保留草稿；失读清空；409 保留原快照与原 revision；读取最新后明确放弃旧稿才采用新版本；不自动重提。 |
| 类型化值/展示 | 四类控件和草稿、码点单行限制、选项/目标名称；停用选项仍可读，不能新选；历史值按身份回填，不显示内部 handle 或 JSON。 |
| 四实体 | 角色、世界观、组织、职业定义共用字段面板；组织与 career_entry 主体不混入成员或人物职业挂接；字段保存不提交外层表单。 |
| 首事件/事件修改 | field_id=null 的首写由 definition_id 进入复合 API；多行可 append，其他仅 replace；相同停用值的 PATCH 省略 value，明确清空保留 null；删除共享事件携带双版本。 |
| 历史预览/深链 | 四种详情 URL 和章锚解析、当前/起始值分离、章初末只读预览；修复跨 HTTP 双版本核对及同版本失败重试。 |
| 卡片 v4 | 解析局部身份、定义/选项/外部目标显式映射、默认 skip 不授权 typed 引用、选择变化后重新预览、409 保留原预览并明确比较采纳；v1–v3 调用继续兼容。 |
| actor/请求/刷新/草稿 | 读取与写入回调核对身份、作品、主体及请求代次；失效不回填旧响应；弹窗草稿独立保存；补齐列表行确认框的稳定身份与刷新失效。 |

## Findings (fixed)

### 1. 相同版本下，按章预览读取失败后的刷新没有重发请求

- File：`frontend/src/components/progressions/EffectiveSettingPreview.tsx:43`。
- Issue：错误区域仅调用父级 refresh。父快照重读成功但 revision 未变时，预览 effect 依赖不变，错误状态会持续；这是本次字段预览也使用的实际路径。
- Fix：刷新同时递增预览自己的 attempt，把其纳入请求身份。即使目录和时间线 revision 不变，也能显式重试；仍保留卸载/身份/AbortSignal 守卫，不触发写入。

### 2. 跨 HTTP 结果拼合时漏核对一侧目录版本

- File：`frontend/src/components/progressions/preview.ts:18`、`EffectiveSettingPreview.tsx`、`useProgressionPanelData.ts:140`。
- Issue：旧预览逻辑只验证 effective 的 timeline revision；entry-fields 验证了 fields revision，但 effective 自己的 fields revision 被忽略。事件冲突对照也只比较 detail 的 timeline revision。在配置竞争但条目时间线未变化时，可能拼合不同目录代次的结果。
- Fix：预览读取集中到 `loadEffectivePreview`，两接口透传同一位置和 signal；`assertSettingReadRevision` 同时检查 timeline/fields revision，并用于事件 detail 对照。任何不匹配都显示需刷新错误，不采用新保存版本、不自动提交草稿。
- Regression：`previewRequest.test.ts` 先复制原条件验证 RED：effective.fields_revision=3、预期和 entry-fields=2 时错误地 resolve；修复后 GREEN。测试同时覆盖两个实际客户端的 URL/位置/signal/credentials、任一 HTTP 目录版本变化，以及 503 后同版本显式重试。

### 3. 选择值缺少可键盘触发的明确清空操作

- File：`frontend/src/components/settingFields/SettingFieldValueControl.tsx:33`。
- Issue：主会话真实浏览器确认，原 `allowClear` 入口只有 aria-hidden 的 AntD 清除图标，无法作为清晰可访问按钮触发 null。
- Fix：下拉与引用有值时共用原生按钮“清空{字段名称}”，使用同一 disabled/archived 锁，明确发出对应 field_type 的 null。保留原选择器逻辑，未改变规划 EntrySelect。
- Evidence：主会话已在真实引用起始值弹窗按 Enter 触发“清空驻地”，经目录冲突读取最新并明确勾选对照后保存，基础值和当前值均显示“未填写”。下拉/null 的请求与纯逻辑合同由既有定向测试及完整测试覆盖。

### 4. 列表下标身份会把旧行确认框复用给新行

- File：`frontend/src/components/progressions/SettingProgressionsPanel.tsx:175`、`TextFieldsPanel.tsx:24`。
- Issue：事件与 legacy 列表未给 rowKey。实际安装的 AntD List 源码在没有 item.key 时使用 `list-item-${index}`；分页/刷新替换列表后，可继续复用旧 Popconfirm 实例。此前行确认框也没有双版本/授权刷新身份。
- Fix：两列表显式使用稳定 id。事件删除和基础确认框，以及 legacy 行操作区域，按 actor、目录版本、时间线版本和读取 loading 代次重建，读取/身份变化会撤掉旧确认。独立字段/事件编辑弹窗仍在列表外，已保留草稿不受影响。
- Verification：实际依赖源码与渲染路径检查；无新增镜像 DOM 测试。全部现有请求/草稿测试及最终完整门禁通过。未把该确认框失效声称为独立真实浏览器测试。

### 5. 卡片定义映射的选择器缺少可访问名称（浏览器追加发现）

- File：`frontend/src/components/entities/CardDefinitionMappings.tsx`。
- Issue：主会话展开真实 v4 卡片的定义映射时，DOM snapshot 中可见“字段处理方式”，但相邻 combobox 没有可访问名称；其他直接放置于无 name 的 Form.Item 下的控件存在相同问题。
- Fix：为处理方式、复用目标、每个选项映射以及改名输入补上含字段/选项名称的 aria-label，同时用 useId 创建唯一 id，与各自可见 Form label 的 htmlFor 关联。数据、条件分支、布局及映射合同不变。
- Review：`CardReferenceMappings` 原已把“选择「目标名」对应的类型”传给 EntrySelect，后者明确设为 Select 的 aria-label，无需重复修改。
- Verification：追加变更仅为标签；两映射文件的 scoped ESLint 退出 0，`npm run build`（含 TypeCheck）退出 0，构建无 warning/error；新增文件 no-index 空白检查无输出。按主会话指令未重复 914 全量测试；上一轮完整测试仍作为行为改动的证据，本次真实 AX/DOM 复验由主会话继续。

### 6. 条目选择器翻页时失焦收起（浏览器追加发现）

- File：`frontend/src/components/entities/EntrySelect.tsx`。
- Issue：主会话用真实 36 条候选目录确认，第二页 6 条由 API 正确返回；但点击 Next/Prev 后面板失焦关闭，必须重开才能使用结果。实际 rc-select 根 onBlur 会延迟关闭，分页输入/按钮的键盘事件还会继续冒泡到 Select 的选项处理。
- Fix：仅在 EntrySelect 的分页区域增加原生 DOM 焦点边界：鼠标操作非 input 分页控件时保留 Select 焦点；Go to 输入仍允许原生聚焦/输入；相关目标仍在分页区域时，在外层原生 div 的 blur capture 阻止 Select 将其当作离开。分页键盘事件在区域内部处理，Escape 仍传给 Select。没有自造定时器、控件替换或领域逻辑修改，原搜索、跨页已选名称、查询身份及错误守卫保留。
- Verification：scoped ESLint 退出 0；最终 `npm run build`（含 TypeCheck）退出 0，无构建 warning/error；entrySelectState、PlanningFields 适配、cardImportMapping、valueDraft 共 4 文件 16 例通过；no-index 空白检查无问题。按主会话指令不重复 914 全量；产品文件已冻结交主会话真实鼠标/键盘复验，不将单测或 HMR 过程当作最终交互证据。

## Findings (not fixed)

前端范围内没有剩余的已确认缺陷或需要未定产品决策的设计问题。

验收边界仍明确保留：本角色没有控制浏览器，也没有做真实网络故障注入；失败重试与双版本竞争的证据是请求测试及 effect/守卫代码检查，不能替代真实浏览器故障场景。主会话已补充卡片、只读和分页的实际验收，见下节；后端/PG门禁由对应角色负责。

## Verification

整体实现（含第1–4项修复）的完整门禁，均在 `frontend/`；随后第5／6项增量门禁见各自记录：

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| Lint | `npm run lint` | PASS，退出 0，零错误、零 warning。 |
| TypeCheck/Build | `npm run build` | PASS，退出 0，`tsc -b && vite build`；无 chunk warning。 |
| Tests | `npm test -- --run` | PASS，129 文件、914 测试；最终开始时间 22:38:48，9.63s。package test 本身为 `vitest run`。 |
| 已跟踪空白 | `git diff --check -- frontend` | PASS，退出 0。 |
| 新增文件空白 | 每个 untracked frontend 文件使用 `git diff --no-index --check /dev/null <file>` | PASS，36 文件无空白问题；未暂存。 |
| 图谱拆包 | `rg -l 'react-flow__' dist/assets --glob '*.js'` | 仅 `RelationGraph-CY6NrSLE.js`。 |

构建最大 chunk 为 vendor-antd 1158.45 kB，低于 1500 kB；设定字段配置独立懒加载 chunk 为 21.75 kB。测试输出有 Node 的既有 localStorage ExperimentalWarning，测试仍全部成功；未为消除环境提示修改产品或测试断言。

定向 RED→GREEN 测试完成后，最终完整门禁对包括行身份和可访问清空在内的最后产品代码有效；未提交、未归档、未勾选父验收。

## 主会话已回报的真实浏览器证据

以下由主会话在隔离 5179/8014 环境实测并通过协作消息回报，本角色没有将其冒记为自己执行：

- 四实体与四种类型值展示；第 5/21 章深链，角色第 20 章章初/末；职业定义独立字段保存保留外层名称草稿且不生成持有人。
- 配置两页 CAS 保留旧稿并阻止直接覆盖；375px 真实宽度无页面横向溢出；显示顺序和编辑按钮间距修复已截图复验。
- 起始值关闭/重开保留草稿；真实引用清空按钮可按 Enter；旧字段创建、遇目录冲突阻止、读取最新/明确对照/选择目标后显式绑定成功，原文保留。
- 卡片真实v4导出→显式复用四定义／逐选项映射／包外幽冥城具体目标→确认导入成功，新角色1、跳过组织1；新角色当前四类快照正确且事件为空。映射标签追加修复也已在真实DOM确认具名combobox。
- 用户明确授权后创建的fields_reader仅projects.read；真实登录可查看字段／历史／配置，保存／清空／记录变化禁用。截图已留存，不用API权限测试代替该证据。
- 分页修复冻结后，以真实36条目录复验：鼠标Next→2/2六条、Prev→1/2保持展开；Go to输入2＋Enter保持展开且不误选；Escape关闭popup；已选分页地点30返回第一页仍显示原选择。字段modal最终关闭，基础／当前仍未填写，未把验证草稿写入。截图`/tmp/ainovel-entry-pagination.jpg`。
- 最终0035模型的新隔离库已核对SQLite AUTOINCREMENT；重新seed后第5章与21章预览正常，停用项历史可见、新选择跳过，恢复启用成功。不能据此跳过尚待收敛的后端完整门禁。

## 规范同步交接

已有字段跨层 spec 和前端索引涵盖本任务的新合同。已向主会话交接三条应写入最终维护规范的实际经验：跨 HTTP 拼合同时核对目录和时间线版本；错误重试需独立请求代次而不能只依赖资料版本变化；Select 清空必须有明确键盘入口。行确认使用稳定身份，版本/actor/刷新失效与草稿保存分离，也应保留。

未扩大为分类实现、迁移设计或新公共 DTO；后续分类仍等待整个字段子任务门禁完成后交接。

## 2026-11-06：最终冻结代码的顺序完整门禁

本轮仅验证当前全部前端，**包含最后的卡片映射可访问名称与 EntrySelect 分页焦点修复**。产品与测试文件保持冻结，未修改测试、过滤输出、放宽 warning 标准、提交或改变任务状态。以下最终结果补全前面第 5/6 项仅做 scope 验证的阶段记录。

严格顺序执行 lint → build → tests，随后检查前端差异空白：

| 命令 | 最终结果 |
| --- | --- |
| `cd frontend && npm run lint` | PASS，退出 0，完整 ESLint，无 warning。 |
| `cd frontend && npm run build` | PASS，退出 0，`tsc -b && vite build`，无构建 warning；最大 vendor-antd 1158.45 kB，低于 1500 kB。 |
| `cd frontend && npm test -- --run` | PASS，退出 0；package 脚本实际为 `vitest run`，本次执行 `vitest run --run`。**2026-11-06 00:00:29 开始，129 文件、914 例全部通过，耗时 6.98s**。 |
| `git diff --check -- frontend` | PASS，退出 0，无输出。 |

测试原样输出 5 条 Node `ExperimentalWarning: localStorage is not available because --localstorage-file was not provided.`，属于 Node 运行环境提示；已如实保留，未禁用或过滤，也未改产品/测试配置来消除提示。Lint、TypeCheck 与 Vite 构建没有 warning。

### 冻结后的真实分页交互证据

由主会话在无新 HMR 的稳定页面，使用真实 8014 目录的 36 条候选复验并回报，本角色仅归档，不冒记为自己操作：

- 鼠标 Next 到 2/2，显示 6 条，面板保持 expanded；Prev 返回 1/2 也保持 expanded。
- Go to 输入 2 后按 Enter，面板仍展开，没有误选条目；Escape 只关闭 popup。
- 选择“分页地点30”后翻回第一页，已选名称正确保留。
- 最后关闭字段弹窗，底层起始值/当前值仍为“未填写”，验证草稿没有写入。
- 截图：[主会话分页复验](/tmp/ainovel-entry-pagination.jpg)；本轮已确认文件存在。

上述结果关闭第 6 项“待稳定页面复验”的阶段状态。当前前端验证 ownership 已释放，产品和测试继续冻结；后续分类仍由主会话按字段整体前置门禁决定，不能由本报告单独宣布整个字段任务或父任务完成。
