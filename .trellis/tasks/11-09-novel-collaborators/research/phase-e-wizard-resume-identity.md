# 向导续写来源身份

## 实现范围

- HTTP 向导续写必须成对携带 `continue_from_task_id` 和客户端原先看到的 `continue_from_task_key`；ID 须为正整数，key 须为规范 UUID。缺少配对或格式错误返回 422；身份、用户、作品或类型不匹配保持原有统一 400，不能按当前整数 ID 补查凭据。
- 接受续写先按认证/User → 作品 → 原任务的顺序取锁，核对原 UUID、真实用户、项目和向导类型，原任务必须是失败、取消或中断且含可续写输出。来源若含协作 grant 则失败关闭，不开放协作者向导。
- 新任务服务端内部 `_wizard_resume` 绑定原任务 id/key 以及原状态、输入、输出和入队时间的指纹；运行构建时重验，不能将被复用 ID 的新任务当原来源。公开响应提供新任务自己的 `task_key`；内部绑定和请求中的旧 key 经统一私人投影隐藏。
- 前缀输出前、每次文本模型与 KB 向量发送前检查来源身份和数据；最终写入时按认证/User → 可见性门禁 → 作品 → 来源任务顺序再次检查。来源变化或账号停用不能写入旧结果；向量策略异常不能降级成关键词后继续发送。已发送给本人、发送时身份有效的内容不承诺撤回。

## 测试

- 修改前两个真实 HTTP 红灯证实：旧 key 的整数 ID 已被新向导占用，及请求仅含整数 ID 而无 key，旧代码都错误返回 202。
- `tests/test_wizard_resume_identity.py` 覆盖旧 ID 重用、无 key、错 key、合法新 key、受理后来源替换、同一来源内容变更、前缀和发送间来源变更、发送后来源变化/账号停用、错误 key 无自动 flush、向量发送前来源变化零出站。
- 原 `tests/test_wizard_resume.py` 只为原请求补充从真实 TaskOut 取得的 UUID，保留原有续写、链式继续、错误响应和模型内容断言。
- `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_wizard_resume_identity.py tests/test_wizard_resume.py tests/test_wizard.py --tb=short --maxfail=1`：**52 passed in 61.17s**；其后新增的缺少 ID 和“前缀后、发送前失效”两项单独 **2 passed in 1.65s**。Ruff 已通过。
- 隔离 PostgreSQL 的 `tests/test_wizard_resume_identity_postgresql.py` 收集 2 项，覆盖替换事务先提交与受理先持来源锁的两种次序；尚待主会话错峰实际运行。SQLite 不能替代 PostgreSQL 行锁证据。

## 仍需闭合

- 本切片只维护严格作者向导的续写来源，不实现 E1–E7 的协作者模型绑定、后台新任务的完整执行身份、所有流帧实时撤权及完整作者模型变更策略；不可据此开放协作者向导或单独部署。
- 通用 provider 的第三方工厂来源与物理发送保证仍依赖整体模型门禁工作；本切片的 FakeLLM 测试不证明真实 HTTP 锁序。最终写入后任务终态/提交确认未知仍需引擎整体协议处理。
