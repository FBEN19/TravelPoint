"""Travelpoint - serveur : comptes (admin / utilisateur), file d'analyse (un lien a la fois) + restaurants de chaque compte."""
import hashlib, hmac, json, os, pathlib, queue, re, secrets, sqlite3, threading, time, urllib.parse, uuid
from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator

import pipeline

DB = pipeline.DATA / "travelpoint.db"
HERE = pathlib.Path(__file__).parent
app = FastAPI(title="Travelpoint")
if not pipeline.or_cle():
    print("⚠️  Pas de cle OpenRouter (data/openrouter.key) : l'IA est desactivee, seules les regles simples analysent les legendes.")
todo: "queue.Queue[str]" = queue.Queue()

# Noms sous lesquels l'app repond (TP_HOTES=a,b pour en ajouter, par ex. l'adresse du Mac sur ton reseau)
HOTES = {"localhost", "127.0.0.1", "::1", "[::1]", *(h.strip().lower() for h in os.environ.get("TP_HOTES", "").split(",") if h.strip())}
COOKIE, SESSION_JOURS = "tp_session", 30


@app.middleware("http")
async def protection(request: Request, call_next):
    """1) Hote : refuse « rebinding DNS » (un site malveillant dont le nom pointe vers 127.0.0.1).
    2) Origine : une page d'un autre site ouverte dans ton navigateur ne peut pas modifier tes donnees.
    3) Les modifications doivent etre envoyees en JSON (le navigateur demande alors la permission, que l'app ne donne a personne)."""
    hote = (request.headers.get("host") or "").rsplit(":", 1)[0].lower() if not (request.headers.get("host") or "").startswith("[") \
        else (request.headers.get("host") or "").split("]")[0].lower() + "]"
    if hote not in HOTES:
        return JSONResponse({"detail": "Hote non autorise"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origine = request.headers.get("origin")
        if origine and (urllib.parse.urlparse(origine).hostname or "").lower() not in HOTES:
            return JSONResponse({"detail": "Origine non autorisee"}, status_code=403)
        if request.method in ("POST", "PATCH", "PUT") and "application/json" not in (request.headers.get("content-type") or "").lower():
            return JSONResponse({"detail": "Envoie les donnees en JSON"}, status_code=415)
    return await call_next(request)


def db():
    c = sqlite3.connect(DB, timeout=30); c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.executescript("""
    create table if not exists jobs(id text primary key, url text, status text, step text, error text, created real, key text, user_id text);
    create table if not exists places(id text primary key, job_id text, url text, data text, done integer default 0, note text default '', created real,
                                      manuel integer default 0, orig text, user_id text);
    create table if not exists users(id text primary key, email text unique not null, pseudo text unique not null, hash text not null,
                                     role text not null default 'user', created real);
    create table if not exists sessions(token text primary key, user_id text not null, expires real not null);
    create table if not exists echecs(cle text primary key, n integer, debut real);
    """)  # manuel = fiche corrigee/creee a la main ; orig = nom + adresse trouves par l'analyse (pour ne pas les re-ajouter)
    for table, col, ddl in (("places", "manuel", "integer default 0"), ("places", "orig", "text"), ("places", "user_id", "text"),
                            ("jobs", "key", "text"), ("jobs", "user_id", "text"), ("places", "partage_par", "text")):  # bases creees avant ces colonnes
        if col not in {r[1] for r in c.execute(f"pragma table_info({table})")}:
            c.execute(f"alter table {table} add column {col} {ddl}")
    for r in c.execute("select id, url from jobs where key is null").fetchall():
        c.execute("update jobs set key=? where id=?", (pipeline.canon(r["url"])[0], r["id"]))
    c.execute("update jobs set status='error', error='Interrompu (serveur relance)' where status in ('queued','running')")
    c.execute("delete from sessions where expires < ?", (time.time(),))


# ---------- comptes ----------
def hacher(mdp):
    """scrypt (bibliotheque standard) avec un sel aleatoire : le mot de passe lui-meme n'est jamais enregistre."""
    sel = secrets.token_bytes(16)
    return "scrypt$16384$8$1$" + sel.hex() + "$" + hashlib.scrypt(mdp.encode(), salt=sel, n=16384, r=8, p=1, dklen=32).hex()


def verifier(mdp, stocke):
    try:
        _, n, r, p, sel, h = stocke.split("$")
        calc = hashlib.scrypt(mdp.encode(), salt=bytes.fromhex(sel), n=int(n), r=int(r), p=int(p), dklen=32).hex()
        return hmac.compare_digest(calc, h)
    except (ValueError, TypeError):
        return False


HASH_FACTICE = hacher(secrets.token_hex(8))  # pour qu'un identifiant inconnu prenne autant de temps qu'un mauvais mot de passe


def jeton(t):
    return hashlib.sha256(t.encode()).hexdigest()  # en base, seulement l'empreinte : une base volee ne donne pas les sessions


def ouvrir_session(response, request, uid):
    t = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("insert into sessions(token,user_id,expires) values(?,?,?)", (jeton(t), uid, time.time() + SESSION_JOURS * 86400))
    response.set_cookie(COOKIE, t, max_age=SESSION_JOURS * 86400, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")


def compte_courant(request):
    t = request.cookies.get(COOKIE)
    if not t:
        return None
    with db() as c:
        r = c.execute("select u.* from sessions s join users u on u.id=s.user_id where s.token=? and s.expires>?", (jeton(t), time.time())).fetchone()
    return dict(r) if r else None


def connecte(request: Request):
    u = compte_courant(request)
    if not u:
        raise HTTPException(401, "Connecte-toi pour continuer.")
    return u


def admin(u=Depends(connecte)):
    if u["role"] != "admin":
        raise HTTPException(403, "Reserve aux administrateurs.")
    return u


ECHECS_MAX, ECHECS_FENETRE = 5, 15 * 60  # 5 essais rates par identifiant (et par adresse IP) -> 15 min de pause


def trop_d_echecs(cles):
    with db() as c:
        for k in cles:
            r = c.execute("select n, debut from echecs where cle=?", (k,)).fetchone()
            if r and r["n"] >= ECHECS_MAX and time.time() - r["debut"] < ECHECS_FENETRE:
                return True
    return False


def noter_echec(cles):
    with db() as c:
        for k in cles:
            r = c.execute("select n, debut from echecs where cle=?", (k,)).fetchone()
            if not r or time.time() - r["debut"] >= ECHECS_FENETRE:
                c.execute("insert or replace into echecs(cle,n,debut) values(?,1,?)", (k, time.time()))
            else:
                c.execute("update echecs set n=n+1 where cle=?", (k,))


class InscriptionIn(BaseModel):
    pseudo: str = Field(min_length=3, max_length=30)
    email: str = Field(min_length=5, max_length=200)
    mdp: str = Field(max_length=200)

    @field_validator("mdp")
    @classmethod
    def mdp_complexe(cls, v):
        """12 caracteres minimum, avec majuscules, minuscules, chiffres et caracteres speciaux."""
        manque = [nom for nom, ok in (("12 caracteres minimum", len(v) >= 12), ("une majuscule", re.search(r"[A-Z]", v)),
                                     ("une minuscule", re.search(r"[a-z]", v)), ("un chiffre", re.search(r"\d", v)),
                                     ("un caractere special", re.search(r"[^A-Za-z0-9]", v))) if not ok]
        if manque:
            raise ValueError("le mot de passe doit contenir " + ", ".join(manque))
        return v

    @field_validator("pseudo")
    @classmethod
    def pseudo_ok(cls, v):
        if not re.fullmatch(r"[\w.-]{3,30}", v.strip()):
            raise ValueError("lettres, chiffres, point, tiret ou _ seulement")
        return v.strip()

    @field_validator("email")
    @classmethod
    def email_ok(cls, v):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v.strip()):
            raise ValueError("adresse e-mail invalide")
        return v.strip().lower()


class ConnexionIn(BaseModel):
    identifiant: str = Field(min_length=1, max_length=200)  # e-mail ou pseudo
    mdp: str = Field(min_length=1, max_length=200)


class PartageIn(BaseModel):
    pseudo: str = Field(min_length=1, max_length=30)


class RoleIn(BaseModel):
    role: str = Field(pattern="^(admin|user)$")


@app.post("/api/inscription")
def inscription(body: InscriptionIn, request: Request, response: Response):
    uid = uuid.uuid4().hex[:12]
    with db() as c:
        if c.execute("select 1 from users where email=? or lower(pseudo)=lower(?)", (body.email, body.pseudo)).fetchone():
            raise HTTPException(409, "Ce pseudo ou cette adresse e-mail est deja utilise.")
        premier = not c.execute("select 1 from users").fetchone()
        c.execute("insert into users(id,email,pseudo,hash,role,created) values(?,?,?,?,?,?)",
                  (uid, body.email, body.pseudo, hacher(body.mdp), "admin" if premier else "user", time.time()))
        if premier:  # les restaurants enregistres avant les comptes reviennent au premier compte (l'admin)
            c.execute("update places set user_id=? where user_id is null", (uid,))
            c.execute("update jobs set user_id=? where user_id is null", (uid,))
    ouvrir_session(response, request, uid)
    return {"pseudo": body.pseudo, "role": "admin" if premier else "user"}


@app.post("/api/connexion")
def connexion(body: ConnexionIn, request: Request, response: Response):
    ident = body.identifiant.strip().lower()
    cles = ["id:" + ident, "ip:" + (request.client.host if request.client else "?")]
    if trop_d_echecs(cles):
        raise HTTPException(429, "Trop d'essais. Reessaie dans 15 minutes.")
    with db() as c:
        u = c.execute("select * from users where email=? or lower(pseudo)=?", (ident, ident)).fetchone()
    if not verifier(body.mdp, u["hash"] if u else HASH_FACTICE) or not u:
        noter_echec(cles)
        raise HTTPException(401, "Identifiant ou mot de passe incorrect.")
    ouvrir_session(response, request, u["id"])
    return {"pseudo": u["pseudo"], "role": u["role"]}


@app.post("/api/deconnexion")
def deconnexion(request: Request, response: Response):
    if t := request.cookies.get(COOKIE):
        with db() as c:
            c.execute("delete from sessions where token=?", (jeton(t),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/moi")
def moi(u=Depends(connecte)):
    return {"pseudo": u["pseudo"], "email": u["email"], "role": u["role"]}


@app.get("/api/admin/comptes")
def comptes(_=Depends(admin)):
    with db() as c:
        return [dict(r) for r in c.execute("select u.id, u.pseudo, u.email, u.role, u.created, "
                                           "(select count(*) from places p where p.user_id=u.id) as restaurants from users u order by u.created")]


def admins_restants(c, sauf):
    return c.execute("select count(*) from users where role='admin' and id<>?", (sauf,)).fetchone()[0]


@app.patch("/api/admin/comptes/{uid}")
def changer_role(uid: str, body: RoleIn, _=Depends(admin)):
    with db() as c:
        if not c.execute("select 1 from users where id=?", (uid,)).fetchone():
            raise HTTPException(404, "Compte introuvable")
        if body.role == "user" and not admins_restants(c, uid):
            raise HTTPException(400, "Il faut garder au moins un administrateur.")
        c.execute("update users set role=? where id=?", (body.role, uid))
    return {"ok": True}


@app.delete("/api/admin/comptes/{uid}")
def supprimer_compte(uid: str, _=Depends(admin)):
    with db() as c:
        u = c.execute("select role from users where id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "Compte introuvable")
        if u["role"] == "admin" and not admins_restants(c, uid):
            raise HTTPException(400, "Il faut garder au moins un administrateur.")
        for t in ("places", "jobs", "sessions"):
            c.execute(f"delete from {t} where user_id=?", (uid,))
        c.execute("delete from users where id=?", (uid,))
    return {"ok": True}


# ---------- analyses ----------
def set_job(jid, **kw):
    with db() as c:
        c.execute(f"update jobs set {', '.join(k + '=?' for k in kw)} where id=?", (*kw.values(), jid))


def worker():
    last_ig = 0.0
    while True:
        jid = todo.get()
        with db() as c:
            row = c.execute("select url, user_id from jobs where id=?", (jid,)).fetchone()
        if not row:  # analyse supprimee entre-temps : on passe, sans faire tomber le serveur
            continue
        url, uid = row["url"], row["user_id"]
        set_job(jid, status="running", step="Demarrage", error=None)
        try:
            frais = "instagram.com" in url and not pipeline.en_cache(url)  # deja analysee (par toi ou un autre compte) : pas d'Instagram
            if frais and time.time() - last_ig < 15:  # evite le blocage anti-bots
                time.sleep(15 - (time.time() - last_ig))
            places = pipeline.analyze(url, lambda s: set_job(jid, step=s))
            if frais:
                last_ig = time.time()  # la pause de 15 s ne concerne que deux liens Instagram a la suite
            with db() as c:  # les fiches modifiees a la main restent ; on n'ajoute pas leur version automatique en double
                keep = "coalesce(manuel,0)=1 or coalesce(done,0)=1 or coalesce(note,'')<>''"  # corrigee a la main, deja testee ou annotee : on la garde
                kept = [json.loads(j) for r in c.execute(f"select data, orig from places where job_id=? and ({keep})", (jid,)) for j in (r["data"], r["orig"] or "{}")]
                c.execute(f"delete from places where job_id=? and not ({keep})", (jid,))
                places = [p for p in places if not any(pipeline.same(p, k) for k in kept)]
                for p in places:
                    c.execute("insert into places(id,job_id,url,data,created,user_id) values(?,?,?,?,?,?)",
                              (uuid.uuid4().hex[:10], jid, url, json.dumps(p, ensure_ascii=False), time.time(), uid))
            set_job(jid, status="done", step=f"{len(places)} lieu(x)" if places else "Aucun restaurant trouve")
        except Exception as e:  # noqa
            set_job(jid, status="error", error=str(e)[:400])


threading.Thread(target=worker, daemon=True).start()


class AnalyzeIn(BaseModel):
    url: str = Field(max_length=2000)
    force: bool = False  # reanalyser une video deja vue (sinon : jamais relancee)


def Txt(n):
    return Field(None, max_length=n)


class Fiche(BaseModel):  # champs modifiables a la main ; "" = vide
    nom: str | None = Txt(200)
    adresse: str | None = Txt(300)
    ville: str | None = Txt(120)
    pays: str | None = Txt(120)
    cuisine: str | None = Txt(200)
    horaires: str | None = Txt(500)
    prix: list[Annotated[str, StringConstraints(max_length=200)]] | None = Field(None, max_length=30)
    site: str | None = Txt(500)
    tel: str | None = Txt(40)
    lat: float | None = Field(None, ge=-90, le=90, allow_inf_nan=False)
    lon: float | None = Field(None, ge=-180, le=180, allow_inf_nan=False)

    @field_validator("site")
    @classmethod
    def http_only(cls, v):
        if v and not re.fullmatch(r"https?://[^\s<>\"']+", v.strip(), re.I):
            raise ValueError("le site doit commencer par http:// ou https://")
        return v


class PlaceIn(Fiche):
    done: bool | None = None
    note: str | None = Field(None, max_length=2000)
    verifie: bool | None = None  # "C'est bon" : confiance -> manuel


class GeoIn(BaseModel):
    adresse: str = Field(min_length=3, max_length=300)


def edits(body):
    """Champs de la fiche effectivement envoyes, nettoyes ('' -> None)."""
    out = {}
    for k in Fiche.model_fields.keys() & body.model_fields_set:
        v = getattr(body, k)
        out[k] = [x.strip() for x in v or [] if x.strip()] if k == "prix" else (v.strip() or None) if isinstance(v, str) else v
    return out


def check_pos(d):
    if (d.get("lat") is None) != (d.get("lon") is None):
        raise HTTPException(400, "Latitude et longitude vont ensemble (remplis les deux, ou aucune).")


@app.post("/api/analyze")
def analyze(body: AnalyzeIn, u=Depends(connecte)):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Colle un lien complet (https://...)")
    if not pipeline.lien_video_accepte(url):
        raise HTTPException(400, "Seuls les liens YouTube, Instagram et TikTok sont acceptes.")
    key, url = pipeline.canon(url)  # meme video = meme cle, quels que soient ?si=, utm_, www, /shorts/ ou /watch...
    with db() as c:  # chaque compte a ses analyses ; une video deja analysee par un autre est relue depuis le cache (sans IA)
        old = c.execute("select * from jobs where key=? and user_id=? order by created desc limit 1", (key, u["id"])).fetchone()
        if old and old["status"] in ("queued", "running", "done") and not (body.force and old["status"] == "done"):  # deja vue : on ne relance ni le site ni les IA
            lieux = [r["id"] for r in c.execute("select id from places where job_id=? order by created", (old["id"],))]
            return {"id": old["id"], "deja": True, "statut": old["status"], "lieux": lieux}
        if old and body.force:
            pipeline.forget(url)
        jid = old["id"] if old else uuid.uuid4().hex[:10]
        if old:
            c.execute("update jobs set status='queued', step='En attente', error=null where id=?", (jid,))
        else:
            c.execute("insert into jobs(id,url,status,step,error,created,key,user_id) values(?,?,?,?,?,?,?,?)",
                      (jid, url, "queued", "En attente", None, time.time(), key, u["id"]))
    todo.put(jid)
    return {"id": jid, "deja": False}


@app.get("/api/jobs")
def jobs(u=Depends(connecte)):
    with db() as c:
        return [dict(r) for r in c.execute("select id, url, status, step, error, created, key from jobs where user_id=? order by created desc limit 30", (u["id"],))]


@app.delete("/api/jobs/{jid}")
def del_job(jid: str, u=Depends(connecte)):
    with db() as c:
        c.execute("delete from jobs where id=? and user_id=? and status='error'", (jid, u["id"]))  # une analyse terminee reste en memoire : la video ne sera pas relancee
    return {"ok": True}


@app.get("/api/places")
def places(u=Depends(connecte)):
    with db() as c:
        rows = c.execute("select * from places where user_id=? order by created desc", (u["id"],)).fetchall()
    return [{"id": r["id"], "url": r["url"], "done": bool(r["done"]), "note": r["note"], "modifie_manuellement": bool(r["manuel"]),
             "partage_par": r["partage_par"], **json.loads(r["data"])} for r in rows]


@app.get("/api/utilisateurs")
def utilisateurs(q: str = "", u=Depends(connecte)):
    """Pseudos des autres comptes (jamais leur e-mail), pour choisir a qui partager une fiche. q : debut ou morceau du pseudo."""
    motif = "%" + re.sub(r"([%_\\])", r"\\\1", q.strip().lstrip("@")[:30]) + "%"
    with db() as c:
        return [r["pseudo"] for r in c.execute("select pseudo from users where id<>? and pseudo like ? escape '\\' "
                                               "order by lower(pseudo) like lower(?) desc, lower(pseudo) limit 50",
                                               (u["id"], motif, q.strip().lstrip("@") + "%"))]


@app.post("/api/places/{pid}/partager")
def partager(pid: str, body: PartageIn, u=Depends(connecte)):
    """Envoie une COPIE de la fiche a un autre compte (par son pseudo) : chacun garde ensuite la sienne (notes, « J'y suis allé »).
    Tes notes perso ne sont jamais partagees."""
    with db() as c:
        row = c.execute("select * from places where id=? and user_id=?", (pid, u["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Restaurant introuvable")
        dest = c.execute("select id, pseudo from users where lower(pseudo)=lower(?)", (body.pseudo.strip().lstrip("@"),)).fetchone()
        if not dest:
            raise HTTPException(404, f"Aucun compte avec le pseudo « {body.pseudo.strip()} ».")
        if dest["id"] == u["id"]:
            raise HTTPException(400, "C'est deja dans ta liste.")
        data = json.loads(row["data"])
        if any(pipeline.same(data, json.loads(r["data"])) for r in c.execute("select data from places where user_id=?", (dest["id"],))):
            raise HTTPException(409, f"{dest['pseudo']} a deja ce restaurant.")
        c.execute("insert into places(id,job_id,url,data,created,manuel,user_id,partage_par) values(?,?,?,?,?,?,?,?)",
                  (uuid.uuid4().hex[:10], None, row["url"], row["data"], time.time(), row["manuel"], dest["id"], u["pseudo"]))
    return {"ok": True, "pseudo": dest["pseudo"]}


@app.post("/api/places")
def add_place(body: Fiche, u=Depends(connecte)):
    f = edits(body); check_pos(f)
    pid = uuid.uuid4().hex[:10]
    with db() as c:
        c.execute("insert into places(id,job_id,url,data,created,manuel,user_id) values(?,?,?,?,?,1,?)",
                  (pid, None, "", json.dumps(pipeline.fiche(**f, confiance="manuel", source="manuel"), ensure_ascii=False), time.time(), u["id"]))
    return {"id": pid}


@app.patch("/api/places/{pid}")
def patch_place(pid: str, body: PlaceIn, u=Depends(connecte)):
    f = edits(body)
    with db() as c:
        row = c.execute("select * from places where id=? and user_id=?", (pid, u["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Restaurant introuvable")
        data = json.loads(row["data"])
        if f or body.verifie:  # correction manuelle : la fiche ne sera plus jamais ecrasee par une reanalyse
            orig = row["orig"] or json.dumps({k: data.get(k) for k in ("nom", "adresse")}, ensure_ascii=False)
            data.update(f, confiance="manuel"); check_pos(data)
            c.execute("update places set data=?, manuel=1, orig=? where id=?", (json.dumps(data, ensure_ascii=False), orig, pid))
        if body.done is not None:
            c.execute("update places set done=? where id=?", (int(body.done), pid))
        if body.note is not None:
            c.execute("update places set note=? where id=?", (body.note[:2000], pid))
    return {"ok": True}


@app.post("/api/geocode")
def geo(body: GeoIn, _=Depends(connecte)):
    g = pipeline.geocode(body.adresse.strip())  # meme limiteur Nominatim (1 requete/s) que l'analyse
    if not g:
        raise HTTPException(404, "Adresse introuvable sur OpenStreetMap. Essaie avec la ville et le pays, ou place le point sur la carte.")
    return g


@app.delete("/api/places/{pid}")
def del_place(pid: str, u=Depends(connecte)):
    with db() as c:
        c.execute("delete from places where id=? and user_id=?", (pid, u["id"]))
    return {"ok": True}


# ---------- pages ----------
@app.get("/")
def accueil():
    """Page d'accueil qui presente le projet ; son bouton « Lancer Travelpoint » mene a /app."""
    return FileResponse(HERE / "static" / "index.html")


@app.get("/connexion")
def page_connexion():
    return FileResponse(HERE / "static" / "connexion.html")


@app.get("/app")
def application(request: Request):
    if not compte_courant(request):
        return RedirectResponse("/connexion?suite=/app", status_code=303)
    return FileResponse(HERE / "static" / "app.html")


@app.get("/autour")
def page_autour(request: Request):
    """Autour de moi : tes restaurants enregistres, du plus proche au plus loin (position du navigateur)."""
    if not compte_courant(request):
        return RedirectResponse("/connexion?suite=/autour", status_code=303)
    return FileResponse(HERE / "static" / "autour.html")


@app.get("/admin")
def page_admin(request: Request):
    u = compte_courant(request)
    if not u:
        return RedirectResponse("/connexion?suite=/admin", status_code=303)
    if u["role"] != "admin":
        return RedirectResponse("/app", status_code=303)
    return FileResponse(HERE / "static" / "admin.html")
