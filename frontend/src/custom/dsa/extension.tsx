import { Briefcase, Crosshair } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { WatchPlanPage } from './pages/WatchPlanPage'
import { PortfolioPage } from './pages/PortfolioPage'

const extension: FrontendExtension = {
  id: 'dsa.migration',
  apiVersion: 1,
  routes: [
    { id: 'dsa-watch', path: '/dsa/watch', component: WatchPlanPage },
    { id: 'dsa-portfolio', path: '/dsa/portfolio', component: PortfolioPage },
  ],
  navigation: [
    { id: 'dsa-watch', routeId: 'dsa-watch', label: '次日盯盘', icon: Crosshair },
    { id: 'dsa-portfolio', routeId: 'dsa-portfolio', label: '持仓', icon: Briefcase },
  ],
}

export default extension
