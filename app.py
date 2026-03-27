#!/usr/bin/env python3
"""Webapp KMZ → Notion / CSV."""

import csv
import io
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from defusedxml.ElementTree import fromstring as xml_fromstring
from flask import Flask, jsonify, render_template, request, send_file
from notion_client import Client as NotionClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max

KML_NS = "{http://www.opengis.net/kml/2.2}"
MAX_KML_SIZE = 10 * 1024 * 1024  # 10 MB max after decompression

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_HEADERS = {"User-Agent": "kmz-to-csv-webapp/1.0"}


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

        lat, lon, alt = "", "", ""
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
                alt = parts[2].strip() if len(parts) >= 3 else ""

        extended = {}
        for tag, get_val in (
            (f"{KML_NS}Data", lambda el: el.findtext(f"{KML_NS}value", "")),
            (f"{KML_NS}SimpleData", lambda el: el.text or ""),
        ):
            for el in pm.iter(tag):
                key = el.get("name", "")
                if key:
                    extended[key] = get_val(el).strip()

        placemarks.append({
            "Nom": name,
            "Description": description,
            "Dossier": folder,
            "Latitude": lat,
            "Longitude": lon,
            "Altitude": alt,
            **extended,
        })

    return placemarks


# ── Geocoding ──────────────────────────────────────────────────────

def reverse_geocode(lat: str, lon: str) -> str:
    if not lat or not lon:
        return ""
    params = urllib.parse.urlencode({
        "lat": lat, "lon": lon, "format": "json", "zoom": 18,
        "addressdetails": 1, "accept-language": "fr",
    })
    url = f"{NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers=NOMINATIM_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("display_name", "")
    except Exception as exc:
        log.warning("Geocodage echoue pour %s,%s : %s", lat, lon, exc)
        return ""


def google_maps_link(lat: str, lon: str) -> str:
    if not lat or not lon:
        return ""
    return f"https://www.google.com/maps?q={lat},{lon}"


# ── Enrichment ─────────────────────────────────────────────────────

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


# ── CSV generation ─────────────────────────────────────────────────

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
    "Nom", "Description", "Dossier", "Carte",
    "Latitude", "Longitude", "Altitude",
    "Adresse", "Google Maps",
}

DB_PROPERTIES = {
    "Description": {"rich_text": {}},
    "Dossier": {"select": {}},
    "Carte": {"select": {}},
    "Latitude": {"number": {"format": "number"}},
    "Longitude": {"number": {"format": "number"}},
    "Altitude": {"number": {"format": "number"}},
    "Adresse": {"rich_text": {}},
    "Google Maps": {"url": {}},
}


def parse_database_id(url_or_id: str) -> str:
    url_or_id = url_or_id.strip().rstrip("/")
    if "/" in url_or_id:
        segment = url_or_id.split("?")[0].split("/")[-1]
        # "Page-Title-abc123def456..." → extract last 32 hex chars
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
    for field in ("Dossier", "Carte"):
        val = row.get(field, "")
        if val:
            props[field] = {"select": {"name": val[:100]}}

    # Numbers
    for field in ("Latitude", "Longitude", "Altitude"):
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


def import_to_notion(token, database_url, rows):
    notion = NotionClient(auth=token)
    database_id = parse_database_id(database_url)

    extra_fields = set()
    for row in rows:
        extra_fields.update(k for k in row if k not in STANDARD_FIELDS)

    ensure_db_properties(notion, database_id, extra_fields)

    created = 0
    errors = []
    for row in rows:
        try:
            create_notion_page(notion, database_id, row)
            created += 1
        except Exception as e:
            errors.append(f"{row.get('Nom', '?')}: {e}")
            log.warning("Erreur Notion pour %s: %s", row.get("Nom"), e)

    return created, errors


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
        return jsonify({"error": "Merci d'envoyer un fichier .kmz"}), 400

    token = (request.form.get("notion_token", "").strip()
             or os.environ.get("NOTION_TOKEN", ""))
    database_url = request.form.get("notion_database", "").strip()

    if not token:
        return jsonify({"error": "Token Notion manquant"}), 400
    if not database_url:
        return jsonify({"error": "URL de la base Notion manquante"}), 400

    with_geocoding = request.form.get("geocoding") == "on"

    try:
        kmz_bytes = file.stream.read()
        kml_text = extract_kml(kmz_bytes)
        carte_name = extract_map_name(kml_text) or Path(file.filename).stem
        placemarks = parse_placemarks(kml_text)

        if not placemarks:
            return jsonify({"error": "Aucun point trouve dans le fichier."}), 400

        rows = enrich_placemarks(placemarks, carte_name, with_geocoding)
        created, errors = import_to_notion(token, database_url, rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"created": created, "errors": errors, "carte": carte_name})


if __name__ == "__main__":
    app.run(debug=False, port=5050)
