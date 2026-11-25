# 作品删除时排队订阅的竞态调查

2026-11-25 后端全量测试中，`test_subscribed_task_deleted_ends_with_error[cancel-settings_overrides0]` 期待最后一个错误帧是“任务已取消”，实际收到“任务不存在”。[脱敏失败摘录](full-suite-failure.json)保留了原用例、耗时与断言结果；原 JUnit 没有内部事件顺序，因此不能声称已直接观测到那一次失败的具体调度顺序。

既有契约允许两个赢家：取消帧先被消费时返回带 `cancelled` 任务的“任务已取消”；作品删除意图先被可见性检查观察到时返回 `task=null` 的“任务不存在”。两者都必须终止旧流，且删除成功后任务实际取消、章节释放、新订阅返回 404。契约见 `.trellis/spec/backend/error-handling.md` 的任务订阅部分。

旧测试仅把取消分支的 `heartbeat_seconds` 设为 60 秒，并在 `stop_project_tasks` 返回后阻塞最终软删；这没有阻止排队订阅使用 `follow_local_queue=True`，其等待上限为 `min(0.2, poll_interval, remaining)`。作品删除先提交 `deletion_started_at`，再取消任务；若可见性轮询在这两步之间发生，流可以按契约先结束，而测试仍强制期待取消帧。

## 屏障实验

- [修正前受控复现](forced-lifecycle.json)：只在测试进程内把真实取消调用延迟 1 秒。删除意图已提交且任务仍为 `queued` 时，可见性检查先返回不可见，公开结果为 `task=null/任务不存在`；随后真实取消关闭 hub，原取消分支断言失败。这证明旧屏障存在该可复现路径，不将其冒充全量失败的已观测内部顺序。
- [修正后延迟取消](post-fix-delayed-cancel.json)：同样延迟 1 秒，但测试等待真正取消完成后才恢复快照之后的消费。记录先后为真实 hub 关闭、任务落库 `cancelled`、章节恢复 `draft`、可见性复查、原作者的安全取消投影、公开 `任务已取消` 帧；投影中的私人引用均已移除。测试通过。
- [修正后强制取消先赢](post-fix-cancel.json)：真实关闭 hub 和取消落库后，公开流返回带 `cancelled` 任务的取消帧，测试通过。
- [修正后生命周期先赢](post-fix-lifecycle.json)：仍先返回 `task=null/任务不存在`，之后执行真实取消、关闭 hub 并释放章节，测试通过。

[顺序自动核验](sequence-audit.json)对上述关键事实及前后顺序执行断言。测试修改仅在 `backend/tests/test_tasks.py` 的取消分支暂停快照后的迭代，并核对真实取消结果、原 hub 已关闭和章节状态；它没有替换产品的取消、授权、逐帧投影或数据库逻辑。原两个分支的不同末帧断言均保留。探针是隔离 pytest 进程内的观察／事件屏障，只记录任务状态、时间、错误类型和布尔事实，不保存正文或凭据。

## 检查与复跑

- `tests/test_tasks.py`：33 passed。
- 原两分支各参数化重复 10 次：20 passed。首次尝试 `--keep-duplicates` 只收集了 2 项，没有作为重复验证；改用本目录的隔离参数化插件。
- 修正后受控延迟取消、强制取消先赢、生命周期先赢三个探针：各 1 passed。

在 `backend/` 下，将 `C7_RACE_PROBE_DIR` 指向本目录，`C7_RACE_REPORT` 指向不存在的结果路径：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. .venv/bin/python -m pytest \
  -n 0 -q --tb=no tests/test_tasks.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=".:$C7_RACE_PROBE_DIR" .venv/bin/python -m pytest \
  -n 0 -q --tb=no -p c7_repeat_probe tests/test_tasks.py::test_subscribed_task_deleted_ends_with_error
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=".:$C7_RACE_PROBE_DIR" \
  C7_RACE_MODE=lifecycle_first C7_RACE_REPORT="$C7_RACE_REPORT" .venv/bin/python -m pytest \
  -n 0 -q --tb=no -p c7_race_probe \
  'tests/test_tasks.py::test_subscribed_task_deleted_ends_with_error[cancel-settings_overrides0]'
```

第三条中的 `lifecycle_first` 表示在取消分支中人为推迟真实取消，目的是重现修正前的轮询先赢；修正后该测试应通过。将模式改为 `cancel_first` 可验证取消先赢；将模式改为 `observe` 并选用 `[lifecycle-settings_overrides0]` 可验证原生命周期分支。每次均须指定新的报告路径。
