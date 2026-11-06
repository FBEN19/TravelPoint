# Banc d'essai Travelpoint — 2026-11-04 10:14 (commit 0dbf3ba)

IA : **OpenRouter (nvidia/nemotron-3.5-super-vl:free, cohere/north-mini-code:free, nvidia/nemotron-3-ultra-550b-a55b:free, dots-studio/dots-3-note-preview:free, nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free, xingchen-agi/xing4.0-29b-a4b:free, openrouter/free)** — lectures : 32 reussies, 0 echouees (tous les modeles indisponibles)

Score sur les **15 cas verifies** :

- Restaurants trouves : **20/20 (100 %)**
- Adresses justes (< 300 m) : **15/15 (100 %)**
- Bonne ville : 20/20 (100 %)
- Faux positifs (fiches en trop) : 0
- Noms qui ressemblent a une adresse : 0
- Erreurs : 0 — Duree totale : 901 s (moyenne 53 s par lien)

Modeles OpenRouter (dans l'ordre d'essai) :

| Modele | Appels | Reponses utiles | Vides | Illisibles | Erreurs | Quota | Sautes (images) | Temps moyen |
|---|---|---|---|---|---|---|---|---|
| nvidia/nemotron-3.5-super-vl:free | 32 | 27 | 5 | 0 | 0 | 0 | 0 | 9.0 s |
| cohere/north-mini-code:free | 4 | 3 | 1 | 0 | 0 | 0 | 1 | 21.6 s |
| nvidia/nemotron-3-ultra-550b-a55b:free | 1 | 0 | 1 | 0 | 0 | 0 | 1 | — |
| dots-studio/dots-3-note-preview:free | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 21.8 s |

| Cas | Trouves | Adresses | En trop | Duree | Remarque |
|---|---|---|---|---|---|
| red-noodle | 2/2 | 2/2 | 0 | 40 s |  |
| yukga | 1/1 | 1/1 | 0 | 13 s |  |
| compass | 2/2 | 0/0 | 0 | 70 s |  |
| holyburek | 1/1 | 1/1 | 0 | 14 s |  |
| madameguan | 4/4 | 4/4 | 0 | 35 s |  |
| sushi-saint-martin | 1/1 | 1/1 | 0 | 17 s |  |
| slak | 1/1 | 0/0 | 0 | 8 s |  |
| osaka-crepe | 1/1 | 1/1 | 0 | 14 s |  |
| bertoni | 1/1 | 1/1 | 0 | 10 s |  |
| longue-vie | 1/1 | 1/1 | 0 | 16 s |  |
| royal-buffet | 1/1 | 0/0 | 0 | 46 s |  |
| honore | 1/1 | 1/1 | 0 | 89 s |  |
| ishin | 1/1 | 0/0 | 0 | 105 s |  |
| thermes-kongfu | 1/1 | 1/1 | 0 | 52 s |  |
| dozo | 1/1 | 1/1 | 0 | 23 s |  |
| brasseries-paris | 3/3 | 0/0 | 0 | 49 s | à confirmer |
| tokyo-5 | 2/3 | 0/0 | 0 | 299 s | à confirmer |

## Detail

### red-noodle — Legende : @handle + 2 adresses (Paris, Lille)
https://www.instagram.com/reel/DddlwsgIa5g/

- ✅ attendu **Red Noodle** (23 Bd des Italiens, 75002 Paris) → Red Noodle (a_verifier, 📍), a 0 m
- ✅ attendu **Red Noodle** (62 Rue de Béthune, 59800 Lille) → Red Noodle (haute, 📍), a 3 m

### yukga — Legende : @yukga.paris + adresse abregee ; titre = compte sorties.paris
https://www.instagram.com/reel/DaYGFJ_NRcc/

- ✅ attendu **Yukga** (26 Avenue de la Porte d'Ivry, 75013 Paris) → Yukga (a_verifier, 📍), a 0 m

### compass — Blocs « 📍@pseudo : offres », adresses en commentaire ; compte = appli Compass
https://www.instagram.com/reel/DcYC7U8t8Gl/

- ✅ attendu **Marseille en Cornet** (Marseille) → Marseille En Cornet (moyenne, 📍)
- ✅ attendu **Farfalou** (Marseille) → Farfalou (moyenne, 📍)

### holyburek — Legende : @holyburek_paris + adresse ; fiche corrigee a la main
https://www.instagram.com/reel/DWFBjl9Df38/

- ✅ attendu **Holyburek** (66 Rue du Faubourg Poissonnière, 75010 Paris) → Holyburek Paris (a_verifier, 📍), a 0 m

### madameguan — Concours ; 1 enseigne (@madameguan) et 4 adresses en liste
https://www.instagram.com/reel/DK7MbO9t16b/

- ✅ attendu **Madame Guan** (63 Rue Saint-Martin, 75004 Paris) → Madameguan (a_verifier, 📍), a 0 m
- ✅ attendu **Madame Guan** (50 Rue Godefroy Cavaignac, 75011 Paris) → Madameguan (a_verifier, 📍), a 0 m
- ✅ attendu **Madame Guan** (13 Rue des Canettes, 75006 Paris) → Madameguan (a_verifier, 📍), a 0 m
- ✅ attendu **Madame Guan** (33 Rue du Rocher, 75008 Paris) → Madameguan (a_verifier, 📍), a 0 m

### sushi-saint-martin — Legende sans nom de restaurant : seulement l'adresse (le nom ne doit pas etre l'adresse)
https://www.instagram.com/reel/DUdpdcrDUpa/

- ✅ attendu **(nom inconnu)** (156 Rue Saint-Martin, 75003 Paris) → None (a_verifier, 📍), a 4 m

### slak — Seulement « 🏠 @slak.paris », infos en fin de video
https://www.instagram.com/reel/DdO6xbjKjRS/

- ✅ attendu **Slak** (Paris) → Slak (haute, 📍)

### osaka-crepe — Legende en anglais, nom entre crochets, adresse au Japon
https://www.instagram.com/reel/DbxtxBUBgT6/

- ✅ attendu **Crepe & Espresso & 9-chome** (1-22 Ikutamaemaemachi, Tennoji-ku, Osaka, Japan) → Crepe & Espresso & 9-chome (a_verifier, 📍), a 0 m

### bertoni — Tout est dans le titre YouTube ; fiche corrigee a la main
https://youtube.com/shorts/dd4x3Xy9xKo

- ✅ attendu **Bertoni** (38 Rue Sylvabelle, Marseille) → Bertoni (a_verifier, 📍), a 0 m

### longue-vie — Belgique, adresse au format « Rue Longue Vie 31, 1050 Ixelles »
https://youtube.com/shorts/eWGdSP9OGrw

- ✅ attendu **Le Longue Vie** (Rue Longue Vie 31, 1050 Ixelles, Belgique) → Le Longue Vie (haute, 📍), a 2 m

### royal-buffet — Nom dans la description (centre commercial Grand Littoral) ; l'app avait trouve « Crep'eat »
https://youtube.com/shorts/A3j_h6ruV-Y

- ✅ attendu **Royal Buffet** (Marseille) → Royal Buffet (moyenne, 📍)

### honore — Nom a l'ecran (« HONORÉ »), titre = une personne (Camille Lavabre) ; a demenage au 21 rue de la Paix Marcel Paul
https://youtube.com/shorts/F01sNpJzZeo

- ✅ attendu **Honoré** (21 Rue de la Paix Marcel Paul, 13001 Marseille) → HONORÉ (moyenne, 📍), a 0 m

### ishin — Nom a l'oral (« Ishin » pour Whisper), ecrit « Isshin » sur l'enseigne, ville dans le titre
https://www.youtube.com/shorts/gdCeQctM9Uw

- ✅ attendu **Isshin Ramen** (Bordeaux) → Isshin Ramen (moyenne, 📍)

### thermes-kongfu — Nom complet dans le titre ; l'enseigne ne montre que « LES THERMES » (= le restaurant voisin du meme groupe sur OSM)
https://www.youtube.com/shorts/fx9w0BFrpDM

- ✅ attendu **Les Thermes Kongfu Pâtes** (2 Rue Castellane, 31000 Toulouse) → Les Thermes Kongfu Pâtes (moyenne, 📍), a 2 m

### dozo — Titre seul « Bonne adresse à Toulouse - Dozo 📍 #hashtags » (les hashtags apres 📍 ne sont pas une adresse) ; inconnu d'OSM
https://www.youtube.com/shorts/1-i3Xcgpjlw

- ✅ attendu **Dozo** (13 Rue d'Alsace Lorraine, 31000 Toulouse) → Dozo (moyenne, 📍), a 0 m

### brasseries-paris — 3 brasseries : Comme un Bouillon (a l'oral), les 2 autres lues a l'ecran - A CONFIRMER
https://youtube.com/shorts/vM8C-TWZD1s

- ✅ attendu **Comme un Bouillon** (Paris) → Comme Un Bouillon (haute, 📍)
- ✅ attendu **La Cantine Diderot** (Paris) → La Cantine Diderot (haute, 📍)
- ✅ attendu **Le Vaudésir** (Paris) → Le Vaudésir (haute, 📍)

### tokyo-5 — 5 adresses a l'oral, liste incomplete - A COMPLETER (Hanbei vient de l'app, non verifie)
https://youtube.com/shorts/rkTW4iFMeQI

- ❌ attendu **Commune** (Tokyo) → —
- ✅ attendu **Riverside Club** (Tokyo) → le Riverside Club (a_verifier, sans position)
- ✅ attendu **Hanbei** (Tokyo) → Hanbei (haute, 📍)
