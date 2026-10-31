# admin-recycle-bin 实施与验证

日期：2026-10-31（Asia/Shanghai）。分支：`codex/admin-recycle-bin`。

状态：功能已实施且可执行的本地质量门禁全部通过：后端1143测试、前端520测试。真实PostgreSQL与原构建目录权限仍为明确的环境/发布门禁缺口，不宣称它们通过。用户已要求提交收尾，并在提交计划后明确要求合并到 main、推送；正在执行，最终结果另记于收尾记录。

## 实际交付

- 作品统一软删除、30天保留、启动补清及小时周期清理；仅管理员查询、恢复和完整名称确认彻底删除。
- 真实执行链排空、持久化准入意图与父项目事务屏障，覆盖queued/running/paused、取消清理、成功回调、独立MCP确认及直接ID入口。
- 用户名下有任何作品（含回收站/待清理）时禁止删号；RESTRICT兜底并发，SQLite787/1811仅精确翻译已核实外键冲突。
- 原ID/owner/status及关联数据恢复，权限投影与底层Key grant分离；Key只改元数据不会撤销回收站隐藏授权。
- 内部last_deleted_at单调高水位、SQLite Project AUTOINCREMENT防同实体/跨实体旧请求误删，公开API不增加字段；恶意备份不注入生命周期状态。
- 管理员动作与系统expire的同事务安全审计，actor为空明确显示系统；原审计历史及日期清理保护保留。
- 回收站桌面/375px明暗布局、所有普通删除入口、删号保护、原始微秒代次传输和目标级cache/旧响应失效。

## 自动化门禁

| 检查 | 当前结果 / 证据 |
|---|---|
| 后端ruff | 通过 |
| 新增生命周期/迁移4模块 | 最新41 passed，覆盖终检安全修复，24.80s |
| 旧失败归因6模块 | 74 passed，72.62s；不与上行相加冒充唯一总数 |
| 最终后端完整套件 | **1143 passed，exit0**，487.19s；包括21项loopback MCP测试，无skip替代；1条既有Gemini SDK警告 |
| 前端ESLint | 通过 |
| 前端完整测试 | 最新67文件、520测试通过 |
| TypeScript app/node | 等价原配置检查通过，tsBuildInfoFile写入允许的临时路径 |
| Vite生产构建 | 等价原配置+临时cacheDir/outDir通过，路由懒加载，无1500kB超限 |
| 原npm run build | **未通过环境门禁**：既有root-owned .tmp/dist阻止写入，不能记成原命令成功 |
| 差异/规范引用 | git diff --check通过；本任务spec本地链接及jsonl引用有效；无关4项内容SHA256保持实施基线 |

正式命令、日志、逐项审查及AC1–AC8映射见 `research/final-check-report.md`；早期旧快照的1116/15等结果不作为最终通过证据。

## 主会话独立运行时与浏览器

- 仅临时SQLite、合成账号、FakeLLM，未访问真实业务库或真实模型。模型chat/embedding/tools/images调用计数均为0。
- 第一轮23条回收站分页/搜索、取消/确认恢复、精确名称purge门禁后取消、到期禁恢复、普通用户无入口/路由拒绝、作品计数含回收站禁删号。
- 375×812浅/暗主题实际检查，documentElement宽度375无横向溢出；临时viewport和主题已恢复。最新核心再次通过真实IAB恢复和system actor只读详情。
- 1811修正后的最新核心重新使用fresh库，12项HTTP全部通过，包括实际purge204、正文/封面字节往返、旧代次409、strict confirm422、普通403/匿名401、startup expire、恢复无新活动任务、删号保护409。
- 最新Key编辑适配另做真实IAB：投影[]和混合范围分别仅改名、实际保存；临时数据库核实名称更新且隐藏grant不丢，3项结果全true。未显示/复制完整Key。
- 两轮浏览器控制台error为空；所有自建IAB标签已关闭，53767/53768/53769/53770均已核实无监听。

详细命令/临时路径和证据边界见 `research/runtime-verification.md`。浏览器未点击不可逆purge最终按钮，实际物理删除由明确的隔离HTTP执行，不能混写两种来源。

## 仍须明确的限制与发布门槛

1. **真实PostgreSQL尚未实测**：无本机工具/可用实例，Docker守护进程未运行；SQLite竞争测试和PG异常形状测试不能替代真实迁移/行锁/并发事务验证。发布前应在临时PG补跑既定矩阵。
2. 原前端标准构建受既有生成目录所有权阻挡；等价TS/Vite和完整Vitest已验证，不修改项目配置或使用sudo掩盖问题。需环境所有者后续恢复生成目录可写权限。
3. 沿用单进程任务引擎，已发出的外部副作用不能撤回；取消任务不会随删除失败自动复原；到期清理不可逆，上线前先备份。
4. 本地质量门禁已完成，用户已授权提交、归档、记录会话及合并 main 后推送；上线前仍须真实PG补验，代码入库不等同于生产发布。原有 `.codex/config.toml`、`.gitignore` 和两个 source-command skill目录保持不动且不纳入提交。

## 规范沉淀

新增 `.trellis/spec/backend/recycle-bin.md` 七段式契约，并同步backend/frontend索引及auth-admin、admin-projects、database、admin-audit、project-transfer、open-api、frontend component/state/quality。记录重复锁保留dirty数据、终态不等于收尾退出、单调删除代次/不复用ID、SQLite1811和隐藏Key授权PATCH的防复发规则。
