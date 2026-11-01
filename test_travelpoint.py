"""Tests Travelpoint (Nominatim simule, aucun reseau) : python test_travelpoint.py
Le serveur FastAPI est lance pour de vrai (uvicorn) sur un port libre, avec une base temporaire."""
import contextlib, hashlib, json, os, socket, sqlite3, tempfile, threading, time, unittest, urllib.error, urllib.parse, urllib.request
import sys, types
from unittest import mock

os.environ["TP_DATA"] = tempfile.mkdtemp(prefix="tp-test-")  # avant d'importer pipeline : base et caches jetables
import uvicorn
import app as server
import pipeline

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
        with mock.patch.object(pipeline, "get_info", return_value=inf):
            return pipeline.analyze("https://exemple.test/" + str(abs(hash(json.dumps(inf)))))

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
        self.assertEqual((f["adresse"], f["confiance"], f["lat"], f["ville"]), ("3 rue de Rivoli, Paris, France", "a_verifier", 48.8566, "Paris"))
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
        self.assertEqual(n, 2)  # « "Farfalou" Marseille adresse » puis le pseudo

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
        self.assertEqual((n1, n2), (2, 0))

    def test_web_desactivee(self):
        with mock.patch.dict(os.environ, {"TP_WEB": "0"}):
            f, n = self.web([])
        self.assertEqual((n, f["confiance"]), (0, "a_verifier"))

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
