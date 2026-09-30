import { Briefcase, Crosshair, TrendingUp } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { WatchPlanPage } from './pages/WatchPlanPage'
import { PortfolioPage } from './pages/PortfolioPage'
import { PaperSimPage } from './pages/PaperSimPage'

const extension: FrontendExtension = {
  id: 'dsa.migration',
  apiVersion: 1,
  routes: [
    { id: 'dsa-watch', path: '/dsa/watch', component: WatchPlanPage },
    { id: 'dsa-portfolio', path: '/dsa/portfolio', component: PortfolioPage },
    { id: 'dsa-paper', path: '/dsa/paper', component: PaperSimPage },
  ],
  navigation: [
    { id: 'dsa-watch', routeId: 'dsa-watch', label: '次日盯盘', icon: Crosshair },
    { id: 'dsa-portfolio', routeId: 'dsa-portfolio', label: '持仓', icon: Briefcase },
    { id: 'dsa-paper', routeId: 'dsa-paper', label: '点位模拟', icon: TrendingUp },
  ],
}

export default extension
