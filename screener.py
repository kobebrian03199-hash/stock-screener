#!/usr/bin/env python3
"""
台股每日漲停篩選器

條件
  1. 當日漲停個股，取前 20 名（依成交量由大到小排序）
  2. 不足 20 檔時，以「漲幅最高者」依序補滿 20 檔
  3. 成交量必須 > 1000 張（1 張 = 1000 股）
  4. 必須為可當沖標的
  5. 列出產業類別與主要產品/服務

用法
  pip install requests
  python screener.py              # 以今日（或最近一個交易日）資料執行
  python screener.py --demo       # 用模擬資料驗證邏輯（不連網）
  python screener.py --top 20 --min-lots 1000 --csv result.csv

資料來源
  行情(上市)  https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
  行情(上櫃)  https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
  當沖標的    https://www.twse.com.tw/rwd/zh/dayTrading/TWTB4U?response=json
  公司資料    https://openapi.twse.com.tw/v1/opendata/t187ap03_L (上市)
              https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O (上櫃)

主要產品/服務
  證交所開放資料沒有「主要經營業務」欄位。程式會先讀本機 business.csv
  （欄位: code,business），讀不到再自動從 Yahoo 奇摩股市個股資料頁抓取
  「主要經營業務」，仍失敗則留白。
"""
import argparse
import csv
import html
import json
import os
import re
import sys
from decimal import Decimal, ROUND_FLOOR

try:
    import requests
except ImportError:  # demo 模式不需要 requests
    requests = None

TWSE_QUOTES = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_QUOTES = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TWSE_DAYTRADE = "https://www.twse.com.tw/rwd/zh/dayTrading/TWTB4U?response=json"
TWSE_COMPANY = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_COMPANY = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
# 櫃買「現股當沖交易標的」頁面對應的端點（由搜尋結果得知，回傳格式尚未實測）。
# 取不到時預設排除上櫃股（見 --keep-unverified-otc），並印出回傳內容供除錯。
TPEX_DAYTRADE = "https://www.tpex.org.tw/www/zh-tw/intraday/list"
MOPS_BUSINESS = "https://mops.twse.com.tw/mops/web/ajax_t05st03"

INDUSTRY = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "13": "電子工業",
    "14": "建材營造", "15": "航運業", "16": "觀光餐旅", "17": "金融保險",
    "18": "貿易百貨", "19": "綜合", "20": "其他", "21": "化學工業",
    "22": "生技醫療", "23": "油電燃氣", "24": "半導體業", "25": "電腦及週邊設備",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業", "33": "農業科技",
    "34": "電子商務", "35": "綠能環保", "36": "數位雲端", "37": "運動休閒",
    "38": "居家生活", "80": "管理股票",
}

UA = {"User-Agent": "Mozilla/5.0 (stock-screener)"}


# ---------------------------------------------------------------- 工具函式
def num(x):
    """'1,234.5' -> Decimal；'--'、''、None -> None"""
    if x is None:
        return None
    s = str(x).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "---", "X", "-"):
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def tick(price: Decimal) -> Decimal:
    """台股股票升降單位"""
    if price < 10:
        return Decimal("0.01")
    if price < 50:
        return Decimal("0.05")
    if price < 100:
        return Decimal("0.1")
    if price < 500:
        return Decimal("0.5")
    if price < 1000:
        return Decimal("1")
    return Decimal("5")


def limit_up_price(prev_close: Decimal) -> Decimal:
    """昨收 x 1.10，依升降單位無條件捨去"""
    raw = prev_close * Decimal("1.10")
    t = tick(raw)
    return (raw / t).to_integral_value(rounding=ROUND_FLOOR) * t


def get_json(url, tries=4, **kw):
    """帶重試的 GET。'Response ended prematurely' 這類連線中斷通常重試即可。"""
    import time
    headers = dict(UA, **{"Accept-Encoding": "identity", "Connection": "close"})
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=30, **kw)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def load_tpex_quotes_legacy(target=None):
    """備用：櫃買舊版『上櫃股票收盤行情』端點（欄位順序依我的記憶，未實測）。
    aaData 每列：代號、名稱、收盤、漲跌、開盤、最高、最低、均價、成交股數…
    target 為民國日期字串如 '1151016'：只取該日；不給則從今天往前找最近交易日。"""
    from datetime import datetime, timedelta, timezone
    if target and len(target) == 7 and target.isdigit():
        days = [(int(target[:3]) + 1911, int(target[3:5]), int(target[5:7]))]
    else:
        now = datetime.now(timezone(timedelta(hours=8)))
        days = []
        for b in range(5):
            d = now - timedelta(days=b)
            days.append((d.year, d.month, d.day))
    out = []
    for y, mo, dd in days:
        roc = f"{y - 1911}/{mo:02d}/{dd:02d}"
        url = ("https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/"
               "stk_quote_result.php")
        try:
            j = get_json(url, params=dict(l="zh-tw", o="json", d=roc))
        except Exception:
            continue
        rows = j.get("aaData") or (j.get("tables") or [{}])[0].get("data") or []
        for r in rows:
            try:
                out.append({"SecuritiesCompanyCode": r[0], "CompanyName": r[1],
                            "Close": r[2], "Change": r[3], "TradingShares": r[8],
                            "Date": f"{y - 1911}{mo:02d}{dd:02d}"})
            except Exception:
                continue
        if out:
            return out
    return out


# ---------------------------------------------------------------- 行情
def pick(d, *names):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


FIELDS = {
    "上市": dict(code=("Code",), name=("Name",), vol=("TradeVolume",),
                 close=("ClosingPrice",), chg=("Change",), date=("Date",)),
    "上櫃": dict(code=("SecuritiesCompanyCode", "Code"), name=("CompanyName", "Name"),
                 vol=("TradingShares", "TradeVolume"), close=("Close", "ClosingPrice"),
                 chg=("Change",), date=("Date",)),
}
QUOTE_NOTES = []  # 上市/上櫃資料日期不一致等提醒，會顯示在網頁底部


def norm_date(s):
    """統一成民國 7 碼，例如 1151016；西元 8 碼會轉換；無法判斷回傳空字串。"""
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 8 and d.startswith("20"):
        d = f"{int(d[:4]) - 1911}{d[4:]}"
    return d if len(d) == 7 else ""


def rows_from(data, market):
    f = FIELDS[market]
    rows = []
    for d in data:
        code = str(pick(d, *f["code"]) or "").strip()
        if not re.fullmatch(r"[1-9]\d{3}", code):  # 只取 4 碼一般股票
            continue
        close, chg = num(pick(d, *f["close"])), num(pick(d, *f["chg"]))
        vol = num(pick(d, *f["vol"]))
        if close is None or chg is None or vol is None or close <= 0:
            continue
        prev = close - chg
        if prev <= 0:
            continue
        rows.append(dict(
            code=code, name=str(pick(d, *f["name"]) or "").strip(),
            market=market, close=close, prev=prev,
            pct=(chg / prev * 100), lots=int(vol / 1000),
            date=norm_date(pick(d, *f["date"])),
        ))
    return rows


def load_quotes():
    rows = []
    for market, url in (("上市", TWSE_QUOTES), ("上櫃", TPEX_QUOTES)):
        try:
            data = get_json(url)
        except Exception as e:
            print(f"[警告] 無法取得{market}行情：{e}", file=sys.stderr)
            data = []
            if market == "上櫃":
                data = load_tpex_quotes_legacy()
                print(f"[資訊] 改用櫃買備用端點，取得 {len(data)} 筆", file=sys.stderr)
        got = rows_from(data, market) if data else []
        print(f"[資訊] {market}行情 {len(got)} 檔", file=sys.stderr)
        rows += got

    # 上市與上櫃必須是同一個交易日，否則排名會混到不同天的資料
    tw = sorted({r["date"] for r in rows if r["market"] == "上市" and r["date"]})
    target = tw[-1] if tw else ""
    otc = [r for r in rows if r["market"] == "上櫃"]
    otc_dates = sorted({r["date"] for r in otc if r["date"]})
    if target and otc_dates and otc_dates != [target]:
        QUOTE_NOTES.append(f"上櫃行情日期 {'/'.join(otc_dates)} 與上市 {target} 不一致")
        fresh = rows_from(load_tpex_quotes_legacy(target), "上櫃")
        rows = [r for r in rows if r["market"] != "上櫃"]
        if fresh:
            rows += fresh
            QUOTE_NOTES.append(f"已改用櫃買備用端點取得 {target} 上櫃行情 {len(fresh)} 檔")
        else:
            QUOTE_NOTES.append("備用端點也取不到同日上櫃行情，本次已排除上櫃股")
        print("[警告] " + "；".join(QUOTE_NOTES), file=sys.stderr)
    return rows


# ---------------------------------------------------------------- 當沖標的
def load_daytrade():
    """回傳 {code: 'both' | 'buy_first_only' | 'otc_unverified'} 與 資料日期"""
    ok, date = {}, ""
    try:
        j = get_json(TWSE_DAYTRADE)
        date = j.get("date", "")
        for t in j.get("tables", []):
            for r in t.get("data", []):
                # 第3欄 'Y' = 暫停現股賣出後現款買進當沖（只能先買後賣）
                ok[str(r[0]).strip()] = "buy_first_only" if r[2].strip() == "Y" else "both"
    except Exception as e:
        print(f"[警告] 無法取得證交所當沖標的：{e}", file=sys.stderr)
    otc = load_tpex_daytrade()
    for code, st in otc.items():
        ok.setdefault(code, st)
    return ok, date, bool(otc)


def load_tpex_daytrade():
    """櫃買當沖標的。回傳 {code: status}；失敗回傳 {} 並印出回傳內容前 300 字供除錯。"""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone(timedelta(hours=8)))
    roc = f"{now.year - 1911}/{now.month:02d}/{now.day:02d}"
    out, last_text = {}, ""
    for date in (roc, ""):  # 先帶今天日期，不行再不帶日期
        try:
            r = requests.get(TPEX_DAYTRADE, headers=UA, timeout=20,
                             params=dict(code="", date=date, response="json"))
            last_text = r.text
            j = r.json()
        except Exception as e:
            print(f"[警告] 櫃買當沖標的請求失敗(date={date!r})：{e}", file=sys.stderr)
            continue
        tables = j.get("tables") or [{"data": j.get("data", [])}]
        for t in tables:
            for row in t.get("data", []) or []:
                if not row:
                    continue
                code = str(row[0]).strip()
                if re.fullmatch(r"[1-9]\d{3}", code):
                    # 若後面欄位出現 'Y'，視為暫停先賣後買（格式未實測，屬推測）
                    flag = any(str(c).strip() == "Y" for c in row[2:])
                    out[code] = "buy_first_only" if flag else "both"
        if out:
            return out
    print("[警告] 無法取得櫃買當沖標的（上櫃股預設排除）。回傳內容前 300 字：\n"
          + last_text[:300], file=sys.stderr)
    return {}


# ---------------------------------------------------------------- 產業 / 業務
def load_industry():
    m = {}
    for url in (TWSE_COMPANY, TPEX_COMPANY):
        try:
            for d in get_json(url):
                code = str(pick(d, "公司代號", "SecuritiesCompanyCode") or "").strip()
                ind = str(pick(d, "產業別", "SecuritiesIndustryCode") or "").strip()
                if code:
                    m[code] = INDUSTRY.get(ind.zfill(2), ind or "未知")
        except Exception as e:
            print(f"[警告] 無法取得公司基本資料 {url}：{e}", file=sys.stderr)
    return m


def load_business_csv(path="business.csv"):
    m = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                m[str(r.get("code", "")).strip()] = (r.get("business") or "").strip()
    return m


BIZ_LOG = []  # 抓取失敗的原因，會顯示在網頁底部方便除錯
BIZ_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# 『主要經營業務』之後若遇到這些欄位名稱，代表業務描述已結束
BIZ_STOP = ("公司名稱", "產業類別", "董事長", "總經理", "發言人", "成立", "上市日期",
            "上櫃日期", "股本", "資本額", "公司網址", "網址", "電話", "地址", "傳真",
            "電子郵件", "股票過戶", "簽證會計師", "公司簡介", "員工人數", "更多",
            "配股資訊", "配息資訊", "股利所屬期間")


def html_to_lines(text):
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    t = re.sub(r"(?i)<br\s*/?>|</(div|li|p|tr|td|th|dd|dt|span|h\d)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return [re.sub(r"\s+", " ", ln).strip() for ln in t.split("\n") if ln.strip()]


def clean_business(parts):
    """去除重複句與句尾標點，用『；』串接，過長則截斷。"""
    seen, out = set(), []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip(" 、。;；")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    s = "；".join(out)
    return s if len(s) <= 90 else s[:90] + "…"


def parse_business(text):
    """從個股資料頁 HTML 取出『主要經營業務』。找不到回傳空字串。"""
    label = "主要經營業務"
    lines = html_to_lines(text)
    for i, ln in enumerate(lines):
        if not ln.startswith(label):
            continue
        out = []
        rest = ln[len(label):].strip(" :：")
        if rest:
            out.append(rest)
        for nxt in lines[i + 1:i + 7]:
            if any(nxt.startswith(s) for s in BIZ_STOP):
                break
            out.append(nxt)
            if len(out) >= 4:
                break
        if out:
            return clean_business(out)
    # 備援：頁面把資料放在 script 的 JSON 內
    m = re.search(r'"主要經營業務"\s*:\s*"([^"]{2,200})"', text)
    return m.group(1).replace("\\n", "、")[:120] if m else ""


def fetch_business(code, market):
    """從 Yahoo 奇摩股市個股資料頁抓『主要經營業務』。失敗回傳空字串並記錄原因。"""
    import time
    suffixes = [".TWO", ".TW"] if market == "上櫃" else [".TW", ".TWO"]
    for suf in suffixes:
        url = f"https://tw.stock.yahoo.com/quote/{code}{suf}/profile"
        try:
            r = requests.get(url, timeout=20, headers={
                "User-Agent": BIZ_UA, "Accept-Language": "zh-TW,zh;q=0.9"})
            if r.status_code != 200:
                BIZ_LOG.append(f"{code}{suf}: HTTP {r.status_code}")
            else:
                b = parse_business(r.text)
                if b:
                    return b
                BIZ_LOG.append(f"{code}{suf}: 頁面找不到『主要經營業務』")
        except Exception as e:
            BIZ_LOG.append(f"{code}{suf}: {type(e).__name__}")
        time.sleep(0.5)
    return ""


def fetch_business_mops(code):
    """從公開資訊觀測站抓『主要經營業務』。頁面格式可能變動，失敗回傳空字串。"""
    try:
        r = requests.post(MOPS_BUSINESS, headers=UA, timeout=20, data=dict(
            encodeURIComponent=1, step=1, firstin=1, off=1, queryName="co_id",
            inpuType="co_id", TYPEK="all", co_id=code))
        r.encoding = "utf-8"
        m = re.search(r"主要經營業務.*?<td[^>]*>(.*?)</td>", r.text, re.S)
        if m:
            txt = re.sub(r"<[^>]+>|&nbsp;", " ", m.group(1))
            txt = re.sub(r"\s+", " ", txt).strip()
            return txt[:120]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 篩選核心
def select(rows, daytrade, otc_ok, top=20, min_lots=1000, keep_unverified_otc=False):
    elig = []
    for r in rows:
        if r["lots"] <= min_lots:
            continue
        status = daytrade.get(r["code"])
        if status is None:
            if r["market"] == "上櫃" and not otc_ok and keep_unverified_otc:
                status = "otc_unverified"
            else:
                continue
        r = dict(r, daytrade=status)
        r["limit_price"] = limit_up_price(r["prev"])
        r["is_limit_up"] = r["close"] >= r["limit_price"]
        elig.append(r)

    limit_ups = sorted((r for r in elig if r["is_limit_up"]),
                       key=lambda r: r["lots"], reverse=True)[:top]
    picked = [dict(r, tag="漲停") for r in limit_ups]
    if len(picked) < top:
        rest = sorted((r for r in elig if not r["is_limit_up"]),
                      key=lambda r: (r["pct"], r["lots"]), reverse=True)
        picked += [dict(r, tag="補進(漲幅)") for r in rest[: top - len(picked)]]
    return picked


DT_LABEL = {"both": "可(先買/先賣)", "buy_first_only": "僅先買後賣",
            "otc_unverified": "未驗證"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-lots", type=int, default=1000, help="成交量門檻(張)，需大於此值")
    ap.add_argument("--csv", default="screener_result.csv")
    ap.add_argument("--no-mops", action="store_true", help="不連公開資訊觀測站抓主要業務")
    ap.add_argument("--keep-unverified-otc", action="store_true",
                    help="櫃買當沖名單取不到時，仍保留上櫃股並標示『未驗證』")
    ap.add_argument("--demo", action="store_true", help="使用模擬資料")
    a = ap.parse_args()

    if a.demo:
        rows, daytrade, otc_ok, industry, biz = demo_data()
        data_date = "DEMO"
    else:
        if requests is None:
            sys.exit("請先安裝 requests：pip install requests")
        rows = load_quotes()
        if not rows:
            sys.exit("取不到行情資料，請確認網路後重試（盤後約 14:00 起才有當日資料）。")
        daytrade, dt_date, otc_ok = load_daytrade()
        industry = load_industry()
        biz = load_business_csv()
        data_date = rows[0]["date"] or dt_date
        print(f"行情日期：{data_date}　當沖名單日期：{dt_date}")

    picked = select(rows, daytrade, otc_ok, a.top, a.min_lots, a.keep_unverified_otc)

    for r in picked:
        r["industry"] = industry.get(r["code"], "未知")
        b = biz.get(r["code"], "")
        if not b and not a.demo and not a.no_mops:
            b = fetch_business(r["code"], r["market"])
        r["business"] = b or "（請補充：business.csv）"

    n_lu = sum(1 for r in picked if r["tag"] == "漲停")
    print(f"\n漲停 {n_lu} 檔，補進 {len(picked) - n_lu} 檔，共 {len(picked)} 檔\n")
    hdr = f"{'#':>2} {'代號':<5}{'名稱':<8}{'市場':<4}{'收盤':>8}{'漲幅%':>8}{'張數':>9}  {'類型':<8}{'當沖':<10}{'產業':<8} 主要產品/服務"
    print(hdr)
    print("-" * 120)
    for i, r in enumerate(picked, 1):
        print(f"{i:>2} {r['code']:<5}{r['name']:<8}{r['market']:<4}{r['close']:>8}"
              f"{r['pct']:>8.2f}{r['lots']:>9,}  {r['tag']:<8}"
              f"{DT_LABEL[r['daytrade']]:<10}{r['industry']:<8} {r['business']}")

    with open(a.csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["排名", "代號", "名稱", "市場", "收盤價", "漲停價", "漲幅%", "成交量(張)",
                    "類型", "當沖資格", "產業類別", "主要產品/服務"])
        for i, r in enumerate(picked, 1):
            w.writerow([i, r["code"], r["name"], r["market"], r["close"], r["limit_price"],
                        f"{r['pct']:.2f}", r["lots"], r["tag"], DT_LABEL[r["daytrade"]],
                        r["industry"], r["business"]])
    print(f"\n已輸出：{a.csv}")


# ---------------------------------------------------------------- demo
def demo_data():
    D = Decimal
    spec = [  # code, name, market, prev, close, lots
        ("2330", "台積電", "上市", "1000", "1000", 50000),
        ("2401", "凌陽", "上市", "20.00", "22.00", 8000),     # 漲停
        ("3008", "大立光", "上市", "2000", "2200", 3000),      # 不到漲停(升降單位5→2200 為漲停)
        ("2303", "聯電", "上市", "50.0", "55.0", 12000),       # 漲停
        ("6488", "環球晶", "上櫃", "400", "440", 6000),        # 漲停但櫃買名單缺
        ("2603", "長榮", "上市", "100", "106", 20000),         # 漲幅補進
        ("2609", "陽明", "上市", "80.0", "83.0", 900),         # 量不足
        ("1101", "台泥", "上市", "30.00", "33.00", 1500),      # 漲停
        ("2330", "dup", "上市", "1", "1", 1),
    ][:-1]
    rows = []
    for c, n, m, p, cl, lots in spec:
        p, cl = D(p), D(cl)
        rows.append(dict(code=c, name=n, market=m, close=cl, prev=p,
                         pct=(cl - p) / p * 100, lots=lots, date="DEMO"))
    daytrade = {"2330": "both", "2401": "both", "3008": "both", "2303": "both",
                "2603": "both", "2609": "both", "1101": "buy_first_only"}
    industry = {"2401": "半導體業", "2303": "半導體業", "2603": "航運業", "1101": "水泥工業",
                "2330": "半導體業", "3008": "光電業"}
    biz = {"2401": "IC設計", "2303": "晶圓代工", "2603": "貨櫃海運", "1101": "水泥、電力"}
    return rows, daytrade, False, industry, biz


if __name__ == "__main__":
    main()
