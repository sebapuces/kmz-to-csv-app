# KMZ to CSV App

Webapp Flask qui convertit des fichiers KMZ (cartes Google Maps) en CSV importables dans Notion.

## Stack

- Python 3.10
- Flask 3.1.3
- defusedxml 0.7.1 (parsing XML securise)
- API Nominatim (OpenStreetMap) pour le geocodage inverse

## Lancer le projet

```bash
python3 -m pip install -r requirements.txt
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

Fichier unique `app.py` avec 4 sections :
- **KMZ/KML parsing** : extraction du .kml depuis le .kmz, parsing des Placemarks (Point, LineString, Data, SimpleData)
- **Geocoding** : reverse geocoding via Nominatim avec rate-limit 1.1s entre chaque appel
- **CSV generation** : construction du CSV avec colonnes dynamiques selon les donnees KML
- **Routes** : `/` (page d'accueil) et `/convert` (POST, retourne le CSV)

## Colonnes CSV produites

Nom, Description, Dossier, Latitude, Longitude, Altitude, [champs etendus KML], Adresse (si geocodage), Google Maps (lien)

## Securite

- XML parse via `defusedxml` (protection XXE)
- Limite taille upload : 50 Mo (KMZ), 10 Mo (KML decompresse)
- Sanitisation du nom de fichier de sortie
- Debug mode desactive
- Pas de secrets dans le code

## API externe

- **Nominatim** (OpenStreetMap) : geocodage inverse, rate-limit obligatoire 1 req/sec, pas de cle API requise
- User-Agent : `kmz-to-csv-webapp/1.0`
