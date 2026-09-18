"""
leon_gas.py — un solo archivo: baja los precios de gasolina y dibuja el tablero de León.

    python leon_gas.py todo        # baja + dibuja + abre el navegador   <- lo normal
    python leon_gas.py bajar       # solo baja un snapshot (esto va en el cron)
    python leon_gas.py tablero     # solo redibuja con lo que ya tengas en disco

Sin argumentos hace "todo".

La fuente (CNE, antes CRE) publica únicamente el precio VIGENTE, no el histórico.
La serie de tiempo se construye acumulando snapshots: corre "bajar" varias veces
al día y con las semanas tendrás un panel que no existe en ningún otro lado.
Los cortes oficiales son a las 1:00, 6:00, 8:00, 11:00, 15:00 y 19:00.

Dependencias:  pip install requests pandas pyarrow
(el mapa usa Leaflet desde CDN; si no hay internet, el resto del tablero igual sirve)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

PLACES_URL = "https://publicacionexterna.azurewebsites.net/publicaciones/places"
PRICES_URL = "https://publicacionexterna.azurewebsites.net/publicaciones/prices"

# Caja alrededor del municipio de León. Recórtala luego con el polígono real
# (KMZ "Límite Urbano" / "Delegaciones" de plataformaleon.gob.mx).
BBOX = {"lon_min": -101.95, "lon_max": -101.30, "lat_min": 20.80, "lat_max": 21.35}

FUELS = ["regular", "premium", "diesel"]
FUEL_LABEL = {"regular": "Magna", "premium": "Premium", "diesel": "Diésel"}
HEADERS = {"User-Agent": "leon-gas-panel/1.0 (research)"}


# ====================================================================== BAJAR

def _tag(t: str) -> str:
    return t.split("}", 1)[-1] if "}" in t else t


def _parse_places(raw: bytes) -> pd.DataFrame:
    rows = []
    for p in ET.fromstring(raw).iter():
        if _tag(p.tag) != "place":
            continue
        rec = {"place_id": p.get("place_id")}
        for el in p.iter():
            k = _tag(el.tag)
            if k in ("name", "cre_id", "x", "y") and el.text:
                rec[k] = el.text.strip()
        rows.append(rec)
    df = pd.DataFrame(rows).rename(columns={"x": "lon", "y": "lat"})
    for c in ("lon", "lat"):
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    return df


def _parse_prices(raw: bytes) -> pd.DataFrame:
    rows = []
    for p in ET.fromstring(raw).iter():
        if _tag(p.tag) != "place":
            continue
        for el in p.iter():
            if _tag(el.tag) == "gas_price":
                rows.append({
                    "place_id": p.get("place_id"),
                    "fuel": (el.get("type") or "").strip().lower(),
                    "price": pd.to_numeric(el.text, errors="coerce"),
                })
    return pd.DataFrame(rows)


def bajar(out_root: Path, nacional: bool = False, inspeccionar: bool = False) -> Path | None:
    import requests  # solo hace falta aquí

    ts = dt.datetime.now().astimezone()
    print(f"[{ts:%H:%M:%S}] descargando de la CNE...")

    def get(url):
        r = requests.get(url, headers=HEADERS, timeout=180)
        r.raise_for_status()
        return r.content

    try:
        places_raw, prices_raw = get(PLACES_URL), get(PRICES_URL)
    except Exception as e:
        print(f"  !! falló la descarga: {e}")
        return None
    print(f"  places {len(places_raw)/1e6:.1f} MB · prices {len(prices_raw)/1e6:.1f} MB")

    if inspeccionar:
        for nombre, raw in (("places", places_raw), ("prices", prices_raw)):
            root = ET.fromstring(raw)
            print(f"\n--- {nombre}: raíz <{_tag(root.tag)}> ---")
            for i, hijo in enumerate(root):
                if i >= 2:
                    break
                print(ET.tostring(hijo, encoding="unicode")[:700])
        return None

    places, prices = _parse_places(places_raw), _parse_prices(prices_raw)
    print(f"  catálogo nacional: {len(places):,} estaciones")
    if places.empty or prices.empty:
        print("  !! parseo vacío — corre 'bajar --inspeccionar', cambió el esquema XML")
        return None

    df = prices.merge(places, on="place_id", how="left").dropna(subset=["lat", "lon", "price"])
    if not nacional:
        df = df[df.lon.between(BBOX["lon_min"], BBOX["lon_max"])
                & df.lat.between(BBOX["lat_min"], BBOX["lat_max"])].copy()

    df["ts"] = ts.isoformat()
    scope = "nacional" if nacional else "leon"
    print(f"  {scope}: {df.place_id.nunique():,} estaciones, {len(df):,} filas precio-combustible")

    destino = out_root / scope / f"fecha={ts.date().isoformat()}"
    destino.mkdir(parents=True, exist_ok=True)
    ruta = destino / f"snapshot_{ts:%H%M%S}.parquet"
    df.to_parquet(ruta, index=False)
    print(f"  guardado -> {ruta}")
    return ruta


# ==================================================================== ANALIZAR

def _cargar(data_dir: Path) -> pd.DataFrame:
    archivos = sorted(data_dir.rglob("*.parquet"))
    if not archivos:
        raise SystemExit(
            f"No encontré ningún .parquet bajo {data_dir.resolve()}\n"
            f"Corre primero:  python {Path(sys.argv[0]).name} bajar"
        )
    df = pd.concat((pd.read_parquet(f) for f in archivos), ignore_index=True)
    print(f"leídos {len(archivos)} snapshots, {len(df):,} filas")

    df = df.dropna(subset=["price", "lat", "lon"])
    df = df[df.price > 0]
    df["ts"] = pd.to_datetime(df["ts"], format="mixed", utc=True)
    df = df.sort_values("ts").drop_duplicates(["place_id", "fuel"], keep="last")
    df["name"] = df["name"].fillna("Sin nombre").astype(str).str.strip()
    return df


def _km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _ahorros(g: pd.DataFrame, radio, tanque, rend):
    """Para cada estación, la mejor alternativa cercana y el ahorro NETO:

        (p_aquí − p_allá) × tanque  −  (2 × km ÷ rendimiento) × p_allá

    El segundo término es la gasolina que quemas yendo y volviendo del desvío.
    """
    recs = g.to_dict("records")
    lats = [r["lat"] for r in recs]
    lons = [r["lon"] for r in recs]
    pr = [r["price"] for r in recs]
    salida = []

    for i in range(len(recs)):
        mejor = None
        for j in range(len(recs)):
            if i == j or pr[j] >= pr[i]:
                continue
            d = _km(lats[i], lons[i], lats[j], lons[j])
            if d > radio:
                continue
            neto = (pr[i] - pr[j]) * tanque - (2 * d / rend) * pr[j]
            if mejor is None or neto > mejor["ahorro"]:
                mejor = {"ahorro": round(neto, 1), "km": round(d, 2),
                         "alt": recs[j]["name"], "alt_precio": round(pr[j], 2)}
        r = recs[i]
        salida.append({
            "id": r["place_id"], "nombre": r["name"],
            "lat": round(r["lat"], 5), "lon": round(r["lon"], 5),
            "precio": round(r["price"], 2),
            "mejor": mejor if mejor and mejor["ahorro"] > 0 else None,
        })
    return salida


def _payload(df: pd.DataFrame, radio, tanque, rend, cercanas) -> dict:
    out = {
        "corte": df["ts"].max().tz_convert("America/Mexico_City").strftime("%Y-%m-%d %H:%M"),
        "params": {"radio": radio, "tanque": tanque, "rendimiento": rend,
                   "cercanas": cercanas},
        "fuels": {},
    }
    for fuel in FUELS:
        g = df[df.fuel == fuel]
        if len(g) < 3:
            continue
        est = _ahorros(g, radio, tanque, rend)
        precios = sorted(e["precio"] for e in est)
        mediana = precios[len(precios) // 2]
        for e in est:
            e["vs_mediana"] = round(mediana - e["precio"], 2)  # + = más barata
        out["fuels"][fuel] = {"label": FUEL_LABEL[fuel], "estaciones": est,
                              "min": precios[0], "max": precios[-1],
                              "mediana": round(mediana, 2), "n": len(est)}
        print(f"  {FUEL_LABEL[fuel]}: {len(est)} estaciones, "
              f"${precios[0]:.2f}–${precios[-1]:.2f} (mediana ${mediana:.2f})")
    if not out["fuels"]:
        raise SystemExit("No hubo suficientes estaciones por combustible.")
    return out


# ======================================================================= HTML

HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Gasolina en León</title>
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#2a78d6">
<meta name="description" content="Dónde está más barata la gasolina en León hoy, y cuánto te queda en la bolsa si te mueves.">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Gasolina León">
<link rel="apple-touch-icon" href="icon-192.png">
<link rel="icon" href="icon-192.png">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  :root {
    color-scheme: light;
    --surface-1:#fcfcfb; --plane:#f9f9f7;
    --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
    --s0:#86b6ef; --s1:#3987e5; --s2:#256abf; --s3:#104281; --ring:#fcfcfb;
    /* Rampa del MAPA: diverging barato <-> caro. No cambia con el tema, porque
       el mapa siempre es claro y los puntos se leen contra el tile.
       Azul (barata) <-> rojo (cara): bajo daltonismo se separan a ΔE 23.8,
       contra 8.4 de un verde azulado y 1.0 de un verde puro — que sería
       literalmente el mismo color para ~8% de los hombres. */
    --barato:#2a78d6; --neutro:#dcdad3; --caro:#d03b3b; --mring:#ffffff;
    --apagado:#a8a69e;   /* las que quedan fuera del radio de interés */
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface-1:#1a1a19; --plane:#0d0d0d;
      --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
      --s0:#184f95; --s1:#256abf; --s2:#3987e5; --s3:#86b6ef; --ring:#1a1a19;
    }
    /* El mapa sigue siendo claro; solo lo bajamos de brillo para que no deslumbre. */
    :root:not([data-theme="light"]) .leaflet-tile-pane { filter:brightness(0.88) saturate(0.9); }
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:24px 16px 56px; background:var(--plane); color:var(--text-primary);
         font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width:1080px; margin:0 auto; }
  h1 { font-size:1.5rem; margin:0 0 4px; letter-spacing:-0.01em; }
  .sub { color:var(--text-secondary); font-size:0.875rem; margin:0 0 20px; }
  .filters { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:20px; }
  .filters button { font:inherit; font-size:0.875rem; padding:7px 14px; cursor:pointer;
    background:var(--surface-1); color:var(--text-secondary);
    border:1px solid var(--border); border-radius:999px; }
  .filters button[aria-pressed="true"] { background:var(--s2); color:#fff; border-color:transparent; font-weight:600; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:20px; }
  .kpi { background:var(--surface-1); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
  .kpi .k { font-size:0.75rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; }
  .kpi .v { font-size:1.6rem; margin-top:4px; line-height:1.1; }
  .kpi .n { font-size:0.8rem; color:var(--text-secondary); margin-top:3px;
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .card { background:var(--surface-1); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:20px; }
  .card h2 { font-size:1rem; margin:0 0 2px; }
  .card p.hint { font-size:0.8125rem; color:var(--text-secondary); margin:0 0 14px; }
  #map { height:520px; border-radius:8px; z-index:0; background:#eceae4; }
  .leaflet-container { background:#eceae4; }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) #map,
    :root:not([data-theme="light"]) .leaflet-container { background:#26262a; }
  }
  .legend { display:flex; align-items:center; gap:8px; margin-top:12px; font-size:0.75rem; color:var(--text-secondary); }
  .legend .ramp { flex:0 1 220px; height:10px; border-radius:2px;
    background:linear-gradient(90deg,var(--barato),var(--neutro),var(--caro)); }
  /* Etiqueta de precio pegada a cada punto */
  .leaflet-tooltip.precio-tag {
    background:rgba(255,255,255,0.92); border:none; box-shadow:none; color:#0b0b0b;
    font-size:11px; font-weight:700; font-variant-numeric:tabular-nums;
    padding:1px 4px; border-radius:3px; margin-left:2px;
  }
  .leaflet-tooltip.precio-tag::before { display:none; }
  .geobar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:14px; }
  .geobar button { font:inherit; font-size:0.875rem; font-weight:600; padding:8px 16px; cursor:pointer;
    background:var(--s2); color:#fff; border:none; border-radius:8px; }
  .geobar span { font-size:0.8125rem; color:var(--text-secondary); }
  .yo { font-weight:700; }
  table { width:100%; border-collapse:collapse; font-size:0.875rem; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--grid); }
  th { font-size:0.75rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; font-weight:600; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  tbody tr:last-child td { border-bottom:none; }
  .pop { font-family:system-ui,sans-serif; font-size:0.8125rem; line-height:1.45; }
  .pop b { display:block; font-size:0.9rem; margin-bottom:2px; }
  .pop .big { font-size:1.15rem; font-variant-numeric:tabular-nums; }
  .pop .save { margin-top:6px; padding-top:6px; border-top:1px solid #ddd; }
  .pop .recibo { width:100%; margin-top:6px; font-variant-numeric:tabular-nums; border-collapse:collapse; }
  .pop .recibo td { padding:1px 0; border:none; font-size:0.78rem; }
  .pop .recibo td:last-child { text-align:right; padding-left:10px; }
  .pop .recibo .tot td { border-top:1px solid #ccc; padding-top:3px; font-weight:700; }

  /* ----- teléfono ----- */
  .tablaScroll { overflow-x:auto; -webkit-overflow-scrolling:touch; }
  .tablaScroll table { min-width:440px; }
  .desliza { display:none; font-size:0.75rem; color:var(--muted); margin:6px 0 0; }
  @media (max-width: 620px) {
    body { padding:16px 12px 40px; }
    .wrap { max-width:100%; }
    h1 { font-size:1.25rem; }
    .kpis { grid-template-columns:1fr 1fr; gap:8px; }
    .kpi { padding:10px 12px; }
    .kpi .v { font-size:1.25rem; }
    .card { padding:12px; margin-bottom:14px; }
    #map { height:62vh; min-height:340px; }
    .geobar button { width:100%; padding:12px 16px; font-size:1rem; }
    .filters button { flex:1 1 auto; text-align:center; }
    th, td { padding:7px 8px; }
    .desliza { display:block; }
  }
  @supports (padding: max(0px)) {  /* muescas y barras de los teléfonos */
    body { padding-left:max(12px, env(safe-area-inset-left));
           padding-right:max(12px, env(safe-area-inset-right));
           padding-bottom:max(40px, env(safe-area-inset-bottom)); }
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Gasolina en León</h1>
  <p class="sub" id="sub"></p>

  <div class="filters" id="fuelFilter" role="group" aria-label="Combustible"></div>
  <div class="kpis" id="kpis"></div>

  <div class="card">
    <h2>Dónde está barata hoy</h2>
    <p class="hint">Entre más intenso el punto, más barata contra la mediana de la ciudad. Haz clic en una estación para ver a dónde te conviene moverte.</p>
    <div class="geobar">
      <button id="btnGeo" type="button">Usar mi ubicación</button>
      <span id="geoMsg">Te marco las que te van quedando más cerca y más baratas.</span>
    </div>
    <div id="map"></div>
    <div class="legend">
      <span id="legMin">La más barata</span>
      <span class="ramp"></span>
      <span id="legMax">La más cara</span>
    </div>
  </div>

  <div class="card" id="cardCerca" hidden>
    <h2>Desde donde estás</h2>
    <p class="hint">Cada renglón es una gasolinera que, respecto a todas las que quedan más cerca, es más barata que cualquiera de ellas. Si una no aparece, es porque hay otra más cerca <em>y</em> más barata — no tiene caso ir. La última columna compara contra quedarte en la que ya tienes más cerca: lo que pagarías de más ahí por una llenada, menos la gasolina que quemas en ir y volver.</p>
    <div class="tablaScroll"><table>
      <thead><tr>
        <th>Estación</th><th class="num">Distancia</th><th class="num">Precio</th>
        <th class="num">Te queda en la bolsa</th>
      </tr></thead>
      <tbody id="tcerca"></tbody>
    </table></div>
    <p class="desliza">Desliza la tabla de lado para ver todas las columnas →</p>
  </div>

  <div class="card">
    <h2>Qué tan dispersos están los precios</h2>
    <p class="hint">Cada barra es el número de gasolineras en ese rango de precio. La línea marca la mediana.</p>
    <div id="hist"></div>
  </div>

  <div class="card">
    <h2>Dónde más te conviene moverte</h2>
    <p class="hint">Si sueles cargar en la estación de la izquierda, esto es lo que te queda en la bolsa por llenar el mismo tanque en la de la derecha — ya restada la gasolina que quemas en ir y volver. No es un ahorro contra "lo normal": es contra esa estación en concreto, en una sola llenada.</p>
    <div class="tablaScroll"><table>
      <thead><tr>
        <th>Si cargas en</th><th class="num">Pagas</th>
        <th>Muévete a</th><th class="num">Paga</th>
        <th class="num">km</th><th class="num">Ahorras</th>
      </tr></thead>
      <tbody id="tsave"></tbody>
    </table></div>
    <p class="desliza">Desliza la tabla de lado para ver todas las columnas →</p>
  </div>

  <div class="card">
    <h2>Las 15 más baratas de hoy</h2>
    <p class="hint">Ranking directo por precio de litro, sin considerar distancia.</p>
    <div class="tablaScroll"><table>
      <thead><tr><th>#</th><th>Estación</th><th class="num">Precio</th><th class="num">vs mediana</th></tr></thead>
      <tbody id="tbody"></tbody>
    </table></div>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// Los datos vienen embebidos (modo local) o de datos.json (modo publicado).
// En el publicado, la página nunca cambia y solo se reemplaza el JSON: por eso
// el sitio se puede actualizar solo, sin volver a generar nada más.
const DATA_EMBEBIDA = __PAYLOAD__;
let DATA = null;

const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const mx  = n => "$" + n.toFixed(2);
const mxr = n => "$" + Math.round(n).toLocaleString("es-MX");

// Si el CDN de Leaflet no cargó, el resto del tablero debe seguir sirviendo.
const HAS_MAP = typeof L !== "undefined";
let map = null;
if (HAS_MAP) {
  map = L.map("map", { scrollWheelZoom:false });

  // Varios fondos, porque ninguno es confiable al 100%:
  //  - CARTO empezó a exigir API key (marca de agua "API KEY REQUIRED").
  //  - OpenStreetMap bloquea con 403 lo que no manda Referer, y un archivo
  //    abierto con file:// no manda ninguno. Sírvelo por HTTP y sí funciona:
  //        python -m http.server 8000
  //    y abre http://localhost:8000/leon_hoy.html
  //  - "Sin fondo" siempre funciona: los puntos solos ya dibujan la ciudad.
  const esriAttr = 'Tiles &copy; Esri';
  const fondos = {
    "Calles (Esri)": L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, attribution: esriAttr }),
    "Gris claro (Esri)": L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 16, attribution: esriAttr }),
    "OpenStreetMap": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' }),
    "Sin fondo": L.tileLayer(""),
  };
  // Si un tile viene con error (403, bloqueo, etc.) lo escondemos en vez de
  // tapizar el mapa con la imagen de "Access blocked" del proveedor.
  Object.values(fondos).forEach(capa =>
    capa.on("tileerror", ev => { if (ev.tile) ev.tile.style.visibility = "hidden"; }));

  fondos["Calles (Esri)"].addTo(map);
  L.control.layers(fondos, null, { position: "topright", collapsed: false }).addTo(map);
} else {
  const el = document.getElementById("map");
  el.style.cssText = "height:auto;padding:24px;border:1px dashed var(--axis);border-radius:8px;color:var(--text-secondary);font-size:0.875rem";
  el.textContent = "No se pudo cargar Leaflet desde el CDN, así que el mapa no se dibujó. El resto del tablero sí funciona.";
}

let layer = null, marcadores = [], yoMarker = null, miPos = null;
let current = null;
if (HAS_MAP) map.on("zoomend", etiquetas);

// --- color: degradado continuo entre la más barata y la más cara del día ----
const hex2rgb = h => [1,3,5].map(i => parseInt(h.slice(i, i+2), 16));
const mix = (a, b, t) => a.map((v,i) => Math.round(v + (b[i]-v)*t));
const rgb = c => `rgb(${c[0]},${c[1]},${c[2]})`;

function colorFor(precio, min, max) {
  const A = hex2rgb(css("--barato")), M = hex2rgb(css("--neutro")), B = hex2rgb(css("--caro"));
  const t = max > min ? (precio - min) / (max - min) : 0.5;   // 0 = la más barata
  return t <= 0.5 ? rgb(mix(A, M, t * 2)) : rgb(mix(M, B, (t - 0.5) * 2));
}

const km = (aLat, aLon, bLat, bLon) => {           // haversine, igual que en Python
  const R = 6371, r = Math.PI/180;
  const p1 = aLat*r, p2 = bLat*r;
  const h = Math.sin((p2-p1)/2)**2 + Math.cos(p1)*Math.cos(p2)*Math.sin((bLon-aLon)*r/2)**2;
  return 2*R*Math.asin(Math.sqrt(h));
};

function render(fuel) {
  current = fuel;
  const d = DATA.fuels[fuel], est = d.estaciones;
  const barata = est.reduce((a,b) => b.precio < a.precio ? b : a);
  const cara   = est.reduce((a,b) => b.precio > a.precio ? b : a);
  const mejor  = est.filter(e => e.mejor).sort((a,b) => b.mejor.ahorro - a.mejor.ahorro)[0];

  document.querySelectorAll("#fuelFilter button").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.fuel === fuel)));

  document.getElementById("kpis").innerHTML = [
    ["Más barata", mx(barata.precio), barata.nombre],
    ["Mediana de la ciudad", mx(d.mediana), d.n + " estaciones"],
    ["Más cara", mx(cara.precio), cara.nombre],
    ["Dispersión", mx(d.max - d.min) + " /L", mxr((d.max - d.min) * DATA.params.tanque) + " por tanque"],
    ["Mayor ahorro con desvío", mejor ? mxr(mejor.mejor.ahorro) : "—",
      mejor ? "si cargas en " + mejor.nombre : "sin alternativa cercana"],
  ].map(([k,v,n]) => `<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join("");

  const v = miPos ? vecindario(d) : null;
  document.getElementById("legMin").textContent =
    (v ? "La más barata de tus " + DATA.params.cercanas + " más cercanas " : "La más barata ") + mx(v ? v.min : d.min);
  document.getElementById("legMax").textContent =
    mx(v ? v.max : d.max) + (v ? " la más cara de esas" : " la más cara");

  if (HAS_MAP) drawMap(d);
  hist(est.map(e => e.precio), d.mediana);
  if (miPos) cercanas(d);

  document.getElementById("tsave").innerHTML = est
    .filter(e => e.mejor).sort((a,b) => b.mejor.ahorro - a.mejor.ahorro).slice(0,15)
    .map(e => `<tr>
      <td>${e.nombre}</td><td class="num">${mx(e.precio)}</td>
      <td>${e.mejor.alt}</td><td class="num">${mx(e.mejor.alt_precio)}</td>
      <td class="num">${e.mejor.km.toFixed(1)}</td>
      <td class="num"><b>${mxr(e.mejor.ahorro)}</b></td>
    </tr>`).join("") || `<tr><td colspan="6">Sin alternativas dentro de ${DATA.params.radio} km.</td></tr>`;

  document.getElementById("tbody").innerHTML = est
    .slice().sort((a,b) => a.precio - b.precio).slice(0,15)
    .map((e,i) => `<tr>
      <td class="num">${i+1}</td><td>${e.nombre}</td><td class="num">${mx(e.precio)}</td>
      <td class="num">${e.vs_mediana > 0 ? "−" + e.vs_mediana.toFixed(2) : "+" + (-e.vs_mediana).toFixed(2)}</td>
    </tr>`).join("");
}

// Las N más cercanas a ti. Sin ubicación, no hay "cercanas".
function vecindario(d) {
  if (!miPos) return null;
  const orden = d.estaciones
    .map(e => ({ e, dist: km(miPos.lat, miPos.lon, e.lat, e.lon) }))
    .sort((a,b) => a.dist - b.dist)
    .slice(0, DATA.params.cercanas);
  const precios = orden.map(o => o.e.precio);
  return {
    ids: new Set(orden.map(o => o.e.id)),
    min: Math.min(...precios), max: Math.max(...precios),
    dists: new Map(orden.map(o => [o.e.id, o.dist])),
  };
}

// El desglose completo de una llenada, para que "ahorras $X" no sea un número
// que cae del cielo: es lo que dejas de pagar por llenar el mismo tanque allá,
// ya restando la gasolina que quemas en el desvío.
function recibo(e) {
  const T = DATA.params.tanque, m = e.mejor;
  const aqui = Math.round(e.precio * T);
  const alla = Math.round(m.alt_precio * T);
  const desvio = Math.round((2 * m.km / DATA.params.rendimiento) * m.alt_precio);
  // El total se saca de los mismos números redondeados que ves, para que la
  // resta cuadre en pantalla y no sobre o falte un peso.
  return `<div class="save">
    <b style="display:inline">Te conviene ir a ${m.alt}</b>, a ${m.km} km
    <table class="recibo">
      <tr><td>Llenar ${T} L aquí</td><td>${mxr(aqui)}</td></tr>
      <tr><td>Llenar ${T} L allá</td><td>−${mxr(alla)}</td></tr>
      <tr><td>Gasolina del desvío (${(2*m.km).toFixed(1)} km)</td><td>−${mxr(desvio)}</td></tr>
      <tr class="tot"><td>Te queda en la bolsa</td><td>${mxr(aqui - alla - desvio)}</td></tr>
    </table></div>`;
}

function drawMap(d) {
  const est = d.estaciones;
  if (layer) map.removeLayer(layer);

  const v = vecindario(d);
  // La escala de color: si ya sé dónde estás, se calcula SOLO entre las que
  // tienes cerca — así el azul es "la más barata de las que te sirven", no la
  // más barata de un León que no vas a cruzar. Sin ubicación, toda la ciudad.
  const lo = v ? v.min : d.min, hi = v ? v.max : d.max;

  // Etiqueta siempre visible: las cercanas si hay ubicación, si no las 6 más baratas.
  const fijas = v ? v.ids
    : new Set(est.slice().sort((a,b) => a.precio - b.precio).slice(0,6).map(e => e.id));
  marcadores = [];

  layer = L.layerGroup(est.map(e => {
    const dentro = !v || v.ids.has(e.id);
    const m = L.circleMarker([e.lat, e.lon], {
      radius: dentro ? 7 : 4, weight: dentro ? 2 : 1, color: css("--mring"),
      fillColor: dentro ? colorFor(e.precio, lo, hi) : css("--apagado"),
      fillOpacity: dentro ? 1 : 0.5,
    });
    m.bindTooltip(mx(e.precio), {
      permanent: true, direction: "right", className: "precio-tag", offset: [4, 0],
    });
    m._fija = fijas.has(e.id);
    marcadores.push(m);
    const s = e.mejor ? recibo(e) :
      `<div class="save">Es la mejor opción en ${DATA.params.radio} km a la redonda.</div>`;
    m.bindPopup(`<div class="pop"><b>${e.nombre}</b>
      <span class="big">${mx(e.precio)}</span> /L ·
      ${e.vs_mediana >= 0 ? e.vs_mediana.toFixed(2) + " abajo" : (-e.vs_mediana).toFixed(2) + " arriba"} de la mediana
      ${s}</div>`, { maxWidth: 260 });
    return m;
  })).addTo(map);
  map.fitBounds(L.latLngBounds(est.map(e => [e.lat, e.lon])).pad(0.05));
  if (miPos) marcaYo();
  etiquetas();
}

// Etiquetas: solo las fijas hasta zoom 14; de ahí en adelante, todas.
function etiquetas() {
  const todas = map.getZoom() >= 14;
  marcadores.forEach(m => {
    const t = m.getTooltip && m.getTooltip();
    if (!t || !t._container) return;
    t._container.style.display = (todas || m._fija) ? "" : "none";
  });
}

function marcaYo() {
  if (yoMarker) map.removeLayer(yoMarker);
  yoMarker = L.circleMarker([miPos.lat, miPos.lon], {
    radius: 9, weight: 3, color: "#ffffff", fillColor: "#0b0b0b", fillOpacity: 1,
  }).bindTooltip("Estás aquí", { permanent: true, direction: "top", className: "precio-tag" });
  yoMarker.addTo(map);
}

// --- "las que me van quedando más cerca y más baratas" -----------------------
// Es la frontera de Pareto: recorres las estaciones de más cerca a más lejos y
// te quedas solo con las que rompen el récord de precio más barato hasta ahí.
// Una estación más lejana Y más cara que otra ya vista no tiene ningún caso.
function cercanas(d) {
  const orden = d.estaciones
    .map(e => ({ ...e, dist: km(miPos.lat, miPos.lon, e.lat, e.lon) }))
    .sort((a,b) => a.dist - b.dist);

  const masCercana = orden[0];
  const frontera = [];
  let record = Infinity;
  for (const e of orden) {
    if (e.precio < record) { record = e.precio; frontera.push(e); }
  }

  document.getElementById("tcerca").innerHTML = frontera.slice(0, 12).map(e => {
    // Ahorro neto contra quedarte en la que ya tienes más cerca.
    const extra = Math.max(0, e.dist - masCercana.dist);
    const neto = (masCercana.precio - e.precio) * DATA.params.tanque
               - (2 * extra / DATA.params.rendimiento) * e.precio;
    const esLaCercana = e.id === masCercana.id;
    return `<tr>
      <td class="${esLaCercana ? "yo" : ""}">${e.nombre}${esLaCercana ? " · la que tienes más cerca" : ""}</td>
      <td class="num">${e.dist.toFixed(1)} km</td>
      <td class="num">${mx(e.precio)}</td>
      <td class="num">${esLaCercana ? "—" : (neto > 0 ? "<b>" + mxr(neto) + "</b>" : "no compensa")}</td>
    </tr>`;
  }).join("");
  document.getElementById("cardCerca").hidden = false;
}

document.getElementById("btnGeo").addEventListener("click", () => {
  const msg = document.getElementById("geoMsg");
  if (!navigator.geolocation) { msg.textContent = "Tu navegador no da ubicación."; return; }
  msg.textContent = "Buscando tu ubicación...";
  navigator.geolocation.getCurrentPosition(
    p => {
      miPos = { lat: p.coords.latitude, lon: p.coords.longitude };
      msg.textContent = "Listo. El color ahora compara solo entre tus "
        + DATA.params.cercanas + " gasolineras más cercanas.";
      render(current);   // redibuja con la escala de color local
    },
    err => {
      msg.textContent = err.code === 1
        ? "No diste permiso de ubicación."
        : "No pude obtener tu ubicación. Ojo: el navegador solo la da por https o localhost, no con el archivo abierto directo (file://). Sirve la carpeta con: python -m http.server 8000";
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

function hist(prices, mediana) {
  const W = 900, H = 215, P = {t:26, r:12, b:28, l:40};
  const lo = Math.min(...prices), hi = Math.max(...prices);
  const NB = 18, w = (hi - lo) / NB || 1;
  const bins = Array.from({length:NB}, (_,i) => ({x0: lo + i*w, x1: lo + (i+1)*w, n:0}));
  prices.forEach(p => bins[Math.min(NB-1, Math.floor((p-lo)/w))].n++);
  const maxN = Math.max(...bins.map(b => b.n));
  const X = v => P.l + (v - lo) / (hi - lo || 1) * (W - P.l - P.r);
  const Y = n => H - P.b - n / maxN * (H - P.t - P.b);
  const bw = (W - P.l - P.r) / NB;

  const bars = bins.map(b => {
    const h = H - P.b - Y(b.n);
    return `<rect x="${X(b.x0)+1}" y="${Y(b.n)}" width="${Math.max(1,bw-2)}" height="${h}"
      rx="${Math.min(4, h/2)}" fill="var(--s1)"><title>${b.n} gasolineras entre $${b.x0.toFixed(2)} y $${b.x1.toFixed(2)}</title></rect>`;
  }).join("");
  const medX = X(mediana), flip = medX > W * 0.7;

  document.getElementById("hist").innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img"
          aria-label="Distribución de precios: ${prices.length} gasolineras entre $${lo.toFixed(2)} y $${hi.toFixed(2)}, mediana $${mediana.toFixed(2)}">
      <text x="${P.l}" y="${P.t+2}" fill="var(--muted)" font-size="11">gasolineras</text>
      <text x="${P.l-8}" y="${Y(maxN)+10}" fill="var(--muted)" font-size="11" text-anchor="end">${maxN}</text>
      <text x="${P.l-8}" y="${H-P.b}" fill="var(--muted)" font-size="11" text-anchor="end">0</text>
      <line x1="${P.l}" y1="${H-P.b}" x2="${W-P.r}" y2="${H-P.b}" stroke="var(--axis)" stroke-width="1"/>
      ${bars}
      <line x1="${medX}" y1="${P.t+8}" x2="${medX}" y2="${H-P.b}"
            stroke="var(--text-primary)" stroke-width="2" stroke-dasharray="4 3"/>
      <text x="${medX + (flip ? -8 : 8)}" y="${P.t+2}" fill="var(--text-primary)" font-size="11"
            font-weight="600" text-anchor="${flip ? "end" : "start"}">mediana $${mediana.toFixed(2)}</text>
      <text x="${P.l}" y="${H-8}" fill="var(--muted)" font-size="11">$${lo.toFixed(2)}</text>
      <text x="${W-P.r}" y="${H-8}" fill="var(--muted)" font-size="11" text-anchor="end">$${hi.toFixed(2)}</text>
    </svg>`;
}

function arranca(datos) {
  DATA = datos;
  document.getElementById("sub").textContent =
    "Corte del " + DATA.corte + " · ahorro calculado para un tanque de " + DATA.params.tanque +
    " L a " + DATA.params.rendimiento + " km/L, con vecinas a " + DATA.params.radio + " km a la redonda.";

  document.getElementById("fuelFilter").innerHTML = Object.entries(DATA.fuels)
    .map(([k,v]) => `<button data-fuel="${k}" aria-pressed="false">${v.label}</button>`).join("");
  document.querySelectorAll("#fuelFilter button").forEach(b =>
    b.addEventListener("click", () => render(b.dataset.fuel)));

  current = Object.keys(DATA.fuels)[0];
  render(current);
}

// Sin datos embebidos, se piden al servidor. El ?v= evita que el navegador
// devuelva los precios de ayer desde su cache.
if (DATA_EMBEBIDA) {
  arranca(DATA_EMBEBIDA);
} else {
  fetch("datos.json?v=" + Date.now())
    .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(arranca)
    .catch(() => {
      document.getElementById("sub").textContent =
        "No pude cargar los precios. Revisa tu conexión y vuelve a entrar.";
    });
}

// Instalable en el teléfono. Solo aplica servido por http/https, no con el
// archivo abierto directo.
if ("serviceWorker" in navigator && location.protocol !== "file:") {
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("sw.js").catch(() => {}));
}
</script>
</body>
</html>
"""


# ======================================================================== PWA

MANIFEST = {
    "name": "Gasolina en León",
    "short_name": "Gasolina León",
    "description": "Dónde está más barata la gasolina en León hoy, y cuánto te queda "
                   "en la bolsa si te mueves.",
    "start_url": ".",
    "scope": ".",
    "display": "standalone",
    "orientation": "portrait-primary",
    "background_color": "#f9f9f7",
    "theme_color": "#2a78d6",
    "lang": "es-MX",
    "icons": [
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

SW_JS = """// Service worker: deja que la app abra aunque no haya señal.
const CACHE = "gasleon-__VER__";
const SHELL = ["./", "./index.html", "./manifest.webmanifest",
               "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;

  // Los precios: SIEMPRE de la red, para que la app nunca muestre los de ayer.
  // Si no hay señal, los últimos que se guardaron.
  if (new URL(req.url).pathname.endsWith("/datos.json")) {
    e.respondWith(
      fetch(req).then(r => {
        const copia = r.clone();
        caches.open(CACHE).then(c => c.put("./datos.json", copia));
        return r;
      }).catch(() => caches.match("./datos.json"))
    );
    return;
  }

  // La página en sí casi no cambia, pero igual se revalida contra la red.
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req).then(r => {
        const copia = r.clone();
        caches.open(CACHE).then(c => c.put("./index.html", copia));
        return r;
      }).catch(() => caches.match("./index.html"))
    );
    return;
  }

  // Todo lo demás (iconos, Leaflet, tiles): del cache si está, si no de la red.
  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(r => {
      if (r.ok && (new URL(req.url).origin === location.origin)) {
        const copia = r.clone();
        caches.open(CACHE).then(c => c.put(req, copia));
      }
      return r;
    }).catch(() => hit))
  );
});
"""


WORKFLOW = """# Recolecta los precios y republica el sitio, solo, en la nube.
# Tu computadora no necesita estar prendida.
name: precios

on:
  schedule:
    # Los 6 cortes oficiales en hora de México (UTC-6), escritos en UTC.
    # 01:00 -> 07:00 | 06:00 -> 12:00 | 08:00 -> 14:00
    # 11:00 -> 17:00 | 15:00 -> 21:00 | 19:00 -> 01:00 del día siguiente
    - cron: "10 7,12,14,17,21,1 * * *"
  workflow_dispatch:        # además, un botón para dispararlo a mano

permissions:
  contents: write           # para guardar los snapshots en el repo
  pages: write
  id-token: write

concurrency:
  group: precios
  cancel-in-progress: false

jobs:
  recolectar:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install requests pandas pyarrow

      - name: Bajar el corte de ahora
        run: python leon_gas.py bajar --data ./data

      - name: Guardar el snapshot en el repo
        run: |
          git config user.name  "recolector"
          git config user.email "actions@github.com"
          git add data/
          git diff --staged --quiet && exit 0
          git commit -m "precios $(date -u +'%Y-%m-%d %H:%M') UTC"
          git pull --rebase --autostash || true   # por si el repo avanzó mientras tanto
          git push

      - name: Armar el sitio
        run: python leon_gas.py sitio --data ./data --carpeta ./sitio --no-servidor

      - uses: actions/upload-pages-artifact@v3
        with:
          path: ./sitio

  publicar:
    needs: recolectar
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
"""

PASOS = """
================================ QUÉ SIGUE ================================

Ya quedaron los dos archivos. Ahora, una sola vez:

 1. Crea un repo PÚBLICO en github.com (por ejemplo: gasolina-leon).

 2. Sube este proyecto. Desde esta carpeta:

      git init
      git add .
      git commit -m "primera version"
      git branch -M main
      git remote add origin https://github.com/TU-USUARIO/gasolina-leon.git
      git push -u origin main

 3. En el repo, ve a Settings > Pages y en "Source" elige GitHub Actions.

 4. Ve a la pestaña Actions, escoge "precios" y dale "Run workflow".
    Esa primera corrida a mano te confirma que todo jala.

Listo. De ahí en adelante se recolecta y se republica solo, 6 veces al día.
Tu sitio queda en:  https://TU-USUARIO.github.io/gasolina-leon/

Dos notas:
 - El repo debe ser público para que Pages y Actions te salgan gratis.
   Como beneficio, tu histórico de precios queda como dataset abierto.
 - Los horarios de GitHub no son exactos: puede retrasarse algunos minutos
   cuando sus servidores andan ocupados. Para esto da igual.
===========================================================================
"""


def automatizar(carpeta: Path) -> None:
    """Deja listos los archivos para que GitHub lo corra todo solo."""
    wf = carpeta / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "precios.yml").write_text(WORKFLOW, encoding="utf-8")
    print(f"  -> {wf / 'precios.yml'}")

    (carpeta / "requirements.txt").write_text(
        "requests\npandas\npyarrow\n", encoding="utf-8")
    print(f"  -> {carpeta / 'requirements.txt'}")

    gi = carpeta / ".gitignore"
    if not gi.exists():
        gi.write_text("sitio/\nleon_hoy.html\n__pycache__/\n", encoding="utf-8")
        print(f"  -> {gi}")

    print(PASOS)


def _png(w: int, h: int, px: bytearray) -> bytes:
    """Codifica RGBA crudo como PNG. Sin Pillow: solo zlib y struct."""
    import struct
    import zlib

    def chunk(tipo: bytes, datos: bytes) -> bytes:
        cuerpo = tipo + datos
        return (struct.pack(">I", len(datos)) + cuerpo
                + struct.pack(">I", zlib.crc32(cuerpo) & 0xFFFFFFFF))

    filas = b"".join(b"\x00" + bytes(px[y * w * 4:(y + 1) * w * 4]) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(filas, 9))
            + chunk(b"IEND", b""))


def _icono(lado: int) -> bytes:
    """Un pin de mapa blanco sobre fondo azul. Dibujado a mano, con supersampling
    2x para que los bordes no queden dentados."""
    AZUL = (0x2A, 0x78, 0xD6)
    BLANCO = (0xFF, 0xFF, 0xFF)
    S = 2                       # muestras por lado
    n = lado * S
    u = n / 512.0               # todo está medido sobre un lienzo de 512

    cx, cy, r = 256 * u, 215 * u, 96 * u      # cabeza del pin
    punta = 410 * u                            # dónde termina la punta
    hueco = 40 * u                             # el agujero del centro

    px = bytearray(lado * lado * 4)
    for y in range(lado):
        for x in range(lado):
            dentro = 0
            for sy in range(S):
                for sx in range(S):
                    fx, fy = x * S + sx + 0.5, y * S + sy + 0.5
                    d2 = (fx - cx) ** 2 + (fy - cy) ** 2
                    if d2 <= hueco ** 2:
                        continue                      # el agujero es azul
                    en_cabeza = d2 <= r ** 2
                    # La punta: un triángulo que se cierra desde la cabeza.
                    en_punta = False
                    if cy < fy <= punta:
                        t = (fy - cy) / (punta - cy)
                        medio = r * (1 - t) ** 0.85
                        en_punta = abs(fx - cx) <= medio
                    if en_cabeza or en_punta:
                        dentro += 1
            a = dentro / (S * S)
            col = tuple(round(AZUL[i] + (BLANCO[i] - AZUL[i]) * a) for i in range(3))
            i = (y * lado + x) * 4
            px[i:i + 4] = bytes(col) + b"\xff"
    return _png(lado, lado, px)


def sitio(data_dir: Path, carpeta: Path, radio, tanque, rend, cercanas) -> Path:
    """Arma la carpeta lista para subir. A diferencia del tablero local, aquí
    los datos van en datos.json aparte: así cada corte solo reemplaza ese
    archivo y la página no se toca."""
    carpeta.mkdir(parents=True, exist_ok=True)

    df = _cargar(data_dir)
    datos = _payload(df, radio, tanque, rend, cercanas)
    (carpeta / "datos.json").write_text(
        json.dumps(datos, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (carpeta / "index.html").write_text(HTML.replace("__PAYLOAD__", "null"), encoding="utf-8")
    print(f"  datos.json ({(carpeta / 'datos.json').stat().st_size/1024:.0f} KB) + index.html")

    (carpeta / "manifest.webmanifest").write_text(
        json.dumps(MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")
    # El service worker no se versiona con el corte: la página ya no cambia,
    # solo el JSON, que siempre se pide a la red.
    (carpeta / "sw.js").write_text(SW_JS.replace("__VER__", "1"), encoding="utf-8")

    for lado in (192, 512):
        destino = carpeta / f"icon-{lado}.png"
        if not destino.exists():            # los íconos no cambian; no los rehagas
            destino.write_bytes(_icono(lado))
            print(f"  icono -> {destino.name}")

    print(f"\n  Carpeta lista para subir: {carpeta.resolve()}")
    print("  Sube TODO su contenido a tu hosting (index.html, manifest, sw.js, iconos).")
    return carpeta / "index.html"


def tablero(data_dir: Path, salida: Path, radio, tanque, rend, cercanas) -> Path:
    df = _cargar(data_dir)
    datos = _payload(df, radio, tanque, rend, cercanas)
    salida.write_text(HTML.replace("__PAYLOAD__", json.dumps(datos, ensure_ascii=False)),
                      encoding="utf-8")
    print(f"tablero -> {salida.resolve()}")
    return salida


def servir(html: Path, puerto: int = 8000, abrir: bool = True) -> None:
    """Sirve la carpeta por HTTP en tu propia máquina.

    Hace falta porque el navegador NO da la ubicación del usuario ni deja que
    ciertos mapas carguen cuando el archivo se abre directo (file://). En
    localhost sí, porque cuenta como contexto seguro.
    """
    import functools
    import http.server

    carpeta = html.resolve().parent

    class Silencioso(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # sin llenar la consola de ruido
            pass

    handler = functools.partial(Silencioso, directory=str(carpeta))

    for intento in range(puerto, puerto + 10):
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", intento), handler)
            break
        except OSError:
            print(f"  puerto {intento} ocupado, pruebo el siguiente...")
    else:
        raise SystemExit("No encontré un puerto libre entre "
                         f"{puerto} y {puerto + 9}.")

    url = f"http://localhost:{intento}/{html.name}"
    print(f"\n  Servidor listo.  ->  {url}")
    print("  Deja esta ventana abierta. Ctrl+C para detenerlo.\n")
    if abrir:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nservidor detenido")
        httpd.shutdown()


# ======================================================================= CLI

def main():
    ap = argparse.ArgumentParser(
        description="Precios de gasolina en León: descarga y tablero, en un solo archivo.")
    ap.add_argument("accion", nargs="?", default="todo",
                    choices=["todo", "bajar", "tablero", "servir", "sitio", "automatizar"],
                    help="todo (default) = baja, dibuja y abre | bajar = solo snapshot "
                         "| tablero = solo redibuja | servir = abre el que ya existe "
                         "| sitio = arma la carpeta PWA lista para subir "
                         "| automatizar = deja listo GitHub para que corra solo")
    ap.add_argument("--data", default="./data", help="carpeta raíz de los snapshots (default ./data)")
    ap.add_argument("--out", default="leon_hoy.html", help="archivo HTML de salida")
    ap.add_argument("--radio", type=float, default=3.0, help="km a la redonda para buscar alternativa")
    ap.add_argument("--tanque", type=float, default=50.0, help="litros por llenada")
    ap.add_argument("--rendimiento", type=float, default=12.0, help="km por litro de tu coche")
    ap.add_argument("--cercanas", type=int, default=10,
                    help="cuántas gasolineras cercanas definen la escala de color (default 10)")
    ap.add_argument("--nacional", action="store_true", help="guarda el país entero, no solo León")
    ap.add_argument("--inspeccionar", action="store_true", help="solo imprime el esquema XML y sale")
    ap.add_argument("--carpeta", default="./sitio",
                    help="carpeta de salida del sitio PWA (acción 'sitio')")
    ap.add_argument("--puerto", type=int, default=8000, help="puerto del servidor local")
    ap.add_argument("--no-abrir", action="store_true", help="no abrir el navegador")
    ap.add_argument("--no-servidor", action="store_true",
                    help="solo generar el archivo, sin levantar el servidor local")
    a = ap.parse_args()

    raiz = Path(a.data)
    scope = "nacional" if a.nacional else "leon"
    html = Path(a.out)

    if a.accion in ("todo", "bajar"):
        bajar(raiz, nacional=a.nacional, inspeccionar=a.inspeccionar)
        if a.inspeccionar:
            return

    if a.accion == "automatizar":
        automatizar(Path("."))
        return

    if a.accion == "sitio":
        html = sitio(raiz / scope, Path(a.carpeta), a.radio, a.tanque, a.rendimiento, a.cercanas)
    elif a.accion in ("todo", "tablero"):
        tablero(raiz / scope, html, a.radio, a.tanque, a.rendimiento, a.cercanas)

    if a.accion == "servir" and not html.exists():
        raise SystemExit(f"No existe {html}. Genera el tablero primero:\n"
                         f"  python {Path(sys.argv[0]).name} tablero")

    # El servidor local es el modo normal: sin él, el navegador no da la
    # ubicación del usuario (file:// no es contexto seguro).
    if a.accion in ("todo", "tablero", "servir", "sitio") and not a.no_servidor:
        servir(html, puerto=a.puerto, abrir=not a.no_abrir)
    elif a.accion in ("todo", "tablero", "sitio"):
        print("  (generado sin servidor: el botón de ubicación no va a funcionar)")


if __name__ == "__main__":
    main()
