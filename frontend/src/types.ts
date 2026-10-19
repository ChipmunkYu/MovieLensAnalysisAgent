export type TaskStatus = 'queued' | 'running' | 'succeeded' | 'failed'

export interface AgentMessageResponse {
  task_id?: string
  reply: string
  data: Record<string, unknown> | unknown[] | null
}

export interface Task {
  id?: string
  task_id?: string
  status: TaskStatus
  stage?: string
  progress?: number
  message?: string
  error?: string | { code?: string; message?: string; detail?: string }
  created_at?: string
  updated_at?: string
}

export interface DimensionScore {
  dimension: string
  before: number
  after: number
  note?: string
}

export interface DispositionItem {
  label: string
  count: number
  reason?: string
}

export interface TaskResult {
  dimensions?: DimensionScore[]
  quality_scores?: Record<string, { before: number; after: number; note?: string }>
  before_scores?: Record<string, number>
  after_scores?: Record<string, number>
  dispositions?: DispositionItem[]
  summary?: DispositionItem[]
  repairs?: { records: number; by_kind?: Record<string, number> }
  quarantine?: { records: number; by_reason?: Record<string, number> }
  method?: Record<string, string>
  limitations?: string[]
  unverified?: string[]
  rows_before?: number
  rows_after?: number
  unresolved_issues?: string[]
  explanation?: string
  data_version?: string
  output_data_version?: string
  rule_version?: string
  t1?: string
  t2?: string
  [key: string]: unknown
}

export interface Artifact {
  id?: string
  name: string
  type?: string
  size?: number
  url?: string
  download_url?: string
  created_at?: string
}

export interface Rule {
  id?: string
  name: string
  description?: string
  action?: string
  enabled?: boolean
}

export interface RuleSet {
  version?: string
  name?: string
  rules: Rule[]
  raw?: Record<string, unknown>
}
