import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { Play, TrendingUp } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import { dsaApi, type PaperTrade, type PaperOutcome } from '../api'
import { EXIT_REASON_LABEL } from '../labels'

const primaryBtnCls =
  'inline-flex items-center gap-1.5 h-8 px-3 rounded-btn bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25 transition-colors duration-150 ease-smooth disabled:opacity-50 disabled:pointer-events-none'

const tileCls = 'rounded-card border border-border bg-surface px-3.5 py-3'
const tileLabel = 'text-[11px] text-muted'
const tileValue = 'mt-1 text-lg font-semibold tabular-nums'
const tileHint = 'mt-0.5 text-[11px] text-muted'

const OUTCOME_LABEL: Record<string, { text: string; cls: string }> = {
  hit: { text: '命中', cls: 'bg-bull/10 text-bull' },
  miss: { text: '失手', cls: 'bg-bear/10 text-bear' },
  neutral: { text: '中性', cls: 'bg-elevated text-muted' },
  unable: { text: '不可评', cls: 'bg-elevated text-muted' },
}

function fmtPct2(v: number | null | undefined, signed = true) {
  if (v == null || Number.isNaN(v)) return '—'
  return `${signed && v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

// ===== 净值曲线 (echarts, 惯例同 Paper.tsx) =====

function EquityChart({ equity }: { equity: { date: string; equity: number; benchmark?: number | null }[] }) {
  const elRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!elRef.current) return
    chartRef.current = echarts.init(elRef.current, undefined, { renderer: 'canvas' })
    const onResize = () => chartRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chartRef.current?.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !equity.length) return
    const dark = document.documentElement.classList.contains('dark')
    const axisColor = dark ? '#8E8E96' : '#52525B'
    const splitColor = dark ? '#353539' : '#E4E4E7'
    chart.setOption({
      grid: { left: 48, right: 16, top: 24, bottom: 24 },
      tooltip: { trigger: 'axis' },
      legend: { data: ['点位模拟', '沪深300'], textStyle: { color: axisColor }, top: 0, right: 8 },
      xAxis: {
        type: 'category',
        data: equity.map((p) => p.date),
        axisLine: { lineStyle: { color: splitColor } },
        axisLabel: { color: axisColor },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: axisColor },
        splitLine: { lineStyle: { color: splitColor } },
      },
      series: [
        {
          name: '点位模拟',
          type: 'line',
          data: equity.map((p) => p.equity),
          showSymbol: false,
          lineStyle: { width: 1.5 },
        },
        {
          name: '沪深300',
          type: 'line',
          data: equity.map((p) => p.benchmark ?? null),
          showSymbol: false,
          lineStyle: { width: 1, type: 'dashed', opacity: 0.6 },
        },
      ],
    })
  }, [equity])

  return <div ref={elRef} className="h-64 w-full" />
}

// ===== 主页面 =====

export function PaperSimPage() {
  const [running, setRunning] = useState(false)
  const queryClient = useQueryClient()

  const overview = useQuery({ queryKey: ['dsa', 'paper', 'overview'], queryFn: () => dsaApi.paperOverview(), retry: 1 })
  const trades = useQuery({ queryKey: ['dsa', 'paper', 'trades'], queryFn: () => dsaApi.paperTrades(), retry: 1 })
  const outcomes = useQuery({ queryKey: ['dsa', 'paper', 'outcomes'], queryFn: () => dsaApi.paperOutcomes(), retry: 1 })

  if (overview.isError) {
    return (
      <div className="h-full flex flex-col">
        <PageHeader title="点位模拟" />
        <EmptyState
          icon={TrendingUp}
          title="后端扩展未就绪"
          hint="点位模拟由后端扩展 /api/ext/dsa/paper 提供，当前连接失败。契约见 docs/dsa-migration/20260929-migration-plan.md §4.6。"
        />
      </div>
    )
  }

  const s = overview.data?.summary
  const state = overview.data?.state
  const tradeItems = trades.data?.items ?? []
  const openTrades = tradeItems.filter((t) => t.status === 'open')
  const closedTrades = tradeItems.filter((t) => t.status === 'closed')
  const stats = outcomes.data?.stats
  const outcomeItems = (outcomes.data?.items ?? []).slice(0, 50)

  const runReplay = async () => {
    setRunning(true)
    try {
      const r = await dsaApi.paperRun()
      toast(`重放完成：${r.episodes} 个 episodes`, 'success')
      queryClient.invalidateQueries({ queryKey: ['dsa', 'paper'] })
    } catch { /* toast 已弹 */ } finally {
      setRunning(false)
    }
  }

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <PageHeader
        title="点位模拟"
        subtitle={state ? `起始 ${state.inception_date} · 本金 ${(state.initial_capital / 10000).toFixed(0)} 万 · ${state.max_slots} 槽位 · 零费用口径（与 DSA 历史可比）` : undefined}
        right={
          <button type="button" className={primaryBtnCls} onClick={runReplay} disabled={running}>
            <Play className="h-3.5 w-3.5" />
            {running ? '重放中…' : '立即重放'}
          </button>
        }
      />

      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
        {/* KPI 磁贴 */}
        <section className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
          <div className={tileCls}>
            <p className={tileLabel}>总收益</p>
            <p className={cn(tileValue, priceColorClass(s?.total_return_pct))}>{fmtPct2(s?.total_return_pct)}</p>
            <p className={tileHint}>自首日起</p>
          </div>
          <div className={tileCls}>
            <p className={tileLabel}>最大回撤</p>
            <p className={cn(tileValue, 'text-bear')}>{s?.max_drawdown_pct != null ? `-${Math.abs(s.max_drawdown_pct).toFixed(2)}%` : '—'}</p>
            <p className={tileHint}>权益序列</p>
          </div>
          <div className={tileCls}>
            <p className={tileLabel}>胜率</p>
            <p className={tileValue}>{s?.win_rate != null ? `${(s.win_rate * 100).toFixed(1)}%` : '—'}</p>
            <p className={tileHint}>已平仓 {s?.closed ?? 0} 笔</p>
          </div>
          <div className={tileCls}>
            <p className={tileLabel}>持仓中 / 跳过</p>
            <p className={tileValue}>{s?.open ?? 0} / {s?.skipped ?? 0}</p>
            <p className={tileHint}>满槽或无行情会跳过</p>
          </div>
        </section>

        {/* 净值曲线 */}
        {(overview.data?.equity?.length ?? 0) > 0 && (
          <section className="rounded-card border border-border bg-surface overflow-hidden">
            <div className="px-4 py-2.5 border-b border-border text-xs font-medium text-secondary">净值曲线</div>
            <div className="px-2 py-2">
              <EquityChart equity={overview.data!.equity} />
            </div>
          </section>
        )}

        {/* 命中率 */}
        <section className="rounded-card border border-border bg-surface overflow-hidden">
          <div className="px-4 py-2.5 border-b border-border text-xs font-medium text-secondary flex items-center gap-2">
            报告命中率
            {stats && (
              <span className="text-muted font-normal">
                总体 {stats.hit_rate != null ? `${(stats.hit_rate * 100).toFixed(1)}%` : '—'}（n={stats.n}）
                {stats.n < 30 && ' · 样本<30 仅供参考'}
              </span>
            )}
            {stats?.by_horizon && (
              <span className="text-muted font-normal ml-auto">
                {(['1', '3', '5', '10'] as const).map((h) => {
                  const b = stats.by_horizon?.[h]
                  if (!b) return null
                  return (
                    <span key={h} className={cn('ml-2', b.n < 30 && 'opacity-50')} title={b.n < 30 ? '样本<30 不下结论' : undefined}>
                      {h}d {b.hit_rate != null ? `${(b.hit_rate * 100).toFixed(0)}%` : '—'}(n={b.n})
                    </span>
                  )
                })}
              </span>
            )}
          </div>
          {outcomeItems.length > 0 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted border-b border-border">
                  <th className="px-4 py-2 font-medium whitespace-nowrap">报告时间</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap">代码</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap">方向</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap">窗口</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap">判定</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">收益</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap">出场</th>
                </tr>
              </thead>
              <tbody>
                {outcomeItems.map((o: PaperOutcome, i: number) => {
                  const oc = OUTCOME_LABEL[o.outcome] ?? OUTCOME_LABEL.unable
                  return (
                    <tr key={`${o.report_id}-${o.horizon}-${i}`} className="border-b border-border/50 hover:bg-elevated/40 transition-colors">
                      <td className="px-4 py-2 text-secondary whitespace-nowrap">
                        {new Date(o.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                      </td>
                      <td className="px-2 py-2 font-medium font-mono tabular-nums">{o.symbol}</td>
                      <td className="px-2 py-2">{o.direction}</td>
                      <td className="px-2 py-2">{o.horizon}d</td>
                      <td className="px-2 py-2"><span className={cn('px-1.5 py-0.5 rounded text-[11px]', oc.cls)}>{oc.text}</span></td>
                      <td className={cn('px-2 py-2 text-right font-mono tabular-nums', priceColorClass(o.return_pct))}>
                        {o.return_pct != null ? fmtPct2(o.return_pct) : '—'}
                      </td>
                      <td className="px-2 py-2 text-muted">{o.exit_reason ? (EXIT_REASON_LABEL[o.exit_reason] ?? o.exit_reason) : '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </section>

        {/* 交易明细 */}
        <section className="rounded-card border border-border bg-surface overflow-hidden">
          <div className="px-4 py-2.5 border-b border-border text-xs font-medium text-secondary">
            交易明细（持仓中 {openTrades.length} · 已平仓 {closedTrades.length}）
          </div>
          {tradeItems.length ? (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted border-b border-border">
                  <th className="px-4 py-2 font-medium whitespace-nowrap">代码</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap">建仓</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">介入价</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">份额</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap">平仓</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">出场价</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap">原因</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">收益</th>
                </tr>
              </thead>
              <tbody>
                {tradeItems.map((t: PaperTrade, i: number) => (
                  <tr key={`${t.symbol}-${t.entry_date}-${i}`} className="border-b border-border/50 hover:bg-elevated/40 transition-colors">
                    <td className="px-4 py-2 whitespace-nowrap">
                      <span className="font-medium font-mono tabular-nums">{t.symbol}</span>
                      {t.name && <span className="ml-1.5 text-muted text-[11px]">{t.name}</span>}
                    </td>
                    <td className="px-2 py-2 text-secondary whitespace-nowrap">{t.entry_date}</td>
                    <td className="px-2 py-2 text-right font-mono tabular-nums">{t.entry_price}</td>
                    <td className="px-2 py-2 text-right font-mono tabular-nums">{t.shares.toFixed(2)}</td>
                    <td className="px-2 py-2 text-secondary whitespace-nowrap">{t.exit_date ?? '持仓中'}</td>
                    <td className="px-2 py-2 text-right font-mono tabular-nums">{t.exit_price ?? '—'}</td>
                    <td className="px-2 py-2">
                      {t.exit_reason
                        ? <span className="px-1.5 py-0.5 rounded bg-elevated text-secondary text-[11px]">{EXIT_REASON_LABEL[t.exit_reason] ?? t.exit_reason}</span>
                        : <span className="text-muted">—</span>}
                    </td>
                    <td className={cn('px-2 py-2 text-right font-mono tabular-nums font-medium', priceColorClass(t.return_pct))}>
                      {t.return_pct != null ? fmtPct2(t.return_pct) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState icon={TrendingUp} title="还没有模拟交易" hint="报告出了点位后点「立即重放」，或等交易日 15:05 后自动重放。" />
          )}
        </section>
      </div>
    </div>
  )
}
