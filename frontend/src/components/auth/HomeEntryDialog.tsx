import { ArrowRightOutlined, BookOutlined } from '@ant-design/icons'
import { Button, Modal } from 'antd'
import { useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '../../stores/auth'
import { StudioBrand } from '../StudioUI'
import './HomeEntryDialog.css'

interface Props { mode: 'login' | 'register' | null; onClose: () => void }

/** 首页欢迎入口；认证与注册开关仍使用真实 store，不提供体验账号。 */
export default function HomeEntryDialog({ mode, onClose }: Props) {
  const status = useAuthStore((s) => s.status)
  const registrationEnabled = useAuthStore((s) => s.registrationEnabled)
  const navigate = useNavigate()
  const previewRequested = useRef(false)
  const registering = mode === 'register' && registrationEnabled
  const headline = registering ? '你的故事，从这一页开始。' : '欢迎回到你的书桌。'

  return <Modal
    open={mode !== null && status !== 'authenticated'}
    width={460}
    title={<div className="home-entry-brand"><StudioBrand onClick={onClose} /><span className="home-entry-accessible-title">{headline}</span></div>}
    footer={null}
    onCancel={onClose}
    closable={{ 'aria-label': '关闭登录入口' }}
    maskClosable
    keyboard
    focusTriggerAfterClose
    destroyOnHidden
    rootClassName="home-entry-root"
    className="home-entry-dialog"
    afterClose={() => {
      if (!previewRequested.current) return
      previewRequested.current = false
      const section = document.getElementById('stories')
      section?.focus({ preventScroll: true })
      section?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
    }}
  >
    <h2 className="home-entry-headline">{headline}</h2>
    <p className="home-entry-description">在创作空间里，留住灵感、构思人物，写下那个一直想讲的故事。</p>
    <div className="home-entry-info"><span><BookOutlined aria-hidden /></span><div><p>你的私人创作书房</p><small>留住灵感 · 构思世界 · 专注落笔</small></div></div>
    <Button type="primary" block className="home-entry-primary" disabled={status === 'unknown'} onClick={() => { onClose(); navigate(registering ? '/login?tab=register' : '/login') }}>{registering ? '创建账户' : '去登录'}<ArrowRightOutlined /></Button>
    <button type="button" className="home-entry-secondary" onClick={() => { previewRequested.current = true; onClose() }}>先看看创作方向</button>
  </Modal>
}
