"""
Monitor de precios en vivo — Fase 2 del asesor de acciones
=============================================================

Qué hace:
  1. Lee `posiciones.json` — las posiciones abiertas (automáticas: se
     crean solas cuando el analista da señal de COMPRA, ver analista.py).
  2. Para cada posición con estado "abierta", consulta el precio actual
     (1 sola llamada liviana a Alpha Vantage, GLOBAL_QUOTE).
  3. Si el precio tocó el objetivo -> avisa "🎯 Objetivo alcanzado", con el
     % de ganancia, y cierra la posición.
  4. Si el precio tocó el stop-loss -> avisa "🛑 Stop-loss alcanzado", con
     el % de pérdida, y cierra la posición.
  5. Si no pasó nada, no manda alerta (para no saturarte) — solo revisa
     silenciosamente.

Pensado para correr varias veces al día durante el horario de mercado
(cada 1-2 horas), NO en tiempo real — es más liviano que el análisis
diario porque solo consulta 1 precio por posición abierta.
"""

import json
import os
from datetime import datetime, timezone

import requests

ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "TU_API_KEY_AQUI")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TU_BOT_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")

POSICIONES_FILE = "posiciones.json"


def get_price(ticker):
    url = "https://www.alphavantage.co/query"
    params = {"function": "GLOBAL_QUOTE", "symbol": ticker, "apikey": ALPHA_VANTAGE_API_KEY}
    r = requests.get(url, params=params, timeout=15)
    data = r.json().get("Global Quote", {})
    precio = data.get("05. price")
    return float(precio) if precio else None


def send_telegram_alert(message):
    if TELEGRAM_BOT_TOKEN.startswith("TU_"):
        print("[Telegram no configurado — se omite el envío]")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=15)


def load_posiciones():
    if not os.path.exists(POSICIONES_FILE):
        return []
    with open(POSICIONES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_posiciones(posiciones):
    with open(POSICIONES_FILE, "w", encoding="utf-8") as f:
        json.dump(posiciones, f, ensure_ascii=False, indent=2)


def pct_ganancia(entrada, salida):
    """% de ganancia (positivo) o pérdida (negativo) al cerrar una posición."""
    return round((salida - entrada) / entrada * 100, 2)


def run():
    posiciones = load_posiciones()
    abiertas = [p for p in posiciones if p.get("estado") == "abierta"]

    if not abiertas:
        print("No hay posiciones abiertas — nada que monitorear.")
        return

    cambios = False

    for pos in abiertas:
        ticker = pos["ticker"]
        try:
            precio_actual = get_price(ticker)
        except Exception as e:
            print(f"Error consultando precio de {ticker}: {e}")
            continue

        if precio_actual is None:
            print(f"No se pudo obtener precio de {ticker} (N/D)")
            continue

        print(f"{ticker}: precio actual {precio_actual}")

        objetivo = float(pos["precio_objetivo"])
        stop = float(pos["stop_loss"])
        entrada = float(pos["precio_entrada"])

        if precio_actual >= objetivo:
            ganancia = pct_ganancia(entrada, precio_actual)
            pos["estado"] = "cerrada_objetivo"
            pos["precio_cierre"] = precio_actual
            pos["ganancia_pct"] = ganancia
            pos["fecha_cierre"] = datetime.now(timezone.utc).isoformat()
            cambios = True
            send_telegram_alert(
                f"🎯 *{ticker}* — ¡Objetivo alcanzado!\n"
                f"Entrada: {entrada} → Salida: {precio_actual}\n"
                f"*Ganancia: +{ganancia}%*"
            )
        elif precio_actual <= stop:
            perdida = pct_ganancia(entrada, precio_actual)
            pos["estado"] = "cerrada_stop"
            pos["precio_cierre"] = precio_actual
            pos["ganancia_pct"] = perdida
            pos["fecha_cierre"] = datetime.now(timezone.utc).isoformat()
            cambios = True
            send_telegram_alert(
                f"🛑 *{ticker}* — ¡Stop-loss alcanzado!\n"
                f"Entrada: {entrada} → Salida: {precio_actual}\n"
                f"*Pérdida: {perdida}%*"
            )
        else:
            print(f"{ticker}: sin cambios (entre stop {stop} y objetivo {objetivo})")

    if cambios:
        save_posiciones(posiciones)


if __name__ == "__main__":
    run()
