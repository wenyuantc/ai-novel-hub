import {
  ApartmentOutlined, ApiOutlined, BgColorsOutlined, BookOutlined, BulbOutlined, ClusterOutlined,
  DashboardOutlined, DatabaseOutlined, FileTextOutlined, GlobalOutlined, HistoryOutlined,
  ImportOutlined, KeyOutlined, OrderedListOutlined, RocketOutlined, SettingOutlined, ShopOutlined, TeamOutlined, TrophyOutlined,
} from '@ant-design/icons'
import type { ReactNode } from 'react'

interface NavItem { to: string; label: string; icon: ReactNode }
export interface NavGroup { title: string; items: NavItem[] }

const GLOBAL_GROUPS: NavGroup[] = [
  { title: '创作空间', items: [
    { to: '/projects', label: '我的作品', icon: <BookOutlined /> },
    { to: '/inspiration', label: '灵感立项', icon: <BulbOutlined /> },
    { to: '/book-import', label: '拆书导入', icon: <ImportOutlined /> },
  ] },
  { title: '创作资源', items: [
    { to: '/settings/ai', label: '模型配置', icon: <SettingOutlined /> },
    { to: '/settings/api-keys', label: 'API 密钥', icon: <KeyOutlined /> },
    { to: '/prompt-templates', label: '提示词模板', icon: <FileTextOutlined /> },
    { to: '/styles', label: '写作风格', icon: <BgColorsOutlined /> },
    { to: '/workshop', label: '提示词工坊', icon: <ShopOutlined /> },
    { to: '/skills', label: '写作技能', icon: <BulbOutlined /> },
    { to: '/mcp-plugins', label: 'MCP 插件', icon: <ApiOutlined /> },
  ] },
]

const PROJECT_GROUPS = [
  { title: '作品', items: [
    { key: 'overview', label: '作品概览', icon: <DashboardOutlined /> },
    { key: 'wizard', label: '创作向导', icon: <RocketOutlined /> },
  ] },
  { title: '故事档案', items: [
    { key: 'world', label: '世界观', icon: <GlobalOutlined /> },
    { key: 'characters', label: '角色', icon: <TeamOutlined /> },
    { key: 'organizations', label: '组织', icon: <ClusterOutlined /> },
    { key: 'relationships', label: '关系', icon: <ApartmentOutlined /> },
    { key: 'careers', label: '职业', icon: <TrophyOutlined /> },
  ] },
  { title: '写作与校验', items: [
    { key: 'outlines', label: '大纲', icon: <OrderedListOutlined /> },
    { key: 'chapters', label: '章节', icon: <BookOutlined /> },
    { key: 'foreshadows', label: '伏笔', icon: <BulbOutlined /> },
    { key: 'mentions', label: '提及追踪', icon: <DashboardOutlined /> },
    { key: 'notes', label: '笔记', icon: <FileTextOutlined /> },
    { key: 'knowledge', label: '知识库', icon: <DatabaseOutlined /> },
    { key: 'memories', label: '剧情记忆', icon: <HistoryOutlined /> },
  ] },
]

/** 桌面与移动导航共用定义；只映射项目已经存在的路由。 */
export function studioNavigation(projectId: number | null, mcpEnabled: boolean, canUseAI = true): NavGroup[] {
  if (projectId !== null) return PROJECT_GROUPS.map((group) => ({ title: group.title, items: group.items.filter((item) => canUseAI || item.key !== 'wizard').map(({ key, ...item }) => ({ ...item, to: `/projects/${projectId}/${key}` })) }))
  return GLOBAL_GROUPS.map((group) => ({ ...group, items: group.items.filter((item) => mcpEnabled || item.to !== '/mcp-plugins') }))
}

export function navigationItem(groups: NavGroup[], path: string) {
  return groups.flatMap((group) => group.items).find((item) => path === item.to || path.startsWith(`${item.to}/`))
}
