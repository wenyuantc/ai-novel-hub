# 第 13 项实施与检查上下文导航

## 用途与加载规则

本文件是调查结果和规范的有界入口，不代替任何拥有源。用户已在最终整体摘要后明确批准实施，各子任务仍须按顺序启动。

当前注入上限为单份引用文件 32768 bytes、整份上下文 131072 bytes。部分规范和调查报告超过单文件上限；把所有领域规范同时内联也会超过总上限。四个任务的 manifests 因此内联通用质量规范及本导航，具体领域材料由承担该步骤的代理按下面的清单分段加载，不能把“只看到路径”或“内容被截断”当作已经阅读。

- 先完整阅读活跃子任务的 `prd.md`、`design.md`、`implement.md`；父任务三文档负责组合契约和版本顺序。
- 完整读取该领域的研究报告，再按实际修改层读取以下适用规范。长文先查看标题，按章节分批读取至该契约结束，不只读前 32768 bytes。
- 若首次注入提示截断或只提供索引，主动读取原文件；派发 prompt 必须指出本导航和实际 Active task 路径。主会话在实施／检查交付中核对加载记录。
- 规范与当前源码拥有源优先于研究中的历史行号。研究给出的锚点用于定位，实施前重新确认迁移 head、版本常量、函数签名和文件状态。
- 本导航不授权产品修改、运行迁移或跳过测试；没有加载某段材料不能用“manifest 校验通过”替代。

## 已收敛的组合边界

- D13.1：配置＋可选设定／大纲，新书正文为空。模板是严格白名单快照，当前档案值转为新书起点；不复制原章节／场景、旧历史、系列绑定或授权。
- D13.2：作品署名＋笔名分组，每书最多一个笔名。私人库只有本人管理；协作者只读获授权作品上的名称。
- D13.3：本地上传覆盖角色、世界观（含地点）、组织、职业；上传／替换／删除及列表／详情展示。系列共享仅沿用原角色／世界观边界。
- 实施顺序：笔名 → 缩略图 → 模板 → 总体验收。单书备份 v9 → v10 → v11；图片阶段设定卡 v6 → v7、effects v6 → v7；模板内部格式另为 v1。
- User／项目／系列锁序、原 actor 复验和一次提交是跨层合同。共享文件不得被不同子任务同时改写。
- 图片资源不可变，旧图保留到所属作品／系列硬删以支持撤销；当前绑定才进入本地 effects，bytes 不进入 AI 输入或通用 effects。
- 模板保存／建书均原子化。成功意图收据保留去重事实；重试先找原结果，再检查新建条件，禁止 receipt 行锁 → Project 的反向锁序。

## 研究报告入口

路径均相对仓库根目录。

| 负责范围 | 必须完整读取的调查材料 | 用途 |
| --- | --- | --- |
| 总任务集成 | `.trellis/tasks/11-16-pen-names-templates-thumbnails/research/current-capabilities.md`，以及各领域报告的规划对照结论 | 来源需求、已确认决策、当前入口与集成风险。 |
| 笔名 | `.trellis/tasks/11-16-pen-names-templates-thumbnails/research/pen-name-design-evidence.md` | 同作者复合 FK、User 屏障、私库投影、所有创建入口、回收站、署名恢复及双 EPUB 路径。 |
| 缩略图 | `.trellis/tasks/11-16-pen-names-templates-thumbnails/research/thumbnail-design-evidence.md` | 图像解码依据、作用域模型、系列转换、effects-only 绑定、备份／卡片、身份隔离、迁移与验证。超过单次内联上限，分段完整读取。 |
| 模板 | `.trellis/tasks/11-16-pen-names-templates-thumbnails/research/template-design-evidence.md` | DTO 白名单、依赖闭包、当前值与 baseline、无提交原语、风格例外、幂等收据和规划对照补齐项。超过单次内联上限，分段完整读取。 |
| 模板图片／总体验收涉及图片 | 上述缩略图报告第 5–8 节及其完整子任务设计 | 资源复制、共享图投影、当前图转移和图像上限；若修改图片实现则升级为读取完整报告。 |

## 领域规范加载清单

三个任务共用 `.trellis/spec/backend/quality-guidelines.md`、`.trellis/spec/frontend/quality-guidelines.md`、`.trellis/spec/guides/cross-layer-thinking-guide.md`，已直接写入 manifests。以下清单保留原有领域材料及理由，并未取消相应要求。

| 规范原文件 | 适用任务／步骤 | 必读主题 |
| --- | --- | --- |
| `.trellis/spec/backend/database-guidelines.md` | 全部后端与迁移 | Engine／session、事务、user_id 隔离、原子迁移、部分更新、长度限制、Migration、命名与外键。若涉及任务／生成记录，再读其对应场景。 |
| `.trellis/spec/backend/rbac-quotas.md` | 笔名、模板；缩略图的鉴权调用 | 原 actor、权限复验、User 屏障和额度，不能把内容编辑能力扩为私人库权限。 |
| `.trellis/spec/backend/recycle-bin.md` | 笔名解除关联、模板结果回收／恢复／清理 | 回收站占额、活跃作品守卫、purge 生命周期与锁序。 |
| `.trellis/spec/backend/project-transfer.md` | 三项注册表、备份、恢复和兼容 | 表反射、RULES／NOT_EXPORTED、格式版本、授权、体积、恢复事务和旧格式。 |
| `.trellis/spec/backend/open-api.md` | 笔名署名和开放 EPUB | 既有开放授权与显式 schema；不能暴露用户私人库或扩大管理入口。 |
| `.trellis/spec/backend/project-cover.md` | 缩略图上传与缓存 | 现有图片鉴权、大小限制、版本与失败保留原图模式；本方案另有真实解码契约。 |
| `.trellis/spec/backend/series-settings.md` | 缩略图系列转换、模板读取共享投影 | canonical／投影、Series → 排序项目锁、source_stamp、提升／采用／移回／退出。 |
| `.trellis/spec/backend/story-entities.md` | 缩略图卡片、模板关系／职业 | 组织、卡片导入导出、关系及关系类型、职业／阶段；涉及分析写回时再完整读该场景。 |
| `.trellis/spec/backend/agent.md` | 缩略图删除／撤销及 effects 接入 | 工具层契约、提案确认、效果快照与撤销、公共投影和安全规则；新图不能进入模型上下文。 |
| `.trellis/spec/backend/setting-progressions.md` | 模板当前值转初始基线 | 当前投影、planned／confirmed、baseline、历史身份隔离和写入原语。 |
| `.trellis/spec/backend/setting-fields.md` | 模板严格 DTO、typed 引用与配置 | 字段定义／选项、编码器、引用重映射、用户可见原始值。 |
| `.trellis/spec/backend/setting-categories.md` | 模板分类配置／规则 | 配置目录、默认值、依赖闭包和规则重建。 |
| `.trellis/spec/backend/setting-ai-visibility.md` | 模板 AI 策略与 links | 人工快照保留隐藏值，AI 过滤不能代替保存；策略和 links 按新身份恢复。 |
| `.trellis/spec/backend/outline.md` | 模板章纲／细纲 | 顺序、角色引用和章纲／章节边界；复用无提交原语，不直接拼接导入服务。 |
| `.trellis/spec/frontend/type-safety.md` | 全部前端／API 客户端 | 类型组织、PATCH 语义、校验、API 客户端契约、错误矩阵、禁止模式；涉及备份传输时读其专节。 |
| `.trellis/spec/frontend/state-management.md` | 全部前端 | actor／project／请求代次守卫、卸载／切换失效、脏表单和局部状态更新。 |

## 长规范章节定位

2026-11-16 标题定位仅用于导航，后续以当前文件标题为准：

- `database-guidelines.md`：事务与查询在前半，Migration／外键在中段；生成记录、后台任务及错误模式在后半。不能只加载开头而漏掉迁移合同。
- `type-safety.md`：类型组织 → PATCH／时间戳 → 校验 → API 客户端契约 → 禁止模式 → 管理员整库备份传输。
- `story-entities.md`：人物关系 → 组织 → 卡片 → 章节上下文 → 向导 → 职业 → 分析写回 → 伏笔。按目标场景读完整规范及必需测试。
- `agent.md`：工具协议 → 工具层 → 提案／确认 → effects／撤销 → 运行／暂停 → 工具事件事务 → 安全规则。图片接入不可漏读 effects 后半的共享保护和事务要求。

## 证据边界

规划阶段只完成规划、只读调查和文档校验。产品测试、真实 PostgreSQL 并发、图像处理打包、迁移和浏览器验收均列入实施计划；完成声明必须依据后续 `verification.md` 的实际结果。
