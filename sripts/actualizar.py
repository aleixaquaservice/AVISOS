"""Genera avisos.json con los avisos vigentes de AEMET (España) e IPMA (Portugal).

Fuente principal: AEMET OpenData (avisos CAP del último boletín, toda España).
Necesita la variable de entorno AEMET_API_KEY. Si OpenData falla, usa los RSS por
comunidad como respaldo.
"""
import io, json, os, re, sys, tarfile, gzip, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

REGIONES = {
    "61": "Andalucía", "62": "Aragón", "63": "Asturias", "64": "Illes Balears", "65": "Canarias",
    "66": "Cantabria", "67": "Castilla y León", "68": "Castilla-La Mancha", "69": "Cataluña",
    "70": "Extremadura", "71": "Galicia", "72": "Comunidad de Madrid", "73": "Región de Murcia",
    "74": "Navarra", "75": "País Vasco", "76": "La Rioja", "77": "Comunitat Valenciana",
}
UA = {"User-Agent": "Mozilla/5.0 (aquasalud-avisos)", "Accept": "application/json"}
MADRID = timezone(timedelta(hours=1))
NIVELES = ("amarillo", "naranja", "rojo")
ZONAS = {}  # codigo -> {nombre, poligonos}


def get(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def parse_date(s):
    s = (s or "").strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s)


# ---------- Fuente principal: AEMET OpenData (CAP) ----------
def _txt(el, tag):
    for c in el.iter():
        if c.tag.split("}")[-1] == tag:
            return (c.text or "").strip()
    return ""


def _pairs(info, tag):
    out = {}
    for c in info:
        if c.tag.split("}")[-1] == tag:
            out[_txt(c, "valueName")] = _txt(c, "value")
    return out


def parse_cap(xml_bytes):
    root = ET.fromstring(xml_bytes)
    if _txt(root, "msgType").lower() == "cancel":
        return []
    sent = parse_date(_txt(root, "sent"))
    out = []
    infos = [i for i in root if i.tag.split("}")[-1] == "info"]
    es = [i for i in infos if _txt(i, "language").lower().startswith("es")] or infos[:1]
    for info in es:
        nivel = _pairs(info, "parameter").get("AEMET-Meteoalerta nivel", "").lower()
        if nivel not in NIVELES:
            continue
        fen = _pairs(info, "eventCode").get("AEMET-Meteoalerta fenomeno", "")
        fen = fen.split(";", 1)[-1].strip() if fen else _txt(info, "event")
        onset, expires = _txt(info, "onset") or _txt(info, "effective"), _txt(info, "expires")
        for area in info:
            if area.tag.split("}")[-1] != "area":
                continue
            code = _pairs(area, "geocode").get("AEMET-Meteoalerta zona", "")
            polys = []
            for c in area:
                if c.tag.split("}")[-1] == "polygon" and (c.text or "").strip():
                    pts = []
                    for par in c.text.split():
                        try:
                            la, lo = par.split(",")[:2]; pts.append([round(float(la), 3), round(float(lo), 3)])
                        except ValueError:
                            pass
                    if len(pts) > 300:
                        paso = -(-len(pts) // 300); pts = pts[::paso]
                    if len(pts) >= 3:
                        polys.append(pts)
            if code and polys and code not in ZONAS:
                ZONAS[code] = {"nombre": _txt(area, "areaDesc"), "poligonos": polys}
            out.append({
                "region": REGIONES.get(code[:2], ""), "zona": _txt(area, "areaDesc"), "codigo": code,
                "fenomeno": fen, "nivel": nivel,
                "inicio": parse_date(onset), "fin": parse_date(expires), "_emitido": sent,
            })
    return out


def from_opendata(key):
    meta = json.loads(get("https://opendata.aemet.es/opendata/api/avisos_cap/ultimoelaborado/area/esp?api_key=" + key))
    estado = int(meta.get("estado", 0))
    if estado == 404:  # AEMET responde 404 cuando no hay avisos en vigor
        return []
    if estado != 200 or not meta.get("datos"):
        raise RuntimeError(f"OpenData estado {estado}: {meta.get('descripcion')}")
    raw = get(meta["datos"])
    avisos = []
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
            for m in tf.getmembers():
                if m.isfile() and m.name.lower().endswith(".xml"):
                    avisos += parse_cap(tf.extractfile(m).read())
    except tarfile.ReadError:
        data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        avisos += parse_cap(data)
    return avisos


# ---------- Respaldo: RSS por comunidad ----------
RSS = "https://www.aemet.es/documentos_d/eltiempo/prediccion/avisos/rss/CAP_AFAC{}_RSS.xml"
TITLE = re.compile(r"Aviso\.\s*Nivel\s+(\w+)\.\s*(.+?)\.\s*(.+?)\s*$", re.I)
SPAN = re.compile(r"de\s+(\d{1,2}:\d{2})\s+(\d{2}-\d{2}-\d{4})\s+\w+\s*\(UTC\s*([+-]\d+)\)\s+a\s+(\d{1,2}:\d{2})\s+(\d{2}-\d{2}-\d{4})\s+\w+\s*\(UTC\s*([+-]\d+)\)", re.I)


def _iso(hm, dmy, off):
    return datetime.strptime(f"{dmy} {hm}", "%d-%m-%Y %H:%M").replace(tzinfo=timezone(timedelta(hours=int(off))))


def parse_feed(xml_bytes, region):
    ch = ET.fromstring(xml_bytes).find("channel")
    pub = parse_date(ch.findtext("pubDate") or ch.findtext("lastBuildDate"))
    out = []
    for it in ch.findall("item"):
        t = (it.findtext("title") or "").strip()
        m, s = TITLE.match(t), SPAN.search(it.findtext("description") or "")
        if t.lower().startswith("estado completo") or not m or not s:
            continue
        out.append({"region": region, "zona": m.group(3).strip(), "codigo": "", "fenomeno": m.group(2).strip(),
                    "nivel": m.group(1).lower(), "inicio": _iso(*s.group(1, 2, 3)), "fin": _iso(*s.group(4, 5, 6)),
                    "_emitido": parse_date(it.findtext("pubDate")) if it.findtext("pubDate") else pub})
    return pub, out


def from_rss(prev):
    fuentes, avisos, fallidas = {}, [], []
    for code, region in REGIONES.items():
        try:
            pub, items = parse_feed(get(RSS.format(code), 30), region)
            fuentes[region] = pub.isoformat(); avisos += items
        except Exception as e:
            fallidas.append(region); print(f"ERROR RSS {region}: {e}", file=sys.stderr)
            if region in prev.get("fuentes", {}):
                fuentes[region] = prev["fuentes"][region]
                avisos += [dict(a, inicio=parse_date(a["inicio"]), fin=parse_date(a["fin"]),
                                _emitido=datetime.min.replace(tzinfo=timezone.utc))
                           for a in prev.get("avisos", []) if a.get("region") == region]
    if len(fallidas) == len(REGIONES):
        raise RuntimeError("No se ha podido leer ningún RSS")
    return fuentes, avisos


# ---------- Portugal: IPMA ----------
# Fuente principal: la página de cada distrito de la web del IPMA (se actualiza antes que la API).
# Respaldo por distrito: la API open-data del IPMA. Todas las horas del IPMA están en UTC.
DISTRITOS = {"PTO": "Porto", "AVR": "Aveiro", "LSB": "Lisboa", "STM": "Santarém", "GDA": "Guarda",
             "LRA": "Leiria", "EVR": "Évora", "STB": "Setúbal", "FAR": "Faro"}
FEN_PT = {"precipitacao": "Lluvias", "trovoada": "Tormentas", "vento": "Vientos", "neve": "Nevadas",
          "tempo quente": "Temperaturas máximas", "tempo frio": "Temperaturas mínimas",
          "agitacao maritima": "Costeros", "nevoeiro": "Nieblas"}
NIV_PT = {"amarelo": "amarillo", "yellow": "amarillo", "laranja": "naranja", "orange": "naranja",
          "vermelho": "rojo", "red": "rojo"}
MESES_PT = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8,
            "set": 9, "out": 10, "nov": 11, "dez": 12}
IPMA_LINEA = re.compile(r"([A-Za-zÀ-ÿ ]+?)\s*-\s*(Amarelo|Laranja|Vermelho)\s*\[\s*(\d{1,2})\s+([A-Za-z]{3})\s+(\d{1,2}):(\d{2})"
                        r"\s*-\s*(\d{1,2})\s+([A-Za-z]{3})\s+(\d{1,2}):(\d{2})\s*\]", re.I)


def _sin_acentos(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").lower().strip()


def _fecha_pt(d, mes, h, mi, now):
    m = MESES_PT.get(mes.lower()[:3])
    if not m:
        raise ValueError(mes)
    y = now.year + (1 if m < now.month - 6 else 0) - (1 if m > now.month + 6 else 0)
    return datetime(y, m, int(d), int(h), int(mi), tzinfo=timezone.utc)


def ipma_pagina(cod, now):
    """Devuelve la lista de avisos de un distrito leyendo su página, o None si la página no es válida."""
    import html as _html
    url = f"https://www.ipma.pt/pt/otempo/prev-sam/?p={cod}&t={int(now.timestamp())}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (aquasalud-avisos)",
                                               "Cache-Control": "no-cache", "Pragma": "no-cache"})
    raw = urllib.request.urlopen(req, timeout=30).read()
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError:
        txt = raw.decode("latin-1")
    txt = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", txt)
    txt = _html.unescape(re.sub(r"<[^>]+>", " ", txt))
    txt = re.sub(r"\s+", " ", txt)
    if not re.search(r"ltima atualiza", txt, re.I):
        return None  # la página no trae los avisos como texto: usar la API
    out = []
    for m in IPMA_LINEA.finditer(txt):
        tipo = _sin_acentos(m.group(1))
        fen = next((v for k, v in FEN_PT.items() if tipo.endswith(k)), None)
        if not fen:
            continue
        out.append({"fenomeno": fen, "nivel": NIV_PT[m.group(2).lower()],
                    "inicio": _fecha_pt(m.group(3), m.group(4), m.group(5), m.group(6), now),
                    "fin": _fecha_pt(m.group(7), m.group(8), m.group(9), m.group(10), now)})
    return out


def ipma_api():
    data = json.loads(get("https://api.ipma.pt/open-data/forecast/warnings/warnings_www.json", 30))
    res = {}
    for w in data:
        cod, niv = w.get("idAreaAviso"), NIV_PT.get((w.get("awarenessLevelID") or "").lower())
        fen = FEN_PT.get(_sin_acentos(w.get("awarenessTypeName")))
        if cod not in DISTRITOS or not niv or not fen:
            continue
        ini = datetime.fromisoformat(w["startTime"]).replace(tzinfo=timezone.utc)
        fin = datetime.fromisoformat(w["endTime"]).replace(tzinfo=timezone.utc)
        res.setdefault(cod, []).append({"fenomeno": fen, "nivel": niv, "inicio": ini, "fin": fin})
    return res


def from_ipma(now):
    """Devuelve (avisos, distritos leídos). Los distritos que no se pudieron leer se rellenan en main()."""
    avisos, api, leidos = [], None, set()
    for cod, nombre in DISTRITOS.items():
        lista = None
        try:
            lista = ipma_pagina(cod, now)
        except Exception as e:
            print(f"IPMA web {cod}: {e}", file=sys.stderr)
        # Página no válida, o sin avisos: se contrasta con la API por si la web no los muestra como texto.
        if not lista:
            if api is None:
                try:
                    api = ipma_api()
                except Exception as e:
                    print(f"IPMA API: {e}", file=sys.stderr)
                    api = False
            if api is False and lista is None:
                continue
            if api:
                lista = lista or api.get(cod, [])
            lista = lista or []
        leidos.add(cod)
        avisos += [dict(a, region="Portugal", zona=nombre, codigo=cod, _emitido=now) for a in lista]
    return avisos, leidos


def main(path="avisos.json"):
    now = datetime.now(timezone.utc)
    try:
        prev = json.load(open(path, encoding="utf-8"))
    except Exception:
        prev = {"fuentes": {}, "avisos": []}
    key = os.environ.get("AEMET_API_KEY", "").strip()
    origen = "opendata"
    try:
        if not key:
            raise RuntimeError("Falta AEMET_API_KEY")
        avisos = from_opendata(key)
        fuentes = {r: now.isoformat(timespec="seconds") for r in REGIONES.values()}
    except Exception as e:
        print(f"OpenData no disponible ({e}); uso RSS de respaldo.", file=sys.stderr)
        origen = "rss"
        fuentes, avisos = from_rss(prev)
    # Portugal (IPMA): si un distrito no se puede leer, se conservan sus avisos anteriores.
    try:
        pt, leidos = from_ipma(now)
    except Exception as e:
        print(f"IPMA no disponible ({e})", file=sys.stderr)
        pt, leidos = [], set()
    avisos += pt
    avisos += [dict(a, inicio=parse_date(a["inicio"]), fin=parse_date(a["fin"]),
                    _emitido=datetime.min.replace(tzinfo=timezone.utc))
               for a in prev.get("avisos", []) if a.get("region") == "Portugal" and a.get("codigo") not in leidos]
    if leidos:
        fuentes["Portugal"] = now.isoformat(timespec="seconds")
    elif "Portugal" in prev.get("fuentes", {}):
        fuentes["Portugal"] = prev["fuentes"]["Portugal"]
    print(f"IPMA: {len(leidos)}/{len(DISTRITOS)} distritos leídos, {len(pt)} avisos")

    best = {}
    for a in avisos:
        if not a["fin"] or a["fin"] <= now:
            continue
        k = (a["zona"].lower(), a["fenomeno"].lower(), a["inicio"], a["fin"], a["nivel"])
        if k not in best or a["_emitido"] >= best[k]["_emitido"]:
            best[k] = a
    res = sorted(best.values(), key=lambda a: (a["inicio"], a["zona"]))
    out = {
        "actualizado": now.astimezone(MADRID).isoformat(timespec="seconds"),
        "origen": origen,
        "fuentes": fuentes,
        "zonas": {k: v for k, v in ZONAS.items() if any(a["codigo"] == k for a in res)},
        "avisos": [{"region": a["region"], "zona": a["zona"], "codigo": a["codigo"], "fenomeno": a["fenomeno"],
                    "nivel": a["nivel"], "inicio": a["inicio"].isoformat(), "fin": a["fin"].isoformat()} for a in res],
    }
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"OK ({origen}): {len(out['avisos'])} avisos")


if __name__ == "__main__":
    main(*sys.argv[1:])
