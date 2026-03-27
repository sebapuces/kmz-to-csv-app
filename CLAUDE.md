# KMZ to Notion App

Webapp Flask qui importe des fichiers KMZ (cartes Google Maps) dans une base Notion, avec export CSV en option.

## Stack

- Python 3.10
- Flask 3.1.3
- notion-client 2.3.0 (SDK officiel Notion)
- defusedxml 0.7.1 (parsing XML securise)
- API Nominatim (OpenStreetMap) pour le geocodage inverse

## Lancer le projet

```bash
python3 -m pip install -r requirements.txt
# Optionnel : export NOTION_TOKEN=ntn_...
python3 app.py
# -> http://localhost:5050
```

## Structure

```
app.py              # Backend Flask (unique fichier)
templates/index.html # Frontend (drag & drop, AJAX)
requirements.txt     # Dependances pinnees
```

## Architecture

Fichier unique `app.py` avec 6 sections :
- **KMZ/KML parsing** : extraction du .kml depuis le .kmz, nom de carte, parsing des Placemarks (Point, LineString, Data, SimpleData)
- **Geocoding** : reverse geocoding via Nominatim avec rate-limit 1.1s entre chaque appel
- **Enrichment** : ajout tag Carte, Adresse geocodee, lien Google Maps
- **CSV generation** : construction du CSV avec colonnes dynamiques
- **Notion integration** : creation auto des proprietes manquantes + import des pages
- **Routes** : `/` (UI), `/convert` (CSV), `/import-notion` (import Notion)

## Proprietes Notion creees

| Propriete | Type Notion | Contenu |
|-----------|-------------|---------|
| Nom | title | Nom du placemark |
| Description | rich_text | Description (HTML strippe) |
| Dossier | select | Dossier/calque KML |
| Carte | select | Nom de la carte (tag pour filtrer) |
| Latitude | number | Latitude GPS |
| Longitude | number | Longitude GPS |
| Altitude | number | Altitude (si presente) |
| Adresse | rich_text | Adresse geocodee (si active) |
| Google Maps | url | Lien direct vers le point |
| [champs KML] | rich_text | Donnees etendues du KML |

Note : la propriete "Place" (lieu sur carte) de Notion n'est pas supportee en ecriture par l'API.

## Securite

- XML parse via `defusedxml` (protection XXE)
- Limite taille upload : 50 Mo (KMZ), 10 Mo (KML decompresse)
- Sanitisation du nom de fichier de sortie
- Token Notion via variable d'env ou formulaire (jamais stocke)
- Debug mode desactive

## API externes

- **Nominatim** (OpenStreetMap) : geocodage inverse, rate-limit 1 req/sec, pas de cle API
- **Notion API** : token d'integration requis (ntn_...), base partagee avec l'integration
- User-Agent Nominatim : `kmz-to-csv-webapp/1.0`
