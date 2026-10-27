# Design

- 请求：`WizardRequest.continue_from_task_id: int | None`。
- 校验（`handlers/wizard.py validate_and_claim` → `wizard_service`）：加载旧任务并检查归属/kind/状态/输出；用旧任务 `input.request` 覆盖本次参数（`ai_config_id` 取本次值优先）；`TaskSpec.input.request` 记录 `continue_from_task_id`；新任务 `retry_of_id` = 旧任务。
- 组装：`build_wizard_job` 读取旧任务 `output_text`，`trim_to_resume_point` 截至最后一个 `}`；messages 追加 `assistant: partial` + `user: wizard.continue 指令`（多轮，不 prefill）；`WizardJob.prefix = partial`。
- 运行：`run_wizard_job` 先 yield prefix 为 delta（output_text 为累积文本，可链式续写），续写片段经 `join_continuation(prefix, cont)`（去 ``` 围栏 / 前导空白、与 prefix 末尾重叠去重、`}` 后接 `{` 补逗号）后进入原 `extract_json → save_wizard_result`。
- 前端：`WizardGenerator` 挂载时无活动任务则查最近一条同 kind 任务，失败且有输出则 `followTask` 恢复（stream 对已结束任务发 snapshot+error）；失败且 `task.text` 非空时显示「从中断处继续」。
