# MovieLens 数据治理前端

React + TypeScript + Vite 单页应用，用于提交自然语言治理任务、轮询任务进度，并展示五维评分、清洗摘要、结果文件与默认规则。

界面采用满高对话工作区：请求、状态和必要摘要显示在中央对话流；详细五维对比、结果文件、规则与报告按真实任务数据组织在右侧可收缩资源树中。桌面端资源面板固定在右侧，小屏幕显示为可关闭抽屉。文件节点直接打开后端提供的 artifact 地址，文件夹节点仅控制层级展开。

## 启动

```bash
npm install
cp .env.example .env
npm run dev
```

Windows PowerShell 可使用 `Copy-Item .env.example .env`。生产构建：

```bash
npm run build
```

## 环境变量

`VITE_API_BASE_URL` 为后端地址，例如 `http://localhost:8000`。留空时使用当前域名，适合由网关转发 `/api` 的部署方式。

## 接口流程

1. 向 `POST /api/agent/messages` 发送 `{ "message": "..." }`。
2. 从响应顶层的 `task_id` 查询任务；若旧服务未返回该字段，则调用使用默认输入的 `POST /api/tasks`。
3. 每 2 秒查询 `GET /api/tasks/{id}`，终态后停止轮询。
4. 成功后并行读取 `/result` 与 `/artifacts`；默认规则来自 `GET /api/rules/default`。

结果接口直接返回含 `dimensions`、`rows_before`、`rows_after`、`dispositions`、`explanation` 和 `rule_version` 的结果 DTO；分数为 0–1 时仍会自动换算为百分制。
