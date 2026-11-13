"""Genera avisos.json con los avisos Meteoalerta vigentes de AEMET.

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
        "avisos": [{"region": a["region"], "zona": a["zona"], "codigo": a["codigo"], "fenomeno": a["fenomeno"],
                    "nivel": a["nivel"], "inicio": a["inicio"].isoformat(), "fin": a["fin"].isoformat()} for a in res],
    }
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"OK ({origen}): {len(out['avisos'])} avisos")


if __name__ == "__main__":
    main(*sys.argv[1:])
