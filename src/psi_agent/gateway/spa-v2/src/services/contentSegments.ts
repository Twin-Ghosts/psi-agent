/**
 * Split streamed assistant ``content`` across tool rounds.
 *
 * Prose before each ``tool_call`` is sealed as process notes (temporary body).
 * The segment after the last tools (or the sole segment) becomes the final bubble.
 */

export type ContentSegments = {
  /** Sealed step-between narration (raw, may include trailing whitespace). */
  sealed: string[]
  /** Growing segment since the last tool_call (or turn start). */
  current: string
}

export function contentSegmentsStart(): ContentSegments {
  return { sealed: [], current: '' }
}

export function appendContentSegment(seg: ContentSegments, delta: string): ContentSegments {
  if (!delta) return seg
  return { sealed: seg.sealed, current: seg.current + delta }
}

/** Call when a tool_call arrives — park current prose as an interim process note. */
export function sealContentBeforeTools(seg: ContentSegments): ContentSegments {
  if (!seg.current.trim()) {
    return { sealed: seg.sealed, current: '' }
  }
  return { sealed: [...seg.sealed, seg.current], current: '' }
}

export function settleContentSegments(seg: ContentSegments): {
  finalText: string
  processNotes: string[]
} {
  const processNotes = seg.sealed.map((s) => s.trim()).filter(Boolean)
  let finalText = seg.current.trim()
  // Model finished inside a tool-round message with no trailing summary segment.
  if (!finalText && processNotes.length > 0) {
    finalText = processNotes.pop() ?? ''
  }
  return { finalText, processNotes }
}
