import type { FormRule } from 'antd'

// 账号表单校验规则（与后端 Credentials 一致），登录/注册/改密/用户管理共用

export const usernameRules: FormRule[] = [
  { required: true, message: '请输入用户名' },
  { min: 3, max: 32, message: '用户名长度 3–32 个字符' },
  { pattern: /^\S+$/, message: '用户名不能包含空白字符' },
]

export const passwordRules: FormRule[] = [
  { required: true, message: '请输入密码' },
  { min: 6, max: 72, message: '密码长度 6–72 个字符' },
]

/** 「再次输入密码」：必须与 field 字段的值一致（表单项需声明 dependencies={[field]}）。 */
export function confirmPasswordRules(field = 'password'): FormRule[] {
  return [
    { required: true, message: '请再次输入密码' },
    ({ getFieldValue }) => ({
      validator: (_, value: string | undefined) =>
        !value || getFieldValue(field) === value
          ? Promise.resolve()
          : Promise.reject(new Error('两次输入的密码不一致')),
    }),
  ]
}
