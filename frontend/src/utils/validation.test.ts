import type { FormInstance, FormRule } from 'antd'
import { describe, expect, it } from 'vitest'
import { confirmPasswordRules, passwordRules, usernameRules } from './validation'

type ValidatorRule = { validator: (rule: unknown, value: string | undefined) => Promise<void> }

function confirmValidator(field: string, values: Record<string, string>): ValidatorRule['validator'] {
  const factory = confirmPasswordRules(field)[1]
  if (typeof factory !== 'function') throw new Error('expected rule factory')
  const form = { getFieldValue: (name: string) => values[name] } as unknown as FormInstance
  return (factory(form) as ValidatorRule).validator
}

describe('账号校验规则', () => {
  it('与后端 Credentials 一致', () => {
    expect(usernameRules).toContainEqual({ min: 3, max: 32, message: '用户名长度 3–32 个字符' })
    expect(passwordRules).toContainEqual({ min: 6, max: 72, message: '密码长度 6–72 个字符' })
    const pattern = (usernameRules as Array<FormRule & { pattern?: RegExp }>).find((r) => r.pattern)?.pattern
    expect(pattern?.test('alice')).toBe(true)
    expect(pattern?.test('a b')).toBe(false)
  })

  it('确认密码需与指定字段一致', async () => {
    const validate = confirmValidator('new_password', { new_password: 'secret1' })
    await expect(validate({}, 'secret1')).resolves.toBeUndefined()
    await expect(validate({}, 'secret2')).rejects.toThrow('两次输入的密码不一致')
    // 空值交给 required 规则提示
    await expect(validate({}, undefined)).resolves.toBeUndefined()
  })
})
