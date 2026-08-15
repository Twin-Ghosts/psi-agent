# 安装期协议同意设计

> **目标**：把《Haitun Agent 软件许可及服务协议》与《Haitun Agent 隐私保护政策》接入 Inno Setup 安装流程 —— 两份可分别点开阅读，**一个勾选框同时覆盖两份**，不勾无法继续安装。
>
> **来源**：负责人 2026-08-15 需求，四项决策已定（见「四项决策」表）。协议正文由法务提供，位于 `src/psi_agent/gateway/spa-v2/legal/Haitun_软件许可及服务协议_1.0.md`、`src/psi_agent/gateway/spa-v2/legal/Haitun_隐私保护政策_1.0.md`。
>
> **范围**：`.github/inno-setup/haitun.iss` 加自定义向导页；新增 `scripts/gen_legal_html.py`；换版 `src/psi_agent/gateway/spa-v2/public/{terms,privacy}.html`；两份 md 源文件补加粗标记。不改认证逻辑、不改 `haitun.c`、不改 SPA 组件代码。

## 设计目标

一句话：许可协议导言已经把交互定死了 ——

> 您在本软件安装过程中勾选同意本协议，即视为您同时同意《Haitun Agent 隐私保护政策》所载明的个人信息处理安排。

「安装过程中勾选」+「一个动作覆盖两份」，因此不能用 Inno 内置 `LicenseFile`（它是单选钮、且一次只挂一份文件）。本设计做四件事：

1. **单一源**：两份 md → 生成器 → HTML，安装器与产品内消费同一份产物
2. **安装器协议页**：自定义向导页，两个可点链接 + 一个勾选框，不勾则「下一步」禁用
3. **加粗**：按规则把「免责 / 限权 / 争议解决 / 个人信息关键项」标进 md 源
4. **换版产品内两页**：现有 `terms.html` / `privacy.html` 是占位稿且与新文档冲突

### 四项决策

| # | 问题 | 决策 | 影响 |
|---|---|---|---|
| 1 | 加粗从哪来 | **自己推断** | 由我们把 `**` 写进 md 源，规则见第二节，diff 可审 |
| 2 | 升级是否重复勾 | **每次都勾** | 不需要注册表、不需要版本比对 —— 整块状态存储从设计中删除 |
| 3 | 产品内过期两页 | **一起换掉** | 纳入本次范围 |
| 4 | 静默安装 | **视为同意** | 不加 `/ACCEPTTOS`，改为文档声明 |

## 一、单一源与生成链路

```
spa-v2/legal/Haitun_软件许可及服务协议_1.0.md ─┐
spa-v2/legal/Haitun_隐私保护政策_1.0.md ───────┴→ scripts/gen_legal_html.py
                                  ↓
                spa-v2/public/terms.html
                spa-v2/public/privacy.html
                                  ↓
                ├→ vite build 把 public/* 拷进 dist/ → PyInstaller
                │  `--add-data spa-v2/dist`（`pyinstaller.yml:32`）→ 产品内可读
                └→ haitun.iss 用 `Flags: dontcopy` 引同一路径 → 安装期可读

（上面省了 `src/psi_agent/gateway/` 前缀，两处 spa-v2 都在它下面。）
```

**生成物入库。** CI 里 `haitun-inno-setup`（`pyinstaller.yml:82`）与前端 build（`:66`）是两个 job，若不入库则两处都得装 Python 跑生成器。入库后加一步 `--check` 校验（生成结果与库内文件不一致则 CI 失败），避免改了 md 忘了重生成。

**安装器不留副本。** `dontcopy` 直接 source 到 `spa-v2/public/`，装机时 `ExtractTemporaryFile` 落 `{tmp}` 再 `ShellExec` 打开。三个文件（两份 HTML + `legal.css`）必须一起解，否则浏览器拿不到样式。

### 生成器要处理的四处源文件特征

两份 md **没有任何 Markdown 结构**：0 个 `#`、0 个 `**`、0 个 `|`。标题是「一、定义」这样的中文序号裸行，表格是 Tab 分隔的裸行。所以不能用通用 md 渲染器，需按下列规则解析：

| 源特征 | 判定 | 输出 |
|---|---|---|
| 第 1 行 | 文件首行 | `<h1>` |
| `更新日期：` / `生效日期：` | 前缀匹配 | 合并成一个 `<p class="meta">` |
| `^[一二三四五六七八九十]+、` | 正则 | `<h2>` |
| 含 `\t` 的连续行块 | Tab 分隔 | `<table>`，首行为 `<th>` |
| 其余非空行 | 兜底 | `<p>` |

**隐私政策的目录块要特判。** `Haitun_隐私保护政策_1.0.md:12-23` 是一个「一、定义 … 十二、联系我们」的目录，与正文标题（`:24-154`）逐字重复。若照 `<h2>` 规则处理会产出 12 个重复标题、并在带 id 时撞 id。规则：**首次出现的 `^[一二三四五六七八九十]+、` 序列若连续无正文夹杂，判为目录**，渲染成 `<nav class="toc">` 的 `<ul>`，只有正文那一遍生成 `<h2 id=...>`。许可协议无此块（`:12` 起即正文），同一规则对它是空操作。

## 二、加粗规则（决策 1）

法务给的源文件没有加粗，但协议正文自己承诺：

> 限制、免除责任条款或者其他涉及您重大权益的条款将以加粗形式提示您重点注意。

决策为「自己推断」，故把 `**` 写进 **md 源**（不是写进生成器）—— 加粗是法律判断，必须留在人能审的 diff 里，不能藏在脚本的正则里。生成器只做 `**x**` → `<strong>x</strong>` 的透传。

四类必加粗，**只粗到承载该含义的那一句或那个分句**，不整段粗（整段粗等于没粗）：

| 类 | 依据 | 典型落点 |
|---|---|---|
| 免除 / 限制我们责任 | 协议导言承诺 | 十四、责任限制 14.1–14.5（`许可协议:160-170`） |
| 限制用户权利 | 同上 | 3.3 许可范围、九、用户行为规范 |
| 争议解决与司法管辖 | 同上 | 15.2 适用法律、15.3 合肥市法院管辖（`:173-174`） |
| 个人信息重大权益 | 隐私政策导言承诺 | 3.6.3 输入内容发往第三方大模型、权限清单里「是否可拒绝＝是/否」列 |

已定的两处**不加粗**：15.4「标题仅为阅读方便」、15.5 可分割性 —— 属技术性条款，不涉重大权益，粗了会稀释真正该看的部分。

## 三、安装器协议页

### 页面位置与构件

`CreateCustomPage(wpWelcome)` 插在欢迎页后、选目录页前。注意 `haitun.iss` 未设 `DisableWelcomePage`，Inno 6 默认无欢迎页，此时该锚点退化为「第一页」，仍是想要的位置。

```
┌─ 许可协议与隐私保护政策 ───────────────────┐
│ 安装前请阅读以下协议。                      │
│                                             │
│   《Haitun Agent 软件许可及服务协议》  ←链接 │
│   《Haitun Agent 隐私保护政策》        ←链接 │
│                                             │
│ ☐ 我已阅读并同意上述协议                    │
└─────────────────────────────────────────────┘
                          [上一步] [下一步(禁用)]
```

- **链接**：用 `TNewStaticText` + `Font.Underline` + `Cursor := crHand` + `OnClick`。不用 Inno 6.3 的 `TNewLinkLabel` —— CI 里 `choco install innosetup`（`pyinstaller.yml:109`）不锁版本，拿到的可能是更早的 6.x。
- **勾选框**：`TNewCheckBox`，文案即协议导言的措辞。
- **门禁**：`CurPageChanged` 里置 `WizardForm.NextButton.Enabled := chk.Checked`，`OnClick` 里同步。**不用 `NextButtonClick` 返回 False 弹提示** —— 那是先让人点了再拒绝，禁用态更直白。

### 打开文档

```pascal
ExtractTemporaryFile('legal.css');   // 必须先解, 否则 HTML 无样式
ExtractTemporaryFile('terms.html');
ShellExec('open', ExpandConstant('{tmp}\terms.html'), '', '', SW_SHOWNORMAL, ewNoWait, rc);
```

`ExtractTemporaryFile` 对同一文件重复调用会报错，故用一个 `Boolean` 标记只解一次。`{tmp}` 由 Inno 在退出时自动清理，无需自己删。

### 文案与语言

`[Languages]` 有 chinesesimplified 与 english 两条，而 `ChineseSimplified.isl` 是构建时下载且被 gitignore（`.github/inno-setup/.gitignore:4`）。因此本页所有文案写进 `[CustomMessages]` 的 `zh`/`en` 两组，不依赖 .isl 提供任何键。两份协议只有中文版，英文语言下点开仍是中文 —— 已知限制，写进 T 段遗留问题。

### 静默安装（决策 4）

`/SILENT` 与 `/VERYSILENT` 跳过全部向导页，含本页 —— 不加 `/ACCEPTTOS`，按决策「视为同意」。在 `.github/inno-setup/oss-publish.md` 写明：静默安装视为部署方已代表最终用户接受两份协议。

### 升级重复勾选（决策 2）

`haitun.c:315` 用 `ShellExecuteW` 拉起 setup 且不带 `/SILENT`，走完整向导，因此自动更新每次都会经过本页。决策为「每次都勾」，故**不写任何状态**（无注册表、无标记文件），这也顺带让「换人用同一台机器」不会静默跳过协议。

## 四、产品内两页换版（决策 3）

现有 `spa-v2/public/terms.html`（5.4KB）/ `privacy.html`（5.1KB）是占位稿，且与新文档**实质冲突**：

| 旧占位稿 | 新文档 |
|---|---|
| `仅保存在本机，我们不做云端同步、不上传、不备份` | `输入内容，将由我们收集并向第三方大模型发送`（隐私政策 3.6.3） |
| `本机功能不依赖登录` | C 端登录已是硬门禁（commit 9f33c52b） |
| 标题《用户服务协议》 | 《Haitun Agent 软件许可及服务协议》 |

两文件改为生成物，直接覆盖。`legal.css` 保留复用，仅新增 `nav.toc` 一条样式（隐私政策目录块用）。

**登录屏的协议链接已由另一批改动整句删除**（同一工作树里、本次实施期间发生）：`legalNote`、`LEGAL_TERMS`、`LEGAL_PRIVACY` 均已不在 `HubLoginPanel.tsx`，测试改为 `queryByRole(...)).toBeNull()` 反向守。理由与本设计一致 —— 同意动作前移到安装期后，登录窗再说一遍是噪音。

因此决策 3 的落点收窄为：**只换 `public/` 下两个 HTML 的内容**（安装器读它们），SPA 侧无链接可改。`spa-v2/AGENTS.md` 已按此改写，原先「协议链接必须保留」那句已作废。

## 五、验收标准

| # | 标准 |
|---|---|
| A1 | 生成器对两份 md 产出 HTML：标题成 `<h2>`、Tab 块成 `<table>`、隐私政策目录不重复出现 |
| A2 | `--check` 模式在 md 改动未重生成时返回非 0 |
| A3 | 安装器协议页：不勾选时「下一步」禁用，勾选后启用 |
| A4 | 两个链接分别能在浏览器打开对应协议，带样式，重复点击不报错 |
| A5 | 断网装机可读（文件内置，非线上链接） |
| A6 | 中英两语言下本页文案均正确，不依赖 .isl |
| A7 | 加粗覆盖四类落点，md diff 可审 |
| A8 | 产品内两页为新版内容（登录面板已无协议链接，无文案可对齐） |
| A9 | `ISCC.exe` 编译通过；spa-v2 测试与 build 通过 |

## 六、实施中改掉的四处设计

写代码时撞到四件设计时没料到的事，一并记在这里（以代码为准）。

**1. `.iss` 必须加 UTF-8 BOM。** 原文件是纯 ASCII（`git show HEAD:.github/inno-setup/haitun.iss` 无非 ASCII 字节），本次的 `[CustomMessages]` 是它第一次出现中文。Inno 6 只在文件带 BOM 时按 UTF-8 读 `.iss`，否则按 ANSI 解 —— 在 `windows-latest` 上会把中文解成乱码，且**不报错**。已加 BOM。

**2. Pascal 注释里不能写花括号常量。** 原设计的注释写了 `{tmp} 由 Inno 退出时自清`，而 Pascal 注释以花括号定界、不嵌套 —— `{tmp}` 的右括号会提前闭合注释，余下文字变成语法错误。改为「临时目录」并在注释里留了这条告示。

**3. `--check` 必须先归一化换行。** 本仓 `core.autocrlf=true` 且无 `.gitattributes`，库内存 LF、Windows 检出得 CRLF。CI 的 `haitun-inno-setup` 与打包 job 都跑在 `windows-latest`，按字节比对会在**干净检出上就判过期**（实测 LF 36,862 字节 vs CRLF 37,054 字节）。生成仍写 LF，比对走 `_normalize()`。

**4. 「下一步」按钮只能在离开协议页那一刻交还。** 原设计的 `CurPageChanged` 是「本页就按勾选状态设，其他页无条件置 `True`」。后者太宽 —— 安装进行页等页面的按钮状态由 Inno 自己管，每翻一页都插一手会把它的状态覆盖掉。改为记一个 `PrevPageID`（`InitializeWizard` 里初始化为 `-1`），只在「上一页是协议页」的那次跳转上恢复按钮：

```pascal
if CurPageID = LegalPage.ID then
  UpdateNextButtonState
else if PrevPageID = LegalPage.ID then
  WizardForm.NextButton.Enabled := True;
PrevPageID := CurPageID;
```

另有一处收窄：`CreateLegalLink` 原设计把 `TNotifyEvent` 当参数类型传，改为返回控件、由调用方赋 `OnClick` —— 少一处与 Inno 版本相关的写法，本地无编译器可验时这样更稳。

## 七、未验到的部分

本机没装 Inno Setup（两处 Program Files 均查过），**本地没跑过 `ISCC.exe`**。改为把分支推上去让 CI 编译 —— `.github/workflows/pyinstaller.yml` 的 `haitun-inno-setup` job 在**每次 push 上都跑**（`on: push` 无分支过滤，job 级也没有 `if`），特性分支同样会编译，所以 A9 的编译那一半由 CI 收。**结果要看 CI**，本文档不代它下结论。

Pascal 侧的静态核对：花括号 14/14 配平、`begin` 11 / `end;` 11 配平、10 个例程、6 个 `{cm:Legal*}` 键在两种语言下都有定义、3 条 `dontcopy` 路径都存在、UTF-8 BOM 在与 origin/main 合并后仍在。

**查过一处存疑写法**：`Cursor := crHand`。Inno 的脚本引擎是 RemObjects Pascal Script + MiniVCL，光标常量用的是 Lazarus 的名字，不是 Delphi 的 `crHandPoint` —— 两份 Inno 实例代码（其中一份正是「装机时显示协议链接」的同类场景）都写 `crHand`，写法成立。

**向导没人点过。** A3–A6（勾选门禁、链接打开、样式、`ScaleY` 0/48/72/108 的排版）需要在 Windows 上装一遍真安装包才能收，只有负责人能做。

英文语言下用户看到的仍是中文正文 —— 两份协议只有中文版，属已知限制。

