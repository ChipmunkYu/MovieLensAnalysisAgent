"""Read-only evidence tools, bound to one server-selected task."""
from __future__ import annotations

import codecs
from pathlib import Path, PureWindowsPath

from backend.config import ROOT, artifact_dir
from backend.models import AnalysisTask, Task

BUSINESS_FILES = (
    "backend/scoring.py", "backend/executors.py",
    *(f"backend/analysis/{name}.py" for name in
      ("dataset", "features", "classification", "clustering", "reduction", "math_tools")),
    "rules/default.json",
)
ARTIFACT_DESCRIPTIONS = {
    "report.json": "清洗与质量评估报告，包含清洗前后指标、问题处置及方法局限。",
    "ratings-cleaned.dat": "清洗后保留的评分记录，供后续分析使用。",
    "ratings-quarantine.tsv": "被隔离的无效评分记录及隔离原因。",
    "result.json": "当前分析任务的结果，具体结构随分类、聚类或降维任务而异。",
    "user-profiles.json": "聚类分析使用的用户画像及特征。",
}
BUSINESS_DESCRIPTIONS = {
    "backend/scoring.py": "评分记录校验、质量指标统计及五维评分与处置说明。",
    "backend/executors.py": "本地清洗执行、清洗产物与报告生成，以及 Hadoop 执行入口。",
    "backend/analysis/dataset.py": "分析数据加载、电影与用户元数据读取及时间划分信息。",
    "backend/analysis/features.py": "用户画像、评分事件与预测输入的特征构建。",
    "backend/analysis/classification.py": "逻辑回归与决策树分类训练、评估及用户电影偏好预测。",
    "backend/analysis/clustering.py": "KMeans 与 DBSCAN 用户聚类、群体摘要及指定用户归属。",
    "backend/analysis/reduction.py": "PCA 与 SVD 降维、信息保留率及降维前后聚类比较。",
    "backend/analysis/math_tools.py": "标准化、二分类指标、KMeans 与 DBSCAN 算法及聚类指标计算。",
    "rules/default.json": "默认清洗与评分规则，定义字段格式、取值范围、时间窗口、去重策略及评分尺度。",
}
MAX_READ = 8192
MAX_CALLS = 8
MAX_FILES = 64


def _name(value: str) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 255 and value not in (".", "..")
            and not any(c in value for c in ("/", "\\", ":", "\x00"))
            and not PureWindowsPath(value).is_absolute())


class TaskEvidence:
    def __init__(self, task: Task | AnalysisTask):
        self.task = task
        self.calls = 0

    def _artifact(self, name: str) -> Path:
        if not _name(name) or Path(name).suffix.lower() not in (".json", ".dat", ".tsv"):
            raise ValueError("unsupported artifact name or extension")
        item = next((item for item in self.task.artifacts or [] if item.get("name") == name), None)
        if item is None or not _name(self.task.id):
            raise ValueError("artifact is not registered on this task")
        root = artifact_dir().resolve()
        directory = root / "analysis" / self.task.id if isinstance(self.task, AnalysisTask) else root / self.task.id
        # Reject redirected task directories, including links to a sibling task inside root.
        if directory.resolve() != directory:
            raise ValueError("redirected task directory")
        path = Path(item.get("path", "")).resolve(strict=True)
        if (not path.is_relative_to(root) or not path.is_relative_to(directory)
                or path.name != name or not path.is_file()):
            raise ValueError("artifact outside current task directory")
        return path

    def list_task_files(self) -> dict:
        files = []
        for item in (self.task.artifacts or [])[:MAX_FILES]:
            name = item.get("name")
            try:
                path = self._artifact(name)
                files.append({"name": name, "size": path.stat().st_size,
                              "description": ARTIFACT_DESCRIPTIONS.get(name, "当前任务登记的文本产物，具体内容需读取确认。")})
            except (OSError, ValueError, TypeError, RuntimeError):
                continue
        return {"files": files, "truncated": len(self.task.artifacts or []) > MAX_FILES,
                "business_files": [{"name": name, "description": BUSINESS_DESCRIPTIONS[name]} for name in BUSINESS_FILES]}

    def _read(self, path: Path, name: str, offset: int, limit: int, business: bool) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_READ:
            raise ValueError("offset must be nonnegative; limit must be 1..8192 bytes")
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            if offset > size:
                raise ValueError("offset exceeds file size")
            stream.seek(offset)
            raw = stream.read(limit)
        if any(byte < 32 and byte not in (9, 10, 13) or byte == 127 for byte in raw):
            raise ValueError("binary content is not supported")
        encoding = "latin1" if path.suffix.lower() in (".dat", ".tsv") else "utf-8"
        # Arbitrary starting offsets may split a character; generated next_offset never does.
        data = raw
        if encoding == "utf-8" and offset:
            while data and data[0] & 0xC0 == 0x80 and len(raw) - len(data) < 3:
                data = data[1:]
        decoder = codecs.getincrementaldecoder(encoding)()
        content = decoder.decode(data, final=offset + len(raw) >= size)
        end = offset + len(raw) - len(decoder.getstate()[0])
        if end == offset and raw:
            raise ValueError("limit too small for a UTF-8 character")
        return {"name": name, "offset": offset, "end_offset": end, "size": size,
                "encoding": encoding, "content": content, "truncated": end < size,
                "next_offset": end if end < size else None,
                "source": "current_business_source_not_execution_snapshot" if business else "current_task_artifact",
                "range_note": "byte range [offset,end_offset); partial initial UTF-8 character omitted; next_offset is a character boundary"}

    def read_task_file(self, name: str, offset: int = 0, limit: int = MAX_READ) -> dict:
        return self._read(self._artifact(name), name, offset, limit, False)

    def read_business_file(self, name: str, offset: int = 0, limit: int = MAX_READ) -> dict:
        if name not in BUSINESS_FILES:
            raise ValueError("business source not allowed")
        expected = ROOT / name
        if expected.resolve() != expected or not expected.is_file():
            raise ValueError("business source unavailable or redirected")
        return self._read(expected, name, offset, limit, True)

    def call(self, name: str, args: dict) -> dict:
        self.calls += 1
        if self.calls > MAX_CALLS:
            return {"error": "evidence tool budget exhausted"}
        allowed = {"list_task_files": set(), "read_task_file": {"name", "offset", "limit"},
                   "read_business_file": {"name", "offset", "limit"}}
        if name not in allowed or not isinstance(args, dict) or set(args) - allowed[name]:
            return {"error": "unknown tool or arguments; task is bound by server"}
        try:
            return getattr(self, name)(**args)
        except (OSError, ValueError, TypeError, RuntimeError):
            return {"error": "evidence unavailable: invalid name, range, encoding or file boundary"}


def evidence_tools() -> list[dict]:
    tools = []
    for name in ("list_task_files", "read_task_file", "read_business_file"):
        properties = {} if name == "list_task_files" else {
            "name": {"type": "string", **({"enum": list(BUSINESS_FILES)} if name == "read_business_file" else {})},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ},
        }
        tools.append({"type": "function", "function": {"name": name,
                      "description": "只读当前任务证据；源代码是当前版本，不是历史执行快照。",
                      "parameters": {"type": "object", "properties": properties,
                                     "required": [] if not properties else ["name"], "additionalProperties": False}}})
    return tools
