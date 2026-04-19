# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projet

Webapp Flask qui importe des fichiers KMZ (cartes Google Maps) dans une base Notion, avec export CSV en option. Contexte L214 : les lieux sont des elevages, couvoirs, abattoirs avec des especes animales.

## Stack

- Python 3.10+
- Flask 3.1.3
- notion-client 3.0.0 (SDK officiel Notion)
- defusedxml 0.7.1 (parsing XML securise)
- certifi (certificats SSL pour Nominatim)
- anthropic (SDK Claude API, pour l'ajout intelligent avec web search)

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
6. **Routes** — `GET /` (UI), `POST /convert` (CSV multi-fichiers), `POST /import-notion` (streaming NDJSON multi-fichiers), `POST /db-schema` (lecture schema Notion), `POST /smart-add` (ajout intelligent streaming NDJSON)
7. **Schema Notion + Ajout intelligent** — lecture dynamique du schema de la base Notion (`read_db_schema`), appel Claude API avec `web_search_20250305` pour rechercher les infos, creation de pages via `create_notion_page(schema=...)` (generique, base sur le schema)
8. **NDJSON helpers** — format streaming : steps `init`, `schema_read`, `searching`, `found`, `geocode`, `imported`, `import_error`, `done`, `error`

Frontend : `templates/index.html` — 3 onglets :
- **Import KMZ** : drag & drop multi-fichiers, import Notion streaming ou export CSV
- **Ajout intelligent** : saisie texte ou upload .md, Claude API recherche les infos et cree les pages
- **Preferences** : token Notion, URL base Notion, cle API Claude (stockes en localStorage)

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
| Date importation | date | Date du jour (ISO) |
| Espece | multi_select | Deduit du nom/description/dossier |
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
- **Ajout intelligent** : utilise Claude API (claude-sonnet-4-20250514) avec l'outil `web_search_20250305` pour chercher des infos sur les lieux. Le prompt inclut le schema dynamique de la base Notion cible. La cle API Claude est fournie par l'utilisateur (localStorage cote client, jamais stockee cote serveur).
- **Preferences** : les tokens/cles sont stockes en localStorage dans le navigateur et envoyes a chaque requete. Pas de session cote serveur.
