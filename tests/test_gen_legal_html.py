"""`scripts/gen_legal_html.py` 的解析规则测试。

锁的是四条容易回退的规则: 目录块判定(重复标题即结束)、Tab 表格、加粗透传、小标题与引出语之分。
脚本在 `scripts/` 下不属包, 故用 importlib 按路径加载。
"""
# ruff: noqa: RUF001, RUF002  全角冒号是协议原文的字面量, 换成半角这些用例就测不到真实输入。

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gen_legal_html.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_legal_html", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load()


def test_toc_run_ends_at_repeated_heading() -> None:
    """目录末项后紧跟的正文首个标题同样匹配「一、」, 只数连续会把它吞进目录。"""
    lines = ["标题", "一、甲", "二、乙", "三、丙", "一、甲", "正文一句。"]
    assert gen._find_toc_range(lines) == (1, 4)


def test_no_toc_when_headings_have_body_between() -> None:
    """许可协议每个标题后紧跟正文, 不应误判出目录。"""
    lines = ["标题", "一、甲", "正文。", "二、乙", "正文。", "三、丙", "正文。"]
    assert gen._find_toc_range(lines) is None


def test_body_heading_survives_toc() -> None:
    """回归: 正文的「一、甲」曾被目录判定吞掉, 生成结果里少一个 h2。

    这里的行序照隐私政策的真实形状: 先一段目录, 再逐章正文。
    """
    lines = ["标题", "一、甲", "二、乙", "三、丙", "一、甲", "正文。", "二、乙", "正文。", "三、丙", "正文。"]
    body, _ = gen._render_body(lines)
    assert body.count("<h2") == 3  # 正文三章都在, 目录不计入编号
    assert '<nav class="toc">' in body
    assert body.count("<li><a href=") == 3  # 三个目录项都能链到正文


def test_bold_passthrough_and_escaping() -> None:
    assert gen._inline("**要紧**的话") == "<strong>要紧</strong>的话"
    # 先转义再替换, 否则 escape 会把生成的 <strong> 一起转掉
    assert gen._inline("a<b & c") == "a&lt;b &amp; c"
    assert gen._inline("**a<b**") == "<strong>a&lt;b</strong>"


def test_tab_block_becomes_table() -> None:
    body, _ = gen._render_body(["标题", "列一\t列二", "值一\t值二"])
    assert "<th>列一</th>" in body
    assert "<td>值一</td>" in body
    assert body.count("<table>") == 1


def test_numbered_subheading_vs_lead_in() -> None:
    """`3.2 注册与登录` 是小标题; 以「：」收尾的同形行是正文引出语。"""
    assert gen._is_h3("3.2 注册与登录")
    assert not gen._is_h3("3.1 我们可能通过以下几种方式收集用户个人信息：")
    # 超长的编号行是正文而非标题
    assert not gen._is_h3("3.3 " + "字" * gen._H3_MAX_LEN)


def test_meta_lines_collected_not_rendered() -> None:
    body, meta = gen._render_body(["标题", "更新日期：2026-08-13", "生效日期：2026-08-13", "正文。"])
    assert meta == ["更新日期：2026-08-13", "生效日期：2026-08-13"]
    assert "更新日期" not in body


def test_repo_docs_render_expected_shape() -> None:
    """对库内两份真实协议做形状断言, 防止源文件换版后静默产出残缺 HTML。"""
    for doc in gen.DOCS_TO_BUILD:
        html_out = gen.render(doc)
        assert html_out.count("<h1>") == 1
        assert html_out.count("<h2") >= 12  # 两份都有十二章以上
        assert 'class="meta"' in html_out
        assert "<strong>" in html_out  # 加粗已写进 md 源
        assert "**" not in html_out  # 加粗标记不应漏进产物


def test_check_mode_passes_for_committed_output() -> None:
    """库内产物应与 md 源一致 —— 不一致说明改了 md 忘了重新生成。"""
    assert gen.main(["--check"]) == 0


def _one_doc_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, out_name: str = "out.html") -> Path:
    """把 DOCS_TO_BUILD 收窄成一份、产物指向 tmp, 返回那个产物路径。

    REPO_ROOT 一并改掉: ``main()`` 打日志时对产物路径调 ``relative_to(REPO_ROOT)``,
    tmp 在库外会直接 ValueError。
    """
    out = tmp_path / out_name
    src = gen.DOCS_TO_BUILD[0]
    monkeypatch.setattr(gen, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gen, "DOCS_TO_BUILD", (gen.LegalDoc(src.src, out, src.browser_title),))
    return out


def test_check_mode_fails_when_output_is_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**CI 拦「改了 md 忘了重生成」全靠这个非 0 返回。**

    原先只测了「一致时返回 0」那一面 —— 那条在 --check 整个坏掉(比如恒返回 0)时
    照样绿, 等于守门的那一半没测。
    """
    out = _one_doc_at(tmp_path, monkeypatch)
    out.write_text("<html>过期内容</html>", encoding="utf-8")
    assert gen.main(["--check"]) == 1
    # 产物缺失也该判过期, 不该当成一致
    out.unlink()
    assert gen.main(["--check"]) == 1


def test_check_mode_ignores_crlf_from_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CRLF 检出不该被判过期。

    本仓 core.autocrlf=true 且无 .gitattributes: 库内存 LF, Windows 检出得 CRLF。
    按字节比对会让 CI(windows-latest)在干净检出上就判过期 —— 这条守住那次修正。
    """
    out = _one_doc_at(tmp_path, monkeypatch)
    generated = gen.render(gen.DOCS_TO_BUILD[0])
    # 模拟 autocrlf 检出: 写成 CRLF
    out.write_bytes(generated.replace("\n", "\r\n").encode("utf-8"))
    assert gen.main(["--check"]) == 0


def test_generate_writes_lf_even_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """生成必须写 LF: 否则换台机器跑一次就产生全文件 diff。"""
    out = _one_doc_at(tmp_path, monkeypatch)
    assert gen.main([]) == 0
    assert b"\r\n" not in out.read_bytes()


def test_check_mode_survives_cp1252_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cp1252 的 stdout 下 ``--check`` 仍要返回 0。

    实测的 CI 失败: GitHub 的 windows-latest 上 stdout 是 cp1252, 编不出
    ``产物与 md 源一致。``, 于是**产物明明一致**却抛 UnicodeEncodeError 退出码 1 ——
    同步守卫变成了「Windows 上必红」。这条用真的 cp1252 缓冲区复现, 不是 mock 掉
    print 了事: 要锁的正是「中文能编出去」。
    """
    # 只要它把产物指到 tmp 的副作用, 返回的路径这条用例不看。
    _one_doc_at(tmp_path, monkeypatch)
    assert gen.main([]) == 0

    with capsys.disabled():
        buf = io.BytesIO()
        cp1252 = io.TextIOWrapper(buf, encoding="cp1252", newline="")
        monkeypatch.setattr(sys, "stdout", cp1252)
        try:
            rc = gen.main(["--check"])
        finally:
            cp1252.flush()
            monkeypatch.undo()

    assert rc == 0
    # reconfigure 之后写进去的是 UTF-8 字节, 所以按 UTF-8 读回来。
    assert "产物与 md 源一致。" in buf.getvalue().decode("utf-8")
