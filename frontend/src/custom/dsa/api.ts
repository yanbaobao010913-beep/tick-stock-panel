// DSA 迁移扩展 API 客户端 — 契约见 docs/dsa-migration/20260929-migration-plan.md
// fetch 封装与 lib/api.ts 同款: 超时 + toast + detail 解析 (lib/api.ts 的 request 未导出, 扩展自带一份)

import { toast } from '@/components/Toast'

const PREFIX = '/api/ext/dsa'
const DEFAULT_TIMEOUT_MS = 30_000

type RequestOptions = RequestInit & { quiet?: boolean; timeoutMs?: number | null }

async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const { quiet, timeoutMs = DEFAULT_TIMEOUT_MS, ...fetchInit } = init ?? {}
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  Object.assign(headers, fetchInit.headers as Record<string, string> | undefined)
  const ctl = timeoutMs == null || fetchInit.signal ? undefined : new AbortController()
  const timeoutSeconds = Math.round((timeoutMs ?? DEFAULT_TIMEOUT_MS) / 1000)
  let timer: number | undefined
  if (ctl && timeoutMs != null) timer = window.setTimeout(() => ctl.abort(), timeoutMs)
  let res: Response
  try {
    res = await fetch(path, { ...fetchInit, headers, ...(ctl ? { signal: ctl.signal } : {}) })
  } catch (err) {
    if (ctl && err instanceof DOMException && err.name === 'AbortError') {
      const msg = `请求超时（${timeoutSeconds}s）· ${path.split('?')[0]}`
      if (!quiet) toast(msg, 'error')
      throw new Error(msg)
    }
    throw err
  } finally {
    if (timer !== undefined) window.clearTimeout(timer)
  }
  if (!res.ok) {
    let detail = ''
    try {
      const j = JSON.parse(await res.text())
      const raw = j.detail ?? j.message ?? ''
      detail = typeof raw === 'string' ? raw : JSON.stringify(raw)
    } catch { /* ignore */ }
    const msg = detail || `${res.status} ${res.statusText}`
    if (res.status !== 401 && !quiet) toast(msg, 'error')
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

// ===== 类型 (契约 §2) =====

export interface ReportPoints {
  ideal_buy?: number | null
  secondary_buy?: number | null
  stop_loss?: number | null
  take_profit?: number | null
}

export interface RiskCondition {
  kind?: string
  text: string
  price?: number | null
}

export interface PhaseDecision {
  action_window?: string | null
  immediate_action?: string | null
  next_check_time?: string | null
  watch_conditions?: string[]
  risk_conditions?: RiskCondition[]
}

export interface ReportSummary {
  id: string
  symbol: string
  name?: string | null
  created_at: string
  mode?: string
  sentiment_score?: number | null
  operation_advice?: string | null
  trend_prediction?: string | null
  analysis_summary?: string | null
  points?: ReportPoints | null
}

export interface ReportDetail extends ReportSummary {
  phase_decision?: PhaseDecision | null
  markdown: string
}

export interface AnalysisTaskItem {
  symbol: string
  status: 'pending' | 'running' | 'done' | 'failed'
  report_id?: string
  error?: string
}

export interface AnalysisTask {
  task_id: string
  status: 'running' | 'done' | 'failed'
  total: number
  done: number
  items: AnalysisTaskItem[]
}

export interface LegacyReportSummary {
  id: string
  symbol: string
  created_at: string
  operation_advice?: string | null
  sentiment_score?: number | null
  ideal_buy?: number | null
  secondary_buy?: number | null
  stop_loss?: number | null
  take_profit?: number | null
  analysis_summary?: string | null
}

export interface LegacyReportDetail extends LegacyReportSummary {
  markdown: string
}

// ===== P5 点位模拟 (契约 §4.6) =====

export interface PaperSummary {
  total_return_pct?: number | null
  max_drawdown_pct?: number | null
  win_rate?: number | null
  closed: number
  open: number
  skipped: number
}

export interface PaperEquityPoint {
  date: string
  equity: number
  benchmark?: number | null
}

export interface PaperOverview {
  state: { inception_date: string; initial_capital: number; max_slots: number }
  summary: PaperSummary
  equity: PaperEquityPoint[]
}

export interface PaperTrade {
  symbol: string
  name?: string | null
  entry_date: string
  entry_price: number
  shares: number
  exit_date?: string | null
  exit_price?: number | null
  exit_reason?: string | null
  return_pct?: number | null
  status: 'open' | 'closed' | 'skipped'
}

export type OutcomeValue = 'hit' | 'miss' | 'neutral' | 'unable'

export interface PaperOutcome {
  report_id: string
  symbol: string
  created_at: string
  direction: string
  horizon: number
  outcome: OutcomeValue
  exit_reason?: string | null
  return_pct?: number | null
}

export interface PaperOutcomes {
  items: PaperOutcome[]
  stats: {
    hit_rate?: number | null
    n: number
    by_horizon?: Record<string, { hit_rate?: number | null; n: number }>
  }
}

// ===== P2 次日盯盘 (契约 §4) =====

export type WatchRuleKind =
  | 'stop_loss' | 'take_profit' | 'add' | 'reduce' | 'entry'
  | `near_${'stop_loss' | 'take_profit' | 'add' | 'reduce'}`
  | `mid_${'stop_loss' | 'take_profit' | 'add' | 'reduce'}`

export interface WatchRule {
  rule_id: string
  kind: WatchRuleKind
  price: number
  severity: 'info' | 'warn' | 'critical'
  enabled: boolean
}

export type WatchSyncState = 'synced' | 'stale' | 'no_points' | 'no_report'

export interface WatchPlanItem {
  symbol: string
  name?: string | null
  holding: boolean
  quantity?: number | null
  avg_cost?: number | null
  report?: {
    id: string
    created_at: string
    operation_advice?: string | null
    sentiment_score?: number | null
    points?: ReportPoints | null
    phase_decision?: PhaseDecision | null
  } | null
  rules: WatchRule[]
  sync_state: WatchSyncState
}

export interface WatchPlan {
  generated_at: string
  positions_source: string
  items: WatchPlanItem[]
}

export interface WatchSyncResult {
  created: number
  updated: number
  removed: number
  skipped: number
}

export interface PremarketCheck {
  name: string
  status: 'ok' | 'warn' | 'fail'
  detail?: string
}

export interface PremarketStatus {
  date: string
  ran_at?: string | null
  checks: PremarketCheck[]
}

// ===== P3 持仓账 (契约 §4.4) =====

export interface PortfolioPosition {
  symbol: string
  name?: string | null
  quantity: number
  avg_cost: number
  total_cost: number
  last_price?: number | null
  market_value?: number | null
  unrealized_pnl?: number | null
  unrealized_pnl_pct?: number | null
}

export interface PortfolioPositions {
  items: PortfolioPosition[]
  totals: {
    total_cost: number
    market_value?: number | null
    unrealized_pnl?: number | null
  }
}

export interface PortfolioTrade {
  id: string
  symbol: string
  side: 'buy' | 'sell'
  quantity: number
  price: number
  fee?: number | null
  traded_at: string
  note?: string | null
}

// ===== 端点 (契约 §3) =====

export const dsaApi = {
  health: () => request<{ status: string; dsa_db_found: boolean; analysis_history_count: number | null }>(`${PREFIX}/health`, { quiet: true }),

  analysisCreate: (symbols: string[], mode: 'full' | 'brief' = 'full') =>
    request<{ task_id: string }>(`${PREFIX}/analysis/tasks`, {
      method: 'POST',
      body: JSON.stringify({ symbols, mode }),
    }),

  analysisTask: (taskId: string) =>
    request<AnalysisTask>(`${PREFIX}/analysis/tasks/${encodeURIComponent(taskId)}`, { quiet: true }),

  reports: (opts?: { symbol?: string; limit?: number; offset?: number; date?: string; before?: string }) => {
    const qs = new URLSearchParams()
    if (opts?.symbol) qs.set('symbol', opts.symbol)
    if (opts?.date) qs.set('date', opts.date)
    if (opts?.before) qs.set('before', opts.before)
    qs.set('limit', String(opts?.limit ?? 50))
    qs.set('offset', String(opts?.offset ?? 0))
    return request<{ total: number; items: ReportSummary[] }>(`${PREFIX}/analysis/reports?${qs}`)
  },

  reportDetail: (id: string) =>
    request<ReportDetail>(`${PREFIX}/analysis/reports/${encodeURIComponent(id)}`),

  reportDelete: (id: string) =>
    request<{ ok: boolean }>(`${PREFIX}/analysis/reports/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  legacyReports: (opts?: { symbol?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams()
    if (opts?.symbol) qs.set('symbol', opts.symbol)
    qs.set('limit', String(opts?.limit ?? 50))
    qs.set('offset', String(opts?.offset ?? 0))
    return request<{ total: number; items: LegacyReportSummary[] }>(`${PREFIX}/legacy/reports?${qs}`, { quiet: true })
  },

  legacyReportDetail: (id: string) =>
    request<LegacyReportDetail>(`${PREFIX}/legacy/reports/${encodeURIComponent(id)}`),

  watchPlan: () => request<WatchPlan>(`${PREFIX}/watch-plan`),

  watchSyncRun: () =>
    request<WatchSyncResult>(`${PREFIX}/watch-sync/run`, { method: 'POST', body: '{}' }),

  premarketStatus: () => request<PremarketStatus>(`${PREFIX}/premarket/status`, { quiet: true }),

  portfolioPositions: () => request<PortfolioPositions>(`${PREFIX}/portfolio/positions`),

  portfolioTrades: (opts?: { symbol?: string; limit?: number }) => {
    const qs = new URLSearchParams()
    if (opts?.symbol) qs.set('symbol', opts.symbol)
    qs.set('limit', String(opts?.limit ?? 100))
    return request<{ items: PortfolioTrade[] }>(`${PREFIX}/portfolio/trades?${qs}`)
  },

  portfolioTradeAdd: (body: { symbol: string; side: 'buy' | 'sell'; quantity: number; price: number; fee?: number; traded_at?: string; note?: string }) =>
    request<PortfolioTrade>(`${PREFIX}/portfolio/trades`, { method: 'POST', body: JSON.stringify(body) }),

  portfolioTradeDelete: (id: string) =>
    request<{ ok: boolean }>(`${PREFIX}/portfolio/trades/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  portfolioImportFromDsa: () =>
    request<{ imported: number; skipped: number }>(`${PREFIX}/portfolio/import-from-dsa`, { method: 'POST', body: '{}' }),

  alertAudits: () =>
    request<{ items: { date: string; total: number; path: string }[] }>(`${PREFIX}/alert-audit`, { quiet: true }),

  alertAuditDetail: (day: string) =>
    request<{ date: string; markdown: string }>(`${PREFIX}/alert-audit/${encodeURIComponent(day)}`),

  paperRun: () =>
    request<{ ok: boolean; episodes: number; ran_at: string }>(`${PREFIX}/paper/run`, { method: 'POST', body: '{}', timeoutMs: 300_000 }),

  paperOverview: () => request<PaperOverview>(`${PREFIX}/paper/overview`),

  paperTrades: () => request<{ items: PaperTrade[] }>(`${PREFIX}/paper/trades`),

  paperOutcomes: () => request<PaperOutcomes>(`${PREFIX}/paper/outcomes`),
}
