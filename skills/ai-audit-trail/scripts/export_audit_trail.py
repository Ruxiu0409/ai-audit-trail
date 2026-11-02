#!/usr/bin/env python3
"""ai-audit-trail: export Claude Code conversation transcripts as an
audit package (PDF + raw JSONL + SHA-256 manifest) for academic review.

Run with:  uv run --with reportlab python export_audit_trail.py [options]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

TOOL_NAME = "ai-audit-trail"
TOOL_VERSION = "1.0.0"
DEFAULT_MAX_CHARS = 20000

# ---------------------------------------------------------------------------
# Locating transcripts
# ---------------------------------------------------------------------------

def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def project_dir_for(cwd: Path) -> Path:
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
    return claude_home() / "projects" / encoded


def find_session_file(session_id: str, cwd: Path) -> Path | None:
    candidate = project_dir_for(cwd) / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
    for p in (claude_home() / "projects").glob(f"*/{session_id}.jsonl"):
        return p
    return None


def subagent_files(session_file: Path) -> list[Path]:
    d = session_file.with_suffix("") / "subagents"
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def read_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"type": "_unparseable", "raw": line})
    return out


def session_title(entries: list[dict]) -> str:
    title = ""
    for e in entries:
        if e.get("type") in ("ai-title", "summary"):
            title = e.get("aiTitle") or e.get("summary") or title
        elif e.get("type") == "custom-title":
            title = e.get("customTitle") or title
    return title


def list_sessions(cwd: Path) -> None:
    pdir = project_dir_for(cwd)
    files = sorted(pdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"在 {pdir} 找不到任何對話紀錄。")
        return
    print(f"專案紀錄目錄：{pdir}\n")
    for p in files:
        entries = read_jsonl(p)
        stamps = [e["timestamp"] for e in entries if e.get("timestamp")]
        n_user = sum(1 for e in entries if e.get("type") == "user" and is_human_prompt(e))
        print(f"{p.stem}  {stamps[0][:16] if stamps else '?':16} → {stamps[-1][:16] if stamps else '?':16}"
              f"  提問 {n_user:3d} 則  {session_title(entries)}")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def is_human_prompt(e: dict) -> bool:
    if e.get("type") != "user" or e.get("isMeta"):
        return False
    c = e.get("message", {}).get("content")
    if isinstance(c, str):
        return True
    return isinstance(c, list) and any(b.get("type") == "text" for b in c) and not any(
        b.get("type") == "tool_result" for b in c)


def block_text(content) -> str:
    """Flatten tool_result content (str or list of blocks) into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                parts.append("[圖片內容，詳見原始 JSONL]")
            else:
                parts.append(json.dumps(b, ensure_ascii=False))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def to_events(entries: list[dict]) -> list[dict]:
    """Convert raw JSONL entries into display events in chronological order."""
    events = []
    for e in entries:
        t = e.get("type")
        ts = e.get("timestamp", "")
        if t == "user":
            msg = e.get("message", {})
            c = msg.get("content")
            if e.get("isCompactSummary"):
                events.append({"kind": "compact", "ts": ts, "text": block_text(c)})
                continue
            if e.get("isMeta"):
                continue
            if isinstance(c, str):
                events.append({"kind": "user", "ts": ts, "text": c})
                continue
            for b in c or []:
                bt = b.get("type")
                if bt == "text":
                    events.append({"kind": "user", "ts": ts, "text": b.get("text", "")})
                elif bt == "image":
                    events.append({"kind": "user", "ts": ts, "text": "[使用者附加圖片，詳見原始 JSONL]"})
                elif bt == "tool_result":
                    events.append({"kind": "tool_result", "ts": ts,
                                   "id": b.get("tool_use_id", ""),
                                   "error": bool(b.get("is_error")),
                                   "text": block_text(b.get("content"))})
        elif t == "assistant":
            msg = e.get("message", {})
            model = msg.get("model", "")
            for b in msg.get("content") or []:
                bt = b.get("type")
                if bt == "text":
                    events.append({"kind": "assistant", "ts": ts, "model": model, "text": b.get("text", "")})
                elif bt == "thinking":
                    txt = b.get("thinking", "")
                    if txt.strip():
                        events.append({"kind": "thinking", "ts": ts, "model": model, "text": txt})
                elif bt == "redacted_thinking":
                    events.append({"kind": "thinking", "ts": ts, "model": model,
                                   "text": "[此段推理內容已由服務端加密，無法顯示]"})
                elif bt == "tool_use":
                    events.append({"kind": "tool_use", "ts": ts, "model": model,
                                   "id": b.get("id", ""), "name": b.get("name", ""),
                                   "text": json.dumps(b.get("input", {}), ensure_ascii=False, indent=2)})
    return events


def session_metadata(path: Path, entries: list[dict]) -> dict:
    stamps = sorted(e["timestamp"] for e in entries if e.get("timestamp"))
    models = Counter()
    versions, entrypoints, cwds, branches = set(), set(), set(), set()
    n_prompts = n_replies = n_tools = 0
    for e in entries:
        t = e.get("type")
        if t in ("user", "assistant"):
            if e.get("version"):
                versions.add(e["version"])
            if e.get("entrypoint"):
                entrypoints.add(e["entrypoint"])
            if e.get("cwd"):
                cwds.add(e["cwd"])
            if e.get("gitBranch"):
                branches.add(e["gitBranch"])
        if t == "assistant":
            m = e.get("message", {}).get("model")
            if m and m != "<synthetic>":
                models[m] += 1
            for b in e.get("message", {}).get("content") or []:
                if b.get("type") == "text":
                    n_replies += 1
                elif b.get("type") == "tool_use":
                    n_tools += 1
        if is_human_prompt(e):
            n_prompts += 1
    session_ids = {e.get("sessionId") for e in entries if e.get("sessionId")}
    return {
        "session_id": path.stem,
        "session_ids_in_file": sorted(session_ids),
        "title": session_title(entries),
        "source_file": str(path),
        "source_sha256": sha256_file(path),
        "source_bytes": path.stat().st_size,
        "first_timestamp_utc": stamps[0] if stamps else None,
        "last_timestamp_utc": stamps[-1] if stamps else None,
        "models": dict(models.most_common()),
        "claude_code_versions": sorted(versions),
        "entrypoints": sorted(entrypoints),
        "working_directories": sorted(cwds),
        "git_branches": sorted(branches),
        "counts": {"user_prompts": n_prompts, "assistant_text_blocks": n_replies, "tool_calls": n_tools},
    }


# ---------------------------------------------------------------------------
# Hashing & timestamping
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rfc3161_timestamp(target: Path, tsa_url: str) -> tuple[bool, str]:
    """Request an RFC 3161 timestamp for `target`. Only its hash leaves the machine."""
    if not shutil.which("openssl") or not shutil.which("curl"):
        return False, "找不到 openssl 或 curl，略過可信時間戳。"
    tsq = target.with_suffix(target.suffix + ".tsq")
    tsr = target.with_suffix(target.suffix + ".tsr")
    try:
        subprocess.run(["openssl", "ts", "-query", "-data", str(target), "-sha256", "-cert",
                        "-no_nonce", "-out", str(tsq)], check=True, capture_output=True)
        subprocess.run(["curl", "-sS", "--fail", "-H", "Content-Type: application/timestamp-query",
                        "--data-binary", f"@{tsq}", "-o", str(tsr), tsa_url],
                       check=True, capture_output=True, timeout=60)
        reply = subprocess.run(["openssl", "ts", "-reply", "-in", str(tsr), "-text"],
                               check=True, capture_output=True, text=True).stdout
        m = re.search(r"Time stamp:\s*(.+)", reply)
        return True, m.group(1).strip() if m else "（已取得回覆）"
    except (subprocess.SubprocessError, OSError) as exc:
        for p in (tsq, tsr):
            p.unlink(missing_ok=True)
        detail = getattr(exc, "stderr", b"") or b""
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        return False, f"可信時間戳申請失敗：{exc} {detail}".strip()


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

FONT_CANDIDATES = [
    # (path, subfont index) — must be TrueType-outline fonts with CJK coverage
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
    ("C:/Windows/Fonts/msjh.ttc", 0),
    ("C:/Windows/Fonts/mingliu.ttc", 0),
    ("/usr/share/fonts/truetype/arphic/uming.ttc", 0),
    ("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
]


def register_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    env = os.environ.get("AI_AUDIT_TRAIL_FONT")
    cands = ([(env, 0)] if env else []) + FONT_CANDIDATES
    for path, idx in cands:
        if path and Path(path).exists():
            try:
                font = TTFont("AuditCJK", path, subfontIndex=idx)
                pdfmetrics.registerFont(font)
                return "AuditCJK", font, path
            except Exception:  # noqa: BLE001 - try next candidate
                continue
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("MSung-Light"))
    return "MSung-Light", None, "MSung-Light (CID, 未內嵌)"


class Renderer:
    def __init__(self, max_chars: int, include_thinking: bool):
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle

        self.max_chars = max_chars
        self.include_thinking = include_thinking
        self.font, self.ttf, self.font_source = register_font()
        self.cmap = getattr(getattr(self.ttf, "face", None), "charToGlyph", None)
        f = self.font
        base = dict(fontName=f, wordWrap="CJK", leading=15, fontSize=10)
        self.colors = colors
        self.st = {
            "title": ParagraphStyle("title", **{**base, "fontSize": 20, "leading": 26, "spaceAfter": 10}),
            "h1": ParagraphStyle("h1", **{**base, "fontSize": 15, "leading": 20, "spaceBefore": 8, "spaceAfter": 6}),
            "h2": ParagraphStyle("h2", **{**base, "fontSize": 12, "leading": 16, "spaceBefore": 6, "spaceAfter": 4}),
            "body": ParagraphStyle("body", **base),
            "small": ParagraphStyle("small", **{**base, "fontSize": 8, "leading": 11,
                                                "textColor": colors.HexColor("#555555")}),
            "label": ParagraphStyle("label", **{**base, "fontSize": 8.5, "leading": 12, "spaceBefore": 8,
                                                "textColor": colors.HexColor("#444444")}),
            "code": ParagraphStyle("code", **{**base, "fontSize": 8, "leading": 10.5,
                                              "backColor": colors.HexColor("#F4F4F4"),
                                              "borderPadding": 4, "leftIndent": 4, "rightIndent": 4,
                                              "spaceBefore": 2, "spaceAfter": 4}),
            "think": ParagraphStyle("think", **{**base, "fontSize": 8.5, "leading": 12,
                                                "textColor": colors.HexColor("#555555"),
                                                "leftIndent": 10, "borderPadding": 3}),
        }

    def esc(self, text: str) -> str:
        if self.cmap is not None:
            text = "".join(
                ch if (ch in "\n\t" or ord(ch) in self.cmap) else f"[U+{ord(ch):04X}]" for ch in text)
        out = html.escape(text, quote=False).replace("\t", "    ")
        out = re.sub(r"(?m)^ +| {2,}", lambda m: "&nbsp;" * len(m.group(0)), out)
        return out.replace("\n", "<br/>")

    def clip(self, text: str, source_hash: str) -> str:
        if self.max_chars and len(text) > self.max_chars:
            omitted = len(text) - self.max_chars
            return (text[: self.max_chars] +
                    f"\n\n…［PDF 為求可讀已省略後續 {omitted:,} 字元；完整內容保存在原始 JSONL "
                    f"(SHA-256 {source_hash[:16]}…)］")
        return text

    def para(self, text: str, style: str):
        from reportlab.platypus import Paragraph
        return Paragraph(self.esc(text), self.st[style])

    def kv_table(self, rows: list[tuple[str, str]]):
        from reportlab.lib.units import mm
        from reportlab.platypus import Table, TableStyle
        data = [[self.para(k, "small"), self.para(v, "body")] for k, v in rows]
        t = Table(data, colWidths=[38 * mm, 132 * mm])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, self.colors.HexColor("#DDDDDD")),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    def build(self, out_pdf: Path, info: dict, sessions: list[dict]) -> None:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.platypus import PageBreak, SimpleDocTemplate, Spacer

        story = []
        story.append(self.para("AI 協作紀錄稽核報告", "title"))
        story.append(self.para("AI Collaboration Audit Trail", "h2"))
        story.append(Spacer(1, 4 * mm))

        rows = []
        for key, label in [("paper_title", "論文 / 計畫名稱"), ("section", "對應章節"),
                           ("author", "作者"), ("affiliation", "單位"), ("note", "備註")]:
            if info.get(key):
                rows.append((label, info[key]))
        rows += [
            ("匯出時間 (UTC)", info["exported_at_utc"]),
            ("匯出時間 (本地)", info["exported_at_local"]),
            ("匯出工具", f"{TOOL_NAME} v{TOOL_VERSION}"),
            ("匯出環境", info["host"]),
            ("PDF 字型", self.font_source),
            ("包含工作階段數", str(len(sessions))),
        ]
        story.append(self.kv_table(rows))
        story.append(Spacer(1, 5 * mm))
        story.append(self.para(
            "說明：本 PDF 是對話紀錄的可讀版本。具法律與稽核效力的原始資料是同一封存包內的 raw/*.jsonl 檔案；"
            "各檔案的 SHA-256 雜湊值列在 manifest.json 與 SHA256SUMS。若任何檔案在封存後被修改，"
            "雜湊值就會對不上。PDF 本身的雜湊值也記錄在 SHA256SUMS 中。", "small"))
        story.append(Spacer(1, 3 * mm))
        story.append(self.para(
            "圖例：【使用者】研究者輸入；【AI 回覆】模型輸出；【AI 推理摘要】模型的推理過程（若有）；"
            "【工具呼叫】/【工具結果】AI 代理執行的指令與其輸出；【系統摘要】對話過長時自動產生的上下文壓縮摘要。"
            "系統提示詞與內部設定未列入 PDF，但完整保存在原始 JSONL。", "small"))

        for idx, s in enumerate(sessions, 1):
            meta, events = s["meta"], s["events"]
            story.append(PageBreak())
            story.append(self.para(f"工作階段 {idx}：{meta['title'] or meta['session_id']}", "h1"))
            models = "、".join(f"{m}（{n} 筆回應紀錄）" for m, n in meta["models"].items()) or "（無）"
            story.append(self.kv_table([
                ("Session ID", meta["session_id"]),
                ("開始 (UTC)", meta["first_timestamp_utc"] or "?"),
                ("結束 (UTC)", meta["last_timestamp_utc"] or "?"),
                ("使用模型", models),
                ("Claude Code 版本", "、".join(meta["claude_code_versions"]) or "?"),
                ("介面 / 入口", "、".join(meta["entrypoints"]) or "?"),
                ("工作目錄", "\n".join(meta["working_directories"]) or "?"),
                ("Git 分支", "、".join(meta["git_branches"]) or "—"),
                ("統計", f"使用者提問 {meta['counts']['user_prompts']} 則、AI 文字回覆 "
                         f"{meta['counts']['assistant_text_blocks']} 段、工具呼叫 {meta['counts']['tool_calls']} 次"),
                ("原始檔", meta["archived_as"]),
                ("原始檔 SHA-256", meta["source_sha256"]),
            ]))
            if s.get("subagents"):
                story.append(Spacer(1, 2 * mm))
                story.append(self.para(
                    "此工作階段另有子代理（subagent）紀錄，已一併封存："
                    + "、".join(sa["archived_as"] for sa in s["subagents"]), "small"))
            story.append(Spacer(1, 4 * mm))
            story += self.render_events(events, meta["source_sha256"])
            for sa in s.get("subagents", []):
                story.append(PageBreak())
                story.append(self.para(f"子代理紀錄：{Path(sa['archived_as']).name}", "h2"))
                story.append(self.para(f"SHA-256 {sa['source_sha256']}", "small"))
                story += self.render_events(sa["events"], sa["source_sha256"])

        footer_hash = sessions[0]["meta"]["source_sha256"][:16] if sessions else ""
        font = self.font

        def on_page(canvas, doc):
            canvas.saveState()
            canvas.setFont(font, 7)
            canvas.setFillGray(0.4)
            canvas.drawString(20 * mm, 10 * mm,
                              f"{TOOL_NAME} · 匯出 {info['exported_at_utc']} · 來源 SHA-256 {footer_hash}…")
            canvas.drawRightString(190 * mm, 10 * mm, f"第 {doc.page} 頁")
            canvas.restoreState()

        doc = SimpleDocTemplate(
            str(out_pdf), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
            topMargin=18 * mm, bottomMargin=18 * mm,
            title=f"AI 協作紀錄稽核報告 {info.get('paper_title') or ''}".strip(),
            author=info.get("author") or "", subject="AI collaboration audit trail",
            creator=f"{TOOL_NAME} v{TOOL_VERSION}",
            keywords=",".join(f"sha256:{s['meta']['source_sha256']}" for s in sessions),
        )
        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)

    def render_events(self, events: list[dict], source_hash: str) -> list:
        out = []
        n_prompt = 0
        tool_names = {}
        for ev in events:
            ts = ev["ts"].replace("T", " ").replace("Z", " UTC")[:23] if ev["ts"] else ""
            k = ev["kind"]
            if k == "user":
                n_prompt += 1
                out.append(self.para(f"【使用者】#{n_prompt} · {ts}", "label"))
                out.append(self.para(self.clip(ev["text"], source_hash), "body"))
            elif k == "assistant":
                out.append(self.para(f"【AI 回覆】{ev.get('model', '')} · {ts}", "label"))
                out.append(self.para(self.clip(ev["text"], source_hash), "body"))
            elif k == "thinking":
                if not self.include_thinking:
                    continue
                out.append(self.para(f"【AI 推理摘要】{ev.get('model', '')} · {ts}", "label"))
                out.append(self.para(self.clip(ev["text"], source_hash), "think"))
            elif k == "tool_use":
                tool_names[ev["id"]] = ev["name"]
                out.append(self.para(f"【工具呼叫】{ev['name']} · {ts}", "label"))
                out.append(self.para(self.clip(ev["text"], source_hash), "code"))
            elif k == "tool_result":
                status = "（錯誤）" if ev["error"] else ""
                out.append(self.para(f"【工具結果】{tool_names.get(ev['id'], '')}{status} · {ts}", "label"))
                out.append(self.para(self.clip(ev["text"] or "（無輸出）", source_hash), "code"))
            elif k == "compact":
                out.append(self.para(f"【系統摘要】對話自動壓縮 · {ts}", "label"))
                out.append(self.para(self.clip(ev["text"], source_hash), "think"))
        return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

README_TEXT = """AI 協作紀錄封存包（{tool} v{ver}）
====================================

本資料夾由 {tool} 於 {when} (UTC) 產生，內容如下：

  transcript.pdf   對話紀錄的可讀版本（給審查者閱讀）
  raw/             Claude Code 原始對話紀錄 JSONL（未經修改的原始證據）
  manifest.json    詮釋資料：模型、版本、時間、論文資訊與每個檔案的 SHA-256
  SHA256SUMS       所有檔案的 SHA-256 雜湊值
{tsa_line}
如何驗證檔案沒有被修改
----------------------
macOS / Linux：在本資料夾執行
    shasum -a 256 -c SHA256SUMS
Windows PowerShell：
    Get-FileHash -Algorithm SHA256 <檔名>
並與 SHA256SUMS 中的值比對。全部顯示 OK 即代表檔案與封存當下完全一致。
{tsa_verify}
注意：raw/ 中的原始紀錄包含完整系統訊息與工作環境資訊（例如路徑、帳號 email），
提交給第三方前請先確認可公開的範圍。
"""

TSA_LINE = "  SHA256SUMS.tsr   （若申請成功才會有）第三方可信時間戳記（RFC 3161），證明 SHA256SUMS 於該時間點已存在\n"
TSA_VERIFY = """
驗證可信時間戳（需要 TSA 的 CA 憑證，例如 FreeTSA 的 cacert.pem）：
    openssl ts -verify -data SHA256SUMS -in SHA256SUMS.tsr -CAfile cacert.pem
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Export Claude Code transcripts as an audit package.")
    ap.add_argument("--session", action="append", default=[],
                    help="Session ID to export (repeatable). Default: current session.")
    ap.add_argument("--transcript", action="append", default=[], type=Path,
                    help="Path to a transcript .jsonl to export (repeatable).")
    ap.add_argument("--all-project", action="store_true",
                    help="Export every session recorded for the current project directory.")
    ap.add_argument("--list", action="store_true", help="List sessions for this project and exit.")
    ap.add_argument("--cwd", type=Path, default=Path.cwd(), help="Project directory (default: cwd).")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output parent directory (default: <cwd>/ai-audit-trail-exports).")
    ap.add_argument("--paper-title", default="")
    ap.add_argument("--section", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--affiliation", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                    help=f"Max characters per block in the PDF (0 = no limit, default {DEFAULT_MAX_CHARS}).")
    ap.add_argument("--no-thinking", action="store_true", help="Omit reasoning summaries from the PDF.")
    ap.add_argument("--tsa", nargs="?", const="https://freetsa.org/tsr", default=None,
                    help="Request an RFC 3161 trusted timestamp (only the hash is sent). "
                         "Default TSA: https://freetsa.org/tsr")
    ap.add_argument("--no-zip", action="store_true", help="Do not create a .zip of the package.")
    args = ap.parse_args()

    if args.list:
        list_sessions(args.cwd)
        return 0

    files: list[Path] = [p.expanduser().resolve() for p in args.transcript]
    for sid in args.session:
        p = find_session_file(sid, args.cwd)
        if not p:
            print(f"找不到 session {sid} 的紀錄檔。", file=sys.stderr)
            return 1
        files.append(p)
    if args.all_project:
        files += sorted(project_dir_for(args.cwd).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
        p = find_session_file(sid, args.cwd) if sid else None
        if not p:
            cands = sorted(project_dir_for(args.cwd).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            p = cands[-1] if cands else None
            if p:
                print(f"未取得目前 session ID，改用最近更新的紀錄：{p.name}", file=sys.stderr)
        if not p:
            print("找不到任何對話紀錄。請用 --list 查看，或用 --transcript 指定檔案。", file=sys.stderr)
            return 1
        files.append(p)
    seen, uniq = set(), []
    for p in files:
        if p not in seen and p.exists():
            seen.add(p)
            uniq.append(p)
    files = uniq

    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out_parent = (args.out or args.cwd / "ai-audit-trail-exports").expanduser().resolve()
    pkg = out_parent / f"ai-audit-trail_{stamp}_{files[0].stem[:8]}"
    raw_dir = pkg / "raw"
    raw_dir.mkdir(parents=True, exist_ok=False)

    sessions = []
    for p in files:
        # Snapshot first, then parse the snapshot, so hash and content always match.
        dest = raw_dir / p.name
        shutil.copy2(p, dest)
        entries = read_jsonl(dest)
        meta = session_metadata(dest, entries)
        meta["original_path"] = str(p)
        meta["archived_as"] = str(dest.relative_to(pkg))
        subs = []
        for sp in subagent_files(p):
            sdest = raw_dir / f"{p.stem}_subagents" / sp.name
            sdest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, sdest)
            subs.append({"archived_as": str(sdest.relative_to(pkg)), "original_path": str(sp),
                         "source_sha256": sha256_file(sdest), "events": to_events(read_jsonl(sdest))})
        sessions.append({"meta": meta, "events": to_events(entries), "subagents": subs})

    info = {
        "paper_title": args.paper_title, "section": args.section, "author": args.author,
        "affiliation": args.affiliation, "note": args.note,
        "exported_at_utc": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "exported_at_local": now.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)"),
        "host": f"{platform.system()} {platform.release()} · Python {platform.python_version()}",
    }

    renderer = Renderer(args.max_chars, include_thinking=not args.no_thinking)
    pdf_path = pkg / "transcript.pdf"
    renderer.build(pdf_path, info, sessions)

    manifest = {
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "exported_at_utc": now.isoformat().replace("+00:00", "Z"),
        "exported_at_local": info["exported_at_local"],
        "environment": info["host"],
        "paper": {k: info[k] for k in ("paper_title", "section", "author", "affiliation", "note")},
        "pdf": {"file": "transcript.pdf", "sha256": sha256_file(pdf_path), "font": renderer.font_source, "max_chars_per_block": args.max_chars,
                "includes_thinking": not args.no_thinking},
        "sessions": [
            {**s["meta"], "subagents": [{k: v for k, v in sa.items() if k != "events"}
                                        for sa in s["subagents"]]}
            for s in sessions
        ],
    }
    (pkg / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    tsa_ok, tsa_msg = False, ""
    readme = pkg / "README.txt"

    def write_sums() -> Path:
        sums = pkg / "SHA256SUMS"
        lines = []
        for f in sorted(pkg.rglob("*")):
            if f.is_file() and f.name not in ("SHA256SUMS", "SHA256SUMS.tsq", "SHA256SUMS.tsr"):
                lines.append(f"{sha256_file(f)}  {f.relative_to(pkg).as_posix()}")
        sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return sums

    readme.write_text(README_TEXT.format(
        tool=TOOL_NAME, ver=TOOL_VERSION, when=info["exported_at_utc"],
        tsa_line=TSA_LINE if args.tsa else "", tsa_verify=TSA_VERIFY if args.tsa else ""), encoding="utf-8")
    sums = write_sums()
    if args.tsa:
        tsa_ok, tsa_msg = rfc3161_timestamp(sums, args.tsa)

    zip_path = None
    if not args.no_zip:
        zip_path = pkg.with_suffix(".zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(pkg.rglob("*")):
                if f.is_file():
                    z.write(f, (Path(pkg.name) / f.relative_to(pkg)).as_posix())

    result = {
        "package_dir": str(pkg),
        "pdf": str(pdf_path),
        "pdf_sha256": sha256_file(pdf_path),
        "zip": str(zip_path) if zip_path else None,
        "zip_sha256": sha256_file(zip_path) if zip_path else None,
        "sessions": [{"session_id": s["meta"]["session_id"], "title": s["meta"]["title"],
                      "sha256": s["meta"]["source_sha256"], "models": s["meta"]["models"],
                      "subagents": len(s["subagents"])} for s in sessions],
        "font": renderer.font_source,
        "tsa": None if not args.tsa else {"ok": tsa_ok, "detail": tsa_msg, "url": args.tsa},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
