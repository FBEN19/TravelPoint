"""Tests Travelpoint (Nominatim simule, aucun reseau) : python test_travelpoint.py
Le serveur FastAPI est lance pour de vrai (uvicorn) sur un port libre, avec une base temporaire."""
import contextlib, hashlib, json, os, pathlib, subprocess, socket, sqlite3, tempfile, threading, time, unittest, urllib.error, urllib.parse, urllib.request
import sys, types
from unittest import mock

os.environ["TP_DATA"] = tempfile.mkdtemp(prefix="tp-test-")  # avant d'importer pipeline : base et caches jetables
import uvicorn
import app as server
import pipeline
LIT_IMAGES = pipeline.lit_images  # la vraie (les tests OpenRouter la remplacent)

REAL_URLOPEN, DEFAULT_DELAY = urllib.request.urlopen, pipeline.NOMINATIM_DELAY
REAL_WEB_SEARCH = pipeline.web_search  # la vraie, pour le test « recherche en panne »

# ---------- faux Nominatim : quelques villes + quelques lieux de restauration ----------
CITIES = {"monnaie, lille": (50.6400, 3.0650), "lille": (50.6329, 3.0586), "paris": (48.8566, 2.3522)}  # rue d'abord, puis ville
PLACES = [  # nom, type OSM, lat, lon, ville
    ("Red Noodle", "restaurant", 50.6329, 3.0586, "Lille"),
    ("Le Zinc", "cafe", 50.6500, 3.0700, "Lille"),
    ("Chez Paul", "restaurant", 50.6400, 3.0650, "Lille"),
    ("Mairie", "townhall", 48.8566, 2.3522, "Paris"),  # pas un lieu de restauration
]
CALLS = []


def fake_nominatim(req, timeout=None):
    qs = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query).items()}
    CALLS.append((time.monotonic(), qs))
    q = qs["q"].lower()
    if "viewbox" in qs:  # recherche d'un etablissement dans une zone (viewbox = gauche,haut,droite,bas)
        lo1, la1, lo2, la2 = map(float, qs["viewbox"].split(","))
        out = [dict(name=n, type=t, **{"class": "amenity"}, lat=str(la), lon=str(lo), display_name=f"{n}, {v}, France", address={"city": v, "country": "France"},
                    extratags={}, namedetails={"name": n}) for n, t, la, lo, v in PLACES if lo1 <= lo <= lo2 and la2 <= la <= la1]
    else:  # geocodage d'une adresse
        out = [dict(lat=str(la), lon=str(lo), display_name=q, address={"city": c.split(", ")[-1].title(), "country": "France"})
               for c, (la, lo) in CITIES.items() if c in q][:1]
    body = json.dumps(out).encode()
    return mock.Mock(read=lambda: body)


def info(description, title="", uploader="un.compte"):
    return {"title": title, "description": description, "uploader": uploader, "channel": uploader, "uploader_id": uploader}


class Pipeline(unittest.TestCase):
    def setUp(self):
        pipeline.NOMINATIM_DELAY = 0
        pipeline.WEB_PAUSE = 0; pipeline.WEB_CACHE.unlink(missing_ok=True)
        p = mock.patch.object(pipeline, "web_search", return_value=[]); p.start(); self.addCleanup(p.stop)  # jamais de vraie recherche web en test
        p = mock.patch.object(pipeline, "ask", side_effect=RuntimeError("pas de LLM en test")); p.start(); self.addCleanup(p.stop)  # sinon : regles
        p = mock.patch("urllib.request.urlopen", fake_nominatim); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(pipeline, "video_stage", return_value=([], "")); p.start(); self.addCleanup(p.stop)  # pas de Whisper/Qwen

    def run_analyze(self, inf):
        self._n = getattr(self, "_n", 0) + 1  # lien unique par analyse : sinon le cache des resultats renverrait celle d'un autre test
        with mock.patch.object(pipeline, "get_info", return_value=inf):
            return pipeline.analyze(f"https://exemple.test/{self.id()}/{self._n}")

    def test1_handle_deux_adresses(self):
        res = self.run_analyze(info("Red Noodle Paris, Lille : les meilleurs nouilles 🍜\n👉 @rednoodle.officiel\n📍 10 rue Esquermoise, Lille\n📍 5 rue de Rivoli, Paris"))
        self.assertEqual([r["nom"] for r in res], ["Red Noodle", "Red Noodle"])
        lille, paris = res
        self.assertEqual(lille["confiance"], "haute")      # OSM confirme un restaurant "Red Noodle" a l'adresse
        self.assertEqual(paris["confiance"], "a_verifier")  # OSM ne le connait pas a Paris
        self.assertTrue(paris["lat"] and paris["lon"])      # mais la fiche garde le marqueur de l'adresse

    def test2_nom_du_compte_ignore(self):
        cap = "Les bonnes adresses de Lille\nSorties Paris vous presente Chez Paul\n📍 12 rue de la Monnaie, Lille\n#sortiesparis #lille"
        p = pipeline.parse_caption(cap, {"sortiesparis"})
        self.assertFalse([c for c in p["candidats"] if "sorties" in c.lower()], p["candidats"])
        self.assertNotIn("sorties", (p["nom"] or "").lower())
        # sans le compte en parametre, le hashtag serait bien un candidat : le test est significatif
        self.assertTrue([c for c in pipeline.parse_caption(cap)["candidats"] if "sorties" in c.lower()])
        # titre yt-dlp "Video by sorties.paris" + uploader/channel/uploader_id
        inf = info(cap, title="Video by sorties.paris", uploader="sorties.paris")
        for r in self.run_analyze(inf):
            self.assertNotIn("sorties", pipeline.nz(r["nom"]))
        # un @handle du compte lui-meme n'est pas le restaurant (cas reel : @bouffe2lolo)
        res = self.run_analyze(info("Chez Paul 🍽️\n📍 3 rue de Rivoli, Paris\nToutes mes adresses : @sorties.paris", uploader="sorties.paris"))
        self.assertEqual([r["nom"] for r in res], ["Chez Paul"])

    def test3_blogueur_jamais_haute(self):
        cap = "Decouverte avec @julie.mange 😋\n📍 3 rue de Rivoli, Paris"
        res = self.run_analyze(info(cap))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["nom"], "julie"); self.assertEqual(res[0]["confiance"], "a_verifier")
        # un autre restaurant seul a l'adresse : au mieux "probable", jamais "haute"
        res = self.run_analyze(info("Decouverte avec @julie.mange 😋\n📍 3 rue Esquermoise, Lille"))
        self.assertNotEqual(res[0]["confiance"], "haute")

    def test4_concours(self):
        cap = "🎉 CONCOURS : bravo à nos gagnants @marie.d et @paul_gourmand ! Merci à @leresto pour la collab\n📍 12 rue Nationale, Lille"
        self.assertIsNone(pipeline.parse_caption(cap)["nom"])
        res = self.run_analyze(info(cap))  # pas de nom : la fiche est placee sur l'adresse, sans prendre un @gagnant pour le restaurant
        self.assertEqual([(r["nom"], r["confiance"], r["ville"]) for r in res], [(None, "a_verifier", "Lille")]); self.assertTrue(res[0]["lat"])
        p = pipeline.parse_caption(cap + "\n👉 @chezpaul.lille")  # un vrai handle sur une autre ligne reste pris
        self.assertEqual((p["nom"], p["nom_src"]), ("chezpaul", "handle"))

    def test_adresse_sans_emoji_ville_en_hashtag(self):  # titre YouTube de Bouffe2lolo : « Bertoni », 38 Rue Sylvabelle, #marseille
        p = pipeline.parse_caption("«\xa0Chez Paul\xa0», 12 Rue de la Monnaie, #lille #restaurant *collaboration commerciale")
        self.assertEqual((p["nom"], p["adresses"], p["villes"]), ("Chez Paul", ["12 Rue de la Monnaie"], ["lille"]))
        self.assertEqual(pipeline.parse_caption("Super adresse : Rue Longue Vie 31, 1050 Ixelles")["adresses"], ["Rue Longue Vie 31, 1050 Ixelles"])
        res = self.run_analyze(info("«\xa0Chez Paul\xa0», 12 Rue de la Monnaie, #lille #restaurant"))
        self.assertEqual([(r["nom"], r["adresse"], r["confiance"]) for r in res], [("Chez Paul", "12 Rue de la Monnaie, Lille", "haute")])

    def test_video_restaurant_inconnu_place_sur_adresse(self):
        spoken = [{"nom": "Resto Fantome", "adresse": "3 rue de Rivoli", "ville": "Paris", "pays": "France"},  # OSM ne le connait pas
                  {"nom": "Chez Paul", "adresse": "12 rue de la Monnaie", "ville": "Lille"},                   # trouve pres de l'adresse
                  {"nom": "Sans Adresse", "adresse": "", "ville": "Lille"}]
        with mock.patch.object(pipeline, "video_stage", return_value=(spoken, "")):
            res = {r["nom"]: r for r in self.run_analyze(info("Ma tournee des restos"))}
        f = res["Resto Fantome"]
        self.assertEqual((f["adresse"], f["confiance"], f["lat"], f["ville"]), ("3 rue de Rivoli, Paris", "a_verifier", 48.8566, "Paris"))
        self.assertEqual((res["Chez Paul"]["confiance"], res["Chez Paul"]["lat"]), ("haute", "50.64"))
        self.assertEqual((res["Sans Adresse"]["lat"], res["Sans Adresse"]["confiance"]), (None, "a_verifier"))

    COMPASS = """La vraie streetfood marseillaise ☀️🤤

Les réducs COMPASS beneeeeef :

📍@marseille_en_cornet :

♾️ Burger ou Fish and chips + frites + boisson à 15€ (au lieu de 19€) ; (sur place)
♾️ Les panisses à 5,90€ (au lieu de 8€) ; (à emporter)

📍@farfalou.canebiere :

🎁 en One Shot (1 fois/an/utilisateur) : 1 quantité de panisse achetée = la même OFFERTE
♾️ 15% de réduction sur les sandwichs (poulpe ou sardines)

👉🏼 Toutes les adresses en commentaire

📱Pour profiter des réducs il faut scanner le QR Code sur place avec ton abonnement Compass (2,99€/mois ou 19,99€/an)

#marseille #food #vieuxport"""

    def test_pins_avec_pseudos_un_restaurant_par_bloc(self):  # appli de reductions : « 📍@resto : » puis ses offres, adresses « en commentaire »
        extra = [("Marseille en Cornet", "restaurant", 43.2950, 5.3740, "Marseille"), ("Farfalou", "restaurant", 43.2975, 5.3810, "Marseille")]
        with mock.patch.dict(CITIES, {"marseille": (43.2965, 5.3698)}), mock.patch.dict(globals(), {"PLACES": PLACES + extra}):
            res = self.run_analyze(info(self.COMPASS, title="Video by compass.aixmarseille", uploader="compass.aixmarseille"))
        self.assertEqual([r["nom"] for r in res], ["Marseille en Cornet", "Farfalou"])
        self.assertEqual({r["confiance"] for r in res}, {"haute"})
        self.assertTrue(any("Burger" in x for x in res[0]["prix"]) and not any("Burger" in x for x in res[1]["prix"]))  # chaque restaurant a ses offres
        self.assertTrue(any("15% de réduction" in x for x in res[1]["prix"]))
        self.assertFalse(any("QR Code" in x or "19,99" in x for r in res for x in r["prix"]))  # l'abonnement de l'appli n'est pas un prix de restaurant
        self.assertEqual({r["ville"] for r in res}, {"Marseille"})
        self.assertFalse(any("@" in (r["adresse"] or "") for r in res))

    def test_pins_avec_pseudos_restaurant_inconnu_d_osm(self):
        with mock.patch.dict(CITIES, {"marseille": (43.2965, 5.3698)}):  # aucun restaurant de ce nom sur OSM
            res = self.run_analyze(info(self.COMPASS, uploader="compass.aixmarseille"))
        self.assertEqual([(r["nom"], r["confiance"], r["ville"], r["adresse"]) for r in res],
                         [("Marseille En Cornet", "a_verifier", "Marseille", None), ("Farfalou", "a_verifier", "Marseille", None)])

    def llm(self, items):  # reponse simulee de l'IA locale
        return mock.patch.object(pipeline, "ask", return_value=items)

    def test_llm_legende_sans_structure(self):  # aucune regle ne marcherait : texte libre, pas de pin, pas de @pseudo
        texte = "Mon spot du moment 🔥 chez Farfalou, grosse terrasse sur la Canebière, les sandwichs poulpe sont fous, 12€. Y'a aussi Marseille en Cornet pour la friture : compte 9,90€ le cornet. #marseille"
        items = [{"nom": "Farfalou", "adresse": "12 rue Imaginaire, Marseille", "ville": "Marseille", "pays": "France", "offres": ["les sandwichs poulpe sont fous, 12€"], "horaires": ""},
                 {"nom": "Marseille en Cornet", "adresse": "", "ville": "Marseille", "offres": ["compte 9,90€ le cornet", "offre inventee a 3€"]},
                 {"nom": "Chez Invente", "adresse": "3 rue Faux, Marseille"},   # nom absent du texte : ecarte
                 {"nom": "foodmars.officiel"}]                                 # compte qui publie : ecarte
        extra = [("Marseille en Cornet", "restaurant", 43.2950, 5.3740, "Marseille"), ("Farfalou", "restaurant", 43.2975, 5.3810, "Marseille")]
        with mock.patch.dict(CITIES, {"marseille": (43.2965, 5.3698)}), mock.patch.dict(globals(), {"PLACES": PLACES + extra}), self.llm(items):
            res = self.run_analyze(info(texte, uploader="foodmars.officiel"))
        self.assertEqual([r["nom"] for r in res], ["Farfalou", "Marseille en Cornet"])
        self.assertEqual((res[0]["prix"], res[1]["prix"]), (["les sandwichs poulpe sont fous, 12€"], ["compte 9,90€ le cornet"]))  # offre inventee ecartee
        self.assertNotIn("Imaginaire", res[0]["adresse"])  # adresse inventee ecartee : celle d'OpenStreetMap
        self.assertEqual({r["confiance"] for r in res}, {"haute"})

    def test_llm_adresse_ecrite_dans_le_texte(self):
        texte = "Pizza au feu de bois chez Don Camillo, 14 rue Paradis, Marseille. Menu midi 14€"
        items = [{"nom": "Don Camillo", "adresse": "14 rue Paradis, Marseille", "ville": "Marseille", "offres": ["Menu midi 14€"], "horaires": "n'importe quoi"}]
        extra = [("Don Camillo", "restaurant", 43.2970, 5.3698, "Marseille")]
        with mock.patch.dict(CITIES, {"marseille": (43.2965, 5.3698)}), mock.patch.dict(globals(), {"PLACES": PLACES + extra}), self.llm(items):
            res = self.run_analyze(info(texte))
        self.assertEqual([(r["nom"], r["confiance"], r["prix"], r["horaires"]) for r in res], [("Don Camillo", "haute", ["Menu midi 14€"], None)])  # horaires non ecrits : ecartes

    FARFALOU = "📍@farfalou.canebiere :\n♾️ 15% de réduction sur les sandwichs\n\n#marseille"  # inconnu d'OSM, pas d'adresse

    def web(self, extraits, cap=None, llm=None):
        """Analyse avec recherche web simulee -> (fiche, nombre de recherches). llm : reponse simulee de l'IA sur les extraits."""
        calls = []

        def fake(q, region="fr-fr"):
            calls.append(q)
            return [dict(r) for r in extraits]
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.dict(CITIES, {"marseille": (43.2965, 5.3698)}))
            st.enter_context(mock.patch.object(pipeline, "web_search", side_effect=fake))
            if llm is not None:  # 1er appel : lecture de la legende (on laisse les regles) ; 2e : lecture des extraits
                st.enter_context(mock.patch.object(pipeline, "ask", side_effect=[RuntimeError("legende : regles"), llm]))
            self._n = getattr(self, "_n", 0) + 1  # lien unique : sinon le cache des resultats renverrait l'analyse d'un autre test
            with mock.patch.object(pipeline, "get_info", return_value=info(cap or self.FARFALOU)):
                res = pipeline.analyze(f"https://exemple.test/{self.id()}/{self._n}")
        return res[0], len(calls)

    def test_web_adresse_trouvee_dans_un_extrait(self):
        f, n = self.web([{"titre": "Farfalou - Restaurant Marseille", "lien": "https://exemple.test/farfalou",
                          "extrait": "Farfalou, 12 Rue de la Canebière, 13001 Marseille. Sandwichs au poulpe."}])
        self.assertEqual((f["nom"], f["confiance"], f["adresse"], f["lat"], f["source_web"]),
                         ("Farfalou", "moyenne", "12 Rue de la Canebière, 13001 Marseille", 43.2965, "https://exemple.test/farfalou"))
        self.assertEqual(n, 1)  # la 1re recherche suffit : pas de requete suivante

    def test_web_extrait_d_un_autre_restaurant(self):
        f, _ = self.web([{"titre": "Chez Paul", "lien": "https://exemple.test/paul", "extrait": "Chez Paul, 3 rue de la République, 13001 Marseille"}])
        self.assertEqual((f["confiance"], f["lat"], f.get("source_web")), ("a_verifier", None, None))

    def test_web_adresse_inventee_par_l_ia(self):
        f, _ = self.web([{"titre": "Farfalou", "lien": "https://exemple.test/f", "extrait": "Farfalou, la meilleure panisse de Marseille"}],
                        llm=[{"restaurant": 1, "adresse": "99 rue Inventée, 13001 Marseille", "resultat": 1}])
        self.assertEqual((f["confiance"], f["lat"]), ("a_verifier", None))

    def test_web_adresse_recopiee_par_l_ia(self):  # « 12 La Canebière » : pas de type de voie, seule l'IA la repere
        f, _ = self.web([{"titre": "Farfalou", "lien": "https://exemple.test/f", "extrait": "Le Farfalou se trouve au 12 La Canebière, 13001 Marseille, ouvert 7j/7"}],
                        llm=[{"restaurant": "1", "adresse": "12 La Canebière, 13001 Marseille", "resultat": "1"}])
        self.assertEqual((f["confiance"], f["adresse"], f["source_web"]), ("moyenne", "12 La Canebière, 13001 Marseille", "https://exemple.test/f"))

    def test_web_adresse_dans_une_autre_ville(self):
        f, _ = self.web([{"titre": "Farfalou Paris", "lien": "https://exemple.test/p", "extrait": "Farfalou, 5 rue de Rivoli, 75001 Paris"}])
        self.assertEqual((f["confiance"], f["lat"]), ("a_verifier", None))

    def test_web_recherche_en_panne(self):
        class Boom:
            def text(self, *a, **k):
                raise RuntimeError("bloque")
        with mock.patch.dict(sys.modules, {"ddgs": types.SimpleNamespace(DDGS=Boom)}):
            self.assertEqual(REAL_WEB_SEARCH("Farfalou Marseille"), [])  # aucune erreur ne remonte
        f, _ = self.web([])
        self.assertEqual(f["confiance"], "a_verifier")

    def test_web_jamais_deux_fois_la_meme_recherche(self):
        _, n1 = self.web([])
        _, n2 = self.web([], cap=self.FARFALOU + " #streetfood")  # autre video, meme restaurant
        self.assertEqual((n1, n2), (3, 0))  # avec guillemets, sans, puis le pseudo ; ensuite plus jamais

    def test_web_desactivee(self):
        with mock.patch.dict(os.environ, {"TP_WEB": "0"}):
            f, n = self.web([])
        self.assertEqual((n, f["confiance"]), (0, "a_verifier"))

    GUAN = """REPAS À GAGNER 🎁🇨🇳

Adresse cachée à Châtelet : de la street food traditionnelle chinoise, la spécialité ici c’est les Jianbing

💰 Jianbing : 8€-14€

📍@madameguan
- 63 Rue Saint-Martin, 75004 Paris
- 50 Rue Godefroy Cavaignac, 75011 Paris
- 13 Rue des Canettes, 75006 Paris
- 33 Rue du Rocher, 75008 Paris

(Collab commerciale)

#concours #restaurant #paris"""

    def test_plusieurs_adresses_regles(self):  # sans IA : une fiche par adresse listee sous « 📍@enseigne »
        res = self.run_analyze(info(self.GUAN, uploader="sorties.paris"))
        self.assertEqual([r["adresse"] for r in res], ["63 Rue Saint-Martin, 75004 Paris", "50 Rue Godefroy Cavaignac, 75011 Paris",
                                                       "13 Rue des Canettes, 75006 Paris", "33 Rue du Rocher, 75008 Paris"])
        self.assertEqual({r["nom"] for r in res}, {"Madameguan"})
        self.assertTrue(all(r["lat"] for r in res))  # chaque adresse a son marqueur
        self.assertFalse(any("Rue" in x for r in res for x in r["prix"]))  # une adresse n'est pas une offre

    def test_pseudo_decoupe_grace_au_texte(self):  # « Rednoodle » -> « Red Noodle »
        texte = "Les meilleures nouilles ! @rednoodle.officiel\n📍 10 rue Esquermoise, Lille\nRed Noodle Paris, Red Noodle Lille"
        with self.llm([{"nom": "@rednoodle.officiel", "adresses": ["10 rue Esquermoise, Lille"]}]):
            self.assertEqual([r["nom"] for r in self.run_analyze(info(texte))], ["Red Noodle"])

    def test_plusieurs_adresses_llm(self):  # l'IA rend une liste ; une adresse inventee est ecartee
        items = [{"nom": "@madameguan", "adresses": ["63 Rue Saint-Martin, 75004 Paris", "50 Rue Godefroy Cavaignac, 75011 Paris", "99 Rue Inventée, 75001 Paris"],
                  "ville": "Paris", "offres": ["Jianbing : 8€-14€"]}]
        with self.llm(items):
            res = self.run_analyze(info(self.GUAN, uploader="sorties.paris"))
        self.assertEqual([(r["nom"], r["adresse"]) for r in res], [("Madameguan", "63 Rue Saint-Martin, 75004 Paris"), ("Madameguan", "50 Rue Godefroy Cavaignac, 75011 Paris")])
        self.assertTrue(all(r["prix"] == ["Jianbing : 8€-14€"] and r["lat"] for r in res))

    def test_adresses_collees_par_l_ia(self):  # ancien format : plusieurs adresses dans un seul champ
        items = [{"nom": "@madameguan", "adresse": "63 Rue Saint-Martin, 75004 Paris\n- 13 Rue des Canettes, 75006 Paris", "ville": "Paris"}]
        with self.llm(items):
            res = self.run_analyze(info(self.GUAN + " ", uploader="sorties.paris"))
        self.assertEqual([r["adresse"] for r in res], ["63 Rue Saint-Martin, 75004 Paris", "13 Rue des Canettes, 75006 Paris"])

    BRASSERIES = "pour la premiere adresse on va pres d'opera ou t'as comme un bouillon et on peut pas faire plus clair. on finit avec un lieu historique"
    PARIS_BOUILLONS = [("Comme Un Bouillon", "restaurant", 48.8686, 2.3326, "Paris"), ("Bouillon Chartier", "restaurant", 48.8718, 2.3434, "Paris")]

    def video(self, spoken, speech="", texte="Je teste 3 brasseries confidentielles pas cheres a Paris", extra=(), web=(), llm_web=None, llm_legende=None, web_fn=None):
        """Analyse ou la video (simulee) fournit les noms. llm_legende : ce que l'IA lit dans la legende (rien par defaut)."""
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.dict(CITIES, {"marseille": (43.2965, 5.3698), "tokyo": (35.68, 139.76), "bordeaux": (44.8378, -0.5792)}))
            st.enter_context(mock.patch.dict(globals(), {"PLACES": PLACES + list(extra)}))
            st.enter_context(mock.patch.object(pipeline, "video_stage", return_value=(spoken, speech)))
            st.enter_context(mock.patch.object(pipeline, "web_search", side_effect=web_fn) if web_fn else mock.patch.object(pipeline, "web_search", return_value=[dict(r) for r in web]))
            if llm_web is not None or llm_legende is not None:  # 1er appel : legende ; puis lecture des extraits web
                seq = [RuntimeError("legende") if llm_legende is None else llm_legende] + ([llm_web] if llm_web is not None else []) + [RuntimeError("fin")] * 5
                st.enter_context(mock.patch.object(pipeline, "ask", side_effect=seq))
            return self.run_analyze(info(texte))

    def test_video_nom_entendu_invente_ou_inconnu_ecarte(self):
        res = self.video([{"nom": "Bouillon Chartier", "ville": "Paris", "_src": "voix"},   # pas dans la transcription (OSM le connait pourtant)
                          {"nom": "lieu historique", "ville": "Paris", "_src": "voix"},     # dans la transcription, mais inconnu partout
                          {"nom": "Gardelion", "ville": "Paris", "_src": "voix"}],          # pas dans la transcription
                         self.BRASSERIES, extra=self.PARIS_BOUILLONS)
        self.assertEqual(res, [])

    def test_video_nom_entendu_coupe_retrouve(self):  # l'IA entend « bouillon » : c'est « Comme un Bouillon », pas Bouillon Chartier
        res = self.video([{"nom": "bouillon", "ville": "Paris", "_src": "voix"}], self.BRASSERIES, extra=self.PARIS_BOUILLONS)
        self.assertEqual([(r["nom"], r["confiance"], r["source"]) for r in res], [("Comme Un Bouillon", "haute", "video")])

    def test_video_nom_lu_a_l_ecran_cherche_sur_le_web(self):  # « HONORÉ » a l'ecran, inconnu d'OSM : adresse trouvee sur le web
        res = self.video([{"nom": "HONORÉ", "ville": "Marseille", "_src": "image"}], texte="L'adresse de Camille Lavabre a Marseille",
                         web=[{"titre": "Honoré - Marseille", "lien": "https://exemple.test/honore", "extrait": "Honoré, 28A Cours Estienne d'Orves, 13001 Marseille"}],
                         llm_web=[{"restaurant": 1, "adresse": "28A Cours Estienne d'Orves, 13001 Marseille", "resultat": 1}])
        self.assertEqual([(r["nom"], r["confiance"], r["adresse"], r["source"], r.get("source_web")) for r in res],
                         [("HONORÉ", "moyenne", "28A Cours Estienne d'Orves, 13001 Marseille", "video", "https://exemple.test/honore")])

    def test_video_nom_lu_a_l_ecran_garde_meme_inconnu(self):  # lu a l'ecran : garde « a verifier » pour que tu le voies
        res = self.video([{"nom": "Le Vaudesir", "ville": "Paris", "_src": "image"}])
        self.assertEqual([(r["nom"], r["confiance"]) for r in res], [("Le Vaudesir", "a_verifier")])

    HONORE_WEB = dict(web=[{"titre": "Honoré - Marseille", "lien": "https://exemple.test/honore", "extrait": "Honoré, 28A Cours Estienne d'Orves, 13001 Marseille"}],
                      llm_web=[{"restaurant": 1, "adresse": "28A Cours Estienne d'Orves, 13001 Marseille", "resultat": 1}])

    def test_legende_faible_on_regarde_la_video(self):  # « L'adresse de Camille Lavabre a Marseille » : une personne, pas le restaurant
        res = self.video([{"nom": "HONORÉ", "ville": "Marseille", "_src": "image"}], texte="L'adresse de Camille Lavabre a Marseille",
                         llm_legende=[{"nom": "Camille Lavabre", "ville": "Marseille"}], **self.HONORE_WEB)
        self.assertEqual([(r["nom"], r["confiance"], r["source"]) for r in res], [("HONORÉ", "moyenne", "video")])  # Camille Lavabre ecartee

    def test_legende_faible_et_video_vide(self):  # la video ne donne rien : on garde ce que disait la legende, « a verifier »
        res = self.video([], texte="On a teste Le Petit Zinc a Marseille", llm_legende=[{"nom": "Le Petit Zinc", "ville": "Marseille"}])
        self.assertEqual([(r["nom"], r["confiance"]) for r in res], [("Le Petit Zinc", "a_verifier")])

    def test_video_nom_entendu_mal_orthographie(self):  # Whisper ecrit « riversaid club »
        res = self.video([{"nom": "Riverside Club", "ville": "Tokyo", "_src": "voix"}], "numero trois le riversaid club c'est un bar",
                         extra=[("The Riverside Club", "bar", 35.66, 139.70, "Tokyo")])
        self.assertEqual([(r["nom"], r["confiance"]) for r in res], [("The Riverside Club", "haute")])

    def test_video_nom_entendu_avec_adresse_garde(self):  # inconnu d'OSM, mais une adresse localisee : on le garde
        res = self.video([{"nom": "commune", "adresse": "3-13 Minamiaoyama, Tokyo", "ville": "Tokyo", "_src": "voix"}], "ca s'appelle commune, 3-13 minamiaoyama, tokyo, le cadre est parfait")  # adresse dite a l'oral
        self.assertEqual([(r["nom"], r["confiance"], bool(r["lat"])) for r in res], [("commune", "a_verifier", True)])

    def test_trace_de_l_analyse(self):
        url = "https://exemple.test/trace"
        with mock.patch.object(pipeline, "get_info", return_value=info("Chez Paul, 12 rue de la Monnaie, Lille")):
            pipeline.analyze(url)
        wd = pipeline.DATA / "jobs" / hashlib.sha1(pipeline.canon(url)[0].encode()).hexdigest()[:10]
        kinds = {json.loads(l)["t"] for l in (wd / "trace.jsonl").read_text().splitlines()}
        self.assertTrue({"osm", "resultat"} <= kinds, kinds)

    def test_ville_n_est_pas_une_adresse(self):  # l'IA donne « MARSEILLE » comme adresse : legende quand meme trop vague, la video tranche
        res = self.video([{"nom": "HONORÉ", "ville": "Marseille", "_src": "image"}], texte="L'adresse de Camille Lavabre a MARSEILLE",
                         llm_legende=[{"nom": "Camille Lavabre", "adresses": ["MARSEILLE"], "ville": "Marseille"}], **self.HONORE_WEB)
        self.assertEqual([r["nom"] for r in res], ["HONORÉ"])

    def test_phrase_d_accroche_n_est_pas_un_nom(self):  # ishin : le titre n'est pas le restaurant ; « Ishin ramen » est dit dans la video
        res = self.video([{"nom": "Ishin ramen", "ville": "Bordeaux", "_src": "voix"}], "on t'emmene chez ishin ramen, un resto qui recree les echoppes japonaises",
                         texte="Le resto japonais le plus immersif de Bordeaux !",
                         llm_legende=[{"nom": "Le resto japonais le plus immersif de Bordeaux", "ville": "Bordeaux"}],
                         web=[{"titre": "Ishin Ramen Bordeaux", "lien": "https://exemple.test/ishin", "extrait": "Ishin Ramen, 12 rue des Bahutiers, 33000 Bordeaux"}],
                         llm_web=[{"restaurant": 1, "adresse": "12 rue des Bahutiers, 33000 Bordeaux", "resultat": 1}])
        self.assertEqual([(r["nom"], r["confiance"], r["adresse"]) for r in res], [("Ishin ramen", "moyenne", "12 rue des Bahutiers, 33000 Bordeaux")])

    def test_video_adresses_vagues_ou_inventees(self):  # trace reelle des brasseries
        spoken = [{"nom": "bouillon", "adresse": "près d'Opera", "ville": "Paris", "_src": "voix"},
                  {"nom": "lieu historique", "adresse": "14ème arrondissement", "ville": "Paris", "_src": "voix"},
                  {"nom": "LA CANTINE DIDEROT", "adresse": "12 rue de la Paix", "ville": "Paris", "_src": "image"},  # meme adresse pour deux noms :
                  {"nom": "Le Vaudésir", "adresse": "12 rue de la Paix", "ville": "Paris", "_src": "image"}]          # inventee
        res = self.video(spoken, self.BRASSERIES, extra=self.PARIS_BOUILLONS + [("La Cantine Diderot", "restaurant", 48.8445, 2.3743, "Paris")])
        self.assertEqual([(r["nom"], r["confiance"], r["lat"]) for r in res],
                         [("Comme Un Bouillon", "haute", "48.8686"), ("La Cantine Diderot", "haute", "48.8445"), ("Le Vaudésir", "a_verifier", None)])

    def test_adresse_hors_de_la_ville_ecartee(self):  # « pres d'Opera » localise a Nice : on ne place pas un restaurant de Marseille a Paris
        res = self.video([{"nom": "Le Fantome", "adresse": "5 rue de Rivoli, Paris", "ville": "Marseille", "_src": "image"}])
        self.assertEqual([(r["nom"], r["lat"]) for r in res], [("Le Fantome", None)])

    def test_vraie_adresse_et_nom_descriptif(self):
        oui = ["12 rue de la Paix", "Rue Longue Vie 31, 1050 Ixelles", "1-22 Ikutamaemaemachi, Tennoji-ku, Osaka", "東京都渋谷区神南1-22-7", "28A Cours Estienne d'Orves, 13001 Marseille"]
        non = ["MARSEILLE", "près d'Opera", "14ème arrondissement", "Nakameguro", "food court à Omotesando", "ramen ya"]
        self.assertEqual([pipeline.vraie_adresse(a) for a in oui + non], [True] * len(oui) + [False] * len(non))
        self.assertEqual([pipeline.nom_descriptif(n) for n in ["Le resto japonais le plus immersif de Bordeaux", "Le meilleur kebab", "Comme un Bouillon", "Le Hanbei"]],
                         [True, True, False, False])

    def test_adresse_ecrite_jamais_ecartee(self):  # l'IA donne ville « Lille » pour deux adresses dont une a Paris
        texte = "Les nouilles @rednoodle.officiel\n📍 5 rue de Rivoli, Paris\n📍 10 rue Esquermoise, Lille\nRed Noodle Paris"
        with self.llm([{"nom": "@rednoodle.officiel", "adresses": ["5 rue de Rivoli, Paris", "10 rue Esquermoise, Lille"], "ville": "Lille"}]):
            res = self.run_analyze(info(texte))
        self.assertEqual([(r["nom"], r["confiance"], float(r["lat"])) for r in res], [("Red Noodle", "a_verifier", 48.8566), ("Red Noodle", "haute", 50.6329)])

    def test_villes_en_liste(self):
        texte = "Les nouilles @rednoodle.officiel\n📍 5 rue de Rivoli, Paris\n📍 10 rue Esquermoise, Lille"
        with self.llm([{"nom": "@rednoodle.officiel", "adresses": ["5 rue de Rivoli, Paris", "10 rue Esquermoise, Lille"], "ville": ["Paris", "Lille"]}]):
            res = self.run_analyze(info(texte))
        self.assertEqual([float(r["lat"]) for r in res], [48.8566, 50.6329])

    def test_web_majuscules_puis_sans_guillemets(self):  # « HONORÉ » : 0 resultat ; « "Honoré" » entre guillemets : 0 ; sans guillemets : trouve
        qs = []

        def web(q, region="fr-fr"):
            qs.append(q)
            return [] if '"' in q else [{"titre": "Honoré Marseille", "lien": "https://exemple.test/h", "extrait": "Honoré, 28A Cours Estienne d'Orves, 13001 Marseille"}]
        res = self.video([{"nom": "HONORÉ", "ville": "MARSEILLE", "_src": "image"}], texte="L'adresse de Camille Lavabre a Marseille", web_fn=web,
                         llm_web=[{"restaurant": 1, "adresse": "28A Cours Estienne d'Orves, 13001 Marseille", "resultat": 1}])
        self.assertEqual(qs, ['"Honoré" Marseille adresse', "Honoré Marseille restaurant adresse"])
        self.assertEqual([(r["nom"], r["confiance"]) for r in res], [("HONORÉ", "moyenne")])

    def test_web_mots_distinctifs(self):  # les pages disent « Ishin », pas « Ishin ramen »
        res = self.video([{"nom": "ishin ramen", "ville": "bordeaux", "_src": "voix"}], "on t'emmene chez ishin ramen, un resto immersif",
                         web=[{"titre": "Ishin - Bordeaux", "lien": "https://exemple.test/ishin", "extrait": "Ishin, restaurant japonais, 12 rue des Bahutiers, 33000 Bordeaux"}],
                         llm_web=[{"restaurant": 1, "adresse": "12 rue des Bahutiers, 33000 Bordeaux", "resultat": 1}])
        self.assertEqual([(r["nom"], r["confiance"]) for r in res], [("ishin ramen", "moyenne")])
        self.assertTrue(pipeline.cite("Ishin ramen", "Ishin, restaurant japonais") and not pipeline.cite("Ishin ramen", "Ramen Bordeaux"))

    def test_nom_qui_est_une_adresse(self):  # la legende ne donne que l'adresse : nom inconnu, ou le seul restaurant a cette adresse
        texte = "CONCEPT UNIQUE EN FRANCE 🍣\n📍156 Rue Saint Martin 75003\n💰 9 - 12€ les 10 pièces"
        item = [{"nom": "156 Rue Saint Martin 75003", "adresses": [], "ville": "Paris"}]
        with self.llm(item):
            res = self.run_analyze(info(texte))
        self.assertEqual([(r["nom"], r["confiance"], bool(r["lat"])) for r in res], [(None, "a_verifier", True)])
        with self.llm(item), mock.patch.dict(globals(), {"PLACES": PLACES + [("Sushi Halal", "restaurant", 48.8566, 2.3522, "Paris")]}):
            res = self.run_analyze(info(texte))
        self.assertEqual([(r["nom"], r["confiance"]) for r in res], [("Sushi Halal", "moyenne")])

    def test_geocode_sans_arrondissement(self):  # « ... 13016 MARSEILLE 16E » introuvable tel quel
        def nom(p):
            return [] if "16E" in p["q"].upper() else [{"lat": "43.36", "lon": "5.34", "address": {"city": "Marseille"}}]
        with mock.patch.object(pipeline, "nominatim", side_effect=nom):
            g = pipeline.geocode("11 AV DE SAINT-ANTOINE 13016 MARSEILLE 16E")
        self.assertEqual((g["lat"], g["precis"]), (43.36, True))

    def test_une_personne_n_est_pas_un_restaurant(self):
        oui = [("Camille Lavabre", "L’adresse de Camille Lavabre à Marseille"), ("Benjamin Mathieu", "un restaurant ouvert par le chef Benjamin Mathieu"),
               ("Hélène Darroze", "les adresses de la cheffe Hélène Darroze")]
        non = [("Le Vaudésir", "l'adresse Le Vaudésir"), ("Chez Paul", "le restaurant Chez Paul"), ("Comme un Bouillon", "le resto de Comme un Bouillon"),
               ("Camille Lavabre", "Camille Lavabre, 12 rue de la Paix")]  # cite sans etre presentee comme une personne : on laisse l'IA decider
        self.assertEqual([pipeline.personne(n, t) for n, t in oui + non], [True] * len(oui) + [False] * len(non))

    def test_honore_la_personne_du_titre_est_ignoree(self):  # vrai cas : la legende ne nomme qu'une personne, le restaurant est a l'ecran
        qs = []

        def web(q, region="fr-fr"):
            qs.append(q)
            return [dict(r) for r in self.HONORE_WEB["web"]]
        res = self.video([{"nom": "HONORÉ", "ville": "Marseille", "_src": "image"}, {"nom": "Benjamin Mathieu", "ville": "Marseille", "_src": "voix"}],
                         "c'est un restaurant qui a ouvert il y a pas longtemps par le chef benjamin mathieu", texte="L’adresse de Camille Lavabre à Marseille",
                         llm_legende=[{"nom": "Camille Lavabre", "adresses": ["MARSEILLE"], "ville": "Marseille"}], web_fn=web, llm_web=self.HONORE_WEB["llm_web"])
        self.assertEqual([(r["nom"], r["confiance"]) for r in res], [("HONORÉ", "moyenne")])
        self.assertFalse(any("Camille" in q or "Benjamin" in q for q in qs))  # jamais cherches sur le web

    def test_ressemblance_des_noms(self):  # vrai cas : « HONORÉ » relie a tort a « Maison Saint Honoré » (1,4 km plus loin)
        meme = [("Hanbei", "Le Hanbei"), ("Holyburek", "Holyburek Paris"), ("ishin ramen", "Ishin"), ("Le Vaudésir", "Vaudésir"),
                ("Red Noodle", "Rednoodle"), ("Marseille en Cornet", "Marseille En Cornet"), ("Madame Guan", "madameguan")]
        autre = [("HONORÉ", "Maison Saint Honoré"), ("bouillon", "Bouillon Chartier"), ("Camille", "Camille Lavabre Traiteur Gourmet")]
        self.assertEqual([pipeline.sim(a, b) >= 0.75 for a, b in meme + autre], [True] * len(meme) + [False] * len(autre))

    def test_rue_n_est_pas_un_nom(self):  # sans IA : « 📍156 Rue Saint Martin 75003 » ne donne pas un restaurant « Rue Saint Martin »
        self.assertIsNone(pipeline.parse_caption("CONCEPT UNIQUE EN FRANCE 🍣\n📍156 Rue Saint Martin 75003\n💰 9 - 12€")["nom"])

    def test_meme_fiche_une_seule_fois(self):  # adresse ecrite dans le titre ET la description (longue-vie)
        t = "📍« Le Longue Vie », Rue Longue Vie 31, 1050 Ixelles, Belgique (invitation-partenariat)"
        d = "📍 « Le Longue Vie », Rue Longue Vie 31, 1050 Ixelles, Belgique"
        with mock.patch.dict(CITIES, {"ixelles": (50.83, 4.37)}), mock.patch.dict(globals(), {"PLACES": PLACES + [("Le Longue Vie", "restaurant", 50.83, 4.37, "Ixelles")]}):
            res = self.run_analyze(info(d, title=t))
        self.assertEqual([(r["nom"], r["confiance"]) for r in res], [("Le Longue Vie", "haute")])

    def test_nominatim_une_requete_par_seconde(self):
        pipeline.NOMINATIM_DELAY = 0.3
        CALLS.clear()
        ts = [threading.Thread(target=pipeline.nominatim, args=({"q": "lille"},)) for _ in range(4)]  # worker + routes web en meme temps
        [t.start() for t in ts]; [t.join() for t in ts]
        gaps = [b[0] - a[0] for a, b in zip(CALLS, CALLS[1:])]
        self.assertEqual(len(gaps), 3); self.assertGreaterEqual(min(gaps), 0.28)
        self.assertGreaterEqual(DEFAULT_DELAY, 1.0)  # reglage reel : au moins 1 s entre deux requetes


# ---------- serveur reel ----------
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


class Canon(unittest.TestCase):  # une video = une cle, quels que soient les parametres du lien
    def meme(self, *urls):
        keys = {pipeline.canon(u)[0] for u in urls}
        self.assertEqual(len(keys), 1, keys)

    def test_youtube_toutes_les_formes(self):
        self.meme("https://youtube.com/shorts/dQw4w9WgXcQ?si=AAA", "https://youtu.be/dQw4w9WgXcQ?si=BBB",
                  "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5s&si=CCC", "https://m.youtube.com/shorts/dQw4w9WgXcQ")

    def test_instagram(self):
        self.meme("https://www.instagram.com/reel/DaYGFJ_NRcc/?utm_source=ig_web_copy_link&rpxt=MzRl", "https://instagram.com/reel/DaYGFJ_NRcc",
                  "https://www.instagram.com/un.compte/reel/DaYGFJ_NRcc/?igsh=xyz", "https://www.instagram.com/p/DaYGFJ_NRcc/")

    def test_tiktok(self):
        self.meme("https://www.tiktok.com/@u/video/7123456789012345678?is_from_webapp=1&sender_device=pc", "https://tiktok.com/@u/video/7123456789012345678/")

    def test_autre_site_garde_les_parametres_utiles(self):
        self.meme("https://exemple.test/v?id=3&utm_source=x", "https://exemple.test/v/?id=3")
        self.assertNotEqual(pipeline.canon("https://exemple.test/v?id=3")[0], pipeline.canon("https://exemple.test/v?id=4")[0])

    def test_videos_differentes_et_lien_propre(self):
        self.assertNotEqual(pipeline.canon("https://youtube.com/shorts/dQw4w9WgXcQ")[0], pipeline.canon("https://youtube.com/shorts/aaaaaaaaaaa")[0])
        self.assertEqual(pipeline.canon("https://youtu.be/dQw4w9WgXcQ?si=ZZZ")[1], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")


class Cache(unittest.TestCase):  # une video deja analysee ne relance ni telechargement, ni Whisper, ni Qwen
    def setUp(self):
        pipeline.NOMINATIM_DELAY = 0
        pipeline.WEB_PAUSE = 0; pipeline.WEB_CACHE.unlink(missing_ok=True)
        p = mock.patch.object(pipeline, "web_search", return_value=[]); p.start(); self.addCleanup(p.stop)  # jamais de vraie recherche web en test
        p = mock.patch.object(pipeline, "ask", side_effect=RuntimeError("pas de LLM en test")); p.start(); self.addCleanup(p.stop)
        p = mock.patch("urllib.request.urlopen", fake_nominatim); p.start(); self.addCleanup(p.stop)

    def test_meme_video_deux_liens_une_seule_analyse(self):
        with mock.patch.object(pipeline, "get_info", return_value=info("Une adresse sans nom de lieu")) as gi, \
             mock.patch.object(pipeline, "video_stage", return_value=([], "")) as vs:
            a = pipeline.analyze("https://youtube.com/shorts/Zz9Zz9Zz9Zz?si=AAA")
            b = pipeline.analyze("https://youtu.be/Zz9Zz9Zz9Zz?si=BBB")
        self.assertEqual(a, b)
        self.assertEqual((gi.call_count, vs.call_count), (1, 1))

    def test_forget_permet_de_reanalyser(self):
        url = "https://youtu.be/Qq1Qq1Qq1Qq"
        with mock.patch.object(pipeline, "get_info", return_value=info("rien")) as gi, mock.patch.object(pipeline, "video_stage", return_value=([], "")):
            pipeline.analyze(url); pipeline.analyze(url); pipeline.forget(url); pipeline.analyze(url)
        self.assertEqual(gi.call_count, 2)

    def test_ancien_dossier_de_cache_conserve(self):  # avant : dossier = hash du lien exact
        raw = "https://youtu.be/Ab1Ab1Ab1Ab?si=OLD"
        old = pipeline.DATA / "jobs" / hashlib.sha1(raw.encode()).hexdigest()[:10]; old.mkdir(parents=True)
        (old / "video.info.json").write_text(json.dumps(info("x")))
        with mock.patch.object(pipeline, "video_stage", return_value=([], "")):
            pipeline.analyze(raw)  # get_info relit le cache : pas de yt-dlp
        new = pipeline.DATA / "jobs" / hashlib.sha1(pipeline.canon(raw)[0].encode()).hexdigest()[:10]
        self.assertTrue((new / "video.info.json").exists() and (new / "result.json").exists() and not old.exists())


class Banc(unittest.TestCase):  # calcul du score du banc d'essai (bench/bench.py)
    @classmethod
    def setUpClass(cls):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location("bench", pathlib.Path(__file__).resolve().parent / "bench" / "bench.py")
        cls.b = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.b)

    def test_score(self):
        F = pipeline.fiche
        cas = {"attendus": [{"nom": "Red Noodle", "ville": "Lille", "adresse": "B"}, {"nom": "Red Noodle", "ville": "Paris", "adresse": "A"},
                            {"nom": None, "ville": "Paris", "adresse": "C"}, {"nom": "Royal Buffet", "ville": "Marseille"}]}
        coords = {"A": [48.8710, 2.3358], "B": [50.6331, 3.0626], "C": [48.8617, 2.3518]}
        res = [F(nom="Red Noodle", ville="Paris", lat="48.8712", lon="2.3360"), F(nom="Red Noodle", ville="Lille"),
               F(nom="156 Rue Saint Martin 75003", ville="Paris", lat=48.8618, lon=2.3519), F(nom="Crep'eat", ville="Marseille", lat=43.36, lon=5.35)]
        s = self.b.score(cas, res, coords)
        self.assertEqual((s["trouves"], s["attendus"], s["adresses_ok"], s["adresses_attendues"]), (3, 4, 2, 3))
        self.assertEqual(s["lignes"][0]["fiche"]["ville"], "Lille")  # chaque Red Noodle va avec sa ville
        self.assertEqual(s["faux_positifs"], ["Crep'eat"])
        self.assertEqual(s["noms_suspects"], ["156 Rue Saint Martin 75003"])
        self.assertEqual(self.b.score({**cas, "partiel": True}, res, coords)["faux_positifs"], [])  # liste incomplete : pas de fiche « en trop »


class OpenRouter(unittest.TestCase):  # IA en ligne (modeles gratuits), avec repli sur l'IA locale
    def setUp(self):
        p = mock.patch.dict(os.environ, {"TP_LLM": "openrouter", "OPENROUTER_API_KEY": "cle-de-test"}); p.start(); self.addCleanup(p.stop)
        q = mock.patch.object(pipeline, "lit_images", return_value=None); q.start(); self.addCleanup(q.stop)  # jamais le vrai catalogue
        pipeline.STATS.update(openrouter=0, repli=0, local=0, quota_jour=0)
        pipeline.STATS_MODELES.clear(); pipeline._or_pause.clear()

    def local(self, sortie):  # IA locale simulee
        return mock.patch.object(pipeline, "run", return_value=mock.Mock(stdout=sortie))

    def test_openrouter_repond(self):
        with mock.patch.object(pipeline, "ask_openrouter", return_value=('```json\n[{"nom": "Farfalou"}]\n```', "x/modele:free")), \
             mock.patch.object(pipeline, "run", side_effect=AssertionError("pas d'IA locale")):
            self.assertEqual(pipeline.ask("consigne"), [{"nom": "Farfalou"}])
        self.assertEqual(pipeline.STATS["openrouter"], 1)

    def test_repli_sur_l_ia_locale(self):  # quota atteint (429) : l'analyse continue avec Qwen en local
        err = urllib.error.HTTPError(pipeline.OR_URL, 429, "Rate limit exceeded", {}, None)
        with mock.patch.object(pipeline, "ask_openrouter", side_effect=err), self.local('[{"nom": "Local"}]'):
            self.assertEqual(pipeline.ask("consigne"), [{"nom": "Local"}])
        self.assertEqual((pipeline.STATS["repli"], pipeline.STATS["local"]), (1, 1))

    def test_sans_cle_ia_locale(self):
        with mock.patch.object(pipeline, "or_cle", return_value=""), mock.patch.object(pipeline, "ask_openrouter", side_effect=AssertionError("pas sans cle")), \
             self.local("[]"):
            self.assertEqual(pipeline.ask("consigne"), [])

    def test_requete_envoyee(self):  # images de la video + routeur gratuit + cle dans l'en-tete
        envoye = {}

        def urlopen(req, timeout=None):
            envoye.update(url=req.full_url, corps=json.loads(req.data), auth=req.get_header("Authorization"))
            return mock.Mock(read=lambda: json.dumps({"model": "x/vision:free", "choices": [{"message": {"content": "[]"}}]}).encode())
        with mock.patch.object(pipeline, "images_video", return_value=["QUJD", "REVG"]), mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch.dict(os.environ, {"TP_OR_MODELES": "openrouter/free"}):
            texte, modele = pipeline.ask_openrouter("lis les enseignes", ("--video", "/tmp/v.mp4", "--fps", "1", "--video-max-frames", "16"), 300)
        c = envoye["corps"]
        self.assertEqual((envoye["url"], envoye["auth"], c["model"], modele), (pipeline.OR_URL, "Bearer cle-de-test", "openrouter/free", "x/vision:free"))
        self.assertEqual([x["type"] for x in c["messages"][0]["content"]], ["text", "image_url", "image_url"])
        self.assertTrue(c["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_plusieurs_modeles(self):  # TP_OR_MODELES : liste essayee dans l'ordre par OpenRouter
        with mock.patch.dict(os.environ, {"TP_OR_MODELES": "a/vision:free, openrouter/free"}):
            self.assertEqual(pipeline.or_modeles(), ["a/vision:free", "openrouter/free"])

    def reponses(self, contenus):  # faux OpenRouter : une reponse par modele, dans l'ordre
        envoye = []

        def urlopen(req, timeout=None):
            corps = json.loads(req.data); envoye.append(corps)
            c = contenus[len(envoye) - 1]
            if isinstance(c, Exception):
                raise c
            return mock.Mock(read=lambda: json.dumps({"model": corps["model"], "choices": [{"message": {"content": c}, "finish_reason": "length" if not c else "stop"}]}).encode())
        return envoye, mock.patch("urllib.request.urlopen", urlopen)

    def test_reponse_vide_modele_suivant(self):  # vrai cas : Nemotron « reflechit » et ne repond rien
        envoye, p = self.reponses(["", '[{"nom": "Farfalou"}]'])
        with p, mock.patch.dict(os.environ, {"TP_OR_MODELES": "nvidia/nemotron-3.5-super-vl:free,google/gemma-4-26b-a4b-it:free"}):
            texte, modele = pipeline.ask_openrouter("consigne", (), 300)
        self.assertEqual((modele, [c["model"] for c in envoye]), ("google/gemma-4-26b-a4b-it:free", ["nvidia/nemotron-3.5-super-vl:free", "google/gemma-4-26b-a4b-it:free"]))
        self.assertTrue(envoye[0]["max_tokens"] >= 2000 and envoye[0]["reasoning"] == {"effort": "low"})

    def test_tout_vide_repli_local(self):  # aucune reponse exploitable : l'IA locale prend le relais (et non « aucun restaurant »)
        envoye, p = self.reponses(["", urllib.error.HTTPError(pipeline.OR_URL, 429, "quota", {}, None)])
        with p, mock.patch.dict(os.environ, {"TP_OR_MODELES": "a:free,b:free"}), self.local('[{"nom": "Local"}]'):
            self.assertEqual(pipeline.ask("consigne"), [{"nom": "Local"}])
        self.assertEqual((pipeline.STATS["repli"], len(envoye)), (1, 2))

    def test_liste_par_defaut(self):  # modeles_openrouter.txt : l'ordre choisi, le routeur gratuit en dernier
        with mock.patch.dict(os.environ, {"TP_OR_MODELES": ""}):
            m = pipeline.or_modeles()
        f = pathlib.Path(pipeline.__file__).resolve().parent / "modeles_openrouter.txt"
        self.assertEqual(m, [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.strip().startswith("#")])
        self.assertEqual(m[-1], "openrouter/free")

    def test_reponse_illisible_modele_suivant(self):  # du bavardage sans JSON : pas « aucun restaurant », on essaie le suivant
        envoye, p = self.reponses(["Je pense qu'il s'agit de Farfalou.", '[{"nom": "Farfalou"}]'])
        with p, mock.patch.dict(os.environ, {"TP_OR_MODELES": "a:free,b:free"}):
            self.assertEqual(pipeline.ask("consigne"), [{"nom": "Farfalou"}])
        self.assertEqual((pipeline.STATS_MODELES["a:free"]["illisibles"], pipeline.STATS_MODELES["b:free"]["ok"]), (1, 1))

    def test_aucun_restaurant_est_une_reponse(self):  # [] : le modele a lu et n'a rien trouve, on ne consomme pas les suivants
        envoye, p = self.reponses(["[]"])
        with p, mock.patch.dict(os.environ, {"TP_OR_MODELES": "a:free,b:free"}):
            self.assertEqual(pipeline.ask("consigne"), [])
        self.assertEqual(len(envoye), 1)

    def test_video_saute_les_modeles_sans_images(self):
        envoye, p = self.reponses(['[{"nom": "Ishin"}]'])
        with p, mock.patch.dict(os.environ, {"TP_OR_MODELES": "texte:free,vision:free"}), mock.patch.object(pipeline, "images_video", return_value=["QUJD"]), \
             mock.patch.object(pipeline, "lit_images", side_effect=lambda m: m.startswith("vision")):
            texte, modele = pipeline.ask_openrouter("lis", ("--video", "/tmp/v.mp4"), 300)
        self.assertEqual((modele, pipeline.STATS_MODELES["texte:free"]["sautes"], pipeline.STATS_MODELES["texte:free"]["appels"]), ("vision:free", 1, 0))

    def test_legende_sans_images_garde_les_modeles_texte(self):
        envoye, p = self.reponses(['[{"nom": "Compass"}]'])
        with p, mock.patch.dict(os.environ, {"TP_OR_MODELES": "texte:free,vision:free"}), mock.patch.object(pipeline, "lit_images", return_value=False):
            self.assertEqual(pipeline.ask_openrouter("lis", (), 300)[1], "texte:free")

    def test_stats_par_modele(self):
        envoye, p = self.reponses(["", urllib.error.HTTPError(pipeline.OR_URL, 500, "panne", {}, None), '[{"nom": "X"}]'])
        with p, mock.patch.dict(os.environ, {"TP_OR_MODELES": "a:free,b:free,c:free"}):
            pipeline.ask("consigne")
        st = pipeline.STATS_MODELES
        self.assertEqual((st["a:free"]["vides"], st["b:free"]["erreurs"], st["c:free"]["ok"], st["c:free"]["appels"]), (1, 1, 1, 1))
        self.assertGreaterEqual(st["c:free"]["secondes"], 0)

    def test_429_pause_puis_modele_suivant(self):  # limite par minute : ce modele est mis de cote une minute
        envoye, p = self.reponses([urllib.error.HTTPError(pipeline.OR_URL, 429, "quota", {}, None), '[{"nom": "X"}]', '[{"nom": "Y"}]'])
        with p, mock.patch.dict(os.environ, {"TP_OR_MODELES": "a:free,b:free"}):
            pipeline.ask("un"); pipeline.ask("deux")
        self.assertEqual([c["model"] for c in envoye], ["a:free", "b:free", "b:free"])  # a:free pas rappele pendant sa pause
        self.assertEqual(pipeline.STATS_MODELES["a:free"]["quota"], 1)

    def test_banc_attend_au_lieu_de_passer(self):  # TP_OR_ATTENTE=1 : le banc juge le modele, pas la file d'attente
        envoye, p = self.reponses([urllib.error.HTTPError(pipeline.OR_URL, 429, "quota", {}, None), '[{"nom": "X"}]'])
        with p, mock.patch.dict(os.environ, {"TP_OR_MODELES": "a:free,b:free", "TP_OR_ATTENTE": "1"}), mock.patch("time.sleep") as dodo:
            self.assertEqual(pipeline.ask_openrouter("lis", (), 300)[1], "a:free")
        self.assertTrue(dodo.called)

    def test_quota_du_jour(self):
        err = urllib.error.HTTPError(pipeline.OR_URL, 429, "quota", {}, __import__("io").BytesIO(b'{"error":{"message":"Rate limit exceeded: free-models-per-day"}}'))
        envoye, p = self.reponses([err])
        with p, mock.patch.dict(os.environ, {"TP_OR_MODELES": "a:free", "TP_OR_ATTENTE": "1"}), mock.patch("time.sleep", side_effect=AssertionError("pas d'attente")):
            with self.assertRaises(RuntimeError):
                pipeline.ask_openrouter("lis", (), 300)
        self.assertEqual(pipeline.STATS["quota_jour"], 1)

    def test_json_ok(self):
        self.assertEqual(pipeline.json_ok('Voici :\n```json\n[{"nom": "A"}]\n```'), [{"nom": "A"}])
        self.assertEqual(pipeline.json_ok("[]"), [])
        self.assertEqual(pipeline.json_ok('Rien [1] puis [{"nom": "B", "offres": ["menu [midi]"]}] fin'), [{"nom": "B", "offres": ["menu [midi]"]}])
        self.assertIsNone(pipeline.json_ok("Je ne vois aucun nom."))
        self.assertIsNone(pipeline.json_ok('[{"nom": "A",'))

    def test_ia_indisponible_n_arrete_pas_la_video(self):  # sans repli local : la lecture echoue, l'analyse continue
        d = pathlib.Path(tempfile.mkdtemp()); (d / "video.mp4").write_bytes(b"x"); (d / "a.txt").write_text("on va chez bouillon")
        with mock.patch.object(pipeline, "ask", side_effect=RuntimeError("OpenRouter indisponible")):
            self.assertEqual(pipeline.video_stage("https://youtube.com/shorts/x", d, "", lambda s: None), ([], "on va chez bouillon"))

    def test_lit_images_depuis_le_catalogue(self):
        cat = {"data": [{"id": "g/vision:free", "architecture": {"input_modalities": ["text", "image"]}}, {"id": "t/texte:free", "architecture": {"input_modalities": ["text"]}}]}
        with mock.patch.object(pipeline, "OR_INFOS", pathlib.Path(tempfile.mkdtemp()) / "m.json"), \
             mock.patch("urllib.request.urlopen", lambda req, timeout=None: mock.Mock(read=lambda: json.dumps(cat).encode())):
            f = LIT_IMAGES
            self.assertEqual((f("g/vision:free"), f("t/texte:free"), f("inconnu:free")), (True, False, None))

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "ffmpeg absent")
    def test_images_reparties_sur_la_video(self):
        d = pathlib.Path(tempfile.mkdtemp()); v = d / "video.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=10", str(v)], check=True)
        imgs = pipeline.images_video(v, 4)
        self.assertEqual(len(imgs), 4)
        self.assertEqual(len(list((d / "images").glob("*.jpg"))), 4)  # en cache pour la prochaine fois


class Voix(unittest.TestCase):  # cas reels du banc OpenRouter (sushi-saint-martin, honore, ishin)
    def test_nom_vide_garde_l_adresse(self):  # l'IA laisse "nom" vide : la fiche garde l'adresse, sans nom invente
        leg = "CONCEPT UNIQUE EN FRANCE 🍣\n📍156 Rue Saint Martin 75003\n💰 9 - 12€ les 10 pièces\n#sushi #restohalal"
        rep = [{"nom": "", "adresses": ["156 Rue Saint Martin 75003"], "ville": "Paris", "offres": ["9 - 12€ les 10 pièces"]}]
        with mock.patch.object(pipeline, "ask", return_value=rep):
            out = pipeline.llm_extract(leg)
        self.assertEqual([(x["nom"], x["adresse"]) for x in out], [(None, "156 Rue Saint Martin 75003")])

    def test_mot_generique_pas_cherche_alentour(self):  # « Sushi » cherche a 2 km donnait un autre restaurant
        vus = []
        with mock.patch.object(pipeline, "nominatim", lambda p: vus.append(p["q"]) or []):
            self.assertIsNone(pipeline.osm("Sushi", near=(48.86, 2.35, 0.02)))
        self.assertEqual(vus, [])

    def test_orthographe_entendue(self):
        self.assertTrue(pipeline.cite("Ishin ramen", "Isshin Ramen - Restaurant japonais Bordeaux", flou=True))
        self.assertFalse(pipeline.cite("Ishin ramen", "Isshin Ramen - Restaurant japonais Bordeaux"))  # ecrit : orthographe exacte
        self.assertFalse(pipeline.cite("Ishin ramen", "Ichiban Ramen Bordeaux", flou=True))

    def test_nom_entendu_confirme_par_un_lieu_ou_l_on_mange(self):
        res = {"\"chez Gonard\" Marseille adresse": [{"titre": "GONNARD Plomberie", "lien": "https://annuaire.fr/1", "extrait": "Gonard, 116 avenue de la Timone, 13010 Marseille"}],
               "\"Ishin ramen\" Bordeaux adresse": [{"titre": "Isshin Ramen - TheFork", "lien": "https://www.thefork.fr/r/1", "extrait": "Restaurant japonais, 12 rue Sainte-Catherine, 33000 Bordeaux"}]}
        with mock.patch.object(pipeline, "web_search", lambda q, region="fr-fr": res.get(q, [])), mock.patch.object(pipeline, "ask", return_value=[]), \
             mock.patch.object(pipeline, "WEB_CACHE", pathlib.Path(tempfile.mkdtemp()) / "w.json"), mock.patch.object(pipeline, "WEB_PAUSE", 0):
            out = pipeline.web_addresses([{"nom": "chez Gonard", "ville": "Marseille", "voix": True}, {"nom": "Ishin ramen", "ville": "Bordeaux", "voix": True}])
        self.assertIsNone(out[0])  # un plombier homonyme ne confirme pas un restaurant
        self.assertEqual(out[1]["adresse"], "12 rue Sainte-Catherine, 33000 Bordeaux")
        self.assertEqual(out[1]["nom"], "Isshin ramen")  # orthographe de la page, pas celle de la transcription

    def test_orthographe(self):
        self.assertEqual(pipeline.orthographe("Ishin ramen", ["ISSHIN RAMEN Bordeaux", "Isshin Ramen - TheFork"]), "Isshin ramen")
        self.assertEqual(pipeline.orthographe("Le Vaudésir", ["Le Vaudésir - Brasserie"]), "Le Vaudésir")  # deja juste : inchange


class AdresseWeb(unittest.TestCase):  # cas ishin : l'IA ajoute la ville, l'extrait coupe autrement
    def test_ville_ajoutee_par_l_ia(self):
        r = {"titre": "Isshin Ramen Bordeaux", "extrait": "Isshin Ramen, 22 Rue Fernand Philippart 33000 Bordeaux. Ramens et donburi."}
        self.assertTrue(pipeline.addr_in("22 Rue Fernand Philippart, Bordeaux", r))
        self.assertFalse(pipeline.addr_in("24 Rue Fernand Philippart, Bordeaux", r))  # numero invente

    def test_extrait_coupe_aux_deux_points(self):
        hit = [{"titre": "Isshin Ramen - menu", "lien": "https://isshinramen.com/menu", "extrait": "22 Rue Fernand Philippart : ramens, donburi, entrées, avec les prix"}]
        with mock.patch.object(pipeline, "web_search", lambda q, region="fr-fr": hit), mock.patch.object(pipeline, "ask", return_value=[]), \
             mock.patch.object(pipeline, "WEB_CACHE", pathlib.Path(tempfile.mkdtemp()) / "w.json"), mock.patch.object(pipeline, "WEB_PAUSE", 0):
            out = pipeline.web_addresses([{"nom": "Isshin Ramen", "ville": "Bordeaux"}])
        self.assertEqual(out[0]["adresse"], "22 Rue Fernand Philippart")


class EnPassant(unittest.TestCase):  # seulement les restaurants que la video presente
    T = ("On va aller chez Gonard.\nEt du coup, c'est quoi, Doré ?\nAlors, c'est un restaurant qui a ouvert il y a pas longtemps par le chef Benjamin Mathieu,\n"
         "qui a deux autres restos que j'adore, la grotte et le grand barbecue de un plaisir pour les papiers.\n")

    def test_autres_restos_du_chef(self):
        self.assertTrue(pipeline.en_passant("la grotte", self.T))
        self.assertFalse(pipeline.en_passant("Doré", self.T))  # le restaurant presente
        self.assertFalse(pipeline.en_passant("Ishin ramen", "On t'emmène chez Ishin ramen, un resto qui recrée l'ambiance des échoppes japonaises."))

    def test_comparaison(self):
        self.assertTrue(pipeline.en_passant("Big Fernand", "Ici les burgers sont fondants, comme chez Big Fernand."))


class BancModeles(unittest.TestCase):  # bench/modeles.py : note, classement, ordre conseille, caches communs
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bench_modeles", pathlib.Path(__file__).resolve().parent / "bench" / "modeles.py")
        cls.m = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.m)

    def r(self, modele, trouves, adresses, en_trop=0, ok=5, appels=5, t=3.0):
        return {"modele": modele, "trouves": trouves, "attendus": 10, "adresses_ok": adresses, "adresses_attendues": 10, "en_trop": en_trop,
                "ok": ok, "appels": appels, "temps_reponse": t if ok else None}

    def test_note(self):
        self.assertEqual(self.m.note(self.r("a", 10, 10)), 100)
        self.assertEqual(self.m.note(self.r("a", 5, 0, en_trop=2)), 35)
        self.assertLess(self.m.note(self.r("muet", 10, 10, ok=0)), self.m.note(self.r("bavard", 10, 10)))  # trouves par les regles seules

    def test_classement_et_ordre(self):
        res = [self.r("lent", 9, 9, t=20), self.r("rapide", 9, 9, t=2), self.r("faux", 10, 10, en_trop=5), self.r("muet", 0, 0, ok=0), self.r("openrouter/free", 8, 8)]
        self.assertEqual([x["modele"] for x in self.m.classer(res)][:3], ["rapide", "lent", "openrouter/free"])  # a note egale : le plus rapide
        self.assertEqual(self.m.ordre(res), ["rapide", "lent", "faux", "openrouter/free"])  # muet retire, routeur en dernier

    def test_memes_donnees_pour_tous(self):  # OSM et web interroges une seule fois pour tous les modeles
        appels = []
        with mock.patch.object(pipeline, "nominatim", lambda params: appels.append(params) or [{"lat": "1"}]), \
             mock.patch.object(pipeline, "web_search", lambda q, region="fr-fr": appels.append(q) or []), \
             mock.patch.object(pipeline, "DATA", pathlib.Path(tempfile.mkdtemp())):
            self.m.memoriser()
            for _ in range(3):
                self.assertEqual(pipeline.nominatim({"q": "Compass"}), [{"lat": "1"}])
                pipeline.web_search("Compass Paris")
        self.assertEqual(appels, [{"q": "Compass"}, "Compass Paris", "Compass Paris", "Compass Paris"])  # un vide web peut etre passager : retente


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.srv = uvicorn.Server(uvicorn.Config(server.app, port=cls.port, log_level="error"))
        threading.Thread(target=cls.srv.run, daemon=True).start()
        while not cls.srv.started:
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.srv.should_exit = True

    def setUp(self):
        pipeline.NOMINATIM_DELAY = 0
        p = mock.patch("urllib.request.urlopen", fake_nominatim); p.start(); self.addCleanup(p.stop)
        with server.db() as c:
            c.execute("delete from places"); c.execute("delete from jobs")

    def call(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with REAL_URLOPEN(req) as r:  # urlopen est simule pour Nominatim : on garde le vrai pour parler a notre serveur
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def places(self):
        return {p["id"]: p for p in self.call("GET", "/places")[1]}

    def wait_job(self):
        for _ in range(100):
            js = self.call("GET", "/jobs")[1]
            if js and js[0]["status"] in ("done", "error"):
                return js[0]
            time.sleep(0.05)
        self.fail("analyse trop longue")

    def test_meme_video_pas_relancee(self):
        n = []

        def fake(url, progress=None):
            n.append(url)
            return [pipeline.fiche(nom="Chez Paul", adresse="1 rue A, Lille", lat=50.6, lon=3.0, confiance="haute", source="legende")]
        with mock.patch.object(pipeline, "analyze", side_effect=fake):
            _, r1 = self.call("POST", "/analyze", {"url": "https://youtube.com/shorts/dQw4w9WgXcQ?si=AAA"})
            self.assertFalse(r1["deja"]); self.assertEqual(self.wait_job()["status"], "done")
            _, r2 = self.call("POST", "/analyze", {"url": "https://youtu.be/dQw4w9WgXcQ?si=BBB"})  # autre lien, meme video
            self.assertTrue(r2["deja"]); self.assertEqual((r2["statut"], r2["id"], len(r2["lieux"])), ("done", r1["id"], 1))
            self.call("DELETE", f"/places/{r2['lieux'][0]}")  # la fiche supprimee ne revient pas
            _, r3 = self.call("POST", "/analyze", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
            self.assertTrue(r3["deja"]); self.assertEqual(r3["lieux"], [])
            time.sleep(0.3)
        self.assertEqual(len(n), 1)  # une seule analyse

    def test_echec_puis_meme_video_autre_lien_reessaie(self):
        calls = []

        def fake(url, progress=None):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("Instagram limite les acces anonymes")
            return []
        with mock.patch.object(pipeline, "analyze", side_effect=fake):
            self.call("POST", "/analyze", {"url": "https://www.instagram.com/reel/DaYGFJ_NRcc/?igsh=1"}); self.assertEqual(self.wait_job()["status"], "error")
            _, r = self.call("POST", "/analyze", {"url": "https://instagram.com/reel/DaYGFJ_NRcc"})
            self.assertFalse(r["deja"]); self.assertEqual(self.wait_job()["status"], "done")
        self.assertEqual(len(calls), 2)

    def test_analyse_terminee_non_supprimable(self):
        with mock.patch.object(pipeline, "analyze", side_effect=lambda *a: []):
            self.call("POST", "/analyze", {"url": "https://youtu.be/dQw4w9WgXcQ"}); j = self.wait_job()
            self.call("DELETE", f"/jobs/{j['id']}")
            self.assertEqual(len(self.call("GET", "/jobs")[1]), 1)  # la video reste « connue »

    def test_reanalyser_en_force_garde_les_fiches_testees(self):
        n = []

        def fake(url, progress=None):
            n.append(1)
            return [pipeline.fiche(nom=f"Resto{len(n)}", adresse=f"{len(n)} rue A, Lille", lat=50.6, lon=3.0, confiance="haute", source="legende")]
        with mock.patch.object(pipeline, "analyze", side_effect=fake), mock.patch.object(pipeline, "forget") as oublie:
            url = "https://youtu.be/dQw4w9WgXcQ"
            self.call("POST", "/analyze", {"url": url}); self.wait_job()
            pid = next(iter(self.places())); self.call("PATCH", f"/places/{pid}", {"done": True})
            self.assertTrue(self.call("POST", "/analyze", {"url": url})[1]["deja"]); self.assertEqual(len(n), 1)  # sans force : rien ne bouge
            self.assertFalse(self.call("POST", "/analyze", {"url": url, "force": True})[1]["deja"]); self.assertEqual(self.wait_job()["status"], "done")
        self.assertEqual(len(n), 2); oublie.assert_called_once()
        self.assertEqual(sorted(p["nom"] for p in self.places().values()), ["Resto1", "Resto2"])  # la fiche deja testee reste, la nouvelle s'ajoute

    def test5_modification_manuelle(self):
        auto = lambda: [pipeline.fiche(nom="Resto Faux", adresse="1 rue A, Lille", lat=50.6, lon=3.0, confiance="a_verifier", source="legende"),
                        pipeline.fiche(nom="Autre", adresse="2 rue B, Lille", lat=50.7, lon=3.1, confiance="haute", source="legende")]
        with mock.patch.object(pipeline, "analyze", side_effect=lambda *a: auto()):
            url = "https://exemple.test/video"
            self.call("POST", "/analyze", {"url": url}); self.assertEqual(self.wait_job()["status"], "done")
            ids = {p["nom"]: i for i, p in self.places().items()}
            faux = ids["Resto Faux"]

            # correction : nom + position
            st, _ = self.call("PATCH", f"/places/{faux}", {"nom": "Vrai Resto", "lat": 48.85, "lon": 2.35, "prix": ["Menu 20 €", " "], "site": "https://vrai.example", "cuisine": ""})
            self.assertEqual(st, 200)
            p = self.places()[faux]
            self.assertEqual((p["nom"], p["lat"], p["lon"], p["prix"], p["cuisine"]), ("Vrai Resto", 48.85, 2.35, ["Menu 20 €"], None))
            self.assertTrue(p["modifie_manuellement"]); self.assertEqual(p["confiance"], "manuel")

            # "Reessayer" le lien (job en erreur) : la fiche corrigee reste, l'ancienne version automatique n'est pas re-ajoutee
            with server.db() as c:
                c.execute("update jobs set status='error'")
            self.assertFalse(self.call("POST", "/analyze", {"url": url})[1]["deja"]); self.assertEqual(self.wait_job()["status"], "done")
            ps = self.places()
            self.assertEqual(len(ps), 2, [q["nom"] for q in ps.values()])
            self.assertEqual((ps[faux]["nom"], ps[faux]["lat"]), ("Vrai Resto", 48.85))
            self.assertNotIn(ids["Autre"], ps)  # la fiche non modifiee, elle, est bien remplacee par la nouvelle analyse
            self.assertEqual(sorted(q["nom"] for q in ps.values()), ["Autre", "Vrai Resto"])

        # creation manuelle puis suppression
        st, r = self.call("POST", "/places", {"nom": "Ajout main", "lat": 1.5, "lon": -2.5})
        self.assertEqual(st, 200)
        q = self.places()[r["id"]]
        self.assertEqual((q["source"], q["confiance"], q["modifie_manuellement"]), ("manuel", "manuel", True))
        self.assertEqual(self.call("DELETE", f"/places/{r['id']}")[0], 200); self.assertNotIn(r["id"], self.places())

        # validation serveur
        n = len(self.places())
        bad = ({"lat": 200, "lon": 0}, {"lat": 0, "lon": -181}, {"site": "javascript:alert(1)"}, {"site": "ftp://x.y"}, {"nom": "x" * 201}, {"prix": ["a"] * 31})
        for b in bad + ({"lat": None},):  # lat seule effacee : la position resterait incomplete
            self.assertGreaterEqual(self.call("PATCH", f"/places/{faux}", b)[0], 400, b)
        for b in bad + ({"lat": 10},):
            self.assertGreaterEqual(self.call("POST", "/places", b)[0], 400, b)
        self.assertEqual(len(self.places()), n)  # rien n'a ete cree par les requetes refusees
        self.assertEqual((self.places()[faux]["lat"], self.places()[faux]["nom"]), (48.85, "Vrai Resto"))  # ni modifie
        self.assertEqual(self.call("PATCH", "/places/inconnu", {"nom": "x"})[0], 404)
        self.assertEqual(self.call("PATCH", f"/places/{faux}", {"nom": "", "lat": None, "lon": None})[0], 200)  # valeurs vides autorisees
        self.assertEqual((self.places()[faux]["nom"], self.places()[faux]["lat"]), (None, None))

    def test_cest_bon(self):
        with server.db() as c:
            c.execute("insert into places(id,job_id,url,data,created) values('x','j','u',?,0)", (json.dumps(pipeline.fiche(nom="Flou", confiance="a_verifier")),))
        self.call("PATCH", "/places/x", {"verifie": True})
        p = self.places()["x"]
        self.assertEqual((p["confiance"], p["modifie_manuellement"], p["nom"]), ("manuel", True, "Flou"))

    def test_geocode(self):
        CALLS.clear()
        st, g = self.call("POST", "/geocode", {"adresse": "10 rue Esquermoise, Lille"})
        self.assertEqual(st, 200); self.assertAlmostEqual(g["lat"], 50.6329)
        self.assertEqual(self.call("POST", "/geocode", {"adresse": "Nulle part-sur-mer"})[0], 404)
        self.assertEqual(self.call("POST", "/geocode", {"adresse": "ab"})[0], 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
