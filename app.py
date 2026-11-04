"""Travelpoint - serveur : file d'analyse (un lien a la fois, pour la RAM) + lieux enregistres."""
import json, pathlib, queue, re, sqlite3, threading, time, uuid
from typing import Annotated
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator

import pipeline

DB = pipeline.DATA / "travelpoint.db"
HERE = pathlib.Path(__file__).parent
app = FastAPI(title="Travelpoint")
if not pipeline.or_cle():
    print("⚠️  Pas de cle OpenRouter (data/openrouter.key) : l'IA est desactivee, seules les regles simples analysent les legendes.")
todo: "queue.Queue[str]" = queue.Queue()


def db():
    c = sqlite3.connect(DB, timeout=30); c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.executescript("""
    create table if not exists jobs(id text primary key, url text, status text, step text, error text, created real, key text);
    create table if not exists places(id text primary key, job_id text, url text, data text, done integer default 0, note text default '', created real,
                                      manuel integer default 0, orig text);
    """)  # manuel = fiche corrigee/creee a la main ; orig = nom + adresse trouves par l'analyse (pour ne pas les re-ajouter)
    for col, ddl in (("manuel", "integer default 0"), ("orig", "text")):  # bases creees avant la modification manuelle
        if col not in {r[1] for r in c.execute("pragma table_info(places)")}:
            c.execute(f"alter table places add column {col} {ddl}")
    if "key" not in {r[1] for r in c.execute("pragma table_info(jobs)")}:  # bases creees avant la detection par identifiant
        c.execute("alter table jobs add column key text")
    for r in c.execute("select id, url from jobs where key is null").fetchall():
        c.execute("update jobs set key=? where id=?", (pipeline.canon(r["url"])[0], r["id"]))
    c.execute("update jobs set status='error', error='Interrompu (serveur relance)' where status in ('queued','running')")


def set_job(jid, **kw):
    with db() as c:
        c.execute(f"update jobs set {', '.join(k + '=?' for k in kw)} where id=?", (*kw.values(), jid))


def worker():
    last_ig = 0.0
    while True:
        jid = todo.get()
        with db() as c:
            row = c.execute("select url from jobs where id=?", (jid,)).fetchone()
        if not row:  # analyse supprimee entre-temps : on passe, sans faire tomber le serveur
            continue
        url = row["url"]
        set_job(jid, status="running", step="Demarrage", error=None)
        try:
            if "instagram.com" in url and time.time() - last_ig < 15:  # evite le blocage anti-bots
                time.sleep(15 - (time.time() - last_ig))
            places = pipeline.analyze(url, lambda s: set_job(jid, step=s))
            if "instagram.com" in url:
                last_ig = time.time()  # la pause de 15 s ne concerne que deux liens Instagram a la suite
            with db() as c:  # les fiches modifiees a la main restent ; on n'ajoute pas leur version automatique en double
                keep = "coalesce(manuel,0)=1 or coalesce(done,0)=1 or coalesce(note,'')<>''"  # corrigee a la main, deja testee ou annotee : on la garde
                kept = [json.loads(j) for r in c.execute(f"select data, orig from places where job_id=? and ({keep})", (jid,)) for j in (r["data"], r["orig"] or "{}")]
                c.execute(f"delete from places where job_id=? and not ({keep})", (jid,))
                places = [p for p in places if not any(pipeline.same(p, k) for k in kept)]
                for p in places:
                    c.execute("insert into places(id,job_id,url,data,created) values(?,?,?,?,?)",
                              (uuid.uuid4().hex[:10], jid, url, json.dumps(p, ensure_ascii=False), time.time()))
            set_job(jid, status="done", step=f"{len(places)} lieu(x)" if places else "Aucun restaurant trouve")
        except Exception as e:  # noqa
            set_job(jid, status="error", error=str(e)[:400])


threading.Thread(target=worker, daemon=True).start()


class AnalyzeIn(BaseModel):
    url: str
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
    note: str | None = None
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
def analyze(body: AnalyzeIn):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Colle un lien complet (https://...)")
    key, url = pipeline.canon(url)  # meme video = meme cle, quels que soient ?si=, utm_, www, /shorts/ ou /watch...
    with db() as c:
        old = c.execute("select * from jobs where key=? order by created desc limit 1", (key,)).fetchone()
        if old and old["status"] in ("queued", "running", "done") and not (body.force and old["status"] == "done"):  # deja vue : on ne relance ni le site ni les IA
            lieux = [r["id"] for r in c.execute("select id from places where job_id=? order by created", (old["id"],))]
            return {"id": old["id"], "deja": True, "statut": old["status"], "lieux": lieux}
        if old and body.force:
            pipeline.forget(url)
        jid = old["id"] if old else uuid.uuid4().hex[:10]
        if old:
            c.execute("update jobs set status='queued', step='En attente', error=null where id=?", (jid,))
        else:
            c.execute("insert into jobs(id,url,status,step,error,created,key) values(?,?,?,?,?,?,?)", (jid, url, "queued", "En attente", None, time.time(), key))
    todo.put(jid)
    return {"id": jid, "deja": False}


@app.get("/api/jobs")
def jobs():
    with db() as c:
        return [dict(r) for r in c.execute("select * from jobs order by created desc limit 30")]


@app.delete("/api/jobs/{jid}")
def del_job(jid: str):
    with db() as c:
        c.execute("delete from jobs where id=? and status='error'", (jid,))  # une analyse terminee reste en memoire : la video ne sera pas relancee
    return {"ok": True}


@app.get("/api/places")
def places():
    with db() as c:
        rows = c.execute("select * from places order by created desc").fetchall()
    return [{"id": r["id"], "url": r["url"], "done": bool(r["done"]), "note": r["note"], "modifie_manuellement": bool(r["manuel"]),
             **json.loads(r["data"])} for r in rows]


@app.post("/api/places")
def add_place(body: Fiche):
    f = edits(body); check_pos(f)
    pid = uuid.uuid4().hex[:10]
    with db() as c:
        c.execute("insert into places(id,job_id,url,data,created,manuel) values(?,?,?,?,?,1)",
                  (pid, None, "", json.dumps(pipeline.fiche(**f, confiance="manuel", source="manuel"), ensure_ascii=False), time.time()))
    return {"id": pid}


@app.patch("/api/places/{pid}")
def patch_place(pid: str, body: PlaceIn):
    f = edits(body)
    with db() as c:
        row = c.execute("select * from places where id=?", (pid,)).fetchone()
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
def geo(body: GeoIn):
    g = pipeline.geocode(body.adresse.strip())  # meme limiteur Nominatim (1 requete/s) que l'analyse
    if not g:
        raise HTTPException(404, "Adresse introuvable sur OpenStreetMap. Essaie avec la ville et le pays, ou place le point sur la carte.")
    return g


@app.delete("/api/places/{pid}")
def del_place(pid: str):
    with db() as c:
        c.execute("delete from places where id=?", (pid,))
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")
