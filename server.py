from fastapi import FastAPI, APIRouter, HTTPException, Query, Depends, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Any
import uuid
from datetime import datetime, timezone


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Support both local .env and Railway environment variables
mongo_url = os.environ.get('MONGO_URL') or os.environ.get('MONGODB_URL')
if not mongo_url:
    raise ValueError("MONGO_URL or MONGODB_URL environment variable is required")
client = AsyncIOMotorClient(mongo_url, tls=True, tlsAllowInvalidCertificates=True)
db_name = os.environ.get('DB_NAME', 'conteggi_personali')
db = client[db_name]

app = FastAPI()
app.state.db = db
api_router = APIRouter(prefix="/api")

# Auth helpers (must be after db is set)
from auth import (
    UserCreate,
    UserPublic,
    LoginBody,
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
    require_admin,
    serialize_user,
    seed_admin,
)


# -------- Models --------
ALLOWED_STATI = {"maturato", "da_maturare"}
ALLOWED_PAGAMENTI = {"assegno", "bonifico", "finanziaria", "contanti"}
ALLOWED_FLAGS = {"active", "sospeso", "recesso"}


class Rata(BaseModel):
    date: str = ""
    paid: bool = False
    paid_at: Optional[str] = None
    amount: float = 0.0


class PersonaCreate(BaseModel):
    nome: str
    color: Optional[str] = "#2563EB"


class Persona(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    nome: str
    color: str = "#2563EB"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CalculationCreate(BaseModel):
    persona_id: Optional[str] = ""
    nome_cliente: Optional[str] = ""
    data: Optional[str] = ""
    note: Optional[str] = ""
    totale_cliente: float
    iva_percentuale: float
    permuta: float = 0.0
    provvigione_percentuale: float
    stato: Optional[str] = "maturato"
    tipo_pagamento: Optional[str] = "contanti"
    acconto: Optional[float] = 0.0
    liquidata: Optional[bool] = False
    flag: Optional[str] = "active"
    contratto_con: Optional[str] = ""
    provvigione_split: Optional[float] = 1.0
    partner_persona_id: Optional[str] = ""
    mirror_calc_id: Optional[str] = ""
    # Accept either list[str] (legacy) or list[{date,paid,paid_at}]
    date_pagamenti: Optional[Any] = None


class Calculation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    persona_id: str = ""
    nome_cliente: str = ""
    data: str = ""
    note: str = ""
    totale_cliente: float
    iva_percentuale: float
    permuta: float
    provvigione_percentuale: float
    imponibile: float
    base_provvigione: float
    provvigione: float
    stato: str = "maturato"
    tipo_pagamento: str = "contanti"
    acconto: float = 0.0
    liquidata: bool = False
    flag: str = "active"
    contratto_con: str = ""
    provvigione_split: float = 1.0
    partner_persona_id: str = ""
    mirror_calc_id: str = ""
    date_pagamenti: List[Rata] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def compute(payload: CalculationCreate) -> dict:
    iva = payload.iva_percentuale or 0.0
    imponibile = payload.totale_cliente / (1 + iva / 100.0)
    base_provvigione = imponibile - (payload.permuta or 0.0)
    split = payload.provvigione_split if payload.provvigione_split and payload.provvigione_split > 0 else 1.0
    provvigione = base_provvigione * (payload.provvigione_percentuale / 100.0) * split
    return {
        "imponibile": round(imponibile, 2),
        "base_provvigione": round(base_provvigione, 2),
        "provvigione": round(provvigione, 2),
    }


def normalize_rate(raw: Any) -> List[dict]:
    if not raw:
        return []
    out: List[dict] = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                out.append({"date": item.strip(), "paid": False, "paid_at": None, "amount": 0.0})
        elif isinstance(item, dict):
            d = (item.get("date") or "").strip()
            if not d:
                continue
            paid = bool(item.get("paid", False))
            paid_at = item.get("paid_at") or None
            try:
                amount = float(item.get("amount") or 0.0)
            except Exception:
                amount = 0.0
            out.append({"date": d, "paid": paid, "paid_at": paid_at, "amount": amount})
    return out


def normalize_payload(payload: CalculationCreate) -> dict:
    stato = (payload.stato or "maturato").lower()
    if stato not in ALLOWED_STATI:
        stato = "maturato"
    tipo = (payload.tipo_pagamento or "contanti").lower()
    if tipo not in ALLOWED_PAGAMENTI:
        tipo = "contanti"
    rate = normalize_rate(payload.date_pagamenti)
    if tipo not in ("assegno", "bonifico"):
        rate = []
    liquidata = bool(payload.liquidata or False)
    # auto promote ONLY when liquidata flag is set (Finanziaria).
    # For assegno/bonifico we keep stato='da_maturare' so partial maturazione
    # is computed per-rata (paid amounts → maturati, unpaid → da maturare).
    if liquidata:
        stato = "maturato"
    acconto = float(payload.acconto or 0.0)
    if tipo != "finanziaria":
        acconto = 0.0
    flag = (payload.flag or "active").lower()
    if flag not in ALLOWED_FLAGS:
        flag = "active"
    contratto_con = (payload.contratto_con or "").strip()
    try:
        split = float(payload.provvigione_split or 1.0)
    except Exception:
        split = 1.0
    if split <= 0 or split > 1:
        split = 1.0
    # If a partner name is provided, force 50% split unless caller explicitly set a smaller share
    if contratto_con and split == 1.0:
        split = 0.5
    return {
        "persona_id": payload.persona_id or "",
        "nome_cliente": payload.nome_cliente or "",
        "data": payload.data or "",
        "note": payload.note or "",
        "totale_cliente": payload.totale_cliente,
        "iva_percentuale": payload.iva_percentuale,
        "permuta": payload.permuta or 0.0,
        "provvigione_percentuale": payload.provvigione_percentuale,
        "stato": stato,
        "tipo_pagamento": tipo,
        "acconto": acconto,
        "liquidata": liquidata,
        "flag": flag,
        "contratto_con": contratto_con,
        "provvigione_split": split,
        "partner_persona_id": payload.partner_persona_id or "",
        "mirror_calc_id": payload.mirror_calc_id or "",
        "date_pagamenti": rate,
    }


def hydrate_calc(d: dict) -> dict:
    """Backfill defaults and normalize date_pagamenti for older docs."""
    d.setdefault("persona_id", "")
    d.setdefault("stato", "maturato")
    d.setdefault("tipo_pagamento", "contanti")
    d.setdefault("acconto", 0.0)
    d.setdefault("liquidata", False)
    d.setdefault("flag", "active")
    d.setdefault("contratto_con", "")
    d.setdefault("provvigione_split", 1.0)
    d.setdefault("partner_persona_id", "")
    d.setdefault("mirror_calc_id", "")
    d["date_pagamenti"] = normalize_rate(d.get("date_pagamenti"))
    return d


def viewer_persona(current: dict, requested: Optional[str]) -> Optional[str]:
    """If user is viewer, force the persona filter to their assigned persona."""
    if (current or {}).get("role") == "viewer":
        return current.get("assigned_persona_id") or "__none__"
    return requested


# -------- Persone --------
# Single-user mode: only ONE persona is allowed in the app. Creation is blocked
# (auto-seeded at startup); update/delete remain enabled for renaming the
# default persona.


@api_router.post("/persone", response_model=Persona)
async def create_persona(payload: PersonaCreate, _: dict = Depends(require_admin)):
    count = await db.persone.count_documents({})
    if count >= 1:
        raise HTTPException(
            status_code=403,
            detail="Modalità mono-utente: è permessa una sola persona. Rinomina quella esistente.",
        )
    obj = Persona(nome=payload.nome.strip() or "Senza nome", color=payload.color or "#2563EB")
    await db.persone.insert_one(obj.dict())
    return obj


@api_router.get("/persone", response_model=List[Persona])
async def list_persone(current: dict = Depends(get_current_user)):
    docs = await db.persone.find({}, {"_id": 0}).sort("created_at", 1).to_list(500)
    if (current or {}).get("role") == "viewer":
        pid = current.get("assigned_persona_id") or ""
        docs = [d for d in docs if d.get("id") == pid]
    return [Persona(**d) for d in docs]


@api_router.put("/persone/{persona_id}", response_model=Persona)
async def update_persona(persona_id: str, payload: PersonaCreate, _: dict = Depends(require_admin)):
    existing = await db.persone.find_one({"id": persona_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Persona non trovata")
    update = {"nome": payload.nome.strip() or "Senza nome", "color": payload.color or "#2563EB"}
    await db.persone.update_one({"id": persona_id}, {"$set": update})
    doc = await db.persone.find_one({"id": persona_id}, {"_id": 0})
    return Persona(**doc)


@api_router.delete("/persone/{persona_id}")
async def delete_persona(persona_id: str, _: dict = Depends(require_admin)):
    res = await db.persone.delete_one({"id": persona_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Persona non trovata")
    # Optional: also remove association on related calcs (keep them but clear persona_id)
    await db.calculations.update_many({"persona_id": persona_id}, {"$set": {"persona_id": ""}})
    return {"deleted": True, "id": persona_id}


# -------- Calcoli --------
@api_router.get("/")
async def root():
    return {"message": "Conteggi personali API"}


PRIVACY_POLICY_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Privacy Policy - Conteggi personali</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         max-width: 720px; margin: 32px auto; padding: 0 20px; color: #0F172A;
         line-height: 1.55; background: #F8FAFC; }
  h1 { color: #2563EB; border-bottom: 2px solid #E2E8F0; padding-bottom: 8px; }
  h2 { color: #0F172A; margin-top: 24px; }
  p { margin: 10px 0; }
  ul { padding-left: 20px; }
  .meta { color: #64748B; font-size: 13px; margin-bottom: 24px; }
  .card { background: #FFFFFF; padding: 20px; border-radius: 12px;
          border: 1px solid #E2E8F0; }
  a { color: #2563EB; }
  .footer { margin-top: 32px; padding-top: 16px; border-top: 1px solid #E2E8F0;
            font-size: 12px; color: #94A3B8; text-align: center; }
</style>
</head>
<body>
<div class="card">
<h1>Privacy Policy</h1>
<p class="meta">Ultimo aggiornamento: Agosto 2025 — App: <strong>Conteggi personali</strong></p>

<h2>1. Premessa</h2>
<p>L'app <strong>Conteggi personali</strong> è un'applicazione ad uso esclusivamente
personale del proprietario, destinata al calcolo e alla gestione di provvigioni,
incassi e scadenze. Non è un servizio pubblico né distribuito a terzi.</p>

<h2>2. Dati raccolti</h2>
<p>L'app gestisce esclusivamente dati inseriti manualmente dall'utente proprietario:</p>
<ul>
  <li>Nomi clienti, importi e date inserite a fini di calcolo provvigioni</li>
  <li>Incassi e pagamenti registrati</li>
  <li>Credenziali di accesso (email e password) memorizzate in forma crittografata
      (hash bcrypt) sul server privato dell'utente</li>
</ul>
<p>L'app <strong>non raccoglie</strong> dati di geolocalizzazione, contatti,
fotografie, identificatori pubblicitari o dati comportamentali.</p>

<h2>3. Permessi richiesti</h2>
<ul>
  <li><strong>Calendario / Promemoria</strong>: per aggiungere opzionalmente le scadenze
      delle rate al calendario dell'utente. L'utilizzo è facoltativo e attivabile
      manualmente.</li>
</ul>

<h2>4. Condivisione con terzi</h2>
<p>I dati <strong>non vengono condivisi</strong> con terze parti, non sono utilizzati per
finalità commerciali, non sono venduti né ceduti. L'app non integra SDK pubblicitari,
di tracciamento o di analytics.</p>

<h2>5. Conservazione</h2>
<p>I dati sono conservati esclusivamente sul server privato del proprietario
dell'app e possono essere cancellati in qualsiasi momento eliminando i record
direttamente dall'interfaccia dell'app.</p>

<h2>6. Sicurezza</h2>
<p>L'accesso all'app è protetto da autenticazione con password. Le password sono
memorizzate utilizzando algoritmo di hashing bcrypt; le comunicazioni con il server
avvengono in HTTPS.</p>

<h2>7. Diritti dell'utente</h2>
<p>Essendo un'app personale, l'unico utente è il proprietario stesso, che mantiene
controllo completo sui propri dati, può visualizzarli, modificarli ed eliminarli
direttamente dall'interfaccia.</p>

<h2>8. Contatti</h2>
<p>Per qualsiasi richiesta relativa alla privacy, contattare il proprietario
dell'applicazione tramite il canale interno utilizzato per l'installazione.</p>

<div class="footer">© Conteggi personali — Uso strettamente personale</div>
</div>
</body>
</html>
"""


@api_router.get("/privacy", response_class=PlainTextResponse)
async def privacy_policy_url_hint():
    """Helper: just tells which is the canonical privacy policy URL (HTML)."""
    return "Open /api/privacy.html in a browser for the privacy policy."


@app.get("/api/privacy.html", response_class=HTMLResponse)
async def privacy_policy_html():
    return PRIVACY_POLICY_HTML


@api_router.post("/calculate", response_model=Calculation)
async def calculate_only(payload: CalculationCreate):
    base = normalize_payload(payload)
    results = compute(payload)
    return Calculation(**base, **results)


@api_router.post("/calculations", response_model=Calculation)
async def create_calculation(payload: CalculationCreate, _: dict = Depends(require_admin)):
    base = normalize_payload(payload)
    results = compute(payload)
    obj = Calculation(**base, **results)
    obj_dict = obj.dict()

    # If a partner persona is selected, create a mirror calc on that account
    partner_id = base.get("partner_persona_id") or ""
    if partner_id and not base.get("mirror_calc_id"):
        partner = await db.persone.find_one({"id": partner_id}, {"_id": 0})
        if partner:
            my_persona = await db.persone.find_one({"id": base.get("persona_id") or ""}, {"_id": 0})
            mirror = dict(obj_dict)
            mirror["id"] = str(uuid.uuid4())
            mirror["persona_id"] = partner_id
            mirror["partner_persona_id"] = base.get("persona_id") or ""
            mirror["mirror_calc_id"] = obj_dict["id"]
            mirror["contratto_con"] = (my_persona or {}).get("nome", "") if my_persona else ""
            await db.calculations.insert_one(mirror)
            obj_dict["mirror_calc_id"] = mirror["id"]

    await db.calculations.insert_one(obj_dict)
    return Calculation(**hydrate_calc(obj_dict))


@api_router.get("/calculations", response_model=List[Calculation])
async def list_calculations(persona_id: Optional[str] = Query(default=None), current: dict = Depends(get_current_user)):
    persona_id = viewer_persona(current, persona_id)
    q: dict = {}
    if persona_id is not None:
        # empty string filters to "no persona"
        q["persona_id"] = persona_id
    docs = await db.calculations.find(q, {"_id": 0}).sort("created_at", -1).to_list(2000)
    return [Calculation(**hydrate_calc(d)) for d in docs]


@api_router.get("/calculations/{calc_id}", response_model=Calculation)
async def get_calculation(calc_id: str, current: dict = Depends(get_current_user)):
    doc = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Calcolo non trovato")
    return Calculation(**hydrate_calc(doc))


@api_router.put("/calculations/{calc_id}", response_model=Calculation)
async def update_calculation(calc_id: str, payload: CalculationCreate, _: dict = Depends(require_admin)):
    existing = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Calcolo non trovato")
    base = normalize_payload(payload)
    results = compute(payload)
    # Preserve mirror_calc_id from existing record
    base["mirror_calc_id"] = existing.get("mirror_calc_id", "") or ""
    await db.calculations.update_one({"id": calc_id}, {"$set": {**base, **results}})

    # Propagate update to mirror calc, but keep its persona/partner/contratto_con
    mirror_id = existing.get("mirror_calc_id") or ""
    if mirror_id:
        mirror = await db.calculations.find_one({"id": mirror_id}, {"_id": 0})
        if mirror:
            propagated = {**base, **results}
            propagated["persona_id"] = mirror.get("persona_id", "")
            propagated["partner_persona_id"] = base.get("persona_id", "")
            propagated["mirror_calc_id"] = calc_id
            # Refresh contratto_con on mirror to reflect original persona's name
            my_persona = await db.persone.find_one({"id": base.get("persona_id") or ""}, {"_id": 0})
            propagated["contratto_con"] = (my_persona or {}).get("nome", "") if my_persona else mirror.get("contratto_con", "")
            await db.calculations.update_one({"id": mirror_id}, {"$set": propagated})

    doc = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    return Calculation(**hydrate_calc(doc))


class RataToggle(BaseModel):
    paid: bool


class CalcMetaPatch(BaseModel):
    nome_cliente: Optional[str] = None
    note: Optional[str] = None


@api_router.patch("/calculations/{calc_id}/meta", response_model=Calculation)
async def patch_calculation_meta(
    calc_id: str,
    body: CalcMetaPatch,
    _: dict = Depends(require_admin),
):
    """Partial update of light-weight fields (nome_cliente, note) without
    triggering a recompute. Used by the Duplicati resolution screen.
    """
    doc = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Calcolo non trovato")
    updates: dict = {}
    if body.nome_cliente is not None:
        nm = body.nome_cliente.strip()
        if not nm:
            raise HTTPException(status_code=400, detail="Nome cliente non valido")
        updates["nome_cliente"] = nm
    if body.note is not None:
        updates["note"] = body.note
    if updates:
        await db.calculations.update_one({"id": calc_id}, {"$set": updates})
    out = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    return Calculation(**hydrate_calc(out))


@api_router.patch("/calculations/{calc_id}/rate/{idx}", response_model=Calculation)
async def toggle_rata(calc_id: str, idx: int, body: RataToggle, _: dict = Depends(require_admin)):
    """Mark a single rata as paid/unpaid. Auto-promote stato to maturato when all paid."""
    doc = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Calcolo non trovato")
    doc = hydrate_calc(doc)
    rate = doc.get("date_pagamenti", [])
    if idx < 0 or idx >= len(rate):
        raise HTTPException(status_code=404, detail="Rata non trovata")
    rate[idx]["paid"] = body.paid
    rate[idx]["paid_at"] = datetime.now(timezone.utc).isoformat() if body.paid else None
    # Keep stato as 'da_maturare' for assegno/bonifico — totals are computed
    # per-rata so paid amounts are counted in maturati and unpaid stay in da_maturare.
    update: dict = {"date_pagamenti": rate}
    await db.calculations.update_one({"id": calc_id}, {"$set": update})

    # Propagate to mirror calc
    mirror_id = doc.get("mirror_calc_id") or ""
    if mirror_id:
        mirror = await db.calculations.find_one({"id": mirror_id}, {"_id": 0})
        if mirror:
            await db.calculations.update_one({"id": mirror_id}, {"$set": update})

    fresh = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    return Calculation(**hydrate_calc(fresh))


@api_router.patch("/calculations/{calc_id}/liquidate", response_model=Calculation)
async def liquidate_finanziaria(calc_id: str, _: dict = Depends(require_admin)):
    """Mark a finanziaria calc as liquidata=true → stato=maturato."""
    doc = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Calcolo non trovato")
    update = {"liquidata": True, "stato": "maturato"}
    await db.calculations.update_one({"id": calc_id}, {"$set": update})
    mirror_id = doc.get("mirror_calc_id") or ""
    if mirror_id:
        await db.calculations.update_one({"id": mirror_id}, {"$set": update})
    fresh = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    return Calculation(**hydrate_calc(fresh))


class FlagBody(BaseModel):
    flag: str  # active | sospeso | recesso


@api_router.patch("/calculations/{calc_id}/flag", response_model=Calculation)
async def set_calc_flag(calc_id: str, body: FlagBody, _: dict = Depends(require_admin)):
    """Set a calc's flag (active/sospeso/recesso). Flagged calcs are excluded from totals."""
    doc = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Calcolo non trovato")
    flag = (body.flag or "active").lower()
    if flag not in ALLOWED_FLAGS:
        raise HTTPException(status_code=400, detail="Flag non valido")
    await db.calculations.update_one({"id": calc_id}, {"$set": {"flag": flag}})
    mirror_id = doc.get("mirror_calc_id") or ""
    if mirror_id:
        await db.calculations.update_one({"id": mirror_id}, {"$set": {"flag": flag}})
    fresh = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    return Calculation(**hydrate_calc(fresh))


@api_router.delete("/calculations/{calc_id}")
async def delete_calculation(calc_id: str, _: dict = Depends(require_admin)):
    doc = await db.calculations.find_one({"id": calc_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Calcolo non trovato")
    await db.calculations.delete_one({"id": calc_id})
    mirror_id = doc.get("mirror_calc_id") or ""
    if mirror_id:
        await db.calculations.delete_one({"id": mirror_id})
    return {"deleted": True, "id": calc_id, "mirror_deleted": bool(mirror_id)}


# ---------- Import calcoli da Excel/CSV/PDF ----------
def _norm_header(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "_").replace("-", "_").replace(".", "")


def _to_float(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        s = str(v).strip().replace("€", "").replace(" ", "").replace(",", ".")
        return float(s) if s else 0.0
    except Exception:
        return 0.0


# Map sinonimi italiani → campo schema
COL_MAP = {
    "nome_cliente": ["nome_cliente", "cliente", "nome", "ragione_sociale", "nome_e_cognome", "nome_cognome"],
    "data": ["data", "data_contratto", "data_pratica"],
    "totale_cliente": ["totale_cliente", "totale", "importo_cliente", "imponibile_cliente", "totale_pratica", "importo", "contratto"],
    "iva_percentuale": ["iva", "iva_%", "iva_percentuale", "aliquota_iva", "iva_perc"],
    "permuta": ["permuta"],
    "provvigione_percentuale": ["provvigione_percentuale", "perc_provvigione", "prov_%", "prov_perc"],
    "tipo_pagamento": ["tipo_pagamento", "pagamento", "metodo_pagamento", "metodo_di_pagamento", "tipo"],
    "note": ["note", "annotazioni", "descrizione", "scadenze"],
    "contratto_con": ["contratto_con", "socio", "partner"],
    "acconto": ["acconto"],
}


def map_row_to_payload(row: dict) -> Optional[dict]:
    """Map a parsed row dict (header-keyed) to a CalculationCreate-compatible dict."""
    norm_row = {_norm_header(k): v for k, v in row.items() if k}
    out: dict = {}
    for field, syns in COL_MAP.items():
        for s in syns:
            if s in norm_row and norm_row[s] not in (None, ""):
                out[field] = norm_row[s]
                break
    if not out.get("nome_cliente"):
        return None
    payload = {
        "persona_id": "",
        "nome_cliente": str(out.get("nome_cliente") or "").strip(),
        "data": str(out.get("data") or "").strip(),
        "note": str(out.get("note") or "").strip(),
        "totale_cliente": _to_float(out.get("totale_cliente")),
        "iva_percentuale": _to_float(out.get("iva_percentuale") or 5),
        "permuta": _to_float(out.get("permuta")),
        "provvigione_percentuale": _to_float(out.get("provvigione_percentuale")),
        "tipo_pagamento": str(out.get("tipo_pagamento") or "contanti").strip().lower() or "contanti",
        "contratto_con": str(out.get("contratto_con") or "").strip(),
        "acconto": _to_float(out.get("acconto")),
        "stato": "maturato",
        "liquidata": False,
        "flag": "active",
        "provvigione_split": 1.0,
        "date_pagamenti": [],
    }
    if payload["totale_cliente"] <= 0 and payload["provvigione_percentuale"] <= 0:
        return None
    if payload["tipo_pagamento"] not in ALLOWED_PAGAMENTI:
        payload["tipo_pagamento"] = "contanti"
    return payload


def parse_xlsx(content: bytes) -> List[dict]:
    import io, openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(c) if c is not None else "" for c in rows[0]]
    out = []
    for r in rows[1:]:
        if all(v in (None, "") for v in r):
            continue
        d = dict(zip(headers, r))
        out.append(d)
    return out


def parse_csv(content: bytes) -> List[dict]:
    import csv, io
    text = content.decode("utf-8-sig", errors="replace")
    # auto-detect ; or , delimiter
    sample = text[:2000]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    return [r for r in reader]


def parse_pdf(content: bytes) -> List[dict]:
    import io, pdfplumber
    out: List[dict] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for t in tables:
                if not t or len(t) < 2:
                    continue
                headers = [str(c or "") for c in t[0]]
                for row in t[1:]:
                    if all((c is None or str(c).strip() == "") for c in row):
                        continue
                    d = dict(zip(headers, [str(c or "") for c in row]))
                    out.append(d)
    return out


def parse_pdf_text_preview(content: bytes) -> List[dict]:
    """
    Extract a best-effort list of {nome_cliente, data} from a PDF that does not
    contain machine-readable tables (typical scanned/exported reports).
    Strategy:
      - Try table extraction first; if rows found, map nome+data from there.
      - Otherwise, scan each page line-by-line and detect rows that contain
        a probable italian name (2+ uppercase tokens) and optionally a date
        in the form GG/MM/AAAA or GG-MM-AAAA. The date can also be on the
        same row (anywhere) or on the previous/next line.
    """
    import io, re, pdfplumber

    NAME_RE = re.compile(r"\b([A-ZÀ-Ý][A-ZÀ-Ý'’\-]{1,})(?:\s+([A-ZÀ-Ý][A-ZÀ-Ý'’\-]{1,})){1,3}\b")
    DATE_RE = re.compile(r"\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})\b")
    STOPWORDS = {
        "RAGIONE", "SOCIALE", "TOTALE", "IMPONIBILE", "PROVVIGIONE", "DATA",
        "CLIENTE", "CONTRATTO", "PRATICA", "IVA", "PAGAMENTO", "ACCONTO",
        "PERMUTA", "BONIFICO", "ASSEGNO", "FINANZIARIA", "CONTANTI", "NOTE",
        "NUMERO", "PERIODO", "ANNO", "MESE", "PAGINA", "PAGE", "DICHIARATI",
        "AGENTE", "COGNOME", "NOME", "CODICE", "FISCALE", "FATTURA",
    }

    out: List[dict] = []
    seen = set()

    def add(name: str, data: str = ""):
        n = " ".join(w.capitalize() for w in re.split(r"\s+", name.strip()) if w)
        if not n:
            return
        key = (n.lower(), data)
        if key in seen:
            return
        seen.add(key)
        out.append({"nome_cliente": n, "data": data})

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        # First try tables (more reliable when present)
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for t in tables:
                if not t or len(t) < 2:
                    continue
                headers = [_norm_header(str(c or "")) for c in t[0]]
                # Find name and date columns
                idx_name = None
                idx_data = None
                for syn in COL_MAP["nome_cliente"]:
                    if syn in headers:
                        idx_name = headers.index(syn)
                        break
                for syn in COL_MAP["data"]:
                    if syn in headers:
                        idx_data = headers.index(syn)
                        break
                if idx_name is None:
                    continue
                for row in t[1:]:
                    cells = [str(c or "").strip() for c in row]
                    if idx_name < len(cells) and cells[idx_name]:
                        nm = cells[idx_name]
                        dt = ""
                        if idx_data is not None and idx_data < len(cells):
                            dt = cells[idx_data]
                        add(nm, dt)

        if out:
            return out

        # Fallback: scan free text line-by-line
        for page in pdf.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for i, ln in enumerate(lines):
                # Skip headerish lines
                upper_tokens = re.findall(r"[A-ZÀ-Ý]{3,}", ln)
                if upper_tokens and all(t in STOPWORDS for t in upper_tokens):
                    continue
                # Find a name in this line (uppercase tokens chain)
                m = NAME_RE.search(ln)
                if not m:
                    continue
                full_name = m.group(0)
                # filter out names that are entirely stopwords
                tokens = [t for t in re.split(r"\s+", full_name) if t]
                if not tokens or all(t in STOPWORDS for t in tokens):
                    continue
                # Look for a date on same line / previous / next line
                date_str = ""
                d = DATE_RE.search(ln)
                if not d and i > 0:
                    d = DATE_RE.search(lines[i - 1])
                if not d and i + 1 < len(lines):
                    d = DATE_RE.search(lines[i + 1])
                if d:
                    gg, mm, aaaa = d.group(1), d.group(2), d.group(3)
                    if len(aaaa) == 2:
                        aaaa = ("20" + aaaa) if int(aaaa) < 70 else ("19" + aaaa)
                    date_str = f"{int(gg):02d}/{int(mm):02d}/{aaaa}"
                add(full_name, date_str)

    return out


def parse_xlsx_preview(content: bytes) -> List[dict]:
    rows = parse_xlsx(content)
    out: List[dict] = []
    for r in rows:
        nr = {_norm_header(k): v for k, v in r.items() if k}
        nm = ""
        dt = ""
        for s in COL_MAP["nome_cliente"]:
            if s in nr and nr[s] not in (None, ""):
                nm = str(nr[s]).strip()
                break
        for s in COL_MAP["data"]:
            if s in nr and nr[s] not in (None, ""):
                dt = str(nr[s]).strip()
                break
        if nm:
            out.append({"nome_cliente": nm, "data": dt})
    return out


def parse_csv_preview(content: bytes) -> List[dict]:
    rows = parse_csv(content)
    out: List[dict] = []
    for r in rows:
        nr = {_norm_header(k): v for k, v in r.items() if k}
        nm = ""
        dt = ""
        for s in COL_MAP["nome_cliente"]:
            if s in nr and nr[s] not in (None, ""):
                nm = str(nr[s]).strip()
                break
        for s in COL_MAP["data"]:
            if s in nr and nr[s] not in (None, ""):
                dt = str(nr[s]).strip()
                break
        if nm:
            out.append({"nome_cliente": nm, "data": dt})
    return out


@api_router.post("/calculations/import")
async def import_calculations(
    persona_id: str = Form(...),
    file: UploadFile = File(...),
    preview: Optional[str] = Form("false"),
    _: dict = Depends(require_admin),
):
    """
    Import calcoli from Excel (.xlsx), CSV, or PDF.
    - When `preview=true`: returns a list of {nome_cliente, data} extracted from the
      file WITHOUT inserting anything. The frontend then shows a dedicated screen
      where the user fills in the missing fields (importo, IVA, tipo pagamento, …)
      and finally calls `/calculations/import/bulk` to persist everything.
    - When `preview=false` (default): legacy behavior, tries to map full rows and
      insert them directly.
    """
    if not persona_id:
        raise HTTPException(status_code=400, detail="persona_id obbligatorio")
    persona = await db.persone.find_one({"id": persona_id}, {"_id": 0})
    if not persona:
        raise HTTPException(status_code=404, detail="Persona non trovata")
    content = await file.read()
    name = (file.filename or "").lower()

    is_preview = str(preview or "").lower() in ("true", "1", "yes", "y")

    try:
        if is_preview:
            if name.endswith(".xlsx") or name.endswith(".xlsm"):
                items = parse_xlsx_preview(content)
            elif name.endswith(".csv"):
                items = parse_csv_preview(content)
            elif name.endswith(".pdf"):
                items = parse_pdf_text_preview(content)
            else:
                raise HTTPException(status_code=400, detail="Formato non supportato (usa .xlsx, .csv o .pdf)")
            return {
                "preview": True,
                "items": items,
                "count": len(items),
                "filename": file.filename or "",
            }

        if name.endswith(".xlsx") or name.endswith(".xlsm"):
            rows = parse_xlsx(content)
        elif name.endswith(".csv"):
            rows = parse_csv(content)
        elif name.endswith(".pdf"):
            rows = parse_pdf(content)
        else:
            raise HTTPException(status_code=400, detail="Formato non supportato (usa .xlsx, .csv o .pdf)")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Impossibile leggere il file: {e}")

    inserted = 0
    skipped = 0
    errors = []
    for r in rows:
        payload_dict = map_row_to_payload(r)
        if not payload_dict:
            skipped += 1
            continue
        payload_dict["persona_id"] = persona_id
        try:
            payload = CalculationCreate(**payload_dict)
            base = normalize_payload(payload)
            results = compute(payload)
            obj = Calculation(**base, **results)
            await db.calculations.insert_one(obj.dict())
            inserted += 1
        except Exception as e:
            errors.append(str(e))
            skipped += 1
    return {
        "inserted": inserted,
        "skipped": skipped,
        "total_rows": len(rows),
        "errors": errors[:5],
    }


class BulkImportItem(BaseModel):
    nome_cliente: str
    data: Optional[str] = ""
    note: Optional[str] = ""
    totale_cliente: Optional[float] = 0.0
    iva_percentuale: Optional[float] = 5.0
    permuta: Optional[float] = 0.0
    provvigione_percentuale: Optional[float] = 0.0
    tipo_pagamento: Optional[str] = "contanti"
    contratto_con: Optional[str] = ""
    acconto: Optional[float] = 0.0
    stato: Optional[str] = "maturato"  # "maturato" | "da_maturare"
    liquidata: Optional[bool] = False
    flag: Optional[str] = "active"
    date_pagamenti: Optional[List[str]] = []  # "GG/MM/AAAA"
    partner_persona_id: Optional[str] = ""  # if set, create mirror calc at 50/50


class BulkImportBody(BaseModel):
    persona_id: str
    items: List[BulkImportItem]


@api_router.post("/calculations/import/bulk")
async def import_calculations_bulk(
    body: BulkImportBody,
    _: dict = Depends(require_admin),
):
    """Persist a list of calculations crafted by the user after preview.

    Supports per-item partner_persona_id: when present, creates a mirrored
    calculation on the partner persona at 50/50 split (just like create_calculation).
    """
    if not body.persona_id:
        raise HTTPException(status_code=400, detail="persona_id obbligatorio")
    persona = await db.persone.find_one({"id": body.persona_id}, {"_id": 0})
    if not persona:
        raise HTTPException(status_code=404, detail="Persona non trovata")
    if not body.items:
        raise HTTPException(status_code=400, detail="Nessun cliente da importare")

    my_nome = (persona or {}).get("nome", "") or ""
    inserted = 0
    mirrored = 0
    skipped = 0
    errors: List[str] = []
    for it in body.items:
        try:
            nm = (it.nome_cliente or "").strip()
            if not nm:
                skipped += 1
                continue
            partner_id = (it.partner_persona_id or "").strip()
            partner_doc = None
            if partner_id:
                partner_doc = await db.persone.find_one({"id": partner_id}, {"_id": 0})
                if not partner_doc or partner_id == body.persona_id:
                    partner_id = ""
                    partner_doc = None
            # If partner present, force 50/50 split and set contratto_con = partner's nome
            split_val = 0.5 if partner_id else 1.0
            contratto_con_final = (it.contratto_con or "").strip()
            if partner_doc:
                contratto_con_final = partner_doc.get("nome", "") or contratto_con_final
            payload_dict = {
                "persona_id": body.persona_id,
                "nome_cliente": nm,
                "data": (it.data or "").strip(),
                "note": (it.note or "").strip(),
                "totale_cliente": float(it.totale_cliente or 0.0),
                "iva_percentuale": float(it.iva_percentuale or 0.0),
                "permuta": float(it.permuta or 0.0),
                "provvigione_percentuale": float(it.provvigione_percentuale or 0.0),
                "tipo_pagamento": (it.tipo_pagamento or "contanti").lower(),
                "contratto_con": contratto_con_final,
                "acconto": float(it.acconto or 0.0),
                "stato": (it.stato or "maturato").lower(),
                "liquidata": bool(it.liquidata or False),
                "flag": (it.flag or "active").lower(),
                "provvigione_split": split_val,
                "partner_persona_id": partner_id,
                "date_pagamenti": [d for d in (it.date_pagamenti or []) if d and str(d).strip()],
            }
            if payload_dict["tipo_pagamento"] not in ALLOWED_PAGAMENTI:
                payload_dict["tipo_pagamento"] = "contanti"
            if payload_dict["stato"] not in ALLOWED_STATI:
                payload_dict["stato"] = "maturato"
            if payload_dict["flag"] not in ALLOWED_FLAGS:
                payload_dict["flag"] = "active"
            payload = CalculationCreate(**payload_dict)
            base = normalize_payload(payload)
            results = compute(payload)
            obj = Calculation(**base, **results)
            obj_dict = obj.dict()

            # Mirror on partner persona if present
            if partner_id and partner_doc:
                mirror = dict(obj_dict)
                mirror["id"] = str(uuid.uuid4())
                mirror["persona_id"] = partner_id
                mirror["partner_persona_id"] = body.persona_id
                mirror["mirror_calc_id"] = obj_dict["id"]
                mirror["contratto_con"] = my_nome
                await db.calculations.insert_one(mirror)
                obj_dict["mirror_calc_id"] = mirror["id"]
                mirrored += 1

            await db.calculations.insert_one(obj_dict)
            inserted += 1
        except Exception as e:
            errors.append(str(e))
            skipped += 1

    return {
        "inserted": inserted,
        "mirrored": mirrored,
        "skipped": skipped,
        "total": len(body.items),
        "errors": errors[:5],
    }



# -------- Ricevuti --------
class RicevutoCreate(BaseModel):
    persona_id: Optional[str] = ""
    data: str  # GG/MM/AAAA
    importo: float
    causale: Optional[str] = ""


class Ricevuto(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    persona_id: str = ""
    data: str
    importo: float
    causale: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@api_router.post("/ricevuti", response_model=Ricevuto)
async def create_ricevuto(payload: RicevutoCreate, _: dict = Depends(require_admin)):
    obj = Ricevuto(
        persona_id=payload.persona_id or "",
        data=payload.data,
        importo=float(payload.importo or 0.0),
        causale=(payload.causale or "").strip(),
    )
    await db.ricevuti.insert_one(obj.dict())
    return obj


@api_router.get("/ricevuti", response_model=List[Ricevuto])
async def list_ricevuti(persona_id: Optional[str] = Query(default=None), current: dict = Depends(get_current_user)):
    persona_id = viewer_persona(current, persona_id)
    q: dict = {}
    if persona_id is not None:
        q["persona_id"] = persona_id
    docs = await db.ricevuti.find(q, {"_id": 0}).sort("created_at", -1).to_list(2000)
    return [Ricevuto(**d) for d in docs]


@api_router.put("/ricevuti/{rid}", response_model=Ricevuto)
async def update_ricevuto(rid: str, payload: RicevutoCreate, _: dict = Depends(require_admin)):
    existing = await db.ricevuti.find_one({"id": rid}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Ricevuto non trovato")
    update = {
        "persona_id": payload.persona_id or "",
        "data": payload.data,
        "importo": float(payload.importo or 0.0),
        "causale": (payload.causale or "").strip(),
    }
    await db.ricevuti.update_one({"id": rid}, {"$set": update})
    doc = await db.ricevuti.find_one({"id": rid}, {"_id": 0})
    return Ricevuto(**doc)


@api_router.delete("/ricevuti/{rid}")
async def delete_ricevuto(rid: str, _: dict = Depends(require_admin)):
    res = await db.ricevuti.delete_one({"id": rid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Ricevuto non trovato")
    return {"deleted": True, "id": rid}


# -------- Ricevuti import (preview + bulk) --------
def _parse_eu_amount(s: str) -> Optional[float]:
    """Parse italian formatted amount '1.500,00' or '1500,00' or '1500.00' or '1500'."""
    if not s:
        return None
    txt = str(s).strip().replace("€", "").replace(" ", "")
    if not txt:
        return None
    # If both '.' and ',' present → assume IT format (',' decimal, '.' thousand)
    if "," in txt and "." in txt:
        txt = txt.replace(".", "").replace(",", ".")
    elif "," in txt:
        # Decimal comma
        txt = txt.replace(".", "").replace(",", ".")
    # else: just dots/digits → leave as-is
    try:
        v = float(txt)
        return v
    except Exception:
        return None


def parse_pdf_ricevuti_preview(content: bytes) -> List[dict]:
    """
    Extract {data, importo, causale} rows from a PDF where each line typically has:
        DD/MM/YYYY [optional 'FT. NN/AAAA'] importo (1.500,00) [nota]
    Notes can also appear on the same line right after the amount or before it.
    """
    import io, re, pdfplumber

    DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
    # Italian-formatted amount that tolerates broken thousands separators
    # produced by PDF text extraction (e.g. "6 50,00" for "650,00", "1.500,00", "1 500,00").
    AMOUNT_RE = re.compile(r"\d[\d\.\s]{0,12}\d,\d{2}|\b\d{1,3},\d{2}\b")

    out: List[dict] = []
    seen = set()

    def add(data: str, imp: float, note: str):
        key = (data, round(imp, 2), (note or "").strip().lower())
        if key in seen:
            return
        seen.add(key)
        out.append({"data": data, "importo": imp, "causale": (note or "").strip()})

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        # Strategy: line-by-line scan FIRST (works for both real tables and free text PDFs).
        # Only fall back to extract_tables() if the per-line scan returns nothing.
        for page in pdf.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            for raw_ln in text.splitlines():
                ln = raw_ln.strip()
                if not ln:
                    continue
                md = DATE_RE.search(ln)
                if not md:
                    continue
                # Strip the matched date from the line BEFORE searching for the amount
                # (avoids matching "2024 6 50,00" as a single number).
                ln_no_date = ln[: md.start()] + " " + ln[md.end():]
                ma = AMOUNT_RE.search(ln_no_date)
                if not ma:
                    continue
                gg, mm, aaaa = md.group(1), md.group(2), md.group(3)
                if len(aaaa) == 2:
                    aaaa = ("20" + aaaa) if int(aaaa) < 70 else ("19" + aaaa)
                data = f"{int(gg):02d}/{int(mm):02d}/{aaaa}"
                imp = _parse_eu_amount(ma.group(0)) or 0.0
                if imp <= 0 or imp > 1_000_000:
                    continue
                # Note = line stripped of date and amount
                note = ln_no_date.replace(ma.group(0), " ")
                note = re.sub(r"\s+", " ", note)
                note = note.replace("€", "").strip(" \t-:|")
                add(data, imp, note)

        if out:
            return out

        # Fallback: real table extraction (rare for these reports)
        for page in pdf.pages:
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            for t in tables:
                if not t:
                    continue
                for row in t:
                    cells = [str(c or "").strip() for c in row]
                    joined = " ".join(cells)
                    md = DATE_RE.search(joined)
                    if not md:
                        continue
                    ma = AMOUNT_RE.search(joined)
                    if not ma:
                        continue
                    gg, mm, aaaa = md.group(1), md.group(2), md.group(3)
                    if len(aaaa) == 2:
                        aaaa = ("20" + aaaa) if int(aaaa) < 70 else ("19" + aaaa)
                    data = f"{int(gg):02d}/{int(mm):02d}/{aaaa}"
                    imp = _parse_eu_amount(ma.group(0)) or 0.0
                    if imp <= 0:
                        continue
                    note_parts = []
                    for c in cells:
                        if c and not DATE_RE.fullmatch(c.strip()) and not AMOUNT_RE.fullmatch(c.strip()):
                            cleaned = DATE_RE.sub("", c)
                            cleaned = AMOUNT_RE.sub("", cleaned)
                            cleaned = cleaned.replace("€", "").strip(" \t-:|")
                            if cleaned and len(cleaned) <= 60:
                                note_parts.append(cleaned)
                    add(data, imp, " ".join(note_parts).strip())

    return out


def parse_xlsx_ricevuti_preview(content: bytes) -> List[dict]:
    """Extract {data, importo, causale} from an Excel file."""
    import io, openpyxl, re
    DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    # Detect header
    headers = [_norm_header(str(c) if c is not None else "") for c in rows[0]]
    DATA_SYNS = ["data", "data_pagamento", "data_ricevuto", "data_pratica"]
    AMT_SYNS = ["importo", "totale", "incasso", "pagato", "valore", "ricevuto"]
    NOTE_SYNS = ["causale", "note", "annotazioni", "descrizione", "nota"]
    idx_data = next((headers.index(h) for h in DATA_SYNS if h in headers), None)
    idx_amt = next((headers.index(h) for h in AMT_SYNS if h in headers), None)
    idx_note = next((headers.index(h) for h in NOTE_SYNS if h in headers), None)
    out: List[dict] = []
    start = 1 if (idx_data is not None or idx_amt is not None) else 0
    for r in rows[start:]:
        if not r:
            continue
        # Try mapping by header indices
        data = ""
        imp = 0.0
        note = ""
        if idx_data is not None and idx_data < len(r):
            cell = r[idx_data]
            if hasattr(cell, "strftime"):
                data = cell.strftime("%d/%m/%Y")
            else:
                m = DATE_RE.search(str(cell or ""))
                if m:
                    gg, mm, aaaa = m.group(1), m.group(2), m.group(3)
                    if len(aaaa) == 2:
                        aaaa = ("20" + aaaa) if int(aaaa) < 70 else ("19" + aaaa)
                    data = f"{int(gg):02d}/{int(mm):02d}/{aaaa}"
        if idx_amt is not None and idx_amt < len(r):
            v = r[idx_amt]
            if isinstance(v, (int, float)):
                imp = float(v)
            else:
                imp = _parse_eu_amount(str(v or "")) or 0.0
        if idx_note is not None and idx_note < len(r):
            note = str(r[idx_note] or "").strip()
        if not data or imp <= 0:
            # Fallback: scan the whole row
            joined = " ".join(str(c) if c is not None else "" for c in r)
            m = DATE_RE.search(joined)
            if m and not data:
                gg, mm, aaaa = m.group(1), m.group(2), m.group(3)
                if len(aaaa) == 2:
                    aaaa = ("20" + aaaa) if int(aaaa) < 70 else ("19" + aaaa)
                data = f"{int(gg):02d}/{int(mm):02d}/{aaaa}"
            if imp <= 0:
                # find first number
                for v in r:
                    if isinstance(v, (int, float)):
                        imp = float(v)
                        break
                    parsed = _parse_eu_amount(str(v or ""))
                    if parsed and parsed > 0:
                        imp = parsed
                        break
        if data and imp > 0:
            out.append({"data": data, "importo": imp, "causale": note})
    return out


@api_router.post("/ricevuti/import")
async def import_ricevuti_file(
    persona_id: str = Form(...),
    file: UploadFile = File(...),
    preview: Optional[str] = Form("true"),
    _: dict = Depends(require_admin),
):
    """
    Preview-mode import for ricevuti (PDF/Excel). Returns
        {preview:true, items:[{data, importo, causale}], count, filename}
    Mirrors /calculations/import.
    """
    if not persona_id:
        raise HTTPException(status_code=400, detail="persona_id obbligatorio")
    persona = await db.persone.find_one({"id": persona_id}, {"_id": 0})
    if not persona:
        raise HTTPException(status_code=404, detail="Persona non trovata")
    content = await file.read()
    name = (file.filename or "").lower()
    is_preview = str(preview or "true").lower() in ("true", "1", "yes", "y")
    try:
        if name.endswith(".xlsx") or name.endswith(".xlsm"):
            items = parse_xlsx_ricevuti_preview(content)
        elif name.endswith(".pdf"):
            items = parse_pdf_ricevuti_preview(content)
        elif name.endswith(".csv"):
            # Reuse xlsx-style header detection on CSV via in-memory conversion
            import io, csv
            text = content.decode("utf-8-sig", errors="replace")
            sample = text[:2000]
            delim = ";" if sample.count(";") > sample.count(",") else ","
            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            tmp_rows = list(reader)
            # Build a faux xlsx by extracting the same way parse_csv works
            items = []
            DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b") if False else __import__("re").compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
            for r in tmp_rows:
                nr = {_norm_header(k): v for k, v in r.items() if k}
                d = ""
                a = 0.0
                n = ""
                for s in ["data", "data_pagamento", "data_ricevuto"]:
                    if s in nr and nr[s]:
                        m = DATE_RE.search(str(nr[s]))
                        if m:
                            gg, mm, aaaa = m.group(1), m.group(2), m.group(3)
                            if len(aaaa) == 2:
                                aaaa = ("20" + aaaa) if int(aaaa) < 70 else ("19" + aaaa)
                            d = f"{int(gg):02d}/{int(mm):02d}/{aaaa}"
                            break
                for s in ["importo", "totale", "incasso", "pagato", "valore"]:
                    if s in nr and nr[s] not in (None, ""):
                        a = _parse_eu_amount(str(nr[s])) or 0.0
                        if a > 0:
                            break
                for s in ["causale", "note", "annotazioni", "descrizione", "nota"]:
                    if s in nr and nr[s]:
                        n = str(nr[s]).strip()
                        break
                if d and a > 0:
                    items.append({"data": d, "importo": a, "causale": n})
        else:
            raise HTTPException(status_code=400, detail="Formato non supportato (usa .xlsx, .csv o .pdf)")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Impossibile leggere il file: {e}")

    if is_preview:
        return {
            "preview": True,
            "items": items,
            "count": len(items),
            "filename": file.filename or "",
        }

    # Direct insert mode (rarely used; frontend always previews first)
    inserted = 0
    skipped = 0
    for it in items:
        try:
            obj = Ricevuto(
                persona_id=persona_id,
                data=it["data"],
                importo=float(it["importo"]),
                causale=str(it.get("causale") or "").strip(),
            )
            await db.ricevuti.insert_one(obj.dict())
            inserted += 1
        except Exception:
            skipped += 1
    return {"inserted": inserted, "skipped": skipped, "total": len(items)}


class RicevutiBulkItem(BaseModel):
    data: str
    importo: float
    causale: Optional[str] = ""


class RicevutiBulkBody(BaseModel):
    persona_id: str
    items: List[RicevutiBulkItem]


@api_router.post("/ricevuti/import/bulk")
async def import_ricevuti_bulk(
    body: RicevutiBulkBody,
    _: dict = Depends(require_admin),
):
    if not body.persona_id:
        raise HTTPException(status_code=400, detail="persona_id obbligatorio")
    persona = await db.persone.find_one({"id": body.persona_id}, {"_id": 0})
    if not persona:
        raise HTTPException(status_code=404, detail="Persona non trovata")
    if not body.items:
        raise HTTPException(status_code=400, detail="Nessun ricevuto da importare")

    inserted = 0
    skipped = 0
    errors: List[str] = []
    for it in body.items:
        try:
            data = (it.data or "").strip()
            imp = float(it.importo or 0.0)
            if not data or imp <= 0:
                skipped += 1
                continue
            obj = Ricevuto(
                persona_id=body.persona_id,
                data=data,
                importo=imp,
                causale=(it.causale or "").strip(),
            )
            await db.ricevuti.insert_one(obj.dict())
            inserted += 1
        except Exception as e:
            errors.append(str(e))
            skipped += 1

    return {
        "inserted": inserted,
        "skipped": skipped,
        "total": len(body.items),
        "errors": errors[:5],
    }


@api_router.get("/persone-stats")
async def persone_stats(current: dict = Depends(get_current_user)):
    """Returns per-persona: maturato_totale, ricevuto_totale, maturato_rimanente."""
    persone_docs = await db.persone.find({}, {"_id": 0}).to_list(500)
    calc_docs = await db.calculations.find({}, {"_id": 0}).to_list(10000)
    ric_docs = await db.ricevuti.find({}, {"_id": 0}).to_list(10000)
    if (current or {}).get("role") == "viewer":
        pid_only = current.get("assigned_persona_id") or ""
        persone_docs = [p for p in persone_docs if p.get("id") == pid_only]
    out = []
    for p in persone_docs:
        pid = p["id"]
        maturato = 0.0
        for c in calc_docs:
            if c.get("persona_id") != pid:
                continue
            if (c.get("flag") or "active") != "active":
                continue
            prov = float(c.get("provvigione") or 0.0)
            stato = c.get("stato", "maturato")
            if stato == "maturato":
                maturato += prov
            else:
                # da_maturare with rate: count paid rate amounts as maturato
                rate = c.get("date_pagamenti") or []
                if rate:
                    total_amount = sum(float(r.get("amount") or 0.0) for r in rate)
                    # Use explicit amounts only when sum is in scale (≤ 110% of prov)
                    use_explicit = total_amount > 0 and total_amount <= prov * 1.1
                    if use_explicit:
                        explicit_paid = sum(float(r.get("amount") or 0.0) for r in rate if r.get("paid"))
                        maturato += min(explicit_paid, prov)
                    else:
                        paid_count = sum(1 for r in rate if r.get("paid"))
                        if paid_count > 0:
                            maturato += prov * (paid_count / len(rate))
        ricevuto = 0.0
        for r in ric_docs:
            if r.get("persona_id") == pid:
                ricevuto += float(r.get("importo") or 0.0)
        out.append({
            "id": pid,
            "nome": p.get("nome", ""),
            "color": p.get("color", "#2563EB"),
            "maturato_totale": round(maturato, 2),
            "ricevuto_totale": round(ricevuto, 2),
            "maturato_rimanente": round(maturato - ricevuto, 2),
        })
    return out


@api_router.get("/pending-rate")
async def pending_rate(persona_id: Optional[str] = Query(default=None), current: dict = Depends(get_current_user)):
    persona_id = viewer_persona(current, persona_id)
    """Return all unpaid rate (across calcs) sorted by due date asc."""
    q: dict = {"tipo_pagamento": {"$in": ["assegno", "bonifico"]}}
    if persona_id is not None:
        q["persona_id"] = persona_id
    docs = await db.calculations.find(q, {"_id": 0}).to_list(2000)
    out = []
    for d in docs:
        d = hydrate_calc(d)
        if (d.get("flag") or "active") != "active":
            continue
        for idx, r in enumerate(d.get("date_pagamenti", [])):
            if not r.get("paid"):
                rata_amount = float(r.get("amount") or 0.0)
                # Fallback: equal share when no explicit amount
                if rata_amount <= 0 and d.get("date_pagamenti"):
                    rata_amount = round(
                        float(d.get("provvigione") or 0.0) / max(len(d["date_pagamenti"]), 1),
                        2,
                    )
                out.append({
                    "calc_id": d["id"],
                    "rata_index": idx,
                    "rata_date": r.get("date", ""),
                    "rata_amount": rata_amount,
                    "nome_cliente": d.get("nome_cliente", ""),
                    "tipo_pagamento": d.get("tipo_pagamento", ""),
                    "provvigione": d.get("provvigione", 0.0),
                    "stato": d.get("stato", "maturato"),
                    "persona_id": d.get("persona_id", ""),
                })
    # Sort by parsed Italian date
    def _key(item):
        s = item.get("rata_date") or ""
        m = None
        try:
            parts = s.replace("-", "/").replace(".", "/").split("/")
            if len(parts) == 3:
                dd, mm, yy = int(parts[0]), int(parts[1]), int(parts[2])
                if yy < 100:
                    yy += 2000
                m = datetime(yy, mm, dd)
        except Exception:
            m = None
        return m or datetime.max
    out.sort(key=_key)
    return out


@api_router.get("/summary")
async def get_summary(persona_id: Optional[str] = Query(default=None), current: dict = Depends(get_current_user)):
    persona_id = viewer_persona(current, persona_id)
    q: dict = {}
    if persona_id is not None:
        q["persona_id"] = persona_id
    docs = await db.calculations.find(q, {"_id": 0}).to_list(10000)
    by_client: dict = {}
    by_ym: dict = {}
    by_year: dict = {}
    grand_total = 0.0
    grand_imponibile = 0.0
    grand_count = 0
    grand_maturato = 0.0
    grand_da_maturare = 0.0
    for raw in docs:
        d = hydrate_calc(raw)
        if (d.get("flag") or "active") != "active":
            continue
        prov = float(d.get("provvigione") or 0.0)
        imp = float(d.get("imponibile") or 0.0)
        stato = d.get("stato") or "maturato"
        grand_total += prov
        grand_imponibile += imp
        grand_count += 1

        # Compute mat/dam at rata level for assegno/bonifico in da_maturare
        if stato == "maturato":
            mat_share = prov
            dam_share = 0.0
        else:
            rate = d.get("date_pagamenti") or []
            if rate:
                paid_amounts = [float(r.get("amount") or 0.0) for r in rate if r.get("paid")]
                explicit_paid = sum(paid_amounts)
                if explicit_paid > 0:
                    mat_share = min(explicit_paid, prov)
                    dam_share = max(prov - mat_share, 0.0)
                else:
                    paid_count = sum(1 for r in rate if r.get("paid"))
                    if paid_count > 0:
                        mat_share = prov * (paid_count / len(rate))
                        dam_share = prov - mat_share
                    else:
                        mat_share = 0.0
                        dam_share = prov
            else:
                mat_share = 0.0
                dam_share = prov
        grand_maturato += mat_share
        grand_da_maturare += dam_share
        nome = (d.get("nome_cliente") or "").strip() or "—"
        if nome not in by_client:
            by_client[nome] = {
                "nome_cliente": nome,
                "count": 0,
                "totale_imponibile": 0.0,
                "totale_provvigioni": 0.0,
            }
        by_client[nome]["count"] += 1
        by_client[nome]["totale_imponibile"] += imp
        by_client[nome]["totale_provvigioni"] += prov
        try:
            dt = datetime.fromisoformat(d.get("created_at"))
        except Exception:
            continue
        ym_key = f"{dt.year}-{dt.month:02d}"
        if ym_key not in by_ym:
            by_ym[ym_key] = {
                "year": dt.year, "month": dt.month, "count": 0,
                "totale_imponibile": 0.0, "totale_provvigioni": 0.0,
            }
        by_ym[ym_key]["count"] += 1
        by_ym[ym_key]["totale_imponibile"] += imp
        by_ym[ym_key]["totale_provvigioni"] += prov
        if dt.year not in by_year:
            by_year[dt.year] = {
                "year": dt.year, "count": 0,
                "totale_imponibile": 0.0, "totale_provvigioni": 0.0,
                "maturato": 0.0, "da_maturare": 0.0,
            }
        by_year[dt.year]["count"] += 1
        by_year[dt.year]["totale_imponibile"] += imp
        by_year[dt.year]["totale_provvigioni"] += prov
        by_year[dt.year]["maturato"] += mat_share
        by_year[dt.year]["da_maturare"] += dam_share

    def _round(items):
        out = []
        for v in items:
            nv = dict(v)
            for k in ("totale_imponibile", "totale_provvigioni", "maturato", "da_maturare"):
                if k in nv:
                    nv[k] = round(nv[k], 2)
            out.append(nv)
        return out

    return {
        "grand_total_provvigioni": round(grand_total, 2),
        "grand_total_imponibile": round(grand_imponibile, 2),
        "grand_count": grand_count,
        "grand_maturato": round(grand_maturato, 2),
        "grand_da_maturare": round(grand_da_maturare, 2),
        "by_client": sorted(_round(list(by_client.values())), key=lambda x: -x["totale_provvigioni"]),
        "by_year": sorted(_round(list(by_year.values())), key=lambda x: -x["year"]),
        "by_year_month": sorted(_round(list(by_ym.values())), key=lambda x: (-x["year"], -x["month"])),
    }


@api_router.get("/cliente-residuo")
async def cliente_residuo(persona_id: Optional[str] = Query(default=None), current: dict = Depends(get_current_user)):
    persona_id = viewer_persona(current, persona_id)
    """Returns per-cliente: maturato, da_maturare, totale provvigioni for the given persona."""
    q: dict = {}
    if persona_id is not None:
        q["persona_id"] = persona_id
    docs = await db.calculations.find(q, {"_id": 0}).to_list(10000)
    by_client: dict = {}
    for raw in docs:
        d = hydrate_calc(raw)
        if (d.get("flag") or "active") != "active":
            continue
        nome = (d.get("nome_cliente") or "").strip() or "—"
        prov = float(d.get("provvigione") or 0.0)
        stato = d.get("stato") or "maturato"
        if nome not in by_client:
            by_client[nome] = {
                "nome_cliente": nome,
                "totale_provvigioni": 0.0,
                "maturato": 0.0,
                "da_maturare": 0.0,
                "count": 0,
            }
        by_client[nome]["count"] += 1
        by_client[nome]["totale_provvigioni"] += prov
        if stato == "maturato":
            by_client[nome]["maturato"] += prov
        else:
            by_client[nome]["da_maturare"] += prov
    out = []
    for v in by_client.values():
        out.append({
            **v,
            "totale_provvigioni": round(v["totale_provvigioni"], 2),
            "maturato": round(v["maturato"], 2),
            "da_maturare": round(v["da_maturare"], 2),
        })
    out.sort(key=lambda x: -x["da_maturare"])
    return out


# -------- Duplicati (stesso cliente su persone diverse) --------
def _norm_nome(s: str) -> str:
    return (s or "").strip().lower()


def _norm_data(s: str) -> str:
    """Normalize a date string to GG/MM/AAAA. Returns '' if invalid."""
    s = (s or "").strip()
    if not s:
        return ""
    if "-" in s and len(s) >= 10:
        try:
            y, m, d = s[:10].split("-")
            return f"{int(d):02d}/{int(m):02d}/{int(y):04d}"
        except Exception:
            return s
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            try:
                d, m, y = parts
                return f"{int(d):02d}/{int(m):02d}/{int(y):04d}"
            except Exception:
                return s
    return s


@api_router.get("/duplicati")
async def list_duplicati(_: dict = Depends(require_admin)):
    """Find clients that appear on MULTIPLE different personas (same nome_cliente
    case-insensitive + same date), excluding mirror pairs (which are intentional splits).

    Returns a list of groups: [{key, nome_cliente, data, cards: [...]}].
    """
    docs = await db.calculations.find({}, {"_id": 0}).to_list(10000)
    persone_docs = await db.persone.find({}, {"_id": 0}).to_list(500)
    persone_by_id = {p["id"]: p for p in persone_docs}

    groups: dict = {}
    for raw in docs:
        d = hydrate_calc(raw)
        nome_norm = _norm_nome(d.get("nome_cliente"))
        data_norm = _norm_data(d.get("data"))
        if not nome_norm:
            continue
        key = f"{nome_norm}|{data_norm}"
        groups.setdefault(key, []).append(d)

    result = []
    for key, cards in groups.items():
        if len(cards) < 2:
            continue
        ids = {c["id"] for c in cards}
        # Exclude cards that are part of an intentional mirror pair within this group
        real_duplicate_cards = []
        for c in cards:
            mirror_id = c.get("mirror_calc_id") or ""
            if mirror_id and mirror_id in ids:
                # this card has its mirror in the group → skip (intentional split)
                continue
            real_duplicate_cards.append(c)
        if len(real_duplicate_cards) < 2:
            continue
        personas_real = {c.get("persona_id", "") for c in real_duplicate_cards}
        if len(personas_real) < 2:
            continue

        nome_pretty = real_duplicate_cards[0].get("nome_cliente") or ""
        data_pretty = real_duplicate_cards[0].get("data") or ""
        enriched = []
        for c in real_duplicate_cards:
            p = persone_by_id.get(c.get("persona_id", ""), {})
            enriched.append({
                **c,
                "persona_nome": p.get("nome", ""),
                "persona_color": p.get("color", "#2563EB"),
            })
        enriched.sort(key=lambda x: x.get("created_at", ""))
        result.append({
            "key": key,
            "nome_cliente": nome_pretty,
            "data": data_pretty,
            "cards": enriched,
        })

    result.sort(key=lambda g: max((c.get("created_at", "") for c in g["cards"]), default=""), reverse=True)
    return result


# -------- Auth & Users --------
@api_router.post("/auth/login")
async def login(body: LoginBody):
    email = body.email.strip().lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Email o password errati")
    token = create_access_token(user["id"], user["email"], user.get("role", "viewer"))
    return {"token": token, "user": serialize_user(user)}


@api_router.get("/auth/me")
async def me(current=Depends(get_current_user)):
    return serialize_user(current)


@api_router.get("/users", response_model=List[UserPublic])
async def list_users(_: dict = Depends(require_admin)):
    docs = await db.users.find({}, {"_id": 0, "password_hash": 0}).to_list(500)
    return [serialize_user(u) for u in docs]


@api_router.post("/users", response_model=UserPublic)
async def create_user(payload: UserCreate, _: dict = Depends(require_admin)):
    email = payload.email.strip().lower()
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email già registrata")
    if len(payload.password or "") < 4:
        raise HTTPException(status_code=400, detail="Password troppo corta")
    role = payload.role if payload.role in ("admin", "viewer") else "viewer"
    obj = {
        "id": str(uuid.uuid4()),
        "email": email,
        "password_hash": hash_password(payload.password),
        "nome": (payload.nome or "").strip(),
        "role": role,
        "assigned_persona_id": payload.assigned_persona_id or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(obj)
    return serialize_user(obj)


@api_router.delete("/users/{user_id}")
async def delete_user(user_id: str, current: dict = Depends(require_admin)):
    if user_id == current.get("id"):
        raise HTTPException(status_code=400, detail="Non puoi eliminare te stesso")
    res = await db.users.delete_one({"id": user_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    return {"deleted": True, "id": user_id}


@api_router.put("/users/{user_id}/persona")
async def assign_persona_to_user(
    user_id: str, body: dict, _: dict = Depends(require_admin)
):
    persona_id = (body or {}).get("assigned_persona_id", "")
    res = await db.users.update_one(
        {"id": user_id}, {"$set": {"assigned_persona_id": persona_id or ""}}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    fresh = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    return serialize_user(fresh)


# -------- Static icon download --------
@api_router.get("/download-icon")
async def download_icon():
    icon_path = ROOT_DIR / "static" / "icon.png"
    if not icon_path.exists():
        raise HTTPException(status_code=404, detail="Icon not found")
    return FileResponse(
        path=str(icon_path),
        filename="icon.png",
        media_type="image/png"
    )


# -------- Screenshots ZIP download --------
@api_router.get("/download-screenshots")
async def download_screenshots():
    zip_path = ROOT_DIR / "static" / "screenshots.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Screenshots not found")
    return FileResponse(
        path=str(zip_path),
        filename="screenshots_conteggi_personali.zip",
        media_type="application/zip"
    )


# -------- Backend ZIP for Railway deployment --------
@api_router.get("/download-backend")
async def download_backend():
    zip_path = ROOT_DIR / "static" / "deploy" / "backend_railway.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Backend ZIP not found")
    return FileResponse(
        path=str(zip_path),
        filename="backend_railway.zip",
        media_type="application/zip"
    )


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def on_startup():
    try:
        await db.users.create_index("email", unique=True)
    except Exception:
        pass
    await seed_admin(db)
    # Single-user mode: ensure exactly one persona exists in DB
    count = await db.persone.count_documents({})
    if count == 0:
        await db.persone.insert_one({
            "id": str(uuid.uuid4()),
            "nome": "Mia attività",
            "color": "#2563EB",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
