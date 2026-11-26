#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Actualiza los enlaces de los canales de tus listas que vienen de iptv-org.

Que hace:
  * Lee mapa_global.csv: dice, para cada canal de cada lista, cual es su
    identificador (tvg-id) en iptv-org.
  * Descarga el index.m3u actual de iptv-org y busca cada canal POR ESE
    IDENTIFICADOR (no por el nombre, que cambia).
  * Si el canal esta: pone el enlace actual (y sus cabeceras) en tu lista.
    Tu nombre, tu tvg-id de guia, tu logo y tu grupo no se tocan.
  * Si el canal NO esta: conserva el ultimo enlace que tenia y lo anota en
    el informe. Si algun dia vuelve a aparecer, se actualiza solo, porque
    cada ejecucion vuelve a intentar todos los canales del mapa.
  * Los canales que no estan en el mapa (los que no vienen de iptv-org) no
    se tocan.
  * Opcional: con la API de iptv-org (streams.json / channels.json) anota en
    el informe enlaces alternativos y canales reemplazados. Nunca los usa
    automaticamente: solo sugiere.

Solo usa la biblioteca estandar de Python 3.
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse

INDEX_URL = "https://iptv-org.github.io/iptv/index.m3u"
STREAMS_URL = "https://iptv-org.github.io/api/streams.json"
CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"

MIN_INDEX = 5000          # un index con menos canales se considera descarga rota
MAX_MISSING = 0.5         # si falta mas de esta fraccion del mapa, algo va mal
HDR_ATTRS = ("http-user-agent", "http-referrer")


# ----------------------------------------------------------------- utilidades
def fetch(src):
    """Lee un archivo local o descarga una URL. Devuelve texto."""
    if os.path.exists(src):
        with open(src, "rb") as fh:
            data = fh.read()
    else:
        req = urllib.request.Request(src, headers={"User-Agent": "listas-iptv-actualizador/1.0"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read()
    return data.decode("utf-8", "replace")


def parse_m3u(text):
    """Devuelve (cabecera, entradas). Cada entrada: extinf, extra[], url."""
    header = ""
    entries = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#EXTM3U"):
            if not header:
                header = line.rstrip()
            continue
        if s.startswith("#EXTINF"):
            cur = {"extinf": line.rstrip(), "extra": [], "url": None}
            continue
        if s.startswith("#"):
            if cur is not None:
                cur["extra"].append(line.rstrip())
            continue
        if cur is not None:
            cur["url"] = s
            entries.append(cur)
            cur = None
    return header, entries


def attr(extinf, name):
    m = re.search(r'(?:^|\s)' + re.escape(name) + r'="([^"]*)"', extinf)
    return m.group(1) if m else ""


def chan_name(extinf):
    return extinf.rsplit(",", 1)[-1].strip()


def sync_headers(extinf, new_extinf):
    """Copia http-user-agent / http-referrer del canal nuevo al tuyo."""
    for a in HDR_ATTRS:
        extinf = re.sub(r'\s+' + re.escape(a) + r'="[^"]*"', "", extinf)
        v = attr(new_extinf, a)
        if v:
            if " group-title=" in extinf:
                extinf = extinf.replace(" group-title=", ' %s="%s" group-title=' % (a, v), 1)
            else:
                extinf = extinf.replace("#EXTINF:-1", '#EXTINF:-1 %s="%s"' % (a, v), 1)
    return extinf


def host(u):
    try:
        return urlparse(u).netloc or u[:40]
    except Exception:
        return u[:40]


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def load_json(src, label, warns):
    try:
        return json.loads(fetch(src))
    except Exception as e:  # la API es opcional
        warns.append("No se pudo leer %s (%s). Se sigue sin ella." % (label, type(e).__name__))
        return None


# ------------------------------------------------------------------ principal
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapa", default="mapa_global.csv")
    ap.add_argument("--index", default=INDEX_URL, help="URL o archivo local del index.m3u")
    ap.add_argument("--streams", default=STREAMS_URL, help="URL o archivo de streams.json ('' = no usar)")
    ap.add_argument("--channels", default=CHANNELS_URL, help="URL o archivo de channels.json ('' = no usar)")
    ap.add_argument("--carpeta", default=".", help="carpeta donde estan tus listas")
    ap.add_argument("--informe", default="informe_actualizacion.md")
    ap.add_argument("--dry-run", action="store_true", help="no escribe nada")
    args = ap.parse_args()

    warns = []

    # 1) index actual de iptv-org
    try:
        _, idx_entries = parse_m3u(fetch(args.index))
    except Exception as e:
        print("ERROR: no se pudo descargar el index de iptv-org (%s). No se cambia nada." % type(e).__name__)
        return 2
    index = {}
    for e in idx_entries:
        tid = attr(e["extinf"], "tvg-id")
        if tid and tid not in index:
            index[tid] = e
    if len(index) < MIN_INDEX:
        print("ERROR: el index solo trae %d canales (se esperaban mas de %d). Descarga rota; no se cambia nada."
              % (len(index), MIN_INDEX))
        return 2

    # 2) mapa: (archivo, nombre en tu lista) -> tvg-id en iptv-org
    mapa = {}
    with open(args.mapa, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            mapa[(row["archivo"], row["nombre_en_tu_lista"])] = row["tvg_id_iptv_org"]
    if not mapa:
        print("ERROR: el mapa esta vacio.")
        return 2

    # 3) API opcional (solo para sugerencias)
    streams = load_json(args.streams, "streams.json", warns) if args.streams else None
    channels = load_json(args.channels, "channels.json", warns) if args.channels else None
    alts = {}
    if isinstance(streams, list):
        for s in streams:
            if isinstance(s, dict) and s.get("channel") and s.get("url"):
                alts.setdefault(s["channel"], []).append(s)
    repl = {}
    if isinstance(channels, list):
        for c in channels:
            if isinstance(c, dict) and c.get("replaced_by"):
                repl[c.get("id")] = c["replaced_by"]

    # 4) procesar cada lista
    updated, missing, ok = [], [], 0
    outputs = {}
    archivos = sorted(set(a for a, _ in mapa))
    for arch in archivos:
        path = os.path.join(args.carpeta, arch)
        if not os.path.exists(path):
            warns.append("No encuentro %s en el repositorio; se omite." % arch)
            continue
        text = open(path, encoding="utf-8").read()
        header, entries = parse_m3u(text)
        changed = False
        vistos = set()
        for e in entries:
            nm = chan_name(e["extinf"])
            tid = mapa.get((arch, nm))
            if tid is None:
                continue
            vistos.add(nm)
            new = index.get(tid)
            if new is None:
                chan = tid.split("@")[0]
                info = {"archivo": arch, "canal": nm, "id": tid,
                        "alt": [(a.get("feed") or "-", a.get("quality") or "-", host(a["url"])) for a in alts.get(chan, [])[:3]],
                        "repl": repl.get(chan)}
                missing.append(info)
                continue
            if new["url"] == e["url"] and new["extra"] == e["extra"]:
                ok += 1
                continue
            updated.append({"archivo": arch, "canal": nm, "antes": host(e["url"]), "ahora": host(new["url"])})
            e["url"] = new["url"]
            e["extra"] = list(new["extra"])
            e["extinf"] = sync_headers(e["extinf"], new["extinf"])
            changed = True
        perdidos = sorted(n for (a, n) in mapa if a == arch and n not in vistos)
        if perdidos:
            warns.append("%s: el mapa menciona %d canal(es) que ya no estan con ese nombre en la lista (%s%s). "
                         "Si les cambiaste el nombre, actualiza mapa_global.csv."
                         % (arch, len(perdidos), "; ".join(perdidos[:5]), "..." if len(perdidos) > 5 else ""))
        if changed:
            out = [header or "#EXTM3U"]
            for e in entries:
                out.append(e["extinf"])
                out.extend(e["extra"])
                out.append(e["url"])
            outputs[path] = "\n".join(out) + "\n"

    total = ok + len(updated) + len(missing)
    if total and len(missing) / float(total) > MAX_MISSING:
        print("ERROR: faltan %d de %d canales en iptv-org. Parece un fallo; no se cambia nada." % (len(missing), total))
        return 2

    # 5) informe (sin fecha, para no crear cambios vacios cada dia)
    L = ["# Informe de actualizacion de listas", "",
         "- Canales revisados: %d" % total,
         "- Sin cambios: %d" % ok,
         "- Enlaces actualizados: %d" % len(updated),
         "- No estan hoy en iptv-org (se conserva el enlace anterior): %d" % len(missing), ""]
    if updated:
        L += ["## Enlaces actualizados", "", "| Lista | Canal | Antes | Ahora |", "|---|---|---|---|"]
        for u in updated:
            L.append("| %s | %s | %s | %s |" % (u["archivo"], u["canal"], u["antes"], u["ahora"]))
        L.append("")
    if missing:
        L += ["## No estan hoy en iptv-org", "",
              "Se conserva el ultimo enlace. Si vuelven a aparecer, se actualizaran solos.", ""]
        for m in missing:
            line = "- **%s** (%s) - `%s`" % (m["canal"], m["archivo"], m["id"])
            if m["repl"]:
                line += " - iptv-org dice que fue reemplazado por `%s`" % m["repl"]
            L.append(line)
            for a in m["alt"]:
                L.append("  - alternativa publicada: feed %s, calidad %s, servidor %s" % a)
        L.append("")
    if warns:
        L += ["## Avisos", ""] + ["- " + w for w in warns] + [""]
    report = "\n".join(L)

    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and not args.dry_run:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")

    if args.dry_run:
        print("(dry-run: no se escribio nada)")
        return 0
    for path, text in outputs.items():
        write_atomic(path, text)
    write_atomic(os.path.join(args.carpeta, args.informe), report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
