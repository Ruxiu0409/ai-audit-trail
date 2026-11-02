<p align="center">
  <img src=".claude-plugin/icon.png" width="128" alt="ai-audit-trail icon">
</p>

<h1 align="center">ai-audit-trail</h1>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-TW.md">繁體中文</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <b>日本語</b>
</p>

Claude Code での AI との対話を、**検証可能な監査用アーカイブ**としてエクスポートする Skill／プラグインです。大学・研究機関や学術誌への AI 利用の開示に使えます。

## なぜ必要か

AI 利用の記録をすべて保存し、求められたら提出するよう研究者に求める大学や学術誌が増えています。スクリーンショットやコピー＆ペーストでは、記録が完全であることも、後から改変されていないことも示せません。この Skill は対話を次のようにまとめます。

```
ai-audit-trail_<UTC時刻>_<セッションID先頭8桁>/
├── transcript.pdf   閲覧用：表紙のメタデータ＋全ターン（質問、回答、推論の要約、ツール呼び出しと結果）
├── raw/*.jsonl      Claude Code の元のトランスクリプト（無加工の原本、サブエージェント分を含む）
├── manifest.json    モデル、Claude Code のバージョン、期間、論文情報、各ファイルの SHA-256
├── SHA256SUMS       全ファイルのハッシュ値
├── SHA256SUMS.tsr   （任意）RFC 3161 タイムスタンプ
└── README.txt       審査者向けの検証手順
```
同名の `.zip` も作成されます。

## インストール

**方法 1：プラグインとしてインストール（推奨）**

Claude Code で次を実行します。
```
/plugin marketplace add Ruxiu0409/ai-audit-trail
/plugin install ai-audit-trail@ai-audit-trail
```

**方法 2：Skill を手動でコピー**

```bash
git clone https://github.com/Ruxiu0409/ai-audit-trail.git
cp -r ai-audit-trail/skills/ai-audit-trail ~/.claude/skills/
```

**必要なもの**：Python 3.9 以上と [`uv`](https://docs.astral.sh/uv/)（`reportlab` を自動でインストールします）。`uv` がない場合は `pip install reportlab` を実行してください。

## 使い方

Claude Code で「この対話を監査用にエクスポートして」と頼むか、`/ai-audit-trail` を実行します。Claude が論文タイトル、章、著者、所属を尋ねます（いずれも省略可）。そのあとアーカイブが作成されます。

スクリプトを直接実行することもできます。

```bash
uv run --with reportlab python skills/ai-audit-trail/scripts/export_audit_trail.py \
  --paper-title "私の論文" --section "第3章 手法" --author "山田太郎"
```

| オプション | 説明 |
|---|---|
| `--list` | このプロジェクトのセッションを一覧表示 |
| `--session ID` | 指定したセッションをエクスポート（複数指定可） |
| `--all-project` | このプロジェクトの全セッションをエクスポート |
| `--transcript PATH` | JSONL ファイルを直接指定 |
| `--out DIR` | 出力先（既定は `./ai-audit-trail-exports/`） |
| `--tsa [URL]` | RFC 3161 タイムスタンプを取得。送信するのはハッシュ値のみ（既定は FreeTSA） |
| `--max-chars N` | PDF の各ブロックの最大文字数（既定 20000、`0` で無制限） |
| `--no-thinking` | 推論の要約を PDF に含めない |
| `--no-zip` | zip を作成しない |

## 検証

```bash
cd ai-audit-trail_xxx/
shasum -a 256 -c SHA256SUMS     # すべて OK と表示されれば、アーカイブ時から変更されていません
# タイムスタンプを取得した場合のみ：
openssl ts -verify -data SHA256SUMS -in SHA256SUMS.tsr -CAfile cacert.pem
```

## 注意事項

- **プライバシー**：`raw/` にはシステムメッセージ全文、ファイルパス、アカウントのメールアドレス、AI が読んだファイルの内容が含まれます。共有する前に必ず確認してください。
- **保存期間**：Claude Code は `cleanupPeriodDays`（既定 30 日）より古いトランスクリプトを削除します。長期の研究では設定値を大きくするか、定期的にエクスポートしてください。
- **対象範囲**：Claude Code の対話のみ対応しています。claude.ai の対話は、claude.ai のデータエクスポート機能を使ってください。
- **法的な位置づけ**：このツールは完全性を技術的に証明するものです。受け入れられるかどうかは、所属機関や学術誌の判断によります。

## ライセンス

[MIT](LICENSE)
