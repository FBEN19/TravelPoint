"""Travelpoint - pipeline d'analyse (restaurants) : lien -> fiches restaurant.
Texte d'abord (legende + OpenStreetMap), video (Whisper en local pour la voix + images lues par OpenRouter) seulement si besoin.
Tout tourne en local ; un dossier de travail par lien (cache) dans data/jobs/<hash>/.
"""
import base64, difflib, html, ipaddress, socket, hashlib, json, os, pathlib, re, subprocess, sys, threading, time, unicodedata, urllib.error, urllib.parse, urllib.request

BIN = pathlib.Path(sys.executable).parent  # binaires du venv (yt-dlp, mlx_whisper)
os.environ["PATH"] += os.pathsep + "/opt/homebrew/bin" + os.pathsep + "/usr/local/bin"  # ffmpeg (brew)
DATA = pathlib.Path(os.environ.get("TP_DATA", "data")); DATA.mkdir(exist_ok=True)
WHISPER = os.environ.get("TP_WHISPER", "mlx-community/whisper-large-v3-turbo")  # plus precis que small sur les noms propres, ~1,6 Go
UA = {"User-Agent": "Travelpoint/0.1 (self-hosted)", "Accept-Language": "fr,en"}
DEBUG = bool(os.environ.get("TP_DEBUG"))
GENERIC = {"fyp", "foryou", "pourtoi", "viral", "reels", "reel", "voyage", "travel", "food", "foodie", "paris", "france",
           "instagram", "tiktok", "explore", "resto", "restaurant", "bonplan", "bonsplans"}
FOOD = {"restaurant", "fast_food", "cafe", "bar", "pub", "food_court", "biergarten", "ice_cream"}
FOOD_SHOP = {"bakery", "pastry", "confectionery", "deli", "chocolate"}


TRACE = [None]  # fichier de trace de l'analyse en cours (reponses de l'IA, recherches OSM et web) : pour comprendre un echec


def trace(kind, **data):
    if TRACE[0]:
        try:
            with open(TRACE[0], "a") as f:
                f.write(json.dumps({"t": kind, **data}, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass


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


VIDEO_SITES = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com")


def lien_video_accepte(url):
    """Seuls YouTube, Instagram et TikTok sont analyses : un lien vers un autre site (ou vers ton reseau local,
    « http://192.168.1.1 ») ne doit jamais etre ouvert par yt-dlp depuis ton Mac."""
    u = urllib.parse.urlparse((url or "").strip())
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and u.port in (None, 80, 443) and any(host == d or host.endswith("." + d) for d in VIDEO_SITES)


def est_public(url):
    """http(s), port standard, et un nom qui ne mene qu'a des adresses publiques d'Internet
    (pas localhost, pas 192.168.x.x / 10.x.x.x, pas les adresses internes des fournisseurs de cloud)."""
    u = urllib.parse.urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.hostname or u.port not in (None, 80, 443) or u.username or u.password:
        return False
    try:
        ips = {a[4][0] for a in socket.getaddrinfo(u.hostname, None)}
    except (OSError, UnicodeError):
        return False
    return bool(ips) and all(ipaddress.ip_address(ip.split("%")[0]).is_global for ip in ips)


class _RedirectionPublique(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not est_public(newurl):  # une page publique qui renvoie vers ton reseau local : refusee
            raise urllib.error.HTTPError(newurl, code, "redirection vers une adresse non publique refusee", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def ouvrir_page_publique(url, timeout=10, taille=600000):
    """Ouvre une page trouvee sur le web (donc choisie par n'importe qui) seulement si elle est publique, redirections comprises."""
    if not est_public(url):
        raise ValueError("adresse non publique")
    ouvreur = urllib.request.build_opener(_RedirectionPublique)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Travelpoint", "Accept-Language": "fr,en"})
    with ouvreur.open(req, timeout=timeout) as r:
        return r.read(taille).decode("utf-8", "ignore")


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
    return list(dict.fromkeys(x.strip() for x in c if x.lower() not in GENERIC and nz(x) not in skip
                              and not re.match(rf"(?i)(?:{TYPES})\b", x.strip())))  # « Rue Saint Martin » : une rue, pas un nom


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
        adrs = list(dict.fromkeys(a.strip(" ,;.-–") for l in body for a in STREET.findall(l)))
        prix = [l for l in body if nz(l) not in {nz(a) for a in adrs}]  # une ligne qui n'est qu'une adresse n'est pas une offre
        out.append({"nom": name_from_handle(h, cands) or pretty(h), "handle": h, "prix": prix[:8], "adresses": adrs})
    return out


def parse_caption(text, skip=()):
    text = scrub(clean(text), skip)
    cands = candidates(text, skip)
    handle = handle_of(text, skip)
    pins = [a for a in (re.sub(r"#[\w-]+", " ", a).strip(" ,;.-–") for a in ADDR.findall(text) if not a.strip().startswith("@")) if len(nz(a)) >= 3]  # « 📍 #food #toulouse » : des hashtags, pas une adresse
    pins = pins or [a.strip(" ,;.-–") for a in STREET.findall(text)]  # sans 📍 : adresse ecrite telle quelle ; « 📍@pseudo » n'est pas une adresse
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


GENERIQUES_NOM = {"le", "la", "les", "l", "the", "chez", "au", "aux", "de", "du", "des", "restaurant", "resto", "bar", "cafe", "brasserie",
                  "bistrot", "ramen", "sushi", "pizza", "pizzeria", "burger", "boulangerie", "officiel", "paris", "marseille", "lyon", "lille",
                  "bordeaux", "nice", "toulouse", "nantes", "bruxelles", "tokyo", "osaka", "london", "londres"}


def sim(a, b):
    """Ressemblance de deux noms (0 a 1). Un nom contenu dans l'autre compte si c'est l'essentiel du nom
    (« Hanbei » / « Le Hanbei », « Holyburek » / « Holyburek Paris »), pas s'il n'en est qu'un morceau (« Honoré » / « Maison Saint Honoré »)."""
    ra, rb = a or "", b or ""
    a, b = nz(ra), nz(rb)
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        (court, rc), (long_, rl) = sorted(((a, ra), (b, rb)), key=lambda x: len(x[0]))
        mots = lambda s: [nz(w) for w in re.findall(r"[\wÀ-ÿ]+", s) if nz(w)]
        reste = [w for w in mots(rl) if w not in mots(rc)]
        if len(court) / len(long_) > 0.5 or all(w in GENERIQUES_NOM for w in reste):
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
    trace("osm", q=params.get("q"), zone=params.get("viewbox"), resultats=[x.get("display_name", "")[:80] for x in r][:5])
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
    tries.insert(1, re.sub(r"(?i)\s*\b\d{1,2}\s*(?:e|è|ème|eme|er)\b(?:\s*arr\w*)?\s*$", "", n))  # sans « 16E » / « 2e arrondissement » final
    tries += [", ".join(parts[-3:]), ", ".join(parts[-2:])] if len(parts) > 2 else []        # quartier + ville + pays
    tries.append(re.sub(r"[\d０-９\-－ー−丁目番地号\s]+$", "", n))                               # adresses japonaises : sans le numero final
    for q in dict.fromkeys(t for t in tries if len(t) > 2):
        r = nominatim({"q": q, "limit": 1, "addressdetails": 1})
        if r:
            a = r[0].get("address", {})
            return {"lat": float(r[0]["lat"]), "lon": float(r[0]["lon"]), "ville": place_city(a), "pays": a.get("country"), "precis": q == n or bool(re.match(r"\s*\d", q))}
    return None


def osm(name, near=None, hint="", pays=""):
    """Etablissement de restauration nomme `name`, cherche DANS UNE ZONE : near=(lat, lon, demi-cote en degres) ou ville `hint`."""
    if name and not [w for w in re.findall(r"[\wÀ-ÿ'’]+", name) if len(nz(w)) >= 3 and nz(w) not in MOTS_GENERIQUES]:
        name = None  # « Sushi », « Restaurant » : pas un nom ; seul le restaurant unique a une adresse precise compte
    p = {"q": name, "addressdetails": 1, "extratags": 1, "namedetails": 1, "limit": 10}
    if not near and hint:
        g = geocode(f"{hint}, {pays}" if pays else hint)
        near = (g["lat"], g["lon"], 0.15) if g else None  # ~15 km autour de la ville
    if near:
        la, lo, d = near
        p.update(viewbox=f"{lo - d},{la + d},{lo + d},{la - d}", bounded=1)
    is_food = lambda r: r.get("type") in FOOD or (r.get("class") == "shop" and r.get("type") in FOOD_SHOP)
    if not name and not (near and near[2] <= 0.002):
        return None  # nom inconnu : on ne peut chercher qu'a une adresse precise
    best = None
    for r in (nominatim(p) if name else []):
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


def ecrit(nom, texte):
    """Le nom figure-t-il dans le texte, a l'orthographe pres ? (anti-invention tolerant aux fautes de transcription)"""
    if nz(nom) and nz(nom) in nz(texte):
        return True
    words, n = re.findall(r"[\wÀ-ÿ'’]+", texte), len(nom.split())
    return any(sim(nom, " ".join(words[i:i + k])) >= 0.85 for k in (n, n + 1) for i in range(len(words) - k + 1))


def variantes(nom, speech):
    """Nom entendu d'un seul mot, souvent coupe par l'IA ('bouillon') -> noms plus longs pris dans la transcription ('comme un bouillon', 'un bouillon')."""
    words = re.findall(r"[\wÀ-ÿ'’]+", speech)
    low, nw = [w.lower() for w in words], nom.lower().split()
    if len(nw) != 1:
        return []
    for i in range(len(low) - len(nw) + 1):
        if low[i:i + len(nw)] == nw:
            return [" ".join(words[i - k:i + len(nw)]) for k in (2, 1) if i - k >= 0]
    return []


# ---------- IA : OpenRouter (modeles gratuits, en ligne). Sans elle (pas de cle, quota, panne) : les regles simples ----------
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
STATS = {"openrouter": 0, "echecs": 0, "quota_jour": 0}  # lectures reussies / echouees (affiche par le banc)


def or_cles():
    """Cles OpenRouter, dans l'ordre : variable OPENROUTER_API_KEY (plusieurs separees par des virgules), ou fichier
    data/openrouter.key, une cle par ligne (jamais versionne : data/ est ignore par git)."""
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        return [k.strip() for k in os.environ["OPENROUTER_API_KEY"].split(",") if k.strip()]
    fichiers = [DATA / "openrouter.key", pathlib.Path(__file__).resolve().parent / "data" / "openrouter.key"]  # (le banc a son propre dossier data)
    f = next((f for f in fichiers if f.exists()), None)
    return [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.strip().startswith("#")] if f else []


def or_cle(usage="ia"):
    """Ligne 1 : l'IA (modeles gratuits). Ligne 2 : la transcription Whisper (payante), pour suivre sa depense a part ;
    sans 2e ligne, la transcription utilise aussi la 1re cle."""
    cles = or_cles()
    if not cles:
        return ""
    return cles[1] if usage == "transcription" and len(cles) > 1 else cles[0]


def or_modeles():
    """Ordre d'essai : TP_OR_MODELES=m1,m2 si defini, sinon modeles_openrouter.txt (une ligne par modele), sinon le routeur gratuit."""
    if os.environ.get("TP_OR_MODELES"):
        return [m.strip() for m in os.environ["TP_OR_MODELES"].split(",") if m.strip()]
    f = pathlib.Path(__file__).resolve().parent / "modeles_openrouter.txt"
    lignes = [l.split("#")[0].strip() for l in f.read_text().splitlines()] if f.exists() else []
    return [l for l in lignes if l] or ["openrouter/free"]


STATS_MODELES = {}  # par modele : appels, ok, vides, illisibles, erreurs, quota, sautes (pas d'images), secondes (des reponses ok)
_or_pause = {}  # modele -> instant ou il redevient utilisable (apres un 429 : limite par minute)
OR_INFOS = DATA / "openrouter_modeles.json"  # ce que chaque modele sait lire, demande a OpenRouter une fois par jour


def st_modele(m):
    return STATS_MODELES.setdefault(m, {"appels": 0, "ok": 0, "vides": 0, "illisibles": 0, "erreurs": 0, "quota": 0, "sautes": 0, "secondes": 0.0, "derniere_erreur": ""})


def lit_images(m):
    """True / False selon OpenRouter ; None si on ne sait pas (liste injoignable, modele inconnu, routeur) : on essaie quand meme."""
    try:
        if not OR_INFOS.exists() or time.time() - OR_INFOS.stat().st_mtime > 86400:
            rep = json.loads(urllib.request.urlopen(urllib.request.Request("https://openrouter.ai/api/v1/models", headers=UA), timeout=20).read())
            infos = {x["id"]: (x.get("architecture") or {}).get("input_modalities") or [] for x in rep.get("data", []) if x.get("id")}
            OR_INFOS.parent.mkdir(parents=True, exist_ok=True)
            OR_INFOS.write_text(json.dumps(infos))
        infos = json.loads(OR_INFOS.read_text())
    except Exception:  # noqa
        return None
    return ("image" in infos[m]) if m in infos else None


def images_video(video, n=12):
    """n images reparties sur TOUTE la video (480 px de large), en base64 ; gardees en cache a cote de la video."""
    d = pathlib.Path(video).parent / "images"
    if not d.exists() or not any(d.glob("*.jpg")):
        d.mkdir(exist_ok=True)
        dur = float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video]).stdout.strip() or 30)
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-vf", f"fps={n / max(dur, 1):.4f},scale=480:-2", "-frames:v", str(n), "-q:v", "5", d / "%02d.jpg"])
    return [base64.b64encode(p.read_bytes()).decode() for p in sorted(d.glob("*.jpg"))][:n]


def or_envoyer(req):
    """Un appel -> (reponse, secondes). Avec TP_OR_ATTENTE=1 (banc des modeles), une limite PAR MINUTE (429) fait attendre
    puis reessayer le meme modele au lieu de passer au suivant : chaque modele est juge sur ses reponses, pas sur la file d'attente."""
    for n in range(4):
        t0 = time.monotonic()
        try:
            return json.loads(urllib.request.urlopen(req, timeout=180).read()), time.monotonic() - t0
        except urllib.error.HTTPError as e:
            try:
                e.corps = e.read().decode(errors="ignore")[:300]
            except Exception:  # noqa
                e.corps = ""
            if e.code != 429 or os.environ.get("TP_OR_ATTENTE") != "1" or re.search(r"(?i)per.?day|daily", e.corps) or n == 3:
                raise
            time.sleep(20 * (n + 1))


def ask_openrouter(prompt, extra, tokens):
    """Meme consigne qu'en local ; une video est envoyee sous forme d'images. Renvoie (texte, modele qui a repondu)."""
    contenu, extra = [{"type": "text", "text": prompt}], [str(x) for x in extra]
    if "--video" in extra:
        n = int(extra[extra.index("--video-max-frames") + 1]) if "--video-max-frames" in extra else 12
        contenu += [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}} for b in images_video(extra[extra.index("--video") + 1], min(n, 12))]
    erreurs, images = [], "--video" in extra
    for m in or_modeles():  # un par un : on passe au suivant sur une erreur, un quota, une reponse vide ou illisible
        st = st_modele(m)
        if images and lit_images(m) is False:
            st["sautes"] += 1
            erreurs.append(f"{m} : ne lit pas les images")
            continue
        if _or_pause.get(m, 0) > time.monotonic() and os.environ.get("TP_OR_ATTENTE") != "1":  # le banc, lui, reessaie toujours
            erreurs.append(f"{m} : en pause (limite par minute)")
            continue
        corps = {"model": m, "messages": [{"role": "user", "content": contenu}], "temperature": 0,
                 "max_tokens": max(tokens * 4, 2000),  # les modeles qui « reflechissent » ont besoin de place (gratuit)
                 "reasoning": {"effort": "low"}}
        req = urllib.request.Request(OR_URL, data=json.dumps(corps).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {or_cle()}", "Content-Type": "application/json", "X-Title": "Travelpoint"})
        st["appels"] += 1
        try:
            rep, duree = or_envoyer(req)
        except urllib.error.HTTPError as e:
            corps_err = getattr(e, "corps", "")
            if e.code == 429:
                st["quota"] += 1
                _or_pause[m] = time.monotonic() + 60
                if re.search(r"(?i)per.?day|daily", corps_err):
                    STATS["quota_jour"] = 1  # quota du jour epuise : le banc des modeles s'arrete
            else:
                st["erreurs"] += 1
            erreurs.append(f"{m} : HTTP {e.code} {corps_err[:100]}".strip())
            st["derniere_erreur"] = f"HTTP {e.code} {corps_err[:150]}".strip()
            continue
        except Exception as e:  # noqa : reseau, delai depasse...
            st["erreurs"] += 1
            erreurs.append(f"{m} : {e}")
            st["derniere_erreur"] = str(e)[:150]
            continue
        if rep.get("error"):
            st["erreurs"] += 1
            erreurs.append(f"{m} : {str(rep['error'])[:120]}")
            st["derniere_erreur"] = str(rep["error"])[:150]
            continue
        choix = (rep.get("choices") or [{}])[0]
        texte = (choix.get("message") or {}).get("content") or ""
        if not texte.strip():
            st["vides"] += 1
            erreurs.append(f"{m} : reponse vide ({choix.get('finish_reason')})")  # toute la place prise par la reflexion
            continue
        if json_ok(texte) is None:
            st["derniere_erreur"] = f"sans JSON : {texte.strip()[:80]}"
            st["illisibles"] += 1
            erreurs.append(f"{m} : reponse illisible ({texte.strip()[:60]!r})")
            continue
        st["ok"] += 1
        st["secondes"] += duree
        return texte, rep.get("model", m)
    raise RuntimeError("; ".join(erreurs)[:400] or "aucun modele")


def json_ok(texte):
    """La liste JSON demandee, ou None si la reponse n'en contient pas (bavardage, JSON casse). [] = « aucun restaurant », une vraie reponse."""
    t = re.sub(r"```(?:json)?", "", texte or "")
    for m in re.finditer(r"\[", t):
        fin = t.rfind("]")
        while fin > m.start():
            try:
                v = json.loads(t[m.start():fin + 1])
                if isinstance(v, list) and all(isinstance(x, dict) for x in v):
                    return v
                break
            except json.JSONDecodeError:
                fin = t.rfind("]", m.start(), fin)
    return None


def ask(prompt, *extra, tokens=300):
    """Lecture par l'IA (OpenRouter) -> liste JSON. RuntimeError si elle est indisponible (TP_LLM=0, pas de cle, quota, panne) :
    chaque appelant continue sans elle (regles simples pour la legende, adresses « 12 rue ... » pour le web, rien pour la video)."""
    if os.environ.get("TP_LLM") == "0":
        raise RuntimeError("IA desactivee (TP_LLM=0)")
    if not or_cle():
        raise RuntimeError("pas de cle OpenRouter (data/openrouter.key)")
    try:
        texte, modele = ask_openrouter(prompt, extra, tokens)
    except Exception as e:  # noqa : quota (429), panne, reseau...
        STATS["echecs"] += 1
        trace("llm_echec", raison=str(e)[:300])
        if DEBUG:
            print("  [llm] OpenRouter indisponible :", e)
        raise RuntimeError(f"OpenRouter indisponible : {e}")
    STATS["openrouter"] += 1
    if DEBUG:
        print(f"  [llm openrouter:{modele}]", texte[-900:])
    trace("llm", consigne=prompt[:200], images=bool(extra), reponse=texte[-1500:], via=f"openrouter:{modele}")
    return json_ok(texte) or []


LLM_PROMPT = ("Voici la legende d'une video de reseau social (format libre : emojis, @pseudos, hashtags, publicites...). "
              "Releve les restaurants, cafes, bars ou boulangeries que la video PRESENTE (pas ceux cites en passant, comme les autres restaurants d'un chef). Pour chacun : "
              '"nom" (tel qu\'ecrit ; un @pseudo place a cote d\'un lieu est en general le nom de l\'etablissement), '
              '"adresses" (liste : TOUTES ses adresses completes ecrites dans le texte, une par element ; [] si aucune), "ville", "pays", '
              '"offres" (les lignes de prix ou d\'offres qui le concernent, '
              'recopiees telles quelles) et "horaires". '
              "Ignore : le compte qui publie, les applis ou marques qui font de la promotion, les gagnants de concours, les abonnements, "
              "et les PERSONNES (chef, influenceur, invite, createur de la video) : une personne n'est jamais un etablissement. "
              "N'invente rien : si une information n'est pas ecrite dans le texte, laisse-la vide. "
              'Reponds uniquement en JSON : [{"nom":"","adresses":[""],"ville":"","pays":"","offres":[""],"horaires":""}] ou [] si aucun.\n\nLEGENDE :\n')


def vraie_adresse(a):
    """« 12 rue X », « Rue X 31, 1050 Ixelles », « 1-22 Ikutamae », un code postal : oui. « MARSEILLE », « pres d'Opera », « Nakameguro » : non."""
    a = (a or "").strip()
    if not a or re.match(r"(?i)(pr[eè]s|[aà] c[oô]t[eé]|vers|en face|quartier|dans le|au c(?:oe|œ)ur|non loin)\b", a):
        return False
    return bool(STREET.search(a) or re.search(r"(?<!\d)\d{1,4}\s*[-‐]\s*\d{1,4}(?!\d)", a) or re.search(r"(?<!\d)\d{4,6}(?!\d)", a))


PERSONNE = (r"(?:l['’]\s*adresse|les\s+adresses|le\s+resto|le\s+restaurant|le\s+spot|la\s+table|le\s+qg|la\s+cantine|chef|cheffe|"
            r"invite|invitee|createur|creatrice|fondateur|fondatrice|proprietaire|par)")


def personne(nom, texte):
    """« L'adresse de Camille Lavabre », « par le chef Benjamin Mathieu » : une personne, pas un etablissement."""
    if not nom or not re.fullmatch(r"[A-ZÀ-Ý][\w'’-]+(?:\s+[A-ZÀ-Ý][\w'’-]+){1,2}", nom.strip()):
        return False  # seulement « Prenom Nom »
    if re.match(r"(?i)(?:le|la|les|l['’]|chez|the|el|il|au|aux)\b", nom.strip()):
        return False  # « Le Vaudesir », « Chez Paul » : des etablissements
    sa = lambda s: "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()
    return bool(re.search(rf"\b{PERSONNE}\s+(?:de\s+|d['’]\s*|du\s+|(?:le|la)\s+chef(?:fe)?\s+)?{re.escape(sa(nom.strip()))}\b", sa(texte)))


def nom_descriptif(n):
    """« Le resto japonais le plus immersif de Bordeaux » : une phrase d'accroche, pas le nom d'un etablissement."""
    return len(n.split()) >= 6 or bool(re.search(r"(?i)\b(?:le|la|les)\s+(?:plus|meilleure?s?)\b|\bmeilleure?s?\b|\bincroyables?\b", n))


def split_addrs(values):
    """["63 Rue A, Paris\n- 50 Rue B, Paris", ...] -> ["63 Rue A, Paris", "50 Rue B, Paris"] (une enseigne peut avoir plusieurs adresses)."""
    out = {}
    for v in values if isinstance(values, list) else [values]:
        for part in re.split(r"\n|;|\s[|/]\s", v if isinstance(v, str) else ""):
            part = part.strip(" -•·*\t")
            if len(nz(part)) >= 6:
                out.setdefault(nz(part), part)
    return list(out.values())


def llm_extract(text, skip=()):
    """L'IA lit la legende telle quelle, quelle que soit sa structure. Anti-invention : un nom, une adresse ou une offre
    qui n'est pas ECRIT dans le texte est ecarte. None = LLM indisponible (ou legende trop courte) : on retombe sur les regles."""
    if os.environ.get("TP_LLM") == "0" or len(text.strip()) < 25:
        return None
    try:
        raw = ask(LLM_PROMPT + text[:3000], tokens=800)
    except RuntimeError:
        return None
    t, out, seen = nz(text), [], set()
    for it in raw:
        if not isinstance(it, dict):
            continue
        nom = str(it.get("nom") or "").strip().lstrip("@")
        if not nz(nom):  # nom vide mais adresse ecrite (« 📍156 Rue Saint Martin 75003 ») : fiche sans nom, comme une adresse prise pour un nom
            ecrites = [a for a in split_addrs(it.get("adresses") if isinstance(it.get("adresses"), list) else it.get("adresse")) if nz(a) in t and vraie_adresse(a)]
            if not ecrites:
                continue
            nom = ecrites[0]
        if nz(nom) not in t or nz(nom) in skip:  # nom invente, ou compte qui publie
            continue
        if nom_descriptif(nom) and not vraie_adresse(nom):
            trace("ecarte", nom=nom, raison="phrase d'accroche, pas un nom")
            continue
        if personne(nom, text):
            trace("ecarte", nom=nom, raison="une personne, pas un etablissement")
            continue
        handle, nom_adresse = None, None
        if vraie_adresse(nom):  # « 156 Rue Saint Martin 75003 » : une adresse, pas un nom (la legende n'en donne pas)
            trace("nom_inconnu", adresse=nom)
            nom_adresse, nom = nom, None
        elif re.fullmatch(r"[\w.]+", nom) and (re.search(r"[_.]", nom) or nom.islower()):  # un pseudo : rednoodle.officiel -> Red Noodle
            handle = nom
            nom = name_from_handle(nom, candidates(text, skip)) or pretty(nom)              # (decoupe grace au texte), sinon Rednoodle
        txt = lambda k: ("" if isinstance(it.get(k), list) else str(it.get(k) or "").strip())  # ["Paris","Lille"] : l'adresse dira la ville
        base = {"nom": nom, "handle": handle, "adresse_ecrite": True, "ville": txt("ville") or None, "pays": txt("pays") or None,
                "prix": [o.strip() for o in it.get("offres") or [] if isinstance(o, str) and nz(o) and nz(o) in t][:8],
                "horaires": txt("horaires") if nz(txt("horaires")) and nz(txt("horaires")) in t else None}
        adresses = [a for a in split_addrs(it.get("adresses") if isinstance(it.get("adresses"), list) else it.get("adresse")) if nz(a) in t and vraie_adresse(a)]  # « MARSEILLE » n'est pas une adresse
        adresses = adresses or ([nom_adresse] if nom_adresse else [])
        for a in adresses or [None]:  # une fiche par adresse ; une adresse inventee (absente du texte) est ecartee
            if (nz(nom), nz(a)) not in seen:
                seen.add((nz(nom), nz(a)))
                out.append({**base, "adresse": a})
    return out


# ---------- recherche web gratuite (bibliotheque ddgs : sans cle ni compte) pour les restaurants inconnus d'OpenStreetMap ----------
WEB_CACHE = DATA / "web_cache.json"  # nom + ville deja cherches : jamais deux fois la meme recherche
WEB_MAX, WEB_PAUSE = 5, 2.0          # au plus 5 recherches par video, 2 s entre deux
WEB_REESSAI = 15                     # attente avant la seconde chance quand les moteurs refusent
_last_web = [0.0]
# moteurs interroges (pas Wikipedia ni Grokipedia, que le mode « auto » met en premier et qui ne connaissent pas les restaurants)
WEB_MOTEURS = os.environ.get("TP_WEB_MOTEURS", "bing,brave,duckduckgo,mojeek,yahoo,startpage,google")
WEB_PROMPT = ("Voici des extraits de resultats de recherche web pour des restaurants. Pour chaque restaurant, recopie son adresse postale "
              "EXACTEMENT telle qu'elle est ecrite dans un extrait (numero, rue, code postal, ville), sans rien ajouter ni deviner, et indique "
              "le numero du resultat d'ou elle vient. Si aucun extrait ne donne son adresse, n'ecris rien pour ce restaurant. "
              'Reponds uniquement en JSON : [{"restaurant":1,"adresse":"","resultat":1}]\n\n')


MOTS_GENERIQUES = {"restaurant", "resto", "ramen", "sushi", "pizza", "pizzeria", "burger", "bar", "cafe", "bistrot", "brasserie", "bouillon",
                   "boulangerie", "chez", "les", "the", "des", "maison"}


def cite(nom, texte, flou=False):
    """Le texte parle-t-il de ce restaurant ? Nom complet, ou tous ses mots distinctifs (« Ishin » suffit pour « Ishin ramen »).
    flou : nom ENTENDU, la transcription ecorche l'orthographe (« Ishin » pour « Isshin ») : un mot tres proche suffit."""
    t = nz(texte)
    if nz(nom) and nz(nom) in t:
        return True
    mots = [nz(w) for w in re.findall(r"[\wÀ-ÿ'’]+", nom or "") if len(nz(w)) >= 3 and nz(w) not in MOTS_GENERIQUES]
    if not flou:  # au pluriel pres : « Kongfu Pâtes » / « Kongfu Pâte »
        return bool(mots) and all(m in t or (len(m) > 4 and m[-1] in "sx" and m[:-1] in t) for m in mots)
    proches = {nz(w) for w in re.findall(r"[\wÀ-ÿ'’]+", texte or "")}
    return bool(mots) and all(m in t or any(len(m) >= 4 and w[:1] == m[:1] and difflib.SequenceMatcher(None, m, w).ratio() >= 0.8 for w in proches) for m in mots)


def orthographe(nom, textes):
    """Nom ENTENDU -> orthographe des pages web qui le confirment : « Ishin ramen » + « Isshin Ramen - TheFork » -> « Isshin ramen »."""
    mots_web = [w for t in textes for w in re.findall(r"[\wÀ-ÿ'’]+", t)]
    out = []
    for w in nom.split():
        m = nz(w)
        proches = [x for x in mots_web if len(m) >= 4 and nz(x) != m and nz(x)[:1] == m[:1] and difflib.SequenceMatcher(None, m, nz(x)).ratio() >= 0.8]
        if proches and not any(nz(x) == m for x in mots_web):  # le mot tel qu'entendu n'apparait nulle part : on prend l'orthographe la plus frequente
            x = max(set(proches), key=proches.count)
            w = x if not x.isupper() else x.title()
        out.append(w)
    return " ".join(out)


RESTO = re.compile(r"(?i)restau|resto\b|bistro|brasserie|caf[eé]|\bbar\b|cuisine|menu|ramen|sushi|pizz|burger|boulang|p[aâ]tisser|traiteur|cantine|"
                   r"food|kitchen|tripadvisor|thefork|lafourchette|opentable|deliveroo|uber ?eats|izakaya|tapas|cr[eê]pe")


def web_search(q, region="fr-fr", dire=None):
    """Recherche web -> [{titre, lien, extrait}]. [] si la bibliotheque est absente, bloquee ou en panne : l'analyse continue."""
    r = []
    for essai_n in range(2):  # moteurs satures ou trop de requetes rapprochees : une seconde chance 15 s plus tard
        try:
            from ddgs import DDGS
            if (wait := WEB_PAUSE - (time.monotonic() - _last_web[0])) > 0:
                time.sleep(wait)
            r = DDGS().text(q, region=region, max_results=8, backend=WEB_MOTEURS) or []
            break
        except ImportError:
            break
        except Exception as e:  # noqa
            trace("web_erreur", q=q, erreur=str(e)[:200])
            if dire:
                dire(f"  web : bloque ({str(e)[:40]})" + (", nouvel essai dans 15 s" if essai_n == 0 else ""))
            if DEBUG: print("  [web] erreur", e)
            if essai_n == 0:
                time.sleep(WEB_REESSAI)
        finally:
            _last_web[0] = time.monotonic()
    if DEBUG: print(f"  [web] {q} -> {[x.get('href') for x in r][:5]}")
    trace("web", q=q, resultats=[x.get("href") for x in r][:8])
    if dire:
        sites = list(dict.fromkeys(urllib.parse.urlparse(x.get("href") or "").netloc.removeprefix("www.") for x in r if isinstance(x, dict)))
        dire(f"  web : {q}  →  {len(r)} resultat(s)" + (f" ({', '.join(sites[:4])})" if sites else ""))
    return [{"titre": str(x.get("title") or ""), "lien": str(x.get("href") or ""), "extrait": str(x.get("body") or "")} for x in r if isinstance(x, dict)]


def addr_in(adr, r):
    """Anti-invention : le numero et la rue sont recopies de CET extrait. La ville peut etre ajoutee par l'IA
    (« 22 Rue Fernand Philippart, Bordeaux » pour un extrait « 22 Rue Fernand Philippart 33000 Bordeaux »)."""
    t = nz(r["titre"] + " " + r["extrait"])
    rue = nz(adr.split(",")[0])
    return len(rue) >= 6 and (nz(adr) in t or rue in t) and bool(re.search(r"\d", adr) or STREET.search(adr))


PAGES_FERMEES = ("pagesjaunes.", "tripadvisor.", "mappy.", "maps.apple.", "google.", "facebook.", "instagram.", "tiktok.", "youtube.")


def adresse_page(url, nom, ville):
    """Les extraits de recherche n'ont pas toujours l'adresse, la page elle-meme souvent si (fiche JSON-LD des annuaires et offices
    de tourisme, ou « 2 Rue Castellane 31000 TOULOUSE » dans le texte). Seulement si la page parle de ce restaurant ET de cette ville."""
    if any(d in url for d in PAGES_FERMEES):
        return None  # pages qui bloquent les robots ou se chargent en JavaScript
    try:
        brut = ouvrir_page_publique(url)
    except Exception:  # noqa : page injoignable, ou adresse non publique
        return None
    rue, cp = re.search(r'"streetAddress"\s*:\s*"([^"]{4,120})"', brut), re.search(r'"postalCode"\s*:\s*"([^"]{3,12})"', brut)
    texte = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", brut))))
    if not cite(nom, texte + " " + brut[:200000], flou=True) or (ville and nz(ville) not in nz(texte + " " + brut[:20000])):
        return None
    if rue and STREET.search(rue.group(1)):
        return f"{rue.group(1).strip()}, {cp.group(1) + ' ' if cp else ''}{ville or ''}".strip(" ,")
    for m in STREET.finditer(texte):
        fin = texte[m.start():m.start() + 120]
        if ville and nz(ville) in nz(fin) and (c := re.search(rf"(?i)^(.*?\b\d{{4,5}}\s+{re.escape(ville)})", fin)):
            return c.group(1).strip(" ,;")
    return None


def web_addresses(items, dire=None):
    """[{nom, ville, pays, handle}] -> [{adresse, source} ou None] dans le meme ordre. Seuls comptent les resultats qui citent le restaurant."""
    try:
        cache = json.loads(WEB_CACHE.read_text())
    except Exception:  # noqa
        cache = {}
    keys = [nz(it["nom"]) + "|" + nz(it.get("ville")) for it in items]
    for k in [k for k, v in cache.items() if isinstance(v, dict) and "vide" in v and time.time() - v["vide"] > 86400]:
        del cache[k]  # echec de plus d'un jour : on retentera
    out, hits, n = [cache.get(k) for k in keys], {}, 0
    out = [None if isinstance(o, dict) and "vide" in o else o for o in out]
    for i, it in enumerate(items):
        if keys[i] in cache or n >= WEB_MAX:
            continue
        reg = "fr-fr" if not it.get("pays") or nz(it["pays"]) in ("france", "fr") else "wt-wt"
        res, seen = [], set()
        nom_q = it["nom"].title() if it["nom"].isupper() else it["nom"]  # « HONORÉ » : 0 resultat en majuscules
        ville_q = (it.get("ville") or "").title() if (it.get("ville") or "").isupper() else (it.get("ville") or "")
        for q in [f'"{nom_q}" {ville_q} adresse', f"{nom_q} {ville_q} restaurant adresse"] + ([f"{it['handle']} {ville_q}"] if it.get("handle") else []):
            if any(STREET.search(r["extrait"]) for r in res) or n >= WEB_MAX:
                break  # deja une adresse dans les resultats : pas de requete suivante (sinon on essaie la suivante)
            n += 1
            for r in web_search(q.strip(), reg, dire):
                texte_r = r["titre"] + " " + r["extrait"] + " " + r["lien"]
                if r["lien"] not in seen and cite(it["nom"], texte_r, flou=it.get("voix")) and (not it.get("voix") or RESTO.search(texte_r)):
                    # le resultat parle bien de CE restaurant (et, pour un nom seulement entendu, d'un lieu ou l'on mange : pas d'un artisan homonyme)
                    seen.add(r["lien"]); res.append(r)
        hits[i] = sorted(res, key=lambda r: not STREET.search(r["extrait"]))[:6]  # les extraits qui donnent une adresse d'abord
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
        bon_nom = orthographe(items[i]["nom"], [r["titre"] for r in hits[i]]) if items[i].get("voix") else items[i]["nom"]
        found = next(({"adresse": adr, "source": hits[i][k - 1]["lien"]} for k, adr in llm.get(num, [])
                      if 1 <= k <= len(hits[i]) and addr_in(adr, hits[i][k - 1])), None)
        if not found:
            for r in hits[i]:
                if m := STREET.search(r["extrait"]):
                    found = {"adresse": re.split(r"(?<=\D)\.\s|\s[-–|·•]\s|\s*[:(]", m.group(0))[0].strip(" ,;."), "source": r["lien"]}
                    break
        if found and bon_nom != items[i]["nom"]:
            found["nom"] = bon_nom
        if not found:  # extraits sans adresse : on ouvre les pages elles-memes (pas un moteur de recherche : pas de blocage)
            for r in hits[i][:4]:
                if adr := adresse_page(r["lien"], items[i]["nom"], items[i].get("ville")):
                    trace("web_page", lien=r["lien"], adresse=adr)
                    found = {"adresse": adr, "source": r["lien"]}
                    break
        out[i] = found
    for i in hits:  # « rien trouve » : retenu une journee seulement (souvent un blocage passager des moteurs)
        cache[keys[i]] = out[i] or {"vide": time.time()}
    try:
        WEB_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
    except OSError:
        pass
    return out


def verify(lieux, villes, progress, source="legende"):
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
        noms = [*b.get("variantes", []), b["nom"]]  # « bouillon » entendu : « comme un bouillon » d'abord, le mot seul en dernier
        if b["nom"] and len(s := re.sub(r"(?i)^(?:le|la|les|l['’]|the|el|il)\s+", "", b["nom"])) >= 3 and s != b["nom"]:
            noms.append(s)  # « Le Hanbei » : OSM le connait sous « Hanbei »
        zs = [z for v in vs[:3] if (z := zone(v, b.get("pays") or ""))]  # villes que OpenStreetMap connait (« #vieuxport » : non)
        hit, addr, g = None, None, None
        if b.get("adresse"):
            addr, g = locate(b["adresse"], vs)
            if g and zs and not b.get("adresse_ecrite") and not any(abs(g["lat"] - z[0]) <= z[2] and abs(g["lon"] - z[1]) <= z[2] for z in zs):
                trace("adresse_ecartee", nom=b["nom"], adresse=addr, raison="hors de la ville citee")
                addr, g = None, None  # « pres d'Opera » localise a Nice : adresse fausse
            hit = next(filter(None, (osm_at(n, g) for n in noms)), None) if g else None
        if not hit and not g:  # pas d'adresse (ou fausse) : on cherche le restaurant dans toute la ville
            hit = next(filter(None, (osm(n, near=z) for n in noms for z in zs)), None) if zs else next(filter(None, (osm(n) for n in noms)), None)
        ville = vs[0][:1].upper() + vs[0][1:] if vs else None
        f = fiche(**hit, prix=b["prix"], source=source) if hit else at_address(b["nom"], addr, g, ville=ville, pays=b.get("pays"), prix=b["prix"], source=source)
        if b.get("horaires") and not f["horaires"]:
            f["horaires"] = b["horaires"]
        res.append(f)
        if not hit and not g and zs and b["nom"] and (b.get("_src") != "voix" or len(b["nom"].split()) > 1):  # ville connue, ni OSM ni adresse : recherche web
            # (sauf un mot entendu seul, trop vague pour une recherche)
            pending.append((len(res) - 1, b, vs, ville, zs[0]))
    if pending and os.environ.get("TP_WEB") != "0":
        progress("Recherche des adresses sur le web")
        found = web_addresses([{"nom": b["nom"], "ville": ville, "pays": b.get("pays"), "handle": b.get("handle"), "voix": b.get("_src") == "voix"}
                               for _, b, _, ville, _ in pending], progress)
        for (i, b, vs, ville, z), w in zip(pending, found):
            progress(f"  web : {b['nom']}  →  " + (f"adresse lue : {w['adresse']} ({urllib.parse.urlparse(w['source']).netloc.removeprefix('www.')})" if w else "pas d'adresse"))
            if not w:
                continue
            addr, g = locate(w["adresse"], vs)
            if not g or abs(g["lat"] - z[0]) > z[2] or abs(g["lon"] - z[1]) > z[2]:
                continue  # adresse introuvable, ou dans une autre ville : homonyme
            hit = osm_at(b["nom"], g)
            f = fiche(**hit, prix=b["prix"], source=source) if hit else at_address(b["nom"], addr, g, ville=ville, pays=b.get("pays"), prix=b["prix"], source=source)
            if not hit:
                f["confiance"] = "moyenne"  # adresse trouvee sur le web, restaurant absent d'OSM : « probable »
            if b.get("horaires") and not f["horaires"]:
                f["horaires"] = b["horaires"]
            f["source_web"] = w["source"]
            if w.get("nom") and not hit:  # « Ishin » entendu, « Isshin » sur les pages qui le confirment
                trace("orthographe", entendu=b["nom"], web=w["nom"])
                f["nom"] = w["nom"]
            res[i] = f
    return res


def essai(f, *a, **k):
    try:
        return f(*a, **k)
    except RuntimeError:  # IA indisponible : cette lecture ne donne rien, l'analyse continue
        return []


EN_PASSANT = re.compile(r"(?i)\b(?:autres?|anciens?|premiers?)\s+(?:restos?|restaurants?|adresses?|etablissements?|établissements?|bistrots?|tables?)\b"
                        r"|\b(?:proprietaire|propriétaire|createur|créateur|fondateur|chef)\s+(?:aussi\s+)?(?:de|du|des)\b|\bcomme\s+(?:chez|a|à)\b")


def en_passant(nom, texte):
    """« par le chef X, qui a deux autres restos que j'adore, la grotte et ... » : un nom cite dans la meme phrase, APRES une telle
    formule, n'est pas le lieu presente par la video."""
    cible, n = nz(nom), len(nom.split())

    def dit(bout):  # meme suite de mots, a une faute de transcription pres (pas « j'adore » pour « Doré »)
        w = [nz(x) for x in re.findall(r"[\wÀ-ÿ]+", bout)]
        return any(c[:1] == cible[:1] and difflib.SequenceMatcher(None, c, cible).ratio() >= 0.85
                   for k in (n, n + 1) for c in ("".join(w[i:i + k]) for i in range(len(w) - k + 1)))
    for phrase in re.split(r"(?<=[.!?])\s+", texte or ""):  # Whisper coupe les lignes en pleine phrase : on decoupe aux points
        m = EN_PASSANT.search(phrase)
        if m and dit(phrase[m.end():]) and not dit(phrase[:m.start()]):
            return True
    return False


# Transcription : "openrouter:<modele>" (Whisper heberge, ~0,0005 $ par video de 60 s ; repli sur Whisper local) ou "local"
TRANSCRIPTION = os.environ.get("TP_TRANSCRIPTION", "openrouter:openai/whisper-large-v3-turbo")  # le meme modele qu'en local, ~0,0007 $ / video
TRANSCRIRE_PROMPT = ("Transcris mot pour mot ce que disent les personnes dans cet audio, dans la langue parlee. Ecris les noms propres "
                     "(restaurants, rues, villes, personnes) avec leur orthographe la plus probable. Reponds uniquement avec la transcription, sans commentaire.")


MOTS_FR = {"le", "la", "les", "des", "du", "un", "une", "et", "est", "pour", "avec", "dans", "sur", "pas", "que", "qui", "tu", "on", "nous",
           "vous", "c'est", "adresse", "adresses", "resto", "meilleur", "meilleure", "ici", "chez", "à", "où", "manger", "trop", "au", "aux",
           "de", "en", "ce", "ça", "il", "elle", "ne", "se", "sa", "son", "ses", "plus", "très", "vient", "d'ouvrir", "mon", "ma", "mes", "ton", "ta"}


def langue_probable(texte):
    """« fr » si la legende est clairement en francais, sinon None (Whisper devine). Evite qu'une video en francais
    avec des noms japonais soit prise pour du norvegien et TRADUITE."""
    mots = re.findall(r"[a-zà-ÿ']+", (texte or "").lower())
    n = sum(m in MOTS_FR for m in mots)
    return "fr" if n >= 2 and n / len(mots) >= 0.08 else None


def transcrire_openrouter(wav, modele, langue=None):
    """L'audio (60 s max, 16 kHz mono) envoye a un modele OpenRouter qui sait ecouter -> texte. RuntimeError si echec."""
    mp3 = pathlib.Path(wav).with_suffix(".mp3")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-b:a", "48k", mp3])  # ~4 fois plus leger que le wav
    if "whisper" in modele:  # Whisper recoit le son tel quel (wav 16 kHz, ~2 Mo) : la compression mp3 lui fait perdre des noms
        audio = {"data": base64.b64encode(pathlib.Path(wav).read_bytes()).decode(), "format": "wav"}
    else:
        audio = {"data": base64.b64encode(mp3.read_bytes()).decode(), "format": "mp3"}
    if "whisper" in modele:  # vrai modele de transcription : point d'acces dedie, renvoie {"text": ...}
        url, corps = OR_URL.replace("/chat/completions", "/audio/transcriptions"), {"model": modele, "input_audio": audio, **({"language": langue} if langue else {})}
    else:  # modele « omni » qui ecoute : on lui demande la transcription dans une conversation
        url, corps = OR_URL, {"model": modele, "temperature": 0, "max_tokens": 3000, "reasoning": {"effort": "low"},
                              "messages": [{"role": "user", "content": [{"type": "text", "text": TRANSCRIRE_PROMPT}, {"type": "input_audio", "input_audio": audio}]}]}
    req = urllib.request.Request(url, data=json.dumps(corps).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {or_cle('transcription')}", "Content-Type": "application/json", "X-Title": "Travelpoint"})
    st = st_modele(modele); st["appels"] += 1
    try:
        rep, duree = or_envoyer(req)
    except urllib.error.HTTPError as e:
        corps_err = getattr(e, "corps", "")
        st["erreurs"] += 1; st["derniere_erreur"] = str(corps_err or e)[:150]
        raise RuntimeError(f"transcription OpenRouter : {e}")
    except Exception as e:  # noqa
        st["erreurs"] += 1; st["derniere_erreur"] = str(getattr(e, "corps", "") or e)[:150]
        raise RuntimeError(f"transcription OpenRouter : {e}")
    texte = (rep.get("text") or ((rep.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if not texte:
        st["vides"] += 1
        raise RuntimeError(f"transcription OpenRouter vide : {str(rep.get('error') or '')[:150]}")
    st["ok"] += 1; st["secondes"] += duree
    return texte


def transcription(wd, progress, langue=None):
    """Paroles de la video, gardees par modele (a.<modele>.txt) : changer de modele ne perd pas les transcriptions de l'autre.
    OpenRouter (TP_TRANSCRIPTION=openrouter:...) en panne : Whisper en local prend le relais."""
    essais = ([TRANSCRIPTION] if TRANSCRIPTION.startswith("openrouter:") and or_cle() and os.environ.get("TP_LLM") != "0" else []) + [WHISPER]
    for modele in essais:
        slug = re.sub(r"[^\w.-]+", "_", modele)
        f = wd / f"a.{slug}.txt"
        if not f.exists() and (wd / "a.txt").exists() and (wd / "a.modele").exists() and (wd / "a.modele").read_text().strip() == modele:
            f.write_text((wd / "a.txt").read_text())  # ancienne facon de ranger
        if f.exists():
            texte = f.read_text().strip()
        else:
            progress("Transcription de l'audio" + (f" ({modele.split(':', 1)[1]})" if modele.startswith("openrouter:") else ""))
            try:
                run(["ffmpeg", "-y", "-loglevel", "error", "-i", wd / "video.mp4", "-t", "60", "-vn", "-ac", "1", "-ar", "16000", wd / "a.wav"])
                if modele.startswith("openrouter:"):
                    texte = transcrire_openrouter(wd / "a.wav", modele.split(":", 1)[1], langue)
                else:
                    run([BIN / "mlx_whisper", wd / "a.wav", "--model", modele, "-f", "txt", "--output-dir", wd, *(["--language", langue] if langue else [])])
                    texte = (wd / "a.txt").read_text().strip()
            except RuntimeError as e:
                trace("transcription_echec", modele=modele, raison=str(e)[:300])
                continue  # modele suivant (Whisper en local)
            f.write_text(texte)
        trace("transcription", modele=modele, texte=texte[:600])
        (wd / "a.txt").write_text(texte); (wd / "a.modele").write_text(modele)
        return texte
    return ""  # pas d'audio exploitable : images seules


def video_stage(url, wd, cap, progress):
    if not (wd / "video.mp4").exists():
        progress("Telechargement de la video")
        ytdlp(url, "-f", "bv*[height<=480]+ba/b", "--merge-output-format", "mp4", "-o", str(wd / "video.%(ext)s"))
    speech = transcription(wd, progress, langue_probable(cap))
    fmt = ('Reponds uniquement en JSON : [{"nom":"","adresse":"","ville":"","pays":""}] ou [] si aucun nom. '
           '"adresse" : numero et rue s\'ils sont dits, ecrits a l\'ecran ou dans la description, sinon "".')
    ctx = f"DESCRIPTION : {cap[:500]}\nPAROLES : {speech[:1800]}\n\n"
    progress("Lecture des noms cites a l'oral")
    p1 = essai(ask, "Voici la transcription automatique d'une video (orthographe approximative, sans majuscules). Elle presente des adresses de "
             "restaurants, cafes, bars ou boulangeries (ignore monuments, plages, autres lieux et les personnes : chef, influenceur, invite). Releve le NOM de chaque etablissement que la video PRESENTE (celui ou l'on va, ou l'on mange, qui est teste ou recommande) ; ignore ceux cites en passant (autres restaurants du chef, comparaisons, souvenirs). "
             "meme s'il ressemble a une expression courante, en retrouvant toutes les adresses annoncees. N'invente rien. Ecris chaque nom comme il est ecrit ou dit, dans sa langue d'origine.\n" + ctx + fmt) if speech else []
    progress("Analyse des images")
    p2 = essai(ask, "Video de voyage.\n" + f"DESCRIPTION : {cap[:500]}\n\n" + "Liste les restaurants, cafes, bars ou boulangeries (pas les monuments ni les autres lieux) "
             "dont tu LIS le NOM a l'ecran (enseigne, devanture, menu, texte incruste). Le son n'est pas fourni : ne devine pas. N'invente rien. Ecris chaque nom comme il est ecrit (caracteres d'origine : japonais, coreen...).\n" + fmt,
             "--video", wd / "video.mp4", "--fps", "1", "--video-max-frames", "16")
    tag = lambda xs, s: [{**q, "_src": s} for q in xs if isinstance(q, dict)]
    return tag(p1, "voix") + tag(p2, "image"), speech  # voix : entendu ; image : lu a l'ecran


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
    est relue depuis le cache result.json : ni telechargement, ni Whisper, ni IA."""
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
    TRACE[0] = wd / "trace.jsonl"
    TRACE[0].write_text("")
    try:
        res = dedoublonne(_analyze(url, wd, progress))
        trace("resultat", fiches=[(f.get("nom"), f.get("confiance"), f.get("adresse")) for f in res])
    finally:
        TRACE[0] = None
    cache.write_text(json.dumps(res, ensure_ascii=False))
    return res


def dedoublonne(fiches):
    """Meme nom, meme endroit, meme adresse (ecrite dans le titre et la description) : une seule fiche. 4 adresses differentes : 4 fiches."""
    out, vus = [], set()
    for f in fiches:
        pos = (round(float(f["lat"]), 4), round(float(f["lon"]), 4)) if f.get("lat") not in (None, "") and f.get("lon") not in (None, "") else nz(f.get("adresse"))
        k = (nz(f.get("nom")), pos, nz(re.sub(r"\([^)]*\)", "", f.get("adresse") or "")))  # « ... Belgique (invitation-partenariat) » = « ... Belgique »
        if k not in vus:
            vus.add(k); out.append(f)
    return out


def en_cache(url):
    """Video deja analysee (par n'importe quel compte) : son resultat est relu sans IA ni telechargement."""
    return (DATA / "jobs" / hashlib.sha1(canon(url)[0].encode()).hexdigest()[:10] / "result.json").exists()


def forget(url):
    """Oublie le resultat en cache d'une video pour pouvoir la reanalyser (legende, video et transcription restent en cache)."""
    (DATA / "jobs" / hashlib.sha1(canon(url)[0].encode()).hexdigest()[:10] / "result.json").unlink(missing_ok=True)
    try:  # et les recherches web restees sans resultat : la reanalyse les retente
        cache = json.loads(WEB_CACHE.read_text())
        WEB_CACHE.write_text(json.dumps({k: v for k, v in cache.items() if not (v is None or (isinstance(v, dict) and "vide" in v))}, ensure_ascii=False))
    except Exception:  # noqa
        pass


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
    legende = []
    if items := llm_extract(cap, acc):
        legende = verify(items, p["villes"], progress)
        if any(f["confiance"] in ("haute", "moyenne") or f.get("lat") for f in legende):
            return legende
        # ni adresse, ni confirmation (« Camille Lavabre », la personne du titre) : la video tranche ; la legende ne sert que si elle ne donne rien
        trace("legende_faible", noms=[f["nom"] for f in legende])

    # 0 bis) secours sans IA : legende en blocs « 📍@restaurant » + ses offres
    if p["lieux"]:
        return verify([{**b, "adresse": a, "adresse_ecrite": True, "ville": None, "pays": None, "horaires": None} for b in p["lieux"] for a in (b["adresses"] or [None])],
                      p["villes"], progress)

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

    # 2) video : noms entendus (voix) et lus a l'ecran (image), verifies comme ceux de la legende (OpenStreetMap, puis web)
    spoken, speech = video_stage(url, wd, cap, progress)
    items, seen, img_addr = [], set(), {}
    for q in spoken:  # une meme adresse donnee a plusieurs noms lus a l'ecran : inventee (« 12 rue de la Paix » pour deux restaurants)
        if q.get("_src", "image") == "image" and q.get("adresse"):
            img_addr[nz(q["adresse"])] = img_addr.get(nz(q["adresse"]), 0) + 1
    lus = {nz(q.get("nom")) for q in spoken if q.get("_src") == "image" and q.get("nom")}
    for q in spoken:
        nom = (q.get("nom") or "").strip()
        for l in legende:  # « LES THERMES » lu sur l'enseigne, « Les Thermes Kongfu Pâtes » ecrit dans la legende : le nom complet
            if nom and l.get("nom") and nz(nom) != nz(l["nom"]) and nz(nom) in nz(l["nom"]):
                trace("nom_complete", lu=nom, legende=l["nom"])
                nom = l["nom"]
                break
        if not nom or nz(nom) in seen or nz(nom) in acc:  # jamais le nom du compte
            continue
        src = q.get("_src", "image")
        if src == "voix" and nz(nom) in lus:  # dit ET lu a l'ecran (« Honoré » / « HONORÉ ») : aussi sur qu'un nom lu
            src = "image"
        if src == "voix" and not ecrit(nom, speech + " " + cap):
            continue  # anti-invention : un nom entendu doit etre ecrit dans la transcription (ou la legende)
        if src == "voix" and en_passant(nom, speech):
            trace("ecarte", nom=nom, raison="cite en passant, pas presente par la video")
            continue
        if nom_descriptif(nom) or personne(nom, speech + " " + cap):
            trace("ecarte", nom=nom, raison="phrase d'accroche ou personne, pas un etablissement")
            continue
        seen.add(nz(nom))
        addr = (q.get("adresse") or "").strip()
        if addr and not (vraie_adresse(addr) and (ecrit(addr, speech + " " + cap) if src == "voix" else img_addr.get(nz(addr)) == 1)):
            trace("adresse_ecartee", nom=nom, adresse=addr, raison="vague, non dite, ou donnee a plusieurs noms")
            addr = ""
        addr = addr or (p["adresses"][0] if len(p["adresses"]) == 1 else "")
        items.append({"nom": nom, "adresse": addr or None, "ville": q.get("ville"), "pays": q.get("pays"), "prix": [], "horaires": None,
                      "_src": src, "variantes": variantes(nom, speech) if src == "voix" else []})
    for b, f in zip(items, verify(items, p["villes"], progress, source="video") if items else []):
        if b["_src"] == "voix" and f["confiance"] == "a_verifier" and not f.get("lat"):
            trace("ecarte", nom=b["nom"], raison="entendu seulement, sans confirmation ni position")
            continue  # seulement entendu, inconnu d'OSM et du web, sans adresse : ecarte (« Gardelion », « lieu historique »...)
        if f.get("adresse") and any(f["adresse"] == x.get("adresse") for x in res):
            continue  # deux noms pour le meme lieu
        res.append(f)
    if not res and legende:
        res = legende  # la video n'a rien donne : on garde les noms de la legende, « a verifier »
    if not res:  # aucun nom trouve, mais des adresses dans la legende : on les place quand meme
        for addr in p["adresses"]:
            addr, g = locate(addr, p["villes"])
            res.append(at_address(None, addr, g, horaires="; ".join(p["horaires"]) or None, prix=p["prix"], source="legende"))
    return res
