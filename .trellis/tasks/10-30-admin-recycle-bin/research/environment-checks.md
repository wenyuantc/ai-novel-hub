# 实施环境检查

日期：2026-10-31，Asia/Shanghai。以下仅是环境能力检查，不表示功能验收通过。

## PostgreSQL

- PATH 中没有 `psql`、`initdb`、`pg_ctl`；Homebrew Cellar/opt 与 Applications 中未发现 PostgreSQL 安装。
- `docker version` 在获得只读沙箱提升后仍报告无法连接 `~/.docker/run/docker.sock`，Docker 守护进程未运行。
- 未启动或修改用户 Docker 服务，也未访问业务数据库。当前可执行 SQLite 与确定性事件屏障测试，但不能据此声称已实测 PostgreSQL 行锁。

## 前端生产构建

- 已核实 `frontend/node_modules/.tmp` 与 `frontend/dist` 均为 `root:staff`、0755，当前开发者无法写入现有生成文件。
- 原 `npm run build` 遇到 tsbuildinfo/dist 的 TS5033/EACCES，是既有文件权限问题；没有修改 package scripts 或依赖配置绕过检查。
- 获准仅对上述两个生成目录尝试 `chown`，但沙箱提升不等于系统 root，操作仍被系统拒绝。未使用 sudo、删除旧产物或修改依赖目录内容。
- 实施者将使用同一项目配置、可写临时 tsBuildInfoFile 和 Vite outDir 进行等价类型检查/生产构建；具体命令与结果以 frontend-report.md 为准，不能将原命令误报为通过。

## 浏览器与隔离运行时

- `npx` 可用；Playwright CLI 离线调用返回 ENOTCACHED（本机未缓存 @playwright/cli）。不为验收联网安装新依赖，改用原生 IAB 工具。
- 默认沙箱禁止绑定本机端口；启动隔离服务时单独申请仅监听 loopback 的权限，不复用用户正在运行的应用或真实数据库。
- 隔离服务脚本位于系统临时目录 `admin-recycle-qa-*`，使用临时 SQLite、合成账号和测试 FakeLLM，计划端口 53767。最终启动地址、前端构建路径、实际浏览器结果和模型调用计数另记。
