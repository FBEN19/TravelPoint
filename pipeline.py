"""Travelpoint - pipeline d'analyse (restaurants) : lien -> fiches restaurant.
Texte d'abord (legende + OpenStreetMap), video (Whisper + Qwen3-VL via MLX) seulement si besoin.
Tout tourne en local ; un dossier de travail par lien (cache) dans data/jobs/<hash>/.
"""
import difflib, hashlib, json, os, pathlib, re, subprocess, sys, threading, time, unicodedata, urllib.parse, urllib.request

BIN = pathlib.Path(sys.executable).parent  # binaires du venv (yt-dlp, mlx_whisper)
os.environ["PATH"] += os.pathsep + "/opt/homebrew/bin" + os.pathsep + "/usr/local/bin"  # ffmpeg (brew)
DATA = pathlib.Path(os.environ.get("TP_DATA", "data")); DATA.mkdir(exist_ok=True)
VLM = os.environ.get("TP_VLM", "mlx-community/Qwen3-VL-4B-Instruct-4bit")
WHISPER = os.environ.get("TP_WHISPER", "mlx-community/whisper-small-mlx")
UA = {"User-Agent": "Travelpoint/0.1 (self-hosted)", "Accept-Language": "fr,en"}
DEBUG = bool(os.environ.get("TP_DEBUG"))
GENERIC = {"fyp", "foryou", "pourtoi", "viral", "reels", "reel", "voyage", "travel", "food", "foodie", "paris", "france",
           "instagram", "tiktok", "explore", "resto", "restaurant", "bonplan", "bonsplans"}
FOOD = {"restaurant", "fast_food", "cafe", "bar", "pub", "food_court", "biergarten", "ice_cream"}
FOOD_SHOP = {"bakery", "pastry", "confectionery", "deli", "chocolate"}


def run(cmd, **k):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **k)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-600:] or f"{cmd[0]} a echoue")
    return r


# ---------- identifiant d'une video (pour ne jamais re-analyser deux fois la meme) ----------
TRACKING = re.compile(r"^(utm_.*|si|igsh|igshid|fbclid|gclid|feature|ref|rpxt|srtk|dlrf|is_from_webapp|sender_device|share_app_id|_r|_t)$", re.I)


def canon(url):
    """Lien -> (cle, lien propre). La cle identifie la VIDEO : ?si=, utm_, igsh..., www/m., /shorts/ ou /watch ne la changent pas."""
    u = urllib.parse.urlparse(url.strip())
    host = re.sub(r"^(www|m|mobile|music)\.", "", u.netloc.lower().split(":")[0])
    path, q = u.path, urllib.parse.parse_qs(u.query)
    if host in ("youtube.com", "youtu.be", "youtube-nocookie.com"):
        vid = path.strip("/").split("/")[0] if host == "youtu.be" else (q.get("v") or [""])[0] or (re.match(r"/(?:shorts|embed|live|v)/([\w-]+)", path) or [None, ""])[1]
        if re.fullmatch(r"[\w-]{11}", vid or ""):
            return f"yt:{vid}", f"https://www.youtube.com/watch?v={vid}"
    if host == "instagram.com":
        m = re.search(r"/(reels?|p|tv)/([\w-]+)", path)
        if m:
            kind = "reel" if m.group(1).startswith("reel") else m.group(1)
            return f"ig:{m.group(2)}", f"https://www.instagram.com/{kind}/{m.group(2)}/"
    if host.endswith("tiktok.com"):
        m = re.search(r"/(?:video|photo)/(\d+)", path)
        if m:
            return f"tt:{m.group(1)}", f"https://www.tiktok.com{path.rstrip('/')}"
    keep = sorted((k, v[0]) for k, v in q.items() if not TRACKING.match(k))  # autre site : on retire seulement les parametres de suivi
    propre = f"https://{host}{path.rstrip('/')}" + ("?" + urllib.parse.urlencode(keep) if keep else "")
    return "url:" + propre[8:].lower(), propre


# ---------- telechargement (yt-dlp) ----------
def needs_login(url):
    return any(d in url for d in ("instagram.com", "tiktok.com", "facebook.com"))


def ytdlp(url, *args):
    """Sans cookies d'abord (YouTube, Instagram anonyme) ; data/cookies.txt si present ; sinon navigateur (TP_COOKIES_BROWSER)."""
    ck = DATA / "cookies.txt"
    tries = [[]]
    if needs_login(url):
        if ck.exists():
            tries = [["--cookies", str(ck)], []]
        elif os.environ.get("TP_COOKIES_BROWSER"):
            tries.append(["--cookies-from-browser", os.environ["TP_COOKIES_BROWSER"]])
    err = None
    for extra in tries:
        try:
            return run([BIN / "yt-dlp", *extra, *args, url])
        except RuntimeError as e:
            err = e
    msg = str(err)
    if "rate-limit" in msg or "login" in msg.lower():
        raise RuntimeError("Instagram limite les acces anonymes : reessaie plus tard, ou ajoute data/cookies.txt.")
    raise err


# ---------- texte : legende ----------
def get_info(url, wd):
    f = wd / "video.info.json"
    if f.exists():
        return json.loads(f.read_text())
    ytdlp(url, "--skip-download", "--write-info-json", "-o", str(wd / "video.%(ext)s"))
    return json.loads(f.read_text())


def clean(t):  # [@x](url) -> @x
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)


def accounts(info):
    """Compte qui publie (uploader, channel, uploader_id), normalise : ce n'est jamais un restaurant."""
    return {n for k in ("uploader", "channel", "uploader_id") if (n := nz(str(info.get(k) or "")))}


ACCOUNT_TITLE = re.compile(r"^\s*(?:video|vid[eé]o|reel|post|photo|image|story)s?\s+(?:by|de|par|from)\s+\S+\s*$", re.I)  # "Video by sorties.paris"
CREDIT = re.compile(r"concours|gagnant|bravo|collab|partenariat|merci|\bvia\b|photo|cr[eé]dit|repost", re.I)


def scrub(text, skip):  # retire les lignes qui citent @compte et les #compte (normalise : sortiesparis == sorties.paris)
    mine = lambda m: nz(m.group(0)) in skip
    text = "\n".join(l for l in text.splitlines() if not any(mine(m) for m in re.finditer(r"@[\w.]+", l)))
    return re.sub(r"#[\w.]+", lambda m: "" if mine(m) else m.group(0), text)


def candidates(text, skip=()):
    c = re.findall(r"\b((?:[A-ZÀ-Ý][a-zà-ÿ'’\-]+[^\S\r\n])+[A-ZÀ-Ý][a-zà-ÿ'’\-]+)\b", text)  # un nom ne passe pas a la ligne
    c += [re.sub(r"(?<=[a-zà-ÿ])(?=[A-Z])", " ", h) for h in re.findall(r"#(\w+)", text) if h.lower() not in GENERIC]
    return list(dict.fromkeys(x.strip() for x in c if x.lower() not in GENERIC and nz(x) not in skip))


def handle_of(text, skip):
    """@handle du restaurant : jamais le compte qui publie, ni une ligne de concours / remerciement / credit."""
    first = None
    for line in text.splitlines():
        if CREDIT.search(line):
            continue
        for arrow, h in re.findall(r"(👉\s*)?(?<![\w.])@([\w.]+)", line):
            h = h.strip(".")
            if nz(h) and nz(h) not in skip:
                if arrow:
                    return h
                first = first or h
    return first


def name_from_handle(handle, cands):  # rednoodle.officiel + "Red Noodle Paris" -> "Red Noodle"
    n = re.sub(r"\W", "", handle.split(".")[0]).lower()
    for c in cands:
        acc, words = "", []
        for w in c.split():
            acc += re.sub(r"\W", "", w.lower()); words.append(w)
            if acc == n:
                return " ".join(words)
    return None


MONEY = re.compile(r"[€$£¥₩₹฿]|円|원|\b(?:EUR|USD|JPY|GBP|KRW|THB|CHF|CAD|AUD)\b|\d\s?(?:euros?|yens?|dollars?|won)\b", re.I)
HOURS = re.compile(r"(?:[\U0001F550-\U0001F567\u23F0]|営業時間\s*[:：]?|(?:Hours|Opening hours|Open|Horaires|Ouvert)\s*[:：])\s*([^\n]+)", re.I)
TYPES = r"rue|avenue|av|boulevard|bd|blvd|place|quai|chemin|all[ée]e|cours|impasse|route|passage|square|faubourg|via|viale|calle|carrer|rua|street|road"
STREET = re.compile(rf"\b\d{{1,4}}(?:\s?(?:bis|ter))?,?\s+(?:{TYPES})\.?\s[^\n#*|]+"                        # "38 Rue Sylvabelle"
                    rf"|\b(?:{TYPES})\.?\s[^\n#*|,\d]{{2,60}}\s\d{{1,4}}\b[^\n#*|]*", re.I)               # "Rue Longue Vie 31, 1050 Ixelles"
QUOTED = re.compile(r"[«“\"]\s*([^»”\"\n]{2,60}?)\s*[»”\"]")                                                    # « Bertoni »
ADDR = re.compile(r"(?:📍|📌|🗺️?|住所\s*[:：]?|(?:Adresse|Address|Addr)\s*[:：])\s*([^\n]+)", re.I)
PIN_HANDLE = re.compile(r"^\s*(?:📍|📌)\s*@([\w.]+)", re.M)  # "📍@restaurant :" = un restaurant tague, pas une adresse
FOOTER = re.compile(r"^\s*(?:👉|📱|#)")                      # fin d'un bloc : « adresses en commentaire », pub de l'appli, hashtags


def pretty(handle):  # marseille_en_cornet -> Marseille En Cornet
    return " ".join(w.capitalize() for w in re.split(r"[_\s]+", handle.split(".")[0]) if w)


def pin_blocks(text, cands, skip=()):
    """Legendes du type « 📍@resto1 : offres... 📍@resto2 : offres... » -> un bloc par restaurant (nom = pseudo, prix/offres = ses lignes)."""
    lines, out = text.splitlines(), []
    for i, l in enumerate(lines):
        m = PIN_HANDLE.match(l)
        if not m or nz(m.group(1).strip(".")) in skip:
            continue
        h, body = m.group(1).strip("."), []
        for l2 in lines[i + 1:]:
            if PIN_HANDLE.match(l2) or FOOTER.match(l2):
                break
            t = re.sub(r"^[^\w€$£¥«“\"(]+", "", l2).strip()  # retire puces et emojis en debut de ligne
            if t and not CREDIT.search(t):
                body.append(t[:200])
        out.append({"nom": name_from_handle(h, cands) or pretty(h), "handle": h, "prix": body[:8],
                    "adresse": next(iter(STREET.findall("\n".join(body))), None)})
    return out


def parse_caption(text, skip=()):
    text = scrub(clean(text), skip)
    cands = candidates(text, skip)
    handle = handle_of(text, skip)
    pins = [a.strip() for a in ADDR.findall(text) if not a.strip().startswith("@")] or [a.strip(" ,;.-–") for a in STREET.findall(text)]  # sans 📍 : adresse ecrite telle quelle ; « 📍@pseudo » n'est pas une adresse
    quoted = QUOTED.search(text)
    name, src = None, None
    if handle:
        name, src = name_from_handle(handle, cands) or handle.split(".")[0], "handle"
    elif pins and not re.search(r"\d", pins[0]):  # "📍Narisawa, Tokyo" : le nom est avant la virgule
        name, src = re.split(r"\s*[,/|·–—-]\s*", pins[0])[0], "pin"
    elif quoted:
        name, src = quoted.group(1), "guillemets"
    elif cands:
        name, src = cands[0], "candidat"
    return {"nom": name, "nom_src": src, "adresses": pins,
            "prix": [l.strip("-• ").strip() for l in text.splitlines() if MONEY.search(l)],
            "horaires": [h.strip() for h in HOURS.findall(text)], "candidats": cands, "texte": text, "lieux": pin_blocks(text, cands, skip),
            "villes": [h for h in re.findall(r"#(\w+)", text) if h.lower() not in GENERIC - {"paris", "france"}][:3]}  # #marseille : ville possible


# ---------- OpenStreetMap (Nominatim : 1 requete/s max) ----------
def nz(t):  # comparaison de noms : sans accents, sans casse ni ponctuation (garde les caracteres japonais, coreens...)
    t = unicodedata.normalize("NFKD", t or "").casefold()
    return re.sub(r"[\W_]+", "", "".join(c for c in t if not unicodedata.combining(c)))


def sim(a, b):
    a, b = nz(a), nz(b)
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


NOMINATIM_DELAY, _nlock, _nlast = 1.1, threading.Lock(), [0.0]  # 1 requete/s max, worker et routes web confondus


def nominatim(params):
    q = urllib.parse.urlencode({**params, "format": "json"})
    with _nlock:
        time.sleep(max(0.0, _nlast[0] + NOMINATIM_DELAY - time.monotonic()))
        try:
            r = json.loads(urllib.request.urlopen(urllib.request.Request("https://nominatim.openstreetmap.org/search?" + q, headers=UA), timeout=20).read())
        except Exception as e:
            r = []
            if DEBUG: print("  [osm] erreur", e)
        _nlast[0] = time.monotonic()
    if DEBUG: print(f"  [osm] {q[:140]} -> {[x.get('display_name', '')[:50] for x in r][:3]}")
    return r


def place_city(a):
    return next((a[k] for k in ("city", "town", "village", "municipality", "suburb", "province", "state") if a.get(k)), None)


def norm_addr(a):
    a = a.replace("’", "'")
    for pat, rep in ((r"\bAv\.?(?=\s)", "avenue"), (r"\b(?:Bd|Bld|Blvd)\.?(?=\s)", "boulevard"), (r"\b(?:Prte|Pte)\.?(?=\s)", "porte"),
                     (r"\bRte\.?(?=\s)", "route"), (r"\bPl\.?(?=\s)", "place"), (r"\bFbg\.?(?=\s)", "faubourg"), (r"\bQu\.?(?=\s)", "quai")):
        a = re.sub(pat, rep, a, flags=re.I)
    return a


def geocode(addr):
    """Adresse (n'importe quel pays) -> {lat, lon, ville, pays, precis}. Reessaie en simplifiant l'adresse."""
    n = norm_addr(addr).replace("〒", " ").strip()
    parts = [p.strip() for p in n.split(",") if p.strip()]
    tries = [n, re.sub(r"^\d+[\w-]*\s*,?\s*", "", n)]                                  # sans numero de rue
    tries += [", ".join(parts[-3:]), ", ".join(parts[-2:])] if len(parts) > 2 else []        # quartier + ville + pays
    tries.append(re.sub(r"[\d０-９\-－ー−丁目番地号\s]+$", "", n))                               # adresses japonaises : sans le numero final
    for q in dict.fromkeys(t for t in tries if len(t) > 2):
        r = nominatim({"q": q, "limit": 1, "addressdetails": 1})
        if r:
            a = r[0].get("address", {})
            return {"lat": float(r[0]["lat"]), "lon": float(r[0]["lon"]), "ville": place_city(a), "pays": a.get("country"), "precis": q == n}
    return None


def osm(name, near=None, hint="", pays=""):
    """Etablissement de restauration nomme `name`, cherche DANS UNE ZONE : near=(lat, lon, demi-cote en degres) ou ville `hint`."""
    p = {"q": name, "addressdetails": 1, "extratags": 1, "namedetails": 1, "limit": 10}
    if not near and hint:
        g = geocode(f"{hint}, {pays}" if pays else hint)
        near = (g["lat"], g["lon"], 0.15) if g else None  # ~15 km autour de la ville
    if near:
        la, lo, d = near
        p.update(viewbox=f"{lo - d},{la + d},{lo + d},{la - d}", bounded=1)
    is_food = lambda r: r.get("type") in FOOD or (r.get("class") == "shop" and r.get("type") in FOOD_SHOP)
    best = None
    for r in nominatim(p):
        if not is_food(r):
            continue  # pas un lieu de restauration
        names = [r.get("name", "")] + list((r.get("namedetails") or {}).values())  # nom local + name:en, name:ja, alt_name...
        score = max(sim(name, v) for v in names)
        if not best or score > best[0]:
            best = (score, r)
    prob = False
    if not best or best[0] < (0.75 if near else 0.85):
        best = None
        if near and near[2] <= 0.002:  # adresse precise : s'il n'y a qu'un restaurant sur place, c'est probablement lui (nom ecrit autrement)
            foods = [r for r in nominatim({**p, "q": "restaurant"}) if is_food(r)]
            if len(foods) == 1:
                best, prob = (0.5, foods[0]), True
        if not best:
            return None
    r = best[1]; a, x, nd = r.get("address", {}), r.get("extratags", {}), r.get("namedetails") or {}
    nom = nd.get("name:fr") or nd.get("name:en") or nd.get("int_name") or r.get("name")
    return {"nom": nom, "nom_local": r.get("name") if r.get("name") != nom else None, "ville": place_city(a), "pays": a.get("country"),
            "adresse": r.get("display_name"), "lat": r["lat"], "lon": r["lon"], "cuisine": x.get("cuisine"), "horaires": x.get("opening_hours"),
            "site": x.get("website") or x.get("contact:website"), "tel": x.get("phone") or x.get("contact:phone"),
            "confiance": "moyenne" if prob else "haute"}


def locate(addr, villes=()):
    """Adresse -> (adresse completee, geocodage). Sans ville ("38 Rue Sylvabelle"), essaie avec les villes connues (#marseille, ville citee)."""
    if "," not in addr:
        for v in filter(None, villes):
            if v.lower() not in addr.lower() and (g := geocode(f"{addr}, {v}")):
                return f"{addr}, {v[:1].upper() + v[1:]}", g
    return addr, geocode(addr)


def osm_at(nom, g):
    """Restaurant `nom` autour d'une adresse geocodee : serre d'abord (~100 m), puis ~2 km."""
    for d in ([0.001] if g["precis"] else []) + [0.02]:
        if hit := osm(nom, near=(g["lat"], g["lon"], d)):
            return hit
    return None


def at_address(nom, addr, g, **k):
    """OSM ne connait pas le restaurant : fiche "a verifier" placee sur l'adresse."""
    g, ville, pays = g or {}, k.pop("ville", None), k.pop("pays", None)
    return fiche(nom=nom, adresse=addr, ville=g.get("ville") or ville, pays=g.get("pays") or pays, lat=g.get("lat"), lon=g.get("lon"), confiance="a_verifier", **k)


def expand(nom, hint, pays, speech):
    """Nom prononce coupe par le modele ('bouillon') : on essaie avec les 1-2 mots precedents ('comme un bouillon')."""
    words = re.findall(r"[\wÀ-ÿ'’]+", speech)
    low, nw = [w.lower() for w in words], nom.lower().split()
    for i in range(len(low) - len(nw) + 1):
        if low[i:i + len(nw)] == nw:
            for back in (2, 1):
                if i - back >= 0:
                    r = osm(" ".join(words[i - back:i + len(nw)]), hint=hint, pays=pays)
                    if r:
                        return r
            break
    return None


# ---------- video : Whisper + Qwen3-VL ----------
def ask(prompt, *extra, tokens=300):
    r = run([sys.executable, "-m", "mlx_vlm.generate", "--model", VLM, *extra, "--max-tokens", str(tokens), "--temperature", "0", "--prompt", prompt])
    if DEBUG:
        print("  [llm]", r.stdout[-900:])
    m = re.search(r"\[\s*\{.*\}\s*\]", r.stdout, re.S)
    try:
        return json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return []


LLM_PROMPT = ("Voici la legende d'une video de reseau social (format libre : emojis, @pseudos, hashtags, publicites...). "
              "Releve les restaurants, cafes, bars ou boulangeries dont elle parle. Pour chacun : "
              '"nom" (tel qu\'ecrit ; un @pseudo place a cote d\'un lieu est en general le nom de l\'etablissement), '
              '"adresse" (complete, si elle est ecrite), "ville", "pays", "offres" (les lignes de prix ou d\'offres qui le concernent, '
              'recopiees telles quelles) et "horaires". '
              "Ignore : le compte qui publie, les applis ou marques qui font de la promotion, les gagnants de concours, les abonnements. "
              "N'invente rien : si une information n'est pas ecrite dans le texte, laisse-la vide. "
              'Reponds uniquement en JSON : [{"nom":"","adresse":"","ville":"","pays":"","offres":[""],"horaires":""}] ou [] si aucun.\n\nLEGENDE :\n')


def llm_extract(text, skip=()):
    """Un LLM local lit la legende telle quelle, quelle que soit sa structure. Anti-invention : un nom, une adresse ou une offre
    qui n'est pas ECRIT dans le texte est ecarte. None = LLM indisponible (ou legende trop courte) : on retombe sur les regles."""
    if os.environ.get("TP_LLM") == "0" or len(text.strip()) < 25:
        return None
    try:
        raw = ask(LLM_PROMPT + text[:3000], tokens=800)
    except RuntimeError:
        return None
    t, out = nz(text), []
    for it in raw:
        if not isinstance(it, dict):
            continue
        nom = str(it.get("nom") or "").strip().lstrip("@")
        if not nz(nom) or nz(nom) not in t or nz(nom) in skip:  # nom invente, ou compte qui publie
            continue
        if re.fullmatch(r"[\w.]+", nom) and re.search(r"[_.]", nom):  # un pseudo : marseille_en_cornet -> Marseille En Cornet
            nom = pretty(nom)
        txt = lambda k: str(it.get(k) or "").strip()
        out.append({"nom": nom, "adresse": txt("adresse") if nz(txt("adresse")) in t and nz(txt("adresse")) else None,
                    "ville": txt("ville") or None, "pays": txt("pays") or None,
                    "prix": [o.strip() for o in it.get("offres") or [] if isinstance(o, str) and nz(o) and nz(o) in t][:8],
                    "horaires": txt("horaires") if nz(txt("horaires")) and nz(txt("horaires")) in t else None})
    return out


# ---------- recherche web gratuite (bibliotheque ddgs : sans cle ni compte) pour les restaurants inconnus d'OpenStreetMap ----------
WEB_CACHE = DATA / "web_cache.json"  # nom + ville deja cherches : jamais deux fois la meme recherche
WEB_MAX, WEB_PAUSE = 5, 2.0          # au plus 5 recherches par video, 2 s entre deux
_last_web = [0.0]
WEB_PROMPT = ("Voici des extraits de resultats de recherche web pour des restaurants. Pour chaque restaurant, recopie son adresse postale "
              "EXACTEMENT telle qu'elle est ecrite dans un extrait (numero, rue, code postal, ville), sans rien ajouter ni deviner, et indique "
              "le numero du resultat d'ou elle vient. Si aucun extrait ne donne son adresse, n'ecris rien pour ce restaurant. "
              'Reponds uniquement en JSON : [{"restaurant":1,"adresse":"","resultat":1}]\n\n')


def web_search(q, region="fr-fr"):
    """Recherche web -> [{titre, lien, extrait}]. [] si la bibliotheque est absente, bloquee ou en panne : l'analyse continue."""
    r = []
    try:
        from ddgs import DDGS
        if (wait := WEB_PAUSE - (time.monotonic() - _last_web[0])) > 0:
            time.sleep(wait)
        r = DDGS().text(q, region=region, max_results=8) or []
    except Exception as e:  # noqa
        if DEBUG: print("  [web] erreur", e)
    _last_web[0] = time.monotonic()
    if DEBUG: print(f"  [web] {q} -> {[x.get('href') for x in r][:5]}")
    return [{"titre": str(x.get("title") or ""), "lien": str(x.get("href") or ""), "extrait": str(x.get("body") or "")} for x in r if isinstance(x, dict)]


def addr_in(adr, r):  # anti-invention : adresse recopiee de CET extrait, avec un numero ou un type de voie
    a = nz(adr)
    return len(a) >= 6 and a in nz(r["titre"] + " " + r["extrait"]) and bool(re.search(r"\d", adr) or STREET.search(adr))


def web_addresses(items):
    """[{nom, ville, pays, handle}] -> [{adresse, source} ou None] dans le meme ordre. Seuls comptent les resultats qui citent le restaurant."""
    try:
        cache = json.loads(WEB_CACHE.read_text())
    except Exception:  # noqa
        cache = {}
    keys = [nz(it["nom"]) + "|" + nz(it.get("ville")) for it in items]
    out, hits, n = [cache.get(k) for k in keys], {}, 0
    for i, it in enumerate(items):
        if keys[i] in cache or n >= WEB_MAX:
            continue
        reg = "fr-fr" if not it.get("pays") or nz(it["pays"]) in ("france", "fr") else "wt-wt"
        res, seen = [], set()
        for q in [f'"{it["nom"]}" {it.get("ville") or ""} adresse'] + ([f'{it["handle"]} {it.get("ville") or ""}'] if it.get("handle") else []):
            if n >= WEB_MAX:
                break
            n += 1
            for r in web_search(q.strip(), reg):
                if r["lien"] not in seen and nz(it["nom"]) in nz(r["titre"] + " " + r["extrait"]):  # le resultat parle bien de CE restaurant
                    seen.add(r["lien"]); res.append(r)
        hits[i] = res[:6]
    todo = [i for i in hits if hits[i]]
    llm = {}
    if todo:  # un seul appel a l'IA pour tous les restaurants (le modele ne se charge qu'une fois)
        lines = []
        for num, i in enumerate(todo, 1):
            lines.append(f"RESTAURANT {num} : {items[i]['nom']} ({items[i].get('ville') or '?'})")
            lines += [f"  [{k}] {r['titre']} - {r['extrait']}" for k, r in enumerate(hits[i], 1)]
        try:
            for a in ask(WEB_PROMPT + "\n".join(lines)[:6000], tokens=400):
                try:
                    llm.setdefault(int(a.get("restaurant")), []).append((int(a.get("resultat")), str(a.get("adresse") or "").strip()))
                except (TypeError, ValueError, AttributeError):
                    continue
        except RuntimeError:
            pass  # IA indisponible : on se contente des adresses au format « 12 rue ... »
    for num, i in enumerate(todo, 1):
        found = next(({"adresse": adr, "source": hits[i][k - 1]["lien"]} for k, adr in llm.get(num, [])
                      if 1 <= k <= len(hits[i]) and addr_in(adr, hits[i][k - 1])), None)
        if not found:
            for r in hits[i]:
                if m := STREET.search(r["extrait"]):
                    found = {"adresse": re.split(r"(?<=\D)\.\s|\s[-–|·•]\s", m.group(0))[0].strip(" ,;."), "source": r["lien"]}
                    break
        out[i] = found
    for i in hits:
        cache[keys[i]] = out[i]  # meme « rien trouve » : on ne recherche plus
    try:
        WEB_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
    except OSError:
        pass
    return out


def verify(lieux, villes, progress):
    """Etablissements cites (nom, adresse ?, ville ?) -> fiches : OpenStreetMap confirme, sinon fiche « a verifier » placee sur l'adresse."""
    progress("Verification sur OpenStreetMap")
    res, zones, pending = [], {}, []  # pending : ni OSM ni adresse -> recherche web ; zones : centre de chaque ville, geocode une seule fois (une requete = 1 s d'attente)

    def zone(v, pays):
        if v.lower() not in zones:
            g = geocode(f"{v}, {pays}" if pays else v)
            zones[v.lower()] = (g["lat"], g["lon"], 0.15) if g else None  # ~15 km autour de la ville
        return zones[v.lower()]
    for b in lieux:
        uniques = {}
        for v in [b.get("ville"), *villes]:  # Marseille / marseille / vieuxport : sans doublons de casse
            if v:
                uniques.setdefault(v.lower(), v)
        vs = list(uniques.values())
        hit, addr, g, zs = None, None, None, []
        if b.get("adresse"):
            addr, g = locate(b["adresse"], vs)
            hit = osm_at(b["nom"], g) if g else None
        if not hit and not g:  # pas d'adresse (ou introuvable) : on cherche le restaurant dans la ville
            zs = [z for v in vs[:3] if (z := zone(v, b.get("pays") or ""))]  # villes que OpenStreetMap connait (« #vieuxport » : non)
            hit = next(filter(None, (osm(b["nom"], near=z) for z in zs)), None) if zs else osm(b["nom"])
        ville = vs[0][:1].upper() + vs[0][1:] if vs else None
        f = fiche(**hit, prix=b["prix"], source="legende") if hit else at_address(b["nom"], addr, g, ville=ville, pays=b.get("pays"), prix=b["prix"], source="legende")
        if b.get("horaires") and not f["horaires"]:
            f["horaires"] = b["horaires"]
        res.append(f)
        if not hit and not g and zs:  # ni OpenStreetMap, ni adresse, mais une ville connue : candidat a la recherche web
            pending.append((len(res) - 1, b, vs, ville, zs[0]))
    if pending and os.environ.get("TP_WEB") != "0":
        progress("Recherche des adresses sur le web")
        found = web_addresses([{"nom": b["nom"], "ville": ville, "pays": b.get("pays"), "handle": b.get("handle")} for _, b, _, ville, _ in pending])
        for (i, b, vs, ville, z), w in zip(pending, found):
            if not w:
                continue
            addr, g = locate(w["adresse"], vs)
            if not g or abs(g["lat"] - z[0]) > z[2] or abs(g["lon"] - z[1]) > z[2]:
                continue  # adresse introuvable, ou dans une autre ville : homonyme
            hit = osm_at(b["nom"], g)
            f = fiche(**hit, prix=b["prix"], source="legende") if hit else at_address(b["nom"], addr, g, ville=ville, pays=b.get("pays"), prix=b["prix"], source="legende")
            if not hit:
                f["confiance"] = "moyenne"  # adresse trouvee sur le web, restaurant absent d'OSM : « probable »
            if b.get("horaires") and not f["horaires"]:
                f["horaires"] = b["horaires"]
            f["source_web"] = w["source"]
            res[i] = f
    return res


def video_stage(url, wd, cap, progress):
    if not (wd / "video.mp4").exists():
        progress("Telechargement de la video")
        ytdlp(url, "-f", "bv*[height<=480]+ba/b", "--merge-output-format", "mp4", "-o", str(wd / "video.%(ext)s"))
    speech = (wd / "a.txt").read_text().strip() if (wd / "a.txt").exists() else ""
    if not speech:
        progress("Transcription de l'audio")
        try:
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", wd / "video.mp4", "-t", "60", "-vn", "-ac", "1", "-ar", "16000", wd / "a.wav"])
            run([BIN / "mlx_whisper", wd / "a.wav", "--model", WHISPER, "-f", "txt", "--output-dir", wd])
            speech = (wd / "a.txt").read_text().strip()
        except RuntimeError:
            speech = ""  # pas d'audio exploitable : images seules
    fmt = ('Reponds uniquement en JSON : [{"nom":"","adresse":"","ville":"","pays":""}] ou [] si aucun nom. '
           '"adresse" : numero et rue s\'ils sont dits, ecrits a l\'ecran ou dans la description, sinon "".')
    ctx = f"DESCRIPTION : {cap[:500]}\nPAROLES : {speech[:1800]}\n\n"
    progress("Lecture des noms cites a l'oral")
    p1 = ask("Voici la transcription automatique d'une video (orthographe approximative, sans majuscules). Elle presente des adresses de "
             "restaurants, cafes, bars ou boulangeries (ignore monuments, plages et autres lieux). Releve le NOM de chaque etablissement cite, "
             "meme s'il ressemble a une expression courante, en retrouvant toutes les adresses annoncees. N'invente rien. Ecris chaque nom comme il est ecrit ou dit, dans sa langue d'origine.\n" + ctx + fmt) if speech else []
    progress("Analyse des images")
    p2 = ask("Video de voyage.\n" + ctx + "Liste les restaurants, cafes, bars ou boulangeries (pas les monuments ni les autres lieux) dont le NOM "
             "est lisible a l'ecran (enseigne, texte incruste) ou prononce. N'invente rien. Ecris chaque nom comme il est ecrit (caracteres d'origine : japonais, coreen...).\n" + fmt,
             "--video", wd / "video.mp4", "--fps", "1", "--video-max-frames", "16")
    return p1 + p2, speech


# ---------- fiche normalisee ----------
def fiche(**k):
    base = {"nom": None, "nom_local": None, "adresse": None, "ville": None, "pays": None, "lat": None, "lon": None, "cuisine": None, "horaires": None,
            "prix": [], "site": None, "tel": None, "confiance": "haute", "source": None}
    base.update({a: b for a, b in k.items() if b is not None or a in base})
    return base


def same(a, b):
    """Meme etablissement ? Par adresse si les deux en ont une, sinon par nom."""
    if nz(a.get("adresse")) and nz(b.get("adresse")):
        return nz(a["adresse"]) == nz(b["adresse"])
    return bool(nz(a.get("nom"))) and nz(a.get("nom")) == nz(b.get("nom"))


def analyze(url, progress=lambda s: None):
    """url -> liste de fiches restaurant (dict). Une video deja analysee (meme identifiant, quels que soient les parametres du lien)
    est relue depuis le cache result.json : ni telechargement, ni Whisper, ni Qwen."""
    legacy = DATA / "jobs" / hashlib.sha1(url.encode()).hexdigest()[:10]  # ancien nom de dossier (hash du lien exact)
    key, url = canon(url)
    wd = DATA / "jobs" / hashlib.sha1(key.encode()).hexdigest()[:10]
    if legacy.exists() and not wd.exists():
        legacy.rename(wd)
    wd.mkdir(parents=True, exist_ok=True)
    cache = wd / "result.json"
    if cache.exists():
        progress("Deja analysee : resultat en cache")
        return json.loads(cache.read_text())
    res = _analyze(url, wd, progress)
    cache.write_text(json.dumps(res, ensure_ascii=False))
    return res


def forget(url):
    """Oublie le resultat en cache d'une video pour pouvoir la reanalyser (legende, video et transcription restent en cache)."""
    (DATA / "jobs" / hashlib.sha1(canon(url)[0].encode()).hexdigest()[:10] / "result.json").unlink(missing_ok=True)


def _analyze(url, wd, progress):
    progress("Lecture du post")
    info = get_info(url, wd)
    acc = accounts(info)
    title = info.get("title") or ""
    if ACCOUNT_TITLE.match(title) or nz(title) in acc:
        title = ""  # titre fourni par yt-dlp = nom du compte
    res, p = [], parse_caption("\n".join(filter(None, [title, info.get("description"), info.get("location")])), acc)
    cap = p["texte"]

    # 0) l'IA locale lit la legende telle quelle (n'importe quelle structure) ; chaque nom doit etre ecrit dans le texte, puis OpenStreetMap confirme
    progress("Lecture de la legende")
    if items := llm_extract(cap, acc):
        return verify(items, p["villes"], progress)

    # 0 bis) secours sans IA : legende en blocs « 📍@restaurant » + ses offres
    if p["lieux"]:
        return verify([{**b, "ville": None, "pays": None, "horaires": None} for b in p["lieux"]], p["villes"], progress)

    # 1) legende : nom + adresses (n'importe quel pays)
    if p["nom"] and p["adresses"]:
        progress("Verification sur OpenStreetMap")
        for addr in p["adresses"]:
            addr, g = locate(addr, p["villes"])
            hit = (osm_at(p["nom"], g) if g else osm(p["nom"], hint=addr.split(",")[-1].strip())) or {}
            lat = hit.get("lat") or (g or {}).get("lat"); lon = hit.get("lon") or (g or {}).get("lon")
            conf = hit.get("confiance") or "a_verifier"  # "haute" seulement si OSM confirme un lieu de restauration de ce nom a l'adresse
            res.append(fiche(nom=hit.get("nom") or p["nom"], nom_local=hit.get("nom_local"), adresse=addr,
                             ville=hit.get("ville") or (g or {}).get("ville"), pays=hit.get("pays") or (g or {}).get("pays"),
                             lat=lat, lon=lon, cuisine=hit.get("cuisine"), horaires=hit.get("horaires") or "; ".join(p["horaires"]) or None,
                             prix=p["prix"], site=hit.get("site"), tel=hit.get("tel"),
                             confiance=conf, source="legende"))
    if res:
        return res

    # 2) video : voix + images
    spoken, speech = video_stage(url, wd, cap, progress)
    progress("Verification sur OpenStreetMap")
    seen = set()
    for q in spoken:
        nom = (q.get("nom") or "").strip()
        if not nom or nom.lower() in seen or nz(nom) in acc:  # jamais le nom du compte
            continue
        seen.add(nom.lower())
        hit = osm(nom, hint=q.get("ville") or "", pays=q.get("pays") or "") or expand(nom, q.get("ville") or "", q.get("pays") or "", speech)
        if hit and any(f["adresse"] == hit["adresse"] for f in res):
            continue
        if hit:
            res.append(fiche(**hit, prix=[], source="video"))
            continue
        # adresse donnee par l'IA (ou la seule adresse de la legende) : on la cherche sur OSM, avec le restaurant s'il y est, sinon l'adresse seule
        addr = (q.get("adresse") or "").strip() or (p["adresses"][0] if len(p["adresses"]) == 1 else "")
        addr, g = locate(addr, [", ".join(filter(None, [q.get("ville"), q.get("pays")])), *p["villes"]]) if addr else (None, None)
        hit = osm_at(nom, g) if g else None
        res.append(fiche(**hit, prix=[], source="video") if hit else at_address(nom, addr, g, ville=q.get("ville"), pays=q.get("pays"), source="video"))
    if not res:  # aucun nom trouve, mais des adresses dans la legende : on les place quand meme
        for addr in p["adresses"]:
            addr, g = locate(addr, p["villes"])
            res.append(at_address(None, addr, g, horaires="; ".join(p["horaires"]) or None, prix=p["prix"], source="legende"))
    return res
