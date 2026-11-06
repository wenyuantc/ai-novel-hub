# 标签与设定自动分类：实施计划

状态：实现和前后端完整门禁及父集成通过；用户已于2026-11-06要求提交、收尾、合并main并推送。按指令交付归档，两项受浏览器策略阻挡的UI验收继续保留人工未验，C-AC8不冒记通过。

## 前置门

- [x] `11-05-typed-setting-fields` F-AC1–11全部通过，字段目录／EntityRef／批量时点投影和v4数据合同已冻结，字段完整check通过。
- [x] 当前迁移head为已交付0035；再次检查Git状态及其他会话修改。不得凭父子链接认为依赖已满足。
- [x] 用户已明确批准包含本子任务的最新最终摘要后才start。本任务不重写前置字段语义。
- [x] 每次分派第一行 `Active task: .trellis/tasks/archive/2026-11/11-05-setting-auto-categories`；实现／检查走Trellis子代理，缺原生注入则按本路径读取artifacts/manifests。
- [x] 共享模型、types.ts、迁移、transfer/effects单写者；所有worker都不是独自工作，不撤销他人变更，不处理无关pelican-cycling.html。

## C1：标签与规则持久化

依赖：前置门。

- [x] 四实体补setting_tags JSON、categories_revision、新类别／规则表和0036迁移；标签字符串与dropdown FK规则的互斥约束可验证。
- [x] 保留旧字段、章锚和字段目录版本；旧项目标签不填入条目，新属性默认空。
- [x] 有标签／规则资料时明确拒绝有损downgrade；PG/SQLite整作品级联和foreign_key_check通过。
- [x] 四实体SQLite AUTOINCREMENT落在ORM与0036真实重建；两建库路径×四实体×删最高／删空共16个HTTP场景，旧请求404且新tags／revision不变，旧全图／FK原开关／0035空态回环保留。

责任包：实体列／新模型→0036迁移→`test_setting_categories_migration.py`顺序。验收：有真实前置字段资料的0035库升级不改变其任何有效值或ID。

## C2：纯规则与有界查询

依赖：C1及字段共享projection。

- [x] 规范标签字符串、all/any与两种条件schema；非法空规则、未知类型、跨组选项拒绝。
- [x] 纯匹配函数按option ID比较，不比较label；缺字段／空值false；停用身份历史可读。
- [x] 共享目录分批候选→批量字段求值→规则匹配→累计total和请求页；先完整匹配后分页，读取无写、无逐行effective查询。
- [x] 当前／已有章／计划章及start/end的位置和原projection一致；标签和规则明确用当前版本。
- [x] 全部候选批次在单一只读一致数据库快照中完成；并发屏障中途改变字段／标签／规则时，只能返回一致快照结果或明确冲突，不混合跨批编辑。

责任包：`schemas/setting_categories.py`＋匹配纯逻辑；新query service＋`tests/test_setting_category_query.py`。测试包含跨类型后页、空页、下拉清空、同名不同ID、20章章末变阵营与有界查询数。

## C3：标签／分类API和并发

依赖：C2。

- [x] 类别CRUD／排序、规则原子替换、只读预览、结果分页、条目标签GET/PATCH和标签建议。
- [x] categories_revision CAS与fields_revision相容性校验；冲突整笔回滚，预览无持久化副作用。
- [x] 路由能力、人工管理员审计和项目屏障登记；只读preview是read权限，不能因POST就意外要求写或放开别的写请求。
- [x] 扩展字段／选项删除保护到分类规则；保持原实体删除、世界观换类和全作品清理合同。
- [x] 定义UPDATE同样保护“仅被规则引用但无值／事件”的情形；类型改变或缩范围破坏规则交集则409，改名／排序／停用正常，失败不改任何行或版本。人工／导入／effects复用同图校验。

责任包：新category service→`api/v1/setting_categories.py`及路由／权限→`tests/test_setting_categories.py`。检查点：定向测试和ruff通过，稳定API后才接UI。

## C4：v5数据兼容和效果恢复

依赖：C3。

- [x] projectv5／APP_SCHEMA0036、旧v4→v5补空、表拓扑与JSON/FK登记、LABELS/ENUM/预览校验；v1–v4继续升级，缺核心规则引用整笔拒绝。
- [x] cardsv5新增包内条目标签，原v4显式引用映射不变；旧卡片空标签，skip不覆盖既有标签，作品分类规则不进入卡片。
- [x] effects v4覆盖标签／类别／规则，补JSON/FK依赖、旧格式整轮预检及恢复后版本递增；不回退fields/categories revision。
- [x] 两目录版本同时排除备份及effects各自项目列集合，恢复从当前高水位递增，整轮失败不递增；回归新导入项目不携带源代次。
- [x] 软删／恢复和整库快照保留完整图；开放读取白名单不意外扩展。

责任按registry/remap→import/export→card→effects顺序分包，不与前置任务同时修改共享文件。运行项目备份、cards、agent effects、recycle-bin相关既有测试，追加v5真实往返和“后来新增规则阻止undo定义”的回归。

## C5：前端API和规则表单

依赖：C3接口冻结。

- [x] C5.A：`types.ts`／新`api/settingCategories.ts`镜像、API客户端／转换／错误code纯测试及审计／刷新资源。
- [x] C5.B：规则草稿与编辑状态／错误定位纯测试，复用冻结客户端。
- [x] 新懒加载设定分类页、router及共享导航；规则编辑支持类型、tag/dropdown条件及ANY/ALL，不提供手动成员维护。
- [x] 标签独立保存区接四实体详情，Select tags不复用别名／规划标签；当前整理口径文字可见。
- [x] 字段／选项停用和配置冲突可定位具体条件，元数据失败可重试；draft与原快照保留，不自动替换为最新revision重提。

责任包：客户端／纯逻辑→规则编辑器→页面与四详情接入；types.ts唯一写者。每包lint/build及相应测试。

## C6：结果、历史深链与失效

依赖：C2/C5。

- [x] 当前／按章切换、已有／计划章、章初／章末、类型／名称筛选和完整匹配分页；筛选改变回第一页。
- [x] 显示服务端匹配原因、标签和类型，区分空列表／失败；深链复用前置四类型helper并携带effective位置，历史值不回填基础编辑器。
- [x] actor／项目／分类／查询／位置及请求代次守卫；失写canEditNow，失读撤下内容，项目刷新不覆盖草稿。
- [x] 桌面Table、375px卡片、明暗主题和键盘操作；不加全局store或新依赖。

验证：URL／query状态／分页／draft纯测试＋真实浏览器快切、两页冲突、错误重试和只读。不能只靠mock断言DOM按钮存在来证明分类结果正确。

## 最终分类与父任务交接

```bash
# backend/
uv run ruff check .
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest
# frontend/
npm run lint
npm run build
npm test
```

- [ ] C-AC1–8、实际SQL／API早晚章节／ANY/ALL／后筛分页、v5往返、PG引用级联验证通过。
- [x] `trellis-check`复核本子任务全部范围；更新分类spec与索引并记录证据。
- [x] 移交父任务完成四字段＋分类＋实际消息＋恢复的一致性验收，之后才更新TODOS第4项。
- [ ] 按父流程统一准备提交清单、用户确认、归档与日志；不自行跳过批准。

## 回滚点

如字段projection或v4合同仍有缺口，回到前置任务修复，分类保持未开始；不复制第二份resolver。新标签／规则资料先做完整v5备份，拒绝有损降级；回退UI不等于删表，不回退作者后续字段或标签。

## 最终证据与外部阻挡

- 后端2408通过／旧0031可选PG一项跳过／既有SDK警告一项，真实退出0；本任务全部PG实跑。前端136文件986测试通过，lint／build／diff均0。
- 两处CUA发现（计划空锚URL、自身保存通知误stale）均RED→GREEN并实际复验。分类与标签双页CAS、四标签独立保存、221条后筛分页、四历史深链、375明暗、v5卡片新建／skip／reuse均实际通过。
- 只读页面及失败重试因站点保存权限／管理策略拒绝未执行，用户重授权后仍阻挡；不换端口／工具绕过，不把自动化当实际UI证据。详见父research/category-browser-verification.md。C-AC8及最终移交保持未勾，等待用户处理或明确接受现有证据。

## 收口授权

2026-11-06：在已说明两项UI受站点策略阻挡的前提下，用户明确要求“提交 收尾 合并到main并推送”。按现有代码与验证记录交付收尾；未执行的只读分类页／失败重试仍保留人工未验，不记为通过。
