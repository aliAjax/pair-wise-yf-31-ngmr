# 航班中断恢复系统

独立的 Python 标准库项目，用 SQLite 保存机场、飞机、机组、航线许可、航班、中断事件和恢复方案。系统会校验维护间隔、执勤时限、机场宵禁、航线许可、资源重叠，并计算取消、延误、受影响旅客和错失衔接成本。

## 运行

```bash
python3 app.py --db airline_recovery.db
```

默认监听 `127.0.0.1:8202`，首页为 `/`，健康检查为 `/health`。

身份头：`X-User-Id`、`X-Role`。角色包括 `viewer`、`scheduler`、`ops_manager`、`auditor`。

## 主要接口

- `POST /api/airports`、`/api/aircraft`、`/api/crew`、`/api/permits`：基础资源与约束。
- `POST /api/airports/{code}/timezone`：用 `expected_revision` 乐观更新机场时区，先提交者写入，后到者收到 `timezone_conflict`。
- `POST /api/flights`、`POST /api/disruptions`：创建航班和中断。
- `POST /api/recovery-plans`：一次提交方案及航班调整。
- `POST /api/plans/{id}/assignments`：用 `expected_revision` 临时改派。
- `POST /api/plans/{id}/validate`、`/lock`：校验并原子锁定方案。
- `GET /api/disruptions/{id}/compare`：比较恢复方案成本。
- `POST /api/flights/{id}/cancel`、`/recover`：取消和人工恢复。
- `GET /api/state`、`GET /api/plans/{id}`：查询状态和影响，时刻均附当地时间（`*_local`）。

## 时区规则

- 航班时刻一律以 UTC 存储；宵禁按机场当地时钟判断（到达时刻换算到目的地时区后与当地宵禁窗口比较）。
- 时区使用 IANA 时区数据库（`zoneinfo`）。把当地墙钟时刻解释为 UTC 时采用固定口径（PEP 495 `fold=0`）：令时回拨的重复小时取第一次（夏令时偏移），春令时跳时的空档顺延到跳时之后。
- 锁定方案时快照各机场所属时区；之后机场时区变更，未锁定方案的旧校验/评分结论立即失效（需重新校验），已锁定方案仍按锁定当时的时区解释。
- 旧数据库缺少时区列时，启动自动迁移并补默认时区 `UTC`；迁移逐条检查列是否存在，失败后重启服务即可重试。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

- 模型使用简化航线许可与宵禁规则；宵禁只比较目的地当地墙钟时刻，不区分日期与节假日。身份头、SQLite 和单进程 HTTP 服务适合原型演示，正式运行需要外部身份系统、共享数据库和更强的跨实例锁。
