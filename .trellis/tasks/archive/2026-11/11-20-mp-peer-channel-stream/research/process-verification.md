# 两个进程的公共 SSE 验收

2026-11-24 在本机环回地址启动两个独立 Uvicorn 进程。每次演练都新建一个临时 SQLite 文件，执行 Alembic 到当前版本后才启动应用。两个应用共享签名密钥、数据库及受控模型服务。生成请求经过生产 HTTP、调度、模型 SDK、持久化和公开 SSE 路由。模型服务控制两段文字返回的时间，让客户端能断言正在执行时已经收到增量。

脚本通过只读查询把任务的公开键、运行租约和操作系统 PID 对齐。端口顺序为实际执行端 A、接入端 B，任务由真实调度决定在哪个进程执行。

| SQLite / 私有通道 | 场景 | A / B PID | A / B 公共端口 | 观测 |
| --- | --- | --- | --- | --- |
| DELETE / UDS | 同一作者同时订阅 A、B | 90955 / 90793 | 59512 / 59439 | 两端完整事件与数据相同，依次为 snapshot、delta、progress、delta、done。 |
| WAL / TCP | 同一作者同时订阅 A、B | 94508 / 94659 | 60255 / 60373 | 两端完整事件与数据相同，顺序与 UDS 一致。 |
| DELETE / UDS | 协作者已收到实时增量后，通过公开 HTTP 撤销授权 | 31001 / 31174 | 54765 / 54844 | B 依次收到 snapshot、delta、error。A、B 均以 task 为 null 的 permission_denied 结束，两端的完整实际响应均未出现撤权后送入模型的标记。 |
| DELETE / UDS | 已收到增量后强杀实际执行进程 | 94509 / 94663 | 60262 / 60381 | B 在原 HTTP 响应继续收到持久化快照，最后只发一个 error。任务状态为 interrupted；重新订阅得到和前一响应最终两帧相同的 snapshot、error，任务详情也返回 interrupted。 |

## 复跑

从仓库根目录进入 backend。脚本会按所在位置找到归档的 Trellis 资料。

```sh
cd backend
PYTHONPATH=. uv run python ../.trellis/tasks/archive/2026-11/11-20-mp-peer-channel-stream/research/process_stream.py --backend . --transport uds --cases forward revoke kill
PYTHONPATH=. uv run python ../.trellis/tasks/archive/2026-11/11-20-mp-peer-channel-stream/research/process_stream.py --backend . --transport tcp --wal --cases forward
```

双应用回归文件为 `backend/tests/test_c4_multiapp_streams.py`。它在同一个 Python 测试进程内为 A、B 分别构建生产应用、连接池、HTTP 端口和真实私有 TCP 监听。只有 A 启动调度；B 经持久化数据库确认任务由 A 执行后才连接真实订阅。用例逐帧比较单章、批量和智能体事件，对照作者与协作者两种身份，另验断线、剩余正文持久化、终态重连和客户端空闲退订。

保留执行进程、只切断私有通道的实测在[浏览器验收记录](frontend-verification.md)。其中作者的 B 端在已收到实时增量后转入持久化快照，最终只收到一次 done。协作者的私有连接被切断但监听仍在线时，B 的公开流受控结束；浏览器重连后收到新快照、后续增量和同一任务的 done。

自动化回归的 SQLite、PostgreSQL 执行数以及最终门禁由[综合验收记录](verification.md)汇总。本页的 PID 和端口只对应上表实际运行过的独立操作系统进程。

## 原执行端已保存章节、尚未发布终态

`test_original_http_stream_recovers_unpublished_chapter_receipt` 使用双应用真实 TCP 端口。作者从 B 订阅，先收到正在执行的 A 发出的正文增量。章节生成器进入最终处理前，屏障保留其调用栈，测试从 B 的独立连接池读到同章已保存版本，与原任务键和执行键匹配的 `phase=completed` 收据，以及仍为 `running` 的任务。A 的 hub 尚未关闭，两端没有发布终态。

断开 A 的私有通道并停止 A 的调度后，库里的任务仍是 `running`。B 启动调度，从原收据接管，在相同 HTTP 响应中发出唯一的 `done`。用例要求任务为 `succeeded`，`unpublished` 为真，`result` 为空，并显示“正文已保存，请刷新后查看”的固定说明。B 的新执行者标识和收据收尾标记落库；重新订阅获得与原响应最后两帧逐字段相同的 `snapshot`、`done`。

2026-11-24 的 SQLite 串行 C4 专项共 110 项，全部通过。JUnit 报告记载本用例用时 4.324 秒，包含上述数据库收据、同一响应及重连的全部断言。另可在 backend 目录运行 `uv run pytest -n 0 -q tests/test_c4_multiapp_streams.py::test_original_http_stream_recovers_unpublished_chapter_receipt`。
