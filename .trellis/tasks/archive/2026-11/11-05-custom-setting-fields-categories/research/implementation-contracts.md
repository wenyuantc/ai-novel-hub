# 实施上下文摘要：字段与分类

日期：2026-11-05。规划基于三份有源码 file:line 的详细研究；本摘要供原生上下文注入。最终权威为当前子任务 `prd.md`／`design.md`／`implement.md`，研究备选不得覆盖已冻结设计。

## 用户决定与交付顺序

1. 自动分类，规则来自标签或下拉字段。
2. 多行、单行、下拉、引用四类字段全部按章回溯，不能实施成静态后三类。
3. 父任务 `11-05-custom-setting-fields-categories` 协调；先交付 `11-05-typed-setting-fields`，门禁通过后才交付 `11-05-setting-auto-categories`，最终父验收。
4. 2026-11-05 最终摘要后用户回复“开始”，已批准完整方案，字段子任务可启动；分类仍须等待字段门禁。

## 时间线与配置

- 保留 `setting_text_fields.id`、`SettingProgression.custom_field_id` 和全部事件／章锚；旧基础文本转 JSON string，不按同名合并、不重放缺失历史。
- 新定义拥有 field_type、适用类型、名称和顺序，新选项拥有稳定 ID。legacy definition_id=NULL且label非空；共享绑定label=NULL，从定义JOIN；保留旧唯一键并加timeline+definition唯一键。
- 空共享字段只读LEFT JOIN为空槽，field_id可空。首次基础编辑／直接首章事件由复合API在同事务建绑定并写入，不做读时初始化，不用definition_id冒充custom_field_id。
- 新 timeline subject 为 organization（organization_id）和 career_entry（career_id）；原 career 是人物职业挂接，membership是人物组织成员。新主体baseline={}，不改变授职、阶段或成员事实。
- 类型固定text/line/dropdown/reference。内部值分别string、string、{option_id}或JSONnull、{reference_handle_id}或JSONnull；公开reference是EntityRef(kind,id)。codec一次规范化，FieldSpec驱动纯resolver，深复制对象。
- 多行append/replace，其他仅replace。PATCH省略不变，null只清空dropdown/reference；拒绝数字字符串、bool伪ID、额外键及跨字段选项。line最大500码点且不能有行分隔符；多行不裁剪旧文。
- fields_revision保护共享配置，timeline revision保护条目值／事件；不借用planning revision。旧/text-fields只写legacy，旧custom_fields仍字符串投影，新field_values为类型化结果。
- 旧事件路由保留expected_revision，共享事件新增强制expected_fields_revision；PATCH在内部canonical命令合并，公共value只编码一次，所有返回统一解码。读目录／字段要前后核对版本，不能旧资料配新版本；跨批分类用一个只读一致快照。
- 停用与新选择校验分离，历史旧值继续求值；改事件备注不迫使清空。已有绑定时不原地换类型，缩小适用范围不得排除保留内容；旧world category PATCH同样检查。

## 引用安全、AI和旧入口

- 稳定handle有project CASCADE及四选一实体FK NO ACTION。引用JSON→handle没有真实FK，仍必须做服务／导入／撤销语义保护；不得声称“外键已自动保护全部JSON”。
- 所有保留基础值和事件含inactive/orphan都参与目标依赖检查；普通DELETE、向导准入与落库、effects恢复共用保护，409给出来源。无引用句柄只能在项目锁内证明后内部清理。整作品PG/SQLite级联必须实际验证。
- 只回溯引用所选目标身份，显示名用当前名称；不递归加入目标正文、不自动选角。字段／选项／条目名称不新增时间线。
- build_setting_context、planning_context_service及analysis_service的既有选中范围共享批量resolver和格式器，独立组织／职业字段不是挂接字段。所有新增字段计入原预算；世界观／角色选择范围不因本任务扩大。
- 所有创建路径要核验：人工、new_character/new_org/new_career、with-relations/members、batch、wizard、book_import/reverse、card_service直接Organization构造、project importer通用插入。空槽不能让无资料向导永远被保护阻止。
- agent工具schema与references白名单、provider显式类型、预览ID归一、effects全表／JSON依赖及整轮旧效果预检一起补；目录版本恢复后递增不回退。开放API保持独立白名单。

## 标签和分类

- 标签选用四实体setting_tags字符串数组，当前整理元数据，非中心可重命名标签库。去空白／去空／去重，大小写精确，50项×50码点；不复用作品tags、别名或规划标签。
- 分类存适用类型、all/any和1–20条规则。tag规则存字符串，dropdown存definition/option真实FK；分类结果不持久化，规则删除不删条目。
- 下拉用所选chapter/planned+start/end有效值；标签及规则用当前保存版本。当前position=None包括全部有效计划变化，不能误写成最后已写章节。
- 缺字段／null false，停用选项历史身份仍匹配；先类型及名称筛候选，分批求值完整规则后累计total和请求页，不能先分页后筛、前端N+1或只统计当前页。
- 规则依赖同时保护定义UPDATE和DELETE；只有规则、零值／事件也不能改dropdown类型或缩范围使规则失配，改名／排序／停用保留规则。
- categories_revision保护规则／标签，fields_revision变化要重新验证规则兼容；只读preview明确登记read权限。历史深链携带位置，不能回填基础编辑草稿。

## 迁移和格式

- 当前仓库head0034。字段0035/projectv4/cardsv4/effectsv3；分类0036/projectv5/cardsv5/effectsv4。实施前重核head，保留逐级旧版转换，不重写已冻结v4含义。
- RULES同时是备份插入／effects恢复拓扑：定义→选项，实体→handle→timeline→绑定→事件，类别规则在定义／选项后。新增FK和JSON列显式登记，JSON精确重映射，不把普通整数和用户文本当ID。
- 缺核心引用整笔拒绝，不能警告后置空；全部保留历史引用的停用选项／句柄都导出。
- cardsv4是当前快照，包内ref在先建实体后解析，包外ref在preview要求显式本作品映射；未解决整笔拒绝，绝不按同名猜测或造空壳。现有卡片无world容器，world目标作为外部ref；v5携带条目标签，不携带作品分类规则。
- 包内引用遇同名默认skip也保持未解析，须显式确认具体复用目标或重命名新建。目录版本从备份PROJECT_RULE和effects独立项目快照两处排除；undo从当前高水位递增，不从before恢复；失败不递增、新导入用新代次。
- SQLite改父表要有真实子行验证不误级联，PG验证JSONnull/CAS/NO ACTION与purge。新数据旧版不能表达时拒绝有损downgrade，保存升级前与新格式完整备份。

## UI和检查

- 字段配置和值／事件独立保存；四类型控件共用，旧字段来源可见。组织／职业自身字段与人物关系／阶段分开。
- 复用抽取规划分页EntrySelect及四类型URL helper，不依赖规划revision/tags，也不扩大提及实体范围。
- read/write权限实时复查；actor/project/entry/category/position/request代次失效，冲突保留草稿与原快照，不刷新revision自动重提。只读、375px、主题、键盘、错误重试和两页竞争真实验证。
- 最终后端ruff+完整pytest、前端lint/build/test、真实PG及隔离浏览器；FakeLLM断言5/20/21章实际messages，字段预览、分类、备份恢复一致。

## 详细依据（按当前切片直接读取）

- `typed-field-timeline.md`：模型／resolver／context源码锚点、JSONnull文档、旧API适配。
- `persistence-and-writers.md`：每条创建／删除入口、RULES／remap、cards、effects、真实head证据。
- `fields-categories-ui.md`：页面／组件／导航／权限／深链锚点；中央标签目录是研究备选，设计已明确采用字符串标签，不误报为缺失。
- 较大的spec不能因自动注入限额而跳过：backend/database-guidelines、project-transfer、story-entities、agent、rbac-quotas、recycle-bin；frontend/component-guidelines、type-safety、hook-guidelines和state-management。先看索引，再按当前切片读原文相关章节。
