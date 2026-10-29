# 父任务最终验收（2026-10-29）

## 范围与环境

- 验收基线：本地 `main` 的 `914cfba`，包含 17 个父任务子任务及其后的封面 / 知页界面更新。
- 仅补齐验收与任务资料；本轮没有修改产品代码、API、数据库或配置契约。
- HTTP 链路使用应用工厂、独立临时 SQLite、FakeLLM 与 fake DNS；真实 MCP 协议测试仅使用测试创建的本地 FastMCP。未连接用户运行数据库或真实模型服务。
- 子任务、依赖、44 项差距的最终归属、已批准取舍及文本复查，见 [final-coverage.md](./final-coverage.md)。2026-10-27 的 P0/P1 记录继续保留于 [integration-acceptance.md](./integration-acceptance.md)。

## 全量门禁

| 命令（各自目录） | 本轮结果 |
|---|---|
| backend：`LC_ALL=en_US.UTF-8 uv run ruff check .` | 退出 0，All checks passed |
| backend：`LC_ALL=en_US.UTF-8 uv run pytest` | 退出 0，975 项通过、1 条 SDK 弃用 warning，322.20 秒 |
| frontend：`npm run lint` | 退出 0 |
| frontend：`npm run build` | 退出 0，包含 TypeScript；无构建 warning |
| frontend：`npm test` | 退出 0，54 文件 / 417 项通过 |
| 仓库：`git diff --check` | 退出 0，归档前复核 |

前端最大 chunk 为 vendor-antd 1,146.09 kB，低于 1,500 kB 门槛；入口 169.29 kB。只有 `RelationGraph-BAORy303.js` 含 `react-flow__`，图谱未混入入口 / vendor。

门禁完整日志及机器记录：`/var/folders/h1/jwfsdnfs4lg5p4fcbkcwwp600000gn/T/mumu-final-quality-zh8t4yq4/`。

唯一 warning 来自 `test_gemini_current_sdk_imagen_unavailable_is_chinese`：`app/ai/providers/gemini.py:151` 的 SDK `generate_images` 方法已弃用，SDK 提示下一主版本移除（不早于 2027-01-01）。当前兼容分支测试通过；本轮记录该已有依赖警告，未修改图片 Provider。

## 同一项目创作链路

执行：在 `backend/` 下运行 `LC_ALL=en_US.UTF-8 PYTHONPATH=. uv run python /tmp/ainovel-final-core-tAbWoP/run_core.py`，退出码 0。57 个 HTTP 请求、123 条断言、11 次 FakeLLM 调用；8 个后台任务全部 `succeeded`。

| 步骤 | 已验证结果 |
|---|---|
| 注册、AI 配置、新建项目 | 注册与创建成功；AI 配置只回传掩码 |
| 世界观 / 职业 / 角色 / 大纲四步向导 | 两条世界观、三名角色、三阶职业和大纲落库，角色职业关系完整 |
| AI 展开、确认细纲、建章 | 草稿确认前无章节；确认后两章与大纲 / 细纲关联完整，角色名字 / 别名匹配正确 |
| 批量生成与审核 | 第一章后暂停，同一任务从第二章继续；审核期间修改被后章提示词读取 |
| 自动分析回写 | 两章分析成功；角色状态、关系亲密度、职业阶段和组织成员回写；十条剧情记忆写入 |
| 后章上下文 | 第二章使用第一章的回写状态、审核编辑及细纲事件 / 目标 |
| 按建议重写与重新分析 | 正文改写、`revise` 版本、新旧分析及评分保留，自动重新分析完成 |
| 版本对比与回滚 | 读取版本生成 11 行 diff；回滚后逐字等于原稿，并新增 `restore` 版本 |
| EPUB 导出 | mimetype 首位且不压缩；OPF / nav / NCX 齐全，含两章当前正文 |
| JSON 导出、预览、导入新项目 | 27 表（20 表非空）行数、归一化内容与外键一致；细纲 / 分析 JSON 角色 id 重映射正确；两章正文 SHA-256 一致；预览与实际计数一致 |
| 脱敏 | 备份和 EPUB 不含测试 API 密钥 |

原始证据：`/tmp/ainovel-final-core-tAbWoP/run_core.py`、`report.json`、`run.log`。本轮往返中的空表不据此宣称非空内容已验证；伏笔、知识库、快照、灵感等完整往返由全量 `test_project_transfer.py` 及其相关模块覆盖。

## P2 安全与交叉功能

在 `backend/` 运行：

```sh
LC_ALL=en_US.UTF-8 uv run pytest -v tests/test_agent_api.py tests/test_agent_mcp_skills.py tests/test_skills.py tests/test_skill_generation.py tests/test_mcp_plugins.py tests/test_workshop.py tests/test_endpoint_guard.py tests/test_ai_configs.py
LC_ALL=en_US.UTF-8 PYTHONPATH=. uv run pytest -v -c pyproject.toml /tmp/mumu-p2-security-iosEHt/test_per_request_guard.py
```

两次退出码均为 0：已有模块 144 项通过（68.98 秒），临时补充 6 项通过（1.38 秒）。144 项属于全量套件的子集，不重复计入全量数量。

| 约束 / 功能 | 已验证结果 |
|---|---|
| 智能体提案确认与撤销 | 并发确认只执行一次；角色写入与整轮撤销正常；正文编辑产生 agent 版本，撤销产生 restore 版本 |
| 私有资源隔离 | 智能体项目 / 会话 / 消息 / 提案等 13 条越权请求 404；私有 Skill 与 MCP 的读写 / 导出 / 调用等均 404，未发起连接 |
| 全局与共享资源权限 | 普通用户可读全局 Skill，修改等操作 403；管理员可管理；工坊普通用户审核 / 官方管理 403，未审核内容和他人分享管理 404；作者仅管理自己的分享 |
| Skill 与生成 / 智能体 | 生成重试、批量审核续跑、智能体确认续跑沿用创建时快照；强制 Skill 在安全规则后注入；他人私有 skill_id 为 404 |
| 工坊分享审核与恢复 | 作者提交 → 管理员通过 → 第二用户导入模板 → 从历史恢复导入前内容；模板升级校验失败不覆盖原值 |
| MCP 确认边界 | 默认 proposed / paused，确认前调用数 0；确认后仅一次；开启小修改自动执行仍需确认 external；配置 / schema 变更产生 409；外部操作不可撤销 |
| AI / MCP 密钥 | Fernet 密文存库，接口只返回掩码；MCP 事件、会话、日志、任务、输出及 checkpoint 不含测试请求头明文 |
| AI SSRF | 回环 / 内网 / 链路本地 / IPv4 映射 IPv6 / 混合解析 / 错误 scheme 被拒；Provider 未被调用，默认不跟随重定向 |
| MCP 逐请求 SSRF | 保存与连接时拒绝私网；HTTP / SSE 后续回环地址、混合 DNS、连接后 DNS 转私网均在实际 httpx 传输前被请求钩子拒绝 |
| MCP 实际协议 / 重定向 | 本地 FastMCP HTTP / SSE 刷新、调用与超时通过；307 只请求 redirect，未访问 target；`follow_redirects=False`、`max_redirects=0` |

原始证据：`/tmp/mumu-p2-security-iosEHt/report.md`、`focused-pytest.log`、`focused-result.json`、`test_per_request_guard.py`、`per-request-guard.log`、`per-request-result.json`。

## Spec 核对与限制

- 已核对后端 quality / outline / batch-generate / project-transfer / agent / skills-mcp / workshop 及前端 quality 规范；本轮没有新增或修改产品契约，也未发现需要写入 spec 的新规则，因此不制造重复的 spec 变更。
- 真实模型质量、公网 MCP、PostgreSQL 真实行锁等待与多进程竞争未在本轮现场验证。AI 端点仍保留既有 spec 记录的检查与连接间 DNS 重绑定窗口。
- 本轮以 HTTP / ASGI 证明数据链路与状态流转；浏览器、移动端、主题及真实认证的既有验收见各归档子任务，最近知页任务已验证 320 / 375 / 768 / 1024 / 1440px。
- 本轮既有未跟踪文件 `pelican-bicycle.html` 保留，不纳入任务提交；归档只处理任务资料和会话日志。

## 结论

父任务保留的 17 个子任务均已完成并进入本地 main；功能覆盖、最终主链路、P2 安全与全量门禁通过，可归档父任务。已取消范围和已批准取舍见覆盖表；本轮无需产品修复。
