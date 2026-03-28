# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projet

Webapp Flask qui importe des fichiers KMZ (cartes Google Maps) dans une base Notion, avec export CSV en option. Contexte L214 : les lieux sont des elevages, couvoirs, abattoirs avec des especes animales.

## Commandes

```bash
python3 -m pip install -r requirements.txt
python3 app.py                # http://localhost:5050
# Optionnel : export NOTION_TOKEN=ntn_...
```

Pas de tests, pas de linter, pas de build step.

## Architecture

Fichier unique `app.py` (~580 lignes) avec 7 sections delimitees par des commentaires `# ──` :

1. **KMZ/KML parsing** — extraction du .kml depuis le .kmz, parsing XML des Placemarks (Point, LineString, Data, SimpleData), extraction couleur depuis styleUrl
2. **Deduction metadonnees** — detection automatique Espece (14 especes par mots-cles), Exploitation (Abattoir > Couvoir > Elevage par priorite), extraction URL depuis description
3. **Geocoding** — reverse geocoding Nominatim avec rate-limit 1.1s, generation liens Google Maps
4. **CSV generation** — enrichissement des placemarks + export CSV colonnes dynamiques
5. **Notion integration** — creation auto des proprietes manquantes dans la base (`ensure_db_properties`), creation de pages avec typage correct (title, rich_text, select, number, url, date)
6. **Routes** — `GET /` (UI), `POST /convert` (CSV multi-fichiers), `POST /import-notion` (streaming NDJSON multi-fichiers)
7. **NDJSON helpers** — format streaming : steps `init`, `geocode`, `imported`, `import_error`, `done`, `error`

Frontend : `templates/index.html` — page unique avec drag & drop multi-fichiers, progress bar temps reel via NDJSON streaming.

## Proprietes Notion

| Propriete | Type Notion | Source |
|-----------|-------------|--------|
| Nom | title | `<Placemark><name>` |
| Description | rich_text | `<description>` (HTML strippe) |
| Dossier | select | Dossier/calque KML parent |
| Couleur | select | styleUrl (`icon-{id}-{hex}`) |
| Carte | select | Nom du document KML ou fichier |
| Latitude / Longitude | number | `<coordinates>` |
| Adresse | rich_text | Nominatim reverse geocoding |
| Google Maps | url | Genere depuis lat/lon |
| Date d'import | date | Date du jour (ISO) |
| Espece | select | Deduit du nom/description/dossier |
| URL | url | Premiere URL dans la description |
| Exploitation | select | Elevage / Couvoir / Abattoir |
| [champs KML etendus] | rich_text | `<Data>` / `<SimpleData>` |

`STANDARD_FIELDS` et `DB_PROPERTIES` dans app.py doivent rester synchronises : tout nouveau champ doit etre ajoute aux deux, plus gere dans `create_notion_page`.

## Contraintes techniques

- **Notion API** : la propriete "Place" (location) est read-only, d'ou Latitude/Longitude en number separes
- **Nominatim** : 1 req/sec max, User-Agent obligatoire, SSL via certifi
- **XML** : toujours utiliser `defusedxml` (jamais `xml.etree` standard) pour prevenir XXE
- **Limites** : 50 Mo upload KMZ, 10 Mo KML decompresse, 2000 chars rich_text Notion, 100 chars select
- **Port** : 5050
- **Multi-fichiers** : les routes `/convert` et `/import-notion` acceptent plusieurs KMZ via `request.files.getlist("kmz_file")`
- **Detection espece/exploitation** : matching par mots-cles dans le texte concatene (nom + description + dossier). L'ordre des dicts `ESPECE_KEYWORDS` et `EXPLOITATION_KEYWORDS` definit la priorite de detection.
