# MovieLens 1M 数据治理 Agent（第一轮）

## 启动方案

### 1. 启动简易前端

在项目根目录执行：

```bash
python3 \
  -m movielens_quality.web_app \
  --input-dir "ml-1m/ml-1m" \
  --output-root "web_outputs" \
  --registry "web_task_registry.json" \
  --port 8000
```

浏览器打开：

```text
http://127.0.0.1:8000
```

前端提交自然语言请求后，会创建任务并轮询任务状态。当前环境尚未安装或配置 Hadoop，因此真实提交会返回 `FAILED`，并显示 `hadoop_preflight` 阶段及具体错误；系统不会生成伪造的成功结果。

### 2. 运行单元测试

```bash
python3 \
  -m unittest discover -s tests -v
```

### 3. 直接运行数据检查

```bash
python3 \
  -m movielens_quality.quality_check \
  --input-dir "ml-1m/ml-1m" \
  --output-dir "quality_report"
```

### 4. 直接运行 Hadoop 流程

```bash
python3 \
  -m movielens_quality.hadoop_pipeline \
  --input-dir "ml-1m/ml-1m" \
  --output-dir "hadoop_quality_report"
```

该命令只有在 Hadoop 已安装、环境变量已配置、且三个 Hadoop 阶段命令已登记后，才会生成成功报告。

## 项目范围

本项目当前实现第一轮功能：

- MovieLens 1M 原始数据解析；
- ISO-8859-1 编码读取；
- `::` 分隔符解析；
- 不将第一行当作表头；
- 字段数量、空值、类型、范围检查；
- 重复记录、冲突键和跨表引用检查；
- 异常记录及异常原因输出；
- Hadoop 清洗流程编排；
- 清洗前后五维质量评分；
- Agent 工具接口；
- 简易前端和任务状态查询。

不包含第二轮分类、聚类、降维，也不包含第三轮知识图谱功能。

## 目录结构

```text
project-root/
├─ README.md
├─ 引言_项目总体要求与汇报安排.md
├─ 迭代一_Hadoop数据清洗与Agent基础.md
├─ ml-1m\
│  └─ ml-1m\
│     ├─ README
│     ├─ movies.dat
│     ├─ ratings.dat
│     └─ users.dat
├─ movielens_quality\
│  ├─ __init__.py
│  ├─ quality_check.py
│  ├─ hadoop_pipeline.py
│  ├─ agent_tools.py
│  └─ web_app.py
└─ tests\
   ├─ test_quality_check.py
   ├─ test_hadoop_pipeline.py
   ├─ test_agent_tools.py
   └─ test_web_app.py
```

## 模块说明

### `quality_check.py`

负责读取和检查三个 `.dat` 文件，并生成：

- `stats.json`：结构化统计结果；
- `anomalies.jsonl`：逐条异常记录及原因。

原始数据目录只读使用，不会被覆盖。

### `hadoop_pipeline.py`

负责严格编排：

```text
Hadoop 预检查
    -> 清洗前质量评估
    -> Hadoop 清洗
    -> 清洗后质量评估
    -> 结构化报告
```

Hadoop 不可用、阶段命令缺失、作业失败或结果文件缺失时，流程返回 `failed`，并记录失败阶段和错误原因。

### `agent_tools.py`

提供 Agent 可调用能力：

- 自然语言请求解析；
- 数据版本、规则版本和评分版本校验；
- 任务 ID 注册；
- `QUEUED`、`RUNNING`、`SUCCESS`、`FAILED` 状态；
- 任务状态查询；
- 评分、异常记录和报告查询。

### `web_app.py`

使用 Python 标准库 HTTP 服务提供简易前端，支持：

- 自然语言输入；
- 自动提交任务；
- 状态轮询；
- 五维分数展示；
- 数据量和处置统计；
- 异常记录和清洗后样例展示；
- 报告查看；
- 基于当前 task_id 追问。

## 五维质量评分

清洗前后使用同一套公式：

```text
Accurate   = 100 × accurate_fields_passed / accurate_fields_checked
Complete   = 100 × complete_records / total_records
Unique     = 100 × (eligible_records - exact_duplicate_records - conflict_records) / eligible_records
Up-to-date（仅 ratings）= 100 × up_to_date_records / total_records
Consistent = 100 × consistent_records / total_records
```

Accurate 按存在且应检查的字段逐项计数：ratings 检查正整数 ID、1–5 整数评分及 `[946684800, 1046476799]` 内的 Unix 秒时间戳；users 检查 ID、Gender、Age、Occupation 的官方编码；movies 检查 ID、官方 18 类 Genres，并仅在 Title 存在末尾括号年份候选时检查 4 位年份及 `1888..2003` 范围。毫秒时间戳判失败但不修复。

Unique v1.1 中，`eligible_records` 是成功解析且身份键完整的记录；users、movies、ratings 的身份键分别为 `UserID`、`MovieID`、`(UserID, MovieID, Timestamp)`。同键整组内容完全相同时保留首条、其余计入 `exact_duplicate_records`；同键存在不同完整内容时，整组所有行计入 `conflict_records`，且该组不再计 exact。`duplicate_records` 仅作为兼容字段，等于两者之和。`eligible_records=0` 时 Unique 为 N/A。冲突会影响 Consistent 的统计生产规则，评分器不会在 Consistent 中再次扣除冲突。

Up-to-date 使用固定 reference `1046476799`，90 天窗口起点为 `1038700799`，按 `1038700799 <= timestamp <= 1046476799`（两端 inclusive）计数。分母始终是 ratings 的物理记录总数；只要时间戳是该窗口内的合法 Unix 秒，即使同一 rating 的其他字段失败，也计入 `up_to_date_records`。users 和 movies 不适用，表级和数据集无适用表时均为 `N/A`（JSON `null`）。数据集级 Up-to-date 仅平均适用且非空的表；其他维度继续对非空表宏平均。清洗前后的 Hadoop 统计产物必须提供 `ratings.up_to_date_records`，并使用相同固定 reference 和窗口。

Consistent 按物理记录计数：三个文件均要求统一结构和字段类型；同一 UserID、MovieID 不能对应冲突内容；Genres 必须使用官方词表且同一电影内不得重复；rating 必须使用秒级时间戳、引用已存在的用户和电影，并且同一 `(UserID, MovieID, Timestamp)` 不能对应不同评分。完全相同的重复记录只影响 Unique，不影响 Consistent；冲突键涉及的全部记录均判为不一致。

其中，`total_records` 是**单表范围内进入该次评分的物理记录行数**。`ratings`、`users`、`movies` 分别使用各自的 `total_records` 计算适用维度分数；数据集级的每个维度只对有记录且该维度适用的表做等权算术平均（宏平均），空表不参与。数据集级评分不会把三表记录合并后用总行数计算微平均，因此记录量较大的 `ratings` 不会淹没其他表的质量表现。没有适用且非空表的维度为 `N/A`（JSON `null`）。

评分结果必须由 Hadoop 阶段生成的统计结果计算。Hadoop 未成功执行时不返回成功评分。

评分局限包括：

- 当前各维度分子的完整业务规则仍有未实现项，现有公式和统计字段不代表五维质量评分已完整实现；
- 格式和业务约束通过不等于现实世界事实真实；
- 用户人口属性来自数据集填写信息，不能仅凭格式证明准确；
- MovieLens 1M 是历史数据，时效性仅表示评分时间戳是否落在上述固定历史 reference 前 90 天，不表示数据在当前日期仍然新鲜；
- 删除或隔离记录可能改善主数据集统计值，但不等于问题被事实修复。

## 数据处置类型

系统区分以下四种处置结果：

- **修复**：可无歧义规范化后继续使用；
- **去重**：重复记录合并或只保留一条；
- **隔离**：无法安全修复，移出主数据集并保留原因；
- **保留标记**：无法证明错误，但保留记录并增加质量标记。

报告中的数据量变化必须分别记录这些类别，不能把隔离或删除直接称为“问题已修复”。

## 版本与任务产物

默认版本：

```text
清洗规则版本：ml1m-cleaning-v1.0
评分配置版本：ml1m-quality-v1.2
```

任务报告应包含：

- `task_id`；
- 任务状态；
- 原始数据版本；
- 清洗后数据版本；
- 清洗规则版本；
- 评分配置版本；
- `T1`、`T2`；
- 清洗前后五维评分；
- 数据量变化；
- 异常处置结果；
- Hadoop 阶段和报告路径。

## Hadoop 配置要求

当前检查结果显示以下命令不可用：

```text
hadoop
hdfs
mapred
yarn
```

正式运行前需要完成：

1. 安装 Hadoop；
2. 配置 `JAVA_HOME`；
3. 配置 `HADOOP_HOME`；
4. 配置 `HADOOP_CONF_DIR`；
5. 验证 Hadoop 单机或伪分布式运行；
6. 登记清洗前评估、清洗和清洗后评估三个实际 Hadoop 作业命令；
7. 确保作业输出 `pre_quality.json`、清洗产物和 `post_quality.json`。

## 测试状态

当前测试覆盖解析器、Hadoop 流程、Agent 工具和前端任务状态。

最近一次结果：

```text
Ran 10 tests
OK
```

测试中的 Hadoop 失败场景使用显式失败断言；不使用模拟成功分数替代真实 Hadoop 结果。

## 当前限制

1. Hadoop 尚未在当前环境安装或配置。
2. 因此尚未产生真实的 Hadoop 清洗成功报告。
3. 前端已具备失败状态展示，但成功页面需要 Hadoop 实际产出完整报告后才能展示真实评分、处置统计和清洗数据样例。
4. 当前 Agent 追问接口返回报告证据摘要，不会在缺少证据时推测结论。
