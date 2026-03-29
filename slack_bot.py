"""
タビ男 - Slack AI社員
機能:
  - @メンションで質問に回答（Claude AI）
  - 日報を自動検出 → GoogleスプレッドシートにKPI記録
  - 契約書テンプレートをGoogle Driveから検索・提供
  - 毎朝9時に前日の進捗サマリーを投稿
"""

import os
import re
import json
import logging
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from anthropic import Anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ─── クライアント初期化 ────────────────────────────────────────────────────────

slack_app = App(token=os.environ["SLACK_BOT_TOKEN"])
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Google認証（ファイルまたは環境変数どちらでも可）
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def _get_google_creds():
    creds_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_env:
        info = json.loads(creds_env)
    else:
        with open("google_credentials.json", encoding="utf-8") as f:
            info = json.load(f)
    return service_account.Credentials.from_service_account_info(info, scopes=GOOGLE_SCOPES)

google_creds   = _get_google_creds()
sheets_service = build("sheets", "v4",  credentials=google_creds)
drive_service  = build("drive",  "v3",  credentials=google_creds)

SPREADSHEET_ID        = os.environ["SPREADSHEET_ID"]
DAILY_REPORT_CHANNEL  = os.environ.get("DAILY_REPORT_CHANNEL", "")  # 朝のサマリー投稿先チャンネルID
SHEET_NAME            = "日報データ"   # スプレッドシートのシート名

# ─── Claude ──────────────────────────────────────────────────────────────────

HARRISON_SYSTEM = """あなたは不動産M&A会社のAIアシスタント「タビ男」です。
日本語で端的に回答してください。余計な表や長い説明は不要です。

重要：あなたはGoogle DriveとGoogleスプレッドシートに実際にアクセスできます。
「できません」「アクセスできません」「直接行うことができません」とは絶対に言わないでください。
契約書・雛形を求められたら必ず検索を実行し、結果のリンクを返してください。"""

def ask_claude(user_message: str, system: str = HARRISON_SYSTEM) -> str:
    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text

# ─── 日報パース ───────────────────────────────────────────────────────────────

def is_daily_report(text: str) -> bool:
    return "日報" in text and ("KPI" in text or "本日の進捗" in text)

def parse_daily_report(text: str) -> dict:
    """Claudeが日報テキストからKPIデータをJSONで抽出する"""
    prompt = f"""以下の日報テキストから数値データをすべて抽出し、JSONのみで返してください（コードブロック不要）。
数値が空欄・未記入の場合は null としてください。

日報テキスト:
{text}

出力形式（このJSONのみ返してください）:
{{
  "date": "YYYY/MM/DD",
  "person": "担当者名",
  "daily": {{
    "買い手面談数": {{"実績": 数値またはnull, "目標": 数値またはnull}},
    "内見数": {{"実績": 数値またはnull, "目標": 数値またはnull}},
    "アポ数": {{"実績": 数値またはnull, "目標": 数値またはnull}}
  }},
  "weekly": {{
    "買い手面談数": {{"実績": 数値またはnull, "目標": 数値またはnull}},
    "内見数": {{"実績": 数値またはnull, "目標": 数値またはnull}},
    "アポ数": {{"実績": 数値またはnull, "目標": 数値またはnull}}
  }},
  "monthly": {{
    "事業譲渡契約": {{"実績": 数値またはnull, "目標": 数値またはnull}},
    "基本合意": {{"実績": 数値またはnull, "目標": 数値またはnull}},
    "意向表明数": {{"実績": 数値またはnull, "目標": 数値またはnull}},
    "自己案件受託数": {{"実績": 数値またはnull, "目標": 数値またはnull}},
    "買い手面談数": {{"実績": 数値またはnull, "目標": 数値またはnull}},
    "内見数": {{"実績": 数値またはnull, "目標": 数値またはnull}}
  }}
}}"""

    result = ask_claude(prompt, system="データ抽出のみ行うアシスタントです。指定されたJSON形式のみを返してください。")
    try:
        return json.loads(result.strip())
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"JSONパース失敗: {result[:200]}")

# ─── Google Sheets ─────────────────────────────────────────────────────────────

def _ensure_sheet_header():
    """シートにヘッダー行がなければ追加する"""
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:F1"
    ).execute()
    if not result.get("values"):
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1:F1",
            valueInputOption="USER_ENTERED",
            body={"values": [["日付", "担当者", "区分", "KPI項目", "実績", "目標"]]}
        ).execute()

def write_report_to_sheets(report: dict):
    """日報データをスプレッドシートに追記する"""
    _ensure_sheet_header()
    date   = report.get("date", "")
    person = report.get("person", "")
    rows   = []

    for section, kpis in [("日次", report.get("daily", {})),
                           ("週次", report.get("weekly", {})),
                           ("月次", report.get("monthly", {}))]:
        for kpi, vals in kpis.items():
            rows.append([date, person, section, kpi,
                         vals.get("実績"), vals.get("目標")])

    if not rows:
        return

    sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A:F",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()
    logging.info(f"シート書き込み完了: {person} {date} ({len(rows)}行)")

def get_today_reports_summary(target_date: str = None) -> str:
    """指定日（デフォルト: 今日）の全スタッフ進捗をテキストで返す"""
    from datetime import date
    if not target_date:
        target_date = date.today().strftime("%Y/%m/%d")

    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A:F"
    ).execute()
    values = result.get("values", [])

    rows = [r for r in values[1:] if r and r[0] == target_date]
    if not rows:
        return f"{target_date} のデータはまだ記録されていません。"

    by_person: dict[str, dict[str, list]] = {}
    for row in rows:
        if len(row) < 5:
            continue
        d, person, section, kpi = row[0], row[1], row[2], row[3]
        actual = row[4] if len(row) > 4 else "-"
        target = row[5] if len(row) > 5 else "-"
        by_person.setdefault(person, {}).setdefault(section, []).append(
            f"  {kpi}: {actual}/{target}"
        )

    lines = [f"📊 *{target_date} 進捗サマリー*"]
    for person, sections in by_person.items():
        lines.append(f"\n*{person}*")
        for section, items in sections.items():
            lines.append(f"  【{section}】")
            lines.extend(items)
    return "\n".join(lines)

# ─── Google Drive ──────────────────────────────────────────────────────────────

def search_contracts(query: str) -> list[dict]:
    """Google Driveから契約書を検索する"""
    # 検索キーワード抽出
    keyword = re.sub(r'<@[A-Z0-9]+>', '', query)
    for remove in ["契約書", "ひな型", "テンプレ", "テンプレート", "出して", "見せて", "ください", "を"]:
        keyword = keyword.replace(remove, "")
    keyword = keyword.strip() or "契約"

    response = drive_service.files().list(
        q=f"name contains '{keyword}' and trashed=false",
        spaces="drive",
        fields="files(id, name, webViewLink, mimeType, modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=5
    ).execute()
    return response.get("files", [])

# ─── Slack イベントハンドラ ────────────────────────────────────────────────────

@slack_app.event("app_mention")
def handle_mention(event, say):
    text       = event.get("text", "")
    thread_ts  = event.get("thread_ts") or event.get("ts")
    clean_text = re.sub(r'<@[A-Z0-9]+>', '', text).strip()

    # 契約書検索
    if any(kw in text for kw in ["契約書", "ひな型", "テンプレ", "NDA", "秘密保持", "雛形", "ひな形", "基本合意", "意向表明", "LOI", "MOA"]):
        say(text="契約書を検索中です...", thread_ts=thread_ts)
        files = search_contracts(text)
        if files:
            links = "\n".join([f"• <{f['webViewLink']}|{f['name']}>" for f in files])
            say(text=f"📄 以下の契約書が見つかりました：\n{links}", thread_ts=thread_ts)
        else:
            say(text="該当する契約書が見つかりませんでした。\n別のキーワードで試してみてください（例：「事業譲渡契約書」）",
                thread_ts=thread_ts)
        return

    # 進捗サマリー
    if any(kw in text for kw in ["進捗", "サマリー", "まとめ", "今日の状況", "日報確認"]):
        summary = get_today_reports_summary()
        say(text=summary, thread_ts=thread_ts)
        return

    # 一般質問 → Claude
    response = ask_claude(clean_text)
    say(text=response, thread_ts=thread_ts)


@slack_app.event("message")
def handle_message(event, say):
    # ボット自身・編集・削除メッセージは無視
    if event.get("bot_id") or event.get("subtype"):
        return

    text     = event.get("text", "")
    thread_ts = event.get("thread_ts") or event.get("ts")

    if not is_daily_report(text):
        return

    say(text="📝 日報を受け取りました。スプレッドシートに記録中...", thread_ts=thread_ts)
    try:
        report = parse_daily_report(text)
        write_report_to_sheets(report)
        person = report.get("person", "不明")
        date   = report.get("date", "")
        say(text=f"✅ *{person}* さんの {date} 日報を記録しました！", thread_ts=thread_ts)
    except Exception as e:
        logging.error(f"日報処理エラー: {e}")
        say(text="⚠️ 日報の処理でエラーが発生しました。フォーマットを確認して再投稿してください。",
            thread_ts=thread_ts)


# ─── 定期タスク（毎朝9時）────────────────────────────────────────────────────

def morning_summary_job():
    """前日の進捗サマリーを毎朝9時に投稿"""
    if not DAILY_REPORT_CHANNEL:
        return
    from datetime import date, timedelta
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y/%m/%d")
    summary = get_today_reports_summary(yesterday)
    slack_app.client.chat_postMessage(
        channel=DAILY_REPORT_CHANNEL,
        text=f"おはようございます！昨日（{yesterday}）の進捗です。\n\n{summary}"
    )
    logging.info("朝のサマリー送信完了")

# ─── 起動 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
    scheduler.add_job(morning_summary_job, "cron", hour=9, minute=0)
    scheduler.start()
    logging.info("タビ男起動中...")
    handler = SocketModeHandler(slack_app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
