#!/usr/bin/env python3
"""ai-audit-trail: export Claude Code conversation transcripts as an
audit package (PDF + redacted JSONL + SHA-256 manifest) for academic review.

Personal data (account email, home-directory user name, git identity,
hostname, phone numbers, national IDs, public IPs, API keys/tokens) and
system messages are removed from everything in the shareable package.
Conversation content, including the contents of files the AI read, is kept.
The untouched originals go to a separate .PRIVATE folder that is never zipped.

Run with:  uv run --with reportlab python export_audit_trail.py [options]
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import html
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

TOOL_NAME = "ai-audit-trail"
TOOL_VERSION = "1.1.0"
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


def parse_jsonl(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"type": "_unparseable", "raw": line})
    return out


def read_jsonl(path: Path) -> list[dict]:
    return parse_jsonl(path.read_text(encoding="utf-8", errors="replace"))


def dump_jsonl(entries: list[dict]) -> bytes:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries).encode("utf-8")


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
# Redaction: drop system messages, mask personal data, keep conversation
# ---------------------------------------------------------------------------

KEEP_ENTRY_TYPES = {"user", "assistant", "attachment", "ai-title", "summary", "custom-title"}
KEEP_ENTRY_FIELDS = {"type", "uuid", "parentUuid", "logicalParentUuid", "timestamp", "sessionId", "agentId",
                     "version", "entrypoint", "cwd", "gitBranch", "isSidechain", "isCompactSummary",
                     "message", "attachment", "aiTitle", "summary", "customTitle", "leafUuid"}
KEEP_MESSAGE_FIELDS = {"id", "type", "role", "model", "content", "stop_reason"}
DROP_BLOCK_FIELDS = {"signature", "caller", "cache_control"}
# Attachments are mostly system context; keep only those carrying file content the AI read.
FILE_ATTACHMENT_RE = re.compile(r"file|directory|selected_lines|pdf|image|notebook", re.I)

SYSTEM_TAG_RE = re.compile(r"<(system-reminder)>.*?</\1>\s*", re.S)
SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
]
SECRET_ASSIGN_RE = re.compile(
    r"(?i)(\bbearer\s+|\b(?:api[_-]?key|access[_-]?key|secret|token|passw(?:or)?d)[\"']?\s*[:=]\s*[\"']?)"
    r"(?!\[REDACTED)([^\s\"'`,;]{8,})")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
EMAIL_ALLOW = {"noreply@anthropic.com", "noreply@github.com"}
HOME_RE = re.compile(r"(/Users/|/home/|[A-Za-z]:\\{1,2}Users\\{1,2})([^/\\\s\"'`:]+)")
HOME_KEEP = {"Shared", "Public", "[REDACTED:USER]"}
TWID_RE = re.compile(r"\b[A-Z][12]\d{8}\b")
PHONE_RES = [
    re.compile(r"(?<![\w+])(?:\+886[\s\-]?|0)9\d{2}[\s\-]?\d{3}[\s\-]?\d{3}(?!\d)"),
    re.compile(r"(?<![\w+])\+\d{1,3}[\s\-]\(?\d{1,4}\)?[\s\-]\d{3,4}[\s\-]?\d{3,4}(?!\d)"),
]
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Keys in Claude Code's own metadata whose values identify the account or organization.
ID_KEY_RE = re.compile(r"(?i)^(?:org|organization|account|user)_?(?:uuid|id)$|^(?:user)?email(?:address)?$")
TERM_STOPLIST = {"root", "admin", "user", "users", "localhost", "runner", "ubuntu", "mac", "home", "claude"}


def auto_identity_terms(cwd: Path) -> list[str]:
    """Identifiers of the person running the export, detected locally."""
    terms = set()
    try:
        terms.add(getpass.getuser())
    except Exception:  # noqa: BLE001
        pass
    for key in ("user.name", "user.email"):
        try:
            v = subprocess.run(["git", "config", "--get", key], cwd=cwd, capture_output=True,
                               text=True, timeout=5).stdout.strip()
            if v:
                terms.add(v)
        except (OSError, subprocess.SubprocessError):
            pass
    host = socket.gethostname().split(".")[0]
    if host:
        terms.add(host)
    return sorted(t for t in terms if len(t) >= 4 and t.lower() not in TERM_STOPLIST)


class Redactor:
    def __init__(self, cwd: Path, extra_terms: list[str]):
        self.counts: Counter = Counter()
        self.auto_terms = auto_identity_terms(cwd)
        self.extra_terms = [t for t in extra_terms if t.strip()]
        self._compile()

    def _compile(self):
        self.term_res = [(cat, self._term_re(terms)) for cat, terms in
                         (("TERM", self.extra_terms), ("IDENTITY", self.auto_terms)) if terms]

    def learn(self, entries: list[dict]) -> None:
        """Pick up account identifiers from the transcript's own metadata (account email,
        organization/account UUIDs) so they are also masked wherever they were echoed,
        e.g. in tool output, and so are fragments such as the email's local part."""
        found = set()

        def walk(o, key=""):
            if isinstance(o, dict):
                if o.get("type") == "base64":
                    return
                for k, v in o.items():
                    walk(v, k)
            elif isinstance(o, list):
                for v in o:
                    walk(v, key)
            elif isinstance(o, str):
                if ID_KEY_RE.search(key) and len(o) >= 6 and " " not in o.strip():
                    found.add(o.strip())
                for m in EMAIL_RE.finditer(o):
                    addr = m.group(0)
                    if addr.lower() in EMAIL_ALLOW or addr.lower().startswith("git@"):
                        continue
                    found.add(addr)
                    local = addr.split("@")[0]
                    if len(local) >= 5:
                        found.add(local)
        walk(entries)
        new = {t for t in found if len(t) >= 4 and t.lower() not in TERM_STOPLIST} - set(self.auto_terms)
        if new:
            self.auto_terms = sorted(set(self.auto_terms) | new)
            self._compile()

    @staticmethod
    def _term_re(terms: list[str]):
        parts = []
        for t in sorted(set(terms), key=len, reverse=True):
            e = re.escape(t)
            parts.append(f"(?<![A-Za-z0-9]){e}(?![A-Za-z0-9])" if t.isascii() else e)
        return re.compile("|".join(parts), re.I)

    def _sub(self, regex, cat, s, repl=None):
        def f(m):
            self.counts[cat] += 1
            return repl(m) if repl else f"[REDACTED:{cat}]"
        return regex.sub(f, s)

    def redact_str(self, s: str) -> str:
        if not s:
            return s
        s = self._sub(SYSTEM_TAG_RE, "SYSTEM-MESSAGE", s, lambda m: "")
        for r in SECRET_RES:
            s = self._sub(r, "SECRET", s)
        s = self._sub(SECRET_ASSIGN_RE, "SECRET", s, lambda m: m.group(1) + "[REDACTED:SECRET]")

        def email(m):
            if m.group(0).lower() in EMAIL_ALLOW or m.group(0).lower().startswith("git@"):
                self.counts["EMAIL"] -= 1
                return m.group(0)
            return "[REDACTED:EMAIL]"
        s = self._sub(EMAIL_RE, "EMAIL", s, email)

        def home(m):
            if m.group(2) in HOME_KEEP or m.group(2).startswith("<"):
                self.counts["USER"] -= 1
                return m.group(0)
            return m.group(1) + "[REDACTED:USER]"
        s = self._sub(HOME_RE, "USER", s, home)
        for cat, r in self.term_res:
            s = self._sub(r, cat, s)
        s = self._sub(TWID_RE, "NATIONAL-ID", s)
        for r in PHONE_RES:
            s = self._sub(r, "PHONE", s)

        def ip(m):
            try:
                if ipaddress.ip_address(m.group(0)).is_global:
                    return "[REDACTED:IP]"
            except ValueError:
                pass
            self.counts["IP"] -= 1
            return m.group(0)
        return self._sub(IPV4_RE, "IP", s, ip)

    def redact_obj(self, o):
        if isinstance(o, str):
            return self.redact_str(o)
        if isinstance(o, list):
            return [self.redact_obj(x) for x in o]
        if isinstance(o, dict):
            if o.get("type") == "base64" and "data" in o:  # binary image payload
                return {k: (v if k == "data" else self.redact_obj(v)) for k, v in o.items()}
            return {k: self.redact_obj(v) for k, v in o.items()}
        return o

    def clean_entry(self, e: dict) -> dict | None:
        t = e.get("type")
        if t not in KEEP_ENTRY_TYPES or e.get("isMeta"):
            self.counts["dropped-system-entries"] += 1
            return None
        if t == "attachment" and not FILE_ATTACHMENT_RE.search(str((e.get("attachment") or {}).get("type", ""))):
            self.counts["dropped-system-entries"] += 1
            return None
        out = {k: v for k, v in e.items() if k in KEEP_ENTRY_FIELDS}
        msg = out.get("message")
        if isinstance(msg, dict):
            msg = {k: v for k, v in msg.items() if k in KEEP_MESSAGE_FIELDS}
            if isinstance(msg.get("content"), list):
                msg["content"] = [{k: v for k, v in b.items() if k not in DROP_BLOCK_FIELDS}
                                  if isinstance(b, dict) else b for b in msg["content"]]
            out["message"] = msg
        return self.redact_obj(out)

    def clean(self, entries: list[dict]) -> list[dict]:
        return [c for c in (self.clean_entry(e) for e in entries) if c is not None]

    def summary(self) -> dict:
        return {k: v for k, v in sorted(self.counts.items()) if v > 0}


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
                if c.strip():
                    events.append({"kind": "user", "ts": ts, "text": c})
                continue
            for b in c or []:
                bt = b.get("type")
                if bt == "text":
                    if b.get("text", "").strip():
                        events.append({"kind": "user", "ts": ts, "text": b.get("text", "")})
                elif bt == "image":
                    events.append({"kind": "user", "ts": ts, "text": "[使用者附加圖片，詳見原始 JSONL]"})
                elif bt == "tool_result":
                    events.append({"kind": "tool_result", "ts": ts,
                                   "id": b.get("tool_use_id", ""),
                                   "error": bool(b.get("is_error")),
                                   "text": block_text(b.get("content"))})
        elif t == "attachment":
            att = e.get("attachment") or {}
            if not FILE_ATTACHMENT_RE.search(str(att.get("type", ""))):
                continue
            name = att.get("filename") or att.get("path") or att.get("displayPath") or ""
            body = {k: v for k, v in att.items() if k not in ("type", "filename", "path", "displayPath")}
            events.append({"kind": "attachment", "ts": ts, "name": f"{att.get('type', '')} {name}".strip(),
                           "text": block_text(body.get("content")) if set(body) == {"content"}
                           else json.dumps(body, ensure_ascii=False, indent=2)})
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
                    f"\n\n…［PDF 為求可讀已省略後續 {omitted:,} 字元；完整內容保存在 transcripts/ 的 JSONL "
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
            ("PDF 字型", Path(self.font_source).name),
            ("包含工作階段數", str(len(sessions))),
            ("個資與系統訊息", "已遮蔽／移除（類別與次數見 manifest.json）" if info.get("redacted")
             else "未遮蔽（完整原始紀錄）"),
        ]
        story.append(self.kv_table(rows))
        story.append(Spacer(1, 5 * mm))
        story.append(self.para(
            "說明：本 PDF 是對話紀錄的可讀版本，機器可讀的版本是同一封存包內的 transcripts/*.jsonl。"
            "各檔案的 SHA-256 雜湊值列在 manifest.json 與 SHA256SUMS，任何檔案在封存後被修改，雜湊值就會對不上。"
            + ("為保護個人資料，帳號 email、使用者名稱、金鑰、電話、身分證字號等已替換為 [REDACTED:類別]，"
               "系統訊息已移除；對話內容與 AI 讀取的檔案內容皆完整保留。未遮蔽的原始紀錄由提交者另行保存，"
               "其 SHA-256 記載於下方「原始檔 SHA-256」，必要時可提供比對。" if info.get("redacted") else ""),
            "small"))
        story.append(Spacer(1, 3 * mm))
        story.append(self.para(
            "圖例：【使用者】研究者輸入；【AI 回覆】模型輸出；【AI 推理摘要】模型的推理過程（若有）；"
            "【工具呼叫】/【工具結果】AI 代理執行的指令與其輸出（含 AI 讀取的檔案內容）；【附加檔案】使用者附加給 AI 的檔案；"
            "【系統摘要】對話過長時自動產生的上下文壓縮摘要。", "small"))

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
                ("封存檔", meta["archived_as"]),
                ("封存檔 SHA-256", meta["source_sha256"]),
                ("原始檔 SHA-256" + ("（遮蔽前）" if info.get("redacted") else ""), meta["original_sha256"]),
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
                story.append(self.para(f"封存檔 SHA-256 {sa['source_sha256']}", "small"))
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
            elif k == "attachment":
                out.append(self.para(f"【附加檔案】{ev['name']} · {ts}", "label"))
                out.append(self.para(self.clip(ev["text"] or "（無內容）", source_hash), "code"))
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
  transcripts/     Claude Code 對話紀錄 JSONL（機器可讀）
  manifest.json    詮釋資料：模型、版本、時間、論文資訊與每個檔案的 SHA-256
  SHA256SUMS       所有檔案的 SHA-256 雜湊值
{tsa_line}{redaction_note}
如何驗證檔案沒有被修改
----------------------
macOS / Linux：在本資料夾執行
    shasum -a 256 -c SHA256SUMS
Windows PowerShell：
    Get-FileHash -Algorithm SHA256 <檔名>
並與 SHA256SUMS 中的值比對。全部顯示 OK 即代表檔案與封存當下完全一致。
{tsa_verify}"""

REDACTION_NOTE = """
個人資料保護
------------
為保護個人資料，本封存包已做以下處理（各類別次數見 manifest.json 的 redaction）：
  - 移除系統訊息與內部設定（系統提示詞、帳號與組織資訊、環境資訊等）
  - 帳號 email、使用者名稱、git 身分、主機名稱、電話、身分證字號、公開 IP、
    API 金鑰與密碼等，替換為 [REDACTED:類別]
對話內容（使用者提問、AI 回覆、工具呼叫與結果、AI 讀取的檔案內容）完整保留。
未遮蔽的原始紀錄由提交者另行保存；其 SHA-256 記載於 manifest.json 的
original_sha256，必要時可提供原始檔比對。
"""

NO_REDACTION_NOTE = """
注意：transcripts/ 為未遮蔽的完整原始紀錄，包含系統訊息與工作環境資訊
（例如路徑、帳號 email），提交給第三方前請先確認可公開的範圍。
"""

PRIVATE_README = """這是未遮蔽的原始對話紀錄（{tool} v{ver}，{when} UTC）
==================================================

請勿提交、上傳或分享這個資料夾。它包含完整的系統訊息、帳號 email、
檔案路徑等個人資料，只供你自己保存，在審查單位要求時用來證明遮蔽版的來源。

對應的封存包：{pkg}
驗證：在本資料夾執行  shasum -a 256 -c SHA256SUMS
其中 raw/ 各檔的雜湊值應與封存包 manifest.json 裡的 original_sha256 相同。
"""

TSA_LINE = "  SHA256SUMS.tsr   （若申請成功才會有）第三方可信時間戳記（RFC 3161），證明 SHA256SUMS 於該時間點已存在\n"
TSA_VERIFY = """
驗證可信時間戳（需要 TSA 的 CA 憑證，例如 FreeTSA 的 cacert.pem）：
    openssl ts -verify -data SHA256SUMS -in SHA256SUMS.tsr -CAfile cacert.pem
"""


def write_sums(folder: Path) -> Path:
    sums = folder / "SHA256SUMS"
    lines = []
    for f in sorted(folder.rglob("*")):
        if f.is_file() and f.name not in ("SHA256SUMS", "SHA256SUMS.tsq", "SHA256SUMS.tsr"):
            lines.append(f"{sha256_file(f)}  {f.relative_to(folder).as_posix()}")
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sums


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
    ap.add_argument("--redact-term", action="append", default=[],
                    help="Extra word to mask everywhere, e.g. a real name or student ID (repeatable).")
    ap.add_argument("--no-redact", action="store_true",
                    help="Put the untouched originals in the package (no masking, system messages kept).")
    ap.add_argument("--no-private-copy", action="store_true",
                    help="Do not keep the unredacted originals in a separate .PRIVATE folder.")
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
    default_out = args.out is None
    out_parent = (args.out or args.cwd / "ai-audit-trail-exports").expanduser().resolve()
    out_parent.mkdir(parents=True, exist_ok=True)
    if default_out and not (out_parent / ".gitignore").exists():
        # Keep exports (especially .PRIVATE originals) out of the user's git repo.
        (out_parent / ".gitignore").write_text("*\n", encoding="utf-8")
    pkg = out_parent / f"ai-audit-trail_{stamp}_{files[0].stem[:8]}"
    tx_dir = pkg / "transcripts"
    tx_dir.mkdir(parents=True, exist_ok=False)

    redactor = None if args.no_redact else Redactor(args.cwd, args.redact_term)
    private = None
    if redactor and not args.no_private_copy:
        private = out_parent / f"{pkg.name}.PRIVATE"
        (private / "raw").mkdir(parents=True, exist_ok=False)

    def archive(src: Path, rel: Path) -> tuple[list[dict], dict]:
        """Snapshot `src` once, keep the original privately, write the shareable copy."""
        original = src.read_bytes()
        entries = parse_jsonl(original.decode("utf-8", errors="replace"))
        if private:
            (private / "raw" / rel).parent.mkdir(parents=True, exist_ok=True)
            (private / "raw" / rel).write_bytes(original)
        if redactor:
            entries = redactor.clean(entries)
            data = dump_jsonl(entries)
        else:
            data = original
        dest = tx_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return entries, {"archived_as": dest.relative_to(pkg).as_posix(),
                         "source_sha256": hashlib.sha256(data).hexdigest(),
                         "original_sha256": hashlib.sha256(original).hexdigest(),
                         "original_bytes": len(original)}

    if redactor:
        for p in files:
            for src in [p, *subagent_files(p)]:
                redactor.learn(read_jsonl(src))

    sessions = []
    for p in files:
        entries, hashes = archive(p, Path(p.name))
        meta = session_metadata(tx_dir / p.name, entries)
        meta["session_id"] = p.stem
        meta.update(hashes)
        subs = []
        for sp in subagent_files(p):
            sentries, shashes = archive(sp, Path(f"{p.stem}_subagents") / sp.name)
            subs.append({**shashes, "events": to_events(sentries)})
        sessions.append({"meta": meta, "events": to_events(entries), "subagents": subs})

    info = {
        "paper_title": args.paper_title, "section": args.section, "author": args.author,
        "affiliation": args.affiliation, "note": args.note, "redacted": bool(redactor),
        "exported_at_utc": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "exported_at_local": now.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)"),
        "host": f"{platform.system()} {platform.release()} · Python {platform.python_version()}",
    }

    renderer = Renderer(args.max_chars, include_thinking=not args.no_thinking)
    pdf_path = pkg / "transcript.pdf"
    renderer.build(pdf_path, info, sessions)

    redaction = None
    if redactor:
        redaction = {
            "enabled": True,
            "counts": redactor.summary(),
            "auto_identity_terms": len(redactor.auto_terms),
            "user_supplied_terms": len(redactor.extra_terms),
            "removed": ["system messages and internal settings", "non-file attachments",
                        "thinking signatures", "request/telemetry fields", "duplicated toolUseResult payloads"],
            "kept": ["user prompts", "assistant replies", "reasoning summaries", "tool calls and results",
                     "contents of files the AI read", "file attachments", "timestamps", "model IDs"],
            "note": "Masked values are replaced with [REDACTED:<CATEGORY>]. The masked values themselves "
                    "are not recorded anywhere in this package.",
        }
    manifest = {
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "exported_at_utc": now.isoformat().replace("+00:00", "Z"),
        "exported_at_local": info["exported_at_local"],
        "environment": info["host"],
        "paper": {k: info[k] for k in ("paper_title", "section", "author", "affiliation", "note")},
        "redaction": redaction or {"enabled": False},
        "pdf": {"file": "transcript.pdf", "sha256": sha256_file(pdf_path),
                "font": Path(renderer.font_source).name, "max_chars_per_block": args.max_chars,
                "includes_thinking": not args.no_thinking},
        "sessions": [
            {**s["meta"], "subagents": [{k: v for k, v in sa.items() if k != "events"}
                                        for sa in s["subagents"]]}
            for s in sessions
        ],
    }
    (pkg / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    (pkg / "README.txt").write_text(README_TEXT.format(
        tool=TOOL_NAME, ver=TOOL_VERSION, when=info["exported_at_utc"],
        tsa_line=TSA_LINE if args.tsa else "",
        redaction_note=REDACTION_NOTE if redactor else NO_REDACTION_NOTE,
        tsa_verify=TSA_VERIFY if args.tsa else ""), encoding="utf-8")
    sums = write_sums(pkg)
    tsa_ok, tsa_msg = False, ""
    if args.tsa:
        tsa_ok, tsa_msg = rfc3161_timestamp(sums, args.tsa)

    if private:
        (private / "README.txt").write_text(PRIVATE_README.format(
            tool=TOOL_NAME, ver=TOOL_VERSION, when=info["exported_at_utc"], pkg=pkg.name), encoding="utf-8")
        write_sums(private)

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
        "private_originals": str(private) if private else None,
        "redaction": redaction["counts"] if redaction else "disabled",
        "sessions": [{"session_id": s["meta"]["session_id"], "title": s["meta"]["title"],
                      "sha256": s["meta"]["source_sha256"], "original_sha256": s["meta"]["original_sha256"],
                      "models": s["meta"]["models"], "subagents": len(s["subagents"])} for s in sessions],
        "font": renderer.font_source,
        "tsa": None if not args.tsa else {"ok": tsa_ok, "detail": tsa_msg, "url": args.tsa},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
