import { useQuery } from '@tanstack/react-query'
import { Modal } from '@/components/Modal'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { dsaApi, type ReportDetail, type LegacyReportDetail, type ReportPoints } from '../api'

function PointItem({ label, value, tone }: { label: string; value?: number | null; tone?: 'danger' | 'accent' }) {
  if (value == null) return null
  const cls = tone === 'danger' ? 'text-danger' : tone === 'accent' ? 'text-accent' : 'text-foreground'
  return (
    <div className="px-3 py-2 rounded-btn bg-elevated/60 border border-border">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`text-sm font-semibold ${cls}`}>{value}</div>
    </div>
  )
}

function PointsCard({ points }: { points?: ReportPoints | null }) {
  if (!points) return null
  const items = [
    <PointItem key="ib" label="理想介入" value={points.ideal_buy} />,
    <PointItem key="sb" label="次优介入" value={points.secondary_buy} />,
    <PointItem key="sl" label="止损" value={points.stop_loss} tone="danger" />,
    <PointItem key="tp" label="止盈" value={points.take_profit} tone="accent" />,
  ].filter(Boolean)
  if (!items.length) return null
  return <div className="grid grid-cols-4 gap-2">{items}</div>
}

interface Props {
  kind: 'report' | 'legacy'
  id: string
  onClose: () => void
}

/** 报告详情: 点位卡 + 盯盘条件 + markdown 全文; 报告库与 DSA 历史共用 */
export function ReportDetailDialog({ kind, id, onClose }: Props) {
  const { data, isLoading } = useQuery({
    queryKey: ['dsa', kind === 'report' ? 'report-detail' : 'legacy-report-detail', id],
    queryFn: () => (kind === 'report' ? dsaApi.reportDetail(id) : dsaApi.legacyReportDetail(id)),
  })

  const detail = data as ReportDetail | LegacyReportDetail | undefined
  const isNew = kind === 'report'
  const newDetail = isNew ? (detail as ReportDetail | undefined) : undefined
  const points: ReportPoints | null | undefined = isNew
    ? newDetail?.points
    : detail
      ? {
          ideal_buy: (detail as LegacyReportDetail).ideal_buy,
          secondary_buy: (detail as LegacyReportDetail).secondary_buy,
          stop_loss: (detail as LegacyReportDetail).stop_loss,
          take_profit: (detail as LegacyReportDetail).take_profit,
        }
      : undefined
  const pd = newDetail?.phase_decision

  return (
    <Modal
      onClose={onClose}
      labelledBy="dsa-report-detail-title"
      panelClassName="w-[94vw] max-w-3xl max-h-[88vh] flex flex-col bg-surface border border-border rounded-card shadow-xl"
    >
      <div className="px-5 py-3 border-b border-border flex items-center gap-3">
        <h2 id="dsa-report-detail-title" className="text-sm font-semibold">
          {detail ? `${detail.symbol}${newDetail?.name ? ` ${newDetail.name}` : ''} 分析报告` : '加载中…'}
        </h2>
        {detail?.operation_advice && (
          <span className="px-1.5 py-0.5 rounded bg-accent/10 text-accent text-xs">{detail.operation_advice}</span>
        )}
        {detail && (
          <span className="text-xs text-muted">
            {new Date(detail.created_at).toLocaleString('zh-CN')}
            {isNew ? '' : ' · DSA 历史（只读）'}
          </span>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
        {isLoading && <div className="text-xs text-muted py-8 text-center">加载中…</div>}

        {detail && <PointsCard points={points} />}

        {pd && (pd.immediate_action || pd.action_window || pd.next_check_time || (pd.watch_conditions?.length ?? 0) > 0 || (pd.risk_conditions?.length ?? 0) > 0) && (
          <div className="rounded-card border border-border bg-elevated/40 px-4 py-3 text-xs space-y-1.5">
            <div className="font-medium text-foreground">盯盘要点</div>
            {pd.immediate_action && <div className="text-secondary">即时动作：{pd.immediate_action}</div>}
            {pd.action_window && <div className="text-secondary">操作窗口：{pd.action_window}</div>}
            {pd.next_check_time && <div className="text-secondary">下次检查：{pd.next_check_time}</div>}
            {(pd.watch_conditions?.length ?? 0) > 0 && (
              <div className="text-secondary">关注条件：{pd.watch_conditions!.join('；')}</div>
            )}
            {(pd.risk_conditions?.length ?? 0) > 0 && (
              <div className="text-danger/90">风险条件：{pd.risk_conditions!.map((r) => r.text).join('；')}</div>
            )}
          </div>
        )}

        {detail && <MarkdownRenderer content={detail.markdown} />}
      </div>
    </Modal>
  )
}
