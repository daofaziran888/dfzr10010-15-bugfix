# 湖仓一体化治理平台

面向数据架构师的湖仓一体化治理平台。覆盖数据湖表格式管理、Iceberg/Hudi表优化、小文件合并、数据压缩策略、元数据服务、表快照管理、数据分支合并与数据回滚恢复的完整湖仓治理链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 湖表格式管理 | `tablefmt` | 表格式配置 | 配置编号、表名称、格式类型 |
| 小文件合并 | `compact` | 合并任务 | 任务编号、目标表名、文件范围 |
| 数据压缩策略 | `compress` | 压缩配置 | 配置编号、目标表名、压缩算法 |
| 元数据服务 | `metadata` | 元数据表 | 表编号、表名称、字段清单 |
| 表快照管理 | `snapshot` | 快照记录 | 快照编号、表名称、快照时间 |
| 数据分支合并 | `branch` | 分支记录 | 分支编号、表名称、分支类型 |
| 数据回滚恢复 | `rollback` | 回滚记录 | 记录编号、表名称、回滚版本 |
| 分区演进管理 | `partition` | 演进计划 | 计划编号、表名称、当前分区 |
| 表优化管理 | `iceberg` | 优化任务 | 任务编号、表名称、优化类型 |
| 数据目录同步 | `catalog` | 同步作业 | 作业编号、源路径、目标目录、调度批次版本、依赖完成顺序 |
| 数据过期清理 | `cleanup` | 清理策略 | 策略编号、目标表名、过期条件 |
| 湖存储配额 | `quota` | 配额配置 | 配置编号、存储空间、表数量上限 |
| 湖仓血缘追踪 | `lineage` | 血缘记录 | 记录编号、上游表名、下游表名 |
| 治理任务调度 | `schedule` | 治理任务/依赖窗口/调度执行批次/审计台账 | 任务编号、任务类型、调度表达式、依赖任务、批次版本、依赖完成顺序 |
| 表统计信息 | `stats` | 统计报告 | 报告编号、表名称、行总数 |
| 数据迁移管理 | `migrate` | 迁移任务 | 任务编号、源表名、目标表名 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 治理任务调度：调度执行批次模型

治理任务调度与数据目录同步围绕“调度执行批次”联动，规则集中在
`backend/app/services/schedule_dispatch.py`，数据落在内存仓库的四张表：
`schedule`（治理任务）、`schedule_dependency`（依赖窗口与依赖完成顺序）、
`schedule_batch`（调度执行批次，版本单调递增）、`schedule_audit`（调度审计台账）。

- 任务状态机：启动 草稿/已暂停→运行中，暂停 运行中→已暂停，归档 已暂停→已完成，
  任何越序动作都会被拒绝并返回原因，不再出现“启动后停草稿、暂停显示运行中”。
- 下游任务只能在上游依赖完成后唤醒；上游未完成的唤醒会被拒绝，原因保留在任务上。
- 同一依赖窗口最多一个有效批次：重复唤醒、上游完成事件与超时重试同时到达都只生效一次；
  超时重试复用原批次接续失败步骤（版本不变），重试耗尽关闭窗口，上游重跑完成后才新建批次。
- 批次状态只能按 待调度→运行中→已完成/失败 推进，已发布（已完成）版本锁定，
  外部接口禁止回退；失败必须填写原因并同步到任务与审计台账。
- 数据目录同步沿用同一版本批次，目录清单记录批次版本与依赖完成顺序；历史无批次记录先兼容，
  再核对版本边界，跨版本/回退请求一律拒绝；处理结果与其余业务链路联动结果都写入调度审计台账。

主要接口（前缀 `/api/schedule`）：`GET /dependencies`、`GET /batches`、`GET /audit`、
`POST /{id}/wake`、`POST /{id}/upstream-complete`、`POST /{id}/timeout-retry`、
`POST /{id}/rerun`、`POST /batches/{id}/advance`、`POST /batches/{id}/fail`、
`POST /linkage/audit`；目录同步批次接口见 `/api/catalog/{id}/sync` 与 `/complete`。

领域：数据工程
子方向：数仓与数据湖
