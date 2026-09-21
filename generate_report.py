"""
投資判断補助アプリ - HTMLレポート生成スクリプト(個人利用版)

market_data.db(fetch_data.py で作成)の内容を読み込み、
ルールベースで簡単な判定を行い、1枚のHTMLファイルとして出力する。

AIは使わず、あらかじめ用意した定型文パターンで文章を組み立てる。
(AI総合判断を入れたい場合は、次のステップでAnthropic APIを組み込みます)

実行方法:
  python generate_report.py

実行すると market_report.html が作成され、自動的にブラウザで開く。
"""

import os
from datetime import datetime

import supabase_client as sb

DB_PATH = "market_data.db"  # 互換性のために残しているが、Supabase移行後は未使用

# GitHub Actions上で実行し、リポジトリ直下に index.html として出力する。
# Vercelがこのリポジトリを見ていれば、pushされると自動でデプロイされる。
OUTPUT_HTML = "index.html"


# ============================================================
# データ取得・判定ロジック
# ============================================================
def _parse_reiwa_date(s):
    """
    財務省CSVの日付表記 'R8.9.16'(令和8年9月16日)を
    ソート可能な (year, month, day) タプルに変換する。
    """
    try:
        era_part, rest = s.split(".", 1)
        reiwa_year = int(era_part.replace("R", ""))
        month, day = rest.split(".")
        year = 2018 + reiwa_year  # 令和1年 = 2019年
        return (year, int(month), int(day))
    except Exception:
        return (0, 0, 0)


def get_jgb_state():
    """日本国債の2年・10年金利から長短金利差とイールドカーブ状態を判定する。"""
    rows = sb.select(
        "raw_jgb_yields",
        {"select": "date,tenor,yield", "tenor": "in.(2年,10年)"},
    )
    if not rows:
        return None

    by_date = {}
    for r in rows:
        date_s, tenor, yield_v = r["date"], r["tenor"], r["yield"]
        key = _parse_reiwa_date(date_s)
        by_date.setdefault(key, {})[tenor] = yield_v
        by_date[key]["_label"] = date_s

    dates_sorted = sorted(by_date.keys())
    complete_dates = [d for d in dates_sorted if "2年" in by_date[d] and "10年" in by_date[d]]
    if len(complete_dates) < 1:
        return None

    latest = complete_dates[-1]
    latest_spread = by_date[latest]["10年"] - by_date[latest]["2年"]

    state = "スティープニング" if latest_spread > 0 else "フラット/逆イールド"
    trend = None
    if len(complete_dates) >= 2:
        prev = complete_dates[-2]
        prev_spread = by_date[prev]["10年"] - by_date[prev]["2年"]
        diff = latest_spread - prev_spread
        if diff > 0.01:
            trend = "拡大(スティープ化)"
        elif diff < -0.01:
            trend = "縮小(フラット化)"
        else:
            trend = "横ばい"

    return {
        "date": by_date[latest]["_label"],
        "yield_2y": by_date[latest]["2年"],
        "yield_10y": by_date[latest]["10年"],
        "spread": round(latest_spread, 3),
        "state": state,
        "trend": trend,
    }


def get_ust_trend():
    """米国債10年金利の直近の動き(上昇/低下/横ばい)を判定する。"""
    rows = sb.select(
        "raw_ust_yields",
        {"select": "date,yield", "tenor": "eq.BC_10YEAR", "order": "date.asc"},
    )
    if len(rows) < 2:
        return None
    latest_date, latest_val = rows[-1]["date"], rows[-1]["yield"]
    prev_val = rows[-2]["yield"]
    diff = latest_val - prev_val
    if diff > 0.05:
        trend = "上昇"
    elif diff < -0.05:
        trend = "低下"
    else:
        trend = "横ばい"
    return {"date": latest_date[:10], "value": latest_val, "diff": round(diff, 3), "trend": trend}


def get_wti_trend():
    """WTI原油価格の直近の動きを判定する。"""
    rows = sb.select(
        "raw_commodities",
        {"select": "date,price", "commodity": "eq.WTI", "order": "date.asc"},
    )
    if len(rows) < 2:
        return None
    latest_date, latest_val = rows[-1]["date"], rows[-1]["price"]
    prev_val = rows[-2]["price"]
    diff = latest_val - prev_val
    pct = (diff / prev_val * 100) if prev_val else 0
    trend = "上昇" if diff > 0 else ("下落" if diff < 0 else "横ばい")
    return {"date": latest_date, "value": latest_val, "diff": round(diff, 2), "pct": round(pct, 1), "trend": trend}


def get_usdjpy_trend():
    """ドル円の直近1週間の動きを判定する。"""
    rows = sb.select(
        "raw_boj_series",
        {"select": "date,value", "series_code": "eq.FXERD04", "order": "date.asc"},
    )
    if len(rows) < 6:
        return None
    latest_date, latest_val = rows[-1]["date"], rows[-1]["value"]
    week_ago_val = rows[-6]["value"]  # 営業日ベースでおよそ1週間前
    diff = latest_val - week_ago_val
    trend = "円安" if diff > 0.3 else ("円高" if diff < -0.3 else "横ばい")
    return {
        "date": latest_date,
        "value": latest_val,
        "diff": round(diff, 2),
        "trend": trend,
    }


TANKAN_LABELS = {
    "TK99F1000601GCQ01000": "大企業製造業",
    "TK99F2000601GCQ01000": "大企業非製造業",
    "TK99F1000601GCQ03000": "中小企業製造業",
    "TK99F2000601GCQ03000": "中小企業非製造業",
}


# ============================================================
# ニュースからのセクター強弱・注目銘柄判定(ルールベース)
# ============================================================
#
# 簡易的なキーワードマッチによる判定です。自然言語処理のような
# 高度な解析はしていないため、あくまで「参考程度」の位置づけです。
#
# セクターの一般的な話題を示すキーワード(SECTOR_KEYWORDS)と、
# そのセクターに属する主要企業名(SECTOR_COMPANIES)を分けて管理する。
# これにより「このセクターの、どの企業のニュースが良かったのか」を
# 具体的に示せるようにしている。
# ※「金利」のような、複数セクターに関係しうる曖昧な語は
#   誤判定(例: 金利ニュースを銀行株の話と混同)を避けるため含めていません。

SECTOR_KEYWORDS = {
    "半導体": ["半導体"],
    "自動車": ["自動車", "EV"],
    "銀行": ["銀行"],
    "商社": ["商社"],
    "小売": ["小売", "百貨店"],
    "通信": ["通信", "携帯"],
    "エネルギー": ["原油", "エネルギー", "電力"],
    "医薬品": ["医薬品", "製薬"],
    "不動産": ["不動産", "マンション", "住宅", "地価"],
    "防衛": ["防衛"],
    "機械・FA": ["工作機械", "ロボット", "設備投資", "FA"],
    "インバウンド": ["インバウンド", "訪日", "観光"],
}

# セクターごとの主要企業(ニュースに登場したら、その企業名も表示する)
SECTOR_COMPANIES = {
    "半導体": ["東京エレクトロン", "アドバンテスト", "レーザーテック", "ディスコ", "ソシオネクスト", "ルネサス"],
    "自動車": ["トヨタ", "ホンダ", "日産", "スズキ", "SUBARU", "マツダ", "デンソー"],
    "銀行": ["三菱UFJ", "三井住友", "みずほ", "りそな"],
    "商社": ["三菱商事", "三井物産", "伊藤忠", "丸紅", "住友商事"],
    "小売": ["セブン&アイ", "イオン", "ファーストリテイリング", "ニトリ"],
    "通信": ["NTT", "KDDI", "ソフトバンク"],
    "エネルギー": ["ENEOS", "出光", "東京電力", "関西電力", "中部電力"],
    "医薬品": ["武田薬品", "第一三共", "アステラス", "中外製薬"],
    "不動産": ["三井不動産", "三菱地所", "住友不動産", "東急不動産"],
    "防衛": ["三菱重工", "IHI", "川崎重工"],
    "機械・FA": ["ファナック", "安川電機", "SMC", "キーエンス", "不二越"],
    "インバウンド": ["ANA", "JAL", "オリエンタルランド", "近鉄"],
}

POSITIVE_WORDS = ["上昇", "高値", "急伸", "増益", "好調", "最高益", "反発", "堅調", "改善", "増収", "買い"]
NEGATIVE_WORDS = ["下落", "安値", "急落", "減益", "低迷", "軟調", "冴えない", "悪化", "減収", "赤字", "売り"]

# セクターの一般語が、無関係な話題(中央銀行の金融政策など)を
# 誤って拾ってしまうのを防ぐための除外ワード
SECTOR_EXCLUDE_KEYWORDS = {
    "銀行": ["日銀", "日本銀行", "中央銀行"],
}


def analyze_sector_sentiment(news_items):
    """
    ニュース見出し・概要から、セクターごとの強弱をキーワードベースで判定する。
    セクターの一般語(SECTOR_KEYWORDS)に加え、そのセクターの主要企業名
    (SECTOR_COMPANIES)がニュースに出た場合も、そのセクターの話題として扱う。

    戻り値: [{"sector":..., "direction": "↑"/"↓"/"→", "positive":n, "negative":n,
              "headlines": [...], "notable_companies": [...] }, ...]
              notable_companies は、好材料っぽい見出しに登場した企業名のリスト
    """
    results = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        companies = SECTOR_COMPANIES.get(sector, [])
        exclude_words = SECTOR_EXCLUDE_KEYWORDS.get(sector, [])
        matched_headlines = []
        pos_count = 0
        neg_count = 0
        notable_companies = set()

        for it in news_items:
            text = it["title"] + " " + it["description"]
            matched_company = [c for c in companies if c in text]
            matched_kw = any(kw in text for kw in keywords)

            # 除外ワードが含まれ、かつ個別企業名も含まれていない場合はスキップ
            # (例: 「日銀」のニュースを「銀行」セクターの話題として拾わない)
            if matched_kw and not matched_company and any(ex in text for ex in exclude_words):
                matched_kw = False

            if not matched_kw and not matched_company:
                continue

            matched_headlines.append(it["title"])
            is_positive = any(pw in text for pw in POSITIVE_WORDS)
            is_negative = any(nw in text for nw in NEGATIVE_WORDS)
            if is_positive:
                pos_count += 1
                for c in matched_company:
                    notable_companies.add(c)
            if is_negative:
                neg_count += 1

        if not matched_headlines:
            continue
        if pos_count > neg_count:
            direction = "↑"
        elif neg_count > pos_count:
            direction = "↓"
        else:
            direction = "→"
        results.append(
            {
                "sector": sector,
                "direction": direction,
                "positive": pos_count,
                "negative": neg_count,
                "headlines": matched_headlines[:3],
                "notable_companies": sorted(notable_companies),
            }
        )
    return results


def extract_notable_stocks(news_items):
    """
    ニュース見出しに登場する企業名を抽出する。
    company_master(東証上場銘柄一覧、Supabaseに保存済み)があればそちらを
    優先して使い(全銘柄対応)、無ければSECTOR_COMPANIESのリストで代替する。
    """
    try:
        master_rows = sb.select_all("company_master", {"select": "name"})
        all_companies = sorted({r["name"] for r in master_rows if r.get("name")})
    except Exception:
        all_companies = []

    if not all_companies:
        all_companies = sorted({c for companies in SECTOR_COMPANIES.values() for c in companies})

    results = {}
    for it in news_items:
        text = it["title"] + " " + it["description"]
        for company in all_companies:
            if len(company) < 2:
                continue
            if company in text:
                results.setdefault(company, []).append(it["title"])
    return [{"company": c, "headlines": h[:2]} for c, h in results.items()]


# ============================================================
# テーマ株エンジン(テーマ別の話題の熱量ランキング)
# ============================================================
#
# 個別セクターの強弱とは別に、「今日はどのテーマが話題になっているか」を
# ニュースの見出し件数ベースでランキングする。株価判定ではなく、
# あくまで「話題の量」の指標。

THEME_KEYWORDS = {
    "AI": ["AI", "人工知能", "生成AI"],
    "防衛": ["防衛", "自衛隊", "安全保障"],
    "半導体": ["半導体"],
    "原子力": ["原発", "原子力", "核燃料"],
    "宇宙": ["宇宙", "ロケット", "衛星"],
    "量子": ["量子"],
    "データセンター": ["データセンター", "クラウド"],
    "EV": ["EV", "電気自動車", "バッテリー"],
    "バイオ": ["バイオ", "創薬", "再生医療"],
    "インバウンド": ["インバウンド", "訪日", "観光"],
}


def analyze_theme_heat(news_items):
    """
    ニュース見出し・概要から、テーマごとの話題件数(熱量)を集計する。
    戻り値: [{"theme":..., "count": n, "headlines": [...]}, ...] (件数の多い順)
    """
    results = []
    for theme, keywords in THEME_KEYWORDS.items():
        matched = []
        for it in news_items:
            text = it["title"] + " " + it["description"]
            if any(kw in text for kw in keywords):
                matched.append(it["title"])
        if matched:
            results.append({"theme": theme, "count": len(matched), "headlines": matched[:3]})
    results.sort(key=lambda x: -x["count"])
    return results


# ============================================================
# ドル円感応度マップ・金利ローテーション判定(ルールベース)
# ============================================================
#
# 為替(ドル円)と米金利の「方向」だけから、業種ごとの追い風/逆風を
# 単純なプラスマイナスの点数で表現する。ニュースを使わない、
# 純粋にマクロ指標だけからの機械的な判定。

FX_SENSITIVITY = {
    "円安": {
        "自動車": 1, "機械・FA": 1, "商社": 1, "半導体": 1, "インバウンド": 1,
        "電力": -1, "エネルギー": -1, "小売": -1,
    },
    "円高": {
        "自動車": -1, "機械・FA": -1, "商社": -1, "半導体": -1, "インバウンド": -1,
        "電力": 1, "エネルギー": 1, "小売": 1,
    },
}

US_RATE_SENSITIVITY = {
    "上昇": {"銀行": 1, "半導体": -1, "不動産": -1},
    "低下": {"銀行": -1, "半導体": 1, "不動産": 1},
}


def analyze_macro_sector_scores(fx, ust):
    """
    ドル円の動き・米長期金利の動きから、業種ごとのマクロ加点を計算する。
    戻り値: [{"sector":..., "score": n, "reasons": [...]}, ...] (点数の高い順)
    """
    scores = {}
    reasons = {}

    if fx and fx["trend"] in FX_SENSITIVITY:
        for sector, delta in FX_SENSITIVITY[fx["trend"]].items():
            scores[sector] = scores.get(sector, 0) + delta
            sign = "追い風" if delta > 0 else "逆風"
            reasons.setdefault(sector, []).append(f"為替: {fx['trend']} → {sign}")

    if ust and ust["trend"] in US_RATE_SENSITIVITY:
        for sector, delta in US_RATE_SENSITIVITY[ust["trend"]].items():
            scores[sector] = scores.get(sector, 0) + delta
            sign = "追い風" if delta > 0 else "逆風"
            reasons.setdefault(sector, []).append(f"米長期金利: {ust['trend']} → {sign}")

    results = [
        {"sector": s, "score": v, "reasons": reasons.get(s, [])}
        for s, v in scores.items()
    ]
    results.sort(key=lambda x: -x["score"])
    return results


# ============================================================
# 銘柄ランキング(マクロ要因 + セクターニュース + 個別ニュースの合算)
# ============================================================
#
# 株価データは使わず、「マクロ要因によるセクタースコア」+
# 「セクター単位のニュース傾向」+「個別銘柄のニュースでの好材料/悪材料」
# を単純に加算した参考スコア。あくまで参考情報であり、実際の株価の
# 動きを予測するものではない。

COMPANY_TO_SECTOR = {
    company: sector for sector, companies in SECTOR_COMPANIES.items() for company in companies
}

# 東証33業種区分 → 本アプリの独自セクターへの対応表(完全一致ではなく近似)。
# ここに無い業種(サービス業など幅広すぎるもの)は、セクター加点の対象外になるが、
# 個別ニュースでの好材料/悪材料の加点は引き続き受けられる。
JPX_INDUSTRY_TO_SECTOR = {
    "銀行業": "銀行",
    "卸売業": "商社",
    "小売業": "小売",
    "情報・通信業": "通信",
    "石油・石炭製品": "エネルギー",
    "電気・ガス業": "エネルギー",
    "不動産業": "不動産",
    "機械": "機械・FA",
    "輸送用機器": "自動車",
    "電気機器": "半導体",
    "精密機器": "半導体",
    "医薬品": "医薬品",
    "陸運業": "インバウンド",
    "海運業": "インバウンド",
    "空運業": "インバウンド",
}


def _get_company_sector_map():
    """
    company_master(東証上場銘柄一覧、業種区分つき)から、
    企業名→独自セクター名 の対応表を作る。取得できなければ
    SECTOR_COMPANIESによる従来の対応表にフォールバックする。
    """
    try:
        rows = sb.select_all("company_master", {"select": "name,industry"})
    except Exception:
        rows = []

    if not rows:
        return dict(COMPANY_TO_SECTOR), []

    mapping = {}
    all_names = []
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        all_names.append(name)
        industry = r.get("industry")
        sector = JPX_INDUSTRY_TO_SECTOR.get(industry)
        if sector:
            mapping[name] = sector
    # 独自リストの対応も残しておく(業種区分でカバーできない防衛等のため)
    for company, sector in COMPANY_TO_SECTOR.items():
        mapping.setdefault(company, sector)
    return mapping, all_names


def analyze_stock_ranking(macro_scores, sector_sentiment, news_items):
    """
    戻り値: [{"company":..., "sector":..., "score": n, "reasons": [...]}, ...]
            (点数の高い順)
    東証上場銘柄一覧(company_master)が使える場合は全銘柄が対象になり、
    使えない場合は手作業で用意した主要企業のみが対象になる。
    """
    macro_by_sector = {m["sector"]: m["score"] for m in macro_scores}
    sentiment_by_sector = {s["sector"]: s for s in sector_sentiment}

    company_sector_map, all_names = _get_company_sector_map()
    # 全銘柄名も対象に含める(セクターが分からなくても個別ニュース加点は入る)
    target_companies = dict.fromkeys(company_sector_map.keys())
    for name in all_names:
        target_companies.setdefault(name, None)

    results = []
    for company in target_companies:
        if len(company) < 2:
            continue
        sector = company_sector_map.get(company)
        score = 0
        reasons = []

        if sector:
            macro_score = macro_by_sector.get(sector, 0)
            if macro_score != 0:
                score += macro_score
                reasons.append(f"セクター({sector})のマクロ要因: {macro_score:+d}点")

            sent = sentiment_by_sector.get(sector)
            if sent:
                net = sent["positive"] - sent["negative"]
                if net != 0:
                    score += net
                    reasons.append(f"セクターニュース傾向: {sent['direction']}")

        pos_headlines, neg_headlines = [], []
        for it in news_items:
            text = it["title"] + " " + it["description"]
            if company not in text:
                continue
            if any(pw in text for pw in POSITIVE_WORDS):
                pos_headlines.append(it["title"])
            if any(nw in text for nw in NEGATIVE_WORDS):
                neg_headlines.append(it["title"])

        if pos_headlines:
            score += 2 * len(pos_headlines)
            reasons.append("個別ニュース(好材料): " + "、".join(pos_headlines[:2]))
        if neg_headlines:
            score -= 2 * len(neg_headlines)
            reasons.append("個別ニュース(悪材料): " + "、".join(neg_headlines[:2]))

        if score != 0:
            results.append({"company": company, "sector": sector, "score": score, "reasons": reasons})

    results.sort(key=lambda x: -x["score"])
    return results


def get_tankan_diffs():
    """短観の業況判断DIについて、前回調査との差分を判定する。"""
    results = []
    for code, label in TANKAN_LABELS.items():
        rows = sb.select(
            "raw_boj_series",
            {"select": "date,value", "series_code": f"eq.{code}", "order": "date.asc"},
        )
        if len(rows) < 2:
            continue
        latest_val = rows[-1]["value"]
        prev_val = rows[-2]["value"]
        diff = latest_val - prev_val
        direction = "改善" if diff > 0 else ("悪化" if diff < 0 else "横ばい")
        results.append(
            {
                "label": label,
                "latest": latest_val,
                "prev": prev_val,
                "diff": diff,
                "direction": direction,
            }
        )
    return results


def get_recent_news(limit=80):
    """Supabaseに保存されたニュースを新しい順に取得する。"""
    rows = sb.select(
        "raw_news",
        {"select": "title,description,link,pub_date", "order": "pub_date.desc", "limit": str(limit)},
    )
    return [
        {"title": r["title"], "description": r.get("description") or "", "link": r["link"], "pub_date": r["pub_date"]}
        for r in rows
    ]


# ============================================================
# 定型文コメントの組み立て(AIを使わないバージョン)
# ============================================================
def build_commentary(jgb, ust, wti, fx, tankan):
    lines = []

    if ust:
        if ust["trend"] == "低下":
            lines.append(
                "米国の長期金利が低下しており、グロース株や設備投資関連株には追い風となりやすい環境です。"
            )
        elif ust["trend"] == "上昇":
            lines.append(
                "米国の長期金利が上昇しており、グロース株には逆風となりやすい一方、金融株には追い風の可能性があります。"
            )
        else:
            lines.append("米国の長期金利は大きな変化がなく、落ち着いた状態です。")

    if jgb:
        if jgb["trend"] == "拡大(スティープ化)":
            lines.append(
                "日本の長短金利差は拡大傾向(スティープ化)にあり、銀行株などには追い風となりやすい局面です。"
            )
        elif jgb["trend"] == "縮小(フラット化)":
            lines.append("日本の長短金利差は縮小傾向(フラット化)にあります。")

    if fx:
        if fx["trend"] == "円安":
            lines.append(
                "為替は円安方向に動いており、輸出企業やインバウンド関連企業には追い風、輸入コストの多い企業には逆風となりやすい状況です。"
            )
        elif fx["trend"] == "円高":
            lines.append("為替は円高方向に動いており、輸出企業には逆風となりやすい状況です。")

    improved = [t["label"] for t in tankan if t["direction"] == "改善"]
    worsened = [t["label"] for t in tankan if t["direction"] == "悪化"]
    if improved:
        lines.append(f"日銀短観では、{'・'.join(improved)}の業況判断DIが改善しています。")
    if worsened:
        lines.append(f"一方で、{'・'.join(worsened)}の業況判断DIは悪化しています。")

    if wti:
        if wti["trend"] == "上昇":
            lines.append("WTI原油価格は上昇しており、エネルギー関連企業には追い風となる可能性があります。")
        elif wti["trend"] == "下落":
            lines.append("WTI原油価格は下落しており、運輸・化学などコスト面での追い風となる可能性があります。")

    if not lines:
        lines.append("現時点では、判定に十分なデータが揃っていません。データ取得を進めてから再度ご確認ください。")

    lines.append(
        "※本レポートはルールベースの機械的な判定であり、投資助言ではありません。最終的な投資判断はご自身で行ってください。"
    )
    return lines


# ============================================================
# HTML生成
# ============================================================
def _sentiment_of(value, positive_test, negative_test):
    if positive_test:
        return "positive"
    if negative_test:
        return "negative"
    return "neutral"


def render_row(label, value_text, sentiment="neutral"):
    """市況サマリーなど、単純な1行データ用(帳簿的な罫線スタイル)。"""
    return f"""
    <div class="row">
      <span class="row-label">{label}</span>
      <span class="value-badge value-{sentiment}">{value_text}</span>
    </div>
    """


def render_card(title, sentiment, value_text=None, highlight=None, sublines=None):
    """
    セクター・テーマ・マクロ要因など、根拠(見出し等)を伴う項目用のカード。
    左端に判定の色を細い帯で示す。
    """
    sub_html = "".join(f"<div class='card-sub'>・{s}</div>" for s in (sublines or []))
    highlight_html = f"<div class='card-highlight'>{highlight}</div>" if highlight else ""
    value_html = (
        f"<span class='value-badge value-{sentiment}'>{value_text}</span>" if value_text is not None else ""
    )
    return f"""
    <div class="card card-{sentiment}">
      <div class="card-head">
        <span class="card-title">{title}</span>
        {value_html}
      </div>
      {highlight_html}
      {sub_html}
    </div>
    """


def generate_html(jgb, ust, wti, fx, tankan, commentary, sector_sentiment=None, notable_stocks=None, theme_heat=None, macro_scores=None, stock_ranking=None):
    rows_html = ""
    if ust:
        sentiment = "positive" if ust["trend"] == "低下" else ("negative" if ust["trend"] == "上昇" else "neutral")
        rows_html += render_row("米長期金利(10年)", f"{ust['value']:.2f}% {ust['trend']}", sentiment)
    if jgb:
        rows_html += render_row(
            "日本長短金利差(10年-2年)", f"{jgb['spread']:.3f}% {jgb['trend'] or '-'}", "neutral"
        )
    if fx:
        sentiment = "positive" if fx["trend"] == "円安" else ("negative" if fx["trend"] == "円高" else "neutral")
        rows_html += render_row("ドル円", f"{fx['value']:.2f}円 {fx['trend']}({fx['diff']:+.2f})", sentiment)
    if wti:
        sentiment = "positive" if wti["trend"] == "上昇" else ("negative" if wti["trend"] == "下落" else "neutral")
        rows_html += render_row("WTI原油", f"${wti['value']:.2f} {wti['trend']}({wti['pct']:+.1f}%)", sentiment)

    tankan_html = ""
    for t in tankan:
        sentiment = "positive" if t["direction"] == "改善" else ("negative" if t["direction"] == "悪化" else "neutral")
        tankan_html += render_row(
            f"短観 {t['label']}", f"{t['latest']:.0f} {t['direction']}({t['diff']:+.0f})", sentiment
        )

    macro_html = ""
    if macro_scores:
        for m in macro_scores:
            sentiment = "positive" if m["score"] > 0 else ("negative" if m["score"] < 0 else "neutral")
            macro_html += render_card(
                m["sector"], sentiment, value_text=f"{m['score']:+d}点", sublines=m["reasons"]
            )

    sector_html = ""
    if sector_sentiment:
        for s in sorted(sector_sentiment, key=lambda x: -(x["positive"] - x["negative"])):
            sentiment = "positive" if s["direction"] == "↑" else ("negative" if s["direction"] == "↓" else "neutral")
            highlight = None
            if s.get("notable_companies"):
                highlight = "有望銘柄: " + "、".join(s["notable_companies"])
            sector_html += render_card(
                s["sector"], sentiment, value_text=s["direction"], highlight=highlight, sublines=s["headlines"]
            )

    theme_html = ""
    if theme_heat:
        max_count = max(t["count"] for t in theme_heat)
        for i, t in enumerate(theme_heat[:10], start=1):
            bar_width = int((t["count"] / max_count) * 100) if max_count else 0
            sub_html = "".join(f"<div class='card-sub'>・{h}</div>" for h in t["headlines"])
            theme_html += f"""
            <div class="card card-neutral">
              <div class="card-head">
                <span class="card-title"><span class="rank">{i}</span>{t['theme']}</span>
                <span class="value-badge value-gold">{t['count']}件</span>
              </div>
              <div class="bar-bg"><div class="bar-fill" style="width:{bar_width}%"></div></div>
              {sub_html}
            </div>
            """

    stocks_html = ""
    if notable_stocks:
        for s in notable_stocks[:10]:
            stocks_html += render_card(s["company"], "neutral", sublines=s["headlines"])

    ranking_html = ""
    if stock_ranking:
        for i, r in enumerate(stock_ranking[:10], start=1):
            sentiment = "positive" if r["score"] > 0 else "negative"
            sub_html = "".join(f"<div class='card-sub'>・{reason}</div>" for reason in r["reasons"])
            sector_label = f" ({r['sector']})" if r.get("sector") else ""
            ranking_html += f"""
            <div class="card card-{sentiment}">
              <div class="card-head">
                <span class="card-title"><span class="rank">{i}</span>{r['company']}<span style="color:var(--text-mute); font-weight:400; font-size:11.5px;">{sector_label}</span></span>
                <span class="value-badge value-{sentiment}">{r['score']:+d}点</span>
              </div>
              {sub_html}
            </div>
            """

    commentary_html = "".join(f"<p>{line}</p>" for line in commentary)

    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>今週の相場レポート</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@600;800&family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0E1015;
    --panel: #171B23;
    --border: #262C37;
    --text: #EEEAE1;
    --text-dim: #A4ABBA;
    --text-mute: #5C6272;
    --positive: #8FC49B;
    --positive-bg: rgba(143,196,155,0.14);
    --negative: #E2896A;
    --negative-bg: rgba(226,137,106,0.14);
    --gold: #D9B54A;
    --gold-bg: rgba(217,181,74,0.16);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: "Noto Sans JP", "Hiragino Sans", sans-serif;
    max-width: 520px; margin: 0 auto; padding: 32px 20px 56px;
    -webkit-font-smoothing: antialiased;
  }}
  header {{ border-bottom: 1px solid var(--border); padding-bottom: 18px; margin-bottom: 28px; }}
  h1 {{
    font-family: "Shippori Mincho", serif; font-weight: 800;
    font-size: 26px; letter-spacing: 0.02em; margin: 0 0 6px;
  }}
  .updated {{ color: var(--text-mute); font-size: 12px; }}
  h2 {{
    font-family: "Shippori Mincho", serif; font-weight: 600;
    font-size: 16px; color: var(--text);
    margin: 36px 0 14px; padding-left: 10px;
    border-left: 3px solid var(--gold);
  }}
  .row {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 11px 2px; border-bottom: 1px solid var(--border); font-size: 14px;
  }}
  .row-label {{ color: var(--text-dim); }}
  .value-badge {{
    font-weight: 700; font-size: 12.5px; font-variant-numeric: tabular-nums;
    padding: 3px 10px; border-radius: 4px; white-space: nowrap;
  }}
  .value-positive {{ background: var(--positive-bg); color: var(--positive); }}
  .value-negative {{ background: var(--negative-bg); color: var(--negative); }}
  .value-neutral  {{ background: rgba(255,255,255,0.06); color: var(--text-dim); }}
  .value-gold     {{ background: var(--gold-bg); color: var(--gold); }}
  p {{ font-size: 14px; line-height: 1.85; color: var(--text-dim); }}
  .disclaimer {{ font-size: 11px; color: var(--text-mute); margin-top: 28px; line-height: 1.7; }}
  .card {{
    background: var(--panel); border: 1px solid var(--border);
    border-left: 3px solid var(--text-mute);
    border-radius: 4px; padding: 13px 15px; margin-bottom: 10px;
  }}
  .card-positive {{ border-left-color: var(--positive); }}
  .card-negative {{ border-left-color: var(--negative); }}
  .card-head {{ display: flex; justify-content: space-between; align-items: center; }}
  .card-title {{ font-weight: 600; font-size: 14.5px; color: var(--text); }}
  .card-sub {{ font-size: 11.5px; color: var(--text-mute); line-height: 1.75; padding-top: 4px; }}
  .card-highlight {{ font-size: 11.5px; color: var(--gold); font-weight: 600; padding-top: 6px; }}
  .rank {{
    display: inline-block; width: 18px; color: var(--text-mute);
    font-family: "Shippori Mincho", serif; font-weight: 600; margin-right: 4px;
  }}
  .bar-bg {{ background: var(--border); height: 4px; border-radius: 2px; margin: 8px 0 4px; }}
  .bar-fill {{ background: var(--gold); height: 4px; border-radius: 2px; }}
</style>
</head>
<body>
  <header>
    <h1>今週の相場レポート</h1>
    <div class="updated">{now} 生成・個人利用(ルールベース判定)</div>
  </header>

  <h2>市況サマリー</h2>
  {rows_html}

  <h2>短観 業況判断DI</h2>
  {tankan_html if tankan_html else '<p>データがまだありません。</p>'}

  <h2>コメント</h2>
  {commentary_html}

  <h2>今日の有望銘柄ランキング(参考)</h2>
  {ranking_html if ranking_html else '<p>判定に必要なデータがまだありません。</p>'}

  <h2>為替・金利によるセクター影響</h2>
  {macro_html if macro_html else '<p>判定に必要なデータがまだありません。</p>'}

  <h2>今日のセクター(ニュースベース)</h2>
  {sector_html if sector_html else '<p>該当するニュースがまだありません。</p>'}

  <h2>テーマ熱量ランキング</h2>
  {theme_html if theme_html else '<p>該当するニュースがまだありません。</p>'}

  <h2>ニュースで話題の銘柄</h2>
  {stocks_html if stocks_html else '<p>該当するニュースがまだありません。</p>'}

  <p class="disclaimer">
    セクター判定・注目銘柄・銘柄ランキングは、キーワードの単純な出現回数や
    ルールベースの加減点に基づく簡易判定であり、実際の株価データは
    使用していません。実際の株価動向を保証するものではなく、
    投資助言でもありません。最終的な投資判断はご自身で行ってください。
  </p>
</body>
</html>
"""
    return html


# ============================================================
# メイン処理
# ============================================================
if __name__ == "__main__":
    jgb = get_jgb_state()
    ust = get_ust_trend()
    wti = get_wti_trend()
    fx = get_usdjpy_trend()
    tankan = get_tankan_diffs()
    news_items = get_recent_news()

    commentary = build_commentary(jgb, ust, wti, fx, tankan)
    sector_sentiment = analyze_sector_sentiment(news_items) if news_items else []
    notable_stocks = extract_notable_stocks(news_items) if news_items else []
    theme_heat = analyze_theme_heat(news_items) if news_items else []
    macro_scores = analyze_macro_sector_scores(fx, ust)
    stock_ranking = analyze_stock_ranking(macro_scores, sector_sentiment, news_items)
    html = generate_html(
        jgb, ust, wti, fx, tankan, commentary,
        sector_sentiment, notable_stocks, theme_heat, macro_scores, stock_ranking,
    )

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"{OUTPUT_HTML} を作成しました。GitHub Actionsがこれをpushし、Vercelが自動デプロイします。")
