export const DOC_SECTIONS = [
  { id: 'quick-start', title: '快速开始' },
  { id: 'authentication', title: '密钥与鉴权' },
  { id: 'endpoints', title: '接口目录' },
  { id: 'pagination', title: '分页与筛选' },
  { id: 'responses', title: '响应与时间' },
  { id: 'errors', title: '错误与限流' },
  { id: 'downloads', title: '封面与全文导出' },
  { id: 'resources', title: '相关入口' },
]

interface Endpoint { path: string; description: string; extra?: string }
interface EndpointGroup { title: string; endpoints: Endpoint[] }

// 对应实际开放路由；全部为 GET，列表的公共分页参数在正文说明。
export const ENDPOINT_GROUPS: EndpointGroup[] = [
  { title: '接入检查', endpoints: [
    { path: '/me', description: '当前密钥的名称、有效期与授权范围' },
  ] },
  { title: '小说与章节', endpoints: [
    { path: '/novels', description: '已授权小说列表、简介与章节统计', extra: 'q、status' },
    { path: '/novels/{novel_id}', description: '小说信息，直接返回小说对象' },
    { path: '/novels/{novel_id}/chapters', description: '分卷目录、字数与状态，不带正文', extra: 'status、updated_after' },
    { path: '/novels/{novel_id}/chapters/{chapter_id}', description: '单章正文与目录元信息' },
    { path: '/novels/{novel_id}/cover', description: '已保存封面的原始图片字节' },
    { path: '/novels/{novel_id}/export', description: 'TXT、Markdown 或 EPUB 全文文件', extra: 'format、chapter_ids、include_synopsis' },
  ] },
  { title: '人物与世界观', endpoints: [
    { path: '/novels/{novel_id}/characters', description: '当前保存的人物档案列表', extra: 'q（姓名）' },
    { path: '/novels/{novel_id}/characters/{character_id}', description: '人物详情、别名与当前状态' },
    { path: '/novels/{novel_id}/world', description: '世界观条目列表', extra: 'category' },
    { path: '/novels/{novel_id}/world/{setting_id}', description: '世界观条目详情' },
  ] },
  { title: '大纲与细纲', endpoints: [
    { path: '/novels/{novel_id}/outlines', description: '带分卷信息的大纲列表', extra: 'volume_no' },
    { path: '/novels/{novel_id}/outlines/{outline_id}', description: '大纲详情' },
    { path: '/novels/{novel_id}/outlines/{outline_id}/plans', description: '大纲下的章节细纲及已建章引用' },
  ] },
  { title: '组织与成员', endpoints: [
    { path: '/novels/{novel_id}/organizations', description: '组织列表与成员总数', extra: 'q（名称）' },
    { path: '/novels/{novel_id}/organizations/{organization_id}', description: '组织详情及上级组织引用' },
    { path: '/novels/{novel_id}/organizations/{organization_id}/members', description: '组织成员、职位与当前状态', extra: 'status' },
  ] },
  { title: '人物关系', endpoints: [
    { path: '/novels/{novel_id}/relationships', description: '关系列表与双方称呼', extra: 'character_id、status、category' },
    { path: '/novels/{novel_id}/relationships/{relationship_id}', description: '单条关系详情' },
    { path: '/novels/{novel_id}/relationship-types', description: '全局预置与当前小说自定义关系类型' },
  ] },
]

export const CHAPTER_PAGE_EXAMPLE = `{
  "items": [
    {
      "id": 305,
      "novel_id": 42,
      "created_at": "2026-10-30T02:00:00Z",
      "updated_at": "2026-10-30T03:00:00Z",
      "chapter_no": 3,
      "title": "第三章 雨后",
      "status": "draft",
      "word_count": 4,
      "volume_no": 1,
      "volume_title": "初见"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}`

export const CHAPTER_DETAIL_EXAMPLE = `{
  "id": 305,
  "novel_id": 42,
  "created_at": "2026-10-30T02:00:00Z",
  "updated_at": "2026-10-30T03:00:00Z",
  "chapter_no": 3,
  "title": "第三章 雨后",
  "status": "draft",
  "word_count": 4,
  "volume_no": 1,
  "volume_title": "初见",
  "content": "雨停了。"
}`

export const VALIDATION_EXAMPLE = `{
  "detail": "请求参数校验失败：page: Input should be greater than or equal to 1",
  "errors": [
    {
      "type": "greater_than_equal",
      "loc": ["query", "page"],
      "msg": "Input should be greater than or equal to 1"
    }
  ]
}`
