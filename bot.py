#!/usr/bin/env python3
from __future__ import annotations
import hashlib, hmac, json, logging, os, re, sqlite3, threading, time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import feedparser
import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request
sent_titles = set()
load_dotenv()

POLITICAL_KEYWORDS = [
    "fed", "federal reserve", "powell", "interest rate", "rates",
    "inflation", "cpi", "ppi", "jobs report", "unemployment",
    "sec", "regulation", "regulatory", "etf", "bitcoin etf",
    "trump", "biden", "election", "white house", "congress",
    "senate", "treasury", "ecb", "bank of england", "boj",
    "war", "iran", "china", "russia", "ukraine", "tariff",
    "sanctions", "recession", "gdp", "macro"
]

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))
DB_PATH = os.getenv("DB_PATH", "seen_articles.db")
TRADINGVIEW_SECRET = os.getenv("TRADINGVIEW_SECRET", "")
ENABLE_WEBHOOK = os.getenv("ENABLE_WEBHOOK", "true").lower() == "true"
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8000"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RSS_FEEDS = [x.strip() for x in os.getenv("RSS_FEEDS", "").split(",") if x.strip()]
SYMBOLS = [x.strip().upper() for x in os.getenv("SYMBOLS", "BTC,ETH").split(",") if x.strip()]

ENABLE_GROK = os.getenv("ENABLE_GROK", "false").lower() == "true"
XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()
XAI_MODEL = os.getenv("XAI_MODEL", "grok-3-mini").strip()
XAI_BASE_URL = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1").strip()
GROK_TIMEOUT = int(os.getenv("GROK_TIMEOUT", "25"))

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("news_alert_bot")

app = Flask(__name__)

@dataclass
class ArticleSignal:
    title: str
    link: str
    source: str
    published: str
    score: int
    bias: str
    symbol_hits: List[str]
    reasons: List[str]
    summary: str

POSITIVE_KEYWORDS: Dict[str, int] = {
    "etf approved": 5, "approval": 3, "adoption": 3, "partnership": 2,
    "integration": 2, "accumulation": 3, "buyback": 2, "bullish": 2,
    "rate cut": 4, "cuts rates": 4, "lower inflation": 2, "surge in inflows": 4,
}
NEGATIVE_KEYWORDS: Dict[str, int] = {
    "hack": -6, "exploit": -5, "lawsuit": -4, "ban": -5, "banned": -5,
    "rejection": -5, "etf rejected": -6, "liquidation": -4, "outflow": -3,
    "bearish": -2, "rate hike": -4, "raises rates": -4, "hot inflation": -3,
    "sec charges": -5, "insolvency": -6,
}
HIGH_IMPACT_KEYWORDS: Dict[str, int] = {
    "fed": 4, "fomc": 4, "cpi": 4, "inflation": 3, "interest rate": 4,
    "sec": 3, "etf": 3, "whale": 2, "regulation": 3, "tariff": 2,
}
NOISE_PATTERNS = [r"\bprice prediction\b", r"\bop-ed\b", r"\bopinion\b", r"\bsponsored\b"]

def ensure_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("CREATE TABLE IF NOT EXISTS seen (article_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)")
        con.commit()

def make_article_id(link: str, title: str) -> str:
    return hashlib.sha256(f"{link}|{title}".encode("utf-8", errors="ignore")).hexdigest()

def article_seen(article_id: str) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT 1 FROM seen WHERE article_id = ?", (article_id,)).fetchone()
    return row is not None

def mark_seen(article_id: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO seen(article_id, seen_at) VALUES(?, ?)", (article_id, datetime.now(timezone.utc).isoformat()))
        con.commit()

def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()

def detect_symbols(text: str) -> List[str]:
    hits, upper_text = [], text.upper()
    for sym in SYMBOLS:
        if re.search(rf"\b{re.escape(sym)}\b", upper_text):
            hits.append(sym)
    return hits

def score_text(text: str) -> Tuple[int, List[str]]:
    score, reasons = 0, []
    lowered = text.lower()
    for pattern in NOISE_PATTERNS:
        if re.search(pattern, lowered):
            reasons.append("contenu potentiellement peu exploitable")
            score -= 2
    for key, weight in POSITIVE_KEYWORDS.items():
        if key in lowered:
            score += weight
            reasons.append(f"+ {key}")
    for key, weight in NEGATIVE_KEYWORDS.items():
        if key in lowered:
            score += weight
            reasons.append(f"{weight} {key}")
    for key, weight in HIGH_IMPACT_KEYWORDS.items():
        if key in lowered:
            score += weight
            reasons.append(f"impact {key}")
    return score, reasons

def classify_bias(score: int) -> str:
    if score >= 5: return "BULLISH"
    if score <= -5: return "BEARISH"
    return "NEUTRAL"

def has_political_signal(text: str) -> bool:
    text = text.lower()
    return any(keyword in text for keyword in POLITICAL_KEYWORDS)

def summarize(entry) -> Optional[ArticleSignal]:
    title = clean_text(getattr(entry, "title", "Sans titre"))
    link = getattr(entry, "link", "")
    summary = clean_text(getattr(entry, "summary", "") or getattr(entry, "description", ""))
    published = clean_text(getattr(entry, "published", "") or getattr(entry, "updated", ""))
    source = clean_text(getattr(getattr(entry, "source", None), "title", "") or "")
    combined = f"{title}. {summary}"
    symbols = detect_symbols(combined)
    score, reasons = score_text(combined)

    if has_political_signal(combined):
        score += 3
        reasons.append("signal politique / macro détecté")
   
    if symbols:
        score += min(len(symbols), 3)
        reasons.append("symbole surveillé détecté")
    if not reasons:
        return None
    return ArticleSignal(title, link, source or "RSS", published or "date inconnue", score, classify_bias(score), symbols, reasons[:8], summary[:280] if summary else "Résumé indisponible")

def should_alert(signal, grok=None):
    # base score
    if signal.score < 6:
        return False

    # si mode ultra important
    if os.getenv("ULTRA_IMPORTANT_ONLY", "true") == "true":
        if not grok:
            return False

        try:
            importance = int(grok.get("importance", 0))
        except:
            importance = 0

        if importance < 8:
            return False

        if grok and grok.get("action", "ATTENDRE") == "ATTENDRE":
            return False

            return True

def telegram_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram non configuré. Message ignoré:\n%s", text)
        return
    resp = requests.post(telegram_api_url("sendMessage"), json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=20)
    if resp.status_code >= 300:
        logger.error("Erreur Telegram %s: %s", resp.status_code, resp.text)

def analyze_with_grok(signal: ArticleSignal) -> Optional[dict]:
    if not ENABLE_GROK or not XAI_API_KEY:
        return None
    url = f"{XAI_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
Tu es un assistant d'analyse trading prudent.
Réponds UNIQUEMENT en JSON valide avec:
{{
  "sentiment": "BULLISH|BEARISH|NEUTRAL",
  "importance": 1,
  "assets": ["BTC"],
  "action": "ACHAT_POSSIBLE|VENTE_POSSIBLE|ATTENDRE",
  "reason": "explication courte",
  "risk": "risque principal"
}}

Titre: {signal.title}
Résumé: {signal.summary}
Source: {signal.source}
Actifs détectés: {", ".join(signal.symbol_hits) if signal.symbol_hits else "aucun"}
Score interne: {signal.score}
Biais interne: {signal.bias}
""".strip()
    payload = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": "Tu réponds uniquement en JSON valide."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=GROK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        return json.loads(content)
    except Exception as exc:
        logger.error("Erreur Grok/xAI: %s", exc)
        return None

def format_signal(signal: ArticleSignal, grok: Optional[dict] = None) -> str:
    symbol_txt = ", ".join(signal.symbol_hits) if signal.symbol_hits else "macro / général"
    reasons = " | ".join(signal.reasons[:5])
    text = (
        f"🚨 News importante détectée\n"
        f"Titre: {signal.title}\n"
        f"Source: {signal.source}\n"
        f"Date: {signal.published}\n"
        f"Actifs: {symbol_txt}\n"
        f"Score interne: {signal.score} → {signal.bias}\n"
        f"Pourquoi: {reasons}\n"
        f"Résumé: {signal.summary}\n"
    )
    if grok:
        assets = ", ".join(grok.get("assets", [])) or symbol_txt
        text += (
            f"\n🤖 Analyse Grok\n"
            f"Sentiment: {grok.get('sentiment', 'NEUTRAL')}\n"
            f"Importance: {grok.get('importance', '?')}/10\n"
            f"Actifs IA: {assets}\n"
            f"Action: {grok.get('action', 'ATTENDRE')}\n"
            f"Raison: {grok.get('reason', 'non précisée')}\n"
            f"Risque: {grok.get('risk', 'non précisé')}\n"
        )
    text += f"\nLien: {signal.link}\n\nAction finale: confirme toujours avec TradingView avant de trader."
    return text

def fetch_feed(url: str) -> List[ArticleSignal]:
    parsed = feedparser.parse(url)
    out = []
    for entry in parsed.entries:
        signal = summarize(entry)
        if signal:
            out.append(signal)
    return out

def news_loop() -> None:
    logger.info("Boucle news démarrée. %s flux RSS surveillés.", len(RSS_FEEDS))
    send_telegram_message("🧪 TEST NEWS BOT OK")
    while True:
        try:
            for feed_url in RSS_FEEDS:
                for signal in fetch_feed(feed_url):
                    article_id = make_article_id(signal.link, signal.title)
                    if article_seen(article_id):
                        continue
                    
                    mark_seen(article_id)
                    grok = analyze_with_grok(signal)

                    if not should_alert(signal, grok):
                        continue
                    
                    if signal.title in sent_titles:
                        continue

                    sent_titles.add(signal.title)                        
                    send_telegram_message(format_signal(signal, grok))
        
        except Exception as exc:
            logger.exception("Erreur pendant le scan des news: %s", exc)
        
        time.sleep(POLL_SECONDS)

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "news-alert-bot-grok"}), 200

@app.post("/webhook/tradingview")
def tradingview_webhook():
    if not ENABLE_WEBHOOK:
        abort(404)
    if TRADINGVIEW_SECRET:
        provided = request.headers.get("X-Webhook-Secret", "")
        if not hmac.compare_digest(provided, TRADINGVIEW_SECRET):
            abort(401)
    data = request.get_json(silent=True) or {}
    message = data.get("message") or json.dumps(data, ensure_ascii=False)
    send_telegram_message(f"📊 Alerte TradingView\n{message}\n\nRappel: combine le signal technique avec le contexte des news.")
    return jsonify({"ok": True}), 200

def send_test_message() -> None:
    send_telegram_message("✅ Bot opérationnel.\nTu recevras ici:\n- les news filtrées\n- l'analyse Grok si activée\n- les alertes TradingView")

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    ensure_db()
    if args.test:
        send_test_message()
        return
    threading.Thread(target=news_loop, daemon=True).start()
    if ENABLE_WEBHOOK:
        app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT, debug=False)
    else:
        while True:
            time.sleep(3600)

if __name__ == "__main__":
    main()
