"""Banc d'essai Travelpoint : analyse de vrais liens et comparaison avec les restaurants attendus (bench/cas.json).

    python bench/bench.py                  # tous les cas
    python bench/bench.py --cas compass,yukga
    python bench/bench.py --frais          # sans reutiliser les legendes/videos deja telechargees

Ne touche jamais a data/ (ta base) : le banc travaille dans bench/data/. Resultat : bench/rapport.md + une ligne dans bench/historique.csv.
"""
import argparse, csv, datetime, hashlib, json, math, os, pathlib, re, shutil, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
if __name__ == "__main__":
    os.environ.setdefault("TP_DATA", str(HERE / "data"))  # avant d'importer pipeline
sys.path.insert(0, str(ROOT))
import pipeline  # noqa: E402

SEUIL_M = 300  # une adresse est « juste » si le marqueur est a moins de 300 m de l'adresse attendue


def dist(a, r):
    """Distance en metres entre [lat, lon] attendu et la fiche r."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], float(r["lat"]), float(r["lon"])))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def a_position(r):
    return r is not None and r.get("lat") not in (None, "") and r.get("lon") not in (None, "")


def score(cas, res, coords):
    """Compare les fiches obtenues aux restaurants attendus. Chaque fiche ne sert qu'une fois."""
    used, lignes = set(), []
    for a in cas["attendus"]:
        exp = coords.get(a.get("adresse")) if a.get("adresse") else None
        pos_ok = lambda r: bool(exp) and a_position(r) and dist(exp, r) <= SEUIL_M
        ville_ok = lambda r: not a.get("ville") or pipeline.nz(a["ville"]) in pipeline.nz(f"{r.get('ville')} {r.get('adresse')}")
        nom_ok = lambda r: max(pipeline.sim(a["nom"], r.get("nom") or ""), pipeline.sim(a["nom"], r.get("nom_local") or "")) >= 0.75
        cands = [i for i, r in enumerate(res) if i not in used and (nom_ok(r) if a.get("nom") else pos_ok(r))]
        cands.sort(key=lambda i: (not pos_ok(res[i]), not ville_ok(res[i])))  # Red Noodle Paris / Lille : la bonne ville d'abord
        r = res[cands[0]] if cands else None
        if cands:
            used.add(cands[0])
        lignes.append({"attendu": a, "fiche": r, "ville_ok": bool(r) and ville_ok(r),
                       "adresse_ok": (pos_ok(r) if r else False) if exp else None,  # None : pas d'adresse attendue (ou introuvable)
                       "distance": round(dist(exp, r)) if exp and a_position(r) else None})
    return {"lignes": lignes, "trouves": sum(1 for l in lignes if l["fiche"]), "attendus": len(lignes),
            "adresses_ok": sum(1 for l in lignes if l["adresse_ok"]), "adresses_attendues": sum(1 for l in lignes if l["adresse_ok"] is not None),
            "villes_ok": sum(1 for l in lignes if l["ville_ok"]),
            "faux_positifs": [] if cas.get("partiel") else [r.get("nom") for i, r in enumerate(res) if i not in used],
            "noms_suspects": [r["nom"] for r in res if r.get("nom") and (re.match(r"\s*\d", r["nom"]) or pipeline.STREET.search(r["nom"]))]}


def coords_attendues(cas_list):
    """Coordonnees des adresses attendues (Nominatim, 1 requete/s), gardees dans bench/coords.json."""
    f = HERE / "coords.json"
    c = json.loads(f.read_text()) if f.exists() else {}
    for a in (a for k in cas_list for a in k["attendus"] if a.get("adresse")):
        if a["adresse"] not in c:
            g = pipeline.geocode(a["adresse"])
            c[a["adresse"]] = [g["lat"], g["lon"]] if g else None
    f.write_text(json.dumps(c, ensure_ascii=False, indent=1))
    return c


def amorcer(key, wd, sources):
    """Reprend la legende, la transcription et la video deja telechargees par l'app (data/jobs), jamais le resultat."""
    src = sources.get(key.split(":", 1)[-1])
    if not src or (wd / "video.info.json").exists():
        return
    wd.mkdir(parents=True, exist_ok=True)
    for n in ["video.info.json", "a.txt", "a.modele", "video.mp4"] + [f.name for f in src.glob("a.*.txt")]:
        if (src / n).exists():
            shutil.copy2(src / n, wd / n)


def sources_app():
    out = {}
    for f in (ROOT / "data" / "jobs").glob("*/video.info.json"):
        try:
            out.setdefault(str(json.loads(f.read_text()).get("id")), f.parent)
        except Exception:  # noqa
            pass
    return out


def commit():
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip() or "?"
    except Exception:  # noqa
        return "?"


def ia():
    if os.environ.get("TP_LLM") != "0" and pipeline.or_cle():
        return f"OpenRouter ({', '.join(pipeline.or_modeles())})"
    return "aucune (regles seules)"


def fmt_fiche(r):
    if not r:
        return "—"
    pos = "📍" if a_position(r) else "sans position"
    return f"{r.get('nom')} ({r.get('confiance')}, {pos})"


def tableau_modeles():
    """Ce que chaque modele OpenRouter a fait pendant ce passage (vide en local)."""
    st = {m: v for m, v in pipeline.STATS_MODELES.items() if v["appels"] or v["sautes"]}
    if not st:
        return []
    out = ["Modeles OpenRouter (dans l'ordre d'essai) :", "", "| Modele | Appels | Reponses utiles | Vides | Illisibles | Erreurs | Quota | Sautes (images) | Temps moyen |",
           "|---|---|---|---|---|---|---|---|---|"]
    for m, v in st.items():
        moy = f"{v['secondes'] / v['ok']:.1f} s" if v["ok"] else "—"
        out.append(f"| {m} | {v['appels']} | {v['ok']} | {v['vides']} | {v['illisibles']} | {v['erreurs']} | {v['quota']} | {v['sautes']} | {moy} |")
    return out + [""]


def rapport(runs, total_s):
    ok = [x for x in runs if x["cas"].get("verifie")]
    tot = lambda k: sum(x["s"][k] for x in ok)
    fp = sum(len(x["s"]["faux_positifs"]) for x in ok)
    sus = sum(len(x["s"]["noms_suspects"]) for x in runs)
    pct = lambda a, b: f"{a}/{b} ({round(100 * a / b)} %)" if b else "—"
    st = pipeline.STATS
    lines = [f"# Banc d'essai Travelpoint — {datetime.datetime.now():%Y-%m-%d %H:%M} (commit {commit()})", "",
             f"IA : **{ia()}** — lectures : {st['openrouter']} reussies, {st['echecs']} echouees (tous les modeles indisponibles)", "",
             f"Score sur les **{len(ok)} cas verifies** :", "",
             f"- Restaurants trouves : **{pct(tot('trouves'), tot('attendus'))}**",
             f"- Adresses justes (< {SEUIL_M} m) : **{pct(tot('adresses_ok'), tot('adresses_attendues'))}**",
             f"- Bonne ville : {pct(tot('villes_ok'), tot('attendus'))}",
             f"- Faux positifs (fiches en trop) : {fp}",
             f"- Noms qui ressemblent a une adresse : {sus}",
             f"- Erreurs : {sum(1 for x in runs if x['erreur'])} — Duree totale : {round(total_s)} s (moyenne {round(total_s / max(len(runs), 1))} s par lien)", "",
             *tableau_modeles(),
             "| Cas | Trouves | Adresses | En trop | Duree | Remarque |", "|---|---|---|---|---|---|"]
    for x in runs:
        s = x["s"]
        rem = x["erreur"] or ("à confirmer" if not x["cas"].get("verifie") else "") + (" · nom = adresse" if s["noms_suspects"] else "")
        lines.append(f"| {x['cas']['id']} | {s['trouves']}/{s['attendus']} | {s['adresses_ok']}/{s['adresses_attendues']} | "
                     f"{len(s['faux_positifs'])} | {round(x['duree'])} s | {rem.strip(' ·')} |")
    lines += ["", "## Detail", ""]
    for x in runs:
        lines += [f"### {x['cas']['id']} — {x['cas'].get('note', '')}", x["cas"]["url"], ""]
        for l in x["s"]["lignes"]:
            a, r = l["attendu"], l["fiche"]
            etat = "✅" if r and (l["adresse_ok"] in (True, None)) and l["ville_ok"] else ("⚠️" if r else "❌")
            d = f", a {l['distance']} m" if l["distance"] is not None else ""
            lines.append(f"- {etat} attendu **{a.get('nom') or '(nom inconnu)'}** ({a.get('adresse') or a.get('ville')}) → {fmt_fiche(r)}{d}")
        for n in x["s"]["faux_positifs"]:
            lines.append(f"- ➕ en trop : {n}")
        if x["erreur"]:
            lines.append(f"- 💥 erreur : {x['erreur']}")
        lines.append("")
    (HERE / "rapport.md").write_text("\n".join(lines))
    h = HERE / "historique.csv"
    new = not h.exists()
    with h.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "commit", "cas_verifies", "trouves", "attendus", "adresses_ok", "adresses_attendues", "faux_positifs", "erreurs", "duree_moyenne_s"])
        w.writerow([f"{datetime.datetime.now():%Y-%m-%d %H:%M}", commit() + ("+openrouter" if ia().startswith("OpenRouter") else ""), len(ok), tot("trouves"), tot("attendus"), tot("adresses_ok"),
                    tot("adresses_attendues"), fp, sum(1 for x in runs if x["erreur"]), round(total_s / max(len(runs), 1))])
    return lines


def charger_cas(ids=None):
    cas = json.loads((HERE / "cas.json").read_text())["cas"]
    return [c for c in cas if c["id"] in ids.split(",")] if ids else cas


def lancer(cas, frais=False, bavard=True):
    """Analyse chaque cas avec le code actuel (jamais le resultat en cache) -> liste de {cas, s, duree, erreur}."""
    dire = print if bavard else (lambda *a, **k: None)
    coords = coords_attendues(cas)
    pipeline.WEB_CACHE.unlink(missing_ok=True)  # chaque passage reteste la recherche web
    sources, runs, last_ig = ({} if frais else sources_app()), [], 0.0
    for i, c in enumerate(cas, 1):
        key, url = pipeline.canon(c["url"])
        wd = pipeline.DATA / "jobs" / hashlib.sha1(key.encode()).hexdigest()[:10]
        if frais and wd.exists():
            shutil.rmtree(wd)
        amorcer(key, wd, sources)
        (wd / "result.json").unlink(missing_ok=True)  # toujours une vraie analyse avec le code actuel
        if "instagram.com" in url and not (wd / "video.info.json").exists() and time.time() - last_ig < 15:
            time.sleep(15 - (time.time() - last_ig))  # Instagram limite les acces anonymes rapproches
        dire(f"\n[{i}/{len(cas)}] {c['id']}  {url}")
        t0, err, res = time.monotonic(), None, []
        try:
            res = pipeline.analyze(url, lambda s: dire("   ·", s))
        except Exception as e:  # noqa
            err = str(e).splitlines()[0][:200]
            dire("   💥", err)
        if "instagram.com" in url:
            last_ig = time.time()
        s = score(c, res, coords)
        runs.append({"cas": c, "s": s, "duree": time.monotonic() - t0, "erreur": err})
        dire(f"   → {s['trouves']}/{s['attendus']} trouves, {s['adresses_ok']}/{s['adresses_attendues']} adresses justes, {len(s['faux_positifs'])} en trop")
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cas", help="ids separes par des virgules (defaut : tous)")
    ap.add_argument("--frais", action="store_true", help="retelecharger legendes et videos au lieu de reprendre celles de l'app")
    args = ap.parse_args()
    cas = charger_cas(args.cas)
    print(f"{len(cas)} cas — donnees du banc : {pipeline.DATA}")
    t_all = time.monotonic()
    runs = lancer(cas, args.frais)
    lines = rapport(runs, time.monotonic() - t_all)
    print("\n" + "\n".join(lines[:10]) + f"\n\nRapport complet : {HERE / 'rapport.md'}")


if __name__ == "__main__":
    main()
