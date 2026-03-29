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
from datetime import date
from pathlib import Path

import certifi

from anthropic import Anthropic
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
    text = re.sub(r"<[^>]+>", " ", text)  # espace pour eviter de coller les mots
    return re.sub(r" {2,}", " ", text).strip()


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


# ── Deduction de metadonnees ──────────────────────────────────────

ESPECE_KEYWORDS = {
    "Poules": [
        "poules?", "poulets?", "gallus", "gallines?", "volailles?", "pondeuses?",
        "poulettes?", "coqs?", "chapons?", "poussins?", "oeufs?",
    ],
    "Dindes": ["dindes?", "dindons?", "dindonneaux?"],
    "Canards": ["canards?", "mulards?", "barbarie", "colverts?", "foie gras", "gavage"],
    "Oies": ["oies?", "oisons?"],
    "Pintades": ["pintades?", "pintadeaux?"],
    "Cailles": ["cailles?"],
    "Pigeons": ["pigeons?", "pigeonneaux?"],
    "Cochons": [
        "cochons?", "porcs?", "porcins?", "truies?", "verrats?",
        "porcelets?", "gorets?",
    ],
    "Bovins": [
        "bovins?", "vaches?", "taureaux?", "veaux?", "genisses?",
        "boeufs?", "taurillons?", "broutards?", "charolais",
        "limousins?", "blonde d'aquitaine", "montbeliardes?",
        "holsteins?", "salers", "aubracs?",
    ],
    "Ovins": ["ovins?", "moutons?", "brebis", "agneaux?", "beliers?"],
    "Caprins": ["caprins?", "chevres?", "boucs?", "chevreaux?", "cabris?"],
    "Lapins": ["lapins?", "cuniculture", "cuniculicole"],
    "Poissons": [
        "poissons?", "pisciculture", "truites?", "saumons?",
        "bars?", "daurades?", "aquaculture",
    ],
    "Equins": ["chevaux?", "cheval", "equins?", "juments?", "poulains?", "anes?"],
}

# Ordre d'insertion = priorite de detection (Abattoir > Couvoir > Elevage)
EXPLOITATION_KEYWORDS = {
    "Abattoir": [
        "abattoirs?", "abattage", "tueries?",
    ],
    "Couvoir": [
        "couvoirs?", "eclosion", "accouvage",
    ],
    "Élevage": [
        "elevages?", "élevages?", "fermes?", "exploitations?", "batiments?",
        "bâtiments?", "hangars?", "stabulations?", "porcheries?",
        "poulaillers?", "bergeries?", "chevreries?", "clapiers?",
        "etables?", "étables?",
    ],
}


def _build_keyword_patterns(keyword_dict: dict) -> list[tuple[str, re.Pattern]]:
    """Compile les mots-cles en regex avec word boundaries."""
    result = []
    for label, keywords in keyword_dict.items():
        pattern = re.compile(r"\b(?:" + "|".join(keywords) + r")\b")
        result.append((label, pattern))
    return result


_ESPECE_PATTERNS = _build_keyword_patterns(ESPECE_KEYWORDS)
_EXPLOITATION_PATTERNS = _build_keyword_patterns(EXPLOITATION_KEYWORDS)


def detect_espece(nom: str, description: str, dossier: str) -> str:
    """Deduit l'espece animale a partir du nom, description et dossier."""
    text = f"{nom} {description} {dossier}".lower()
    for espece, pattern in _ESPECE_PATTERNS:
        if pattern.search(text):
            return espece
    return ""


def detect_exploitation(nom: str, description: str, dossier: str) -> str:
    """Classe le lieu en Elevage, Couvoir ou Abattoir."""
    text = f"{nom} {description} {dossier}".lower()
    for type_expl, pattern in _EXPLOITATION_PATTERNS:
        if pattern.search(text):
            return type_expl
    return ""


def extract_url(description: str) -> str:
    """Extrait la premiere URL trouvee dans la description."""
    m = re.search(r'https?://[^\s<>"\']+', description)
    return m.group(0).rstrip(".,;:)") if m else ""


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


# ── Enrichissement ────────────────────────────────────────────────

def enrich_row(row: dict, carte_name: str, with_geocoding: bool,
               rate_limit: bool = False) -> dict:
    """Enrichit un placemark avec metadonnees, geocodage, liens."""
    row = dict(row)
    row["Carte"] = carte_name
    lat, lon = row.get("Latitude", ""), row.get("Longitude", "")

    if with_geocoding:
        if rate_limit:
            time.sleep(1.1)
        row["Adresse"] = reverse_geocode(lat, lon)

    row["Google Maps"] = google_maps_link(lat, lon)
    row["Date d'import"] = date.today().isoformat()

    nom = row.get("Nom", "")
    desc = row.get("Description", "")
    dossier = row.get("Dossier", "")
    row["Espèce"] = detect_espece(nom, desc, dossier)
    row["URL"] = extract_url(desc)
    row["Exploitation"] = detect_exploitation(nom, desc, dossier)

    return row


def enrich_placemarks(placemarks, carte_name, with_geocoding):
    return [
        enrich_row(pm, carte_name, with_geocoding, rate_limit=(i > 0))
        for i, pm in enumerate(placemarks)
    ]


# ── CSV generation ────────────────────────────────────────────────

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
    "Date d'import", "Espèce", "URL", "Exploitation",
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
    "Date d'import": {"date": {}},
    "Espèce": {"select": {}},
    "URL": {"url": {}},
    "Exploitation": {"select": {}},
}

NOTION_TYPE_MAP = {
    "title": "titre",
    "rich_text": "texte",
    "number": "nombre",
    "select": "choix unique",
    "multi_select": "choix multiples",
    "date": "date",
    "url": "URL",
    "email": "email",
    "phone_number": "telephone",
    "checkbox": "case a cocher",
    "status": "statut",
}

WRITABLE_TYPES = set(NOTION_TYPE_MAP.keys())


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


def read_db_schema(notion, database_id: str) -> dict:
    """Lit le schema d'une base Notion et retourne {nom_prop: {type, label, options?}}."""
    db = notion.databases.retrieve(database_id)
    schema = {}
    for name, prop in db["properties"].items():
        ptype = prop["type"]
        options = None
        if ptype in ("select", "multi_select"):
            options = [opt["name"] for opt in prop[ptype].get("options", [])]
        schema[name] = {"type": ptype, "label": NOTION_TYPE_MAP.get(ptype, ptype)}
        if options:
            schema[name]["options"] = options
    return schema


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


def create_notion_page(notion, database_id: str, row: dict, schema: dict | None = None):
    """Cree une page Notion. Utilise le schema dynamique si fourni,
    sinon fallback sur le mapping hardcode pour les champs standard KMZ."""
    props = {}

    if schema:
        for name, value in row.items():
            if name not in schema or value is None or value == "":
                continue
            ptype = schema[name]["type"]

            if ptype == "title":
                props[name] = {"title": [{"text": {"content": str(value)[:2000]}}]}
            elif ptype == "rich_text":
                props[name] = {"rich_text": [{"text": {"content": str(value)[:2000]}}]}
            elif ptype == "number":
                try:
                    props[name] = {"number": float(value)}
                except (ValueError, TypeError):
                    pass
            elif ptype == "select":
                props[name] = {"select": {"name": str(value)[:100]}}
            elif ptype == "multi_select":
                if isinstance(value, list):
                    props[name] = {"multi_select": [{"name": str(v)[:100]} for v in value]}
                else:
                    props[name] = {"multi_select": [{"name": str(value)[:100]}]}
            elif ptype == "url":
                props[name] = {"url": str(value)}
            elif ptype == "email":
                props[name] = {"email": str(value)}
            elif ptype == "phone_number":
                props[name] = {"phone_number": str(value)}
            elif ptype == "date":
                props[name] = {"date": {"start": str(value)}}
            elif ptype == "checkbox":
                props[name] = {"checkbox": bool(value)}

        if "Date d'import" in schema and "Date d'import" not in row:
            props["Date d'import"] = {"date": {"start": date.today().isoformat()}}
    else:
        # Fallback hardcode pour l'import KMZ (schema standard connu)
        nom = row.get("Nom", "Sans nom")
        props["Nom"] = {"title": [{"text": {"content": nom[:2000]}}]}

        for field in ("Description", "Adresse"):
            val = row.get(field, "")
            if val:
                props[field] = {"rich_text": [{"text": {"content": val[:2000]}}]}

        for field in ("Dossier", "Couleur", "Carte", "Espèce", "Exploitation"):
            val = row.get(field, "")
            if val:
                props[field] = {"select": {"name": val[:100]}}

        for field in ("Latitude", "Longitude"):
            val = row.get(field, "")
            if val:
                try:
                    props[field] = {"number": float(val)}
                except ValueError:
                    pass

        for field in ("Google Maps", "URL"):
            val = row.get(field, "")
            if val:
                props[field] = {"url": val}

        date_import = row.get("Date d'import", "")
        if date_import:
            props["Date d'import"] = {"date": {"start": date_import}}

        for k, v in row.items():
            if k not in STANDARD_FIELDS and v:
                props[k] = {"rich_text": [{"text": {"content": str(v)[:2000]}}]}

    notion.pages.create(parent={"database_id": database_id}, properties=props)


# ── NDJSON helpers ─────────────────────────────────────────────────

def _ndjson_line(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False) + "\n"


def _ndjson_error(message: str):
    return Response(
        _ndjson_line({"step": "error", "message": message}),
        mimetype="application/x-ndjson",
        status=400,
    )


def _ndjson_response(generator):
    """Wrap un generateur dans une Response streaming NDJSON."""
    return Response(
        stream_with_context(generator),
        mimetype="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


def _import_rows_to_notion(notion, database_id, rows, schema=None, extra=None):
    """Boucle partagee : cree des pages Notion, yield la progression NDJSON."""
    created = 0
    errors = []

    for i, row in enumerate(rows):
        nom = row.get("Nom", "?")
        try:
            create_notion_page(notion, database_id, row, schema)
            created += 1
            yield _ndjson_line({
                "step": "imported",
                "current": i + 1,
                "total": len(rows),
                "name": nom,
            })
        except Exception as e:
            errors.append(f"{nom}: {e}")
            log.warning("Erreur Notion pour %s: %s", nom, e)
            yield _ndjson_line({
                "step": "import_error",
                "current": i + 1,
                "total": len(rows),
                "name": nom,
                "message": str(e)[:200],
            })

    done = {"step": "done", "created": created, "errors": errors}
    if extra:
        done.update(extra)
    yield _ndjson_line(done)


# ── Routes ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    has_token = bool(os.environ.get("NOTION_TOKEN"))
    models = [{"id": mid, "label": info["label"]} for mid, info in CLAUDE_MODELS.items()]
    return render_template("index.html", has_token=has_token, models=models)


@app.route("/convert", methods=["POST"])
def convert():
    files = request.files.getlist("kmz_file")
    files = [f for f in files if f and f.filename.lower().endswith(".kmz")]
    if not files:
        return "Merci d'envoyer au moins un fichier .kmz", 400

    with_geocoding = request.form.get("geocoding") == "on"

    all_rows = []
    for file in files:
        kmz_bytes = file.stream.read()
        kml_text = extract_kml(kmz_bytes)
        carte_name = extract_map_name(kml_text) or Path(file.filename).stem
        placemarks = parse_placemarks(kml_text)
        if placemarks:
            all_rows.extend(enrich_placemarks(placemarks, carte_name, with_geocoding))

    if not all_rows:
        return "Aucun point trouve dans les fichiers.", 400

    csv_text = build_csv(all_rows)

    if len(files) == 1:
        stem = Path(files[0].filename).stem
        safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in stem)
        output_name = (safe_name or "export") + ".csv"
    else:
        output_name = "export_multi.csv"

    mem = io.BytesIO(csv_text.encode("utf-8"))
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=output_name)


@app.route("/import-notion", methods=["POST"])
def import_notion_route():
    files = request.files.getlist("kmz_file")
    files = [f for f in files if f and f.filename.lower().endswith(".kmz")]
    if not files:
        return _ndjson_error("Merci d'envoyer au moins un fichier .kmz")

    token = (request.form.get("notion_token", "").strip()
             or os.environ.get("NOTION_TOKEN", ""))
    database_url = request.form.get("notion_database", "").strip()

    if not token:
        return _ndjson_error("Token Notion manquant")
    if not database_url:
        return _ndjson_error("URL de la base Notion manquante")

    with_geocoding = request.form.get("geocoding") == "on"

    all_cartes = []
    for file in files:
        try:
            kmz_bytes = file.stream.read()
            kml_text = extract_kml(kmz_bytes)
            carte_name = extract_map_name(kml_text) or Path(file.filename).stem
            placemarks = parse_placemarks(kml_text)
            if placemarks:
                all_cartes.append((carte_name, placemarks))
        except Exception as e:
            log.warning("Erreur parsing %s: %s", file.filename, e)

    if not all_cartes:
        return _ndjson_error("Aucun point trouve dans les fichiers.")

    total_points = sum(len(pms) for _, pms in all_cartes)

    def generate():
        notion = NotionClient(auth=token)
        database_id = parse_database_id(database_url)
        carte_names = ", ".join(c for c, _ in all_cartes)

        yield _ndjson_line({
            "step": "init",
            "total": total_points,
            "carte": carte_names,
            "files": len(all_cartes),
        })

        extra_fields = set()
        for _, placemarks in all_cartes:
            for pm in placemarks:
                extra_fields.update(k for k in pm if k not in STANDARD_FIELDS)

        try:
            ensure_db_properties(notion, database_id, extra_fields)
        except Exception as e:
            yield _ndjson_line({"step": "error", "message": f"Erreur config base Notion : {e}"})
            return

        # Enrichir tous les placemarks avec geocodage streaming
        all_rows = []
        global_idx = 0
        for carte_name, placemarks in all_cartes:
            for pm in placemarks:
                row = enrich_row(pm, carte_name, with_geocoding,
                                 rate_limit=(global_idx > 0 and with_geocoding))
                if with_geocoding:
                    yield _ndjson_line({
                        "step": "geocode",
                        "current": global_idx + 1,
                        "total": total_points,
                        "name": row.get("Nom", ""),
                        "adresse": row.get("Adresse", "")[:100],
                    })
                all_rows.append(row)
                global_idx += 1

        yield from _import_rows_to_notion(
            notion, database_id, all_rows,
            extra={"carte": carte_names},
        )

    return _ndjson_response(generate())


# ── Schema Notion + Ajout intelligent ─────────────────────────────

@app.route("/db-schema", methods=["POST"])
def db_schema_route():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("notion_token", "").strip()
    database_url = data.get("notion_database", "").strip()

    if not token:
        return jsonify({"error": "Token Notion manquant"}), 400
    if not database_url:
        return jsonify({"error": "URL de la base Notion manquante"}), 400

    try:
        notion = NotionClient(auth=token)
        database_id = parse_database_id(database_url)
        schema = read_db_schema(notion, database_id)
        return jsonify({"schema": schema})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def build_claude_prompt(schema: dict, user_query: str) -> str:
    """Construit le prompt pour Claude avec le schema de la base."""
    schema_desc = []
    for name, info in schema.items():
        line = f'- "{name}" ({info["label"]})'
        if info.get("options"):
            line += f' — valeurs existantes : {", ".join(info["options"][:20])}'
        schema_desc.append(line)

    return f"""Tu es un assistant qui recherche des informations sur des lieux (elevages, abattoirs, couvoirs, entreprises agroalimentaires) pour les ajouter dans une base de donnees.

Voici le schema de la base Notion cible :
{chr(10).join(schema_desc)}

L'utilisateur te demande :
{user_query}

INSTRUCTIONS :
1. Utilise l'outil web_search pour chercher des informations sur le(s) lieu(x) demande(s).
2. Pour CHAQUE lieu trouve, retourne un objet JSON avec les proprietes de la base remplies au mieux.
3. Pour les champs "select" avec des options existantes, utilise une option existante si elle correspond, sinon propose une nouvelle valeur.
4. Pour les coordonnees (Latitude/Longitude), cherche-les sur le web.
5. Pour le champ "Google Maps", genere le lien https://www.google.com/maps?q=LAT,LON
6. Le champ "Date d'import" sera rempli automatiquement, ne le remplis pas.
7. Ne remplis PAS les champs pour lesquels tu n'as aucune information fiable.

Reponds UNIQUEMENT avec un JSON valide, sous la forme d'un tableau d'objets :
[{{"Nom": "...", "Latitude": 48.123, ...}}]

Pas de texte avant ou apres le JSON. Chaque objet represente un lieu a ajouter."""


# Prix par million de tokens (USD) — maj: mai 2025
CLAUDE_MODELS = {
    "claude-sonnet-4-20250514": {
        "label": "Sonnet 4",
        "input": 3.0,
        "output": 15.0,
    },
    "claude-haiku-3-5-20241022": {
        "label": "Haiku 3.5",
        "input": 0.80,
        "output": 4.0,
    },
    "claude-opus-4-20250514": {
        "label": "Opus 4",
        "input": 15.0,
        "output": 75.0,
    },
}

DEFAULT_MODEL = "claude-sonnet-4-20250514"


def compute_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Calcule le cout en USD a partir de l'usage tokens."""
    pricing = CLAUDE_MODELS.get(model_id, CLAUDE_MODELS[DEFAULT_MODEL])
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


def call_claude_smart_add(api_key: str, schema: dict, user_query: str,
                          model: str = DEFAULT_MODEL):
    """Appelle Claude API avec web search pour trouver les infos.

    Retourne (rows, cost_usd) — rows est une liste de dicts,
    cost_usd le cout total estime en dollars.

    web_search_20250305 is a server-managed tool: the API executes searches
    internally. We run an agentic loop in case the model needs multiple turns.
    """
    client = Anthropic(api_key=api_key)
    writable_schema = {k: v for k, v in schema.items() if v["type"] in WRITABLE_TYPES}
    prompt = build_claude_prompt(writable_schema, user_query)
    messages = [{"role": "user", "content": prompt}]

    total_input = 0
    total_output = 0

    for _ in range(10):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}],
            messages=messages,
        )
        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens
        if response.stop_reason == "end_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": "Continue."})
    else:
        log.warning("Claude smart-add: boucle agentic interrompue apres 10 tours")

    cost_usd = compute_cost(model, total_input, total_output)
    log.info("Claude smart-add: %d input + %d output tokens, cout ~$%.4f",
             total_input, total_output, cost_usd)

    text_parts = [block.text for block in response.content if block.type == "text"]
    full_text = "\n".join(text_parts).strip()

    try:
        return json.loads(full_text), cost_usd
    except json.JSONDecodeError:
        pass

    match = re.search(r'\[.*\]', full_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0)), cost_usd
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Claude n'a pas retourne de JSON valide. Reponse : {full_text[:500]}")


@app.route("/models", methods=["GET"])
def models_route():
    return jsonify([
        {"id": mid, "label": info["label"]}
        for mid, info in CLAUDE_MODELS.items()
    ])


@app.route("/smart-add", methods=["POST"])
def smart_add_route():
    token = request.form.get("notion_token", "").strip()
    database_url = request.form.get("notion_database", "").strip()
    claude_key = request.form.get("claude_key", "").strip()
    user_query = request.form.get("query", "").strip()
    model = request.form.get("model", "").strip() or DEFAULT_MODEL
    if model not in CLAUDE_MODELS:
        model = DEFAULT_MODEL
    model_label = CLAUDE_MODELS[model]["label"]

    md_file = request.files.get("md_file")
    if md_file and md_file.filename:
        md_content = md_file.stream.read().decode("utf-8")
        if user_query:
            user_query = f"{user_query}\n\nContenu du fichier {md_file.filename} :\n{md_content}"
        else:
            user_query = f"Trouve tous les lieux mentionnes dans ce document et ajoute-les dans la base :\n\n{md_content}"

    if not token:
        return _ndjson_error("Token Notion manquant (configurer dans Preferences)")
    if not database_url:
        return _ndjson_error("URL de la base Notion manquante (configurer dans Preferences)")
    if not claude_key:
        return _ndjson_error("Cle API Claude manquante (configurer dans Preferences)")
    if not user_query:
        return _ndjson_error("Aucune requete ou fichier fourni")

    def generate():
        yield _ndjson_line({"step": "init", "message": "Lecture du schema de la base Notion..."})

        try:
            notion = NotionClient(auth=token)
            database_id = parse_database_id(database_url)
            schema = read_db_schema(notion, database_id)
        except Exception as e:
            yield _ndjson_line({"step": "error", "message": f"Erreur lecture base Notion : {e}"})
            return

        yield _ndjson_line({
            "step": "schema_read",
            "message": f"Schema lu : {len(schema)} proprietes",
            "properties": list(schema.keys()),
        })

        yield _ndjson_line({"step": "searching",
                            "message": f"Recherche avec {model_label}..."})

        try:
            rows, cost_usd = call_claude_smart_add(claude_key, schema, user_query, model)
        except Exception as e:
            yield _ndjson_line({"step": "error", "message": f"Erreur Claude API : {e}"})
            return

        if not isinstance(rows, list):
            rows = [rows]

        total = len(rows)
        yield _ndjson_line({"step": "found", "total": total,
                            "message": f"{total} lieu(x) trouve(s)"})

        yield from _import_rows_to_notion(
            notion, database_id, rows, schema=schema,
            extra={"cost_usd": round(cost_usd, 4), "model": model_label},
        )

    return _ndjson_response(generate())


if __name__ == "__main__":
    app.run(debug=False, port=5050)
