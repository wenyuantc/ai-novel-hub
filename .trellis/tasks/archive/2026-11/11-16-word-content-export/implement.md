# 实施计划：Word 与内容组合导出

状态：用户已批准方案，实施与验收已完成；具体结果见 [验证记录](verification.md)。待按 Trellis 流程确认提交、归档和记录会话。

## 启动门禁

- [x] 用户同意创建任务并规划。
- [x] 用户确认方案 A 和资料按具体条目选择。
- [x] 代码、权限与 DOCX 官方能力研究落盘。
- [x] PRD 收敛，design.md／implement.md 已形成。
- [x] 主会话展示最终方案，用户在后续消息明确批准实现。
- [x] 在批准后执行 `task.py start`，当前分支为 `codex/word-content-export`。

一个任务共享选择和文档模型，以下步骤有先后依赖，不拆成独立发布的父子任务。

## 执行顺序与所有权

1. **主会话准备**：核对当前 Git 状态，保护他人改动，读取 Phase 2.1。主会话管理需求／设计／计划、最终文档和提交；按当前 Git 规范隔离实现。
2. **后端契约与装配**（trellis-implement 负责 backend 相关代码／测试）：新 schema／POST／纯值模型，章景与摘要、当前设定字段和关系、完整笔记、本人聊天 helper 及批量最终核验。旧 ExportFormat／GET／开放 API 不变。
3. **渲染与附件**（同一实现代理，依赖第 2 步）：DOCX 依赖及 uv.lock、三种 renderer、中文与富格式、受控提示头和 no-store，渲染后的权限复核。新增真实 HTTP 和内容结构测试，验证零 AI／零持久改写。
4. **前端与下载**（实现代理负责 frontend 相关代码／测试，依赖接口确定）：ExportModal 与选择组件、分页与搜索、仅资料、EPUB 原路径，API 类型与 POST，身份／授权／弹窗代次守卫。Vitest 验证状态与实际下载副作用，不只检查源码字符串。
5. **独立检查**（主会话派发 trellis-check，依赖实现完成）：按 AC1–AC9 检查调用链、权限、共享单会话兼容、全部内容／格式与前端竞态；局部问题修复，接口或范围变更退回主会话裁决。
6. **集成验收与文档**（主会话协调）：完成全量门禁、真实浏览器和 DOCX 视觉检查，再同步 README、TODOS、spec 与证据记录。
7. **收尾**（主会话）：执行 Phase 3 规范与提交前流程；按实际授权提交／推送，不把实现批准当成推送授权；任务与会话记录按 Trellis 规则收尾。

派发首行必须为当前 `Active task:`。优先原生上下文注入；缺失时子代理读取 prd／design／implement 和 jsonl 中的 spec／research。明确所有权，告知代理不是唯一工作者，不撤销他人修改，不擅自改范围或提交。公共契约未定前不并行编辑共同文件；主会话不与实现代理重叠改产品代码。

## 验证矩阵

| 组 | 内容 | AC |
| --- | --- | --- |
| 选择／文档 | 三格式、顺序、章景、仅资料、空选／空正文、缺失／过期摘要、中文文件名、非法 XML。 | 1–5、8 |
| 正文／格式 | 粗斜体、标题、列表、引用、emoji、CRLF、无区块／标记、并发修改版本。 | 1、4 |
| 资料 | 四类与四字段、具名引用、当前关系不扩展档案、已采用系列、长笔记／长聊天。 | 5、6 |
| 权限／竞争 | owner/editor/viewer、仅管理员／管理员兼成员、他人聊天、跨作品、撤权重邀、token失效、回收、删除、渲染期间变化。 | 6、7 |
| 前端 | 分页不丢项、全选／空选、格式切换、重复点击、失败重试、关闭／身份切换后晚到 blob。 | 8、9 |
| 兼容 | 旧 TXT／MD／EPUB、开放 API、JSON 备份、原 agent 安全投影和详情。 | 9 |

先跑新增测试与受影响回归，再跑项目规定的完整门禁。目录与命令：

```bash
# backend/；确保本机 UTF-8 locale，避免迁移配置中文读取失败
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check .
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_export.py tests/test_epub.py tests/test_rich_document_render.py tests/test_rich_document_analysis.py
# 再加本任务新增导出／聊天／竞争测试，具体文件按实现记录
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest

# frontend/
npm run lint
npm run build
npm test
```

权限／快照竞争必须含真实 PostgreSQL 隔离测试，沿用质量规范的专用账号／库、环境变量及随机 schema；不能连接开发数据做破坏性测试，不能以 SQLite 代替 PG 结论。环境造成的 skip／阻塞必须如实记录，不称全部通过。

实际验收使用隔离假作品／账号：

- 桌面、375px、浅／深主题、键盘选择，实际下载 DOCX／MD／TXT，另确认 EPUB。
- DOCX ZIP/XML 独立内容／格式断言＋库重新打开＋实际可用文档渲染／查看工具检查多页中文、长段落与空行。没有验证的软件版本不得声称通过。
- 百万中文混合章节／场景／资料样本，记录耗时、输出大小及资源异常；不能截断内容或放宽正确性来通过。
- 新文件空白单独检查；获准暂存后 `git diff --cached --check` 必须通过。

## 风险与回滚

- 聊天批量核验为主要共享风险；禁止通过删 proofs 或绕过投影解决失败。需语义调整时回到规划。
- 导出专用 invitation_id／member.revision／last_deleted_at 必须与全部聊天证据在最终同一只读快照核验；测试覆盖 viewer 旧会话 writable 始终 false 时撤权后重邀、权限改回与作品回收恢复。新增 conversation_snapshots 不得覆盖提案 conversations 映射。
- 最终快照在渲染结束后开启，PG 连接级 REPEATABLE READ、SQLite 显式 BEGIN，分批有界且不另开验证连接。测试跨 400 条消息、接近参数上限、取消释放事务、SQLite WAL／默认日志及 PG 快照竞争；原 actor/token 必须跨事务保留，二次投影复用原始冻结 payload／proofs。
- 场景投影须保持同快照与码点，旧阅读器／导出回归独立验。
- DOCX 可撤回 renderer 和依赖锁文件，前端可恢复旧导出，无数据库回滚。
- 下载副作用必须在最终身份／代次检查之后，不能只测试 success 提示。

## 当前证据

实施与独立检查结果、完整门禁和实际浏览器证据见 [验证记录](verification.md)；本清单本身不是通过记录。
