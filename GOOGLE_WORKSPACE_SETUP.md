# Google Workspace Setup

## 目的

`Vercel` 上で動く `LINE` bot が、ローカルファイルではなく `Google Drive / Google Sheets` から直接プロジェクト情報を読むための設定メモです。

## 方式

この bot は `service account` を使って `Google Drive` と `Google Sheets` を読みます。

必要なデータ:

- README相当のテキスト
- LINE会話ログ
- `申請管理.xlsx`
- 収支スプレッドシート

## 1. Google Cloud で service account を作る

- `Google Cloud Console` で新規プロジェクトを作成
- `Google Drive API` を有効化
- `Google Sheets API` を有効化
- `Service Account` を作成
- `JSON key` を発行

## 2. service account に閲覧権限を付ける

service account の `client_email` を確認して、以下をそのメールアドレスに共有してください。

- READMEファイル
- LINEログファイル
- `申請管理.xlsx`
- 収支スプレッドシート

注意:

- 個人の `My Drive` にあるファイルは、service account に共有しないと読めません。
- フォルダごと共有しても良いですが、まずは必要ファイルだけ共有するのが安全です。

## 3. 各ファイルIDを取る

Google Drive / Sheets のURLからIDを抜きます。

例:

- `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit`
- `https://docs.google.com/document/d/DOC_ID/edit`
- `https://drive.google.com/file/d/FILE_ID/view`

## 4. Vercel の Environment Variables

以下を `Vercel Project Settings > Environment Variables` に登録します。

- `PROJECT_DATA_SOURCE=google_workspace`
- `GEMINI_API_KEY`
- `GEMINI_MODEL`
- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_TARGET_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `GOOGLE_DRIVE_README_FILE_ID`
- `GOOGLE_DRIVE_LINE_LOG_FILE_ID`
- `GOOGLE_DRIVE_APPLICATION_TRACKER_FILE_ID`
- `GOOGLE_SHEETS_REVENUE_SPREADSHEET_ID`
- `GOOGLE_SHEETS_REVENUE_RANGE`

## 5. GOOGLE_SERVICE_ACCOUNT_JSON の入れ方

サービスアカウントのJSONを1行の文字列としてそのまま貼り付けます。

例:

```json
{"type":"service_account","project_id":"...","private_key_id":"...","private_key":"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n","client_email":"...","client_id":"...","token_uri":"https://oauth2.googleapis.com/token"}
```

## 6. この実装が読むもの

- `GOOGLE_DRIVE_README_FILE_ID`
  - Google Doc でも通常ファイルでも可
- `GOOGLE_DRIVE_LINE_LOG_FILE_ID`
  - txt推奨。Google Docでも可
- `GOOGLE_DRIVE_APPLICATION_TRACKER_FILE_ID`
  - `.xlsx`
- `GOOGLE_SHEETS_REVENUE_SPREADSHEET_ID`
  - Google Sheets
- `GOOGLE_SHEETS_REVENUE_RANGE`
  - 例: `Summary!A1:Z50`

## 7. デプロイ後の確認

- `https://<your-vercel-domain>/health` が `{"status":"ok"}` を返すか確認
- LINE Developers の Webhook URL に以下を設定
  - `https://<your-vercel-domain>/webhook`
- `Use webhook` を有効化
- 必要なら `Verify` を押して疎通確認
