import {
  ArrowRightOutlined, ArrowUpOutlined, BookOutlined, BulbOutlined, CheckOutlined,
  ContactsOutlined, EditOutlined, MessageOutlined, MinusOutlined, OrderedListOutlined,
  PlayCircleOutlined, PlusOutlined,
} from '@ant-design/icons'
import { useState, type KeyboardEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { StudioBrand } from '../components/StudioUI'
import HomeEntryDialog from '../components/auth/HomeEntryDialog'
import { useAuthStore } from '../stores/auth'
import './HomePage.css'

// 公开的功能文案与创作示意，不是用户项目、模型输出或作品数据。
const FEATURES = [
  { title: '灵感构思', icon: <BulbOutlined />, label: '从一个念头出发', heading: '如果一座城，能听见所有人的梦', rows: ['故事钩子：唯一不会做梦的少女，听见了整座城的秘密。', '人物目标：找到自己的第一个梦，也找到失踪的母亲。', '情节方向：一封来自梦境的信，带她走进无名之城。'], note: '想法不必完整。一个人、一句话、一场梦，都能成为故事的种子。' },
  { title: '大纲规划', icon: <OrderedListOutlined />, label: '让故事有迹可循', heading: '雨夜到归途 · 故事脉络', rows: ['第一幕 · 雨夜拾灯：修灯师收到一封来自未来的信。', '第二幕 · 无灯之城：她走进长安没有记载的第十三坊。', '第三幕 · 万灯归途：灯灭之时，她认出了自己的字迹。'], note: '把开头、转折与结局慢慢串起来。走得再远，也记得故事最初的方向。' },
  { title: '角色塑造', icon: <ContactsOutlined />, label: '让每个人物鲜活起来', heading: '一位修灯师 · 人物的内心世界', rows: ['她想要什么：找到失踪七年的父亲，修好他留下的灯。', '她害怕什么：灯亮的那一天，等待也会一起结束。', '她如何成长：从守着别人的答案，到点亮自己的路。'], note: '从外在目标到内心矛盾，陪你认识笔下的人，让每一次选择都有来处。' },
  { title: '正文协作', icon: <EditOutlined />, label: '陪你跨过空白的一页', heading: '月下归人 · 一个开头的可能', rows: ['长安的雨，下到第三日才停。', '她伸出手，拂去灯罩上薄薄的灰。七年了，那盏灯终于微微亮了一下。', '门外的人轻轻叩了三下。是父亲每次归家时的习惯。'], note: '续写一段，换个说法，试一种节奏。灵感是搭档，最后落笔的人始终是你。' },
]
const STEPS = [
  { number: '01', title: '留住一个念头', text: '记下一句话，选一个故事类型。你不需要一开始，就想好全部的世界。' },
  { number: '02', title: '一起搭建世界', text: '梳理故事大纲、认识笔下的人，让散落的灵感，长成清晰的脉络。' },
  { number: '03', title: '写下你的章节', text: '在专注的创作台里落笔，让 AI 提供一些可能，再写成自己的表达。' },
]
const DIRECTIONS = [
  { genre: '东方玄幻', lines: ['东方', '玄幻'], tone: 'sage', excerpt: '「等灯亮的时候，就去城南找我。」她记了七年。今夜，门外终于响起了脚步声。' },
  { genre: '科幻幻想', lines: ['科幻', '幻想'], tone: 'night', excerpt: '地球停止接收来信的第三十年，她在一颗废弃星球上，听见了母亲年轻时的声音。' },
  { genre: '奇幻言情', lines: ['奇幻', '言情'], tone: 'sand', excerpt: '每当那棵树开花，她就会忘记一个人。这一次，花瓣上却出现了自己的名字。' },
]
const QUESTIONS = [
  { question: '没有写作经验，也可以开始吗？', answer: '可以。从一句想法开始，先构思人物和大纲，再一点点写出章节。你可以按自己的节奏来。' },
  { question: 'AI 会替我决定故事的走向吗？', answer: '故事由你主导。每个建议都可以修改、舍弃或重写，最后留下的，是你想表达的那个世界。' },
  { question: '可以先看看实际的写作界面吗？', answer: '可以。登录后即可进入创作空间，从新建作品开始，体验设定、大纲与正文编辑。' },
]

/** 注册开关与登录态来自现有认证 store；认证未完成时不提前提供注册入口。 */
function CreationEntry({ children, className, signedInLabel, onEnter }: { children: ReactNode; className: string; signedInLabel?: string; onEnter: (mode: 'login' | 'register') => void }) {
  const status = useAuthStore((s) => s.status)
  const registrationEnabled = useAuthStore((s) => s.registrationEnabled)
  const label = status === 'authenticated' ? signedInLabel ?? children : registrationEnabled ? children : '登录开始创作'
  const content = <>{label}<ArrowRightOutlined /></>
  if (status === 'unknown') return <button type="button" className={className} disabled aria-busy="true">{children}<ArrowRightOutlined /></button>
  if (status === 'authenticated') return <Link className={className} to="/projects?create=1">{content}</Link>
  return <button type="button" className={className} onClick={() => onEnter(registrationEnabled ? 'register' : 'login')}>{content}</button>
}

/** 选项卡遵循方向键、Home/End 与单个 Tab 焦点的键盘交互。 */
function moveTab(event: KeyboardEvent<HTMLButtonElement>, index: number, total: number, select: (index: number) => void) {
  if (event.altKey || event.ctrlKey || event.metaKey) return
  let next: number
  if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = (index + 1) % total
  else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = (index - 1 + total) % total
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = total - 1
  else return
  event.preventDefault()
  select(next)
  event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus()
}

/** CSS 书本与稿纸插画，仅用于呈现创作氛围。 */
function HeroIllustration() {
  return <div className="home-illustration" aria-hidden="true">
    <div className="home-illustration-scene">
      <div className="home-orbit" /><div className="home-sun" />
      <div className="home-hero-book"><div className="home-book-arch" /><p>山海<br />有声</p><span>每个世界，等你落笔</span></div>
      <div className="home-manuscript"><p className="home-manuscript-label">创作手稿 · 功能示意</p><h2>故事的开头，<br />可以是任何样子。</h2><p className="home-manuscript-prose">一封未曾寄出的信，一座只在雨夜出现的城。她推开窗时，春天恰好落在了纸上。</p><div className="home-manuscript-rule" /><p className="home-manuscript-note">你落下第一笔，<br />世界便有了回声。</p></div>
    </div>
    <div className="home-idea-note"><span><BulbOutlined /></span><div><p>一个念头，更多种可能。</p><small>下一句，和灵感一起想。</small></div></div>
  </div>
}

function Capabilities() {
  const [feature, setFeature] = useState(1)
  const selected = FEATURES[feature]
  return <section id="features" className="home-capabilities home-section" aria-labelledby="home-features-heading">
    <div className="home-section-heading"><h2 id="home-features-heading">从「有个想法」，<br />到「有个世界」。</h2><p>创作不只有落笔的那一刻。<br />从最初的构思到每一处细节，让灵感找到落脚的地方。</p></div>
    <div className="home-feature-layout">
      <div className="home-feature-tabs" role="tablist" aria-label="了解创作能力" aria-orientation="vertical">{FEATURES.map((item, index) => <button key={item.title} type="button" role="tab" id={`home-feature-tab-${index}`} aria-selected={feature === index} aria-controls="home-feature-preview" tabIndex={feature === index ? 0 : -1} onClick={() => setFeature(index)} onKeyDown={(event) => moveTab(event, index, FEATURES.length, setFeature)}>{item.icon}{item.title}{feature === index && <ArrowRightOutlined />}</button>)}</div>
      <div role="tabpanel" id="home-feature-preview" aria-labelledby={`home-feature-tab-${feature}`} tabIndex={0} className="home-feature-preview">
        <div className="home-feature-content"><div className="home-feature-label"><p>{selected.label}</p><span>功能示意</span></div><h3>{selected.heading}</h3><ol>{selected.rows.map((row, index) => <li key={row}><span>{String(index + 1).padStart(2, '0')}</span><p>{row}</p></li>)}</ol></div>
        <aside className="home-feature-partner"><MessageOutlined /><h3>你的灵感搭档</h3><p>{selected.note}</p><Link to="/projects">在创作台体验<ArrowUpOutlined rotate={45} /></Link></aside>
      </div>
    </div>
  </section>
}

function StoryDirections() {
  const [sample, setSample] = useState(0)
  return <section id="stories" className="home-stories home-section" aria-labelledby="home-stories-heading" tabIndex={-1}>
    <div className="home-story-intro"><p className="home-eyebrow">创作方向示例</p><h2 id="home-stories-heading">你的想象，<br />不必只有一种模样。</h2><p className="home-story-excerpt" role="tabpanel" id="home-direction-preview" aria-labelledby={`home-direction-tab-${sample}`} tabIndex={0}>{DIRECTIONS[sample].excerpt}</p>
      <div className="home-direction-tabs" role="tablist" aria-label="查看不同故事类型">{DIRECTIONS.map((item, index) => <button key={item.genre} type="button" role="tab" id={`home-direction-tab-${index}`} aria-selected={sample === index} aria-controls="home-direction-preview" tabIndex={sample === index ? 0 : -1} onClick={() => setSample(index)} onKeyDown={(event) => moveTab(event, index, DIRECTIONS.length, setSample)}>{item.genre}</button>)}</div>
    </div>
    <div className="home-direction-books">{DIRECTIONS.map((item, index) => <button key={item.genre} type="button" className={`home-direction-book home-direction-${item.tone}`} aria-label={`查看${item.genre}创作方向示例`} aria-pressed={sample === index} onClick={() => setSample(index)}><span className="home-jacket" aria-hidden="true"><i className="home-jacket-arch" /><i className="home-jacket-sun" /><i className="home-jacket-mountain" /><span className="home-jacket-title">{item.lines.map((line) => <span key={line}>{line}</span>)}</span><small>创作方向</small></span><span className="home-direction-caption">{item.genre}</span></button>)}</div>
  </section>
}

function Questions({ onEnter }: { onEnter: (mode: 'login' | 'register') => void }) {
  const [question, setQuestion] = useState<number | null>(0)
  return <section id="questions" className="home-questions home-section" aria-labelledby="home-questions-heading">
    <div className="home-final-invitation"><BookOutlined aria-hidden /><h2>好故事，<br />值得一个开始。</h2><p>那个在心里很久的世界，今天就写下来吧。</p><CreationEntry className="home-button home-button-gold" signedInLabel="开始一个新故事" onEnter={onEnter}>写下我的第一章</CreationEntry></div>
    <div className="home-faq"><h2 id="home-questions-heading">开始之前，你可能想知道</h2><div>{QUESTIONS.map((item, index) => <div className="home-faq-item" key={item.question}><h3><button type="button" id={`home-question-${index}`} aria-expanded={question === index} aria-controls={`home-answer-${index}`} onClick={() => setQuestion(question === index ? null : index)}>{item.question}{question === index ? <MinusOutlined /> : <PlusOutlined />}</button></h3><p id={`home-answer-${index}`} aria-labelledby={`home-question-${index}`} className="home-faq-answer" hidden={question !== index}>{item.answer}</p></div>)}</div></div>
  </section>
}

/** 独立公开官网首页：只读取认证状态，不请求或展示用户私有作品。 */
export default function HomePage() {
  const status = useAuthStore((s) => s.status)
  const [entry, setEntry] = useState<'login' | 'register' | null>(null)
  return <div className="public-home" id="home-top">
    <header className="home-header home-section"><StudioBrand /><nav className="home-navigation" aria-label="官网导航"><a href="#features">创作能力</a><a href="#steps">如何开始</a><a href="#stories">故事示例</a><a href="#questions">常见问题</a></nav><div className="home-header-entry">{status === 'authenticated' ? <Link className="home-login-link" to="/projects">进入空间</Link> : <button type="button" className="home-login-link" disabled={status === 'unknown'} onClick={() => setEntry('login')}>登录</button>}<CreationEntry className="home-button home-header-create" signedInLabel="新建作品" onEnter={setEntry}>开始创作</CreationEntry></div></header>
    <main>
      <section className="home-hero home-section" aria-labelledby="home-title"><div className="home-hero-copy"><p className="home-eyebrow"><EditOutlined />为每一个想写故事的人</p><h1 id="home-title">你的下一部小说，<br /><span>从这里开始。</span></h1><p className="home-hero-description">从一闪而过的灵感，到一个完整的世界。<br />知页陪你理清大纲、塑造角色、写下章节，<br />让脑海中的故事，慢慢拥有自己的模样。</p><div className="home-hero-actions"><CreationEntry className="home-button home-hero-create" signedInLabel="开始一个新故事" onEnter={setEntry}>开始我的第一个故事</CreationEntry><Link className="home-workspace-link" to="/projects"><PlayCircleOutlined />看看创作台</Link></div><ul className="home-promises">{['故事由你主导', '灵感随时记录', '专注每一章'].map((item) => <li key={item}><CheckOutlined />{item}</li>)}</ul></div><HeroIllustration /></section>
      <div className="home-capability-band home-section"><p>给想象一方书桌，给故事一个开始。</p><ul>{['灵感构思', '故事大纲', '人物设定', '正文协作'].map((item) => <li key={item}>{item}</li>)}</ul></div>
      <Capabilities />
      <section id="steps" className="home-steps home-section" aria-labelledby="home-steps-heading"><h2 id="home-steps-heading">把故事写出来，从这三步开始。</h2><ol>{STEPS.map((step) => <li key={step.number}><span>{step.number}</span><div><h3>{step.title}</h3><p>{step.text}</p></div></li>)}</ol></section>
      <StoryDirections />
      <Questions onEnter={setEntry} />
    </main>
    <footer className="home-footer home-section"><StudioBrand /><p>知页小说 · 为热爱故事的人而作</p><div><a href="#home-top">回到首页顶部</a><Link to="/projects">进入创作空间<ArrowUpOutlined rotate={45} /></Link></div></footer>
    <HomeEntryDialog mode={entry} onClose={() => setEntry(null)} />
  </div>
}
