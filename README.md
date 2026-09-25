# 水果深加工招商台账后端服务

记录合作主体、园区、项目、洽谈、立项、里程碑和投产后的产能兑现情况，为招商团队提供可追溯的业务接口。

## 运行约定

服务端代码位于 `app` 目录，默认使用项目目录中的 SQLite 文件保存业务数据。环境变量可以覆盖数据库位置和接口前缀，临时配置不应提交到仓库。

## 测试

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 项目风险快照

面向市级招商例会取数场景，按指定基准时间点冻结单个项目的投资额、里程碑、产能兑现与未解决跟进事项：

- `POST /api/v1/projects/{project_id}/risk-snapshots`：生成快照，请求体 `{"as_of": "2026-09-01T00:00:00", "operator": "张科长"}`，`as_of` 缺省为当前时间。同一项目同一基准时间重复请求返回已冻结快照（首次 201，重复 200），后续项目修改不影响已生成快照。
- `GET /api/v1/projects/{project_id}/risk-snapshots`：快照列表（仅元信息）。
- `GET /api/v1/projects/{project_id}/risk-snapshots/{snapshot_id}`：下载快照完整内容。

快照只纳入基准时间之前创建的关联记录，项目状态按状态日志还原到基准时点；返回内容包含稳定排序的风险项、缺失字段清单与各部分资料的来源时间，且不含联系人电话、邮箱等敏感字段。

以上接口通过 `X-User-Role` 请求头鉴权：`admin`、`investment_lead` 可生成与下载，其余角色或缺失角色返回 403。

## 编译检查

在项目根目录执行：

```bash
python3 -m compileall -q app tests
```

## 启动服务

准备依赖后可执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，根路径返回服务状态，接口文档位于 `/docs`。
