import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Briefcase, Download, Plus, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import { dsaApi, type PortfolioTrade } from '../api'

const btnCls =
  'inline-flex items-center gap-1.5 h-8 px-2.5 rounded-btn bg-elevated text-xs text-secondary hover:bg-elevated/80 hover:text-foreground transition-colors duration-150 ease-smooth'
const primaryBtnCls =
  'inline-flex items-center gap-1.5 h-8 px-3 rounded-btn bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25 transition-colors duration-150 ease-smooth disabled:opacity-50 disabled:pointer-events-none'

function fmtNum(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: digits })
}

function fmtTime(iso: string) {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// ===== 记一笔对话框 =====

function TradeDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [quantity, setQuantity] = useState('')
  const [price, setPrice] = useState('')
  const [fee, setFee] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async () => {
    const qty = Number(quantity)
    const px = Number(price)
    if (!/^\d{6}$/.test(symbol.trim())) {
      toast('代码格式不对（6 位数字）', 'error')
      return
    }
    if (!(qty > 0) || !(px > 0)) {
      toast('数量和价格必须大于 0', 'error')
      return
    }
    setSubmitting(true)
    try {
      await dsaApi.portfolioTradeAdd({
        symbol: symbol.trim(),
        side,
        quantity: qty,
        price: px,
        ...(Number(fee) > 0 ? { fee: Number(fee) } : {}),
        ...(note.trim() ? { note: note.trim() } : {}),
      })
      toast('已记账', 'success')
      onSaved()
    } catch {
      setSubmitting(false)
    }
  }

  const inputCls = 'h-8 w-full px-2.5 rounded-btn bg-elevated border border-border text-xs text-foreground placeholder:text-muted'

  return (
    <Modal onClose={onClose} labelledBy="dsa-trade-title" panelClassName="w-[92vw] max-w-md bg-surface border border-border rounded-card shadow-xl">
      <div className="px-5 py-4 border-b border-border">
        <h2 id="dsa-trade-title" className="text-sm font-semibold">记一笔</h2>
      </div>
      <div className="px-5 py-4 space-y-3">
        <div className="grid grid-cols-2 gap-2">
          {(['buy', 'sell'] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setSide(s)}
              className={cn(
                'h-8 rounded-btn text-xs font-medium border transition-colors',
                side === s
                  ? s === 'buy'
                    ? 'border-bull/50 bg-bull/10 text-bull'
                    : 'border-bear/50 bg-bear/10 text-bear'
                  : 'border-border bg-elevated text-secondary hover:text-foreground',
              )}
            >
              {s === 'buy' ? '买入' : '卖出'}
            </button>
          ))}
        </div>
        <input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="代码，如 600519" className={inputCls} />
        <div className="grid grid-cols-2 gap-2">
          <input value={quantity} onChange={(e) => setQuantity(e.target.value)} placeholder="数量（股）" inputMode="decimal" className={inputCls} />
          <input value={price} onChange={(e) => setPrice(e.target.value)} placeholder="价格（元）" inputMode="decimal" className={inputCls} />
        </div>
        <input value={fee} onChange={(e) => setFee(e.target.value)} placeholder="费用（可省）" inputMode="decimal" className={inputCls} />
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="备注（可省）" className={inputCls} />
      </div>
      <div className="px-5 py-3 border-t border-border flex justify-end gap-2">
        <button type="button" className={btnCls} onClick={onClose}>取消</button>
        <button type="button" className={primaryBtnCls} onClick={submit} disabled={submitting}>
          {submitting ? '保存中…' : '保存'}
        </button>
      </div>
    </Modal>
  )
}

// ===== 从 DSA 导入确认框 =====

function ImportDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [running, setRunning] = useState(false)
  const run = async () => {
    setRunning(true)
    try {
      const r = await dsaApi.portfolioImportFromDsa()
      toast(`导入完成：${r.imported} 条流水${r.skipped ? `，跳过 ${r.skipped} 条` : ''}`, 'success')
      onDone()
    } catch {
      setRunning(false)
    }
  }
  return (
    <Modal onClose={onClose} labelledBy="dsa-import-title" panelClassName="w-[92vw] max-w-sm bg-surface border border-border rounded-card shadow-xl">
      <div className="px-5 py-4 border-b border-border">
        <h2 id="dsa-import-title" className="text-sm font-semibold">从 DSA 导入流水</h2>
      </div>
      <div className="px-5 py-4 text-xs text-secondary leading-relaxed">
        把 DSA 库里的全部买卖流水一次性导入本账（只读旧库，不改它）。已有流水时会被拒绝——这是开局一次性操作，导入后只在 TSP 记账。
      </div>
      <div className="px-5 py-3 border-t border-border flex justify-end gap-2">
        <button type="button" className={btnCls} onClick={onClose}>取消</button>
        <button type="button" className={primaryBtnCls} onClick={run} disabled={running}>
          <Download className="h-3.5 w-3.5" />
          {running ? '导入中…' : '确认导入'}
        </button>
      </div>
    </Modal>
  )
}

// ===== 主页面 =====

export function PortfolioPage() {
  const [showTrade, setShowTrade] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const queryClient = useQueryClient()

  const positions = useQuery({ queryKey: ['dsa', 'portfolio', 'positions'], queryFn: () => dsaApi.portfolioPositions(), retry: 1 })
  const trades = useQuery({ queryKey: ['dsa', 'portfolio', 'trades'], queryFn: () => dsaApi.portfolioTrades(), retry: 1 })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['dsa', 'portfolio'] })
  }

  if (positions.isError) {
    return (
      <div className="h-full flex flex-col">
        <PageHeader title="持仓" />
        <EmptyState
          icon={Briefcase}
          title="后端扩展未就绪"
          hint="持仓账由后端扩展 /api/ext/dsa/portfolio 提供，当前连接失败。契约见 docs/dsa-migration/20260929-migration-plan.md §4.4。"
        />
      </div>
    )
  }

  const items = positions.data?.items ?? []
  const totals = positions.data?.totals
  const tradeItems = trades.data?.items ?? []
  const totalMv = totals?.market_value ?? null
  const totalPnlPct =
    totals?.unrealized_pnl != null && totals.total_cost > 0
      ? (totals.unrealized_pnl / totals.total_cost) * 100
      : null

  const tileCls = 'rounded-card border border-border bg-surface px-3.5 py-3'
  const tileLabel = 'text-[11px] text-muted'
  const tileValue = 'mt-1 text-lg font-semibold tabular-nums'
  const tileHint = 'mt-0.5 text-[11px] text-muted'

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <PageHeader
        title="持仓"
        right={
          <div className="flex items-center gap-2">
            <button type="button" className={btnCls} onClick={() => setShowImport(true)}>
              <Download className="h-3.5 w-3.5" />
              从 DSA 导入
            </button>
            <button type="button" className={primaryBtnCls} onClick={() => setShowTrade(true)}>
              <Plus className="h-3.5 w-3.5" />
              记一笔
            </button>
          </div>
        }
      />

      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
        {/* KPI 磁贴 (照搬 DSA 持仓页磁贴行) */}
        <section className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
          <div className={tileCls}>
            <p className={tileLabel}>总市值</p>
            <p className={tileValue}>{totalMv != null ? fmtNum(totalMv) : '—'}</p>
            <p className={tileHint}>{items.length} 只持仓</p>
          </div>
          <div className={tileCls}>
            <p className={tileLabel}>总成本</p>
            <p className={tileValue}>{totals ? fmtNum(totals.total_cost) : '—'}</p>
            <p className={tileHint}>FIFO 重放</p>
          </div>
          <div className={tileCls}>
            <p className={tileLabel}>浮动盈亏</p>
            <p className={cn(tileValue, priceColorClass(totals?.unrealized_pnl))}>
              {totals?.unrealized_pnl != null ? `${totals.unrealized_pnl >= 0 ? '+' : ''}${fmtNum(totals.unrealized_pnl)}` : '—'}
            </p>
            <p className={tileHint}>
              {totalPnlPct != null ? `${totalPnlPct >= 0 ? '+' : ''}${totalPnlPct.toFixed(2)}%` : '—'}
            </p>
          </div>
          <div className={tileCls}>
            <p className={tileLabel}>流水笔数</p>
            <p className={tileValue}>{tradeItems.length}</p>
            <p className={tileHint}>手工记账 + DSA 导入</p>
          </div>
        </section>
        {/* 持仓面板 */}
        <section className="rounded-card border border-border bg-surface overflow-hidden">
          <div className="px-4 py-2.5 border-b border-border text-xs font-medium text-secondary">
            当前持仓（{items.length}）
          </div>
          {items.length ? (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted border-b border-border">
                  <th className="px-4 py-2 font-medium whitespace-nowrap">代码</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">现价</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">数量</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">成本价</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">市值</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">权重%</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">浮盈</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">浮盈%</th>
                </tr>
              </thead>
              <tbody>
                {items.map((p) => {
                  const weight = p.market_value != null && totalMv != null && totalMv > 0
                    ? (p.market_value / totalMv) * 100
                    : null
                  return (
                    <tr key={p.symbol} className="border-b border-border/50 hover:bg-elevated/40 transition-colors">
                      <td className="px-4 py-2 whitespace-nowrap">
                        <span className="font-medium font-mono tabular-nums">{p.symbol}</span>
                        {p.name && <span className="ml-1.5 text-muted text-[11px]">{p.name}</span>}
                      </td>
                      <td className="px-2 py-2 text-right font-mono tabular-nums">{p.last_price != null ? fmtNum(p.last_price) : '—'}</td>
                      <td className="px-2 py-2 text-right font-mono tabular-nums">{fmtNum(p.quantity, 0)}</td>
                      <td className="px-2 py-2 text-right font-mono tabular-nums">{fmtNum(p.avg_cost)}</td>
                      <td className="px-2 py-2 text-right font-mono tabular-nums">{p.market_value != null ? fmtNum(p.market_value) : '—'}</td>
                      <td className="px-2 py-2 text-right font-mono tabular-nums">{weight != null ? `${weight.toFixed(1)}%` : '—'}</td>
                      <td className={cn('px-2 py-2 text-right font-mono tabular-nums font-medium', priceColorClass(p.unrealized_pnl))}>
                        {p.unrealized_pnl != null ? `${p.unrealized_pnl >= 0 ? '+' : ''}${fmtNum(p.unrealized_pnl)}` : '—'}
                      </td>
                      <td className={cn('px-2 py-2 text-right font-mono tabular-nums font-medium', priceColorClass(p.unrealized_pnl_pct))}>
                        {p.unrealized_pnl_pct != null ? `${p.unrealized_pnl_pct >= 0 ? '+' : ''}${p.unrealized_pnl_pct.toFixed(2)}%` : '—'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
              <tfoot>
                <tr className="border-t border-border text-xs font-semibold">
                  <td className="px-4 py-2" colSpan={4}>合计</td>
                  <td className="px-2 py-2 text-right font-mono tabular-nums">{totalMv != null ? fmtNum(totalMv) : '—'}</td>
                  <td className="px-2 py-2 text-right font-mono tabular-nums">{totalMv != null ? '100.0%' : '—'}</td>
                  <td className={cn('px-2 py-2 text-right font-mono tabular-nums', priceColorClass(totals?.unrealized_pnl))}>
                    {totals?.unrealized_pnl != null ? `${totals.unrealized_pnl >= 0 ? '+' : ''}${fmtNum(totals.unrealized_pnl)}` : '—'}
                  </td>
                  <td className={cn('px-2 py-2 text-right font-mono tabular-nums', priceColorClass(totalPnlPct))}>
                    {totalPnlPct != null ? `${totalPnlPct >= 0 ? '+' : ''}${totalPnlPct.toFixed(2)}%` : '—'}
                  </td>
                </tr>
              </tfoot>
            </table>
          ) : (
            <EmptyState
              icon={Briefcase}
              title="还没有持仓"
              hint="点「从 DSA 导入」把旧账一次性搬过来，或「记一笔」开始记新账。"
            />
          )}
        </section>

        {/* 流水面板 */}
        <section className="rounded-card border border-border bg-surface overflow-hidden">
          <div className="px-4 py-2.5 border-b border-border text-xs font-medium text-secondary">
            买卖流水（{tradeItems.length}）
          </div>
          {tradeItems.length > 0 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted border-b border-border">
                  <th className="px-4 py-2 font-medium whitespace-nowrap">时间</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap">代码</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap">方向</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">数量</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">价格</th>
                  <th className="px-2 py-2 font-medium whitespace-nowrap text-right">费用</th>
                  <th className="px-2 py-2 font-medium">备注</th>
                  <th className="px-2 py-2 w-10" />
                </tr>
              </thead>
              <tbody>
                {tradeItems.map((t: PortfolioTrade) => (
                  <tr key={t.id} className="border-b border-border/50 hover:bg-elevated/40 transition-colors">
                    <td className="px-4 py-2.5 text-secondary whitespace-nowrap">{fmtTime(t.traded_at)}</td>
                    <td className="px-2 py-2.5 font-medium">{t.symbol}</td>
                    <td className="px-2 py-2.5">
                      <span className={cn('px-1.5 py-0.5 rounded text-[11px]', t.side === 'buy' ? 'bg-bull/10 text-bull' : 'bg-bear/10 text-bear')}>
                        {t.side === 'buy' ? '买入' : '卖出'}
                      </span>
                    </td>
                    <td className="px-2 py-2.5 text-right">{fmtNum(t.quantity, 0)}</td>
                    <td className="px-2 py-2.5 text-right">{fmtNum(t.price)}</td>
                    <td className="px-2 py-2.5 text-right">{t.fee != null ? fmtNum(t.fee) : '—'}</td>
                    <td className="px-2 py-2.5 text-muted max-w-40 truncate">{t.note || '—'}</td>
                    <td className="px-2 py-2.5">
                      <button
                        type="button"
                        aria-label={`删除流水 ${t.symbol}`}
                        className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 transition-colors"
                        onClick={async () => {
                          await dsaApi.portfolioTradeDelete(t.id)
                          toast('流水已删除', 'success')
                          invalidate()
                        }}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      {showTrade && (
        <TradeDialog
          onClose={() => setShowTrade(false)}
          onSaved={() => {
            setShowTrade(false)
            invalidate()
          }}
        />
      )}
      {showImport && (
        <ImportDialog
          onClose={() => setShowImport(false)}
          onDone={() => {
            setShowImport(false)
            invalidate()
          }}
        />
      )}
    </div>
  )
}
