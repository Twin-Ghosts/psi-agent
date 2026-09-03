/**
 * Cursor-style turn progress log.
 *
 * Sealed ``lines`` = brief activity summaries (读取 `foo.py`, 执行命令…).
 * Trailing ``current`` = only the live state (规划下一步… / 撰写回复…).
 * Never push「规划下一步」into ``lines`` — that stays the live trailer, like
 * Cursor's "Planning next moves" under the short summaries above it.
 */

import { translate, DEFAULT_LANGUAGE, type Language } from '../i18n'

const TOOL_CALL_FULL = /\[Tool Call:\s*([A-Za-z0-9_.-]+)\(([\s\S]*)\)\]\s*$/

/** Fixed vocabulary for the live trailer only. */
export const TURN_PROGRESS = {
  planning: '规划下一步…',
  writing: '撰写回复…',
  toolGeneric: '调用工具',
} as const

export type ProgressLog = {
  /** Brief sealed summaries above the live trailer (Cursor activity lines). */
  lines: string[]
  /** Live trailer only — 规划下一步… / 撰写回复…. */
  current: string
}

export function progressLogStart(language: Language = DEFAULT_LANGUAGE): ProgressLog {
  return { lines: [], current: translate(language, 'turn.planning') }
}

function basename(path: string): string {
  const normalized = path.replace(/\\/g, '/').replace(/\/+$/, '')
  const parts = normalized.split('/')
  return parts[parts.length - 1] || path
}

function asString(v: unknown): string {
  return typeof v === 'string' ? v.trim() : ''
}

function parseToolArgs(raw: string): Record<string, unknown> {
  try {
    const v = JSON.parse(raw) as unknown
    return v && typeof v === 'object' && !Array.isArray(v)
      ? (v as Record<string, unknown>)
      : {}
  } catch {
    return {}
  }
}

function pathArg(args: Record<string, unknown>): string {
  return (
    asString(args.path)
    || asString(args.file)
    || asString(args.filename)
    || asString(args.target)
    || asString(args.file_path)
  )
}

function queryArg(args: Record<string, unknown>): string {
  return asString(args.query) || asString(args.pattern) || asString(args.q) || asString(args.glob)
}

function commandArg(args: Record<string, unknown>): string {
  return asString(args.command) || asString(args.cmd) || asString(args.c)
}

/** Brief Cursor-like one-liner for a tool call (no raw JSON, no planning trailer). */
export function summarizeToolCall(
  name: string,
  argsRaw: string,
  language: Language = DEFAULT_LANGUAGE,
): string {
  const key = name.trim().toLowerCase()
  const args = parseToolArgs(argsRaw)
  const path = pathArg(args)
  const file = path ? basename(path) : ''
  const query = queryArg(args)
  const cmd = commandArg(args)

  if (key === 'read') return file ? translate(language, 'turn.readFile', { file }) : translate(language, 'turn.readFiles')
  if (key === 'write') return file ? translate(language, 'turn.writeFile', { file }) : translate(language, 'turn.writeFiles')
  if (key === 'edit') return file ? translate(language, 'turn.editFile', { file }) : translate(language, 'turn.editFiles')
  if (key === 'list_dir') return file ? translate(language, 'turn.listDirFile', { file }) : translate(language, 'turn.listDir')
  if (key === 'find_files') return query ? translate(language, 'turn.findFilesQuery', { query }) : translate(language, 'turn.findFiles')
  if (key === 'bash' || key === 'powershell') {
    if (!cmd) return translate(language, 'turn.runCommand')
    const short = cmd.length > 36 ? `${cmd.slice(0, 36)}…` : cmd
    return translate(language, 'turn.runCommandShort', { cmd: short })
  }
  if (key === 'todo') return translate(language, 'turn.updateTodo')
  if (key === 'search' || key === 'web_search') {
    return query ? translate(language, 'turn.searchQuery', { query }) : translate(language, 'turn.search')
  }
  if (key === 'fetch') return translate(language, 'turn.fetchPage')
  if (key === 'clarify') return translate(language, 'turn.waitingConfirm')
  if (key === 'skill_manage') return translate(language, 'turn.manageSkill')
  if (key === 'schedule_manage') return translate(language, 'turn.manageSchedule')
  if (key === 'flow_manage') return translate(language, 'turn.orchestrateFlow')

  const prefix = key.split('_')[0] ?? key
  if (prefix === 'browser') return translate(language, 'turn.browsePage')
  if (prefix === 'feishu') return translate(language, 'turn.feishuOp')
  if (prefix === 'wiki' || prefix === 'goal') return translate(language, 'turn.updateKnowledge')
  if (prefix === 'memory') return translate(language, 'turn.memoryOp')
  if (prefix === 'session' || prefix === 'sessions') return translate(language, 'turn.sessionOp')
  if (!key) return translate(language, 'turn.toolGeneric')
  return translate(language, 'turn.callTool', { name })
}

export function summarizeToolCallText(
  text: string,
  language: Language = DEFAULT_LANGUAGE,
): string | null {
  const m = text.match(TOOL_CALL_FULL) ?? text.match(/\[Tool Call:\s*([A-Za-z0-9_.-]+)/)
  if (!m) return null
  return summarizeToolCall(m[1] ?? '', m[2] ?? '{}', language)
}

/** @deprecated prefer summarizeToolCall — kept for call sites that only have a name. */
export function labelForToolName(name: string, language: Language = DEFAULT_LANGUAGE): string {
  return summarizeToolCall(name, '{}', language)
}

function pushSummary(log: ProgressLog, summary: string, language: Language): ProgressLog {
  const last = log.lines[log.lines.length - 1]
  if (last === summary) {
    return { lines: log.lines, current: translate(language, 'turn.planning') }
  }
  return {
    lines: [...log.lines, summary],
    current: translate(language, 'turn.planning'),
  }
}

/**
 * Apply a reasoning / content signal.
 * thinking → trailer 规划下一步 (never a sealed line).
 * tool_call → seal a brief summary line; trailer stays 规划下一步.
 * tool_result → trailer 规划下一步 (no line).
 * content → trailer 撰写回复 (do not seal 规划下一步 into history).
 */
export function applyProgressEvent(
  log: ProgressLog,
  kind: string | undefined,
  text: string,
  language: Language = DEFAULT_LANGUAGE,
): ProgressLog {
  if (kind === 'tool_call') {
    const summary = summarizeToolCallText(text, language) ?? translate(language, 'turn.toolGeneric')
    return pushSummary(log, summary, language)
  }
  if (kind === 'tool_result') {
    return { lines: log.lines, current: translate(language, 'turn.planning') }
  }
  if (kind === 'content') {
    return { lines: log.lines, current: translate(language, 'turn.writing') }
  }
  // thinking / unknown — live trailer only
  if (log.current === translate(language, 'turn.writing')) {
    return { lines: log.lines, current: translate(language, 'turn.writing') }
  }
  return { lines: log.lines, current: translate(language, 'turn.planning') }
}
