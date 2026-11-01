# Travelpoint (restaurants)

Colle un lien TikTok / Instagram / YouTube : l'app retrouve les restaurants montrés (adresse, horaires, prix, cuisine) et les range par ville sur une carte.
Tout tourne en local sur le Mac (Qwen3-VL + Whisper via MLX). Aucune clé API.

## Installation (une fois)
```bash
brew install ffmpeg
cd TravelPoint
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Lancer
```bash
source .venv/bin/activate
uvicorn app:app --port 8420
```
Puis ouvre http://localhost:8420. Au premier lien analysé via la video, ~3,5 Go de modeles sont telecharges.

## Comment ca marche
1. **Legende du post** (yt-dlp, sans telecharger la video) : nom, adresses, prix, horaires -> verification OpenStreetMap. Quelques secondes.
2. Si la legende ne donne rien : **video** (Whisper pour la voix, Qwen3-VL pour les images), ~1 min.
Les analyses passent une par une (8 Go de RAM). Un lien deja analyse n'est pas relu.

## Corriger une fiche
Chaque fiche a un bouton **Modifier** (nom, adresse, horaires, prix…, position). « Chercher cette adresse » relance le geocodage ;
on peut aussi deplacer le marqueur ou cliquer sur la carte. **C'est bon** valide une fiche « a verifier » / « probable ».
**+ Ajouter un restaurant** cree une fiche a la main. Une fiche modifiee ou creee a la main n'est jamais ecrasee ni supprimee par « Reessayer ».
Le nom du compte qui publie n'est jamais pris pour un restaurant ; un @handle n'est « haute » confiance que si OpenStreetMap
trouve un lieu de restauration de ce nom pres de l'adresse (sinon « a verifier »).

## Tests
`python test_travelpoint.py` (Nominatim simule, aucun reseau, base temporaire).

## Instagram
L'acces anonyme marche quelques fois puis Instagram limite. Dans ce cas : « Reessayer » plus tard, ou place un `cookies.txt`
(export Netscape d'un navigateur connecte, idealement un compte dedie) dans `data/cookies.txt`. Rien d'autre a changer.
Variable optionnelle : `TP_COOKIES_BROWSER=chrome` pour lire les cookies d'un navigateur.

## Donnees
`data/travelpoint.db` (SQLite) et `data/jobs/<lien>/` (legende, video, transcription en cache). Supprime `data/` pour tout remettre a zero.
Reglages : `TP_VLM`, `TP_WHISPER` (autres modeles), `TP_DATA` (dossier de donnees).

## Dans le monde entier
Les lieux sont cherches sur OpenStreetMap **autour de l'adresse** (ou de la ville citee), pas par texte : ca marche avec n'importe quel pays et
n'importe quelle ecriture (le nom du post peut etre latin, celui d'OSM japonais ou coreen). Les devises (€ $ £ ¥ ₩ 円...) sont reconnues.
Un restaurant seul a l'adresse indiquee mais au nom different apparait avec la mention « probable ».
En cas de souci, lance avec `TP_DEBUG=1 uvicorn app:app --port 8420` : chaque requete OpenStreetMap s'affiche dans le terminal.

## Une video n'est analysee qu'une fois
Chaque lien est ramene a l'identifiant de la video (YouTube, Instagram, TikTok) : `?si=`, `utm_`, `igsh`, `/shorts/` ou `/watch`, `www.`... ne changent rien.
Si tu recolles la meme video, meme avec un lien different, l'app ne relance ni telechargement, ni Whisper, ni Qwen : elle te montre les restaurants deja trouves.
Le resultat est garde dans `data/jobs/<video>/result.json`. Une analyse terminee ne peut pas etre supprimee depuis la page (seules les erreurs, via « Fermer »).
Pour forcer une nouvelle analyse : recolle le lien puis OK dans la fenetre « reanalyser quand meme ». Les fiches modifiees, testees ou annotees sont conservees.

## Legendes de tous les formats
Les posts n'ont aucune structure commune : l'IA locale (Qwen3-VL) lit donc la legende telle quelle et releve les etablissements, leurs offres, adresses et horaires.
Garde-fou : un nom, une adresse ou une offre qui n'est pas ECRIT dans le texte est ecarte (l'IA ne peut pas en inventer), puis OpenStreetMap confirme que le lieu existe.
Si l'IA est indisponible ou ne trouve rien, de simples regles (📍, @pseudos, adresses) prennent le relais. `TP_LLM=0` desactive l'IA ; `TP_DEBUG=1` montre sa reponse brute.
Ce qui est mal lu se corrige a la main avec « Modifier ».

## Adresses introuvables sur OpenStreetMap : recherche web gratuite
Quand OSM ne connait pas un restaurant et que le post ne donne pas d'adresse, l'app cherche « "Nom" Ville adresse » sur le web
(bibliotheque `ddgs`, gratuite, sans cle ni compte). Elle ne garde que les resultats qui citent le restaurant, l'IA recopie l'adresse
d'un extrait (elle doit y etre ecrite mot pour mot), puis l'adresse doit se trouver dans la ville citee. La fiche passe en « probable »
avec un lien « Source ↗ » vers la page d'origine. Au plus 5 recherches par video, jamais deux fois la meme (`data/web_cache.json`).
`TP_WEB=0` desactive la recherche ; `TP_DEBUG=1` affiche les requetes. Si la recherche echoue ou est bloquee, la fiche reste « a verifier ».
