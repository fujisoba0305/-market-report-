"""
投資判断補助アプリ - 公式データ取得スクリプト(個人利用版・標準ライブラリのみ版)

このバージョンは pandas / requests / lxml を使わず、
Pythonに標準で入っている機能だけで動くようにしています。
(会社PCや一部のWindows環境では、pip でインストールした
 pandasなどの外部ライブラリがセキュリティポリシーでブロックされる
 ことがあるため、より確実に動く形にしました)

必要な準備:
  1. e-Stat: https://www.e-stat.go.jp/ でユーザー登録し、
     マイページから「アプリケーションID」を発行してください(無料)。
     -> ESTAT_APP_ID に設定
  2. EIA (米エネルギー情報局): https://www.eia.gov/opendata/register.php
     で無料APIキーを取得してください。
     -> EIA_API_KEY に設定
  3. 日本銀行API・財務省CSV・米財務省XMLはキー不要です。

依存パッケージ: 不要(Python標準ライブラリのみ)

実行方法:
  python fetch_data.py
"""

import csv
import io
import json
import os
import re
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

import supabase_client as sb

# ============================================================
# 設定(ご自身のキーに書き換えてください)
# ============================================================
ESTAT_APP_ID = os.environ.get("ESTAT_APP_ID", "YOUR_ESTAT_APP_ID")
EIA_API_KEY = os.environ.get("EIA_API_KEY", "YOUR_EIA_API_KEY")

TIMEOUT = 20  # seconds
DB_PATH = "market_data.db"


def _http_get(url, params=None):
    """標準ライブラリだけでHTTP GETするヘルパー関数。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "personal-use-script"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read()


def _strip_ns(tag):
    """XMLタグ名から名前空間部分を取り除く(例: '{...}NEW_DATE' -> 'NEW_DATE')。"""
    return tag.split("}")[-1] if "}" in tag else tag


# ============================================================
# ① 日本国債金利(財務省)
# ============================================================
def fetch_jgb_yields():
    """
    財務省「国債金利情報」から日次の国債金利(1~40年)を取得する。
    出典: 財務省 https://www.mof.go.jp/jgbs/reference/interest_rate/
    利用条件: 公共データ利用規約(PDL1.0)適用、商用利用可、出典表示のみ条件

    戻り値: [{"date": "2026/9/16", "1年": "0.500", "2年": "..."}, ...]
            のような辞書のリスト
    """
    url = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
    raw = _http_get(url)
    text = raw.decode("shift_jis", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    header = rows[1]  # 1行目はタイトル行なのでスキップ
    data_rows = rows[2:]
    result = []
    for r in data_rows:
        if not r or not r[0]:
            continue
        result.append(dict(zip(header, r)))
    return result


# ============================================================
# ② 米国債金利(米財務省 Daily Treasury Par Yield Curve Rates)
# ============================================================
def fetch_us_treasury_yields(year=None):
    """
    米財務省の日次国債金利(2/5/10/20/30年等)をXMLフィードから取得する。
    出典: U.S. Department of the Treasury
    利用条件: 米国政府著作物のためパブリックドメイン、自由利用可

    戻り値: [{"NEW_DATE": "...", "BC_2YEAR": "...", ...}, ...]
    """
    if year is None:
        year = datetime.now().year
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/"
        "interest-rates/pages/xmlview"
    )
    params = {
        "data": "daily_treasury_yield_curve",
        "field_tdr_date_value": year,
    }
    raw = _http_get(url, params)
    root = ET.fromstring(raw)

    result = []
    for elem in root.iter():
        if _strip_ns(elem.tag) == "properties":
            record = {}
            for child in elem:
                record[_strip_ns(child.tag)] = child.text
            result.append(record)
    return result


# ============================================================
# ③ 日銀 時系列統計データ(短観・金利・為替など)
# ============================================================
def fetch_boj_series(db, codes, start_date, end_date, lang="jp"):
    """
    日銀の時系列統計データ検索サイトAPI(2026年2月開始)から
    指定した系列コードのデータを取得する。

    引数:
      db: データベース名(例: "CO")
      codes: 系列コードのリスト
             ※系列コードは https://www.stat-search.boj.or.jp/ の
               検索画面で対象の統計を検索すると確認できます。
      start_date, end_date: "YYYYMM" または "YYYY" 形式
    出典: 日本銀行 時系列統計データ検索サイト
    利用条件: 引用としての利用は可。商用目的での転載・複製は
              日銀調査統計局へ要事前相談(個人利用なら通常問題なし)。
    """
    base = "https://www.stat-search.boj.or.jp/api/v1/getDataCode"
    params = {
        "format": "json",
        "lang": lang,
        "db": db,
        "startDate": start_date,
        "endDate": end_date,
        "code": ",".join(codes),
    }
    raw = _http_get(base, params)
    return json.loads(raw)


def _find_series_list(obj):
    """
    日銀APIのJSONレスポンスから、SERIES_CODEを含むレコードのリストを
    再帰的に探し出すヘルパー関数(正確なラップ構造が不明でも動くようにする)。
    """
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_series_list(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "SERIES_CODE" in obj[0]:
            return obj
        for item in obj:
            found = _find_series_list(item)
            if found is not None:
                return found
    return None


def fetch_boj_metadata(db, lang="jp"):
    """
    日銀メタデータAPIから、指定したDBに含まれる全系列の情報
    (系列コード・系列名称・単位・期種など)を取得する。

    db名の例:
      CO   = 短観
      FM01 = 無担保コールO/N物レート
      FM08 = 外国為替市況(ドル円など)
      IR01～IR04 = 預金・貸出関連金利
    出典: 日本銀行 時系列統計データ検索サイト
    """
    url = "https://www.stat-search.boj.or.jp/api/v1/getMetadata"
    params = {"format": "json", "lang": lang, "db": db}
    raw = _http_get(url, params)
    data = json.loads(raw)
    records = _find_series_list(data)
    return records or []


def search_boj_series(db, keyword, lang="jp"):
    """
    指定したDB内の系列を、系列名(日本語)にキーワードが含まれるかで検索する。

    使用例:
      results = search_boj_series("CO", "設備投資")
      for r in results:
          print(r["SERIES_CODE"], r["NAME_OF_TIME_SERIES_J"])

      results = search_boj_series("FM08", "ドル")
      for r in results:
          print(r["SERIES_CODE"], r["NAME_OF_TIME_SERIES_J"])
    """
    records = fetch_boj_metadata(db, lang)
    return [r for r in records if keyword in (r.get("NAME_OF_TIME_SERIES_J") or "")]


# ============================================================
# ④ e-Stat(景気ウォッチャー調査・機械受注・鉱工業生産・CPI等)
# ============================================================
def fetch_estat_stats(stats_data_id, app_id=None):
    """
    e-Stat(政府統計の総合窓口)APIから統計表データを取得する。
    出典: 政府統計の総合窓口(e-Stat)
    利用条件: 商用利用可。出典表示必須。
    """
    app_id = app_id or ESTAT_APP_ID
    url = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData"
    params = {"appId": app_id, "statsDataId": stats_data_id}
    raw = _http_get(url, params)
    return json.loads(raw)


def search_estat_tables(keyword, app_id=None, limit=10):
    """
    e-Statの「統計表情報取得」APIを使い、キーワードで統計表を検索する。

    使用例:
      results = search_estat_tables("景気ウォッチャー調査")
      for r in results:
          print(r["id"], r["title"])
    """
    app_id = app_id or ESTAT_APP_ID
    url = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsList"
    params = {"appId": app_id, "searchWord": keyword, "limit": limit}
    raw = _http_get(url, params)
    data = json.loads(raw)

    results = []
    try:
        table_list = data["GET_STATS_LIST"]["DATALIST_INF"]["TABLE_INF"]
        if isinstance(table_list, dict):
            table_list = [table_list]
        for t in table_list:
            title = t.get("TITLE")
            if isinstance(title, dict):
                title = title.get("$")
            results.append(
                {
                    "id": t.get("@id"),
                    "title": title,
                    "survey_date": t.get("SURVEY_DATE"),
                }
            )
    except (KeyError, TypeError):
        return data
    return results


# ============================================================
# ⑤ WTI原油価格(米EIA)
# ============================================================
def fetch_wti_price(api_key=None, series_id="RWTC"):
    """
    米エネルギー情報局(EIA)からWTI原油スポット価格(日次)を取得する。
    出典: U.S. Energy Information Administration (EIA)
    利用条件: 米国政府著作物のためパブリックドメイン、自由利用可
    """
    api_key = api_key or EIA_API_KEY
    url = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
    params = {
        "api_key": api_key,
        "frequency": "daily",
        "data[0]": "value",
        "facets[series][]": series_id,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 30,
    }
    raw = _http_get(url, params)
    return json.loads(raw)


# ============================================================
# ⑥ JPX 投資部門別売買状況(株式・週間)
# ============================================================
#
# JPXのページはJavaScriptで描画される旨の注記がありますが、
# 実際にはダウンロードリンクを含む表がHTML内に存在しており、
# 単純なHTTP取得でも内容を確認できます(2026年9月時点で確認済み)。
#
# 【重要】ファイル形式が変わります
#   2026年9月29日公表分より、ファイル名・形式が変更されます:
#     旧: stock_vol_1_YYMMWW.xls (株数) / stock_val_1_YYMMWW.xls (金額)
#          ※旧形式は.xls(古いExcel形式)で、標準ライブラリだけでは読めません
#          (xlrdライブラリが別途必要。ただしxlrdは純粋なPython実装なので、
#           pandasで起きたようなセキュリティブロックは起きにくいはずです)
#     新: stock_1_w_YYYYMMDD_YYYYMMDD.xlsx (株数・金額が1ファイルに統合)
#          ※新形式は.xlsxで、標準ライブラリ(zipfile + xml)だけで読めます
#
#   このスクリプトの parse_xlsx_generic() は新形式(.xlsx)を想定しています。
#   2026年9月29日以降、実際のファイルを取得して中身を確認してから、
#   投資部門別のシグナル判定ロジックに接続してください。

JPX_WEEKLY_INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/investor-type/index.html"


def fetch_jpx_weekly_links():
    """
    JPX「投資部門別売買状況(株式・週間)」のページから、
    ダウンロードリンクをHTMLから正規表現で抽出する。

    戻り値: 見つかった順(通常は新しい週が先頭)の URL のリスト
            (.xlsx / .xls の両方の形式に対応)
    出典: 日本取引所グループ(JPX)
    """
    raw = _http_get(JPX_WEEKLY_INDEX_URL)
    html = raw.decode("utf-8", errors="replace")

    # href="...stock_vol_1_XXXXXX.xls" / stock_val_1_... / stock_1_w_....xlsx
    # など、ファイル名にstock_が含まれ、拡張子がxlsまたはxlsxのリンクを抽出
    import re

    pattern = r'href="([^"]+?stock[^"]*?\.xlsx?)"'
    matches = re.findall(pattern, html)

    # 相対URLの場合はドメインを補完
    full_urls = []
    for m in matches:
        if m.startswith("http"):
            full_urls.append(m)
        else:
            full_urls.append("https://www.jpx.co.jp" + m)
    return full_urls


def download_file(url, save_path):
    """指定URLのファイルをそのまま保存する(Excel/PDF問わず)。"""
    raw = _http_get(url)
    with open(save_path, "wb") as f:
        f.write(raw)
    return save_path


def parse_xlsx_generic(path, sheet_index=1):
    """
    .xlsxファイルを、追加ライブラリなし(zipfile + xml標準ライブラリのみ)で読み込み、
    指定シート(デフォルトは1枚目)の内容をセルの2次元リストとして返す。

    注意: .xls(拡張子が.xls、旧形式)には対応していません。
          xlsxはZIP形式のため、まずファイルの中身を確認してください。

    戻り値: [[セル, セル, ...], [セル, セル, ...], ...] (行ごとのリスト)
    """
    import zipfile

    with zipfile.ZipFile(path) as z:
        # 共有文字列(テキストセルの実体)を読み込む
        shared_strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            with z.open("xl/sharedStrings.xml") as f:
                tree = ET.parse(f)
                ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                for si in tree.getroot().findall("a:si", ns):
                    texts = si.findall(".//a:t", ns)
                    shared_strings.append("".join(t.text or "" for t in texts))

        sheet_path = f"xl/worksheets/sheet{sheet_index}.xml"
        if sheet_path not in z.namelist():
            raise ValueError(
                f"{sheet_path} が見つかりません。"
                f"存在するファイル一覧: {[n for n in z.namelist() if 'sheet' in n]}"
            )

        with z.open(sheet_path) as f:
            tree = ET.parse(f)
            ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            rows = []
            for row_elem in tree.getroot().findall(".//a:sheetData/a:row", ns):
                row_values = []
                for c in row_elem.findall("a:c", ns):
                    cell_type = c.get("t")
                    v_elem = c.find("a:v", ns)
                    if v_elem is None:
                        row_values.append(None)
                        continue
                    v = v_elem.text
                    if cell_type == "s":  # shared string
                        row_values.append(shared_strings[int(v)])
                    else:
                        try:
                            row_values.append(float(v))
                        except (ValueError, TypeError):
                            row_values.append(v)
                rows.append(row_values)
    return rows


# ============================================================
# ⑨ 東証上場銘柄一覧(全銘柄名の辞書として使う)
# ============================================================
#
# 「ニュースで話題の銘柄」の対象を、手作業で選んだ数十社だけでなく
# 東証上場銘柄(約4,000社)全体に広げるために使う。
# JPXが毎月末時点のデータをExcelで無料公開している。
# ファイルのURLは月ごとにランダムな符号を含むため、まずページを解析して
# 現在のリンクを見つける。

JPX_LISTED_INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"


def fetch_jpx_listed_companies_link():
    """東証上場銘柄一覧(data_j.xlsx)の現在のダウンロードURLを取得する。"""
    raw = _http_get(JPX_LISTED_INDEX_URL)
    html = raw.decode("utf-8", errors="replace")
    match = re.search(r'href="([^"]+?data_j\.xlsx)"', html)
    if not match:
        raise ValueError("data_j.xlsxへのリンクが見つかりませんでした。ページ構成が変わった可能性があります。")
    url = match.group(1)
    if not url.startswith("http"):
        url = "https://www.jpx.co.jp" + url
    return url


def fetch_jpx_listed_companies():
    """
    東証上場銘柄一覧を取得し、行データ(2次元リスト)として返す。
    1行目がヘッダー(列名)になっている想定。
    """
    url = fetch_jpx_listed_companies_link()
    tmp_path = "data_j.xlsx"
    download_file(url, tmp_path)
    rows = parse_xlsx_generic(tmp_path)
    return rows


def save_company_master(rows, db_path=DB_PATH):
    """
    fetch_jpx_listed_companies() の結果をSupabaseに保存する。
    ヘッダー行から「コード」「銘柄名」「33業種区分」の列を自動で探して使う。
    """
    if not rows:
        return 0
    header = rows[0]
    try:
        code_idx = [i for i, h in enumerate(header) if h and "コード" in str(h)][0]
        name_idx = [i for i, h in enumerate(header) if h and "銘柄名" in str(h)][0]
    except IndexError:
        raise ValueError(f"コード・銘柄名の列が見つかりませんでした。ヘッダー: {header}")

    industry_idx = None
    candidates = [i for i, h in enumerate(header) if h and "33業種区分" in str(h)]
    if candidates:
        industry_idx = candidates[0]

    fetched_at = datetime.now().isoformat()
    out_rows = []
    for r in rows[1:]:
        if len(r) <= max(code_idx, name_idx):
            continue
        code, name = r[code_idx], r[name_idx]
        if not code or not name:
            continue
        industry = r[industry_idx] if industry_idx is not None and len(r) > industry_idx else None
        out_rows.append(
            {"code": str(code), "name": str(name), "industry": industry, "fetched_at": fetched_at}
        )

    # 大量データのため1000件ずつ分けて送信する
    total = 0
    for i in range(0, len(out_rows), 1000):
        chunk = out_rows[i : i + 1000]
        total += sb.upsert("company_master", chunk, on_conflict="code")
    return total


# ============================================================
# ⑦ ニュース(NHK NEWS WEB RSS)
# ============================================================
#
# NHKが提供する公式RSSフィードから、ニュースの見出し・概要を取得する。
# 個人利用(自分の判断材料として読む)を前提としています。
#
# カテゴリ番号は昔からの慣例で 0=主要, 5=経済 とされていますが、
# 2026年9月時点でNHK側の番号割り当てが変わっている可能性があります。
# cat5 が経済ニュースでない場合は、他の番号も試してみてください。

NHK_RSS_BASE = "https://news.web.nhk/n-data/conf/na/rss/"


def fetch_nhk_news(category="cat5"):
    """
    NHK NEWS WEBのRSSフィードからニュース一覧を取得する。

    引数:
      category: "cat0"(主要) や "cat5"(経済、想定)などカテゴリ番号
    戻り値: [{"title":..., "description":..., "link":..., "pub_date":...}, ...]
    出典: NHK(日本放送協会)
    """
    url = NHK_RSS_BASE + f"{category}.xml"
    raw = _http_get(url)
    root = ET.fromstring(raw)
    items = []
    for item in root.findall(".//item"):
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "description": (item.findtext("description") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "pub_date": (item.findtext("pubDate") or "").strip(),
            }
        )
    return items


def save_news(items, category, db_path=DB_PATH):
    """fetch_nhk_news() の結果をSupabaseに保存する(linkをキーに重複排除)。"""
    fetched_at = datetime.now().isoformat()
    out_rows = []
    for it in items:
        if not it.get("link"):
            continue
        out_rows.append(
            {
                "link": it["link"],
                "title": it["title"],
                "description": it["description"],
                "pub_date": it["pub_date"],
                "category": category,
                "source": "NHK",
                "fetched_at": fetched_at,
            }
        )
    return sb.upsert("raw_news", out_rows, on_conflict="link")


# ============================================================
# ⑧ 株価データ(Stooq)
# ============================================================
#
# Stooqは無料で株価データを提供するサイトですが、2026年に入り
# ボット対策が強化されたようで、簡素なUser-Agentでのアクセスは
# ブロックされることがあります。ブラウザに近いヘッダーを付けて
# アクセスすることで、通常のブラウザと同じように振る舞う。
#
# 日本株は「証券コード.jp」形式(例: トヨタ=7203.jp)、
# 米国株は「ティッカー.us」形式(例: SPY=spy.us)で指定する。

STOOQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://stooq.com/",
}


def fetch_stooq_price(symbol):
    """
    Stooqから指定銘柄の日次価格データ(CSV)を取得する。

    引数:
      symbol: 例 "7203.jp"(トヨタ)、"spy.us"(米国ETF)
    戻り値: [{"date":..., "open":..., "high":..., "low":..., "close":..., "volume":...}, ...]
            (古い順)
    出典: Stooq(個人利用を前提)
    """
    url = "https://stooq.com/q/d/l/"
    params = {"s": symbol, "i": "d"}
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, headers=STOOQ_HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        raw = res.read()

    text = raw.decode("utf-8", errors="replace")
    if text.strip().startswith("<") or "Exceeded" in text or "denied" in text.lower():
        raise ValueError(f"Stooqから正しいデータが返りませんでした(先頭200文字): {text[:200]}")

    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        rows.append(
            {
                "date": r.get("Date"),
                "open": r.get("Open"),
                "high": r.get("High"),
                "low": r.get("Low"),
                "close": r.get("Close"),
                "volume": r.get("Volume"),
            }
        )
    return rows


def save_stock_prices(symbol, rows, db_path=DB_PATH):
    """fetch_stooq_price() の結果をSupabaseに保存する。"""
    fetched_at = datetime.now().isoformat()
    out_rows = []
    for r in rows:
        try:
            close = float(r["close"])
        except (ValueError, TypeError):
            continue
        out_rows.append(
            {"symbol": symbol, "date": r["date"], "close": close, "source": "Stooq", "fetched_at": fetched_at}
        )
    return sb.upsert("raw_stock_prices", out_rows, on_conflict="symbol,date")


# ============================================================
# Supabase保存(テーブルは supabase_schema.sql で事前作成済み)
# ============================================================
def init_db(db_path=DB_PATH):
    """
    互換性のために残している空の関数。
    テーブル作成はSupabaseのSQL Editorで supabase_schema.sql を
    実行して行うため、ここでは何もしない。
    """
    pass



def save_jgb_yields(rows, db_path=DB_PATH):
    """fetch_jgb_yields() の結果(辞書のリスト)をSupabaseに保存する。"""
    fetched_at = datetime.now().isoformat()
    out_rows = []
    for row in rows:
        date_col = list(row.keys())[0]
        date_val = row[date_col]
        for tenor, val in row.items():
            if tenor == date_col:
                continue
            if val in (None, "", "-"):
                continue
            try:
                val_f = float(val)
            except ValueError:
                continue
            out_rows.append(
                {"date": date_val, "tenor": tenor, "yield": val_f, "source": "MOF", "fetched_at": fetched_at}
            )
    return sb.upsert("raw_jgb_yields", out_rows, on_conflict="date,tenor")


def save_ust_yields(rows, db_path=DB_PATH):
    """fetch_us_treasury_yields() の結果をSupabaseに保存する。"""
    fetched_at = datetime.now().isoformat()
    out_rows = []
    for row in rows:
        date_val = row.get("NEW_DATE")
        for key, val in row.items():
            if not key.startswith("BC_"):
                continue
            if val in (None, ""):
                continue
            try:
                val_f = float(val)
            except ValueError:
                continue
            out_rows.append(
                {"date": date_val, "tenor": key, "yield": val_f, "source": "US Treasury", "fetched_at": fetched_at}
            )
    return sb.upsert("raw_ust_yields", out_rows, on_conflict="date,tenor")


def save_wti_price(eia_response, db_path=DB_PATH):
    fetched_at = datetime.now().isoformat()
    try:
        records = eia_response["response"]["data"]
    except (KeyError, TypeError):
        raise ValueError("想定と異なるレスポンス構造です。EIA APIの仕様をご確認ください。")
    out_rows = []
    for r in records:
        date_val = r.get("period")
        val = r.get("value")
        if date_val is None or val is None:
            continue
        out_rows.append(
            {"date": date_val, "commodity": "WTI", "price": float(val), "source": "EIA", "fetched_at": fetched_at}
        )
    return sb.upsert("raw_commodities", out_rows, on_conflict="date,commodity")


def save_estat_series(stats_data_id, estat_response, db_path=DB_PATH):
    fetched_at = datetime.now().isoformat()
    try:
        values = estat_response["GET_STATS_DATA"]["STATISTICAL_DATA"]["DATA_INF"]["VALUE"]
        if isinstance(values, dict):
            values = [values]
    except (KeyError, TypeError):
        raise ValueError("想定と異なるレスポンス構造です。e-Stat APIの仕様をご確認ください。")

    out_rows = []
    for v in values:
        time_val = v.get("@time")
        val = v.get("$")
        if time_val is None or val is None:
            continue
        cat_parts = [f"{k}={v[k]}" for k in v if k.startswith("@cat")]
        category_key = ",".join(cat_parts) if cat_parts else "default"
        try:
            val_f = float(val)
        except (ValueError, TypeError):
            continue
        out_rows.append(
            {
                "time": time_val,
                "stats_data_id": stats_data_id,
                "category_key": category_key,
                "value": val_f,
                "source": "e-Stat",
                "fetched_at": fetched_at,
            }
        )
    return sb.upsert("raw_macro_indicators", out_rows, on_conflict="time,stats_data_id,category_key")


def save_boj_series(db_name, boj_response, db_path=DB_PATH):
    """
    fetch_boj_series() の結果をSupabaseに保存する。
    構造: response["RESULTSET"] = [
      {"SERIES_CODE": "...", "NAME_OF_TIME_SERIES_J": "...",
       "VALUES": {"SURVEY_DATES": [20260101, ...], "VALUES": [156.2, ...]}},
      ...
    ]
    (2026年9月時点の実データで構造確認済み)
    """
    fetched_at = datetime.now().isoformat()
    out_rows = []
    for series in boj_response.get("RESULTSET", []):
        code = series.get("SERIES_CODE")
        name = series.get("NAME_OF_TIME_SERIES_J")
        values_block = series.get("VALUES", {})
        dates = values_block.get("SURVEY_DATES", [])
        vals = values_block.get("VALUES", [])
        for d, v in zip(dates, vals):
            if v is None:
                continue
            out_rows.append(
                {
                    "db": db_name,
                    "series_code": code,
                    "series_name": name,
                    "date": str(d),
                    "value": float(v),
                    "source": "BOJ",
                    "fetched_at": fetched_at,
                }
            )
    return sb.upsert("raw_boj_series", out_rows, on_conflict="db,series_code,date")


# ============================================================
# 動作確認用
# ============================================================
if __name__ == "__main__":
    init_db()

    print("=== 日本国債金利(財務省) ===")
    try:
        rows = fetch_jgb_yields()
        print(f"{len(rows)}件取得。直近3件:")
        for r in rows[-3:]:
            print(r)
        n = save_jgb_yields(rows)
        print(f"-> Supabaseに {n} 件保存しました")
    except Exception as e:
        print("取得エラー:", e)

    print("\n=== 米国債金利(米財務省) ===")
    try:
        rows = fetch_us_treasury_yields()
        print(f"{len(rows)}件取得。直近3件:")
        for r in rows[-3:]:
            print(r)
        n = save_ust_yields(rows)
        print(f"-> Supabaseに {n} 件保存しました")
    except Exception as e:
        print("取得エラー:", e)

    print("\n=== WTI原油価格(EIA、APIキー未設定なら失敗します) ===")
    try:
        data = fetch_wti_price()
        n = save_wti_price(data)
        print(f"-> Supabaseに {n} 件保存しました")
    except Exception as e:
        print("取得エラー(APIキーを設定してください):", e)

    print("\n=== e-Stat 統計表の検索・保存 ===")
    try:
        # 景気ウォッチャー調査のDI実値が入っている表を明示的に指定
        # (検索結果の1番目は「調査客体の構成」など、目的と違う表のことがあるため)
        target_stats_data_id = "0003348423"  # 季節調整値 全国の分野・業種別DIの推移

        results = search_estat_tables("景気ウォッチャー調査")
        print("検索結果(参考):")
        for r in results[:5]:
            mark = " <- 今回取得するのはこれ" if r["id"] == target_stats_data_id else ""
            print(f"  {r['id']} - {r['title']}{mark}")

        # 前回誤って保存した表のデータを削除しておく(初回のみ意味がある処理)
        try:
            sb.delete("raw_macro_indicators", {"stats_data_id": "neq." + target_stats_data_id})
        except Exception:
            pass  # 該当データが無い場合はそのまま無視

        data = fetch_estat_stats(target_stats_data_id)
        n = save_estat_series(target_stats_data_id, data)
        print(f"-> Supabaseに {n} 件保存しました(統計表ID: {target_stats_data_id})")
    except Exception as e:
        print("取得エラー:", e)

    print("\n=== Stooq: トヨタ(7203.jp)の株価取得テスト ===")
    try:
        rows = fetch_stooq_price("7203.jp")
        print(f"{len(rows)}件取得。直近3件:")
        for r in rows[-3:]:
            print(" ", r)
        n = save_stock_prices("7203.jp", rows)
        print(f"-> Supabaseに {n} 件保存しました")
    except Exception as e:
        print("取得エラー:", e)

    print("\n=== NHKニュース: 経済カテゴリ(cat5)を取得 ===")
    try:
        news_items = fetch_nhk_news("cat5")
        n = save_news(news_items, "cat5")
        print(f"{len(news_items)}件取得、Supabaseに {n} 件保存しました。")
        print("見出し(最初の5件):")
        for it in news_items[:5]:
            print(" -", it["title"])
    except Exception as e:
        print("取得エラー(cat5が経済カテゴリでない可能性があります):", e)
        print("代わりに主要ニュース(cat0)を試します...")
        try:
            news_items = fetch_nhk_news("cat0")
            n = save_news(news_items, "cat0")
            print(f"{len(news_items)}件取得、Supabaseに {n} 件保存しました。")
        except Exception as e2:
            print("取得エラー:", e2)

    print("\n=== JPX: 東証上場銘柄一覧を取得・保存 ===")
    try:
        rows = fetch_jpx_listed_companies()
        print(f"{len(rows)-1}社ぶんのデータを取得しました。ヘッダー: {rows[0]}")
        n = save_company_master(rows)
        print(f"-> Supabaseに {n} 件保存しました")
    except Exception as e:
        print("取得エラー:", e)

    print("\n=== JPX: 投資部門別売買状況(株式・週間)のリンク取得テスト ===")
    try:
        links = fetch_jpx_weekly_links()
        print(f"{len(links)}件のリンクを検出しました。最初の4件:")
        for l in links[:4]:
            print(" ", l)
        if links and links[0].endswith(".xlsx"):
            print("-> .xlsx形式です。parse_xlsx_generic() で中身を確認できます。")
        elif links:
            print("-> まだ.xls(旧形式)です。9月29日の切替後に新形式で試してください。")
    except Exception as e:
        print("取得エラー:", e)

    print("\n=== 日銀短観: 業況判断DI(実績)を取得・保存 ===")
    # search_boj_series("CO", "業況") で大企業/中小企業 × 製造業/非製造業を
    # 絞り込んで見つけたコード(2026年9月時点で確認済み)
    tankan_codes = {
        "TK99F1000601GCQ01000": "大企業製造業",
        "TK99F2000601GCQ01000": "大企業非製造業",
        "TK99F1000601GCQ03000": "中小企業製造業",
        "TK99F2000601GCQ03000": "中小企業非製造業",
    }
    try:
        # 終了期は実行時点の四半期を使う(将来にわたって固定日付にならないように)
        # 短観のAPIは四半期形式(YYYYQQ、01=1Q...04=4Q)を使う
        now = datetime.now()
        current_quarter = (now.month - 1) // 3 + 1
        end_quarter = f"{now.year}{current_quarter:02d}"
        result = fetch_boj_series(
            "CO", list(tankan_codes.keys()), "202001", end_quarter
        )
        n = save_boj_series("CO", result, DB_PATH)
        print(f"-> Supabaseに {n} 件保存しました")
        for series in result.get("RESULTSET", []):
            code = series.get("SERIES_CODE")
            label = tankan_codes.get(code, code)
            dates = series["VALUES"]["SURVEY_DATES"]
            vals = series["VALUES"]["VALUES"]
            recent = [(d, v) for d, v in zip(dates, vals) if v is not None][-2:]
            print(f"  {label}: 直近2件 {recent}")
    except Exception as e:
        print("取得エラー:", e)

    try:
        print("\n--- ドル円(FXERD04)を取得・保存 ---")
        # 終了日は実行時点の年月を使う(将来にわたって固定日付にならないように)
        end_month = datetime.now().strftime("%Y%m")
        result = fetch_boj_series("FM08", ["FXERD04"], "202601", end_month)
        n = save_boj_series("FM08", result, DB_PATH)
        print(f"-> Supabaseに {n} 件保存しました")
        # 直近の値を確認表示
        series = result["RESULTSET"][0]
        dates = series["VALUES"]["SURVEY_DATES"]
        vals = series["VALUES"]["VALUES"]
        recent = [(d, v) for d, v in zip(dates, vals) if v is not None][-3:]
        print("直近の値:", recent)
    except Exception as e:
        print("取得エラー:", e)
