# Banc d'essai Travelpoint — 2026-11-03 18:45 (commit 5eb40bc)

IA : **OpenRouter (nvidia/nemotron-3.5-super-vl:free, cohere/north-mini-code:free, nvidia/nemotron-3-ultra-550b-a55b:free, dots-studio/dots-3-note-preview:free, nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free, xingchen-agi/xing4.0-29b-a4b:free, openrouter/free)** — lectures : 3 par OpenRouter, 0 en local (dont 0 replis)

Score sur les **1 cas verifies** :

- Restaurants trouves : **0/1 (0 %)**
- Adresses justes (< 300 m) : **—**
- Bonne ville : 0/1 (0 %)
- Faux positifs (fiches en trop) : 0
- Noms qui ressemblent a une adresse : 0
- Erreurs : 0 — Duree totale : 59 s (moyenne 59 s par lien)

Modeles OpenRouter (dans l'ordre d'essai) :

| Modele | Appels | Reponses utiles | Vides | Illisibles | Erreurs | Quota | Sautes (images) | Temps moyen |
|---|---|---|---|---|---|---|---|---|
| nvidia/nemotron-3.5-super-vl:free | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 9.0 s |

| Cas | Trouves | Adresses | En trop | Duree | Remarque |
|---|---|---|---|---|---|
| ishin | 0/1 | 0/0 | 0 | 59 s |  |

## Detail

### ishin — Nom a l'oral (« Ishin » pour Whisper), ecrit « Isshin » sur l'enseigne, ville dans le titre
https://www.youtube.com/shorts/gdCeQctM9Uw

- ❌ attendu **Isshin Ramen** (Bordeaux) → —
