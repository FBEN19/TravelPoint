# Banc des modeles OpenRouter — 2026-11-03 18:07

8 cas (yukga, compass, holyburek, madameguan, slak, royal-buffet, honore, ishin), chaque modele seul, sans repli local. Note /100 = 50 × restaurants trouves + 30 × adresses justes + 20 × reponses utiles − 5 par fiche en trop.

| # | Modele | Note | Trouves | Adresses | En trop | Reponses utiles | Vides | Illisibles | Erreurs | Quota | Images | Temps / reponse | Temps / lien |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | nvidia/nemotron-3.5-super-vl:free | **94.5** | 92 % | 100 % | 0 | 14/15 | 1 | 0 | 0 | 0 | oui | 6.6 s | 19 s |
| 2 | cohere/north-mini-code:free | **85.8** | 83 % | 86 % | 0 | 12/13 | 1 | 0 | 0 | 0 | non | 10.0 s | 22 s |
| 3 | nvidia/nemotron-3-ultra-550b-a55b:free | **82.4** | 83 % | 86 % | 1 | 12/12 | 0 | 0 | 0 | 0 | non | 11.0 s | 20 s |
| 4 | dots-studio/dots-3-note-preview:free | **80.9** | 92 % | 86 % | 0 | 7/15 | 8 | 0 | 0 | 0 | oui | 23.9 s | 53 s |
| 5 | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | **76.2** | 83 % | 86 % | 2 | 16/17 | 1 | 0 | 0 | 0 | oui | 12.5 s | 28 s |
| 6 | xingchen-agi/xing4.0-29b-a4b:free | **69.0** | 67 % | 86 % | 0 | 1/2 | 0 | 0 | 0 | 1 | non | 20.4 s | 19 s |
| 7 | recursive/flint-v1.0:free | **59.0** | 67 % | 86 % | 0 | 0/13 | 0 | 0 | 13 | 0 | non | — | 7 s |
| 8 | liquid/lfm-2.5-2.6b:free | **59.0** | 67 % | 86 % | 0 | 0/1 | 0 | 0 | 0 | 1 | non | — | 15 s |
| 9 | thinkingmachines/inkling-small:free | **59.0** | 67 % | 86 % | 0 | 0/17 | 0 | 0 | 17 | 0 | oui | — | 0 s |
| 10 | thinkingmachines/inkling:free | **59.0** | 67 % | 86 % | 0 | 0/17 | 0 | 0 | 17 | 0 | oui | — | 0 s |
| 11 | nvidia/nemotron-3.5-content-safety:free | **59.0** | 67 % | 86 % | 0 | 0/17 | 0 | 0 | 17 | 0 | oui | — | 0 s |
| 12 | google/gemma-4-26b-a4b-it:free | **59.0** | 67 % | 86 % | 0 | 0/1 | 0 | 0 | 0 | 1 | oui | — | 15 s |
| 13 | google/gemma-4-31b-it:free | **59.0** | 67 % | 86 % | 0 | 0/1 | 0 | 0 | 0 | 1 | oui | — | 15 s |

Colonnes : *Reponses utiles* = reponses JSON exploitables / appels ; *Vides* = le modele a tout depense a « reflechir » ; *Illisibles* = pas de liste JSON ; *Quota* = limites 429 ; *Images* = sait lire les images de la video (sinon la lecture des images est sautee).

Ordre conseille pour `modeles_openrouter.txt` (modeles sans aucune reponse utile retires) :

```
nvidia/nemotron-3.5-super-vl:free
cohere/north-mini-code:free
nvidia/nemotron-3-ultra-550b-a55b:free
dots-studio/dots-3-note-preview:free
nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free
xingchen-agi/xing4.0-29b-a4b:free
openrouter/free
```
