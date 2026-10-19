# MovieLens Analysis Agent

MovieLens `ratings.dat` 清洗、五维数据质量评分和结果下载。后端为 FastAPI，前端为 React，清洗与评分可跑在本地进程或真实 Hadoop 集群上。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

分别启动 API、Worker 和前端：

```powershell
uvicorn backend.main:app --reload
python -m backend.worker
cd frontend
npm install
npm run dev
```

前端详见 `frontend/README.md`。不传 `input_path` 时使用 `DEFAULT_INPUT_PATH`（默认 `data/sample/ratings.dat`）；显式路径相对 `DATA_DIR` 解析且不能越界。输入和清洗结果使用 ISO-8859-1 编码及 `::` 分隔。

## 在已有 Hadoop 集群上运行

前提：本机（或运行本服务的那台机器）已能直接执行 `hadoop` 和 `hdfs`，集群 HDFS/YARN 正常，且**每个 worker 节点都有 `python3`**。

1. 确认客户端可用：

   ```bash
   hadoop version
   hdfs dfs -ls /
   ```

2. 在 `.env` 中设置执行器与集群参数：

   ```dotenv
   DEFAULT_EXECUTOR=hadoop
   HADOOP_BIN=hadoop
   HDFS_BIN=hdfs
   HDFS_WORK_DIR=/movielens/work
   STREAMING_PYTHON=python3
   DATA_VERSION=ml1m-raw-v1
   T1=2000-01-01T00:00:00Z
   T2=2003-01-01T00:00:00Z
   ```

   `HADOOP_STREAMING_JAR` 通常不必设置：程序会先查 `$HADOOP_HOME/share/hadoop/tools/lib/hadoop-streaming-*.jar`，再查 `hadoop classpath`。若两处都找不到，手动指定。

3. 启动 API 与 Worker，照常提交任务。一次任务会依次执行四个 Streaming 作业：

   - `score_before`：原始数据五维评分
   - `clean`：校验、格式修复、按 `(user, movie)` 保留最新时间戳
   - `quarantine`：把无法修复的记录连同原因隔离，不静默删除
   - `score_after`：清洗结果五维评分

4. 确认任务真的跑在 Hadoop 上：

   ```bash
   yarn application -list
   hdfs dfs -ls /movielens/work/<task_id>
   ```

   任务目录下应有 `raw/`、`before/`、`clean/`、`quarantine/`、`after/`、`report.json`。前端资源面板里的 `ratings-cleaned.dat`、`ratings-quarantine.tsv` 和 `report.json` 是从 HDFS 取回的实际产物。

完整参数、目录结构与常见错误见 `hadoop/README.md`。

## 清洗与评分口径

五个维度各自衡量不同问题，避免一个缺陷同时拉低多项分数：

| 维度 | 衡量内容 | 分母 |
|---|---|---|
| Accurate | UserID、MovieID 为正整数，评分为 1–5 的整数 | 输入记录数 |
| Complete | 四个必填字段均存在且非空白 | 输入记录数 |
| Unique | 不同 `(UserID, MovieID)` 业务键数量 | 可用记录数 |
| Up-to-date | 时间戳落在数据集声明采集期 2000-01-01 至 2003-02-28 | 取值合法的记录数 |
| Consistent | 字段以规范整数写法、使用 `::` 分隔 | 输入记录数 |

清洗动作分成四类，不再把删除当成修复：

- **修复后保留**：去除首尾空白、把 `4.0` 规范为 `4`，取值不变。
- **去重删除**：同一用户与电影保留时间戳最新的一条。
- **隔离待处理**：格式、取值或时间戳不合格的记录写入 `ratings-quarantine.tsv`，带原因，不丢弃。
- **保留有效记录**：最终进入清洗结果的数据。

明确不做的判断：

- 历史归档无法用“今天”衡量新鲜度，Up-to-date 只检查时间戳是否落在声明采集期内。
- 无法验证评分是否真实反映用户偏好，格式正确不等于内容真实。
- 本轮只评估 `ratings.dat`，`users.dat` 与 `movies.dat` 未纳入评分。
- 评分数量异常的活跃用户属于业务现象，不作为质量错误。

## 数据与版本

- `rules/default.json` 是版本化的清洗与评分配置。
- 每次任务记录 `rule_version`、`data_version`、`output_data_version`、`task_id`、`t1`、`t2`。
- `T1`/`T2` 未设置时结果中为 `null`，不会用猜测值填充。
- 执行失败时任务进入 `failed`，错误信息包含失败阶段和命令输出，不生成占位结果。
