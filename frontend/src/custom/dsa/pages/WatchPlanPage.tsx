import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, AlertTriangle, XCircle, Crosshair, RefreshCw, Zap, ClipboardList } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import {
  dsaApi,
  type WatchPlanItem,
  type WatchRule,
  type WatchSyncState,
  type PremarketCheck,
} from '../api'
import { ReportDetailDialog } from '../components/ReportDetailDialog'

const primaryBtnCls =
  'inline-flex items-center gap-1.5 h-8 px-3 rounded-btn bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25 transition-colors duration-150 ease-smooth disabled:opacity-50 disabled:pointer-events-none'

const KIND_LABEL: Record<string, string> = {
  stop_loss: '止损',
  take_profit: '止盈',
  add: '加仓',
  reduce: '减仓',
  entry: '介入',
}

// 阶梯 kind (near_/mid_ 前缀) → 接近X/逼近X; 未知 kind 原样显示不炸版
function kindLabel(kind: string): string {
  if (kind.startsWith('near_')) return `接近${KIND_LABEL[kind.slice(5)] ?? kind.slice(5)}`
  if (kind.startsWith('mid_')) return `逼近${KIND_LABEL[kind.slice(4)] ?? kind.slice(4)}`
  return KIND_LABEL[kind] ?? kind
}

const SYNC_LABEL: Record<WatchSyncState, { text: string; cls: string }> = {
  synced: { text: '已挂线', cls: 'bg-accent/10 text-accent' },
  stale: { text: '待对账', cls: 'bg-warning/15 text-warning' },
  no_points: { text: '无点位', cls: 'bg-elevated text-muted' },
  no_report: { text: '无报告', cls: 'bg-elevated text-muted' },
}

function RuleChips({ rules }: { rules: WatchRule[] }) {
  if (!rules.length) return <span className="text-muted text-xs">—</span>
  // 主规则在前, 接近/逼近阶梯在后 (chips 列宽可控)
  const tier = (k: string) => (k.startsWith('near_') ? 1 : k.startsWith('mid_') ? 2 : 0)
  const sorted = [...rules].sort((a, b) => tier(a.kind) - tier(b.kind) || a.price - b.price)
  return (
    <div className="flex flex-wrap gap-1">
      {sorted.map((r) => (
        <span
          key={r.rule_id}
          title={`${kindLabel(r.kind)}线 ${r.price} · ${r.severity}${r.enabled ? '' : ' · 已禁用'}`}
          className={cn(
            'px-1.5 py-0.5 rounded text-[11px] whitespace-nowrap',
            r.severity === 'critical' ? 'bg-danger/10 text-danger' : r.severity === 'warn' ? 'bg-warning/15 text-warning' : 'bg-elevated text-secondary',
            !r.enabled && 'opacity-50 line-through',
          )}
        >
          {kindLabel(r.kind)} {r.price}
        </span>
      ))}
    </div>
  )
}

function PointsInline({ item }: { item: WatchPlanItem }) {
  const p = item.report?.points
  if (!p) return <span className="text-muted text-xs">—</span>
  const parts: [string, number | null | undefined, string][] = item.holding
    ? [
        ['止损', p.stop_loss, 'text-danger'],
        ['止盈', p.take_profit, 'text-accent'],
        ['加仓', p.secondary_buy, 'text-secondary'],
      ]
    : [['介入', p.ideal_buy, 'text-secondary']]
  const cells = parts.filter(([, v]) => v != null)
  if (!cells.length) return <span className="text-muted text-xs">—</span>
  return (
    <div className="flex flex-wrap gap-x-2.5 text-xs">
      {cells.map(([k, v, cls]) => (
        <span key={k} className="whitespace-nowrap">
          <span className="text-muted">{k}</span> <span className={cn('font-medium', cls)}>{v}</span>
        </span>
      ))}
    </div>
  )
}

function WatchConditions({ item }: { item: WatchPlanItem }) {
  const pd = item.report?.phase_decision
  const texts = [
    pd?.immediate_action,
    ...(pd?.watch_conditions ?? []),
    ...(pd?.risk_conditions ?? []).map((r) => r.text),
  ].filter(Boolean) as string[]
  if (!texts.length) return <span className="text-muted text-xs">—</span>
  return (
    <span className="text-xs text-secondary line-clamp-2" title={texts.join('；')}>
      {texts.join('；')}
    </span>
  )
}

function CheckIcon({ status }: { status: PremarketCheck['status'] }) {
  if (status === 'ok') return <CheckCircle2 className="h-3.5 w-3.5 text-accent shrink-0" />
  if (status === 'warn') return <AlertTriangle className="h-3.5 w-3.5 text-warning shrink-0" />
  return <XCircle className="h-3.5 w-3.5 text-danger shrink-0" />
}

function ItemTable({
  items,
  holding,
  onOpenReport,
}: {
  items: WatchPlanItem[]
  holding: boolean
  onOpenReport: (reportId: string) => void
}) {
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-muted border-b border-border">
          <th className="px-5 py-2 font-medium">代码</th>
          {holding && <th className="px-2 py-2 font-medium">持仓</th>}
          <th className="px-2 py-2 font-medium">最新建议</th>
          <th className="px-2 py-2 font-medium">{holding ? '点位' : '介入点'}</th>
          <th className="px-2 py-2 font-medium">盯盘要点</th>
          <th className="px-2 py-2 font-medium">挂线</th>
          <th className="px-2 py-2 font-medium">状态</th>
          <th className="px-2 py-2 w-14" />
        </tr>
      </thead>
      <tbody>
        {items.map((it) => {
          const sync = SYNC_LABEL[it.sync_state]
          return (
            <tr key={it.symbol} className="border-b border-border/50 hover:bg-elevated/40 transition-colors">
              <td className="px-5 py-2.5 whitespace-nowrap">
                <div className="font-medium">{it.symbol}</div>
                {it.name && <div className="text-muted text-[11px]">{it.name}</div>}
              </td>
              {holding && (
                <td className="px-2 py-2.5 text-secondary whitespace-nowrap">
                  {it.quantity != null ? `${it.quantity}股 @ ${it.avg_cost ?? '—'}` : '—'}
                </td>
              )}
              <td className="px-2 py-2.5">
                {it.report?.operation_advice ? (
                  <span className="px-1.5 py-0.5 rounded bg-accent/10 text-accent whitespace-nowrap">{it.report.operation_advice}</span>
                ) : (
                  <span className="text-muted">—</span>
                )}
              </td>
              <td className="px-2 py-2.5"><PointsInline item={it} /></td>
              <td className="px-2 py-2.5 max-w-72"><WatchConditions item={it} /></td>
              <td className="px-2 py-2.5"><RuleChips rules={it.rules} /></td>
              <td className="px-2 py-2.5">
                <span className={cn('px-1.5 py-0.5 rounded text-[11px] whitespace-nowrap', sync.cls)}>{sync.text}</span>
              </td>
              <td className="px-2 py-2.5">
                {it.report && (
                  <button type="button" className="text-xs text-accent hover:underline whitespace-nowrap" onClick={() => onOpenReport(it.report!.id)}>
                    看报告
                  </button>
                )}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

// ===== 告警复盘 (P4 §4.5-3, 回放审计只读展示) =====

function AlertAuditBlock() {
  const [expanded, setExpanded] = useState(false)
  const audits = useQuery({ queryKey: ['dsa', 'alert-audit'], queryFn: () => dsaApi.alertAudits(), retry: 1 })
  const latest = audits.data?.items?.[0]
  const detail = useQuery({
    queryKey: ['dsa', 'alert-audit', latest?.date],
    queryFn: () => dsaApi.alertAuditDetail(latest!.date),
    enabled: expanded && !!latest,
  })
  if (!latest) return null
  return (
    <section className="mx-5 mt-4 rounded-card border border-border bg-surface overflow-hidden">
      <button
        type="button"
        className="w-full px-4 py-2.5 flex items-center gap-2 text-xs hover:bg-elevated/40 transition-colors"
        onClick={() => setExpanded((v) => !v)}
      >
        <ClipboardList className="h-3.5 w-3.5 text-muted" />
        <span className="font-medium text-secondary">告警复盘</span>
        <span className="text-muted">{latest.date} · {latest.total} 条触发</span>
        <span className="ml-auto text-muted">{expanded ? '收起' : '展开'}</span>
      </button>
      {expanded && (
        <div className="px-4 py-3 border-t border-border">
          {detail.data?.markdown
            ? <MarkdownRenderer content={detail.data.markdown} />
            : <div className="text-xs text-muted">加载中…</div>}
        </div>
      )}
    </section>
  )
}

export function WatchPlanPage() {
  const [openedReport, setOpenedReport] = useState<string | null>(null)
  const [syncing, setSyncing] = useState(false)
  const queryClient = useQueryClient()

  const plan = useQuery({ queryKey: ['dsa', 'watch-plan'], queryFn: () => dsaApi.watchPlan(), retry: 1 })
  const premarket = useQuery({ queryKey: ['dsa', 'premarket'], queryFn: () => dsaApi.premarketStatus(), retry: 1 })

  if (plan.isError) {
    return (
      <div className="h-full flex flex-col">
        <PageHeader title="次日盯盘" />
        <EmptyState
          icon={Crosshair}
          title="后端扩展未就绪"
          hint="次日盯盘由后端扩展 /api/ext/dsa/watch-plan 提供，当前连接失败。请确认后端已加载 DSA 盯盘扩展（契约见 docs/dsa-migration/20260929-migration-plan.md §4）。"
        />
      </div>
    )
  }

  const items = plan.data?.items ?? []
  const holdings = items.filter((it) => it.holding)
  const watchers = items.filter((it) => !it.holding)
  const checks = premarket.data?.checks ?? []
  const abnormalChecks = checks.filter((c) => c.status !== 'ok')

  const runSync = async () => {
    setSyncing(true)
    try {
      const r = await dsaApi.watchSyncRun()
      toast(`对账完成：新建 ${r.created} · 更新 ${r.updated} · 移除 ${r.removed} · 跳过 ${r.skipped}`, 'success')
      queryClient.invalidateQueries({ queryKey: ['dsa', 'watch-plan'] })
    } catch {
      setSyncing(false)
      return
    }
    setSyncing(false)
  }

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <PageHeader
        title="次日盯盘"
        subtitle={
          plan.data
            ? `持仓来源 ${plan.data.positions_source} · 生成于 ${new Date(plan.data.generated_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}`
            : undefined
        }
        right={
          <button type="button" className={primaryBtnCls} onClick={runSync} disabled={syncing}>
            <Zap className="h-3.5 w-3.5" />
            {syncing ? '对账中…' : '立即对账'}
          </button>
        }
      />

      <div className="flex-1 overflow-y-auto pb-6">
        {checks.length > 0 && (
          <div className="mx-5 mt-3 px-4 py-3 rounded-card border border-border bg-surface">
            <div className="flex items-center gap-2 text-xs">
              <span className="font-medium text-foreground">盘前自检</span>
              <span className="text-muted">
                {premarket.data?.date}
                {abnormalChecks.length === 0 ? ' · 全部正常' : ` · ${abnormalChecks.length} 项异常`}
              </span>
            </div>
            {abnormalChecks.length > 0 && (
              <div className="mt-2 space-y-1">
                {abnormalChecks.map((c) => (
                  <div key={c.name} className="flex items-center gap-1.5 text-xs text-secondary">
                    <CheckIcon status={c.status} />
                    <span>{c.name}{c.detail ? `：${c.detail}` : ''}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {plan.isLoading ? (
          <div className="px-5 py-8 text-center text-xs text-muted">
            <RefreshCw className="h-4 w-4 animate-spin inline-block mr-2" />
            加载盯盘清单…
          </div>
        ) : !items.length ? (
          <EmptyState
            icon={Crosshair}
            title="盯盘清单为空"
            hint="自选股还没有分析报告。先去「分析中心」跑一轮批量分析，报告里的点位会自动出现在这里并挂成盘中告警线。"
          />
        ) : (
          <>
            <div className="px-5 pt-4 pb-1 text-xs font-medium text-secondary">
              持仓（{holdings.length}）<span className="text-muted font-normal"> · 止损/止盈 critical，加仓/减仓 warn</span>
            </div>
            {holdings.length ? <ItemTable items={holdings} holding onOpenReport={setOpenedReport} /> : (
              <div className="px-5 py-3 text-xs text-muted">当前无持仓（持仓来源：{plan.data?.positions_source}）</div>
            )}

            <div className="px-5 pt-4 pb-1 text-xs font-medium text-secondary">
              空仓自选（{watchers.length}）<span className="text-muted font-normal"> · 理想介入点挂 info 线</span>
            </div>
            {watchers.length ? <ItemTable items={watchers} holding={false} onOpenReport={setOpenedReport} /> : (
              <div className="px-5 py-3 text-xs text-muted">自选股全部在持仓中</div>
            )}
          </>
        )}

        <AlertAuditBlock />
      </div>

      {openedReport && <ReportDetailDialog kind="report" id={openedReport} onClose={() => setOpenedReport(null)} />}
    </div>
  )
}
