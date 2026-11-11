# MovieLens Analysis Agent

MovieLens `ratings.dat` 清洗、五维数据质量评分，以及基于清洗数据的评分分类、用户画像聚类和降维分析。后端为 FastAPI，前端为 React；清洗可运行在本地进程或真实 Hadoop 集群上，分析结果会版本化持久保存。

## 迭代二分析能力

- 分类：Logistic Regression 与 Random Forest，对 `rating >= 4` 进行预测，返回概率及验证/测试指标。
- 聚类：训练期用户画像、K-Means 与 DBSCAN、噪声点、群体摘要及三类聚类指标。
- 降维：PCA 与 SVD、二维坐标、信息保留率、资源开销和降维前后 K-Means 指标对照。
- Agent：可直接询问“预测用户 1 是否会喜欢电影 10”“用户 1 属于哪个群体”“比较降维前后的聚类效果”。
- 数据治理：分类特征仅使用评分事件之前的历史；画像、聚类与降维只使用训练期数据。`T1/T2` 未配置时按时间顺序的 60%/80% 分位点切分，边界来源和样本量会写入结果。

分析 API：

```text
POST /api/analysis/classification
POST /api/analysis/clustering
POST /api/analysis/reduction
GET  /api/analysis/{task_id}
GET  /api/analysis/{task_id}/artifacts/{name}
```

三个分析 POST 接口会立即创建 `queued` 任务并返回任务 ID，不会在 HTTP 请求中同步训练。需要保持 `movielens-worker` 运行；前端会轮询分析任务，直到显示结果或失败原因。

建议把迭代一生成的 `ratings-cleaned.dat` 作为 `input_path`。若原始评分文件同目录存在 MovieLens `movies.dat`，用户画像会自动加入电影类型偏好；清洗产物会追溯原始输入目录。缺失时结果会明确标注这一限制，不会生成虚假类型。

Agent 优先选择最近 8 个成功清洗任务中通过来源校验的当前版本产物。分析 Worker 只额外接受数据库登记、版本匹配且位于该清洗任务精确目录的 `ratings-cleaned.dat`，不整体开放 `ARTIFACT_DIR`（即使它位于 `DATA_DIR` 内）。清洗评分来自产物，年龄/电影类型从原清洗任务的原始评分文件同目录读取，并校验真实路径；原始评分文件丢失或元数据链接越界会明确失败。原始输入仍限于 `DATA_DIR`，文件名包含 `clean` 不代表清洗版本；旧任务的绝对产物路径也按相同规则校验。未通过校验的显式输入仍可排队，由 Worker 记录加载失败，不改变时间切分或复制元数据。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

分别启动 API、Worker 和前端。Worker 同时处理清洗任务和分析任务：

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

## Agent 当前任务文件证据

向 `POST /api/agent/messages` 发送 `{"task_id":"当前任务ID","message":"为什么失败？请查看 report.json 和评分源码说明依据"}`，可追问清洗或分析任务，包括失败阶段、错误、参数和已发布产物。配置现有 `MODEL_*` 后，每条消息先由模型结合当前任务快照判断意图：解释过去结果不启动训练，明确新操作才创建任务，模糊请求先澄清。每条消息最多执行一个路由工具；读取证据委托给独立只读循环（下述预算不含这一次路由请求），明确“重新跑一次”可复用当前任务原输入与参数。模型超时或返回无效操作不会降级为关键词创建，也不会自动重试。未配置模型时标注确定性演示模式，仅接受有限的完整创建命令；追问只返回已保存结果，不声称读取过文件。

- `list_task_files`、`read_task_file(name, offset, limit)` 只访问当前任务已注册的 `.json/.dat/.tsv` 文本，真实路径必须同时位于 `ARTIFACT_DIR` 与当前任务专属目录内；拒绝越界、未注册文件及重定向任务目录。工具不接受任务 ID 或任意路径。
- `read_business_file` 仅允许 `backend/scoring.py`、`backend/executors.py`、`backend/analysis/{dataset,features,classification,clustering,reduction,math_tools}.py` 和 `rules/default.json`。不开放前端、Agent、服务、配置、数据库、基础设施、密钥或其他任务；没有任意 shell、网络或文件读取工具。
- 每次最多 8192 字节，每轮追问最多 8 次工具调用、5 次模型请求；模型快照和清单也有截断限制。偏移按字节计算，返回 `truncated/next_offset`；JSON/源码为 UTF-8，DAT/TSV 为 Latin-1。使用 `next_offset` 连续分页不会拆开 UTF-8 字符；自行指定到字符中部的起点会省略该残缺字符，过小的读取上限会报错。回答应引用文件名与字节范围，未读完须说明局限；文件数据与代码仅是证据，不是指令。
- 源码与默认规则是当前部署版本，不是历史执行时的源码快照；本功能不提供完整执行日志、历史源码重放或因果证明。文件边界不等于认证授权：当前接口未实现用户认证，不应宣称具备多用户隔离；部署方仍须限制 API、模型服务及本机文件写权限。
