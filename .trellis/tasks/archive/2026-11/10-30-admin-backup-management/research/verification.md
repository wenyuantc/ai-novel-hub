# 实施与验收记录

任务：管理员整库备份管理。分支 codex/admin-backup-management，基线 b2b485e。用户于2026-11-02批准最终完整规划。

## 当前结果

| 检查 | 结果/证据 |
| --- | --- |
| 后端ruff | 最终检查已通过 |
| 后端全量pytest | 1526 passed，exit0，738.41秒；最后路由元数据改动另60+36项增量通过，quality.json |
| 最终装饰器增量 | contracts/review/open_api 60项通过，无重复operationId警告 |
| 前端lint/build/test | 全部通过，95文件724项；真浏览器另验 |
| 后端依赖审计 | pip-audit2.10.1：73运行包0漏洞，pip-audit-final.json |
| 前端依赖审计 | 14项既有moderate，0high/critical；原始及可达性见dependency-audit.md |
| 真实隔离all | af56冻结镜像32项全部通过，6次真实恢复并启动目标应用；all.json，资源回收通过 |
| 真浏览器 | browser.json及两截图：4种动作角色、CAS/秘密留空、同actor撤权、原生加密下载hash、375px/明暗主题；临时资源已回收 |
| 最终镜像metadata smoke | 0c40461镜像构建与OpenAPI唯一编号、真实加密备份、鉴权HEAD/GET及hash冒烟通过，final-smoke.json；资源回收通过 |

## 证据范围

acceptance/history.jsonl保留失败与修复记录，不能将早期失败隐藏成首次全过。roundtrip/storage/smtp为独立场景证据，all是完整矩阵结果。整库数据库、CA私钥、随机凭据与下载文件均只在仓库外私有临时目录，证据只含安全结论、image ID、hash和合成数据截图。浏览器使用614镜像；之后只有终态临时明文回收和OpenAPI编号元数据变更，分别由最终真实可靠性场景与镜像smoke覆盖，前端产物未变。

## 修复与维护约定

真实GPG长路径失败、SMTP累计超时/取消、首块前下载断开、请求Task身份、未知存储结果、终态临时明文回收、HEAD403授权刷新、前端时间/旧响应竞态均已增加针对性回归，核心约定见 .trellis/spec/backend/admin-backups.md。历史0029权限常量保持33项，0030迁移当前37项目录；作品JSON格式仍v1，只更新APP_SCHEMA元数据。

## 收口记录

最终门禁、32项真实all、最终镜像smoke和浏览器均已通过；AC1–AC14及实施清单已核对。用户已批准research/commit-plan.md的三批提交，并要求收尾、合并main和推送；三批118文件工作提交已按白名单完成；任务按Trellis归档和会话流程收尾，随后按授权合并main并推送。保留用户原.lithe/run/local.json及其暂存状态，后续提交不得混入它。

最终镜像仅在af56完整矩阵源码基础上修复GET/HEAD的OpenAPI唯一编号，HTTP行为和前端未变；最终0c40461镜像定向冒烟验证该差异。未重新执行不受影响的昂贵恢复矩阵。所有验收归属资源的只读复核见acceptance/cleanup.json。

最终全量基线有2项警告：旧GET/HEAD重复编号已由最终源码和增量/smoke消除；Gemini既有弃用警告保留。全范围检查无新增未修产品缺陷；DNS检查到实际连接仍有已记录窗口，按README部署出站限制。

工作提交：f3028c406b9d4a962ec5029ea6e9b9784e97bcbf, 71087fb3b7d3b3d1a3e23e45e1c2cfa43a8365a5, 4f395f96868affd28ffaac6ceb4727e5723e90b5。归档及会话记录使用独立后续提交，不混入工作提交。
