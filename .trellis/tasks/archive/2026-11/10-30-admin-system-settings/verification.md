# 管理员系统设置验证报告

日期：2026-11-01（Asia/Shanghai）。分支codex/admin-system-settings，基线97193269c4bfcf896163e49236866da7a9f83ca9。实现与验证完成；用户随后明确授权提交、归档记录、合并main并推送，工作提交已完成并已归档，合并与推送按commit-plan.md继续执行。

## 交付

六项白名单、后台覆盖优先及跨重启持久、来源/部署/代码默认展示、逐项恢复、严格校验与revision冲突保护、成功事务审计、共享Settings热发布、真实注册/两级调度/分析/MCP/限流接入、公开开关30秒同步均已完成。部署凭据不入管理界面或审计。新增迁移0027，部署先执行alembic upgrade head；Docker入口已自动执行，本次未操作开发者真实数据库。

## 后端

工作目录：/Users/wenyuan/IdeaProjects/my/ai-novel-hub/backend。

```bash
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

- 最终完整pytest：**1332 passed、1 warning、exit 0**，561.44秒；退出码来自实际进程返回，不是摘要推断。
- 最终Ruff：exit0。uv缓存读取受沙箱限制，使用已存在虚拟环境等价运行，未安装或修改依赖。
- 原始full日志/private/tmp/admin-system-settings-backend-final-full.log。启动时重定向至/tmp/admin-system-settings-backend-full-final.log，随后原子改名，进程继续写同一inode。
- warning是既有Gemini SDK generate_images弃用提示，不属于本功能；未放宽警告或修改无关图像功能。
- 最终check对当前40项设置/运行/迁移测试另行完整复跑：40 passed、exit0、33.11秒，命令如下，原始stdout在工具执行记录（未另存日志）：

```bash
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/python -m pytest tests/test_system_settings.py tests/test_system_settings_runtime.py tests/test_system_settings_migrations.py --tb=short --show-capture=no
```

### 快照与首轮故障边界

首轮full为1325 passed+1个既有writeback订阅10秒超时，exit1，不算通过；该单项复跑1 passed、整个writeback模块9 passed，未改产品/超时。随后最终full全通过。

最终full collection后仅本任务新并发测试的Gate身份屏障得到修正，**17个backend产品/迁移文件哈希从起跑到结束完全一致**；因此结论由该完整产品快照通过加当前40项通过组合支撑，不伪称full collection已使用修正后测试定义。哈希证据research/backend-product-snapshot.json，原始来源/private/tmp/admin-system-settings-backend-final-product-snapshot.json。

## 前端

工作目录：/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend。

```bash
env HOME=/private/tmp/admin-system-settings-review-vitest-home npm test -- --configLoader runner --no-cache --fsModuleCachePath /private/tmp/admin-system-settings-review-vitest-cache
npm run lint
npx tsc -p tsconfig.app.json --tsBuildInfoFile /private/tmp/admin-system-settings-review-app.tsbuildinfo
npx tsc -p tsconfig.node.json --tsBuildInfoFile /private/tmp/admin-system-settings-review-node.tsbuildinfo
npx vite build --configLoader runner --outDir /private/tmp/admin-system-settings-frontend-dist
```

- **81个文件、634测试通过**（17.01秒），lint、两份tsc、等价生产构建均exit0，最终构建1.93秒。
- 标准build被既有root所有的node_modules/.tmp写权限阻止；没有放宽权限/更改工具配置或覆盖原dist。Vitest/TS/Vite使用临时HOME/cache/buildinfo/outDir的等价门禁，不把等价通过声称为原标准build命令通过。
- 最终chunk AdminSystemSettingsPage-BgodjdVo.js，最大vendor-antd约1147KB，小于原1500KB门槛，无新依赖。
- 前端原始stdout在工具执行记录，临时缓存/buildinfo/产物路径不是原始日志文件。

## 独立全范围审查

独立trellis-check覆盖全部backend/frontend变更（含新文件）、PRD/design/implement及规范。修复StrictMode busy、确认框本页所有权、开放API超大限额导致后续500，以及仅测试层Gate顺序假设。最终无剩余确定产品缺陷；详细证据research/full-scope-check.md。

## 真实运行验收

见research/browser-verification.md。隔离临时SQLite+合成账号+FakeLLM、生产前端、127.0.0.1:8769：真实UI暂存/保存/恢复/来源、独立匿名页30秒同步、MCP403、409草稿保护、受控500恢复、同库冷启动、API数值上限与真实读取/恢复、审计详情、会话401弹窗撤下、普通用户403、375px与浅暗主题均观察通过。

最终临时库revision5、六条设置审计（含冲突失败）、FakeLLM调用0；合成Key已撤销并清明文。两个命名浏览器、最初孤立about:blank daemon及两次预览进程已停止；原有8000服务未触碰。截图在/Users/wenyuan/.codex/visualizations/2026/10/31/01a192df-d16b-7f21-ba75-657760f0a945/，临时证据在/private/tmp/admin-system-settings-browser/。

## 验收与保护

AC1–AC10均由自动化与真实运行组合覆盖。PostgreSQL未实测，不能把SQLite并发/迁移结果冒称PG验证；继续沿用既有单进程前提。提交确认不确定和数据库不可用的边界见系统设置规范，不承诺离线一致或任意强杀无故障。

用户原有.codex/config.toml、.gitignore、两个source-command skill共4文件的SHA256保持不变；只提交本任务文件，原有无关修改明确排除。最终diff检查及Trellis上下文validate通过；规范与README已同步，收尾按用户后续明确授权执行。
