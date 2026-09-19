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

HTML = '''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Gasolina en León</title>
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#0c1f18">
<meta name="description" content="Dónde está más barata la gasolina en León hoy, y cuánto te queda en la bolsa si te mueves.">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Gasolina León">
<link rel="apple-touch-icon" href="icon-192.png">
<link rel="icon" href="icon-192.png">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  /* Tema de León: verde de campo y dorado de cuero. Un solo tema, siempre
     oscuro — es identidad, no preferencia del sistema. Todos los contrastes
     de texto verificados contra WCAG (el más bajo, 5.3:1). */
  :root {
    color-scheme: dark;
    --plane:#0c1f18;          /* fondo de la página */
    --surface-1:#143025;      /* tarjetas */
    --surface-2:#1b3d2f;      /* filas, realces suaves */
    --ink:#f4f1e8;            /* texto principal */
    --ink-2:#b9c7ba;          /* texto secundario */
    --muted:#8fa396;          /* etiquetas tenues */
    --oro:#d9a441;            /* acento: botones, cifras que importan */
    --oro-ink:#1a1205;        /* texto encima del dorado */
    --verde:#7fc79a;          /* buenas noticias */
    --linea:rgba(244,241,232,0.12);

    /* Rampa del mapa. NO sigue el tema: los puntos van sobre tiles claros,
       y azul<->rojo es el par que aguanta el daltonismo (ΔE 23.8). */
    --barato:#2a78d6; --neutro:#dcdad3; --caro:#d03b3b; --mring:#ffffff;
    --apagado:#a8a69e;
  }
  * { box-sizing:border-box; }
  html, body { overflow-x:hidden; max-width:100%; }
  body {
    margin:0; padding:20px 14px 44px;
    background:var(--plane); color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
    -webkit-text-size-adjust:100%;
  }
  .wrap { max-width:940px; margin:0 auto; }
  h1 { font-size:1.4rem; margin:0 0 3px; letter-spacing:-0.01em; }
  .sub { color:var(--ink-2); font-size:0.8125rem; margin:0 0 18px; line-height:1.45; }

  /* ---------- invitación a instalar ---------- */
  .instalar { display:none; gap:10px; align-items:center; margin-bottom:16px;
    padding:12px 14px; background:var(--surface-1); border:1px solid var(--linea);
    border-left:4px solid var(--oro); border-radius:12px; }
  .instalar.ver { display:flex; }
  .instalar .txt { flex:1 1 160px; font-size:0.8125rem; color:var(--ink-2); line-height:1.4; }
  .instalar .txt b { display:block; color:var(--ink); font-size:0.9rem; margin-bottom:1px; }
  .instalar button { font:inherit; font-size:0.875rem; font-weight:700; padding:9px 16px;
    cursor:pointer; background:var(--oro); color:var(--oro-ink); border:none;
    border-radius:9px; white-space:nowrap; }
  .instalar .cerrar { background:none; color:var(--muted); font-size:1.1rem;
    padding:4px 6px; font-weight:400; }

  /* ---------- filtros ---------- */
  .filters { display:flex; gap:8px; margin-bottom:16px; }
  .filters button { flex:1; font:inherit; font-size:0.875rem; padding:10px 8px;
    cursor:pointer; background:var(--surface-1); color:var(--ink-2);
    border:1px solid var(--linea); border-radius:999px; }
  .filters button[aria-pressed="true"] { background:var(--oro); color:var(--oro-ink);
    border-color:transparent; font-weight:700; }

  /* ---------- cifras de cabecera ---------- */
  .kpis { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:16px; }
  .kpi { background:var(--surface-1); border:1px solid var(--linea); border-radius:12px;
    padding:12px; min-width:0; }
  .kpi .k { font-size:0.7rem; color:var(--muted); text-transform:uppercase;
    letter-spacing:0.05em; line-height:1.25; }
  .kpi .v { font-size:1.4rem; margin-top:5px; line-height:1.1; }
  .kpi .n { font-size:0.75rem; color:var(--ink-2); margin-top:3px; line-height:1.3;
    overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
  .kpi.barata .v { color:var(--verde); }

  /* ---------- tarjetas ---------- */
  .card { background:var(--surface-1); border:1px solid var(--linea); border-radius:14px;
    padding:16px; margin-bottom:16px; }
  .card h2 { font-size:1.05rem; margin:0 0 3px; }
  .card p.hint { font-size:0.8125rem; color:var(--ink-2); margin:0 0 14px; line-height:1.45; }

  /* ---------- mapa ---------- */
  .geobar { margin-bottom:12px; }
  .geobar button { width:100%; font:inherit; font-size:1rem; font-weight:700;
    padding:13px 16px; cursor:pointer; background:var(--oro); color:var(--oro-ink);
    border:none; border-radius:10px; }
  .geomsg { font-size:0.8125rem; color:var(--ink-2); margin:10px 0 0; line-height:1.4; }
  #map { height:58vh; min-height:320px; border-radius:10px; z-index:0; background:#e8e6e0; }
  .leaflet-container { background:#e8e6e0; font-family:inherit; }
  .escala { display:flex; align-items:center; gap:8px; margin-top:12px;
    font-size:0.75rem; color:var(--ink-2); }
  .escala .ramp { flex:1; height:9px; border-radius:99px;
    background:linear-gradient(90deg,var(--barato),var(--neutro),var(--caro)); }

  /* ---------- cuánto cambia (el número grande) ---------- */
  .hero { text-align:center; padding:6px 0 2px; }
  .hero .cifra { font-size:3rem; line-height:1; color:var(--oro); font-weight:700;
    letter-spacing:-0.02em; }
  .hero .pie { font-size:0.9rem; color:var(--ink-2); margin:10px auto 0; max-width:30em;
    line-height:1.5; }
  .rango { margin-top:20px; }
  .rango .barra { height:12px; border-radius:99px;
    background:linear-gradient(90deg,var(--barato),var(--neutro),var(--caro)); position:relative; }
  .rango .marca { position:absolute; top:-4px; width:3px; height:20px; border-radius:2px;
    background:var(--ink); box-shadow:0 0 0 2px var(--surface-1); }
  .rango .pies { display:flex; justify-content:space-between; margin-top:9px;
    font-size:0.75rem; color:var(--muted); }
  .rango .pies b { display:block; color:var(--ink); font-size:0.95rem;
    font-variant-numeric:tabular-nums; }
  .rango .pies .med { text-align:center; }
  .rango .pies .der { text-align:right; }

  /* ---------- consejos de movimiento ---------- */
  .consejos { display:grid; grid-template-columns:1fr; gap:12px; }
  @media (min-width:700px) { .consejos { grid-template-columns:1fr 1fr; } }
  .consejo { background:var(--surface-2); border-radius:12px; padding:14px; }
  .consejo .lab { font-size:0.7rem; color:var(--muted); text-transform:uppercase;
    letter-spacing:0.05em; }
  .consejo .est { display:flex; justify-content:space-between; align-items:baseline;
    gap:10px; margin-top:2px; }
  .consejo .est .nom { font-size:0.95rem; min-width:0; overflow-wrap:anywhere; }
  .consejo .est .pre { font-size:0.95rem; font-variant-numeric:tabular-nums;
    white-space:nowrap; color:var(--ink-2); }
  .consejo .flecha { font-size:0.8125rem; color:var(--muted); margin:9px 0 7px;
    padding-left:2px; }
  .consejo .dest .nom { font-weight:700; }
  .consejo .dest .pre { color:var(--verde); font-weight:700; }
  .consejo .total { display:flex; justify-content:space-between; align-items:baseline;
    margin-top:12px; padding-top:10px; border-top:1px solid var(--linea); }
  .consejo .total span { font-size:0.8125rem; color:var(--ink-2); }
  .consejo .total b { font-size:1.35rem; color:var(--oro); font-variant-numeric:tabular-nums; }

  /* ---------- lista desde tu ubicación ---------- */
  .cerca { list-style:none; margin:0; padding:0; }
  .cerca li { display:flex; justify-content:space-between; align-items:baseline; gap:12px;
    padding:12px 0; border-bottom:1px solid var(--linea); }
  .cerca li:last-child { border-bottom:none; }
  .cerca .izq { min-width:0; }
  .cerca .nom { font-size:0.95rem; overflow-wrap:anywhere; }
  .cerca .meta { font-size:0.8125rem; color:var(--ink-2); margin-top:3px;
    font-variant-numeric:tabular-nums; }
  .cerca .der { text-align:right; white-space:nowrap; }
  .cerca .der b { font-size:1.05rem; color:var(--oro); font-variant-numeric:tabular-nums; }
  .cerca .der small { display:block; font-size:0.7rem; color:var(--muted); }
  .cerca .tuya .nom { color:var(--verde); font-weight:700; }

  /* ---------- ranking ---------- */
  table { width:100%; border-collapse:collapse; font-size:0.9rem; table-layout:fixed; }
  th, td { text-align:left; padding:10px 6px; border-bottom:1px solid var(--linea); }
  th { font-size:0.7rem; color:var(--muted); text-transform:uppercase;
    letter-spacing:0.05em; font-weight:600; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .col-pos { width:2.2rem; } .col-pre { width:5.2rem; }
  td.nom { overflow-wrap:anywhere; }
  tbody tr:last-child td { border-bottom:none; }
  tbody tr:first-child td { color:var(--verde); font-weight:700; }

  /* ---------- globo del mapa ---------- */
  .leaflet-popup-content-wrapper { background:var(--surface-1); color:var(--ink);
    border-radius:12px; }
  .leaflet-popup-tip { background:var(--surface-1); }
  .leaflet-popup-content { margin:12px 14px; font-family:inherit; }
  .pop b.tit { display:block; font-size:0.95rem; margin-bottom:3px; }
  .pop .big { font-size:1.3rem; font-variant-numeric:tabular-nums; color:var(--ink); }
  .pop .save { margin-top:9px; padding-top:9px; border-top:1px solid var(--linea);
    font-size:0.8125rem; color:var(--ink-2); }
  .pop .recibo { width:100%; margin-top:7px; font-variant-numeric:tabular-nums;
    border-collapse:collapse; }
  .pop .recibo td { padding:2px 0; border:none; font-size:0.8rem; color:var(--ink-2); }
  .pop .recibo td:last-child { text-align:right; padding-left:12px; color:var(--ink); }
  .pop .recibo .tot td { border-top:1px solid var(--linea); padding-top:5px;
    font-weight:700; color:var(--oro); }
  .leaflet-tooltip.precio-tag { background:rgba(255,255,255,0.93); border:none;
    box-shadow:none; color:#12241c; font-size:11px; font-weight:700;
    font-variant-numeric:tabular-nums; padding:1px 4px; border-radius:3px; }
  .leaflet-tooltip.precio-tag::before { display:none; }
  .leaflet-control-attribution { background:rgba(255,255,255,0.75) !important;
    font-size:9px !important; }

  @media (max-width:420px) {
    h1 { font-size:1.2rem; }
    .kpi .v { font-size:1.15rem; }
    .kpi .k { font-size:0.64rem; }
    .hero .cifra { font-size:2.5rem; }
  }
  @supports (padding: max(0px)) {
    body { padding-left:max(14px, env(safe-area-inset-left));
           padding-right:max(14px, env(safe-area-inset-right));
           padding-bottom:max(44px, env(safe-area-inset-bottom)); }
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Gasolina en León</h1>
  <p class="sub" id="sub"></p>

  <div class="instalar" id="instalar">
    <div class="txt"><b>Guárdala en tu teléfono</b><span id="instalarComo"></span></div>
    <button id="btnInstalar" type="button">Instalar</button>
    <button class="cerrar" id="cerrarInstalar" type="button" aria-label="Cerrar">✕</button>
  </div>

  <div class="filters" id="fuelFilter" role="group" aria-label="Combustible"></div>
  <div class="kpis" id="kpis"></div>

  <div class="card">
    <h2>Dónde está barata hoy</h2>
    <p class="hint">Los puntos azules son las baratas y los rojos las caras. Toca cualquiera para ver si te conviene moverte.</p>
    <div class="geobar" id="geobar">
      <button id="btnGeo" type="button">Usar mi ubicación</button>
    </div>
    <div id="map"></div>
    <p class="geomsg" id="geoMsg" hidden></p>
    <div class="escala">
      <span id="legMin"></span><span class="ramp"></span><span id="legMax"></span>
    </div>
  </div>

  <div class="card" id="cardCerca" hidden>
    <h2>Las mejores desde donde estás</h2>
    <p class="hint">Van de más cerca a más lejos, y cada una es más barata que todas las anteriores. Las que se saltan es porque hay otra más cerca y más barata: no tiene caso ir.</p>
    <ul class="cerca" id="listaCerca"></ul>
  </div>

  <div class="card">
    <h2>¿Cuánto cambia de una gasolinera a otra?</h2>
    <div class="hero">
      <div class="cifra" id="heroCifra"></div>
      <p class="pie" id="heroPie"></p>
    </div>
    <div class="rango">
      <div class="barra"><div class="marca" id="marcaMed"></div></div>
      <div class="pies">
        <div><b id="rMin"></b>la más barata</div>
        <div class="med"><b id="rMed"></b>lo normal</div>
        <div class="der"><b id="rMax"></b>la más cara</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Si cargas aquí, mejor ve allá</h2>
    <p class="hint">Los cambios que más te convienen hoy en toda la ciudad. La cifra dorada es lo que te queda en la bolsa por llenar el mismo tanque, ya restando la gasolina del desvío.</p>
    <div class="consejos" id="consejos"></div>
  </div>

  <div class="card">
    <h2>Las 15 más baratas de hoy</h2>
    <p class="hint">Por precio de litro, sin importar qué tan lejos queden.</p>
    <table>
      <thead><tr>
        <th class="col-pos">#</th><th>Estación</th><th class="num col-pre">Precio</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// Los datos vienen embebidos (modo local) o de datos.json (modo publicado).
const DATA_EMBEBIDA = __PAYLOAD__;
let DATA = null;

const CENTRO_LEON = [21.1215, -101.6827];
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const mx  = n => "$" + n.toFixed(2);
const mxr = n => "$" + Math.round(n).toLocaleString("es-MX");

// Si el CDN de Leaflet no cargó, el resto del tablero debe seguir sirviendo.
const HAS_MAP = typeof L !== "undefined";
let map = null;
if (HAS_MAP) {
  map = L.map("map", { scrollWheelZoom:false, zoomControl:true });
  map.setView(CENTRO_LEON, 12);
  // Un solo fondo, sin menú de capas: Esri no pide API key ni Referer.
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "Tiles &copy; Esri" }
  ).addTo(map).on("tileerror", ev => { if (ev.tile) ev.tile.style.visibility = "hidden"; });
} else {
  const el = document.getElementById("map");
  el.style.cssText = "height:auto;padding:22px;border:1px dashed var(--linea);border-radius:10px;color:var(--ink-2);font-size:0.875rem";
  el.textContent = "No se pudo cargar el mapa. El resto de la información sí funciona.";
}

let layer = null, marcadores = [], yoMarker = null, miPos = null;
let current = null;
if (HAS_MAP) map.on("zoomend", etiquetas);

// --- color: degradado continuo entre la más barata y la más cara ------------
const hex2rgb = h => [1,3,5].map(i => parseInt(h.slice(i, i+2), 16));
const mezcla = (a, b, t) => a.map((v,i) => Math.round(v + (b[i]-v)*t));
const rgb = c => `rgb(${c[0]},${c[1]},${c[2]})`;

function colorFor(precio, min, max) {
  const A = hex2rgb(css("--barato")), M = hex2rgb(css("--neutro")), B = hex2rgb(css("--caro"));
  const t = max > min ? Math.min(1, Math.max(0, (precio - min) / (max - min))) : 0.5;
  return t <= 0.5 ? rgb(mezcla(A, M, t * 2)) : rgb(mezcla(M, B, (t - 0.5) * 2));
}

const km = (aLat, aLon, bLat, bLon) => {
  const R = 6371, r = Math.PI/180;
  const p1 = aLat*r, p2 = bLat*r;
  const h = Math.sin((p2-p1)/2)**2 + Math.cos(p1)*Math.cos(p2)*Math.sin((bLon-aLon)*r/2)**2;
  return 2*R*Math.asin(Math.sqrt(h));
};

// Las N más cercanas a ti. Sin ubicación, no hay "cercanas".
function vecindario(d) {
  if (!miPos) return null;
  const orden = d.estaciones
    .map(e => ({ e, dist: km(miPos.lat, miPos.lon, e.lat, e.lon) }))
    .sort((a,b) => a.dist - b.dist)
    .slice(0, DATA.params.cercanas);
  const precios = orden.map(o => o.e.precio);
  return { ids: new Set(orden.map(o => o.e.id)),
           min: Math.min(...precios), max: Math.max(...precios) };
}

function recibo(e) {
  const T = DATA.params.tanque, m = e.mejor;
  const aqui = Math.round(e.precio * T);
  const alla = Math.round(m.alt_precio * T);
  const desvio = Math.round((2 * m.km / DATA.params.rendimiento) * m.alt_precio);
  return `<div class="save">
    <b>Te conviene ir a ${m.alt}</b>, a ${m.km} km
    <table class="recibo">
      <tr><td>Llenar ${T} L aquí</td><td>${mxr(aqui)}</td></tr>
      <tr><td>Llenar ${T} L allá</td><td>−${mxr(alla)}</td></tr>
      <tr><td>Gasolina del desvío</td><td>−${mxr(desvio)}</td></tr>
      <tr class="tot"><td>Te queda en la bolsa</td><td>${mxr(aqui - alla - desvio)}</td></tr>
    </table></div>`;
}

function render(fuel) {
  current = fuel;
  const d = DATA.fuels[fuel], est = d.estaciones;
  const barata = est.reduce((a,b) => b.precio < a.precio ? b : a);
  const cara   = est.reduce((a,b) => b.precio > a.precio ? b : a);
  const T = DATA.params.tanque;

  document.querySelectorAll("#fuelFilter button").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.fuel === fuel)));

  document.getElementById("kpis").innerHTML = [
    ["barata", "La más barata", mx(d.min), barata.nombre],
    ["", "Precio normal", mx(d.mediana), d.n + " gasolineras"],
    ["", "La más cara", mx(d.max), cara.nombre],
  ].map(([cl,k,v,n]) =>
    `<div class="kpi ${cl}"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`
  ).join("");

  // El número grande: qué te cuesta equivocarte de gasolinera.
  document.getElementById("heroCifra").textContent = mxr((d.max - d.min) * T);
  document.getElementById("heroPie").textContent =
    "Eso es lo que hay de diferencia entre llenar " + T +
    " litros en la gasolinera más cara de León y en la más barata. Es el mismo tanque.";
  document.getElementById("rMin").textContent = mx(d.min);
  document.getElementById("rMed").textContent = mx(d.mediana);
  document.getElementById("rMax").textContent = mx(d.max);
  const pos = d.max > d.min ? (d.mediana - d.min) / (d.max - d.min) : 0.5;
  document.getElementById("marcaMed").style.left =
    "calc(" + (pos * 100).toFixed(1) + "% - 1.5px)";

  const v = vecindario(d);
  document.getElementById("legMin").textContent = mx(v ? v.min : d.min);
  document.getElementById("legMax").textContent = mx(v ? v.max : d.max);

  if (HAS_MAP) drawMap(d);
  if (miPos) cercanas(d);

  // Consejos de movimiento, como tarjetas (una tabla de 6 columnas no cabe
  // en un teléfono sin obligar a deslizar de lado).
  document.getElementById("consejos").innerHTML = est
    .filter(e => e.mejor).sort((a,b) => b.mejor.ahorro - a.mejor.ahorro).slice(0,8)
    .map(e => `<div class="consejo">
        <div class="lab">Si cargas en</div>
        <div class="est"><span class="nom">${e.nombre}</span><span class="pre">${mx(e.precio)}</span></div>
        <div class="flecha">↓ muévete ${e.mejor.km.toFixed(1)} km</div>
        <div class="lab">Mejor ve a</div>
        <div class="est dest"><span class="nom">${e.mejor.alt}</span><span class="pre">${mx(e.mejor.alt_precio)}</span></div>
        <div class="total"><span>Te queda en la bolsa</span><b>${mxr(e.mejor.ahorro)}</b></div>
      </div>`).join("")
    || '<p class="hint">Hoy no hay diferencias que valgan el desvío.</p>';

  document.getElementById("tbody").innerHTML = est
    .slice().sort((a,b) => a.precio - b.precio).slice(0,15)
    .map((e,i) => `<tr>
      <td class="num">${i+1}</td><td class="nom">${e.nombre}</td>
      <td class="num">${mx(e.precio)}</td></tr>`).join("");
}

function drawMap(d) {
  const est = d.estaciones;
  if (layer) map.removeLayer(layer);

  const v = vecindario(d);
  const lo = v ? v.min : d.min, hi = v ? v.max : d.max;
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
    m.bindTooltip(mx(e.precio), { permanent:true, direction:"right",
      className:"precio-tag", offset:[4,0] });
    m._fija = fijas.has(e.id);
    marcadores.push(m);
    const s = e.mejor ? recibo(e)
      : `<div class="save">Es la mejor opción en ${DATA.params.radio} km a la redonda.</div>`;
    m.bindPopup(`<div class="pop"><b class="tit">${e.nombre}</b>
      <span class="big">${mx(e.precio)}</span> por litro ${s}</div>`, { maxWidth: 250 });
    return m;
  })).addTo(map);

  // Sin ubicación, encuadra la ciudad; con ubicación, tu zona.
  if (miPos) { marcaYo(); map.setView([miPos.lat, miPos.lon], 14); }
  else map.setView(CENTRO_LEON, 12);
  etiquetas();
}

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
    radius: 9, weight: 3, color: "#ffffff", fillColor: "#12241c", fillOpacity: 1,
  }).bindTooltip("Estás aquí", { permanent:true, direction:"top", className:"precio-tag" });
  yoMarker.addTo(map);
}

// La frontera de Pareto: de más cerca a más lejos, quedándote solo con las que
// rompen el récord de precio. Si una está más lejos Y más cara, sobra.
function cercanas(d) {
  const orden = d.estaciones
    .map(e => ({ ...e, dist: km(miPos.lat, miPos.lon, e.lat, e.lon) }))
    .sort((a,b) => a.dist - b.dist);
  const masCercana = orden[0];

  const frontera = [];
  let record = Infinity;
  for (const e of orden) if (e.precio < record) { record = e.precio; frontera.push(e); }

  document.getElementById("listaCerca").innerHTML = frontera.slice(0, 10).map(e => {
    const extra = Math.max(0, e.dist - masCercana.dist);
    const neto = (masCercana.precio - e.precio) * DATA.params.tanque
               - (2 * extra / DATA.params.rendimiento) * e.precio;
    const esTuya = e.id === masCercana.id;
    return `<li class="${esTuya ? "tuya" : ""}">
      <div class="izq">
        <div class="nom">${e.nombre}</div>
        <div class="meta">${e.dist.toFixed(1)} km · ${mx(e.precio)} por litro</div>
      </div>
      <div class="der">${esTuya
        ? '<small>la que tienes<br>más cerca</small>'
        : (neto > 0 ? "<b>" + mxr(neto) + "</b><small>te queda</small>"
                    : '<small>no compensa<br>el desvío</small>')}</div>
    </li>`;
  }).join("");
  document.getElementById("cardCerca").hidden = false;
}

document.getElementById("btnGeo").addEventListener("click", () => {
  const msg = document.getElementById("geoMsg");
  const barra = document.getElementById("geobar");
  msg.hidden = false;
  if (!navigator.geolocation) { msg.textContent = "Tu teléfono no está dando la ubicación."; return; }
  msg.textContent = "Buscando dónde estás...";
  navigator.geolocation.getCurrentPosition(
    p => {
      miPos = { lat: p.coords.latitude, lon: p.coords.longitude };
      barra.hidden = true;                    // ya no hace falta el botón
      msg.textContent = "Listo. Los colores ahora comparan solo entre tus "
        + DATA.params.cercanas + " gasolineras más cercanas.";
      render(current);
    },
    err => {
      msg.textContent = err.code === 1
        ? "No diste permiso de ubicación. Puedes activarlo en los ajustes del navegador."
        : "No pude obtener tu ubicación. Inténtalo de nuevo en un momento.";
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

function arranca(datos) {
  DATA = datos;
  document.getElementById("sub").textContent =
    "Precios del " + DATA.corte + ". Las cuentas son para un tanque de " +
    DATA.params.tanque + " litros en un coche que hace " + DATA.params.rendimiento + " km por litro.";

  document.getElementById("fuelFilter").innerHTML = Object.entries(DATA.fuels)
    .map(([k,v]) => `<button data-fuel="${k}" aria-pressed="false">${v.label}</button>`).join("");
  document.querySelectorAll("#fuelFilter button").forEach(b =>
    b.addEventListener("click", () => render(b.dataset.fuel)));

  current = Object.keys(DATA.fuels)[0];
  render(current);
}

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

if ("serviceWorker" in navigator && location.protocol !== "file:") {
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("sw.js").catch(() => {}));
}

// ---- invitación a instalar -------------------------------------------------
(() => {
  const caja = document.getElementById("instalar");
  const btn = document.getElementById("btnInstalar");
  const como = document.getElementById("instalarComo");
  let evento = null;

  const yaInstalada = matchMedia("(display-mode: standalone)").matches
    || navigator.standalone === true;
  let rechazada = false;
  try { rechazada = localStorage.getItem("instalar-no") === "1"; } catch (e) {}
  if (yaInstalada || rechazada) return;

  const esIOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
    && !/CriOS|FxiOS/.test(navigator.userAgent);

  window.addEventListener("beforeinstallprompt", e => {
    e.preventDefault(); evento = e;
    como.textContent = "Queda con su ícono, como cualquier app.";
    btn.hidden = false; caja.classList.add("ver");
  });

  if (esIOS) {
    como.textContent = "Toca Compartir abajo y luego «Agregar a inicio».";
    btn.hidden = true; caja.classList.add("ver");
  }

  btn.addEventListener("click", async () => {
    if (!evento) return;
    evento.prompt();
    const { outcome } = await evento.userChoice;
    evento = null;
    if (outcome === "accepted") caja.classList.remove("ver");
  });

  document.getElementById("cerrarInstalar").addEventListener("click", () => {
    caja.classList.remove("ver");
    try { localStorage.setItem("instalar-no", "1"); } catch (e) {}
  });

  window.addEventListener("appinstalled", () => caja.classList.remove("ver"));
})();
</script>
</body>
</html>
'''


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
    "background_color": "#0c1f18",
    "theme_color": "#0c1f18",
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

  // Los precios: SIEMPRE de la red, para que nunca se muestren los de ayer.
  // Sin señal, los últimos que se guardaron.
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

      # Sin 'cache: pip' a propósito: ese caché exige que exista un
      # requirements.txt en el repo y, si falta, la corrida entera falla.
      - run: pip install --quiet requests pandas pyarrow

      - name: Bajar el corte de ahora
        run: python leon_gas.py bajar --data ./data

      - name: Guardar el snapshot en el repo
        run: |
          git config user.name  "recolector"
          git config user.email "actions@github.com"
          git add data/
          git diff --staged --quiet && exit 0
          git commit -m "precios $(date -u +'%Y-%m-%d %H:%M') UTC"
          git pull --rebase --autostash || true
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

Ya quedaron los archivos. Ahora, una sola vez:

 1. Crea un repo PÚBLICO en github.com (por ejemplo: gasolina-leon).

 2. Sube este proyecto. Desde esta carpeta:

      git init
      git add .
      git commit -m "primera version"
      git branch -M main
      git remote add origin https://github.com/TU-USUARIO/gasolina-leon.git
      git push -u origin main

 3. En el repo: Settings > Actions > General > Workflow permissions,
    elige "Read and write permissions" y guarda.

 4. En el repo: Settings > Pages, en "Source" elige GitHub Actions.

 5. Pestaña Actions > "precios" > "Run workflow", para probar.

Listo. De ahí en adelante se recolecta y se republica solo, 6 veces al día.
Tu sitio queda en:  https://TU-USUARIO.github.io/gasolina-leon/
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
    """Pin de mapa dorado sobre verde de León. Provisional: reemplázalo
    poniendo tus propios icon-192.png e icon-512.png en la carpeta."""
    FONDO = (0x14, 0x30, 0x25)
    PIN = (0xD9, 0xA4, 0x41)
    S = 2
    u = (lado * S) / 512.0

    cx, cy, r = 256 * u, 215 * u, 96 * u
    punta = 410 * u
    hueco = 40 * u

    px = bytearray(lado * lado * 4)
    for y in range(lado):
        for x in range(lado):
            dentro = 0
            for sy in range(S):
                for sx in range(S):
                    fx, fy = x * S + sx + 0.5, y * S + sy + 0.5
                    d2 = (fx - cx) ** 2 + (fy - cy) ** 2
                    if d2 <= hueco ** 2:
                        continue
                    en_cabeza = d2 <= r ** 2
                    en_punta = False
                    if cy < fy <= punta:
                        t = (fy - cy) / (punta - cy)
                        en_punta = abs(fx - cx) <= r * (1 - t) ** 0.85
                    if en_cabeza or en_punta:
                        dentro += 1
            a = dentro / (S * S)
            col = tuple(round(FONDO[i] + (PIN[i] - FONDO[i]) * a) for i in range(3))
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

    _iconos(carpeta)

    # La versión del caché sale de un hash del contenido: si cambias el ícono o
    # la página, cambia sola y los teléfonos tiran lo viejo. Sin esto, la gente
    # se queda con el ícono anterior para siempre.
    import hashlib
    h = hashlib.sha1()
    for nombre in ("index.html", "manifest.webmanifest", "icon-192.png", "icon-512.png"):
        h.update((carpeta / nombre).read_bytes())
    ver = h.hexdigest()[:10]
    (carpeta / "sw.js").write_text(SW_JS.replace("__VER__", ver), encoding="utf-8")
    print(f"  version del cache: {ver}")

    print(f"\n  Carpeta lista para subir: {carpeta.resolve()}")
    return carpeta / "index.html"


# ------------------------------------------------------------------ íconos

def _leer_png(ruta: Path):
    """Lee un PNG de 8 bits (RGB o RGBA, sin entrelazado) y devuelve
    (ancho, alto, píxeles RGBA). Sin Pillow."""
    import struct
    import zlib

    d = ruta.read_bytes()
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("no es un PNG")

    i, idat, w = 8, bytearray(), None
    while i < len(d):
        ln = struct.unpack(">I", d[i:i + 4])[0]
        tipo = d[i + 4:i + 8]
        datos = d[i + 8:i + 8 + ln]
        if tipo == b"IHDR":
            w, hgt, prof, color, _, _, entre = struct.unpack(">IIBBBBB", datos)
            if prof != 8 or color not in (2, 6) or entre:
                raise ValueError("necesito un PNG de 8 bits RGB o RGBA sin entrelazar")
            canales = 4 if color == 6 else 3
        elif tipo == b"IDAT":
            idat += datos
        elif tipo == b"IEND":
            break
        i += 12 + ln

    crudo = zlib.decompress(bytes(idat))
    linea = w * canales
    px = bytearray(w * hgt * 4)
    prev = bytearray(linea)
    pos = 0
    for y in range(hgt):
        filtro = crudo[pos]; pos += 1
        fila = bytearray(crudo[pos:pos + linea]); pos += linea
        for x in range(linea):                      # deshacer el filtro PNG
            a = fila[x - canales] if x >= canales else 0
            b = prev[x]
            c = prev[x - canales] if x >= canales else 0
            if filtro == 1:   fila[x] = (fila[x] + a) & 0xFF
            elif filtro == 2: fila[x] = (fila[x] + b) & 0xFF
            elif filtro == 3: fila[x] = (fila[x] + (a + b) // 2) & 0xFF
            elif filtro == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                fila[x] = (fila[x] + pred) & 0xFF
        prev = fila
        for x in range(w):
            s, t = x * canales, (y * w + x) * 4
            px[t:t + 3] = fila[s:s + 3]
            px[t + 3] = fila[s + 3] if canales == 4 else 255
    return w, hgt, px


def _escalar(w: int, h: int, px: bytearray, lado: int) -> bytearray:
    """Reescala a lado×lado promediando por área (queda limpio al reducir)."""
    out = bytearray(lado * lado * 4)
    for y in range(lado):
        y0, y1 = y * h // lado, max(y * h // lado + 1, (y + 1) * h // lado)
        for x in range(lado):
            x0, x1 = x * w // lado, max(x * w // lado + 1, (x + 1) * w // lado)
            acc = [0, 0, 0, 0]; n = 0
            for sy in range(y0, y1):
                base = sy * w * 4
                for sx in range(x0, x1):
                    p = base + sx * 4
                    for c in range(4):
                        acc[c] += px[p + c]
                    n += 1
            t = (y * lado + x) * 4
            for c in range(4):
                out[t + c] = acc[c] // n
    return out


def _iconos(carpeta: Path) -> None:
    """Los íconos de la app, por orden de preferencia:

      1. icono.png junto al script  -> se reescala a los dos tamaños
      2. icon-192.png / icon-512.png junto al script -> se copian tal cual
      3. si no hay nada, se dibuja uno provisional
    """
    raiz = Path(__file__).resolve().parent
    fuente = raiz / "icono.png"

    if fuente.exists():
        try:
            w, h, px = _leer_png(fuente)
            if w != h:
                print(f"  ! icono.png no es cuadrado ({w}x{h}); se va a deformar")
            for lado in (192, 512):
                datos = px if (w == lado and h == lado) else _escalar(w, h, px, lado)
                (carpeta / f"icon-{lado}.png").write_bytes(_png(lado, lado, datos))
                print(f"  icono tuyo (de icono.png) -> icon-{lado}.png")
            return
        except Exception as e:
            print(f"  ! no pude leer icono.png ({e}); uso los de respaldo")

    for lado in (192, 512):
        nombre = f"icon-{lado}.png"
        propio = raiz / nombre
        destino = carpeta / nombre
        if propio.exists() and propio.resolve() != destino.resolve():
            destino.write_bytes(propio.read_bytes())
            print(f"  icono tuyo -> {nombre}")
        else:
            destino.write_bytes(_icono(lado))
            print(f"  icono provisional -> {nombre}")


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
