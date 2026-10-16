import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import type { Artifact, DimensionScore, DispositionItem, Rule, RuleSet, Task, TaskResult } from './types'

const DEFAULT_PROMPT = '请使用默认规则清洗 MovieLens 1M，评估清洗前后的五个数据质量维度，并说明处理了哪些问题。'
const TERMINAL = new Set(['succeeded', 'completed', 'failed', 'cancelled'])
const DIMENSIONS: Record<string, [string, string]> = {
  accurate: ['准确性', 'Accurate'], completeness: ['完整性', 'Complete'], complete: ['完整性', 'Complete'],
  unique: ['唯一性', 'Unique'], uniqueness: ['唯一性', 'Unique'], up_to_date: ['时效性', 'Up-to-date'],
  timeliness: ['时效性', 'Up-to-date'], consistent: ['一致性', 'Consistent'], consistency: ['一致性', 'Consistent'],
}
type IconName = 'send' | 'file' | 'folder' | 'chart' | 'chevron' | 'panel' | 'close' | 'spark' | 'alert' | 'report' | 'rule'
type Selection = { type: 'scores' } | { type: 'report' } | { type: 'rule'; rule: Rule }

function Icon({ name }: { name: IconName }) {
  const paths: Record<IconName, React.ReactNode> = {
    send: <><path d="M4 12 20 4l-5 16-3-6-8-2Z" /><path d="m12 14 8-10" /></>,
    file: <><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v5h5" /></>,
    folder: <path d="M3 7h7l2 2h9v10H3z" />,
    chart: <><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>,
    chevron: <path d="m9 6 6 6-6 6" />,
    panel: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M15 4v16" /></>,
    close: <path d="m6 6 12 12M18 6 6 18" />,
    spark: <path d="m12 3 1.4 4.1L17 9l-3.6 1.9L12 15l-1.4-4.1L7 9l3.6-1.9L12 3ZM5 16l.7 2.3L8 19l-2.3.7L5 22l-.7-2.3L2 19l2.3-.7L5 16Z" />,
    alert: <><path d="M12 3 2.7 20h18.6z" /><path d="M12 9v4M12 17h.01" /></>,
    report: <><path d="M5 3h14v18H5z" /><path d="M8 8h8M8 12h8M8 16h5" /></>,
    rule: <><path d="M5 4h14v16H5z" /><path d="m8 9 1.5 1.5L12 8M8 15h8" /></>,
  }
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">{paths[name]}</svg>
}

function taskIdOf(value: { id?: string; task_id?: string }) { return value.task_id ?? value.id }
function errorText(error: Task['error']) {
  if (!error) return ''
  return typeof error === 'string' ? error : error.message ?? error.detail ?? error.code ?? '任务执行失败'
}
function statusText(status?: string) {
  return ({ queued: '等待调度', pending: '准备中', running: '正在处理', succeeded: '任务已完成', completed: '任务已完成', failed: '执行失败', cancelled: '已取消' } as Record<string, string>)[status ?? ''] ?? '正在创建任务'
}
function clampScore(score: number) { return score <= 1 ? score * 100 : score }
function scoresOf(result?: TaskResult): DimensionScore[] {
  if (!result) return []
  if (result.dimensions) return result.dimensions
  if (result.quality_scores) return Object.entries(result.quality_scores).map(([dimension, score]) => ({ dimension, ...score }))
  const keys = new Set([...Object.keys(result.before_scores ?? {}), ...Object.keys(result.after_scores ?? {})])
  return [...keys].map(dimension => ({ dimension, before: result.before_scores?.[dimension] ?? 0, after: result.after_scores?.[dimension] ?? 0 }))
}
function dispositionsOf(result?: TaskResult): DispositionItem[] { return result?.dispositions ?? result?.summary ?? [] }
function formatCount(value?: number) { return value == null ? '未提供' : new Intl.NumberFormat('zh-CN').format(value) }
function formatBytes(value?: number) {
  if (value == null) return '大小未知'
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 ** 2).toFixed(1)} MB`
}

export default function App() {
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT)
  const [lastPrompt, setLastPrompt] = useState('')
  const [agentReply, setAgentReply] = useState('')
  const [task, setTask] = useState<Task>()
  const [result, setResult] = useState<TaskResult>()
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [rules, setRules] = useState<RuleSet>()
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [panelOpen, setPanelOpen] = useState(true)
  const [expanded, setExpanded] = useState(() => new Set(['task', 'charts', 'files', 'rules']))
  const [selection, setSelection] = useState<Selection>()
  const streamEnd = useRef<HTMLDivElement>(null)
  const taskId = task ? taskIdOf(task) : undefined
  const scores = useMemo(() => scoresOf(result), [result])
  const dispositions = useMemo(() => dispositionsOf(result), [result])
  const running = submitting || !!task && !TERMINAL.has(task.status)
  const failed = task?.status === 'failed'
  const progress = Math.max(0, Math.min(100, task?.progress == null ? (task?.status === 'succeeded' ? 100 : 0) : task.progress <= 1 ? task.progress * 100 : task.progress))

  useEffect(() => { api.getDefaultRules().then(setRules).catch(() => {}) }, [])
  useEffect(() => { streamEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [task?.status, result, error])
  useEffect(() => {
    function closeOnEscape(event: globalThis.KeyboardEvent) { if (event.key === 'Escape') setPanelOpen(false) }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [])
  useEffect(() => {
    if (!taskId || !task || TERMINAL.has(task.status)) return
    const timer = window.setInterval(async () => {
      try {
        const next = await api.getTask(taskId)
        setTask(next)
        if (next.status === 'succeeded') {
          const [nextResult, nextArtifacts] = await Promise.all([api.getResult(taskId), api.getArtifacts(taskId)])
          setResult(nextResult); setArtifacts(nextArtifacts); window.clearInterval(timer)
        } else if (next.status === 'failed') window.clearInterval(timer)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : '无法获取任务状态'); window.clearInterval(timer)
      }
    }, 2000)
    return () => window.clearInterval(timer)
  }, [taskId, task?.status])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const message = prompt.trim()
    if (!message || running) return
    setSubmitting(true); setError(''); setTask(undefined); setResult(undefined); setArtifacts([]); setSelection(undefined); setLastPrompt(message); setAgentReply('')
    try {
      const agent = await api.sendMessage(message)
      setAgentReply(agent.reply)
      const id = taskIdOf(agent)
      setTask(id ? await api.getTask(id) : await api.createTask())
    } catch (cause) { setError(cause instanceof Error ? cause.message : '任务提交失败，请稍后重试') }
    finally { setSubmitting(false) }
  }
  function submitOnShortcut(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit() }
  }
  function toggle(node: string) {
    setExpanded(current => { const next = new Set(current); next.has(node) ? next.delete(node) : next.add(node); return next })
  }
  function select(next: Selection) { setSelection(next); setPanelOpen(true) }
  const hasReportResources = !!result && (!!result.explanation || dispositions.length > 0 || !!rules?.rules?.length)

  return <div className={`workspace ${panelOpen ? 'panel-open' : 'panel-closed'}`}>
    <header className="topbar">
      <div className="brand"><span className="brand-mark">M</span><div><b>MovieLens</b><span>质量工作台</span></div></div>
      <div className="top-context"><span className={`status-light ${failed ? 'bad' : running ? 'busy' : ''}`} />{running ? '任务处理中' : failed ? '任务异常' : '数据治理助手'}</div>
      <button className="icon-button panel-button" type="button" onClick={() => setPanelOpen(value => !value)} aria-expanded={panelOpen} aria-controls="resource-panel" aria-label={panelOpen ? '收起资源面板' : '展开资源面板'}><Icon name="panel" /></button>
    </header>

    <main className="conversation" aria-label="任务对话工作区">
      <div className="chat-stream">
        <section className="welcome assistant-message">
          <div className="avatar"><Icon name="spark" /></div>
          <div className="message-body"><p className="message-label">治理助手</p><h1>从一句清晰的要求开始。</h1><p>我会执行 MovieLens 数据清洗，跟踪任务进度，并将图表、规则与结果文件整理到右侧资源面板。</p></div>
        </section>

        {lastPrompt && <section className="user-message" aria-label="你的请求"><div>{lastPrompt}</div></section>}

        {(submitting || task || error) && <section className="assistant-message task-message" aria-live="polite">
          <div className="avatar"><Icon name={error || failed ? 'alert' : 'spark'} /></div>
          <div className="message-body">
            <p className="message-label">治理助手</p>
            {error ? <div className="error-message"><strong>暂时无法继续</strong><p>{error}</p><button type="button" onClick={() => setError('')}>关闭提示</button></div> : failed ? <div className="error-message"><strong>任务执行失败</strong><p>{errorText(task?.error) || '任务未生成可用结果，请检查服务后重试。'}</p></div> : <>
              <div className="status-line"><strong>{statusText(task?.status)}</strong>{taskId && <code title={taskId}>#{taskId.slice(0, 8)}</code>}</div>
              {agentReply && <p>{agentReply}</p>}
              {!result && <><p className="stage-copy">{task?.stage ?? (submitting ? '正在解析你的请求' : '等待后端返回当前阶段')} · {Math.round(progress)}%</p><div className="progress-track" aria-label={`任务进度 ${Math.round(progress)}%`}><span style={{ width: `${progress}%` }} /></div></>}
            </>}
          </div>
        </section>}

        {result && <section className="assistant-message result-message">
          <div className="avatar"><Icon name="spark" /></div>
          <div className="message-body">
            <p className="message-label">治理助手 · 结果摘要</p>
            <h2>清洗与质量评估已完成</h2>
            <p>记录数从 <b>{formatCount(result.rows_before)}</b> 变为 <b>{formatCount(result.rows_after)}</b>{dispositions.length ? `，生成 ${dispositions.length} 项处置记录。` : '。'}</p>
            {scores.length ? <div className="delta-summary" aria-label="五维评分变化摘要">{scores.map(score => { const names = DIMENSIONS[score.dimension.toLowerCase()] ?? [score.dimension, 'Quality']; const delta = clampScore(score.after) - clampScore(score.before); return <span key={score.dimension}><b>{names[0]}</b><i className={delta < 0 ? 'negative' : ''}>{delta > 0 ? '+' : ''}{delta.toFixed(1)}</i></span> })}</div> : <p className="muted-copy">接口未返回五维评分。</p>}
            {result.explanation && <p className="explanation">{result.explanation}</p>}
            <button className="text-button" type="button" onClick={() => select({ type: 'scores' })} disabled={!scores.length}>在资源面板查看详细对比 <Icon name="chevron" /></button>
          </div>
        </section>}
        <div ref={streamEnd} />
      </div>

      <div className="composer-wrap">
        <form className="composer" onSubmit={submit}>
          <label className="sr-only" htmlFor="prompt">描述数据治理任务</label>
          <textarea id="prompt" value={prompt} onChange={event => setPrompt(event.target.value)} onKeyDown={submitOnShortcut} maxLength={1000} rows={2} disabled={running} placeholder="描述你要完成的数据治理工作…" />
          <button type="submit" disabled={!prompt.trim() || running} aria-label={running ? '任务处理中' : '发送任务'}>{running ? <span className="spinner" /> : <Icon name="send" />}</button>
          <span className="composer-hint">Enter 发送 · Shift + Enter 换行</span>
        </form>
      </div>
    </main>

    {panelOpen && <button className="drawer-backdrop" type="button" onClick={() => setPanelOpen(false)} aria-label="关闭资源面板" />}
    {panelOpen && <aside className="resource-panel" id="resource-panel" aria-label="任务资源面板">
      <div className="panel-head"><div><span>RESOURCE INDEX</span><h2>任务资源</h2></div><button className="icon-button close-panel" type="button" onClick={() => setPanelOpen(false)} aria-label="关闭资源面板"><Icon name="close" /></button></div>
      <div className="resource-tree" role="tree" aria-label="本次任务资源树">
        {!task ? <div className="resource-empty"><span><Icon name="folder" /></span><strong>尚无任务资源</strong><p>任务完成后，这里会按图表、结果文件和规则建立资源索引。</p></div> : <>
          <TreeFolder label="本次任务" node="task" level={1} open={expanded.has('task')} onToggle={toggle} />
          {expanded.has('task') && <div role="group">
            {scores.length > 0 && <><TreeFolder label="图表" node="charts" level={2} open={expanded.has('charts')} onToggle={toggle} />{expanded.has('charts') && <TreeItem label="清洗前后五维对比" icon="chart" level={3} active={selection?.type === 'scores'} onClick={() => select({ type: 'scores' })} />}</>}
            {artifacts.length > 0 && <><TreeFolder label="结果文件" node="files" level={2} open={expanded.has('files')} onToggle={toggle} />{expanded.has('files') && artifacts.map((artifact, index) => { const href = artifact.download_url ?? artifact.url; return <a className="tree-row tree-link" role="treeitem" aria-level={3} style={{ '--level': 3 } as React.CSSProperties} key={artifact.id ?? `${artifact.name}-${index}`} href={href ? api.resolveUrl(href) : undefined} target={href ? '_blank' : undefined} rel="noreferrer" aria-disabled={!href} title={artifact.name}><span className="tree-spacer" /><Icon name="file" /><span className="tree-name">{artifact.name}</span></a> })}</>}
            {hasReportResources && <><TreeFolder label="规则与报告" node="rules" level={2} open={expanded.has('rules')} onToggle={toggle} />{expanded.has('rules') && <div role="group">
              {(result.explanation || dispositions.length > 0) && <TreeItem label="处置摘要与结果说明" icon="report" level={3} active={selection?.type === 'report'} onClick={() => select({ type: 'report' })} />}
              {rules?.rules?.map((rule, index) => <TreeItem key={rule.id ?? `${rule.name}-${index}`} label={rule.name} icon="rule" level={3} active={selection?.type === 'rule' && selection.rule === rule} onClick={() => select({ type: 'rule', rule })} />)}
            </div>}</>}
          </div>}
        </>}
      </div>
      {selection && result && <ResourceDetail selection={selection} scores={scores} result={result} dispositions={dispositions} rules={rules} />}
    </aside>}
    {!panelOpen && <button className="panel-rail" type="button" onClick={() => setPanelOpen(true)} aria-label="展开资源面板"><Icon name="panel" /><span>资源</span></button>}
  </div>
}

function TreeFolder({ label, node, level, open, onToggle }: { label: string; node: string; level: number; open: boolean; onToggle: (node: string) => void }) {
  return <button className="tree-row tree-folder" type="button" role="treeitem" aria-level={level} aria-expanded={open} style={{ '--level': level } as React.CSSProperties} onClick={() => onToggle(node)} title={label}><Icon name="chevron" /><Icon name="folder" /><span className="tree-name">{label}</span></button>
}
function TreeItem({ label, icon, level, active, onClick }: { label: string; icon: IconName; level: number; active: boolean; onClick: () => void }) {
  return <button className={`tree-row tree-item ${active ? 'active' : ''}`} type="button" role="treeitem" aria-level={level} aria-selected={active} style={{ '--level': level } as React.CSSProperties} onClick={onClick} title={label}><span className="tree-spacer" /><Icon name={icon} /><span className="tree-name">{label}</span></button>
}
function ResourceDetail({ selection, scores, result, dispositions, rules }: { selection: Selection; scores: DimensionScore[]; result: TaskResult; dispositions: DispositionItem[]; rules?: RuleSet }) {
  if (selection.type === 'scores') return <section className="resource-detail" aria-label="清洗前后五维对比详情"><DetailHead eyebrow="CHART" title="清洗前后五维对比" /><div className="score-legend"><span>清洗前</span><b>清洗后</b></div>{scores.map(score => { const before = clampScore(score.before), after = clampScore(score.after); const names = DIMENSIONS[score.dimension.toLowerCase()] ?? [score.dimension, 'Quality']; return <div className="score-detail" key={score.dimension}><div><b>{names[0]}</b><span>{before.toFixed(1)} → <strong>{after.toFixed(1)}</strong></span></div><div className="bar-compare" title={`清洗前 ${before.toFixed(1)}，清洗后 ${after.toFixed(1)}`}><i style={{ width: `${Math.min(100, before)}%` }} /><b style={{ width: `${Math.min(100, after)}%` }} /></div>{score.note && <small>{score.note}</small>}</div> })}</section>
  if (selection.type === 'rule') return <section className="resource-detail" aria-label={`${selection.rule.name}规则详情`}><DetailHead eyebrow="RULE" title={selection.rule.name} /><p>{selection.rule.description ?? selection.rule.action ?? '接口未提供规则说明。'}</p><div className="meta-pills"><span>{selection.rule.enabled === false ? '已停用' : '已启用'}</span>{rules?.version && <span>版本 {rules.version}</span>}</div></section>
  return <section className="resource-detail" aria-label="处置摘要与结果说明"><DetailHead eyebrow="REPORT" title="处置摘要与结果说明" /><div className="record-change"><span>处理前 <b>{formatCount(result.rows_before)}</b></span><Icon name="chevron" /><span>处理后 <b>{formatCount(result.rows_after)}</b></span></div>{dispositions.length > 0 && <ul>{dispositions.map((item, index) => <li key={`${item.label}-${index}`}><span><b>{item.label}</b>{item.reason && <small>{item.reason}</small>}</span><strong>{formatCount(item.count)}</strong></li>)}</ul>}<p>{result.explanation ?? '本次任务未附加结果说明。'}</p>{result.unresolved_issues?.length ? <><h4>仍需关注</h4><ul className="issues">{result.unresolved_issues.map(issue => <li key={issue}>{issue}</li>)}</ul></> : null}</section>
}
function DetailHead({ eyebrow, title }: { eyebrow: string; title: string }) { return <header><span>{eyebrow}</span><h3>{title}</h3></header> }
