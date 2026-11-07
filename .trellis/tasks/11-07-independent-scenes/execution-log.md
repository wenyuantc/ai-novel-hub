# 独立场景实施记录

## 开始与边界

- 2026-11-07：用户在最终规划摘要后回复“开始”，已记入三份主产物；task.py start 成功，状态 in_progress，分支 codex/independent-scenes。
- main 只协调／验证／维护任务文档；foundation、planning、frontend 三个原生 trellis-implement 已派发，所有权见 change-boundary.md，不允许子代理递归派发。
- 两个既有未跟踪 source-command skill 目录保留，不纳入本任务。没有提交／归档。
- 基础／规划／AI 的精确合同分别见 research/foundation-contract.md、planning-contract.md、ai-contract.md。已在实现前纠正 split 右片回落默认的合同漂移，应复制原始覆盖意图。

## HEAD 隔离基线

源码来自 git archive HEAD，私有临时目录：/private/var/folders/02/8zs7r3tx38sb90lkttcw585c0000gn/T/ainovel-scenes-baseline-dpqvd2lv。仅复用原有依赖目录，不包含用户数据库、.env 或未提交产品代码。macOS /var→/private 真实路径归一后校验 archive 成员，未跳过任何路径。

- 后端 ruff check：通过。
- 前端 lint：通过。
- 前端 build：通过（tsc + Vite 正式构建）。
- 前端 test：141 文件／1011 用例通过，29.85s；有既有 Node localstorage-file 警告。
- 后端完整 pytest：独立后台运行，日志在基线 backend/baseline-backend.log；中途在原 HEAD 已观察到失败，待完整报告，不将其算作本次场景回归或假称原基线全绿。

## 验证环境准备

Docker 初始未运行，使用已安装 Docker.app 后台启动，现 daemon 可用。未连接真实项目数据库。按仓库 compose 的 postgres:16-alpine 准备仅本机临时测试实例，后续使用 scenes_test／scenes_verify 专用库及隔离 schema；凭据只放私有临时目录，不写仓库。

产品实现、focused 测试和最终 PG／浏览器结果在后续追加；规划阶段的空白／上下文检查不等于产品验收。

## E0 环境根因与基线重跑

- 默认 LC_ALL/LANG=C.UTF-8 在本机 macOS 使 locale.getencoding() 实测 US-ASCII，而 preferred=UTF-8／UTF8 mode=1，正是显式 locale 读中文 Alembic ini 的风险。
- 原 HEAD serial 基线在 38 失败、1336 通过、7 跳过后受控 SIGINT 中断（日志保留）；摘要大量 UnicodeDecodeError，不能把无效 locale 下的迁移失败算作场景回归。
- 已验证 en_US.UTF-8 的 native encoding 为 UTF-8，通知两个后端代理不得改中文配置／测试掩盖；新 HEAD 基线使用合法 locale、临时 pytest-xdist 3.8.0 与 5 worker，并启用专用本机 PG 库。临时工具不改项目依赖或锁文件。
- PostgreSQL 16.14 新私有目录 /private/tmp/ainovel-scenes-pg-oqmvzo7x，127.0.0.1:32814。scenes_verify／fields_verify／series_verify／ainovel_test 分库并用独立 schema；凭据仅私有 connection.json，未写仓库。只复用旧测试二进制，没有复用旧数据目录。
- Docker 官方16-alpine镜像已下载完成，但验证使用上述私有原生PG，未创建共享容器。

## 实现期间的首轮证据（非最终门禁）

- foundation：tests/test_scenes.py 8/8 通过（15.49s），含 Unicode 无损、CAS、旧多Scene覆盖拒绝、soft-retire、只读不flush且列表不取正文。0039／v7仍在完善。
- frontend：API与草稿控制器 focused Vitest 8/8 通过；其余工作流和整体门禁未完成，不替代浏览器。
- planning 临时独占 context_builder 的可选 position／manual_character_ids／source_text 桥接以完成真实预览；后续 AI 使用其冻结签名，不并发修改。

## 合法环境完整基线终态

HEAD 隔离归档使用 en_US.UTF-8、临时 pytest-xdist 3.8.0（5 worker）、三个旧专用 PG URL 完整执行：2517 passed，1 warning，962.61s，无失败／跳过。pytest/uv 进程exit0。原38个Unicode迁移失败未通过改代码处理，已由合法locale重跑排除。

## E1 与 E2 新增证据

- E1 scene_versions 3/3、SQLite实际迁移3/3、真实PG2/2通过。PG首轮发现约束命名二次前缀，使用 op.f 修复并重跑，未以SQLite替代。含实际0038回填／子行／安全降级、content CAS竞争、rank互换、作品级联，使用UUID schema并最终DROP。
- E2 86个新旧规划／演变／context focused通过，另2个never／隐藏／覆盖cast／未来状态／字段分类场景通过。直接ORM seed缺Scene导致的旧context fixture失败采用显式initialize处理，未改GET或删原断言。
- 前端首轮 lint/build通过，已提供基础可浏览里程碑；私有SQLite／FakeLLM服务在127.0.0.1:41227，QA状态与持久合成凭据仅保存在私有临时目录。浏览器实际验收尚在进行。

## E3／E4 与独立审查

- E3冻结交接：实际FakeLLM／原任务并发／Agent／batch／规划focused 110 passed；真实隔离PG AI 5 passed。旧四处正文旁路已切Scene原语。精确变更与兼容边界见research/ai-handoff.md。
- 独立trellis-check发现并经真实RED→GREEN修复C1–C9：目录父章版本混读、跨章历史unit、标签历史依赖、UI全字段误造版本、删章Scene角色副本、非法ID输入、归属校验顺序、Core undo历史引用和过度绑定前章普通版本。证据见research/check-report.md，不以focused替代完整门禁。
- 前端完整145文件／1033测试首次通过；真实浏览器又发现C10“Scene end显示本章结束后”，统一六处边界／孤儿文案后全量145文件／1042测试通过，lint/build过。check独立33测试＋lint/build通过；main已刷新UI确认“本场景结束后”。
- README／实际scenes code-spec及相关规范已同步；9份规范摘录重新按真实原文、行号、SHA逐段核验，单份<32768字节；两JSONL清单各31条，validate零警告。未修改Trellis框架或用户两个原有skill目录。

## 第一轮本次完整后端门禁（保留失败）

- 合法UTF-8、临时pytest-xdist 3.8.0／5 worker、四组专用真实PG URL全启用：18 failed、2640 passed、1 SDK既有warning，691.83s；exit1。604份后端源码／测试／模板从启动到结束SHA未变，不是边写边测的混合证据。
- 私有完整日志：`/private/var/folders/02/8zs7r3tx38sb90lkttcw585c0000gn/T/ainovel-scenes-ui-l7y3_blv/verification/backend-full.log`；运行状态与源码cut在同目录。日志／凭据不纳入仓库。
- foundation拥有5个旧0035/0036实际迁移测试失败；planning拥有6个人工恢复／新不变量fixture和清空版本审计断言；AI拥有7个任务／上下文／旧模板／局部模拟改稿／并发配额失败。只按精确原因修复，不删断言、不把不安全状态判成功、不跳过PG、不用改历史迁移head掩盖ORM新列问题。
- 当前尚不能报告整任务质量门禁通过，须修复后重新完整执行。

## 真实浏览器验收进行记录

main使用专用headed Chrome会话、私有SQLite/FakeLLM和合成账号，不连接用户作品或真实模型。已实际完成保存／快捷键、POV覆盖、矩阵清空与继承联动、emoji码点无损拆分、三景卡片、按钮与原生拖动排序、整章只读通读、元数据合并、结构恢复、双页409保稿并明确重试、只读账号、375px及明暗主题；单Scene生成、三景整章一次CAS、第二景主生成中取消不半写、五局部emoji预览/采纳；v7真实文件上传预览→导入新项目（3景／22结构历史、ID重映射）；旧纯文本明确提取；Scene end事件维护与前后场景预览、保留锚点阻塞合并。各次写入另经真实HTTP读取核对服务端投影与版本。

旧纯文本演示条目是私有QA中明确seed的null snapshot历史，不冒充实际迁移证据；旧数据库迁移由独立SQLite/PG测试证明。私有截图、实际LLM消息与逐案evidence.json保留在QA目录。全量及最终浏览器剩余项仍需最后记录，不把上述结果概括为所有故障模式均已人工穷举。

## 首轮完整门禁修复与第二轮验证

- 18个失败已按所有权精确处理：历史0035/0036迁移采用真实历史表反射与同方言冻结DDL约束对比（34 passed）；结构恢复携内容CAS、清空保留结构版本审计边界、旧context权威Scene种子与位置JSON字段兼容（79 passed）；AI历史坏JSON只读投影／共同背景选角／旧章级单景重写字数真回归及必要测试兼容（194 passed、真实PG 5 passed）。三份 full-suite-*-fixes.md 保存实际原因与未放宽的断言。
- quota原5秒节点独立复现是交错建章使globalplanning安全失效，非运行太慢；仅把三章预先创建，保留额度变化与三任务成功全部原断言。未增加超时或放宽stamp。
- main独立前端完整lint/build/test：145文件、1042测试通过，50.38s，642份前端源码启动到结束SHA未变；原Node localstorage警告保留。全backend Ruff通过。
- 第二轮完整后端以同一合法locale、5worker和4组专用PG开启，启动cut604文件；终态尚未返回，不能据focused宣称全绿。证据使用backend-full-2.log、backend-run-2.json，不覆盖首轮失败。
- main已安全重启自有QAserver到最新后端（合成DB与凭据保留），真实Chrome完成单Scene自动续写两次实际请求／一次revision／兄弟景和元数据不变；非法Scene ID均422、错章404且零新增出站。
- 最终审查发现C11：分析抽屉旧文案／评分对比没有区分Scene任务，可能永远等不存在的自动全章分析。已交前端窄修目标范围、字数提示、真实任务范围和仅整章评分对比，后端源码继续冻结；此前前端门禁不冒充C11后最终门禁。

## 第二轮完整后端终态与最后安全修复

- 第二轮全量真实终态：2665 passed、1既有GeminiSDKwarning，635.15s（runner640.13s），exit0；四组真实PG全部启用，无跳过；604份源码／模板／测试从启动到结束SHA未变。
- main实际完成批量UI两章（3景／2景）、逐章审核暂停／继续：第一章3次主请求一次CAS后暂停，第二章未写；继续后另2次主请求，每章仅一次revision，身份／顺序／摘要／规划保持。私有作品明确关闭分析，避免混淆响应。
- 终审C12真实RED：合法无历史非空稿经旧单景章PATCH纯空白，正文确实清空但versions仍0，原稿不可恢复。check用/tmp单例载入原fixture验证1 failed/2.14s，未破坏第二轮源码cut。根因是旧PATCH `not content` 与version_service `.strip()`判清空不一致。
- 第二轮全量结束且runner回收后，才授权foundation改chapter_service一处判定及永久四参数HTTP回归，保留正文精确空白与普通10分钟合并；修后主会话第三轮完整验证。第二轮绿不冒充C12后全量。

## C12闭合与第三轮门禁

- foundation永久回归原代码1 failed/3 passed（3.10s），一处判空修复后4 passed（5.80s）；章／两类版本／Scene四全文件和旧管理员版本审计44 passed（31.54s），Ruff／空白通过，正式冻结。独立check未改原/tmp RED脚本复跑1 passed（1.35s），永久四参数另4 passed（6.77s），原稿和精确空白均保留结构版本且可恢复。
- main在冻结后启动第三轮完整后端（四组真实PG、5worker、604文件cut），独立日志backend-full-3.log，不改写首轮失败或第二轮全绿证据。当前仍待终态。
- actual reader DOM按现有段落展示完整三景顺序（两段中第一段含前两景，不虚报三段）；真实TXT／Markdown与EPUB导出均按五景顺序，EPUB实际解包并用XML解析XHTML核对。

## 最终后端与C11真实界面证据

- 第三轮最终全后端：2669 passed、1既有Gemini SDK弃用warning，693.90s（runner698.74s），exit0；604文件SHA启动至结束一致、四组真实PG全启用、零跳过。进程已reap。原首轮18失败和第二轮全绿日志均保留，不覆盖历史证据。
- C11首轮冻结后，main独立lint/build/test全部通过，147文件1052测试、644文件SHA稳定、90.4s；真实Chrome实际做单Scene建议重写：抽屉稳定身份#1、实际主调用Gate运行提示正确、留空目标21字（不是300）、只改目标正文，章分析变stale且没有新增分析任务；完成后没有伪评分或“正在重分析”。
- check扩查到未订阅另一景任务的active-store结束入口仍漏刷新分析。main独立前端门禁结束后才允许frontend窄补wasOtherSceneWriting及精确回归；后端继续冻结，此前147/1052不冒充该补丁后最终前端。
- 最新C12服务真实走versions=false导出→正常导入独立作品，未手工seed数据库，确实得到无历史单景章；纯空白旧PATCH后两份精确结构历史、原Scene身份与原稿恢复通过。
- 所有PG测试结束后，main只检查自有四个专用库：剩余测试schema均0、public表均0；仅停止本次新数据目录的PG实例。未停止其他PG或清理全局Docker资源，私有连接文件与日志留在临时目录不入库。

## C11最终源码冻结与自动化收口

- 未订阅另一景任务的active→false真实effect先RED（分析刷新0次），窄补后3文件32focused通过；owner完整147文件1054测试与lint/build通过，正式冻结。
- main独立最终完整前端：lint20.94s、build10.81s、test15.80s全部exit0，147文件1054测试，644份源码SHA全程不变，合计47.6s。最终证据另存frontend-*-final-2.log／frontend-run-final-2.json，不覆盖此前147/1052的前一轮。
- check独立32focused通过（2.04s），直接重核最终backend604／frontend644份源码cut全部一致；31条manifest和9份摘录逐段原文、SHA、行号、大小均通过。未跟踪文件no-index空白检查发现摘录EOF额外空行，用显式结束marker保留逐字符原文后消除，无源码变更。
- 当前源码没有未解决高风险，仅待main最后双tab真实UI补充和文档状态收口；不追加无关样式或旧行为范围。

## 最终闭合（待用户提交确认）

- C11最后真实双tab通过：先打开Scene3并保留未保存emoji草稿，再由另一页启动Scene1润色Task9主Gate。当前页明确显示另一景后台写作、未接其流；结束后自动刷新章分析“已过期”，本地草稿保留并显示最新版本核对提示。实际HTTP确认只改Scene1／一次内容版本，Scene3服务端未写，分析任务前后2／2；所有29项UI／HTTP证据passed且无pending。
- 独立trellis-check最终全范围审查通过：C1–C12全部闭合，无实质性遗留发现或待验审查项；最终后端604／前端644文件cut、两31条清单和9份规范摘录均直接复核。没有把专项SSR／effect测试冒充真实浏览器。
- README、实际code-spec、PRD／design／implement已同步；TODO第7项按真实验收勾选，第1／3项场景边界同步。未宣称富文本、全自动保存、跨章移动或模型创作遵从。
- 自有writer／readonly两个Chrome会话已关闭，自有QAserver停止前确认无运行／排队／暂停任务并保存实际messages；专用PG已在确认四库零遗留测试schema后停止。未动他人进程／浏览器会话或全局Docker资源；私有DB／凭据／日志不入库。
- 当前仍为in_progress，未暂存、未提交、未归档。完整文件清单与一笔coherent feat提交计划已准备；两个原有未跟踪source-command skill目录明确排除，等待用户Phase3.4一次确认。

## 用户收尾授权

2026-11-07，用户明确要求“提交 收尾 合并main并推送”，确认已呈现的完整文件清单与工作提交计划。提交前再次核对243个本任务路径与计划完全相同、无新出现的未识别变更，backend604／frontend644份源文件仍逐个匹配最终全量cut。远端origin/main、本地main和分支原HEAD均为ae2eb72；保留两个原有skill目录，不暂存、不删除，不改应用依赖或Git／Trellis全局配置。

按工作提交→独立归档提交→独立日志提交→合并main／推送执行；归档及日志脚本使用公开`--no-commit`选项后按窄路径手工提交中文消息，保留三阶段顺序，不amend、不强推。实际Git结果在后续归档记录与会话日志中记录，不预报合并／推送成功。
