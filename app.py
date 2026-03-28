#!/usr/bin/env python3
"""Webapp KMZ → Notion / CSV."""

import csv
import io
import json
import logging
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import certifi

from defusedxml.ElementTree import fromstring as xml_fromstring
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
from notion_client import Client as NotionClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max

KML_NS = "{http://www.opengis.net/kml/2.2}"
MAX_KML_SIZE = 10 * 1024 * 1024  # 10 MB max after decompression

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_HEADERS = {"User-Agent": "kmz-to-csv-webapp/1.0"}
SSL_CTX = ssl.create_default_context(cafile=certifi.where())


# ── KMZ / KML parsing ─────────────────────────────────────────────

def extract_kml(kmz_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith(".kml"):
                info = zf.getinfo(name)
                if info.file_size > MAX_KML_SIZE:
                    raise ValueError("Fichier KML trop volumineux")
                return zf.read(name).decode("utf-8")
    raise ValueError("Aucun fichier .kml dans le .kmz")


def extract_map_name(kml_text: str) -> str:
    root = xml_fromstring(kml_text)
    doc = root.find(f".//{KML_NS}Document")
    if doc is not None:
        name = doc.findtext(f"{KML_NS}name", "").strip()
        if name:
            return name
    return ""


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


# Google Maps icon style format: "icon-{ID}-{HEX_COLOR}[-normal|-highlight|-nodesc...]"
COLOR_NAMES = {
    "000000": "Noir", "0288D1": "Bleu", "0F9D58": "Vert",
    "1A237E": "Bleu fonce", "3949AB": "Indigo", "7CB342": "Vert clair",
    "9C27B0": "Violet", "A52714": "Rouge fonce", "BDBDBD": "Gris",
    "C2185B": "Rose", "E65100": "Orange", "F57F17": "Jaune fonce",
    "FF5252": "Rouge", "FFD600": "Jaune", "FFEA00": "Jaune vif",
    "795548": "Marron",
}


def extract_color(style_url: str) -> str:
    """Extrait la couleur depuis un styleUrl KML (ex: '#icon-1899-FF5252' → 'Rouge')."""
    m = re.search(r"icon-\d+-([0-9A-Fa-f]{6})", style_url)
    if not m:
        return ""
    hex_color = m.group(1).upper()
    return COLOR_NAMES.get(hex_color, f"#{hex_color}")


def parse_placemarks(kml_text: str) -> list[dict]:
    root = xml_fromstring(kml_text)
    placemarks = []

    folder_map = {}
    for folder_el in root.iter(f"{KML_NS}Folder"):
        fname = folder_el.findtext(f"{KML_NS}name", "").strip()
        for child in folder_el:
            if child.tag == f"{KML_NS}Placemark":
                folder_map[child] = fname

    for pm in root.iter(f"{KML_NS}Placemark"):
        name = pm.findtext(f"{KML_NS}name", "").strip()
        description = strip_html(pm.findtext(f"{KML_NS}description", ""))
        folder = folder_map.get(pm, "")

        lat, lon = "", ""
        point = pm.find(f".//{KML_NS}Point/{KML_NS}coordinates")
        line = pm.find(f".//{KML_NS}LineString/{KML_NS}coordinates")
        coords_text = ""
        if point is not None:
            coords_text = point.text.strip()
        elif line is not None:
            coords_text = line.text.strip().split()[0]
        if coords_text:
            parts = coords_text.split(",")
            if len(parts) >= 2:
                lon, lat = parts[0].strip(), parts[1].strip()

        style_url = pm.findtext(f"{KML_NS}styleUrl", "")
        couleur = extract_color(style_url)

        extended = {}
        for tag, get_val in (
            (f"{KML_NS}Data", lambda el: el.findtext(f"{KML_NS}value", "")),
            (f"{KML_NS}SimpleData", lambda el: el.text or ""),
        ):
            for el in pm.iter(tag):
                key = el.get("name", "")
                if key:
                    extended[key] = get_val(el).strip()

        # Ignorer les placemarks sans coordonnees (notes, fragments, annotations)
        if not lat:
            continue

        placemarks.append({
            "Nom": name,
            "Description": description,
            "Dossier": folder,
            "Couleur": couleur,
            "Latitude": lat,
            "Longitude": lon,
            **extended,
        })

    return placemarks


# ── Geocoding ──────────────────────────────────────────────────────

def reverse_geocode(lat: str, lon: str) -> str:
    if not lat or not lon:
        log.info("Geocodage ignore : coordonnees vides (lat=%r, lon=%r)", lat, lon)
        return ""
    params = urllib.parse.urlencode({
        "lat": lat, "lon": lon, "format": "json", "zoom": 18,
        "addressdetails": 1, "accept-language": "fr",
    })
    url = f"{NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers=NOMINATIM_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
            data = json.loads(resp.read())
            addr = data.get("display_name", "")
            log.info("Geocodage OK pour %s,%s : %s", lat, lon, addr[:80])
            return addr
    except Exception as exc:
        log.warning("Geocodage echoue pour %s,%s : %s", lat, lon, exc)
        return ""


def google_maps_link(lat: str, lon: str) -> str:
    if not lat or not lon:
        return ""
    return f"https://www.google.com/maps?q={lat},{lon}"


# ── CSV generation ─────────────────────────────────────────────────

def enrich_placemarks(placemarks, carte_name, with_geocoding):
    rows = []
    for i, pm in enumerate(placemarks):
        row = dict(pm)
        row["Carte"] = carte_name
        lat, lon = row.get("Latitude", ""), row.get("Longitude", "")
        if with_geocoding:
            if i > 0:
                time.sleep(1.1)
            row["Adresse"] = reverse_geocode(lat, lon)
        row["Google Maps"] = google_maps_link(lat, lon)
        rows.append(row)
    return rows


def build_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    fieldnames = list(dict.fromkeys(k for row in rows for k in row))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


# ── Notion integration ─────────────────────────────────────────────

STANDARD_FIELDS = {
    "Nom", "Description", "Dossier", "Couleur", "Carte",
    "Latitude", "Longitude",
    "Adresse", "Google Maps",
}

DB_PROPERTIES = {
    "Description": {"rich_text": {}},
    "Dossier": {"select": {}},
    "Couleur": {"select": {}},
    "Carte": {"select": {}},
    "Latitude": {"number": {"format": "number"}},
    "Longitude": {"number": {"format": "number"}},
    "Adresse": {"rich_text": {}},
    "Google Maps": {"url": {}},
}


def parse_database_id(url_or_id: str) -> str:
    url_or_id = url_or_id.strip().rstrip("/")
    if "/" in url_or_id:
        segment = url_or_id.split("?")[0].split("/")[-1]
        clean = segment.replace("-", "")
        if len(clean) >= 32:
            hex_part = clean[-32:]
            if all(c in "0123456789abcdef" for c in hex_part):
                return hex_part
        return segment
    return url_or_id.replace("-", "")


def ensure_db_properties(notion, database_id, extra_fields):
    db = notion.databases.retrieve(database_id)
    existing = set(db["properties"].keys())

    updates = {}
    for name, config in DB_PROPERTIES.items():
        if name not in existing:
            updates[name] = config
    for field in extra_fields:
        if field not in existing and field not in updates:
            updates[field] = {"rich_text": {}}

    if updates:
        notion.databases.update(database_id, properties=updates)


def create_notion_page(notion, database_id, row):
    props = {}

    # Title
    nom = row.get("Nom", "Sans nom")
    props["Nom"] = {"title": [{"text": {"content": nom[:2000]}}]}

    # Rich text
    for field in ("Description", "Adresse"):
        val = row.get(field, "")
        if val:
            props[field] = {"rich_text": [{"text": {"content": val[:2000]}}]}

    # Select
    for field in ("Dossier", "Couleur", "Carte"):
        val = row.get(field, "")
        if val:
            props[field] = {"select": {"name": val[:100]}}

    # Numbers
    for field in ("Latitude", "Longitude"):
        val = row.get(field, "")
        if val:
            try:
                props[field] = {"number": float(val)}
            except ValueError:
                pass

    # URL
    gm = row.get("Google Maps", "")
    if gm:
        props["Google Maps"] = {"url": gm}

    # Extended KML fields
    for k, v in row.items():
        if k not in STANDARD_FIELDS and v:
            props[k] = {"rich_text": [{"text": {"content": str(v)[:2000]}}]}

    notion.pages.create(parent={"database_id": database_id}, properties=props)


# ── Routes ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    has_token = bool(os.environ.get("NOTION_TOKEN"))
    return render_template("index.html", has_token=has_token)


@app.route("/convert", methods=["POST"])
def convert():
    file = request.files.get("kmz_file")
    if not file or not file.filename.lower().endswith(".kmz"):
        return "Merci d'envoyer un fichier .kmz", 400

    with_geocoding = request.form.get("geocoding") == "on"

    kmz_bytes = file.stream.read()
    kml_text = extract_kml(kmz_bytes)
    carte_name = extract_map_name(kml_text) or Path(file.filename).stem
    placemarks = parse_placemarks(kml_text)

    if not placemarks:
        return "Aucun point trouve dans le fichier.", 400

    rows = enrich_placemarks(placemarks, carte_name, with_geocoding)
    csv_text = build_csv(rows)

    stem = Path(file.filename).stem
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in stem)
    output_name = (safe_name or "export") + ".csv"
    mem = io.BytesIO(csv_text.encode("utf-8"))
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=output_name)


@app.route("/import-notion", methods=["POST"])
def import_notion_route():
    file = request.files.get("kmz_file")
    if not file or not file.filename.lower().endswith(".kmz"):
        return _ndjson_error("Merci d'envoyer un fichier .kmz")

    token = (request.form.get("notion_token", "").strip()
             or os.environ.get("NOTION_TOKEN", ""))
    database_url = request.form.get("notion_database", "").strip()

    if not token:
        return _ndjson_error("Token Notion manquant")
    if not database_url:
        return _ndjson_error("URL de la base Notion manquante")

    with_geocoding = request.form.get("geocoding") == "on"

    try:
        kmz_bytes = file.stream.read()
        kml_text = extract_kml(kmz_bytes)
        carte_name = extract_map_name(kml_text) or Path(file.filename).stem
        placemarks = parse_placemarks(kml_text)
    except Exception as e:
        return _ndjson_error(str(e))

    if not placemarks:
        return _ndjson_error("Aucun point trouve dans le fichier.")

    def generate():
        total = len(placemarks)
        notion = NotionClient(auth=token)
        database_id = parse_database_id(database_url)

        # Ensure properties
        yield _ndjson_line({"step": "init", "total": total, "carte": carte_name})

        extra_fields = set()
        for pm in placemarks:
            extra_fields.update(k for k in pm if k not in STANDARD_FIELDS)

        try:
            ensure_db_properties(notion, database_id, extra_fields)
        except Exception as e:
            yield _ndjson_line({"step": "error", "message": f"Erreur config base Notion : {e}"})
            return

        created = 0
        errors = []

        for i, pm in enumerate(placemarks):
            row = dict(pm)
            row["Carte"] = carte_name
            lat, lon = row.get("Latitude", ""), row.get("Longitude", "")

            # Geocoding
            if with_geocoding:
                if i > 0:
                    time.sleep(1.1)
                addr = reverse_geocode(lat, lon)
                row["Adresse"] = addr
                yield _ndjson_line({
                    "step": "geocode",
                    "current": i + 1,
                    "total": total,
                    "name": row.get("Nom", ""),
                    "adresse": addr[:100],
                })

            row["Google Maps"] = google_maps_link(lat, lon)

            # Create Notion page
            try:
                create_notion_page(notion, database_id, row)
                created += 1
                yield _ndjson_line({
                    "step": "imported",
                    "current": i + 1,
                    "total": total,
                    "name": row.get("Nom", ""),
                })
            except Exception as e:
                err = f"{row.get('Nom', '?')}: {e}"
                errors.append(err)
                log.warning("Erreur Notion pour %s: %s", row.get("Nom"), e)
                yield _ndjson_line({
                    "step": "import_error",
                    "current": i + 1,
                    "total": total,
                    "name": row.get("Nom", ""),
                    "message": str(e)[:200],
                })

        yield _ndjson_line({
            "step": "done",
            "created": created,
            "errors": errors,
            "carte": carte_name,
        })

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


def _ndjson_line(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False) + "\n"


def _ndjson_error(message: str):
    return Response(
        _ndjson_line({"step": "error", "message": message}),
        mimetype="application/x-ndjson",
        status=400,
    )


if __name__ == "__main__":
    app.run(debug=False, port=5050)
