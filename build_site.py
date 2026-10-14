#!/usr/bin/env python3
"""產生 docs/index.html 與 docs/result.csv（供 GitHub Pages 發佈）。

python build_site.py          # 抓真實資料
python build_site.py --demo   # 用模擬資料測試版面
"""
import argparse
import csv
import html
import os
import sys
from datetime import datetime, timedelta, timezone

import screener as S

TZ = timezone(timedelta(hours=8))
OUT = "docs"


def run(demo):
    if demo:
        rows, daytrade, otc_ok, industry, biz = S.demo_data()
        return (S.select(rows, daytrade, otc_ok), industry, biz, "DEMO", "DEMO",
                "模擬資料")
    rows = S.load_quotes()
    if not rows:
        sys.exit("取不到行情資料")
    daytrade, dt_date, otc_ok = S.load_daytrade()
    industry = S.load_industry()
    biz = S.load_business_csv()
    picked = S.select(rows, daytrade, otc_ok)
    for r in picked:
        if not biz.get(r["code"]):
            biz[r["code"]] = S.fetch_business(r["code"], r["market"])
    n = lambda m: sum(1 for r in rows if r["market"] == m)
    d = lambda m: sum(1 for r in rows if r["market"] == m and r["code"] in daytrade)
    stats = (f"診斷：行情 上市 {n('上市')} 檔／上櫃 {n('上櫃')} 檔；"
             f"其中可當沖 上市 {d('上市')}／上櫃 {d('上櫃')}；"
             f"行情日期 {rows[0]['date']}")
    got = sum(1 for r in picked if biz.get(r["code"]))
    stats += f"；主要業務 {got}/{len(picked)}"
    if S.BIZ_LOG:
        stats += f"（失敗例：{S.BIZ_LOG[0]}）"
    print(stats)
    return picked, industry, biz, rows[0]["date"], dt_date, stats


def esc(x):
    return html.escape(str(x))


def build_html(picked, updated, qdate, ddate, stats=""):
    n_lu = sum(1 for r in picked if r["tag"] == "漲停")
    cards = []
    for i, r in enumerate(picked, 1):
        up = r["pct"] >= 0
        cards.append(f"""
<article class="card {'lu' if r['tag']=='漲停' else 'fill'}">
  <header>
    <span class="rank">{i}</span>
    <div class="id"><b>{esc(r['code'])}</b> {esc(r['name'])}
      <small>{esc(r['market'])}</small></div>
    <span class="tag">{esc(r['tag'])}</span>
  </header>
  <div class="nums">
    <div><label>收盤</label>{esc(r['close'])}</div>
    <div class="{'up' if up else 'dn'}"><label>漲幅</label>{r['pct']:+.2f}%</div>
    <div><label>成交量</label>{r['lots']:,} 張</div>
  </div>
  <dl>
    <dt>當沖</dt><dd>{esc(S.DT_LABEL[r['daytrade']])}</dd>
    <dt>產業</dt><dd>{esc(r['industry'])}</dd>
    <dt>主要產品/服務</dt><dd>{esc(r['business'] or '（尚未建立資料）')}</dd>
  </dl>
</article>""")
    return f"""<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日漲停篩選</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--tx:#1b1f24;--mu:#6b7280;--bd:#e5e7eb;--up:#d92d20;--dn:#12b76a;--ac:#2563eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#171a21;--tx:#e8eaed;--mu:#9aa3af;--bd:#262b36;--ac:#6ea0ff}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 -apple-system,"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif}}
main{{max-width:960px;margin:0 auto;padding:16px}}
h1{{font-size:20px;margin:8px 0 4px}}
.meta{{color:var(--mu);font-size:13px;margin-bottom:12px}}
.grid{{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(290px,1fr))}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px 14px}}
.card.lu{{border-left:4px solid var(--up)}} .card.fill{{border-left:4px solid var(--mu)}}
header{{display:flex;align-items:center;gap:10px}}
.rank{{width:24px;height:24px;border-radius:50%;background:var(--bd);display:grid;place-items:center;font-size:12px}}
.id{{flex:1}} .id small{{color:var(--mu);margin-left:4px}}
.tag{{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--bd);color:var(--mu)}}
.lu .tag{{color:var(--up);border-color:var(--up)}}
.nums{{display:flex;justify-content:space-between;margin:10px 0;font-variant-numeric:tabular-nums;font-weight:600}}
.nums label{{display:block;font-weight:400;font-size:12px;color:var(--mu)}}
.up{{color:var(--up)}} .dn{{color:var(--dn)}}
dl{{margin:0;display:grid;grid-template-columns:auto 1fr;gap:4px 10px;font-size:14px}}
dt{{color:var(--mu)}} dd{{margin:0}}
footer{{color:var(--mu);font-size:12px;margin:16px 0}}
a{{color:var(--ac)}}
</style></head><body><main>
<h1>每日漲停篩選</h1>
<div class="meta">行情日期 {esc(qdate)} ・ 更新於 {esc(updated)} ・ 漲停 {n_lu} 檔、補進 {len(picked)-n_lu} 檔 ・
<a href="result.csv">下載 CSV</a></div>
<div class="grid">{''.join(cards)}</div>
<footer>條件：漲停前20名（依成交量），不足以漲幅最高者補進；成交量 &gt; 1000 張；可當沖標的。
當沖名單日期 {esc(ddate)}。資料來源：臺灣證券交易所、櫃買中心。僅供參考，不構成投資建議。<br>{esc(stats)}</footer>
</main></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    picked, industry, biz, qdate, ddate, stats = run(a.demo)
    for r in picked:
        r["industry"] = industry.get(r["code"], "未知")
        r["business"] = biz.get(r["code"], "")
    updated = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    os.makedirs(OUT, exist_ok=True)
    with open(f"{OUT}/index.html", "w", encoding="utf-8") as f:
        f.write(build_html(picked, updated, qdate, ddate, stats))
    with open(f"{OUT}/result.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["排名", "代號", "名稱", "市場", "收盤價", "漲停價", "漲幅%", "成交量(張)",
                    "類型", "當沖資格", "產業類別", "主要產品/服務"])
        for i, r in enumerate(picked, 1):
            w.writerow([i, r["code"], r["name"], r["market"], r["close"], r["limit_price"],
                        f"{r['pct']:.2f}", r["lots"], r["tag"], S.DT_LABEL[r["daytrade"]],
                        r["industry"], r["business"]])
    print(f"完成：{len(picked)} 檔，行情日期 {qdate}")


if __name__ == "__main__":
    main()
