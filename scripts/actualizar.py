"""Lee los avisos Meteoalerta de AEMET (RSS por comunidad) y genera avisos.json."""
import json, re, sys, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

FEEDS = {
    "61": "Andalucía", "62": "Aragón", "63": "Asturias", "64": "Illes Balears", "65": "Canarias",
    "66": "Cantabria", "67": "Castilla y León", "68": "Castilla-La Mancha", "69": "Cataluña",
    "70": "Extremadura", "71": "Galicia", "72": "Comunidad de Madrid", "73": "Región de Murcia",
    "74": "Navarra", "75": "País Vasco", "76": "La Rioja", "77": "Comunitat Valenciana",
}
URL = "https://www.aemet.es/documentos_d/eltiempo/prediccion/avisos/rss/CAP_AFAC{}_RSS.xml"
TITLE = re.compile(r"Aviso\.\s*Nivel\s+(\w+)\.\s*(.+?)\.\s*(.+?)\s*$", re.I)
SPAN = re.compile(r"de\s+(\d{1,2}:\d{2})\s+(\d{2}-\d{2}-\d{4})\s+\w+\s*\(UTC\s*([+-]\d+)\)\s+a\s+(\d{1,2}:\d{2})\s+(\d{2}-\d{2}-\d{4})\s+\w+\s*\(UTC\s*([+-]\d+)\)", re.I)
MADRID = timezone(timedelta(hours=1))


def iso(hm, dmy, off):
    tz = timezone(timedelta(hours=int(off)))
    return datetime.strptime(f"{dmy} {hm}", "%d-%m-%Y %H:%M").replace(tzinfo=tz)


def parse_date(s):
    s = (s or "").strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s)


def parse_feed(xml_bytes, region):
    root = ET.fromstring(xml_bytes)
    ch = root.find("channel")
    pub = parse_date(ch.findtext("pubDate") or ch.findtext("lastBuildDate"))
    out = []
    for it in ch.findall("item"):
        t = (it.findtext("title") or "").strip()
        if t.lower().startswith("estado completo"):
            continue
        m = TITLE.match(t)
        s = SPAN.search(it.findtext("description") or "")
        if not m or not s:
            continue
        nivel, fen, zona = m.group(1).lower(), m.group(2).strip(), m.group(3).strip()
        out.append({
            "region": region, "zona": zona, "fenomeno": fen, "nivel": nivel,
            "inicio": iso(s.group(1), s.group(2), s.group(3)),
            "fin": iso(s.group(4), s.group(5), s.group(6)),
            "_emitido": parse_date(it.findtext("pubDate")) if it.findtext("pubDate") else pub,
        })
    return pub, out


def main(path="avisos.json"):
    now = datetime.now(timezone.utc)
    try:
        prev = json.load(open(path, encoding="utf-8"))
    except Exception:
        prev = {"fuentes": {}, "avisos": []}
    fuentes, avisos, fallidas = {}, [], []
    for code, region in FEEDS.items():
        try:
            req = urllib.request.Request(URL.format(code), headers={"User-Agent": "Mozilla/5.0 (aquasalud-avisos)"})
            data = urllib.request.urlopen(req, timeout=30).read()
            pub, items = parse_feed(data, region)
            fuentes[region] = pub.isoformat()
            avisos += items
        except Exception as e:
            fallidas.append(region)
            print(f"ERROR {region}: {e}", file=sys.stderr)
            if region in prev.get("fuentes", {}):
                fuentes[region] = prev["fuentes"][region]
                avisos += [dict(a, inicio=parse_date(a["inicio"]), fin=parse_date(a["fin"]), _emitido=datetime.min.replace(tzinfo=timezone.utc))
                           for a in prev.get("avisos", []) if a.get("region") == region]
    if len(fallidas) == len(FEEDS):
        print("No se ha podido leer ningún feed; no se cambia nada.", file=sys.stderr)
        sys.exit(1)
    best = {}
    for a in avisos:
        if a["fin"] <= now:
            continue
        k = (a["zona"].lower(), a["fenomeno"].lower(), a["inicio"])
        if k not in best or a["_emitido"] >= best[k]["_emitido"]:
            best[k] = a
    res = sorted(best.values(), key=lambda a: (a["inicio"], a["zona"]))
    out = {
        "actualizado": now.astimezone(MADRID).isoformat(timespec="seconds"),
        "fuentes": fuentes,
        "avisos": [{"region": a["region"], "zona": a["zona"], "fenomeno": a["fenomeno"], "nivel": a["nivel"],
                    "inicio": a["inicio"].isoformat(), "fin": a["fin"].isoformat()} for a in res],
    }
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"OK: {len(out['avisos'])} avisos; fallidas: {', '.join(fallidas) or 'ninguna'}")


if __name__ == "__main__":
    main(*sys.argv[1:])
