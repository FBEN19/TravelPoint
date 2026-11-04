# Travelpoint (restaurants)

Colle un lien TikTok / Instagram / YouTube : l'app retrouve les restaurants montrés (adresse, horaires, prix, cuisine) et les range par ville sur une carte.
L'IA passe par OpenRouter (modeles gratuits, cle dans `data/openrouter.key`) ; la voix est transcrite en local par Whisper (MLX).

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
2. Si la legende ne donne rien : **video** (Whisper pour la voix, images lues par OpenRouter), ~1 min.
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
Reglages : `TP_WHISPER` (modele de transcription, par defaut `mlx-community/whisper-large-v3-turbo`), `TP_DATA` (dossier de donnees).

## Dans le monde entier
Les lieux sont cherches sur OpenStreetMap **autour de l'adresse** (ou de la ville citee), pas par texte : ca marche avec n'importe quel pays et
n'importe quelle ecriture (le nom du post peut etre latin, celui d'OSM japonais ou coreen). Les devises (€ $ £ ¥ ₩ 円...) sont reconnues.
Un restaurant seul a l'adresse indiquee mais au nom different apparait avec la mention « probable ».
En cas de souci, lance avec `TP_DEBUG=1 uvicorn app:app --port 8420` : chaque requete OpenStreetMap s'affiche dans le terminal.

## Une video n'est analysee qu'une fois
Chaque lien est ramene a l'identifiant de la video (YouTube, Instagram, TikTok) : `?si=`, `utm_`, `igsh`, `/shorts/` ou `/watch`, `www.`... ne changent rien.
Si tu recolles la meme video, meme avec un lien different, l'app ne relance ni telechargement, ni Whisper, ni IA : elle te montre les restaurants deja trouves.
Le resultat est garde dans `data/jobs/<video>/result.json`. Une analyse terminee ne peut pas etre supprimee depuis la page (seules les erreurs, via « Fermer »).
Pour forcer une nouvelle analyse : recolle le lien puis OK dans la fenetre « reanalyser quand meme ». Les fiches modifiees, testees ou annotees sont conservees.

## Legendes de tous les formats
Les posts n'ont aucune structure commune : l'IA (OpenRouter) lit donc la legende telle quelle et releve les etablissements, leurs offres, adresses et horaires.
Garde-fou : un nom, une adresse ou une offre qui n'est pas ECRIT dans le texte est ecarte (l'IA ne peut pas en inventer), puis OpenStreetMap confirme que le lieu existe.
Si l'IA est indisponible ou ne trouve rien, de simples regles (📍, @pseudos, adresses) prennent le relais. `TP_LLM=0` desactive l'IA ; `TP_DEBUG=1` montre sa reponse brute.
Ce qui est mal lu se corrige a la main avec « Modifier ».

## Adresses introuvables sur OpenStreetMap : recherche web gratuite
Quand OSM ne connait pas un restaurant et que le post ne donne pas d'adresse, l'app cherche « "Nom" Ville adresse » sur le web
(bibliotheque `ddgs`, gratuite, sans cle ni compte). Elle ne garde que les resultats qui citent le restaurant, l'IA recopie l'adresse
d'un extrait (elle doit y etre ecrite mot pour mot), puis l'adresse doit se trouver dans la ville citee. La fiche passe en « probable »
avec un lien « Source ↗ » vers la page d'origine. Au plus 5 recherches par video, jamais deux fois la meme (`data/web_cache.json`).
`TP_WEB=0` desactive la recherche ; `TP_DEBUG=1` affiche les requetes. Si la recherche echoue ou est bloquee, la fiche reste « a verifier ».

## Banc d'essai
`python bench/bench.py` analyse les liens de `bench/cas.json` avec le code actuel et compare aux restaurants attendus :
restaurant trouve (nom), adresse juste (marqueur a moins de 300 m), bonne ville, fiches en trop, noms qui ressemblent a une adresse.
- Il travaille dans `bench/data/`, jamais dans `data/` (ta base n'est pas touchee).
- Il reprend les legendes, transcriptions et videos deja telechargees par l'app ; `--frais` retelecharge tout. `--cas id1,id2` pour quelques cas.
- Resultat : `bench/rapport.md` (detail par lien) et une ligne dans `bench/historique.csv` pour suivre les progres d'un commit a l'autre.
- Ajouter un cas : un lien + les restaurants attendus dans `cas.json` (`verifie: false` tant que la reponse n'est pas sure).
Compter environ 1 min par lien quand la video doit etre analysee ; 15 s d'attente entre deux liens Instagram pas encore telecharges.

## IA en ligne : OpenRouter (modeles gratuits)
L'IA (lecture des legendes, des images et des resultats web) passe par les modeles gratuits d'OpenRouter :
1. Cree une cle sur openrouter.ai, puis mets-la dans `data/openrouter.key` (ignore par git) ou dans la variable `OPENROUTER_API_KEY`.
2. Lance normalement : `uvicorn app:app --port 8420` (ou `python bench/bench.py`). Sans cle, l'app previent au demarrage et n'utilise que les regles simples.
- Modeles : ta liste (voir ci-dessous), avec en dernier `openrouter/free`, le routeur des modeles gratuits.
  Ordre d'essai : `modeles_openrouter.txt` (une ligne par modele). Un modele qui echoue (erreur, quota, reponse vide ou sans JSON) passe la main
  au suivant ; « aucun restaurant » (`[]`) est une vraie reponse et s'arrete la. Pour la lecture des images de la video, les modeles
  qui ne lisent pas les images sont sautes (OpenRouter est interroge une fois par jour). Ponctuellement : `TP_OR_MODELES=m1:free,m2:free`.
  La trace de chaque analyse dit quel modele a repondu.
- Une video est envoyee sous forme de 12 images reparties sur toute sa duree. La voix reste transcrite en local par Whisper.
- Limites des modeles gratuits : 20 requetes/minute, 1 000 par jour (une fois 10 $ de credits achetes). Si OpenRouter est sature ou en panne,
  l'analyse continue sans IA, avec les regles simples (moins de restaurants trouves). Le rapport du banc dit combien de lectures ont reussi ou echoue.
- **Comparer les modeles** : `python bench/modeles.py` fait passer chaque modele seul (sans les autres en secours) sur 8 cas du banc
  et les classe : restaurants trouves, adresses justes, fiches en trop, reponses utiles / vides / illisibles, temps par reponse.
  Rapport `bench/modeles.md`, historique `bench/modeles.csv`. `--appliquer` reecrit `modeles_openrouter.txt` dans l'ordre du classement ;
  `--modeles a,b` / `--cas tous` pour choisir. Environ 3 requetes par cas et par modele : surveille le quota de 1 000 par jour.
- Ce que tu envoies (legendes, transcriptions, images) part chez les fournisseurs des modeles : verifie leurs conditions dans les reglages de confidentialite d'OpenRouter.
