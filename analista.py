"""
Asesor automático de inversión en acciones (mediano/largo plazo: semanas a 4 meses)
=====================================================================================

Qué hace:
  1. Para cada ticker de tu watchlist, descarga datos técnicos y fundamentales
     (Alpha Vantage).
  2. Junta también titulares de noticias recientes.
  3. Le pasa todo eso a Claude (API de Anthropic) con el prompt de "analista
     financiero" para que devuelva una recomendación estructurada:
     COMPRA / ESPERA / VENTA, con rango de entrada, objetivo y stop-loss.
  4. Guarda las recomendaciones y, si corresponde, envía una alerta a Telegram.
  5. (fase 2) Un monitor separado vigila si el precio en vivo toca el
     objetivo o el stop de las posiciones abiertas, y avisa.

Cómo correrlo:
  - Configura las variables de entorno (ver abajo, sección CONFIG).
  - `pip install requests`
  - `python analista.py`

Pensado para correr 1 vez al día (ej. con cron), no en tiempo real —
el horizonte es de semanas/meses, no hace falta más frecuencia.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

# ============================================================
# CONFIG — completa esto con tus propias claves
# ============================================================

ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "TU_API_KEY_AQUI")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "TU_API_KEY_AQUI")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TU_BOT_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")

# Watchlist inicial — acciones populares. Edítala a gusto.
# 5 tickers x 5 llamadas a Alpha Vantage = 25/día, justo en el límite del plan gratuito.
WATCHLIST = [
    "NVDA", "TSLA", "AAPL", "META", "MSFT",
]

# Dónde guardamos el historial de recomendaciones
OUTPUT_FILE = "recomendaciones.jsonl"

ANALYST_PROMPT = """Eres un analista financiero profesional especializado en inversión de mediano/largo plazo (horizonte de semanas a 4 meses).

Analiza la siguiente acción con los datos entregados:

TICKER: {ticker}
Precio actual: {precio}
Medias móviles: MA50={ma50}, MA200={ma200}
RSI (14): {rsi}
Volumen vs promedio: {volumen}
Últimos resultados financieros: {fundamentales}
Próximo earnings: {fecha_earnings}
Noticias recientes (últimas 72h): {noticias}

Con base en análisis técnico Y fundamental combinado, responde SOLO en este JSON, sin texto adicional ni backticks:
{{
  "ticker": "{ticker}",
  "recomendacion": "COMPRA" | "ESPERA" | "VENTA",
  "confianza": "alta" | "media" | "baja",
  "precio_entrada_rango": "ej. 820-835" o null,
  "precio_objetivo": "..." o null,
  "stop_loss": "..." o null,
  "horizonte_estimado": "ej. 6-10 semanas",
  "razonamiento": "explicación breve tipo analista, 3-4 frases"
}}
"""


# ============================================================
# 1. OBTENER DATOS (Alpha Vantage)
# ============================================================

def get_quote(ticker):
    """Precio actual y volumen."""
    url = "https://www.alphavantage.co/query"
    params = {"function": "GLOBAL_QUOTE", "symbol": ticker, "apikey": ALPHA_VANTAGE_API_KEY}
    r = requests.get(url, params=params, timeout=15)
    data = r.json().get("Global Quote", {})
    return {
        "precio": data.get("05. price", "N/D"),
        "volumen": data.get("06. volume", "N/D"),
    }


def get_technical_indicators(ticker):
    """Medias móviles y RSI usando los endpoints técnicos de Alpha Vantage."""
    base = {"apikey": ALPHA_VANTAGE_API_KEY, "symbol": ticker, "interval": "daily"}

    def fetch(function, extra):
        params = {**base, "function": function, **extra}
        r = requests.get("https://www.alphavantage.co/query", params=params, timeout=15)
        return r.json()

    ma50 = fetch("SMA", {"time_period": 50, "series_type": "close"})
    ma200 = fetch("SMA", {"time_period": 200, "series_type": "close"})
    rsi = fetch("RSI", {"time_period": 14, "series_type": "close"})

    def last_value(payload, key):
        series = payload.get(key, {})
        if not series:
            return "N/D"
        latest_date = sorted(series.keys())[-1]
        return list(series[latest_date].values())[0]

    return {
        "ma50": last_value(ma50, "Technical Analysis: SMA"),
        "ma200": last_value(ma200, "Technical Analysis: SMA"),
        "rsi": last_value(rsi, "Technical Analysis: RSI"),
    }


def get_fundamentals(ticker):
    """Resumen fundamental básico (overview de la empresa)."""
    url = "https://www.alphavantage.co/query"
    params = {"function": "OVERVIEW", "symbol": ticker, "apikey": ALPHA_VANTAGE_API_KEY}
    r = requests.get(url, params=params, timeout=15)
    data = r.json()
    return {
        "pe_ratio": data.get("PERatio", "N/D"),
        "profit_margin": data.get("ProfitMargin", "N/D"),
        "revenue_growth_yoy": data.get("QuarterlyRevenueGrowthYOY", "N/D"),
        "next_earnings": data.get("LatestQuarter", "N/D"),
    }


def get_news(ticker):
    """Titulares recientes vía el endpoint de noticias/sentimiento de Alpha Vantage."""
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": ticker,
        "apikey": ALPHA_VANTAGE_API_KEY,
        "limit": 5,
    }
    r = requests.get(url, params=params, timeout=15)
    data = r.json().get("feed", [])
    return [item.get("title", "") for item in data[:5]]


# ============================================================
# 2. LLAMAR AL "CEREBRO" ANALISTA (API de Anthropic)
# ============================================================

def ask_analyst(ticker, quote, tech, fundamentals, news):
    prompt = ANALYST_PROMPT.format(
        ticker=ticker,
        precio=quote["precio"],
        ma50=tech["ma50"],
        ma200=tech["ma200"],
        rsi=tech["rsi"],
        volumen=quote["volumen"],
        fundamentales=json.dumps(fundamentals, ensure_ascii=False),
        fecha_earnings=fundamentals.get("next_earnings", "N/D"),
        noticias=" | ".join(news) if news else "Sin noticias relevantes recientes",
    )

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    response.raise_for_status()
    text = response.json()["content"][0]["text"]
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


# ============================================================
# 3. ALERTA POR TELEGRAM
# ============================================================

def send_telegram_alert(message):
    if TELEGRAM_BOT_TOKEN.startswith("TU_"):
        print("[Telegram no configurado — se omite el envío]")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=15)


def format_alert(analysis):
    emoji = {"COMPRA": "🟢", "ESPERA": "🟡", "VENTA": "🔴"}.get(analysis["recomendacion"], "⚪")
    lines = [
        f"{emoji} *{analysis['ticker']}* — {analysis['recomendacion']} (confianza: {analysis['confianza']})",
        f"Horizonte: {analysis['horizonte_estimado']}",
    ]
    if analysis.get("precio_entrada_rango"):
        lines.append(f"Entrada: {analysis['precio_entrada_rango']}")
    if analysis.get("precio_objetivo"):
        lines.append(f"Objetivo: {analysis['precio_objetivo']}")
    if analysis.get("stop_loss"):
        lines.append(f"Stop-loss: {analysis['stop_loss']}")
    lines.append(f"\n{analysis['razonamiento']}")
    return "\n".join(lines)


# ============================================================
# 4. PIPELINE PRINCIPAL
# ============================================================

def analyze_ticker(ticker):
    print(f"Analizando {ticker}...")
    quote = get_quote(ticker)
    tech = get_technical_indicators(ticker)
    fundamentals = get_fundamentals(ticker)
    news = get_news(ticker)
    analysis = ask_analyst(ticker, quote, tech, fundamentals, news)
    analysis["timestamp"] = datetime.now(timezone.utc).isoformat()
    return analysis


def save_analysis(analysis):
    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(analysis, ensure_ascii=False) + "\n")


def run():
    for ticker in WATCHLIST:
        try:
            analysis = analyze_ticker(ticker)
            save_analysis(analysis)

            if analysis["recomendacion"] in ("COMPRA", "VENTA"):
                send_telegram_alert(format_alert(analysis))

            print(json.dumps(analysis, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"Error analizando {ticker}: {e}")

        time.sleep(15)  # respeta límites de rate de las APIs gratuitas


if __name__ == "__main__":
    run()
