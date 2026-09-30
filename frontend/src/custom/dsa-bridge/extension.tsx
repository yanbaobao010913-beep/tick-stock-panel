/**
 * DSA 迁移可行性探针前端: 注册 /dsa-bridge 路由与导航项,
 * 展示后端 /api/ext/dsa/health 的真实只读桥接结果.
 */
import { useEffect, useState } from 'react'
import { Plug } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { api } from '@/lib/api'

interface DsaBridgeHealth {
  status: string
  dsa_db_found: boolean
  analysis_history_count: number | null
}

function DsaBridgePage() {
  const [health, setHealth] = useState<DsaBridgeHealth | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .dsaBridgeHealth()
      .then((data) => {
        if (!cancelled) setHealth(data)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="p-4 space-y-4">
      <h1 className="text-lg font-semibold">DSA 桥接探针</h1>
      {error && <div className="text-sm text-danger">请求失败: {error}</div>}
      {!health && !error && <div className="text-sm text-muted">加载中…</div>}
      {health && (
        <div className="space-y-2">
          <div className="text-4xl font-bold tabular-nums">
            {health.analysis_history_count ?? '—'}
          </div>
          <div className="text-xs text-muted">analysis_history 记录数（DSA 只读库）</div>
          <div className="text-sm">
            DSA 数据库:{' '}
            <span className={health.dsa_db_found ? 'text-success' : 'text-danger'}>
              {health.dsa_db_found ? '已连接' : '未找到'}
            </span>
          </div>
        </div>
      )}
    </div>
  )
}

const extension: FrontendExtension = {
  id: 'dsa.bridge',
  apiVersion: 1,
  routes: [{ id: 'dsa-bridge', path: '/dsa-bridge', component: DsaBridgePage }],
  navigation: [
    { id: 'dsa-bridge', routeId: 'dsa-bridge', label: 'DSA桥接', icon: Plug, order: 900 },
  ],
}

export default extension
