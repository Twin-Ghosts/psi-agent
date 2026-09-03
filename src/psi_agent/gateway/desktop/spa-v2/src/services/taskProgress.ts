import type { SessionTodo } from './api'
import type { Task, TaskStep } from '../haitun-agent/model'
import { plainTextPreview } from './assistantDisplay'
import { translate, DEFAULT_LANGUAGE, type Language } from '../i18n'

/** Upper-level lifecycle for a task card (independent of todo UI detail). */
export type TaskPhase = 'advance' | 'deliver' | 'done'

export type ProgressInput = {
  /** Chat SSE turn in flight. */
  streaming: boolean
  /** At least one successful agent reply has settled (this turn or history). */
  turnSettled: boolean
  todos: SessionTodo[]
  hasDeliverables: boolean
}

export type ProgressProjection = {
  phase: TaskPhase
  steps: TaskStep[]
  /** 0–100. Meaningful with todo track or when phase is done; else unused when indeterminate. */
  progress: number
  /** No todo track + in-flight work — pulse UI, not a fake %. */
  indeterminate: boolean
  /** Corner label when todo track exists (e.g. ``2/5``); empty otherwise. */
  progressLabel: string
  /** True when Session has an active (non-cancelled) todo list. */
  hasTodoTrack: boolean
  updated: string
  /** Sidebar「当前阶段」prefer this over digging into steps. */
  phaseLabel: string
}

function activeTodos(todos: SessionTodo[]): SessionTodo[] {
  return todos.filter((t) => t.status !== 'cancelled')
}

function todoMiddle(
  active: SessionTodo[],
): { label: string; detail?: string; done: boolean; completed: number; total: number } {
  const total = active.length
  const completed = active.filter((t) => t.status === 'completed').length
  const inProgIdx = active.findIndex((t) => t.status === 'in_progress')
  if (inProgIdx >= 0) {
    return {
      label: `${inProgIdx + 1}/${total}`,
      detail: active[inProgIdx]?.content,
      done: false,
      completed,
      total,
    }
  }
  if (completed >= total) {
    return { label: `${total}/${total}`, done: true, completed, total }
  }
  const nextIdx = active.findIndex((t) => t.status === 'pending')
  const current = nextIdx >= 0 ? nextIdx + 1 : Math.min(completed + 1, total)
  return {
    label: `${current}/${total}`,
    detail: nextIdx >= 0 ? active[nextIdx]?.content : undefined,
    done: false,
    completed,
    total,
  }
}

function todoStatusToStepState(status: string): TaskStep['state'] {
  if (status === 'completed') return 'done'
  if (status === 'in_progress') return 'working'
  return 'waiting'
}

function activityCopy(input: {
  phase: TaskPhase
  streaming: boolean
  hasDeliverables: boolean
}, language: Language): { label: string; detail?: string; state: TaskStep['state']; updated: string } {
  if (input.phase === 'done') {
    return {
      label: translate(language, 'progress.phaseDone'),
      state: 'done',
      updated: translate(language, 'progress.updatedDone'),
    }
  }
  if (input.phase === 'deliver') {
    return {
      label: translate(language, 'progress.phaseDeliver'),
      detail: input.hasDeliverables ? translate(language, 'progress.deliverDetail') : undefined,
      state: 'working',
      updated: translate(language, 'progress.updatedDelivering'),
    }
  }
  if (input.streaming) {
    return {
      label: translate(language, 'progress.phaseWorking'),
      state: 'working',
      updated: translate(language, 'progress.updatedAgentWorking'),
    }
  }
  return {
    label: translate(language, 'progress.phaseWaiting'),
    detail: translate(language, 'progress.waitingDetail'),
    state: 'waiting',
    updated: translate(language, 'progress.updatedWaiting'),
  }
}

/**
 * Layer 1 — lifecycle phase.
 * Layer 2 — with todos: real checklist steps + N/M; without: single activity line (no fake 3-step track).
 */
export function resolveTaskProgress(
  input: ProgressInput,
  language: Language = DEFAULT_LANGUAGE,
): ProgressProjection {
  const active = activeTodos(input.todos)
  const hasTodoTrack = active.length > 0
  const middle = hasTodoTrack
    ? todoMiddle(active)
    : { label: '', detail: undefined, done: false, completed: 0, total: 0 }

  let phase: TaskPhase
  if (input.streaming) {
    if (hasTodoTrack && middle.done) {
      phase = 'deliver'
    } else if (!hasTodoTrack && input.hasDeliverables) {
      phase = 'deliver'
    } else {
      phase = 'advance'
    }
  } else if (input.turnSettled) {
    phase = 'done'
  } else {
    phase = 'advance'
  }

  let steps: TaskStep[]
  let progress: number
  let indeterminate: boolean
  let progressLabel: string
  let phaseLabel: string
  let updated: string

  if (hasTodoTrack) {
    // Checklist UI always mirrors AppData todo status — chat turnSettled must
    // not paint unfinished items green or inflate N/M / % (真实任务进度).
    steps = active.map((t) => ({
      label: t.content,
      state: todoStatusToStepState(t.status),
    }))
    if (phase === 'deliver') {
      // Only reached when middle.done while streaming — append deliver line.
      steps = [...steps, { label: translate(language, 'progress.checklistDeliver'), state: 'working' }]
    }

    progress = Math.round((middle.completed / Math.max(middle.total, 1)) * 100)
    if (phase === 'deliver') progress = Math.max(progress, 85)
    indeterminate = false
    progressLabel = middle.label
    phaseLabel =
      phase === 'done'
        ? middle.done
          ? translate(language, 'progress.checklistDone')
          : translate(language, 'progress.checklistReplied', { label: middle.label })
        : phase === 'deliver'
          ? translate(language, 'progress.checklistDeliver')
          : middle.detail
            ? `${middle.label} · ${middle.detail}`
            : middle.label
    updated =
      phase === 'done'
        ? middle.done
          ? translate(language, 'progress.updatedChecklistDone')
          : translate(language, 'progress.checklistRepliedWithList', { label: middle.label })
        : phase === 'deliver'
          ? translate(language, 'progress.updatedDelivering')
          : translate(language, 'progress.checklistSynced')
  } else {
    const activity = activityCopy({
      phase,
      streaming: input.streaming,
      hasDeliverables: input.hasDeliverables,
    }, language)
    steps = [{ label: activity.label, state: activity.state, detail: activity.detail }]
    progress = phase === 'done' ? 100 : 0
    indeterminate = phase === 'advance' || phase === 'deliver'
      ? input.streaming || phase === 'deliver'
      : false
    // Idle (not streaming, not settled): not indeterminate — quiet waiting.
    if (phase === 'advance' && !input.streaming) {
      indeterminate = false
    }
    progressLabel = ''
    phaseLabel = activity.label
    updated = activity.updated
  }

  return {
    phase,
    steps,
    progress: Number.isFinite(progress) ? progress : 0,
    indeterminate,
    progressLabel,
    hasTodoTrack,
    updated,
    phaseLabel,
  }
}

export type ApplyProgressPatch = {
  streaming?: boolean
  turnSettled?: boolean
  todos?: SessionTodo[]
  summary?: string
  /** Force hasDeliverables; default derives from task file lists. */
  hasDeliverables?: boolean
}

/** Project phase → steps onto a task (single write path for the card). */
export function applyTaskProgress(
  task: Task,
  patch: ApplyProgressPatch = {},
  language: Language = DEFAULT_LANGUAGE,
): Task {
  const todos = patch.todos ?? task.todoItems ?? []
  const turnSettled = patch.turnSettled !== undefined ? patch.turnSettled : (task.turnSettled ?? false)
  const streaming = patch.streaming === true
  const hasDeliverables =
    patch.hasDeliverables
    ?? (task.newDeliverables.length > 0 || task.deliverables.length > 0)

  const proj = resolveTaskProgress({
    streaming,
    turnSettled,
    todos,
    hasDeliverables,
  }, language)

  const summary = patch.summary?.trim()
  return {
    ...task,
    turnSettled,
    todoItems: patch.todos !== undefined ? patch.todos : task.todoItems,
    phase: proj.phase,
    steps: proj.steps,
    progress: proj.progress,
    progressIndeterminate: proj.indeterminate,
    progressLabel: proj.progressLabel,
    hasTodoTrack: proj.hasTodoTrack,
    updated: proj.updated,
    // Card / context / history previews: strip MD delimiters (## ** `) for readability.
    summary: summary ? plainTextPreview(summary, 120) : task.summary,
  }
}
