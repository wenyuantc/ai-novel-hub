# 依赖审计证据

2026-11-02，实施前对未改动的前端依赖执行审计。默认 npm registry 为 npmmirror，其安全接口返回 404 / NOT_IMPLEMENTED；改用命令参数 `npm audit --registry=https://registry.npmjs.org --json`，没有修改用户 npm 配置。

命令退出 1；原始脱敏结果见 `npm-audit.json`：14 项 moderate，high/critical 均为 0。14 项包含传递影响，来源是下面 3 项公告，不表示 14 个独立漏洞。本任务未新增前端依赖，不以 `audit fix --force` 或未经授权的 React Router 主版本升级掩盖结果。

## 可达性判断

- [minimatch GHSA-g3hv-75wg-m249](https://github.com/isaacs/minimatch/security/advisories/GHSA-g3hv-75wg-m249)：攻击者控制 glob 且优化级别至少 2 时可能放大计算。当前位于 ESLint/TypeScript ESLint 开发依赖链；应用运行代码没有引用 minimatch，lint 的 glob 来自仓库配置。依此判断，备份网页没有该攻击入口；开发工具链仍带公告版本，应单独跟进其兼容修复。
- [React Router GHSA-337j-9hxr-rhxg](https://github.com/remix-run/react-router/security/advisories/GHSA-337j-9hxr-rhxg)：官方说明仅影响手动 SSR/hydration 且错误对象可被攻击者影响的使用方式。当前 `frontend/src/main.tsx` 使用 `createRoot`、Vite SPA，无服务端 hydration，依此判断不满足触发前提。
- [React Router GHSA-wrjc-x8rr-h8h6](https://github.com/remix-run/react-router/security/advisories/GHSA-wrjc-x8rr-h8h6)：攻击者路径传入导航时存在外部重定向风险。当前备份入口使用固定本地路径、数值执行 ID 和限定目的地；既有登录跳转从 Router location state 取值，RequireAuth 用当前 pathname/search 构造。未观察到本任务把 S3 端点、SMTP 参数或其他任意用户字符串传给 Router 导航；这是一项针对当前变更的可达性判断，并非对整个项目的无漏洞证明。

官方修复版本与本项目现有主版本不同；保留完整审计退出状态，后续框架升级需独立验证兼容性。后端锁文件和新增依赖的审计在锁文件稳定后补充。

## 后端

使用 PyPA `pip-audit 2.10.1`，`uv export --frozen --no-dev --no-hashes --no-emit-project` 导出完整锁定运行依赖到仓库外临时目录，再以 Python 3.12 执行 `pip-audit -r <临时文件> --format json --disable-pip --no-deps`。这里 `--no-deps` 表示导出已包含完整传递依赖，不由 pip 再次解析或安装，不省略任何已锁定运行包。初次漏配 `--no-deps` 的命令没有形成有效 JSON 报告，未计为通过。

首轮有效扫描退出 1，发现既有 pyasn1 0.6.4 的 4 项和 pypdf 6.21.2 的 7 项公告；原始结果保留 `pip-audit.json`。`kb_parser.py` 实际使用 PdfReader/pages，PDF 输入攻击面可达，因此在本次依赖质量检查中采取兼容补丁修复：pypdf 6.21.4、pyasn1 0.6.5，不强制升级其他依赖。官方修复依据：[pypdf 6.21.4](https://github.com/py-pdf/pypdf/releases/tag/6.21.4)、[pyasn1 0.6.5](https://github.com/pyasn1/pyasn1/releases/tag/v0.6.5)。补丁后的扫描与解析回归结果待补充，不把锁文件变更本身记为通过。

补丁后同一命令再次扫描：退出0，覆盖73个锁定运行包，0项漏洞，见 `pip-audit-final.json`。扫描工具未改动应用虚拟环境；解析及全量回归由实施检查记录另列。前端原14项moderate仍如实保留，不能将后端扫描通过误写为全项目审计零告警。
