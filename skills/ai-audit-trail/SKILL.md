---
name: ai-audit-trail
description: "AI 協作紀錄稽核封存：把 Claude Code 的完整對話紀錄（使用者提問、AI 回覆、推理摘要、工具呼叫與結果）匯出成 PDF，並附上原始 JSONL、SHA-256 雜湊清單、模型與版本等詮釋資料，供學術單位或期刊備查。當使用者想備份、匯出、封存 AI 對話紀錄，或需要提交 AI 使用證明時使用。觸發詞：備份對話、匯出對話紀錄、AI 使用紀錄、AI 協作紀錄、留存備查、稽核、audit trail、export transcript、archive conversation、AI disclosure evidence。"
license: MIT
metadata:
  version: "1.0.0"
---

# ai-audit-trail：AI 協作紀錄稽核封存

把 Claude Code 對話打包成可以拿去備查的封存包：

```
ai-audit-trail_<UTC時間>_<session前8碼>/
├── transcript.pdf   給審查者看的可讀版（含封面詮釋資料、逐則對話）
├── raw/*.jsonl      Claude Code 原始紀錄（未修改的原始證據，含子代理紀錄）
├── manifest.json    模型、Claude Code 版本、起訖時間、論文資訊、各檔 SHA-256
├── SHA256SUMS       全部檔案雜湊值（可用 shasum -a 256 -c 驗證）
├── SHA256SUMS.tsr   （選用）RFC 3161 第三方可信時間戳
└── README.txt       給審查者的驗證說明
```
另外會產生同名的 `.zip` 方便上傳。

## 執行流程

### 1. 確認要填的資料（一次問完，使用者可以略過）

如果使用者呼叫時沒有提供，就用一則訊息詢問下面幾項（全部選填）：
- 論文或計畫名稱（`--paper-title`）
- 對應章節或用途，例如「第三章 方法、程式實作」（`--section`）
- 作者（`--author`）、單位（`--affiliation`）
- 備註（`--note`）

匯出範圍預設是**目前這個對話**。如果使用者想要其他對話或整個專案的所有對話，先執行 `--list` 讓使用者挑選，再改用 `--session <ID>`（可重複）或 `--all-project`。

可信時間戳（`--tsa`）會把 SHA256SUMS 的**雜湊值**（不含任何對話內容）送到外部時間戳服務（預設 FreeTSA）。這是對外傳送資料，**要先徵得使用者同意才能加上**。

### 2. 執行匯出

```bash
uv run --with reportlab python "${CLAUDE_SKILL_DIR}/scripts/export_audit_trail.py" \
  --paper-title "..." --section "..." --author "..." --affiliation "..."
```

- 腳本會從環境變數 `CLAUDE_CODE_SESSION_ID` 找到目前的對話；如果找不到，就改用這個專案最近更新的紀錄，並在 stderr 提示。
- 預設輸出到目前工作目錄下的 `ai-audit-trail-exports/`；要換位置就用 `--out <資料夾>`。
- 沒有 `uv` 的話：先執行 `python3 -m pip install --user reportlab`，再用 `python3` 直接跑腳本。
- 其他選項：
  - `--max-chars N`：PDF 裡每個區塊最多顯示幾個字元，預設 20000，`0` 表示不截斷。被截斷的內容在原始 JSONL 裡仍然完整。
  - `--no-thinking`：PDF 不放 AI 推理摘要。
  - `--transcript <path>`：直接指定一個 JSONL 檔。
  - `--no-zip`：不產生 zip。

腳本最後會印出一段 JSON，內容包含封存路徑、PDF 和 zip 的 SHA-256、各對話的雜湊值、使用的模型，以及時間戳的申請結果。

### 3. 回報給使用者

用繁體中文簡短回報：
1. 封存資料夾和 zip 的路徑，以及 zip 的 SHA-256（建議使用者把這個值另外記在論文附錄或寄給自己，當作封存當下的指紋）。
2. 匯出了哪些對話、用了哪些模型。
3. 驗證方法：在封存資料夾執行 `shasum -a 256 -c SHA256SUMS`。
4. 如果有申請時間戳，附上取得的時間；如果申請失敗，照實說明原因。
5. **隱私提醒**：`raw/` 裡的原始紀錄包含系統訊息、檔案路徑、帳號 email、讀過的檔案內容等資訊。提交給第三方之前，請使用者先確認哪些內容可以公開。不要自己去刪改 raw 檔，改了雜湊值就對不上了。

## 注意事項

- **紀錄只到匯出那一刻為止**。建議在對話結束前最後一步再匯出。匯出這個動作本身也會被記在紀錄裡，這是正常的。
- **Claude Code 預設會自動清除舊的對話紀錄**（`cleanupPeriodDays`，預設 30 天）。長期研究專案建議在 `~/.claude/settings.json` 把它調大，或定期匯出。如果使用者需要，可以協助設定。
- PDF 是「可讀版」，具有證據效力的是 `raw/*.jsonl` 加上 SHA256SUMS。PDF 只顯示對話本身，不顯示系統提示詞和內部設定，這些都完整保存在 raw 檔。
- PDF 字型缺少的字元（例如部分 emoji）會顯示成 `[U+XXXX]`，不會悄悄消失。
- 只支援 Claude Code 的對話紀錄。claude.ai 網頁版的對話請改用網頁版的資料匯出功能。
- 預設字型：macOS 用 Arial Unicode、Windows 用微軟正黑體；也可以用環境變數 `AI_AUDIT_TRAIL_FONT` 指定一個 TrueType 字型。
