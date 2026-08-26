---
name: llm-wiki
description: "Build and maintain a self-growing, interlinked Markdown knowledge base (Andrej Karpathy's \"LLM wiki\" pattern): compile what you learn into durable, cross-referenced pages under <workspace>/wiki/ instead of re-searching raw sources every time. Each page is Markdown with YAML frontmatter (title/tags/aliases/timestamps) and a body that links other pages with [[wikilink]] syntax. Use when asked to record, organize, or look up domain knowledge (LLM concepts, papers, techniques, glossary terms) as a browsable second brain, or to answer from / extend that wiki. Drives the dedicated wiki_* tools (wiki_write/read/search/list/links/delete) — this skill is the discipline for when and how to use them."
category: coding
---

# LLM Wiki

Maintain a persistent, interlinked Markdown knowledge base following Andrej
Karpathy's "LLM wiki" idea: instead of re-searching raw documents on every
question, **compile** knowledge into durable, cross-referenced pages that live
under `<workspace>/wiki/` and compound over time. Prefer answering from the wiki;
when you learn something new or the user shares a source, sink it into a page so
the next question is cheaper.

This skill is the **discipline layer**: it decides *when* to record knowledge and
*how* to organize, name, and link pages. All read/write/search operations go
through the dedicated **`wiki_*` tools**, which own the storage format (slug
derivation, YAML frontmatter, timestamps, link indexing). Do not hand-craft wiki
files with the generic `write`/`edit`/`bash` tools — let the tools keep the
format and link graph consistent.

## 两个库:个人的 与 全组织共享的

每个用户的 Session 有自己的 workspace,所以一页写在谁的目录里就只有谁看得到 ——
个人笔记本该如此,但**组织级**的知识每人各存一份就毫无意义:甲写的页乙查不到。
所以有两个库:

| | 在哪 | 谁看得到 |
|---|---|---|
| 个人库 | `<workspace>/wiki/` | 只有本人的 Session |
| **共享库** | `PSI_WIKI_SHARED_DIR` 指的目录 | **所有人**,可读**也可写** |

- **读操作**(`wiki_read` / `_search` / `_list` / `_links`)自动跨两库,不必指定去哪找。
  返回里每页带 `scope`(`personal` / `shared`),据此就知道它是不是全员可见。
- **写操作**跟着页面走:`wiki_write` 的 `scope="auto"`(默认)会就地改那一页 ——
  改共享页就是为所有人改掉,不会分叉出一份私有副本悄悄遮蔽它。
- **新页默认进个人库**。要发布成全员可见,显式传 `scope="shared"` —— 这是一个动作,
  不是随手记笔记的副作用。
- 同名时**个人页遮蔽共享页**:workspace 是本人控制的东西。
- 没配共享库时 `scope="shared"` 会**报错**而不是静默写成个人页(否则你以为发布了、
  别人却查不到)。

什么该进共享库:组织结构与汇报关系、跨人的口径与约定、团队级 SOP、公司周期性事实。
什么留个人库:自己的草稿、只与本人相关的上下文、还没定稿的判断。

**删共享页影响所有人** —— `wiki_delete` 的返回会告诉你删掉的是哪一边,删之前看清楚。

The pattern has **three layers**: raw sources (read, never rewrite) → the
`wiki/*.md` pages (the durable synthesis layer) → the **schema layer** in
[SCHEMA.md](SCHEMA.md), which defines page anatomy, naming/linking rules, the
three maintenance loops, and known anti-patterns. **Read SCHEMA.md before
seeding a new wiki or running a lint pass**, and seed a copy into the wiki as
`wiki/_schema.md` (title `_schema`) so the contract travels with the knowledge
base. The core idea: **preserve synthesis, don't regenerate it** — write a
conclusion into a page once, then revise that page rather than recomputing it.

Reply in Chinese unless the user clearly uses another language.

## The tools

| Tool | Purpose |
|------|---------|
| `wiki_write(title, content, tags="", aliases="", overwrite=True)` | Create or update a page. Slug, frontmatter, and `created`/`updated` timestamps are handled for you; re-writing the same title updates that page and preserves its original `created`. |
| `wiki_read(title_or_slug)` | Read one page's body + metadata (tags, aliases, timestamps, outgoing links). |
| `wiki_search(query, limit=20)` | Full-text search across titles, tags, aliases, and bodies; title/tag hits rank above body hits, each result carries a snippet. |
| `wiki_list(tag="")` | Table of contents: every page's slug/title/tags/updated/links, optionally filtered to one tag. |
| `wiki_links(title_or_slug)` | A page's link graph: `outgoing`, `backlinks`, and `broken` (links to pages that don't exist yet). |
| `wiki_delete(title_or_slug)` | Delete a page; reports the `orphaned_backlinks` whose `[[links]]` are now broken. |

## Where the wiki lives

- Root: `wiki/` under the workspace — created automatically on first `wiki_write`.
- One page per concept, filename `wiki/<slug>.md`. The slug is derived by the
  tool from the title (lowercased, non-alphanumerics collapsed to single dashes),
  e.g. "Rotary Positional Embeddings" → `rotary-positional-embeddings`. You pass
  the human title; you never compute the slug yourself.

## Page format (owned by the tools)

Each page is Markdown with YAML frontmatter (`title`, `slug`, `tags`, `aliases`,
`created`, `updated`) followed by the body. You supply `title`, `content`,
`tags`, and `aliases` to `wiki_write`; the tool writes the frontmatter and
timestamps. Body content follows these conventions:

- **Body links** use `[[Page Title]]` or `[[Page Title|display text]]`. The link
  target resolves by slugifying the text before the `|`. Link liberally — even to
  pages that don't exist yet; they surface as `broken` in `wiki_links` and become
  "wanted pages" to write next.
- Keep pages **atomic**: one concept per page, a few paragraphs. When a page
  starts covering two things, split it and link the halves instead.
- Add a `## Sources` section with the URLs/refs the page was compiled from.
- **tags** are kebab-case topic labels; **aliases** are alternate names people
  search by (e.g. `RoPE` for "Rotary Positional Embeddings") — `wiki_search`
  matches them too.

Example body passed as `content`:

```markdown
RoPE injects absolute position by rotating the query/key vectors, so the dot
product in [[Self-Attention]] depends only on the *relative* offset. Contrast
with [[Absolute Positional Embeddings]]. Widely used in [[LLaMA]] and others.

## Sources
- <https://arxiv.org/abs/2104.09864>
```

## Seeding a new wiki

When asked to build a wiki on a topic (and `wiki_list` shows it's empty or the
topic is absent):

1. Read [SCHEMA.md](SCHEMA.md) and `wiki_write` a copy into the wiki as title
   `_schema`, adapting its "Domain conventions" to the topic.
2. `wiki_write` an `index` hub page that will link the top-level topics.
3. Create one atomic page per concept, linking them with `[[…]]`, and add each
   to the `index` hub. Do **not** dump everything into one page or one chat blob.

## The three maintenance loops

Run the loop that fits the request — the wiki is a living layer, not a one-shot
document. Full detail (and the anti-patterns to avoid) is in
[SCHEMA.md](SCHEMA.md); the short form:

### Ingest — new material arrives

1. Identify the concepts in the source.
2. Per concept: `wiki_search` → if it exists, `wiki_read` + merge + `wiki_write`
   the same title (preserves `created`); if not, `wiki_write` a new page.
3. Add the source to each touched page's `## Sources`; link new `[[concepts]]`
   even before their pages exist.

### Query — a question comes in

1. `wiki_search` the term(s); `wiki_read` the top page(s).
2. Answer **from the wiki**, citing the page slug(s).
3. If the wiki is thin on a high-value topic, answer and **backfill** with
   `wiki_write` so the next ask is cheaper. Backfilling is how the wiki compounds.

### Lint — periodic health check ("check/clean/maintain the wiki")

1. `wiki_list` to enumerate pages.
2. For each page, `wiki_links`: collect `broken` (→ wanted pages to write) and
   empty-`backlinks` non-hub pages (→ orphans to link from `index`).
3. Flag stale pages (old `updated`) for re-ingest; merge near-duplicates
   (`wiki_delete` the loser, repoint its backlinks).
4. Report a short punch-list; fix the cheap items immediately. A `[[link]]` that
   resolves to nothing is a lint failure to fix, not a cosmetic issue.

## Housekeeping

- Renaming a concept: `wiki_write` under the new title, use `wiki_links` on the
  old page to find inbound links, repoint them, then `wiki_delete` the old page.
- Deleting: `wiki_delete` reports `orphaned_backlinks`; fix or drop those now-
  broken `[[…]]` links so the graph stays consistent.

## Notes

- Never store secrets in the wiki. It's plain Markdown the user reads and edits.
- The `wiki/*.md` files **are** the wiki. If you export to HTML for viewing,
  keep the Markdown sources — never delete them and keep only the HTML.
- The value compounds: the more you compile into linked pages, the less you
  re-search — that's the whole point of the pattern.
