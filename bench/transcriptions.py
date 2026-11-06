"""Compare deux transcriptions des memes videos : Whisper en local et un modele OpenRouter qui ecoute l'audio.

    python bench/transcriptions.py                                   # cas du banc qui ont une video
    python bench/transcriptions.py --cas honore,ishin
    python bench/transcriptions.py --modele nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free   # un autre modele

Resultat : bench/transcriptions.md (les deux textes cote a cote, le temps, et les noms attendus retrouves ou non).
Une requete OpenRouter par video. Les transcriptions sont gardees : relancer ne coute rien.
"""
import argparse, hashlib, os, pathlib, sys, time

HERE = pathlib.Path(__file__).resolve().parent
os.environ.setdefault("TP_DATA", str(HERE / "data"))
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
pipeline = bench.pipeline


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cas", help="ids separes par des virgules (defaut : les cas dont la video est deja telechargee)")
    ap.add_argument("--modele", default="openai/whisper-large-v3-turbo")
    args = ap.parse_args()
    if not pipeline.or_cle():
        sys.exit("Pas de cle OpenRouter (data/openrouter.key).")
    sources, lignes = bench.sources_app(), []
    omni = "openrouter:" + args.modele
    lignes += [f"# Transcriptions : Whisper local ({pipeline.WHISPER}) contre {args.modele}", ""]
    total = {"whisper": 0, "omni": 0, "noms": 0}
    for c in bench.charger_cas(args.cas):
        key, url = pipeline.canon(c["url"])
        wd = pipeline.DATA / "jobs" / hashlib.sha1(key.encode()).hexdigest()[:10]
        bench.amorcer(key, wd, sources)
        if not (wd / "video.mp4").exists():
            continue  # cas sans video telechargee (legende suffisante) : rien a comparer
        print(f"\n{c['id']}")
        textes = {}
        for nom, modele in (("whisper", pipeline.WHISPER), ("omni", omni)):
            pipeline.TRANSCRIPTION = modele if nom == "omni" else ""
            t0 = time.monotonic()
            info = __import__("json").loads((wd / "video.info.json").read_text()) if (wd / "video.info.json").exists() else {}
            texte = pipeline.transcription(wd, lambda s: print("   ·", s), pipeline.langue_probable(f"{info.get('title') or ''}\n{info.get('description') or ''}"))
            utilise = (wd / "a.modele").read_text().strip() if (wd / "a.modele").exists() else ""
            textes[nom] = (texte if utilise == modele else f"(echec : {pipeline.STATS_MODELES.get(args.modele, {}).get('derniere_erreur') or 'Whisper a pris le relais'})",
                           time.monotonic() - t0)
        noms = [a["nom"] for a in c["attendus"] if a.get("nom")]
        total["noms"] += len(noms)
        lignes += [f"## {c['id']}", c["url"], ""]
        for nom in ("whisper", "omni"):
            texte, duree = textes[nom]
            trouves = [n for n in noms if pipeline.ecrit(n, texte)]
            total[nom] += len(trouves)
            lignes += [f"**{'Whisper' if nom == 'whisper' else args.modele}** ({duree:.0f} s, noms attendus entendus : {len(trouves)}/{len(noms)})", "",
                       "> " + texte[:1200].replace("\n", " "), ""]
            print(f"   {nom:8} {len(trouves)}/{len(noms)} noms  {texte[:100]!r}")
    lignes[2:2] = [f"Noms attendus retrouves dans la transcription : Whisper {total['whisper']}/{total['noms']}, "
                   f"{args.modele} {total['omni']}/{total['noms']}.", ""]
    (HERE / "transcriptions.md").write_text("\n".join(lignes))
    print(f"\n{lignes[2]}\nRapport : {HERE / 'transcriptions.md'}")


if __name__ == "__main__":
    main()
