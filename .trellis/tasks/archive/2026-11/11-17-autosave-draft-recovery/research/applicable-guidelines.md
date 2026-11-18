# 本任务适用规范摘记

日期：2026-11-17。用途：若直接注入大型规范文件，Trellis 的每文件 32768 字节限制会截断相关内容；因此本任务 JSONL 注入本摘记及较小领域规范。原文件仍是规范来源，实施时按下面定位读取适用段落；这是有范围的摘要，不替代源文件，也不修改项目全局规范。

## 后端事务、权限和错误

- `.trellis/spec/backend/database-guidelines.md:77–108`：业务与成功审计同事务；章写入保持 `version_service.flush_versioned`，不要把审计错误误译成业务竞争。复用现有项目／User 写屏障和人工可管理权限，不全局改变 owned helper 的含义。
- 同文件 `163–169`：PATCH 区分未传／显式 null，按 model_fields_set 判断。`413–468` 是章节版本规范入口；其中旧的“空白不写”说明与后续 Scene 契约不同，清空边界以当前 `.trellis/spec/backend/scenes.md:99–110` 和真实服务实现为准，不恢复旧缺陷。
- `.trellis/spec/backend/error-handling.md:9–41,139–162`：业务用 AppError 子类，未知错误隐藏为 500；422 保留清洗后的字段错误，不回显请求原文。结构化 extra 按当前 errors.py 实现；不能在路由另写 ORM 或无鉴权错误查询。
- `.trellis/spec/backend/quality-guidelines.md:40–144,183–234`：功能变更同步根 README；真实服务／路由配隔离 SQLite，测试 use FakeLLM 和 tmp_path，不用正式数据、共享固定 DB 或真实模型。异步竞争使用受控事件／屏障，不靠固定 sleep。
- 后端最终命令为 `uv run ruff check .` 与 `uv run pytest -n 4 --dist loadfile --durations=20`。`README.md:889–895` 和当前 CI 是现存测试入口；旧 spec 中 PG fixture 已不存在，不能宣称运行或重建它们。

## 前端类型和 API

- `.trellis/spec/frontend/type-safety.md:104–130`：章 title/content/status 的省略或 null 原语义为不变；outline_id=null 是解除关联。服务端时间经 parseServerTime/formatTime，不能用裸 Date/dayjs 解析。
- 同文件 `137–162,192–218,251–259`：传输类型镜像后端；本地 JSON 从 unknown 校验，禁 any 和宽泛 as；字段条件不能因为前端 trim 抹掉原基线。本任务 expected_title 必须按服务端原值保留，与普通新标题规范化区分。
- 全部服务端请求走 src/api/client 与领域 API，保留 credentials/include、ApiError、认证请求代次和 isCurrent；页面不直接 fetch，不自造另一套 401 登出。网络错误与 AbortError 不证明服务器未提交。
- 处理响应时根据本任务的冻结快照额外检查目标／版本／完整文档一致性；不能把既有 request<T> 的静态类型当作持久化草稿验证。

## 前端组件与交互

- `.trellis/spec/frontend/component-guidelines.md:37–56,161–220`：AntD 反馈来自 App.useApp，沿用 zh_CN；章响应不把整章投影回灌当前景；AI 开始前保存最新编辑，任务卸载 detach，不隐式取消后台任务。
- 同文件 `231–267`：受控 value/onChange、窄 props、主题颜色来自 token；图标按钮有 aria-label/title；移动端使用 useIsMobile，375px 不撑宽；快捷键通过 ref 读取最新处理函数。
- `.trellis/spec/frontend/state-management.md`：控制器是本地编辑所有者；服务端派生字段来源单一；账号变化隔离状态，外部刷新不得替换未保存输入。
- `.trellis/spec/frontend/quality-guidelines.md`：完整门禁是 npm run lint、npm run build、npm test；新增文件必须另做空白检查；功能完成才把 README、TODOS 和 spec 写成已实现。

## 本任务有意改变的旧行为

- 普通网络保存不再禁用正文输入；AI／恢复／结构变更的动作屏障仍保留。旧组件的 saving===disabled 需要按 design.md 区分，不能照搬摘要中的旧“忙时禁用全部”模式。
- 标题 HTTP 更新新增前置条件；独立摘要仍手动提交。场景/富文档的完整证据、权限和历史规则保持有效，具体目标契约以本任务 design.md 为准。
- 源规范内大量其他功能或过时指引不自动扩展本任务范围；遇当前代码证据冲突要记录并核实，不能为符合过时片段删除用户刚调整的测试体系。
