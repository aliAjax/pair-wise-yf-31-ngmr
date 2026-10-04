# 航班中断恢复系统

独立的 Python 标准库项目，用 SQLite 保存机场、飞机、机组、航线许可、航班、中断事件和恢复方案。系统会校验维护间隔、执勤时限、机场宵禁、航线许可、资源重叠，并计算取消、延误、受影响旅客和错失衔接成本。

## 时区口径

- 所有航班时刻以 **UTC** 存储；机场记录 IANA 时区（`timezone`，如 `Asia/Shanghai`、`Europe/London`）和**当地挂钟**宵禁窗口（`curfew_start`/`curfew_end`，可跨午夜）。
- 恢复方案校验宵禁时，先把 UTC 到达时刻换算成目的地当地时间，再与当地宵禁比较，避免跨时区把两地时刻当成同一时区误判。
- 夏令时固定口径（PEP 495）：
  - **空档 gap**（春季跳表后不存在的当地小时）：顺延到跳表之后（fold=0 解析）。
  - **重复小时 repeated**（秋季回表）：取第一次出现，即夏令时那次（fold=0）。
  - 换算结果带 `wall_kind`（normal/gap/repeated）、`dst`、`utc_offset`。
- 宵禁 `start==end` 表示全天宵禁。

## 运行

```bash
python3 app.py --db airline_recovery.db
```

默认监听 `127.0.0.1:8202`，首页为 `/`，健康检查为 `/health`。

历史数据库没有机场时区列，启动时自动迁移补默认区（环境变量 `AIRLINE_DEFAULT_TZ`，默认 `UTC`）。迁移逐版本记录在 `schema_migrations`，每步幂等；中断后重启会补齐缺失列后才记版本，可安全重试。

身份头：`X-User-Id`、`X-Role`。角色包括 `viewer`、`scheduler`、`ops_manager`、`auditor`。

## 主要接口

- `POST /api/airports`、`/api/aircraft`、`/api/crew`、`/api/permits`：基础资源与约束。机场可带 `timezone`（仅首次创建时写入，重复 seed 不覆盖别人已做的时区更新）。
- `POST /api/airports/{code}/timezone`：修改机场时区，需 `expected_tz_version` 乐观锁。两人同时提交时只让先到的写入，后到者收到 409 `timezone_conflict` 与当前版本。
- `POST/GET /api/time-convert`：UTC↔当地换算。入参 `timezone` 或 `airport`，配合 `utc` 或不带偏移的 `local` 挂钟时间。
- `POST /api/flights`、`POST /api/disruptions`：创建航班和中断。
- `POST /api/recovery-plans`：一次提交方案及航班调整。
- `POST /api/plans/{id}/assignments`：用 `expected_revision` 临时改派（改派后旧校验结论作废）。
- `POST /api/plans/{id}/validate`、`/lock`：校验并原子锁定方案。
- `GET /api/disruptions/{id}/compare`：比较恢复方案成本。
- `POST /api/flights/{id}/cancel`、`/recover`：取消和人工恢复。
- `GET /api/state`、`GET /api/plans/{id}`：查询状态和影响，返回每条起降的当地时间、时区、DST 标记。

## 时区更新与方案结论

- 草稿方案保存校验结论（`conclusion_status`：valid/invalid）。机场时区更新后，所有引用该机场的**未执行（草稿）方案**结论立即失效（置 `stale`、清空分数），必须重新校验；审计日志记录 `timezone_invalidated`。
- **已锁定方案**不失效：锁定时把各机场时区与宵禁快照写入 `timezone_snapshot_json`，此后始终按锁定当时的时区解释，页面展示的当地时间也基于快照。
- 首页表格同时显示当地时间与 UTC；原始 JSON 在页面底部。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_timezone.py` 覆盖跨时区宵禁、DST 空档/重复小时、老库迁移补默认区与失败重试、并发时区更新首写获胜、旧结论失效与锁定快照。

## 主要局限

时区使用系统 IANA 数据库（`zoneinfo`），精简环境如缺失需安装 `tzdata`；模型使用简化航线许可与宵禁规则。身份头、SQLite 和单进程 HTTP 服务适合原型演示；首写获胜依赖进程内写锁与乐观版本，多实例部署需要共享数据库和更强的跨实例锁。
