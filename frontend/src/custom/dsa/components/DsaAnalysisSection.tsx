import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FlaskConical, ListChecks, Play, RefreshCw, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { EmptyState } from '@/components/EmptyState'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { dsaApi, type ReportSummary, type LegacyReportSummary } from '../api'
import { ReportDetailDialog } from './ReportDetailDialog'

const btnCls =
  'inline-flex items-center gap-1.5 h-8 px-2.5 rounded-btn bg-elevated text-xs text-secondary hover:bg-elevated/80 hover:text-foreground transition-colors duration-150 ease-smooth'
const primaryBtnCls =
  'inline-flex items-center gap-1.5 h-8 px-3 rounded-btn bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25 transition-colors duration-150 ease-smooth disabled:opacity-50 disabled:pointer-events-none'

function fmtTime(iso: string) {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function PointsCell({ points }: { points?: ReportSummary['points'] | null }) {
  if (!points) return <span className="text-muted">—</span>
  const parts: [string, number | null | undefined][] = [
    ['介入', points.ideal_buy],
    ['次优', points.secondary_buy],
    ['止损', points.stop_loss],
    ['止盈', points.take_profit],
  ]
  const text = parts.filter(([, v]) => v != null).map(([k, v]) => `${k}${v}`).join(' ')
  return <span className="text-xs text-secondary whitespace-nowrap">{text || '—'}</span>
}

// ===== 新建分析对话框 =====

function NewAnalysisDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (taskId: string) => void }) {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [manual, setManual] = useState('')
  const [mode, setMode] = useState<'full' | 'brief'>('full')
  const [submitting, setSubmitting] = useState(false)

  const { data: watchlist } = useQuery({
    queryKey: ['watchlist', 'list'],
    queryFn: () => api.watchlistList(),
  })
  const symbols = (watchlist?.symbols ?? []).map((s) => s.symbol)

  const toggle = (sym: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(sym)) next.delete(sym)
      else next.add(sym)
      return next
    })
  }

  const submit = async () => {
    const extra = manual.split(/[\s,，;；]+/).map((s) => s.trim()).filter(Boolean)
    const all = [...new Set([...selected, ...extra])]
    if (!all.length) {
      toast('至少选择或输入一只股票', 'error')
      return
    }
    if (all.length > 50) {
      toast(`一次最多 50 只（当前 ${all.length} 只）`, 'error')
      return
    }
    setSubmitting(true)
    try {
      const { task_id } = await dsaApi.analysisCreate(all, mode)
      toast(`分析任务已创建（${all.length} 只）`, 'success')
      onCreated(task_id)
    } catch {
      setSubmitting(false)
    }
  }

  return (
    <Modal onClose={onClose} labelledBy="dsa-new-analysis-title" panelClassName="w-[92vw] max-w-xl bg-surface border border-border rounded-card shadow-xl">
      <div className="px-5 py-4 border-b border-border flex items-center justify-between">
        <h2 id="dsa-new-analysis-title" className="text-sm font-semibold">新建分析</h2>
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value as 'full' | 'brief')}
          className="h-7 px-2 rounded-btn bg-elevated text-xs text-secondary border border-border"
          aria-label="分析模式"
        >
          <option value="full">完整报告</option>
          <option value="brief">简报</option>
        </select>
      </div>
      <div className="px-5 py-4 max-h-[50vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs text-muted">自选股（已选 {selected.size}）</span>
          <div className="flex gap-2">
            <button type="button" className="text-xs text-accent hover:underline" onClick={() => setSelected(new Set(symbols))}>全选</button>
            <button type="button" className="text-xs text-muted hover:underline" onClick={() => setSelected(new Set())}>清空</button>
          </div>
        </div>
        <div className="grid grid-cols-4 gap-1.5">
          {symbols.map((sym) => (
            <label
              key={sym}
              className={`flex items-center gap-1.5 px-2 py-1.5 rounded-btn text-xs cursor-pointer border transition-colors ${
                selected.has(sym) ? 'border-accent/50 bg-accent/10 text-foreground' : 'border-border bg-elevated/50 text-secondary hover:text-foreground'
              }`}
            >
              <input type="checkbox" checked={selected.has(sym)} onChange={() => toggle(sym)} className="accent-current" />
              {sym}
            </label>
          ))}
          {!symbols.length && <span className="col-span-4 text-xs text-muted py-4 text-center">自选股为空，可在下方手动输入代码</span>}
        </div>
        <textarea
          value={manual}
          onChange={(e) => setManual(e.target.value)}
          placeholder="手动补充代码，空格/逗号分隔，如 600519, 000001"
          className="mt-3 w-full h-16 px-2.5 py-2 rounded-btn bg-elevated border border-border text-xs text-foreground placeholder:text-muted resize-none"
        />
      </div>
      <div className="px-5 py-3 border-t border-border flex justify-end gap-2">
        <button type="button" className={btnCls} onClick={onClose}>取消</button>
        <button type="button" className={primaryBtnCls} onClick={submit} disabled={submitting}>
          <Play className="h-3.5 w-3.5" />
          {submitting ? '创建中…' : '开始分析'}
        </button>
      </div>
    </Modal>
  )
}

// ===== 任务进度卡 =====

function TaskProgressCard({ taskId, onDone }: { taskId: string; onDone: () => void }) {
  const { data: task } = useQuery({
    queryKey: ['dsa', 'analysis-task', taskId],
    queryFn: () => dsaApi.analysisTask(taskId),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 2000 : false),
  })

  useEffect(() => {
    if (task && task.status !== 'running') onDone()
  }, [task, onDone])

  if (!task) return null
  const pct = task.total ? Math.round((task.done / task.total) * 100) : 0
  const running = task.status === 'running'

  return (
    <div className="px-4 py-3 rounded-card border border-border bg-surface">
      <div className="flex items-center justify-between text-xs">
        <span className="text-secondary">
          分析任务 {task.task_id} · {running ? '进行中' : task.status === 'done' ? '已完成' : '失败'} {task.done}/{task.total}
        </span>
        {running && <RefreshCw className="h-3.5 w-3.5 text-accent animate-spin" />}
      </div>
      <div className="mt-2 h-1.5 rounded-full bg-elevated overflow-hidden">
        <div className="h-full bg-accent transition-all duration-500" style={{ width: `${pct}%` }} />
      </div>
      {task.items.some((it) => it.status === 'failed') && (
        <div className="mt-2 text-xs text-danger">
          {task.items.filter((it) => it.status === 'failed').map((it) => `${it.symbol} ${it.error ?? ''}`).join('；')}
        </div>
      )}
    </div>
  )
}

// ===== 报告表格 (报告库 / DSA 历史共用行结构) =====

interface ReportRow {
  id: string
  symbol: string
  name?: string | null
  created_at: string
  operation_advice?: string | null
  sentiment_score?: number | null
  summary?: string | null
  pointsText?: React.ReactNode
}

function ReportsTable({
  rows,
  onOpen,
  onDelete,
}: {
  rows: ReportRow[]
  onOpen: (id: string) => void
  onDelete?: (id: string) => void
}) {
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-muted border-b border-border">
          <th className="py-2 pr-2 font-medium whitespace-nowrap">时间</th>
          <th className="px-2 py-2 font-medium whitespace-nowrap">代码</th>
          <th className="px-2 py-2 font-medium whitespace-nowrap">评分</th>
          <th className="px-2 py-2 font-medium whitespace-nowrap">建议</th>
          <th className="px-2 py-2 font-medium whitespace-nowrap">点位</th>
          <th className="px-2 py-2 font-medium">摘要</th>
          {onDelete && <th className="px-2 py-2 w-10" />}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr
            key={r.id}
            className="border-b border-border/50 hover:bg-elevated/40 cursor-pointer transition-colors"
            onClick={() => onOpen(r.id)}
          >
            <td className="py-2.5 pr-2 text-secondary whitespace-nowrap">{fmtTime(r.created_at)}</td>
                <td className="px-2 py-2.5 font-medium whitespace-nowrap">
                  {r.symbol}
                  {r.name && <span className="ml-1 text-muted font-normal">{r.name}</span>}
                </td>
            <td className="px-2 py-2.5">{r.sentiment_score ?? '—'}</td>
            <td className="px-2 py-2.5">
              {r.operation_advice ? <span className="px-1.5 py-0.5 rounded bg-accent/10 text-accent">{r.operation_advice}</span> : '—'}
            </td>
            <td className="px-2 py-2.5">{r.pointsText}</td>
            <td className="px-2 py-2.5 text-muted max-w-64 truncate">{r.summary || '—'}</td>
            {onDelete && (
              <td className="px-2 py-2.5">
                <button
                  type="button"
                  aria-label={`删除 ${r.symbol} 报告`}
                  className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 transition-colors"
                  onClick={(e) => {
                    e.stopPropagation()
                    onDelete(r.id)
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// ===== 主区块: 嵌进核心个股分析页的 DSA 决策报告区 =====

export function DsaAnalysisSection({ symbol }: { symbol?: string }) {
  const [tab, setTab] = useState<'reports' | 'legacy'>('reports')
  const [scope, setScope] = useState<'today' | 'past'>('today')
  const [symbolFilter, setSymbolFilter] = useState('')
  const [showNew, setShowNew] = useState(false)
  const [activeTaskIds, setActiveTaskIds] = useState<string[]>([])
  const [analyzingWatchlist, setAnalyzingWatchlist] = useState(false)
  const [opened, setOpened] = useState<{ kind: 'report' | 'legacy'; id: string } | null>(null)
  const queryClient = useQueryClient()

  const now = new Date()
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`

  const health = useQuery({ queryKey: ['dsa', 'health'], queryFn: () => dsaApi.health(), retry: 1 })

  const reports = useQuery({
    queryKey: ['dsa', 'reports', symbolFilter, scope, today],
    queryFn: () =>
      dsaApi.reports({
        symbol: symbolFilter || undefined,
        date: scope === 'today' ? today : undefined,
        before: scope === 'past' ? today : undefined,
      }),
    enabled: tab === 'reports',
  })

  const legacy = useQuery({
    queryKey: ['dsa', 'legacy-reports', symbolFilter],
    queryFn: () => dsaApi.legacyReports({ symbol: symbolFilter || undefined }),
    enabled: tab === 'legacy',
  })

  // 一键分析全部自选: 契约单任务上限 50, 超出分块为多个任务
  const analyzeWatchlist = async () => {
    setAnalyzingWatchlist(true)
    try {
      const { symbols } = await api.watchlistList()
      const codes = [...new Set(symbols.map((s) => s.symbol))]
      if (!codes.length) {
        toast('自选股是空的，先去自选页加几只', 'error')
        return
      }
      const ids: string[] = []
      for (let i = 0; i < codes.length; i += 50) {
        const chunk = codes.slice(i, i + 50)
        const { task_id } = await dsaApi.analysisCreate(chunk, 'full')
        ids.push(task_id)
      }
      toast(`已创建自选全量分析（${codes.length} 只${ids.length > 1 ? `，分 ${ids.length} 个任务` : ''}）`, 'success')
      setActiveTaskIds((prev) => [...prev, ...ids])
    } catch { /* toast 已由 request 弹出 */ } finally {
      setAnalyzingWatchlist(false)
    }
  }

  const reportRows: ReportRow[] = (reports.data?.items ?? []).map((r: ReportSummary) => ({
    id: r.id,
    symbol: r.symbol,
    name: r.name,
    created_at: r.created_at,
    operation_advice: r.operation_advice,
    sentiment_score: r.sentiment_score,
    summary: r.analysis_summary,
    pointsText: <PointsCell points={r.points} />,
  }))

  const legacyRows: ReportRow[] = (legacy.data?.items ?? []).map((r: LegacyReportSummary) => ({
    id: r.id,
    symbol: r.symbol,
    created_at: r.created_at,
    operation_advice: r.operation_advice,
    sentiment_score: r.sentiment_score,
    summary: r.analysis_summary,
    pointsText: (
      <PointsCell
        points={{ ideal_buy: r.ideal_buy, secondary_buy: r.secondary_buy, stop_loss: r.stop_loss, take_profit: r.take_profit }}
      />
    ),
  }))

  return (
    <section className="rounded-card border border-border bg-surface overflow-hidden">
      <div className="flex items-center justify-between gap-4 px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold tracking-tight flex items-center gap-2">
          决策报告（DSA）
          <span className="text-xs text-muted font-normal">四点位 + 操作建议 + 盯盘条件</span>
        </h2>
        <div className="flex items-center gap-2">
          <button type="button" className={primaryBtnCls} onClick={analyzeWatchlist} disabled={analyzingWatchlist}>
            <ListChecks className="h-3.5 w-3.5" />
            {analyzingWatchlist ? '创建中…' : '分析自选'}
          </button>
          <button type="button" className={btnCls} onClick={() => setShowNew(true)}>
            <Play className="h-3.5 w-3.5" />
            新建分析
          </button>
        </div>
      </div>

      {health.isError ? (
        <div className="m-4 px-4 py-3 rounded-card border border-warning/40 bg-warning/10 text-xs text-warning">
          DSA 分析后端扩展未就绪（/api/ext/dsa 连接失败）。契约见 docs/dsa-migration/20260929-migration-plan.md。
        </div>
      ) : (
        <>
          {activeTaskIds.length > 0 && (
            <div className="px-4 pt-3 space-y-2">
              {activeTaskIds.map((id) => (
                <TaskProgressCard
                  key={id}
                  taskId={id}
                  onDone={() => {
                    queryClient.invalidateQueries({ queryKey: ['dsa', 'reports'] })
                  }}
                />
              ))}
            </div>
          )}

          <div className="flex items-center gap-3 px-4 border-b border-border">
            {(['reports', 'legacy'] as const).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={`py-2 text-xs border-b-2 transition-colors ${
                  tab === t ? 'border-accent text-foreground font-medium' : 'border-transparent text-muted hover:text-secondary'
                }`}
              >
                {t === 'reports' ? '报告库' : 'DSA 历史（只读）'}
              </button>
            ))}
            {symbol && symbolFilter !== symbol && (
              <button
                type="button"
                className="my-1.5 px-1.5 py-0.5 rounded bg-accent/10 text-accent text-[11px] hover:bg-accent/20 transition-colors"
                onClick={() => setSymbolFilter(symbol)}
              >
                只看 {symbol}
              </button>
            )}
            <div className="ml-auto my-1.5 flex items-center rounded-btn bg-elevated border border-border overflow-hidden">
              {(['today', 'past'] as const).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setScope(s)}
                  className={`h-7 px-2.5 text-xs transition-colors ${
                    scope === s ? 'bg-accent/15 text-accent font-medium' : 'text-muted hover:text-secondary'
                  }`}
                >
                  {s === 'today' ? '今日' : '往期'}
                </button>
              ))}
            </div>
            <input
              value={symbolFilter}
              onChange={(e) => setSymbolFilter(e.target.value.trim())}
              placeholder="按代码筛选"
              className="my-1.5 h-7 w-32 px-2 rounded-btn bg-elevated border border-border text-xs text-foreground placeholder:text-muted"
            />
          </div>

          <div className="px-4 pb-2">

          {tab === 'reports' ? (
            reportRows.length ? (
              <ReportsTable
                rows={reportRows}
                onOpen={(id) => setOpened({ kind: 'report', id })}
                onDelete={async (id) => {
                  await dsaApi.reportDelete(id)
                  toast('报告已删除', 'success')
                  queryClient.invalidateQueries({ queryKey: ['dsa', 'reports'] })
                }}
              />
            ) : (
              <EmptyState
                icon={FlaskConical}
                title={scope === 'today' ? '今天还没有分析报告' : '往期没有分析报告'}
                hint={
                  scope === 'today'
                    ? '点「分析自选」一键全量分析，或「新建分析」挑几只。交易日 18:00 也会自动全量分析（后端定时任务）。'
                    : '切换到「今日」查看最新报告，或调整代码筛选。'
                }
              />
            )
          ) : legacyRows.length ? (
            <ReportsTable rows={legacyRows} onOpen={(id) => setOpened({ kind: 'legacy', id })} />
          ) : (
            <EmptyState
              icon={FlaskConical}
              title={legacy.isError ? 'DSA 旧库不可用' : '旧库暂无记录'}
              hint={legacy.isError ? '未找到或无法读取 DSA 数据库（只读桥接）。旧报告仍在 daily_stock_analysis 项目中可查。' : 'DSA 的 analysis_history 里没有匹配的历史报告。'}
            />
          )}
          </div>
        </>
      )}

      {showNew && (
        <NewAnalysisDialog
          onClose={() => setShowNew(false)}
          onCreated={(taskId) => {
            setShowNew(false)
            setActiveTaskIds((prev) => [...prev, taskId])
          }}
        />
      )}

      {opened && <ReportDetailDialog kind={opened.kind} id={opened.id} onClose={() => setOpened(null)} />}
    </section>
  )
}
