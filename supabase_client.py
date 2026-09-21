"""
Supabase(Postgres)への接続を担当する共通モジュール。
fetch_data.py(書き込み)と generate_report.py(読み込み)の両方から使う。

標準ライブラリのみで、SupabaseのREST API(PostgREST)を直接呼び出す。
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error

# ここをご自身のSupabaseプロジェクトの値に置き換えてください。
# (GitHub Actions等で使う場合は、環境変数 SUPABASE_URL / SUPABASE_KEY が
#  設定されていればそちらが優先されます)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://tqvxmzyxiikhxiipdlss.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_9GkFXJZFQDQIBOwDvJNPbw_TaSPAwOu")

TIMEOUT = 30


def _headers(extra=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def upsert(table, rows, on_conflict):
    """
    rowsをtableへupsert(あれば更新、なければ挿入)する。

    引数:
      table: テーブル名
      rows: 挿入する辞書のリスト(キーはカラム名)
      on_conflict: 主キーのカラム名(カンマ区切り、例: "date,tenor")
    戻り値: 送信した件数
    """
    if not rows:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    headers = _headers({"Prefer": "resolution=merge-duplicates,return=minimal"})
    data = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            res.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase upsertエラー({table}): {e.code} {body}") from e
    return len(rows)


def select(table, params=None):
    """
    tableからデータを取得する。

    引数:
      params: クエリパラメータの辞書。例:
        {"select": "*", "order": "date.desc", "limit": "10"}
        条件を付けたい場合は {"tenor": "eq.10年"} のようにPostgRESTの書式で指定する。
    戻り値: 辞書のリスト
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            raw = res.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase selectエラー({table}): {e.code} {body}") from e
    return json.loads(raw)


def delete(table, params):
    """条件に一致する行を削除する。paramsはPostgRESTのフィルタ書式。"""
    url = f"{SUPABASE_URL}/rest/v1/{table}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers(), method="DELETE")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        res.read()
