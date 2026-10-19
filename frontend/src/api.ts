import type { AgentMessageResponse, Artifact, RuleSet, Task, TaskResult } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })

  if (!response.ok) {
    let detail = ''
    try {
      const body = await response.json() as { detail?: string; message?: string }
      detail = body.detail ?? body.message ?? ''
    } catch { /* 接口可能返回空响应或纯文本。 */ }
    throw new Error(detail || `请求失败（HTTP ${response.status}）`)
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  sendMessage: (message: string, taskId?: string) => request<AgentMessageResponse>('/api/agent/messages', {
    method: 'POST', body: JSON.stringify({ message, ...(taskId ? { task_id: taskId } : {}) }),
  }),
  createTask: (payload?: unknown) => request<Task>('/api/tasks', {
    method: 'POST', body: JSON.stringify(payload ?? {}),
  }),
  getTask: (id: string) => request<Task>(`/api/tasks/${encodeURIComponent(id)}`),
  getResult: (id: string) => request<TaskResult>(`/api/tasks/${encodeURIComponent(id)}/result`),
  getArtifacts: async (id: string) => {
    const data = await request<Artifact[] | { artifacts: Artifact[] }>(`/api/tasks/${encodeURIComponent(id)}/artifacts`)
    return Array.isArray(data) ? data : data.artifacts
  },
  getDefaultRules: async () => {
    const data = await request<RuleSet | RuleSet['rules']>('/api/rules/default')
    return Array.isArray(data) ? { rules: data } : data
  },
  resolveUrl: (url: string) => /^https?:\/\//.test(url) ? url : `${API_BASE}${url.startsWith('/') ? '' : '/'}${url}`,
}
