# MovieLens Analysis Agent

用于 MovieLens `ratings.dat` 清洗、五维数据质量评分和结果下载的 FastAPI + React 示例。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

随后分别启动 API、Worker 和前端：

```powershell
uvicorn backend.main:app --reload
python -m backend.worker
cd frontend
npm install
npm run dev
```

前端见 `frontend/README.md`。不传 `input_path` 时使用 `DEFAULT_INPUT_PATH`（默认 `data/sample/ratings.dat`）；显式路径相对 `DATA_DIR` 解析且不能越界。输入和清洗后的 `.dat` 文件使用 ISO-8859-1 编码及 `::` 分隔。

主要配置见 `.env.example`。Hadoop 执行器支持两种方式：本地方式让 `HADOOP_COMMAND` 以 `HADOOP_BIN` 开头，并在执行前检查本机 Hadoop；外部方式可让命令以本机已有客户端（如 `ssh`）开头，并用可选的 `HADOOP_PREFLIGHT_COMMAND` 检查远端，不要求本机安装 Hadoop。例如输入、规则及产物目录由本机和远端共享时，可配置 `HADOOP_COMMAND="ssh hadoop-host /opt/jobs/run-cleaner {input} {output} {result} {rules}"`。外部任务必须在占位符指定、本机可见的 `{output}` 和 `{result}` 路径生成文件，否则任务会明确进入 `failed`。本项目不负责 SSH 密钥、文件上传下载或 Hadoop 安装。
