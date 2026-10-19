# Hadoop 运行说明

本目录只包含 Streaming 作业脚本，不包含 Hadoop 安装。服务端通过 `hadoop`、`hdfs` 命令行把作业提交到已有集群。

## 目录

```
hadoop/streaming/
  score_mapper.py       统计五维计数与 (user, movie) 业务键
  score_reducer.py      汇总计数并输出 M 行指标
  clean_mapper.py       输出可用行，键为 (user, movie)
  clean_reducer.py      同一键保留最新时间戳，输出 :: 格式
  quarantine_mapper.py  输出被拒绝的行及原因
  quarantine_reducer.py 原样输出隔离记录
```

`backend/scoring.py` 在选择规则和判定逻辑上被本地执行器与上述脚本共同使用；提交作业时该文件会随 `-file` 一起分发到节点。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEFAULT_EXECUTOR` | `local` | `hadoop` 时默认走集群 |
| `HADOOP_BIN` | `hadoop` | Hadoop 客户端 |
| `HDFS_BIN` | `hdfs` | HDFS 客户端 |
| `HDFS_WORK_DIR` | `/movielens/work` | 每任务工作目录的父目录 |
| `HADOOP_STREAMING_JAR` | 自动查找 | `hadoop-streaming-*.jar` 路径 |
| `HADOOP_STREAMING_SCRIPTS` | `<repo>/hadoop/streaming` | 脚本目录 |
| `STREAMING_PYTHON` | `python3` | worker 节点上的 Python 命令 |
| `HADOOP_JOB_TIMEOUT` | `1800` | 单个作业超时秒数 |
| `HADOOP_JOB_REDUCES` | 未设置 | 需要固定 reducer 数时设置 |

## 提交流程

任务目录结构：

```
/movielens/work/<task_id>/
  raw/         上传的原始 ratings.dat
  before/      清洗前评分输出
  clean/       清洗后数据
  quarantine/  无法修复的记录及原因
  after/       清洗后评分输出
  report.json  结果报告
```

每个作业的命令形如：

```bash
hadoop jar "$HADOOP_STREAMING_JAR" \
  -D mapreduce.job.name=movielens-clean-<task_id> \
  -file scoring.py -file rules.json \
  -file clean_mapper.py -file clean_reducer.py \
  -mapper "python3 clean_mapper.py" \
  -reducer "python3 clean_reducer.py" \
  -input /movielens/work/<task_id>/raw \
  -output /movielens/work/<task_id>/clean
```

`-file` 必须带上 `scoring.py` 和 `rules.json`，否则 reducer 节点上找不到依赖。脚本以 `rules.json` 作为默认规则路径，也可通过第一个参数覆盖。

## 编码与分片

- 原始数据和清洗结果都是 ISO-8859-1，脚本显式使用 `latin-1` 编解码，不依赖节点 locale。
- 输入没有表头，字段用 `::` 分隔。
- 评分作业的中间输出使用 TAB 分隔，这是 Hadoop Streaming 的默认分隔符。
- `clean` 输出会按键的字典序分散到多个 `part-*`，内容正确但行顺序与本地执行器不同；下游按行读取即可。

## 一次手动验证

```bash
# 1. 客户端和集群
hadoop version
hdfs dfs -ls /

# 2. 上传一份小样本
hdfs dfs -mkdir -p /movielens/work/manual/raw
hdfs dfs -put local-ratings.dat /movielens/work/manual/raw/

# 3. 用服务提交（推荐），或手工执行上面的 hadoop jar 命令

# 4. 查看作业与产物
yarn application -list
hdfs dfs -ls -R /movielens/work/manual
hdfs dfs -cat /movielens/work/manual/before/part-00000
```

## 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| `找不到 hadoop-streaming jar` | 未设置 `HADOOP_HOME`，`hadoop classpath` 也不含该 jar | 设置 `HADOOP_STREAMING_JAR` |
| `python3: command not found` | worker 节点没有 Python 3 | 安装 Python 3，或把 `STREAMING_PYTHON` 指向真实路径 |
| `ModuleNotFoundError: scoring` | `-file scoring.py` 缺失或脚本未与依赖一起分发 | 检查提交命令 |
| 作业一直 `ACCEPTED` | YARN 资源不足 | 查看 `yarn application -status <app_id>` 与 NodeManager 日志 |
| 权限错误 | 当前用户对 `HDFS_WORK_DIR` 无写权限 | 修改目录所有者和权限，或换工作目录 |
| 输出目录已存在 | 上一次作业残留 | 驱动会在提交前 `rm -r -f`，手工执行时需自行删除 |

## 本地无法验证的部分

开发机没有 Hadoop，因此以下内容只在模拟集群中验证过命令序列与数据流，**未在真实集群上跑过**：

- 真实 `hadoop jar` 参数解析与 YARN 调度；
- 各节点 Python 版本与 `-file` 分发；
- HDFS 权限与路径配置；
- 大文件下的执行时间与 reducer 数量。

第一次在真实集群运行时，建议先用 `data/sample/ratings.dat` 提交一次，确认 `before/`、`clean/`、`quarantine/`、`after/` 四个目录都有 `part-*` 输出后再换全量数据。
