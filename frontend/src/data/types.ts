/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
  /** 登记弹窗只收集这些字段，其余为系统生成字段（如人群规模、确认版本） */
  createFields?: string[]
  /** 动作按钮仅在行状态命中时展示，避免在错误状态触发必然失败的动作 */
  actionStatus?: Record<string, string[]>
  /** 列表中以结构化 JSON 展示的字段（成员来源等数组/对象） */
  jsonFields?: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
