# 水果深加工招商台账后端服务

记录合作主体、园区、项目、洽谈、立项、里程碑和投产后的产能兑现情况，为招商团队提供可追溯的业务接口。

## 运行约定

服务端代码位于 `app` 目录，默认使用项目目录中的 SQLite 文件保存业务数据。环境变量可以覆盖数据库位置和接口前缀，临时配置不应提交到仓库。

## 风险快照接口（只读冻结报告）

按指定时间点汇总项目投资额、里程碑、产能兑现与未解决跟进事项，生成后全量冻结，项目后续修改不影响已生成报告。

| 方法与路径 | 说明 | 最低角色 |
|---|---|---|
| `POST /api/v1/risk-snapshots/` | 按 `project_id` + `as_of_date` 生成快照；同项目同时间点重复生成幂等返回原快照（201 新建 / 200 已有） | analyst |
| `GET /api/v1/risk-snapshots/` | 快照清单（可按项目、日期筛选） | viewer |
| `GET /api/v1/risk-snapshots/lookup` | 按项目+时间点精确查找 | viewer |
| `GET /api/v1/risk-snapshots/{id}` | 查看冻结载荷（记录审计） | viewer |
| `GET /api/v1/risk-snapshots/{id}/download` | 下载冻结 JSON（记录审计，响应头 `X-Snapshot-Hash` 为文件字节 SHA-256） | viewer |
| `GET /api/v1/risk-snapshots/{id}/access-logs` | 查看/下载审计记录 | director |

鉴权使用 `Authorization: Bearer <token>`。令牌通过环境变量 `RISK_SNAPSHOT_TOKENS` 配置，格式为 `姓名:角色:令牌`，多条以英文逗号分隔，角色取值 `director`/`analyst`/`viewer`；未配置时使用内置演示令牌，生产部署必须覆盖。快照载荷不包含联系人姓名、电话、邮箱等敏感字段。

时间点语义：`as_of_date` 含当日，截止时刻为次日 00:00（UTC），关联记录按入库时间严格 `< cutoff` 纳入；项目状态按状态变更日志重放。

## 测试

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

在项目根目录执行：

```bash
python3 -m compileall -q app tests
```

## 启动服务

准备依赖后可执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，根路径返回服务状态，接口文档位于 `/docs`。
