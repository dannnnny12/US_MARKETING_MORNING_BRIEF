import os, json, logging
from datetime import datetime, timedelta
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

NEWS_QUERIES = [
    "US stock market economy Federal Reserve inflation earnings",
    "world news geopolitics energy commodities central banks",
    "Taiwan semiconductor supply chain TSMC trade",
]
RSS_BASE = "https://news.google.com/rss/search"

TICKERS = {
    "S&P 500": "^GSPC", 
    "NASDAQ Composite": "^IXIC",
    "Dow Jones": "^DJI", 
    "Philadelphia Semiconductor": "^SOX",
    "VIX": "^VIX", 
    "台積電 ADR": "TSM",
    "輝達 (NVDA)": "NVDA",
    "蘋果 (AAPL)": "AAPL",
    "谷歌 (GOOGL)": "GOOGL",
    "美光 (MU)": "MU",
    "VOO": "VOO",
    "美元指數代理（UUP）": "UUP", 
    "美國 10 年期公債殖利率代理（IEF）": "IEF",
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
    
    # 💡 採用高度精簡、直擊重點的專業操盤手語氣
    prompt = f"""
你是資深操盤手兼財經主筆。現在時間：{now}。
請根據下方資料，產出一份極度簡短、乾淨、適合手機閱讀的晨報。

【寫作與排版規範】
1. 完全不要寫冗長的學術分析，用最白話、俐落的短句。
2. 結構固定為以下 5 大塊：
   - 🎯 市場總結（1-2句講完核心情境）
   - 📊 重點行情（須包含S&P 500、NASDAQ、Dow Jones、費城半導體指數、VIX 恐慌指數、台積電 ADR、MU美光、NVDA輝達、AAPL、GOOGL、VOO ⚠️ 注意：絕對不要使用 Markdown 表格！請改用條列式清單呈現，格式範例：`- 指數名稱：-X.XX%`）
   - 📰 隔夜關鍵大事（條列 3-4 則最重要事件，附上簡短白話解釋）
   - 🇹🇼 台股影響直擊（點出今日台股壓力或機會，含預估點數）
   - 👀 今日盯盤重點（2-3個開盤後必須盯的訊號）
   - 💡 操作建議（一句話總結）

行情資料（Yahoo Finance 日線收盤）：
{json.dumps(market, ensure_ascii=False)}+
        

新聞資料：
{json.dumps(news, ensure_ascii=False)}
"""
    msg = client.messages.create(
        model=MODEL, max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return "\n".join(block.text for block in msg.content if getattr(block, "type", "") == "text")

def markdown_to_html(report):
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
<div style="font-size:13px;opacity:.9;margin-top:6px">極簡、俐落、直擊重點</div></div>
<div style="background:white;padding:22px;border-radius:0 0 16px 16px;box-shadow:0 4px 18px #102a4312">{body}
<hr style="border:0;border-top:1px solid #e5eaf0;margin:24px 0"><p style="font-size:12px;color:#68778a">🤖 AI 輔助研究摘要｜不構成投資建議。</p></div></div></body></html>'''

def inline_format(text):
    text = html.escape(text, quote=True)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\[(.+?)\]\((https?://[^ )]+)\)', r'<a href="\2" style="color:#2563a6">\1</a>', text)
    text = re.sub(r'(https?://[^\s<]+)', r'<a href="\1" style="color:#2563a6">來源連結</a>', text)
    return text

def send_line_message(report):
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_ids_raw = os.getenv("LINE_USER_IDS") or os.getenv("LINE_USER_ID", "")
    user_ids = [uid.strip() for uid in user_ids_raw.split(",") if uid.strip()]
    
    if not token or not user_ids:
        logging.warning("缺少 LINE 設定，跳過 LINE 發送。")
        return

    clean_report = report
    clean_report = re.sub(r'#{1,6}\s*', '', clean_report)
    clean_report = re.sub(r'\*\*(.+?)\*\*', r'\1', clean_report)
    clean_report = re.sub(r'\[(.+?)\]\((https?://[^ )]+)\)', r'\1 (\2)', clean_report)

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    safe_report = clean_report[:4500]
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    
    for user_id in user_ids:
        payload = {
            "to": user_id,
            "messages": [
                {
                    "type": "text",
                    "text": f"🌅 美股晨報 ({today_str})\n━━━━━━━━━━━━━━━\n{safe_report}\n━━━━━━━━━━━━━━━\n💡 完整內容請參閱本地 HTML 檔案。"
                }
            ]
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                logging.info(f"LINE 訊息成功發送給: {user_id}")
            else:
                logging.warning(f"LINE 發送給 {user_id} 失敗: {response.status_code}\n{response.text}")
        except Exception as e:
            logging.warning(f"LINE 發送給 {user_id} 發生例外錯誤: {e}")

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
    
    send_line_message(report)
    logging.info("完成並推播：%s", stamp)
        
if __name__ == "__main__":
    main()