#!/usr/bin/env python3
"""Webapp KMZ → CSV pour Notion (avec geocodage)."""

import csv
import io
import json
import logging
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from defusedxml.ElementTree import fromstring as xml_fromstring

from flask import Flask, render_template, request, send_file

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max

KML_NS = "{http://www.opengis.net/kml/2.2}"
MAX_KML_SIZE = 10 * 1024 * 1024  # 10 MB max après décompression

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


def parse_placemarks(kml_text: str) -> list[dict]:
    root = xml_fromstring(kml_text)
    placemarks = []

    # Build placemark → folder name lookup in one pass
    folder_map = {}
    for folder_el in root.iter(f"{KML_NS}Folder"):
        fname = folder_el.findtext(f"{KML_NS}name", "").strip()
        for child in folder_el:
            if child.tag == f"{KML_NS}Placemark":
                folder_map[child] = fname

    for pm in root.iter(f"{KML_NS}Placemark"):
        name = pm.findtext(f"{KML_NS}name", "").strip()
        description = pm.findtext(f"{KML_NS}description", "").strip()
        folder = folder_map.get(pm, "")

        lat, lon, alt = "", "", ""
        point = pm.find(f".//{KML_NS}Point/{KML_NS}coordinates")
        line = pm.find(f".//{KML_NS}LineString/{KML_NS}coordinates")
        coords_text = ""
        if point is not None:
            coords_text = point.text.strip()
        elif line is not None:
            # LineString: multiple coord tuples separated by whitespace, take the first
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

        lieu = f"{lat}, {lon}" if lat and lon else ""

        placemarks.append({
            "Nom": name,
            "Description": description,
            "Dossier": folder,
            "Lieu": lieu,
            **extended,
        })

    return placemarks


# ── Geocoding ──────────────────────────────────────────────────────

def reverse_geocode(lat: str, lon: str) -> str:
    """Renvoie une adresse lisible depuis les coordonnées, ou '' en cas d'échec."""
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
        log.warning("Geocodage échoué pour %s,%s : %s", lat, lon, exc)
        return ""


def google_maps_link(lat: str, lon: str) -> str:
    if not lat or not lon:
        return ""
    return f"https://www.google.com/maps?q={lat},{lon}"


# ── CSV generation ─────────────────────────────────────────────────

def build_csv(placemarks: list[dict], with_geocoding: bool) -> str:
    if not placemarks:
        return ""

    rows = []
    for i, pm in enumerate(placemarks):
        row = dict(pm)
        lieu = row.get("Lieu", "")
        if lieu:
            lat, lon = lieu.split(", ", 1)
        else:
            lat, lon = "", ""
        if with_geocoding:
            if i > 0:
                time.sleep(1.1)
            row["Adresse"] = reverse_geocode(lat, lon)
        row["Google Maps"] = google_maps_link(lat, lon)
        rows.append(row)

    fieldnames = list(dict.fromkeys(k for row in rows for k in row))

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


# ── Routes ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/convert", methods=["POST"])
def convert():
    file = request.files.get("kmz_file")
    if not file or not file.filename.lower().endswith(".kmz"):
        return "Merci d'envoyer un fichier .kmz", 400

    with_geocoding = request.form.get("geocoding") == "on"

    kml_text = extract_kml(file.stream.read())
    placemarks = parse_placemarks(kml_text)

    if not placemarks:
        return "Aucun point trouve dans le fichier.", 400

    csv_text = build_csv(placemarks, with_geocoding)

    stem = Path(file.filename).stem
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in stem)
    output_name = (safe_name or "export") + ".csv"
    mem = io.BytesIO(csv_text.encode("utf-8"))
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=output_name)


if __name__ == "__main__":
    app.run(debug=False, port=5050)
