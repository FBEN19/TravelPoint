"""Banc des modeles OpenRouter : chaque modele SEUL (sans les autres en secours) sur les memes cas, puis un classement.

    python bench/modeles.py                                   # modeles de modeles_openrouter.txt, cas « rapides »
    python bench/modeles.py --cas tous                        # tous les cas (plus de requetes)
    python bench/modeles.py --modeles google/gemma-4-31b-it:free,nvidia/nemotron-3.5-super-vl:free
    python bench/modeles.py --appliquer                       # ecrit le classement dans modeles_openrouter.txt

Mesure par modele : restaurants trouves, adresses justes, fiches en trop, temps moyen d'une reponse, reponses
vides / illisibles / erreurs, et s'il lit les images. Les resultats OpenStreetMap et web sont mis en cache pendant le
banc : tous les modeles recoivent exactement les memes informations et seul le temps de l'IA compte.
Resultat : bench/modeles.md + une ligne par modele dans bench/modeles.csv. Le quota gratuit est de 1000 requetes/jour.
"""
import argparse, csv, datetime, json, os, pathlib, sys, time

HERE = pathlib.Path(__file__).resolve().parent
os.environ.setdefault("TP_DATA", str(HERE / "data"))  # avant d'importer pipeline : jamais ta base data/
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
pipeline = bench.pipeline

RAPIDES = "compass,yukga,madameguan,holyburek,slak,honore,ishin,royal-buffet"  # 4 legendes variees + 4 videos (voix et images)
FICHIER = pipeline.pathlib.Path(pipeline.__file__).resolve().parent / "modeles_openrouter.txt"


def memoriser():
    """OSM et recherche web identiques pour tous les modeles de ce banc (et instantanes apres le premier)."""
    for nom, garder_vide in (("nominatim", True), ("web_search", False)):
        f = pipeline.DATA / f"memo_{nom}.json"
        f.unlink(missing_ok=True)  # repart de zero a chaque banc (une panne passagere ne reste pas en cache)
        memo = {}
        vraie = getattr(pipeline, nom)

        def enveloppe(*a, _vraie=vraie, _memo=memo, _f=f, _vide=garder_vide, **k):
            cle = json.dumps([a[:2], {x: y for x, y in k.items() if x != "dire"}], sort_keys=True, ensure_ascii=False, default=str)  # sans l'affichage
            if cle not in _memo:
                r = _vraie(*a, **k)
                if r or _vide:
                    _memo[cle] = r
                    _f.parent.mkdir(parents=True, exist_ok=True)
                    _f.write_text(json.dumps(_memo, ensure_ascii=False))
                return r
            return _memo[cle]
        setattr(pipeline, nom, enveloppe)


def passer(modele, cas):
    os.environ["TP_OR_MODELES"] = modele
    pipeline.STATS_MODELES.clear()
    t0, runs = time.monotonic(), []
    for c in cas:
        runs += bench.lancer([c], bavard=False)
        s = runs[-1]["s"]
        print(f"   {c['id']:<20} {s['trouves']}/{s['attendus']} trouves, {s['adresses_ok']}/{s['adresses_attendues']} adresses, {len(s['faux_positifs'])} en trop"
              + (f"  💥 {runs[-1]['erreur'][:60]}" if runs[-1]["erreur"] else ""))
        if pipeline.STATS["quota_jour"]:
            break
    st = pipeline.STATS_MODELES.get(modele) or pipeline.st_modele(modele)
    ok = [x for x in runs if x["cas"].get("verifie")]
    tot = lambda k: sum(x["s"][k] for x in ok)
    return {"modele": modele, "images": pipeline.lit_images(modele), "cas": len(runs),
            "trouves": tot("trouves"), "attendus": tot("attendus"), "adresses_ok": tot("adresses_ok"), "adresses_attendues": tot("adresses_attendues"),
            "en_trop": sum(len(x["s"]["faux_positifs"]) for x in ok), "erreurs_cas": sum(1 for x in runs if x["erreur"]),
            "duree_lien": (time.monotonic() - t0) / max(len(runs), 1), **{k: st.get(k, "") for k in ("appels", "ok", "vides", "illisibles", "erreurs", "quota", "sautes", "derniere_erreur")},
            "temps_reponse": st["secondes"] / st["ok"] if st["ok"] else None}


def note(r):
    """Sur 100 : 50 pour les restaurants trouves, 30 pour les adresses justes, 20 pour les reponses utiles, -5 par fiche en trop.
    (Les regles de Travelpoint trouvent deja une partie des restaurants sans IA : un modele qui ne repond jamais ne doit pas s'en attribuer le merite.)"""
    pct = lambda a, b: a / b if b else 0
    return round(50 * pct(r["trouves"], r["attendus"]) + 30 * pct(r["adresses_ok"], r["adresses_attendues"]) + 20 * pct(r["ok"], r["appels"])
                 - 5 * r["en_trop"], 1)


def classer(res):
    fiab = lambda r: r["ok"] / r["appels"] if r["appels"] else 0
    return sorted(res, key=lambda r: (-note(r), -fiab(r), r["temps_reponse"] if r["temps_reponse"] is not None else 1e9))


def ecrire(res, cas, interrompu):
    pct = lambda a, b: f"{round(100 * a / b)} %" if b else "—"
    oui = {True: "oui", False: "non", None: "?"}
    lignes = [f"# Banc des modeles OpenRouter — {datetime.datetime.now():%Y-%m-%d %H:%M}", "",
              f"{len(cas)} cas ({', '.join(c['id'] for c in cas)}), chaque modele seul, sans les autres en secours. "
              "Note /100 = 50 × restaurants trouves + 30 × adresses justes + 20 × reponses utiles − 5 par fiche en trop.", ""]
    if interrompu:
        lignes += ["⚠️ **Quota du jour epuise : banc interrompu**, les modeles suivants n'ont pas ete testes. Relance demain avec `--modeles` sur ceux qui manquent.", ""]
    lignes += ["| # | Modele | Note | Trouves | Adresses | En trop | Reponses utiles | Vides | Illisibles | Erreurs | Quota | Images | Temps / reponse | Temps / lien |",
               "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(classer(res), 1):
        t = f"{r['temps_reponse']:.1f} s" if r["temps_reponse"] is not None else "—"
        peu = " ⚠️ peu teste" if r["appels"] < 5 else ""
        lignes.append(f"| {i} | {r['modele']}{peu} | **{note(r)}** | {pct(r['trouves'], r['attendus'])} | {pct(r['adresses_ok'], r['adresses_attendues'])} | {r['en_trop']} | "
                      f"{r['ok']}/{r['appels']} | {r['vides']} | {r['illisibles']} | {r['erreurs']} | {r['quota']} | {oui[r['images']]} | {t} | {round(r['duree_lien'])} s |")
    lignes += ["", "Colonnes : *Reponses utiles* = reponses JSON exploitables / appels ; *Vides* = le modele a tout depense a « reflechir » ; "
               "*Illisibles* = pas de liste JSON ; *Quota* = limites 429 ; *Images* = sait lire les images de la video (sinon la lecture des images est sautee).", "",
               "Note de reference : sans aucune reponse de l'IA (regles seules), un modele obtient la note des modeles a 0 reponse utile.", "",
               *[f"- {r['modele']} : {r['derniere_erreur']}" for r in res if r.get("derniere_erreur") and not r["ok"]],
               "", "Ordre conseille pour `modeles_openrouter.txt` (modeles sans aucune reponse utile retires) :", "", "```", *ordre(res), "```"]
    (HERE / "modeles.md").write_text("\n".join(lignes))
    f = HERE / "modeles.csv"
    nouveau = not f.exists() or f.stat().st_size == 0
    with f.open("a", newline="") as h:
        w = csv.writer(h)
        if nouveau:
            w.writerow(["date", "modele", "note", "cas", "trouves", "attendus", "adresses_ok", "adresses_attendues", "en_trop", "appels", "ok", "vides",
                        "illisibles", "erreurs", "quota", "images", "temps_reponse_s", "duree_lien_s"])
        for r in res:
            w.writerow([f"{datetime.datetime.now():%Y-%m-%d %H:%M}", r["modele"], note(r), r["cas"], r["trouves"], r["attendus"], r["adresses_ok"], r["adresses_attendues"],
                        r["en_trop"], r["appels"], r["ok"], r["vides"], r["illisibles"], r["erreurs"], r["quota"], r["images"],
                        round(r["temps_reponse"], 1) if r["temps_reponse"] is not None else "", round(r["duree_lien"])])
    return lignes


def ordre(res):
    garde = [r["modele"] for r in classer(res) if r["ok"] and r["modele"] != "openrouter/free"]
    return garde + ["openrouter/free"]


def appliquer(res):
    entete = [l for l in FICHIER.read_text().splitlines() if l.strip().startswith("#")] if FICHIER.exists() else []
    FICHIER.write_text("\n".join(entete + [f"# Classement du banc du {datetime.date.today()}"] + ordre(res)) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modeles", help="modeles separes par des virgules (defaut : modeles_openrouter.txt, sans le routeur openrouter/free)")
    ap.add_argument("--cas", default=RAPIDES, help=f"ids separes par des virgules, ou « tous » (defaut : {RAPIDES})")
    ap.add_argument("--appliquer", action="store_true", help="ecrire l'ordre obtenu dans modeles_openrouter.txt")
    ap.add_argument("--oui", action="store_true", help="ne pas demander de confirmation")
    args = ap.parse_args()
    if not pipeline.or_cle():
        sys.exit("Pas de cle OpenRouter : mets-la dans data/openrouter.key (ou OPENROUTER_API_KEY).")
    modeles = [m.strip() for m in args.modeles.split(",") if m.strip()] if args.modeles else [m for m in pipeline.or_modeles() if m != "openrouter/free"]
    cas = bench.charger_cas(None if args.cas == "tous" else args.cas)
    estimation = len(modeles) * len(cas) * 3
    print(f"{len(modeles)} modeles × {len(cas)} cas ≈ {estimation} requetes au maximum (quota : 1000 par jour, ~20 par minute).")
    if not args.oui and input("On y va ? [o/N] ").strip().lower() not in ("o", "oui", "y"):
        return
    os.environ.update({"TP_OR_ATTENTE": "1"})
    os.environ.pop("TP_LLM", None)
    memoriser()
    res, interrompu = [], False
    for i, m in enumerate(modeles, 1):
        print(f"\n[{i}/{len(modeles)}] {m}  (lit les images : {({True: 'oui', False: 'non', None: '?'})[pipeline.lit_images(m)]})")
        r = passer(m, cas)
        res.append(r)
        print(f"   ⇒ note {note(r)} — {r['ok']}/{r['appels']} reponses utiles"
              + (f", {r['temps_reponse']:.1f} s par reponse" if r["temps_reponse"] is not None else ""))
        if pipeline.STATS["quota_jour"]:
            interrompu = True
            print("\n⚠️  Quota du jour epuise : arret.")
            break
        ecrire(res, cas, False)  # rapport mis a jour au fur et a mesure
    lignes = ecrire(res, cas, interrompu)
    if args.appliquer and res:
        appliquer(res)
        print(f"\nOrdre ecrit dans {FICHIER.name}")
    print("\n" + "\n".join(lignes[4:6 + len(res) + (2 if interrompu else 0)]) + f"\n\nRapport complet : {HERE / 'modeles.md'}")


if __name__ == "__main__":
    main()
