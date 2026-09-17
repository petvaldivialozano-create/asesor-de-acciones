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
# Watchlist inicial — acciones populares. Edítala a gusto.
# 4 tickers x 5 llamadas a Alpha Vantage = 20/día, dejando margen para
# que el monitor de precios (fase 2) pueda revisar posiciones abiertas
# sin quedarse sin cupo (25/día total en el plan gratuito).
WATCHLIST = [
    "NVDA", "TSLA", "AAPL", "META",
]

# Dónde guardamos el historial de recomendaciones
OUTPUT_FILE = "recomendaciones.jsonl"
POSICIONES_FILE = "posiciones.json"

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


def parse_price(valor):
    """Convierte '205-215' -> 210.0 (punto medio), o '210' -> 210.0. None si no hay dato."""
    if not valor:
        return None
    valor = str(valor).strip()
    if "-" in valor:
        try:
            a, b = valor.split("-")
            return (float(a) + float(b)) / 2
        except ValueError:
            return None
    try:
        return float(valor)
    except ValueError:
        return None


def load_posiciones():
    if not os.path.exists(POSICIONES_FILE):
        return []
    with open(POSICIONES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_posiciones(posiciones):
    with open(POSICIONES_FILE, "w", encoding="utf-8") as f:
        json.dump(posiciones, f, ensure_ascii=False, indent=2)


def pct_ganancia(entrada, salida):
    return round((salida - entrada) / entrada * 100, 2)


def actualizar_posiciones(analysis, posiciones):
    """El sistema actúa como si TÚ hubieras comprado: abre la posición sola
    en COMPRA, y la cierra sola si luego llega una señal de VENTA sobre el
    mismo ticker. Devuelve True si hubo cambios que guardar."""
    ticker = analysis["ticker"]
    recomendacion = analysis.get("recomendacion")
    cambios = False

    abierta = next(
        (p for p in posiciones if p["ticker"] == ticker and p.get("estado") == "abierta"), None
    )

    if recomendacion == "COMPRA" and not abierta:
        entrada = parse_price(analysis.get("precio_entrada_rango"))
        objetivo = parse_price(analysis.get("precio_objetivo"))
        stop = parse_price(analysis.get("stop_loss"))
        if entrada is not None and objetivo is not None and stop is not None:
            posiciones.append({
                "ticker": ticker,
                "precio_entrada": entrada,
                "precio_objetivo": objetivo,
                "stop_loss": stop,
                "fecha_compra": datetime.now(timezone.utc).isoformat(),
                "estado": "abierta",
                "origen": "automática (señal COMPRA del analista)",
            })
            cambios = True
            print(f"→ Posición ABIERTA automáticamente en {ticker} @ {entrada}")

    elif recomendacion == "VENTA" and abierta:
        precio_actual = parse_price(analysis.get("precio_entrada_rango")) or abierta["precio_entrada"]
        ganancia = pct_ganancia(abierta["precio_entrada"], precio_actual)
        abierta["estado"] = "cerrada_señal_venta"
        abierta["precio_cierre"] = precio_actual
        abierta["ganancia_pct"] = ganancia
        abierta["fecha_cierre"] = datetime.now(timezone.utc).isoformat()
        cambios = True
        print(f"→ Posición CERRADA en {ticker} por señal de VENTA ({ganancia}%)")

    return cambios


def run():
    resultados = []
    posiciones = load_posiciones()
    cambios_posiciones = False

    for ticker in WATCHLIST:
        try:
            analysis = analyze_ticker(ticker)
            save_analysis(analysis)
            resultados.append(analysis)

            if actualizar_posiciones(analysis, posiciones):
                cambios_posiciones = True

            print(json.dumps(analysis, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"Error analizando {ticker}: {e}")
            resultados.append({"ticker": ticker, "recomendacion": "ERROR", "error": str(e)})

        time.sleep(15)  # respeta límites de rate de las APIs gratuitas

    if cambios_posiciones:
        save_posiciones(posiciones)

    enviar_resumen_diario(resultados, posiciones)


def enviar_resumen_diario(resultados, posiciones):
    """Envía SIEMPRE un mensaje a Telegram: con las señales de COMPRA/VENTA
    (y el estado de las posiciones automáticas) si las hay, o confirmando
    que no hay novedades hoy si todo salió en ESPERA."""
    accionables = [a for a in resultados if a.get("recomendacion") in ("COMPRA", "VENTA")]
    fecha = datetime.now(timezone.utc).strftime("%d-%m-%Y")

    if accionables:
        partes = [f"📊 *Análisis del {fecha}*\n"]
        for analysis in accionables:
            partes.append(format_alert(analysis))
        abiertas_hoy = [p for p in posiciones if p.get("estado") == "abierta"]
        cerradas_hoy = [p for p in posiciones if str(p.get("fecha_cierre", "")).startswith(
            datetime.now(timezone.utc).strftime("%Y-%m-%d"))]
        if abiertas_hoy:
            partes.append("📌 Posiciones abiertas: " + ", ".join(
                f"{p['ticker']} @ {p['precio_entrada']}" for p in abiertas_hoy))
        if cerradas_hoy:
            partes.append("✅ Cerradas hoy: " + ", ".join(
                f"{p['ticker']} ({p.get('ganancia_pct', 0):+}%)" for p in cerradas_hoy))
        mensaje = "\n\n".join(partes)
    else:
        tickers_ok = [a["ticker"] for a in resultados if a.get("recomendacion") == "ESPERA"]
        tickers_error = [a["ticker"] for a in resultados if a.get("recomendacion") == "ERROR"]
        mensaje = f"📊 *Análisis del {fecha}*\n\n✅ No hay entradas ni salidas para hoy."
        if tickers_ok:
            mensaje += f"\nRevisadas en ESPERA: {', '.join(tickers_ok)}"
        if tickers_error:
            mensaje += f"\n⚠️ No se pudieron analizar: {', '.join(tickers_error)}"

    send_telegram_alert(mensaje)


if __name__ == "__main__":
    run()
