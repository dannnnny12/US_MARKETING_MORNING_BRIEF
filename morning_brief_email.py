import os, json, smtplib, logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import html, re
from zoneinfo import ZoneInfo

import yfinance as yf
import feedparser
import requests
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Taipei"))
HOURS_BACK = int(os.getenv("HOURS_BACK", "18"))
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Google News RSS search feeds; titles/links are used as source material, not treated as verified facts.
NEWS_QUERIES = [
    "US stock market economy Federal Reserve inflation earnings",
    "world news geopolitics energy commodities central banks",
    "Taiwan semiconductor supply chain TSMC trade",
]
RSS_BASE = "https://news.google.com/rss/search"

TICKERS = {
    "S&P 500": "^GSPC", "NASDAQ Composite": "^IXIC",
    "Dow Jones": "^DJI", "Philadelphia Semiconductor": "^SOX",
    "VIX": "^VIX", "台積電 ADR": "TSM",
    "美元指數代理（UUP）": "UUP", "美國 10 年期公債殖利率代理（IEF）": "IEF",
}

def market_snapshot():
    symbols = list(TICKERS.values())
    data = yf.download(symbols, period="5d", interval="1d",
                       auto_adjust=True, progress=False, threads=True)
    result = []
    if data.empty:
        return [{"error": "Yahoo Finance 暫時沒有回傳行情資料"}]
    close = data["Close"]
    for name, ticker in TICKERS.items():
        try:
            series = close[ticker].dropna()
            if len(series) < 2:
                continue
            last, prev = float(series.iloc[-1]), float(series.iloc[-2])
            result.append({"name": name, "ticker": ticker,
                           "close": round(last, 3),
                           "change_pct": round((last / prev - 1) * 100, 2)})
        except Exception as e:
            logging.warning("行情缺漏 %s: %s", ticker, e)
    return result

def fetch_news():
    now = datetime.now(TZ)
    cutoff = now - timedelta(hours=HOURS_BACK)
    articles, seen = [], set()
    for query in NEWS_QUERIES:
        try:
            resp = requests.get(RSS_BASE, params={
                "q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"
            }, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for item in feed.entries:
                link = item.get("link", "")
                title = item.get("title", "").strip()
                if not title or link in seen:
                    continue
                published = item.get("published", "")
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(published)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=TZ)
                    if dt.astimezone(TZ) < cutoff:
                        continue
                except Exception:
                    pass
                seen.add(link)
                articles.append({
                    "title": title,
                    "source": item.get("source", {}).get("title", "Google News RSS"),
                    "published": published,
                    "link": link,
                })
        except Exception as e:
            logging.warning("新聞來源失敗（%s）: %s", query, e)
    return articles[:60]

def generate_report(market, news):
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z")
    prompt = f"""
你是專業的全球總體經濟與台灣股市晨報分析師。現在時間：{now}。
請用繁體中文產出一份可在 2 分鐘內讀完、視覺活潑且適合手機閱讀的晨報。
請大量但適度使用有意義的 emoji 作為視覺導覽，例如：🌅📈📉🇺🇸🌏🏦🧠🇹🇼💡⚠️🔍✅；每個段落標題前放 1 個 emoji，行情漲跌可用 🟢🔴⚪，不要每句都塞 emoji。
請使用清楚的 Markdown：## 作為大段標題、### 作為小標題、條列式重點、粗體標示關鍵數字與結論。避免輸出一整大段純文字。

資料時間範圍：最近約 {HOURS_BACK} 小時（新聞時間戳若缺漏，請明確標示）。
行情資料（Yahoo Finance 日線收盤價及相較前一交易日變動）：
{json.dumps(market, ensure_ascii=False)}

新聞標題與來源連結（僅為候選線索，不能只憑標題斷言內文事實）：
{json.dumps(news, ensure_ascii=False)}

請依序輸出：
1. 今日一句話總結：市場目前在交易什麼。
2. 美股行情：逐項整理主要指數、半導體、VIX、台積電 ADR；註明這是最近一個可取得的日線收盤，不要假裝是即時盤中數據。
3. 隔夜重要事件：按「美國政策/央行/經濟數據」、「企業/科技/半導體」、「地緣政治/能源/其他全球事件」分類。每項附來源連結；無法由標題確認的內容標示「待查證」。
4. 總體經濟解讀：利率預期、美元、債券、通膨、景氣、風險偏好之間的因果關係；區分已知事實與推論。
5. 對今日台股的可能影響：加權指數、台積電/半導體、電子權值、金融、航運/原物料等可能受影響族群；列出利多、利空與中性情境。
6. 今日觀察清單：開盤後要驗證的 3–5 個訊號。

若資料不足，直接說明缺漏，不可捏造數字、新聞內文或事件。所有行情需標明日期/時間與資料限制。
"""
    msg = client.messages.create(
        model=MODEL, max_tokens=4500,
        messages=[{"role": "user", "content": prompt}]
    )
    return "\n".join(block.text for block in msg.content if getattr(block, "type", "") == "text")

def markdown_to_html(report):
    """Convert the report's simple Markdown into a mobile-friendly HTML email."""
    lines = report.splitlines()
    out = []
    in_list = False
    for raw in lines:
        line = raw.strip()
        if not line:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if line.startswith("- ") or line.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = line[2:]
            out.append(f"<li>{inline_format(content)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("### "):
            out.append(f"<h3>{inline_format(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline_format(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{inline_format(line[2:])}</h1>")
        else:
            out.append(f"<p>{inline_format(line)}</p>")
    if in_list:
        out.append("</ul>")
    body = "\n".join(out)
    return f'''<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#f3f5f8;font-family:Arial,'Microsoft JhengHei',sans-serif;color:#202938;line-height:1.75">
<div style="max-width:760px;margin:0 auto;padding:18px 12px">
<div style="background:linear-gradient(135deg,#102a43,#2563a6);color:white;padding:24px;border-radius:16px 16px 0 0">
<div style="font-size:13px;opacity:.85">DAILY GLOBAL MARKET BRIEF</div><div style="font-size:25px;font-weight:700;margin-top:6px">🌅 美股隔夜 × 台股晨報</div>
<div style="font-size:13px;opacity:.9;margin-top:6px">把全球事件，轉換成台股市場語言</div></div>
<div style="background:white;padding:22px;border-radius:0 0 16px 16px;box-shadow:0 4px 18px #102a4312">{body}
<hr style="border:0;border-top:1px solid #e5eaf0;margin:24px 0"><p style="font-size:12px;color:#68778a">🤖 AI 輔助研究摘要｜行情與新聞可能有延遲或缺漏，請點選原始來源查證。本報告不構成投資建議。</p></div></div></body></html>'''

def inline_format(text):
    text = html.escape(text, quote=True)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\[(.+?)\]\((https?://[^ )]+)\)', r'<a href="\2" style="color:#2563a6">\1</a>', text)
    text = re.sub(r'(https?://[^\s<]+)', r'<a href="\1" style="color:#2563a6">來源連結</a>', text)
    return text

def send_email(text_report, html_report):
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError("缺少 Email 設定：" + ", ".join(missing))

    now = datetime.now(TZ).strftime("%Y-%m-%d")
    msg = MIMEMultipart("alternative")  # 支援純文字與 HTML 雙重夾帶
    msg["Subject"] = f"美股隔夜與台股晨報｜{now}"
    msg["From"] = os.getenv("EMAIL_FROM", os.getenv("SMTP_USER"))
    msg["To"] = os.getenv("EMAIL_TO")

    # 附加純文字備份與美化後的 HTML 內容
    msg.attach(MIMEText(text_report, "plain", "utf-8"))
    msg.attach(MIMEText(html_report, "html", "utf-8"))

    with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587"))) as server:
        server.starttls()
        server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
        server.send_message(msg)

def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("請在 .env 設定 ANTHROPIC_API_KEY")
    logging.info("抓取行情與新聞")
    market = market_snapshot()
    news = fetch_news()
    logging.info("整理分析：行情 %d 筆、新聞 %d 則", len(market), len(news))
    report = generate_report(market, news)
    os.makedirs("reports", exist_ok=True)
    stamp = datetime.now(TZ).strftime("reports/morning_%Y%m%d_%H%M")
    md_file = stamp + ".md"
    html_file = stamp + ".html"
    html_report = markdown_to_html(report)
    with open(md_file, "w", encoding="utf-8") as f:
        f.write(report)
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_report)
    send_email(report, html_report)
    logging.info("完成並寄送：%s（Markdown + HTML）", stamp)

if __name__ == "__main__":
    main()