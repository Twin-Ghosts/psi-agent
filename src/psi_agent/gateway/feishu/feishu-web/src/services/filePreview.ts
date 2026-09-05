/**
 * 交付物「能不能在前端直接预览」的判断。
 *
 * 文本/图片直接渲染; pdf / office / sheet / csv / 音视频交给 services/blobPreview.ts
 * 动态加载对应渲染库。
 */

const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif"]);
const AUDIO_EXTS = new Set(["mp3", "wav", "ogg", "m4a", "flac"]);
const VIDEO_EXTS = new Set(["mp4", "webm", "mov", "m4v"]);
const MARKDOWN_EXTS = new Set(["md", "markdown"]);
const BINARY_EXTS = new Set(["pdf", "docx", "xls", "xlsx", "pptx"]);
const TEXT_EXTS = new Set([
  "txt",
  "log",
  "json",
  "jsonl",
  "tsv",
  "yaml",
  "yml",
  "toml",
  "ini",
  "xml",
  "py",
  "ts",
  "tsx",
  "js",
  "jsx",
  "css",
  "sh",
  "sql",
]);

export function extensionOf(name: string): string {
  const base = String(name || "").split(/[\\/]/).pop() || "";
  const dot = base.lastIndexOf(".");
  if (dot <= 0 || dot === base.length - 1) return "";
  return base.slice(dot + 1).toLowerCase();
}

export type PreviewKind = "image" | "markdown" | "html" | "text" | "blob" | "none";

export function previewKindOf(name: string): PreviewKind {
  const ext = extensionOf(name);
  if (!ext) return "none";
  if (IMAGE_EXTS.has(ext) || ext === "svg") return "image";
  if (MARKDOWN_EXTS.has(ext)) return "markdown";
  if (ext === "html" || ext === "htm") return "html";
  if (
    AUDIO_EXTS.has(ext)
    || VIDEO_EXTS.has(ext)
    || BINARY_EXTS.has(ext)
    || ext === "csv"
  ) return "blob";
  if (TEXT_EXTS.has(ext)) return "text";
  return "none";
}

export function isBlobPreviewable(name: string): boolean {
  return previewKindOf(name) !== "none";
}

const IMAGE_MIME: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  bmp: "image/bmp",
  ico: "image/x-icon",
  avif: "image/avif",
  svg: "image/svg+xml",
  mp3: "audio/mpeg",
  wav: "audio/wav",
  ogg: "audio/ogg",
  m4a: "audio/mp4",
  flac: "audio/flac",
  mp4: "video/mp4",
  webm: "video/webm",
  mov: "video/quicktime",
  m4v: "video/mp4",
};

export function mimeOf(name: string): string {
  return IMAGE_MIME[extensionOf(name)] || "application/octet-stream";
}

/**
 * ``/workspace/file`` 的 data 字段是 base64, 文本要解出来才能显示。
 *
 * 不能用 ``atob`` 直接得字符串: 它按 latin-1 解, 中文会变乱码。先还原字节再用 UTF-8 解码。
 */
export function decodeBase64Text(b64: string): string {
  try {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder("utf-8").decode(bytes);
  } catch {
    return "";
  }
}
