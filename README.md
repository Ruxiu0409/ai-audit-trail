# ai-audit-trail

把 Claude Code 的 AI 協作對話匯出成**可驗證的稽核封存包**，供學術單位、期刊或審查委員備查。

Export your Claude Code conversations as a **verifiable audit package** (PDF + raw JSONL + SHA-256 manifest, with an optional RFC 3161 trusted timestamp) for academic AI-use disclosure. [English below](#english).

---

## 為什麼需要

越來越多學術單位和期刊要求研究者保留 AI 協作的完整紀錄，必要時提供備查。單純截圖或複製貼上，很難證明紀錄完整，也很難證明內容沒有被事後改過。這個 Skill 會把對話打包成：

```
ai-audit-trail_<UTC時間>_<session前8碼>/
├── transcript.pdf   可讀版：封面詮釋資料＋逐則對話（提問、回覆、推理摘要、工具呼叫與結果）
├── raw/*.jsonl      Claude Code 原始紀錄（原始證據，含子代理紀錄）
├── manifest.json    模型、Claude Code 版本、起訖時間、論文資訊、各檔 SHA-256
├── SHA256SUMS       全部檔案雜湊值
├── SHA256SUMS.tsr   （選用）RFC 3161 第三方可信時間戳
└── README.txt       給審查者的驗證說明
```
另外會產生同名 `.zip`。

## 安裝

**方法一：用 plugin 安裝（建議）**

在 Claude Code 裡執行：
```
/plugin marketplace add Ruxiu0409/ai-audit-trail
/plugin install ai-audit-trail@ai-audit-trail
```

**方法二：手動複製 Skill**

```bash
git clone https://github.com/Ruxiu0409/ai-audit-trail.git
cp -r ai-audit-trail/skills/ai-audit-trail ~/.claude/skills/
```

**需求**：Python 3.9 以上，以及 [`uv`](https://docs.astral.sh/uv/)（會自動安裝 `reportlab`）。沒有 `uv` 的話，執行 `pip install reportlab` 也可以。

## 使用

在 Claude Code 對話中說「幫我備份這次對話」或「匯出 AI 協作紀錄」，或者直接輸入 `/ai-audit-trail`。Claude 會先問你論文名稱、章節、作者等資訊（都可以略過），再產生封存包。

也可以直接執行腳本：

```bash
uv run --with reportlab python skills/ai-audit-trail/scripts/export_audit_trail.py \
  --paper-title "我的論文" --section "第三章 方法" --author "王小明"
```

| 選項 | 說明 |
|---|---|
| `--list` | 列出這個專案的所有對話 |
| `--session ID` | 匯出指定的對話（可以重複使用） |
| `--all-project` | 匯出這個專案的所有對話 |
| `--transcript PATH` | 直接指定一個 JSONL 檔 |
| `--out DIR` | 輸出位置（預設 `./ai-audit-trail-exports/`） |
| `--tsa [URL]` | 申請 RFC 3161 可信時間戳，只送出雜湊值（預設用 FreeTSA） |
| `--max-chars N` | PDF 中每個區塊的字數上限，預設 20000，`0` 表示不截斷 |
| `--no-thinking` | PDF 不放推理摘要 |
| `--no-zip` | 不產生 zip |

## 驗證

```bash
cd ai-audit-trail_xxx/
shasum -a 256 -c SHA256SUMS          # 每個檔案都顯示 OK，代表與封存當下一致
openssl ts -verify -data SHA256SUMS -in SHA256SUMS.tsr -CAfile cacert.pem   # 有申請時間戳時才需要
```

## 注意

- **隱私**：`raw/` 裡包含完整系統訊息、檔案路徑、帳號 email，以及 AI 讀過的檔案內容。提交前請先確認哪些可以公開。
- **保存期限**：Claude Code 預設會刪除 30 天以前的對話紀錄（設定項 `cleanupPeriodDays`）。長期研究建議把它調大，或定期匯出。
- **適用範圍**：只支援 Claude Code 的對話。claude.ai 網頁版請用網頁內建的資料匯出功能。
- **法律效力**：這個工具提供技術上的完整性證明，但不代表任何單位一定會採認。請以你所屬單位或期刊的規定為準。

---

## English

**ai-audit-trail** is a Claude Code skill/plugin that packages a conversation into an audit bundle:
a human-readable PDF (user prompts, assistant replies, reasoning summaries, tool calls and results, all timestamped),
the untouched raw JSONL transcript(s) including subagents, a `manifest.json` with model IDs, Claude Code versions,
session time range and paper metadata, and a `SHA256SUMS` file. Optionally, `--tsa` obtains an RFC 3161 trusted
timestamp over `SHA256SUMS`. Only the hash is sent, never the conversation.

**Install:** `/plugin marketplace add Ruxiu0409/ai-audit-trail`, then `/plugin install ai-audit-trail@ai-audit-trail`.
Alternatively, copy `skills/ai-audit-trail` into `~/.claude/skills/`.

**Use:** ask Claude to "export this conversation for audit", or run `/ai-audit-trail`.
Verify a package with `shasum -a 256 -c SHA256SUMS`.

**Caveats:** raw transcripts contain system messages, paths, and account info, so review them before sharing.
Claude Code deletes transcripts older than `cleanupPeriodDays` (30 by default). This tool proves integrity
technically; whether a given institution accepts it is up to that institution.

## License

[MIT](LICENSE)
