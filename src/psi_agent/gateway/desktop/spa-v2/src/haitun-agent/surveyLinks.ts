import type { Language } from '../i18n'

/** 中文版体验反馈问卷（Feishu 表单）。 */
export const SURVEY_URL_ZH =
  'https://genuineknowledge.feishu.cn/share/base/form/shrcn7pp47SeGec2M4Srnbt75Rg?from=navigation'

/** 英文版体验反馈问卷（运营提供）。 */
export const SURVEY_URL_EN =
  'https://genuineknowledge.feishu.cn/share/base/form/shrcnWL8QaqtPAZSpA8AqKiSmdb?from=navigation'

/** 按界面语言返回问卷链接；英文版本缺失时回退中文。 */
export function surveyUrlFor(language: Language): string {
  return language === 'en-US' && SURVEY_URL_EN.trim() ? SURVEY_URL_EN : SURVEY_URL_ZH
}
