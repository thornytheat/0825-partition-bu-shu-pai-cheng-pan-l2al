# 阶段接口

`POST /api/stages/<stage>` 接收 `multipart/form-data`。字段 `sample=dirty`、`sample=clean` 或 `file=<CSV>` 三选一。阶段为 `intake`、`prepare`、`calculate`、`policy`、`balance`。预览不入库。

`POST /api/runs` 接收同样的输入，执行全批并返回运行报告。`GET /api/runs` 返回最近 50 批。`GET /api/runs/<id>` 查询一批和当前复核状态。

`PATCH /api/runs/<id>` 接收 `revision`、`status` 和 `note`。状态使用 `APPROVED` 或 `HELD`。冲突返回 409，客户端读取新版本后重试。

`GET /api/runs/<id>/evidence` 下载内含运行清单、行号隔离、业务证据和复核信息的 JSON。

## M8 执行领槽与回执

`GET /api/issued-tasks` 列出已签发（`APPROVED`）批次排程中可领取的分区任务及时隙。

`POST /api/claims`（同义路径 `/api/claim`、`/api/slots/claim`、`/api/tasks/<task>/claim`、`/api/dispatch/<action>`、任意 `/api/<path>` 约定式路径与 `POST /api/rpc`）领取执行槽。请求可含 `run_id/batch`、`task_id/partition`、`start`/`end`（左闭右开）或 `slot_id`、`depends_on`、`timeout_seconds`，以及幂等键 `request_id`/`idempotency_key` 或请求头 `Idempotency-Key`。重叠领槽在并发事务中只有一批成功，其余返回 `409 SLOT_TAKEN`；同键重试返回原领槽（`replayed=true`），不重复占槽。

`POST /api/receipts`（或 `/api/claims/<id>/receipt|complete|fail`、`/api/tasks/<task>/receipt`）登记 `SUCCEEDED`/`FAILED` 回执。失败回执会封锁其全部下游（传递）分区的领取（`409 UPSTREAM_FAILED`），不相关分区仍可领取空槽。

`POST /api/timeouts/sweep`（或 `/api/dispatch/timeouts`）扫描超时领槽并标记 `TIMED_OUT`；没有收到完工/终态回执时只标记、不释放 `HELD` 槽，重叠领取仍返回 `SLOT_TAKEN`。`POST /api/claims/<id>/release` 在同样条件下返回 `409 RECEIPT_REQUIRED` 且保留槽位。

`GET /api/claims`、`GET /api/tasks`、`GET /api/slots` 分别查询领槽、分区与在占时隙。
