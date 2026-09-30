"""Minimal first-iteration web UI for the MovieLens governance Agent."""

from __future__ import annotations

import json
import argparse
import re
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .agent_tools import MovieLensGovernanceAgent, parse_natural_language_request

HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>MovieLens 数据治理</title>
<style>
body{font-family:Arial,sans-serif;max-width:1100px;margin:30px auto;padding:0 18px;background:#f5f7fb;color:#18202a}
textarea{width:100%;min-height:90px;padding:10px}button{padding:9px 16px;margin:8px 0;cursor:pointer}
section{background:white;padding:18px;margin:14px 0;border-radius:8px;box-shadow:0 1px 5px #ccd}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.card{padding:10px;background:#eef3ff}
pre{white-space:pre-wrap;overflow:auto;background:#111827;color:#e5e7eb;padding:12px;border-radius:5px}
.error{color:#b42318}.ok{color:#087443}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd;padding:6px;text-align:left}
</style></head><body>
<h1>MovieLens 1M 数据治理 Agent</h1>
<section><label for="request">自然语言请求</label>
<textarea id="request">请使用默认规则清洗 MovieLens 1M，并评估清洗前后的五维质量。</textarea>
<button id="submit">提交任务</button><p id="status">尚未提交任务</p><div id="progress"></div></section>
<main id="result"></main>
<script>
const $=id=>document.getElementById(id);
let currentTask=null, timer=null;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function score(v){return v==null?'N/A':esc(v);}
function showStatus(data){$("status").innerHTML=`任务 ${esc(data.task_id||'')}：<b>${esc(data.status)}</b>`+
 (data.failed_stage?`，阶段：${esc(data.failed_stage)}`:'')+
 (data.error?`<span class="error">，${esc(data.error)}</span>`:'');}
function render(report){
 if(report.status!=="SUCCESS"){ $("result").innerHTML=`<section class="error"><h2>任务失败</h2><p>失败阶段：${esc(report.failed_stage)}</p><p>${esc(report.error)}</p><p>日志：${esc(report.log_path)}</p></section>`; return; }
 const q=report.quality||{}, pre=q.pre||{}, post=q.post||{}, delta=q.delta||{};
 let rows=(q.dimensions||Object.keys(pre)).map(k=>`<tr><td>${esc(k)}</td><td>${score(pre[k])}</td><td>${score(post[k])}</td><td>${score(delta[k])}</td></tr>`).join('');
 const v=report.data_volume||{}, d=report.disposition||{};
 $("result").innerHTML=`<section><h2>质量评分</h2><table><tr><th>维度</th><th>清洗前</th><th>清洗后</th><th>变化</th></tr>${rows}</table>
 <p>依据：${esc(q.formula||'实际 Hadoop 报告未提供')}</p><p>局限：格式和约束检查不能证明用户属性的现实真实性；时效性仅 ratings 按固定观测截止点前 90 天窗口评价，users/movies 不适用。</p></section>
 <section><h2>数据处置</h2><div class="grid">
 <div class="card">原始/清洗后<br>${esc(v.input_records)} / ${esc(v.cleaned_records)}</div>
 <div class="card">修复<br>${esc(d.repaired)}</div><div class="card">去重<br>${esc(d.deduplicated)}</div>
 <div class="card">隔离<br>${esc(d.quarantined)}</div><div class="card">保留标记<br>${esc(d.flagged)}</div></div>
 <p>数据版本：${esc(report.dataset_version)} → ${esc(report.cleaned_data_version)}<br>规则：${esc(report.rule_version)}<br>T1：${esc(report.T1)}　T2：${esc(report.T2)}</p></section>
 <section><h2>异常记录</h2><p>路径：${esc(report.anomalies)}</p><pre id="anomalies">加载中...</pre></section>
 <section><h2>清洗后数据样例</h2><pre>${esc(report.cleaned_sample||'实际报告未提供样例')}</pre></section>
 <section><h2>报告</h2><p><a href="/api/tasks/${encodeURIComponent(report.task_id)}/report" target="_blank">查看结构化 JSON 报告</a></p>
 <h2>围绕当前 task_id 追问</h2><input id="question" style="width:80%;padding:8px" placeholder="例如：为什么评分发生变化？"><button onclick="ask()">追问</button><pre id="answer"></pre></section>`;
 fetch(`/api/tasks/${encodeURIComponent(report.task_id)}/anomalies`).then(r=>r.json()).then(x=>$("anomalies").textContent=JSON.stringify(x,null,2));
}
async function poll(){
 const r=await fetch(`/api/tasks/${encodeURIComponent(currentTask)}`), d=await r.json(); showStatus(d);
 $("progress").textContent=d.progress||'正在等待实际 Hadoop 任务返回...';
 if(d.status==="SUCCESS"||d.status==="FAILED"){clearInterval(timer); const x=await fetch(`/api/tasks/${encodeURIComponent(currentTask)}/result`); render(await x.json());}
}
$("submit").onclick=async()=>{ $("result").innerHTML=''; const r=await fetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:$("request").value})}); const d=await r.json(); if(!r.ok){showStatus(d);return;} currentTask=d.task_id;showStatus(d);timer=setInterval(poll,1000);poll();};
async function ask(){const q=$("question").value;const r=await fetch(`/api/tasks/${encodeURIComponent(currentTask)}/question`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});$("answer").textContent=JSON.stringify(await r.json(),null,2);}
</script></body></html>"""


class TaskStore:
    def __init__(self, agent: MovieLensGovernanceAgent, input_dir: Path, output_root: Path):
        self.agent, self.input_dir, self.output_root = agent, input_dir, output_root
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.tasks: dict[str, Future[dict[str, Any]]] = {}
        self.lock = threading.Lock()

    def submit(self, text: str) -> str:
        web_id = f"web-{uuid.uuid4().hex}"
        output = self.output_root / web_id
        request = parse_natural_language_request(text, self.input_dir, output)
        future = self.executor.submit(self.agent.start_task, request)
        with self.lock:
            self.tasks[web_id] = future
        return web_id

    def status(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            future = self.tasks.get(task_id)
        if future is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        if not future.done():
            return {"task_id": task_id, "status": "RUNNING", "progress": "Hadoop 流程执行中"}
        try:
            result = future.result()
        except Exception as exc:
            return {"task_id": task_id, "status": "FAILED", "failed_stage": "agent", "error": str(exc)}
        return {"task_id": task_id, "status": result["status"], "progress": "流程已结束",
                "failed_stage": result.get("failed_stage"), "error": result.get("error"),
                "backend_task_id": result.get("backend_task_id"), "log_path": result.get("log_path")}

    def result(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            future = self.tasks.get(task_id)
        if future is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        if not future.done():
            return self.status(task_id)
        result = future.result()
        if result["status"] != "SUCCESS":
            return {"task_id": task_id, "status": "FAILED", "failed_stage": result.get("failed_stage"),
                    "error": result.get("error"), "log_path": result.get("log_path")}
        details = self.agent.get_task_result(result["task_id"])
        report = json.loads(Path(details["report"]).read_text(encoding="utf-8"))
        report["task_id"] = task_id
        report["anomalies"] = details["anomalies"]
        report["log_path"] = details["log_path"]
        sample_path = report.get("cleaned_sample_path")
        if sample_path and Path(sample_path).is_file():
            report["cleaned_sample"] = Path(sample_path).read_text(
                encoding="utf-8", errors="replace"
            )[:10000]
        return report

    def anomalies(self, task_id: str) -> list[dict[str, Any]]:
        report = self.result(task_id)
        path = report.get("anomalies")
        if not path or not Path(path).is_file():
            return []
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()[:100]]

    def question(self, task_id: str, question: str) -> dict[str, Any]:
        if not question.strip():
            return {"task_id": task_id, "error": "问题不能为空"}
        report = self.result(task_id)
        if report.get("status") != "SUCCESS":
            return {"task_id": task_id, "status": report.get("status"), "error": report.get("error")}
        return {"task_id": task_id, "answer": "当前报告已记录评分、处置和局限；请根据报告中的真实字段继续核对。",
                "evidence": {"quality": report.get("quality"), "data_volume": report.get("data_volume"),
                             "disposition": report.get("disposition")}}


def make_handler(store: TaskStore):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload: Any, code: int = 200, content_type: str = "application/json"):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if content_type == "application/json" else payload.encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def _json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict): raise ValueError("JSON body must be an object")
            return value

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self._send(HTML, content_type="text/html")
            match = re.fullmatch(r"/api/tasks/([^/]+)(?:/(result|anomalies|report))?", path)
            if not match:
                return self._send({"error": "Not found"}, 404)
            task_id, suffix = match.group(1), match.group(2)
            try:
                if suffix == "result": value = store.result(task_id)
                elif suffix == "anomalies": value = store.anomalies(task_id)
                elif suffix == "report":
                    value = json.dumps(store.result(task_id), ensure_ascii=False, indent=2)
                    return self._send(value, content_type="text/plain")
                else: value = store.status(task_id)
                return self._send(value)
            except (KeyError, ValueError, OSError) as exc:
                return self._send({"error": str(exc)}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._json()
                if path == "/api/tasks":
                    task_id = store.submit(str(body.get("request", "")))
                    return self._send({"task_id": task_id, "status": "QUEUED"}, 202)
                match = re.fullmatch(r"/api/tasks/([^/]+)/question", path)
                if match:
                    return self._send(store.question(match.group(1), str(body.get("question", ""))))
                return self._send({"error": "Not found"}, 404)
            except (KeyError, ValueError, OSError) as exc:
                return self._send({"status": "FAILED", "error": str(exc)}, 400)

        def log_message(self, *_args):
            return
    return Handler


def serve(input_dir: str | Path, output_root: str | Path, registry: str | Path, host: str = "127.0.0.1", port: int = 8000):
    store = TaskStore(MovieLensGovernanceAgent(registry), Path(input_dir), Path(output_root))
    server = ThreadingHTTPServer((host, port), make_handler(store))
    print(f"MovieLens UI: http://{host}:{port}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MovieLens first-iteration web UI")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    serve(args.input_dir, args.output_root, args.registry, args.host, args.port)


if __name__ == "__main__":
    main()
