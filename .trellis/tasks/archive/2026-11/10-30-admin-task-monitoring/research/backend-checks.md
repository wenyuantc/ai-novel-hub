# 后端实施与验证记录

日期：2026-10-31～2026-11-01。仅后端交付；前端/真实浏览器及独立评审由主会话协调。

## 实施范围

- 新增0026仅ADD COLUMN：Task.queued_at、admin_cancel_operation_id、admin_cancel_requested_at及窄索引；旧时间null、pending拒绝降级，不重建SQLite父表。
- 新增admin_task schema/service/router：UUID定位，显式元数据白名单；过滤CTE、计数、分页在同SQL中读取，包括回收站/删除中作品。原Task.title/input/output/result/checkpoint/配置不读取，最终错误摘要仅由状态推导，连原Task.error也不读取；阶段仅固定文案/严格数字模板。
- task.cancel_request审计与持久marker同事务受理；202不等于已取消；有界原因仅此动作允许自由文本，真实actor/owner及安全目标名称留痕。
- 独立single-flight取消drain、queued扫描/claim屏障、queued/paused先清理后终态，running原finalize/recover状态+统计+清marker同CAS提交。abort/持久化失败保留原屏障，未知handler的pending fail-closed。
- owner取消/继续/结束/拆书跳过均加marker条件写；单删/裁剪/清理/删号/项目排空/purge/shutdown保护pending及真实清理链。
- commit确认丢失重读同UUID真实结果；进程内已知done/paused暂存赢家按queued_at本轮隔离；已完成管理员资源代次同样按本轮隔离，并跟随现存Task生命周期清缓存。resume入队时间微秒递增，冻结/回拨不复用轮次。

## 环境与命令

本沙箱默认uv缓存`/Users/wenyuan/.cache/uv`拒绝访问，使用本次专用可写缓存，不修改开发者环境：

```bash
env UV_CACHE_DIR=/private/tmp/ai-novel-admin-task-uv uv run ruff check .
env UV_CACHE_DIR=/private/tmp/ai-novel-admin-task-uv LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest ... -q
```

下列命令工作目录均为`backend/`，数据库均由pytest tmp_path逐例隔离、FakeLLM，不访问真实模型/DNS。

## 已完成检查

- `uv run ruff check .`（带上述UV_CACHE_DIR）：通过。
- `git diff --check -- backend`：通过。
- 首轮新API/迁移：`tests/test_admin_tasks.py tests/test_admin_task_monitoring_migrations.py`，22 passed（后续补了成功旧error/阶段白名单例）。
- 高风险既有回归：`tests/test_tasks.py tests/test_task_statistics.py tests/test_task_statistics_integration.py tests/test_task_statistics_identity.py tests/test_recycle_bin_tasks.py tests/test_admin_audit_logs.py tests/test_admin.py tests/test_batch_generate.py tests/test_book_import.py tests/test_migrations.py`，205 passed in 132.06s。
- 新增4文件联合：`tests/test_admin_tasks.py tests/test_admin_task_cancellation.py tests/test_admin_task_monitoring_migrations.py tests/test_admin_task_adversarial.py`，68 passed in 36.45s（后续独立评审又补了确认丢失/轮次/未知handler）。
- 确认丢失补丁后局部：`tests/test_admin_task_cancellation.py tests/test_task_statistics_integration.py tests/test_task_statistics_identity.py`，52 passed in 21.15s。
- 轮次/未知恢复补丁后主取消：`tests/test_admin_task_cancellation.py`，29 passed in 14.29s；之后新增unknown-finalize例，最终联合验证另记。
- 独立对抗代理的16例与关联验证详见`adversarial-checks.md`，独立check新增两例详见checker报告。

## 中止/失败记录（不算通过）

- 第一次全量session37716：414 passed、1 failed后正常SIGINT，退出2。唯一失败是旧测试把running残留原error当作execution_failed；主会话要求安全投影仅按真实状态推导后，独立测试代理核实契约并修正该断言，隐私/搜索/审计断言保留。
- 第二次全量session4564：449 passed、0失败时因独立评审发现额外产品边界而正常Ctrl-C，退出2。不能宣称全量通过。
- 联合session53987：启动瞬间独立checker新测试文件尚未落盘，file not found、no tests ran，退出4；文件出现后以同命令重新执行session27983。

## 最终联合/全量

冻结交接时联合（session27983）：**114 passed in 50.65s，退出0**。

```bash
env UV_CACHE_DIR=/private/tmp/ai-novel-admin-task-uv LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_admin_tasks.py tests/test_admin_task_cancellation.py tests/test_admin_task_monitoring_migrations.py tests/test_admin_task_adversarial.py tests/test_admin_task_check_regressions.py tests/test_task_statistics_integration.py tests/test_task_statistics_identity.py tests/test_recycle_bin_tasks.py -q
```

独立checker后续补齐共享`complete_state`在finalize/recover/drain的真实commit确认丢失处理、缓存元数据500-key分块核验，并精确适配既有User真实写锁与SSE两种赢家的事件屏障测试，未放宽取消/权限/超时契约。

最终重跑（session77921）使用允许本机loopback的审批环境，仅用于原有MCP本地协议测试，不访问真实LLM或真实库；完整日志：`/private/tmp/admin-task-monitoring-01a1928f-20261101-final2.log`。

```bash
env UV_CACHE_DIR=/private/tmp/ai-novel-admin-task-uv LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q > /private/tmp/admin-task-monitoring-01a1928f-20261101-final2.log 2>&1
```

**最终结果：1292 passed, 1 warning in 574.90s (0:09:34)，进程正常退出0。**
无失败、无跳过；唯一warning为既有Gemini Imagen SDK弃用提示。最终冻结代码上的`ruff check .`与`git diff --check -- backend`也再次通过。

## 最终后端变更文件

- 模型/迁移：`app/models/entities.py`、`alembic/versions/0026_admin_task_monitoring.py`。
- 管理API/DTO：`app/api/v1/__init__.py`、`app/api/v1/admin_tasks.py`、`app/schemas/admin_task.py`、`app/schemas/audit.py`。
- 业务/审计/生命周期：`app/services/admin_task_service.py`、`admin_service.py`、`audit_writer.py`、`task_service.py`、`recycle_bin_service.py`、`book_import/reverse.py`、`kb_index_service.py`。
- 引擎/收尾：`app/services/task_engine/engine.py`、`cancellation.py`、`handlers/chapter.py`、`handlers/batch.py`。
- 新增验证：`tests/test_admin_tasks.py`、`test_admin_task_cancellation.py`、`test_admin_task_monitoring_migrations.py`、`test_admin_task_adversarial.py`、`test_admin_task_check_regressions.py`。
- 既有验证精确适配：`tests/test_project_lifecycle.py`、`tests/test_tasks.py`；保留RESTRICT防御，SSE取消/不可见两种真实赢家由事件屏障分别验证，未改超时/弱化权限或取消契约。

本实现代理后期冻结后，独立checker拥有最终产品/测试修复；其完整结论见本任务`research/full-scope-check.md`。没有提交、切换分支或改动开发者真实数据库。

## 未验证边界

- 未运行真实PostgreSQL并发/行锁验证，SQLite结果不等价于PG实测。
- 不承诺任意强杀时恢复“正文事务已提交但Task finalize尚未提交”的精确赢家；重启仍按原interrupted语义，不重放LLM/MCP。
- 未知handler或异常终态+marker保持安全屏障并暴露取消中，不猜测abort或删除恢复依据，需要运维修复不一致数据。


---

# 最终全量第一轮失败定位

执行目录`backend/`，session49112，2026-11-01；命令：

```bash
env UV_CACHE_DIR=/private/tmp/ai-novel-admin-task-uv LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q
```

输出汇总：`7 failed, 1283 passed, 1 warning in 584.70s (0:09:44)`；进程正常退出1，未中止。
工具返回的22448-token完整trace被工具输出截断，未事前重定向文件，以下仅保留实际可观察的关键trace，不冒充完整日志。

## MCP四例

```
FAILED tests/test_mcp_plugins.py::test_connect_time_ssrf_check_sends_no_request[settings_overrides0]
FAILED tests/test_mcp_plugins.py::test_redirect_not_followed
FAILED tests/test_mcp_plugins.py::test_fastmcp_integration_list_and_call[settings_overrides0-streamable_http]
FAILED tests/test_mcp_plugins.py::test_fastmcp_integration_list_and_call[settings_overrides0-sse]

tests/mcp_fakes.py:87: PermissionError
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
E PermissionError: [Errno 1] Operation not permitted
```

这是沙箱本机监听权限失败，尚未执行协议被测行为，不能跳过后声称通过。

## 项目生命周期

```
FAILED tests/test_project_lifecycle.py::test_concurrent_project_creation_after_empty_count_blocks_user_deletion
E sqlite3.OperationalError: database is locked
```

独立INSERT参数包含作品标题“计数后并发创建”；旧测试的说明是“SQLite的User行锁不生效时，count之后并发创建仍由RESTRICT兜底”。新User真实no-op写锁改变了该可达时序，需独立check验证/更新真实屏障测试，不能仅加大锁超时。

## 关系生成超时

```
FAILED tests/test_relations_generate.py::test_empty_result_and_errors_never_write[   -failed-模型未返回任何内容]
output='   ', status='failed', error='模型未返回任何内容'
fake_llm.responses=[['   ']], calls=[]

tests/test_relations_generate.py:260: task, events = await run_task(...)
tests/helpers.py:285: events = await stream_task(client, resp.json()["id"])
tests/helpers.py:276: resp = await asyncio.wait_for(client.get(f"/api/v1/tasks/{task_id}/stream"), within)
...
fastapi/dependencies/utils.py:802: solved = await run_in_threadpool(call, **solved_result.values)
starlette/concurrency.py:34: return await anyio.to_thread.run_sync(func)
anyio/_backends/_asyncio.py:2670: async with limiter or cls.current_default_thread_limiter()
anyio/_backends/_asyncio.py:2208: await AsyncIOBackend.cancel_shielded_checkpoint()
anyio/_backends/_asyncio.py:2588: await sleep(0)
asyncio/tasks.py:650: yield
E asyncio.exceptions.CancelledError
...
asyncio/timeouts.py:115: raise TimeoutError from exc_val
E TimeoutError
```

时间记录：setup `00:04:57.055`；register201 `.384`，AI配置201 `.408`，作品201 `.426`，三个人物201 `.448/.453/.457`，生成请求202 `.503`；未出现模型调用。不能仅据此把超时归为业务失败，需复现。

## 软删订阅事件

```
FAILED tests/test_tasks.py::test_subscribed_task_deleted_ends_with_error[settings_overrides0]
tests/test_tasks.py:486:
assert events[-1][0] == "error" and events[-1][1]["detail"] == "任务已取消"
E AssertionError: assert ('error' == 'error' and '任务不存在' == '任务已取消')
```

测试配置`task_max_per_user=1`、`engine.heartbeat_seconds=0.05`；第一章FakeLLM被Gate阻塞，第二项目章节任务queued，已等subscriber_count==1后DELETE第二项目204。

实际日志：GET tasks/2/stream 200 `00:06:08.394`；DELETE projects/2 204 `.405`。原高风险205项回归此例曾通过，当前可能是心跳与删除协调边界的可控竞态，需check定位，不以概率通过替代契约。
