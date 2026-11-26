/**
 * 人群圈选器前端领域逻辑：与后端 cohort_logic.parse_condition 保持同一套口径。
 *
 * 页面先用它做输入校验并明确区分两类失败：
 * - 条件缺失：条件为空，无法生成人群；
 * - 条件无法解析：格式错误、缺少实验编码、实验/分组非法。
 * 服务端会再次校验，前端校验只为提前给出可修正的提示。
 */
export type CohortConditionScope = {
  实验编码: string
  分组名称?: string
  用户标识?: string
  [key: string]: string | undefined
}

export type CohortConditionResult = {
  ok: boolean
  /** 失败原因（条件缺失 / 条件无法解析），成功时为空串 */
  reason: string
  scope?: CohortConditionScope
}

const GROUP_NAMES = ['对照组', '实验组']

export function parseCohortCondition(raw: unknown): CohortConditionResult {
  const text = String(raw ?? '').trim()
  if (!text) {
    return { ok: false, reason: '条件缺失：圈选条件为空，无法生成人群' }
  }

  let scope: Record<string, string>
  if (text.startsWith('{')) {
    let loaded: unknown
    try {
      loaded = JSON.parse(text)
    } catch {
      return { ok: false, reason: '条件无法解析：JSON 格式错误' }
    }
    if (typeof loaded !== 'object' || loaded === null || Array.isArray(loaded)) {
      return { ok: false, reason: '条件无法解析：条件必须是键值对象' }
    }
    scope = {}
    for (const [key, value] of Object.entries(loaded as Record<string, unknown>)) {
      const strValue = String(value ?? '').trim()
      if (strValue) scope[key.trim()] = strValue
    }
  } else {
    scope = {}
    for (const part of text.replace(/[，,]/g, ';').split(';')) {
      const token = part.trim()
      if (!token) continue
      if (!token.includes('=')) {
        return {
          ok: false,
          reason: '条件无法解析：片段需写成 键=值，例如 实验编码=E1;分组名称=实验组',
        }
      }
      const [key, ...rest] = token.split('=')
      const name = key.trim()
      const value = rest.join('=').trim()
      if (!name || !value) {
        return { ok: false, reason: '条件无法解析：存在空的键或值' }
      }
      scope[name] = value
    }
  }

  if (Object.keys(scope).length === 0) {
    return { ok: false, reason: '条件缺失：圈选条件为空，无法生成人群' }
  }
  if (!scope['实验编码']) {
    return { ok: false, reason: '条件无法解析：缺少必填的 实验编码，无法限定圈选范围' }
  }
  if (scope['分组名称'] && !GROUP_NAMES.includes(scope['分组名称'])) {
    return {
      ok: false,
      reason: `条件无法解析：分组名称 ${scope['分组名称']} 非法，仅支持 对照组 / 实验组`,
    }
  }
  return { ok: true, reason: '', scope: scope as CohortConditionScope }
}

/** 登记/生成前的页面侧校验，返回错误提示；通过返回空串 */
export function validateCohortInput(values: Record<string, string>): string {
  const parsed = parseCohortCondition(values['圈选条件'])
  return parsed.ok ? '' : parsed.reason
}
