#!/usr/bin/env python3
"""
marmi_app.py — Max Marmi Chat DB
Avvia con: python marmi_app.py
Poi apri: http://localhost:7860
"""

import os, sqlite3, json, re, hashlib, secrets, datetime
from pathlib import Path
from functools import wraps
from flask import (Flask, request, jsonify, session,
                   redirect, url_for, make_response)

try:
    import anthropic as _anthropic
    _AI_PKG = True
except ImportError:
    _AI_PKG = False

BASE_DIR = Path(__file__).parent
MARMI_DB = BASE_DIR / "marmi.db"
# Su Railway (o altri cloud) APP_DB va in /tmp per evitare problemi di permessi
_is_cloud = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER"))
APP_DB = Path("/tmp/marmi_app.db") if _is_cloud else BASE_DIR / "marmi_app.db"

app = Flask(__name__)
app.secret_key = "max-marmi-secret-2024-xK9pL"

# ── Inizializzazione client AI ─────────────────────────────────────────────
def _load_api_key():
    """Legge la API key: prima da env, poi dal file .anthropic_key."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        kf = BASE_DIR / ".anthropic_key"
        if kf.exists():
            key = kf.read_text().strip()
    # Scarta placeholder e chiavi troppo corte
    if key.startswith("sk-ant-") and len(key) > 40:
        return key
    return ""

def _get_ai_client():
    """Restituisce un client Anthropic fresco con la key aggiornata."""
    if not _AI_PKG:
        return None
    key = _load_api_key()
    if not key:
        return None
    return _anthropic.Anthropic(api_key=key)

AI_ENABLED = _AI_PKG and bool(_load_api_key())

# Schema DB esposto all'agente AI
_DB_SCHEMA = """
Database SQLite 'marmi.db' — archivio blocchi Max Marmi Carrara SRL (2012-2025)

Tabella `blocchi` (5.010 righe — una per blocco di marmo):
  anno          INTEGER   anno acquisto/lavorazione (2012–2025)
  numero_blocco INTEGER   numero identificativo blocco
  data_acquisto TEXT      data acquisto (testo)
  materiale     TEXT      tipo marmo (es. CALACATTA, STATUARIO, ARABESCATO VAGLI,
                          BIANCO CARRARA, BARDIGLIO, NERO MARQUINA, BOTTICINO…)
  fornitore     TEXT      nome fornitore/cava
  deposito      TEXT      luogo di deposito/stoccaggio
  stato         TEXT      stato: EVASO=venduto/lavorato, LASTRE=tagliato in lastre,
                          ROOT=in magazzino originale
  peso_ton      REAL      peso in tonnellate
  costo_blocco  REAL      costo acquisto blocco grezzo
  spese_trasporto REAL    spese di trasporto
  costo_finale  REAL      costo totale (costo_blocco + spese_trasporto + lavorazioni)
  totale_ricavi REAL      ricavi totali generati dal blocco
  differenze    REAL      margine = totale_ricavi - costo_finale (pos=profitto, neg=perdita)
  ha_lavorazioni INTEGER  1 se il blocco ha lavorazioni associate

Tabella `lavorazioni` (operazioni di trasformazione blocchi):
  blocco_id   INTEGER FK → blocchi.rowid
  tipo        TEXT    tipo operazione (SEGAGIONE, LUCIDATURA, RESINATURA, TAGLIO, …)
  mq          REAL    metri quadri lavorati
  prezzo      REAL    prezzo per mq
  totale_euro REAL    costo totale operazione

Tabella `ricavi` (dettaglio ricavi per blocco):
  blocco_id   INTEGER FK → blocchi.rowid
  tipo        TEXT    tipo ricavo
  mq          REAL    mq venduti
  prezzo      REAL    prezzo/mq
  totale_euro REAL    totale ricavo

NOTE IMPORTANTI:
- Usa sempre UPPER() o LIKE '%...%' per i nomi dei materiali (sono in maiuscolo nel DB)
- Per i filtri su materiale usa: UPPER(materiale) LIKE '%CALACATTA%'
- La colonna 'differenze' rappresenta il margine (profitto/perdita per blocco)
- Raggruppa sempre per anno o materiale nelle analisi aggregate
- Usa ROUND(val, 0) o ROUND(val, 2) per valori numerici
"""

_AI_SYSTEM = f"""Sei l'Agente AI di Max Marmi Carrara SRL, un assistente specializzato nell'analisi dell'archivio blocchi di marmo.

{_DB_SCHEMA}

REGOLE DI RISPOSTA:
1. Rispondi SEMPRE in italiano, in modo conversazionale e diretto
2. Rispondi alla domanda specifica — non fare riepiloghi generici se non richiesti
3. Per domande semplici (es. "qual è il fatturato totale?") rispondi con UNA frase + dato
4. Usa tabelle Markdown SOLO se ci sono più di 3 righe di dati da confrontare
5. Per confronti, classifiche, trend → usa la tabella
6. Formatta i numeri: €1.234.567 per valori monetari, virgola per decimali
7. Usa **grassetto** per evidenziare il dato chiave della risposta
8. Se la domanda è ambigua, fai una assunzione ragionevole e comunicala
9. Se non trovi dati, spiega perché con precisione e suggerisci come affinare la ricerca
10. Puoi incrociare più tabelle nella stessa query per rispondere a domande complesse
11. NON mostrare mai il codice SQL nella risposta finale
12. Sii conciso: non scrivere più di quello che serve per rispondere bene
"""

# ─────────────────────────────────────────────────────────────────────────────
# DB APPLICAZIONE (utenti, conversazioni, messaggi)
# ─────────────────────────────────────────────────────────────────────────────
def get_app_db():
    conn = sqlite3.connect(str(APP_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_app_db():
    conn = get_app_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        username     TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role         TEXT NOT NULL DEFAULT 'user',  -- 'admin' | 'user'
        created_at   TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS conversations (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER REFERENCES users(id),
        title      TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER REFERENCES conversations(id),
        role            TEXT NOT NULL,  -- 'user' | 'assistant'
        content         TEXT NOT NULL,
        created_at      TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_msgs_conv ON messages(conversation_id);
    CREATE INDEX IF NOT EXISTS idx_msgs_time ON messages(created_at);
    CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);
    """)
    conn.commit()

    # Crea utenti se non esistono
    USERS = [
        ("simone",  "Simone",  "admin"),
        ("giacomo", "Giacomo", "admin"),
        ("kevin",   "Kevin",   "admin"),
        ("matteo",  "Matteo",  "user"),
        ("marco",   "Marco",   "user"),
        ("luca",    "Luca",    "user"),
        ("sara",    "Sara",    "user"),
        ("andrea",  "Andrea",  "user"),
        ("paolo",   "Paolo",   "user"),
        ("chiara",  "Chiara",  "user"),
    ]
    # Password salvate in un file separato
    pwd_file = BASE_DIR / "credenziali_utenti.txt"
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        lines = ["MAX MARMI — CREDENZIALI UTENTI\n" + "="*40 + "\n\n"]
        for uname, dname, role in USERS:
            pwd = secrets.token_urlsafe(8)
            h   = hashlib.sha256(pwd.encode()).hexdigest()
            conn.execute("INSERT OR IGNORE INTO users (username,display_name,password_hash,role) VALUES (?,?,?,?)",
                         (uname, dname, h, role))
            tag = "👑 ADMIN" if role == "admin" else "👤 Utente"
            lines.append(f"{tag}\n  Username : {uname}\n  Password : {pwd}\n\n")
        conn.commit()
        pwd_file.write_text("".join(lines), encoding="utf-8")
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# AUTH helpers
# ─────────────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login")
        if session.get("role") != "admin":
            return redirect("/")
        return f(*args, **kwargs)
    return decorated

def current_user():
    return {"id": session["user_id"], "display_name": session["display_name"],
            "role": session["role"], "username": session["username"]}

# ─────────────────────────────────────────────────────────────────────────────
# QUERY ENGINE (linguaggio naturale → SQL su marmi.db)
# ─────────────────────────────────────────────────────────────────────────────

# Materiali noti nel DB – usati per rilevamento diretto nel testo
KNOWN_MATERIALS = [
    "calacatta", "statuario", "arabescato", "bianco carrara", "bardiglio",
    "nero marquina", "botticino", "travertino", "emperador", "marfil",
    "crema marfil", "bianco", "nero", "rosa portogallo", "verde alpi",
    "giallo siena", "rosso verona", "breccia", "onyx", "onice",
    "calacata", "statuar", "arabesc", "carrara", "portoro",
    "grigio carnico", "fior di pesco", "fossil", "limestone",
    "oyster", "calacatta oyster", "pietra", "quarzo",
]

# Pattern (regex, sql_template, titolo)
# Nota: {WHERE} verrà sostituito con i filtri dinamici di anno/materiale
QUERY_MAP = [
    # ── Conteggio blocchi ──────────────────────────────────────────────────
    (r'quanti\s+blocchi|numero\s+(?:di\s+)?blocchi|totale\s+blocchi|conteggio\s+blocchi|blocchi.{0,30}(?:ci\s+sono|hai|abbiamo|ho|erano)',
     "SELECT materiale, COUNT(*) blocchi, ROUND(AVG(totale_ricavi),0) ricavo_medio, ROUND(SUM(differenze),0) margine_totale FROM blocchi {WHERE} GROUP BY materiale ORDER BY blocchi DESC",
     "Blocchi per materiale"),

    (r'blocchi.{0,20}(?:anno|per\s+anno|ogni\s+anno)|quanti.{0,15}(?:ogni|per)\s+anno',
     "SELECT anno, COUNT(*) blocchi, ROUND(SUM(totale_ricavi),0) fatturato, ROUND(SUM(differenze),0) margine FROM blocchi {WHERE} GROUP BY anno ORDER BY anno",
     "Blocchi per anno"),

    # ── Fatturato / Ricavi ────────────────────────────────────────────────
    (r'(?:fatturato|ricavi?|vendite?|guadagnato|incassato|entrate).{0,30}(?:materiale|tipo|marmo|pietra|per\s+(?:tipo|material))',
     "SELECT materiale, COUNT(*) n, ROUND(AVG(totale_ricavi),0) ricavo_medio, ROUND(SUM(totale_ricavi),0) fatturato_totale, ROUND(AVG(differenze),0) margine_medio FROM blocchi {WHERE_MAT} WHERE totale_ricavi>0 GROUP BY materiale ORDER BY fatturato_totale DESC",
     "Ricavi per materiale"),

    (r'(?:fatturato|ricavi?|vendite?|guadagnato|incassato|entrate).{0,30}(?:anno|per\s+anno|ogni\s+anno|annuale|annuo)|andamento.{0,20}(?:fatturato|ricavi?|vendite?)|trend.{0,20}(?:fatturato|ricavi?)|come\s+(?:sta\s+andando|è\s+andata|siamo\s+messi|è\s+andato)',
     "SELECT anno, COUNT(*) blocchi, ROUND(SUM(totale_ricavi),0) fatturato, ROUND(SUM(costo_finale),0) costi, ROUND(SUM(differenze),0) margine, ROUND(AVG(differenze),0) margine_medio_blocco FROM blocchi {WHERE} GROUP BY anno ORDER BY anno",
     "Riepilogo annuale"),

    (r'(?:fatturato|ricavi?|guadagnato|incassato)\s+(?:totale|complessivo|in\s+tutto|di\s+tutto|globale)|totale\s+(?:fatturato|ricavi?)|quanto\s+(?:abbiamo\s+fatto|ha\s+fatto|abbiamo\s+guadagnato|è\s+il\s+fatturato)',
     "SELECT anno, COUNT(*) blocchi, ROUND(SUM(totale_ricavi),0) fatturato, ROUND(SUM(costo_finale),0) costi, ROUND(SUM(differenze),0) margine FROM blocchi {WHERE} GROUP BY anno ORDER BY anno",
     "Riepilogo fatturato totale"),

    # ── Margine / Profitto ────────────────────────────────────────────────
    (r'(?:margine|profitto|guadagno|utile|differenz).{0,30}(?:materiale|tipo|marmo)',
     "SELECT materiale, COUNT(*) n, ROUND(SUM(differenze),0) margine_totale, ROUND(AVG(differenze),0) margine_medio, ROUND(SUM(totale_ricavi),0) fatturato FROM blocchi {WHERE_MAT} WHERE differenze IS NOT NULL GROUP BY materiale ORDER BY margine_totale DESC",
     "Margine per materiale"),

    (r'(?:margine|profitto|guadagno|utile|differenz).{0,30}(?:anno|annuale|per\s+anno)',
     "SELECT anno, COUNT(*) blocchi, ROUND(SUM(totale_ricavi),0) fatturato, ROUND(SUM(differenze),0) margine, ROUND(AVG(differenze),0) margine_medio_blocco FROM blocchi {WHERE} GROUP BY anno ORDER BY anno",
     "Margine per anno"),

    # ── Migliore anno / materiale ─────────────────────────────────────────
    (r'(?:miglior|migliore|anno\s+(?:migliore|con\s+più|record|top)|qual\s+è\s+(?:stato\s+)?l.anno)',
     "SELECT anno, COUNT(*) blocchi, ROUND(SUM(totale_ricavi),0) fatturato, ROUND(SUM(differenze),0) margine FROM blocchi {WHERE} GROUP BY anno ORDER BY fatturato DESC",
     "Anno per fatturato (migliore in cima)"),

    # ── Fornitori ─────────────────────────────────────────────────────────
    (r'fornitore|fornitor|supplier|vendor|da\s+chi\s+(?:acquist|compri|compra)|da\s+quale\s+(?:fornitore|azienda)',
     "SELECT fornitore, COUNT(*) blocchi, ROUND(SUM(totale_ricavi),0) fatturato, ROUND(SUM(differenze),0) margine_totale, ROUND(AVG(peso_ton),1) peso_medio FROM blocchi {WHERE_MAT} WHERE fornitore IS NOT NULL GROUP BY fornitore ORDER BY fatturato DESC LIMIT 25",
     "Performance per fornitore"),

    # ── Deposito ─────────────────────────────────────────────────────────
    (r'deposito|magazzino|dove\s+(?:sono|stanno|si\s+trovano)|ubicazion|locazion|storage',
     "SELECT deposito, COUNT(*) blocchi, ROUND(SUM(differenze),0) margine_totale, ROUND(AVG(peso_ton),1) peso_medio FROM blocchi {WHERE_MAT} WHERE deposito IS NOT NULL GROUP BY deposito ORDER BY blocchi DESC",
     "Blocchi per deposito"),

    # ── Top / Bottom ──────────────────────────────────────────────────────
    (r'(?:top|miglior[ei]|più\s+profittevol|più\s+redditizi|maggior\s+(?:margine|profitto|guadagno))',
     "SELECT anno, numero_blocco, materiale, fornitore, ROUND(peso_ton,1) peso, ROUND(totale_ricavi,0) ricavi, ROUND(differenze,0) margine FROM blocchi {WHERE_MAT} WHERE differenze IS NOT NULL ORDER BY differenze DESC LIMIT 20",
     "Top 20 blocchi più profittevoli"),

    (r'(?:perdita|perdite|in\s+rosso|negativ|peggiori?|meno\s+profittevol|sotto\s+(?:costo|zero))',
     "SELECT anno, numero_blocco, materiale, fornitore, ROUND(costo_finale,0) costo, ROUND(totale_ricavi,0) ricavi, ROUND(differenze,0) perdita FROM blocchi {WHERE_MAT} WHERE differenze<0 ORDER BY differenze ASC LIMIT 25",
     "Blocchi in perdita"),

    # ── Lavorazioni ───────────────────────────────────────────────────────
    (r'lavorazion|segagion|lucidatur|resinatur|lavora|lavorat|transform',
     "SELECT tipo, COUNT(*) operazioni, ROUND(SUM(totale_euro),0) costo_totale, ROUND(AVG(totale_euro),0) costo_medio FROM lavorazioni GROUP BY tipo ORDER BY costo_totale DESC",
     "Costi di lavorazione"),

    # ── Stato ─────────────────────────────────────────────────────────────
    (r'stato|evaso|(?:blocchi\s+)?(?:ancora\s+)?(?:in\s+stock|disponibili?|rimanenti?|non\s+evasi)',
     "SELECT stato, COUNT(*) blocchi, ROUND(AVG(totale_ricavi),0) ricavo_medio, ROUND(SUM(differenze),0) margine_totale FROM blocchi {WHERE_MAT} GROUP BY stato",
     "Blocchi per stato"),

    # ── Peso ─────────────────────────────────────────────────────────────
    (r'peso|tonn|kg\b|tonnellat|quant.{0,10}(?:pesa|pesano)|dimensi',
     "SELECT materiale, COUNT(*) n, ROUND(AVG(peso_ton),2) peso_medio, ROUND(MIN(peso_ton),2) min_ton, ROUND(MAX(peso_ton),2) max_ton, ROUND(SUM(peso_ton),1) totale_ton FROM blocchi {WHERE_MAT} WHERE peso_ton IS NOT NULL GROUP BY materiale ORDER BY peso_medio DESC",
     "Peso per materiale"),

    # ── Costi ─────────────────────────────────────────────────────────────
    (r'costo|costi|quanto\s+(?:costano?|abbiamo\s+(?:speso|pagato|investito))|spes[ae]|investimento',
     "SELECT materiale, COUNT(*) n, ROUND(AVG(costo_finale),0) costo_medio, ROUND(SUM(costo_finale),0) costo_totale FROM blocchi {WHERE_MAT} WHERE costo_finale IS NOT NULL GROUP BY materiale ORDER BY costo_totale DESC",
     "Costi per materiale"),

    # ── Riepilogo generale ────────────────────────────────────────────────
    (r'riepilogo|riassunto|sommario|panoramica|sintesi|overview|tutto|generale|statistiche?|dati|report|bilancio',
     "SELECT anno, COUNT(*) blocchi, ROUND(SUM(totale_ricavi),0) fatturato, ROUND(SUM(differenze),0) margine FROM blocchi {WHERE} GROUP BY anno ORDER BY anno",
     "Riepilogo 2012–2025"),
]

RE_ANNO    = re.compile(r'\b(20\d{2})\b')
RE_RANGE   = re.compile(r'\b(20\d{2})\b.{0,20}\b(20\d{2})\b')

def detect_material(text):
    """Rileva il nome del materiale nel testo, sia via regex che via lista known."""
    t_up = text.upper()
    # Check known materials first (longest match wins)
    found = []
    for m in KNOWN_MATERIALS:
        if m.upper() in t_up:
            found.append(m.upper())
    if found:
        return max(found, key=len)  # longest match
    # Fallback regex: dopo parole-chiave contesto
    m = re.search(r'(?:di|del|dello|della|dei|degli|il|la|lo|tipo|materiale|marmo|pietra)\s+([A-Za-z][A-Za-z\s]{2,24}?)(?:\s+nel|\s+per|\s+del|\s+nel|\s+anno|\?|$)', text, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip().upper()
        if len(candidate) >= 3:
            return candidate
    return None

def build_where(anni, material):
    """Costruisce la clausola WHERE e i parametri per anno e materiale."""
    parts, params = [], []
    if len(anni) == 1:
        parts.append("anno=?"); params.append(int(anni[0]))
    elif len(anni) == 2:
        a1, a2 = sorted(int(a) for a in anni)
        parts.append("anno BETWEEN ? AND ?"); params += [a1, a2]
    if material:
        parts.append("UPPER(materiale) LIKE ?"); params.append(f'%{material}%')
    if not parts:
        return "", []
    return "WHERE " + " AND ".join(parts), params

def inject_where(sql, where_clause, is_mat_only=False):
    """Inserisce la WHERE clause nell'SQL in modo sicuro."""
    if not where_clause:
        return sql
    # Remove the placeholder
    if "{WHERE_MAT}" in sql:
        # {WHERE_MAT}: used in queries that already have their own WHERE
        sql = sql.replace("{WHERE_MAT} WHERE", f"{where_clause} AND")
    elif "{WHERE}" in sql:
        sql = sql.replace("{WHERE}", where_clause)
    else:
        # Fallback: inject before GROUP BY or ORDER BY or LIMIT
        for keyword in ["GROUP BY", "ORDER BY", "LIMIT"]:
            if keyword in sql:
                sql = sql.replace(keyword, where_clause + " " + keyword, 1)
                return sql
        sql += " " + where_clause
    return sql

def rows_to_list(rows):
    return [dict(r) for r in rows]

def fmt_val(col, v):
    MONEY = {'ricav','marg','fatt','cost','euro','perd','perdita','tot','invest','spesa'}
    if v is None: return "–"
    if isinstance(v,(int,float)) and any(m in col.lower() for m in MONEY):
        return f"€{v:,.0f}"
    return str(v)

def natural_query(text):
    conn = sqlite3.connect(str(MARMI_DB))
    conn.row_factory = sqlite3.Row
    t = text.lower()

    # ── 1. Blocco specifico per numero ──────────────────────────────────────
    m = re.search(r'\b(?:BL\.?\s*|blocco\s+)(\d{3,5})\b', text, re.IGNORECASE)
    if m:
        nr = int(m.group(1))
        rows = conn.execute("""
            SELECT anno, numero_blocco, data_acquisto, materiale, fornitore,
                   deposito, stato, ROUND(peso_ton,2) peso_ton,
                   ROUND(costo_blocco,0) costo_blocco, ROUND(costo_finale,0) costo_finale,
                   ROUND(totale_ricavi,0) ricavi, ROUND(differenze,0) margine
            FROM blocchi WHERE numero_blocco=?""", (nr,)).fetchall()
        conn.close()
        if rows: return rows_to_list(rows), f"Scheda blocco BL {nr}"
        # Try partial match
        rows = conn.execute("SELECT anno, numero_blocco, materiale, fornitore, ROUND(totale_ricavi,0) ricavi, ROUND(differenze,0) margine FROM blocchi WHERE CAST(numero_blocco AS TEXT) LIKE ?", (f'%{nr}%',)).fetchall()
        conn.close()
        if rows: return rows_to_list(rows), f"Blocchi con numero simile a {nr}"
        return [], f"Nessun blocco trovato con numero {nr}"

    # ── 2. Estrai filtri anno e materiale ────────────────────────────────────
    anni = RE_ANNO.findall(text)
    material = detect_material(text)
    where_clause, params = build_where(anni, material)

    # ── 3. Pattern matching ──────────────────────────────────────────────────
    for pattern, sql_tpl, title in QUERY_MAP:
        if re.search(pattern, t, re.IGNORECASE):
            sql = inject_where(sql_tpl, where_clause)
            try:
                rows = conn.execute(sql, params).fetchall()
                conn.close()
                if rows:
                    return rows_to_list(rows), title
            except Exception:
                pass

    # ── 4. Fallback intelligente ─────────────────────────────────────────────
    # Se abbiamo filtri attivi, proviamo una query generale filtrata
    if anni or material:
        sql = "SELECT anno, numero_blocco, materiale, fornitore, ROUND(peso_ton,1) peso, ROUND(totale_ricavi,0) ricavi, ROUND(differenze,0) margine FROM blocchi " + (where_clause or "") + " ORDER BY totale_ricavi DESC LIMIT 30"
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        if rows:
            lbl = []
            if material: lbl.append(material.title())
            if anni: lbl.append(f"anno{'i' if len(anni)>1 else ''} {', '.join(anni)}")
            return rows_to_list(rows), "Blocchi — " + " · ".join(lbl)
        return [], "Nessun blocco trovato con questi criteri"

    # Fallback: riepilogo annuale
    rows = conn.execute(
        "SELECT anno, COUNT(*) blocchi, ROUND(SUM(totale_ricavi),0) fatturato, ROUND(SUM(differenze),0) margine FROM blocchi GROUP BY anno ORDER BY anno"
    ).fetchall()
    conn.close()
    return rows_to_list(rows), "Riepilogo generale 2012–2025"


def format_answer(rows, title, question=""):
    if not rows:
        q = question.lower()
        hints = []
        if any(m in q for m in ["calacatt","statuar","arabes","bianco","carrara","nero","bardig"]):
            hints.append("il materiale potrebbe avere un nome leggermente diverso nel DB")
        if re.search(r"20\d{2}", q):
            hints.append("verifica che l'anno sia compreso tra 2012 e 2025")
        base = "Non ho trovato dati per questa richiesta."
        if hints:
            base += " Nota: " + "; ".join(hints) + "."
        base += " Prova a riformulare, oppure clicca uno dei suggerimenti nella chat."
        return base

    cols = list(rows[0].keys())
    n    = len(rows)

    # 1. Blocco singolo — risposta narrativa
    if n == 1 and "numero_blocco" in cols:
        r    = rows[0]
        mat  = r.get("materiale","N/D")
        forn = r.get("fornitore","N/D")
        anno = r.get("anno","")
        peso = r.get("peso_ton","")
        costo= r.get("costo_finale") or r.get("costo_blocco")
        ric  = r.get("ricavi") or r.get("totale_ricavi")
        diff = r.get("margine") or r.get("differenze")
        stato= r.get("stato","")
        dep  = r.get("deposito","")
        txt  = f"Il blocco **BL {r.get('numero_blocco','')}** è di **{mat}**, acquistato nel **{anno}**"
        if forn and forn != "N/D": txt += f" dal fornitore **{forn}**"
        txt += "."
        if dep:  txt += f" Deposito: **{dep}**."
        if peso:
            try: txt += f" Peso: **{float(peso):,.2f} t**."
            except: pass
        if costo:
            try: txt += f" Costo: **€{float(costo):,.0f}**."
            except: pass
        if ric:
            try: txt += f" Ricavi: **€{float(ric):,.0f}**."
            except: pass
        if diff:
            try:
                d = float(diff)
                esito = "positivo" if d >= 0 else "negativo"
                txt += f" Margine ({esito}): **€{d:,.0f}**."
            except: pass
        if stato: txt += f" Stato: _{stato}_."
        return txt

    # 2. Più blocchi specifici (<=5) — elenco compatto
    if "numero_blocco" in cols and n <= 5:
        parts = []
        for r in rows:
            bl  = r.get("numero_blocco","")
            mat = r.get("materiale","")
            ann = r.get("anno","")
            ric = r.get("ricavi") or r.get("totale_ricavi")
            mg  = r.get("margine") or r.get("differenze")
            line = f"**BL {bl}** — {mat}, {ann}"
            if ric:
                try: line += f", ricavi €{float(ric):,.0f}"
                except: pass
            if mg:
                try: line += f", margine €{float(mg):,.0f}"
                except: pass
            parts.append(line)
        return "\n\n".join(parts)

    # 3. Riepilogo annuale
    if "anno" in cols and "fatturato" in cols:
        tot_f = sum(r.get("fatturato") or 0 for r in rows)
        tot_m = sum(r.get("margine") or 0 for r in rows)
        best  = max(rows, key=lambda r: r.get("fatturato") or 0)
        worst = min(rows, key=lambda r: r.get("fatturato") or 0)
        intro = (f"In **{n} anni** analizzati il fatturato complessivo è stato **€{tot_f:,.0f}** "
                 f"con margine totale **€{tot_m:,.0f}**. "
                 f"Anno migliore: **{best.get('anno')}** (€{(best.get('fatturato') or 0):,.0f}), "
                 f"anno con meno fatturato: **{worst.get('anno')}** (€{(worst.get('fatturato') or 0):,.0f}).")

    # 4. Materiale + fatturato
    elif "materiale" in cols and ("fatturato_totale" in cols or "ricavo_medio" in cols):
        best = rows[0]
        fatt = best.get("fatturato_totale") or best.get("ricavo_medio") or 0
        tot  = sum(r.get("fatturato_totale") or r.get("ricavo_medio") or 0 for r in rows)
        try:
            perc = float(fatt)/float(tot)*100 if tot else 0
            intro = (f"Su **{n} materiali**, il più redditizio è **{best.get('materiale')}** "
                     f"con **€{float(fatt):,.0f}** ({perc:.0f}% del totale). "
                     f"Fatturato totale combinato: **€{tot:,.0f}**.")
        except:
            intro = f"Il materiale più redditizio è **{best.get('materiale')}**."

    # 5. Materiale + conteggio
    elif "materiale" in cols and "blocchi" in cols:
        best   = rows[0]
        tot_bl = sum(r.get("blocchi") or 0 for r in rows)
        intro  = (f"L'archivio ha **{tot_bl} blocchi** distribuiti in **{n} materiali**. "
                  f"Il più presente è **{best.get('materiale')}** con **{best.get('blocchi')} blocchi**.")

    # 6. Fornitore
    elif "fornitore" in cols:
        best   = rows[0]
        tot_bl = sum(r.get("blocchi") or 0 for r in rows)
        fatt   = best.get("fatturato") or best.get("margine_totale") or 0
        try:
            intro = (f"Lavorato con **{n} fornitori** per **{tot_bl} blocchi** totali. "
                     f"Il fornitore principale è **{best.get('fornitore')}** "
                     f"con **€{float(fatt):,.0f}** di fatturato.")
        except:
            intro = f"Hai lavorato con **{n} fornitori**."

    # 7. Blocchi con margine (top/bottom)
    elif "numero_blocco" in cols and ("margine" in cols or "differenze" in cols):
        best    = rows[0]
        mc      = "margine" if "margine" in cols else "differenze"
        marg_v  = best.get(mc) or 0
        negativi= sum(1 for r in rows if (r.get(mc) or 0) < 0)
        try:
            if float(marg_v) >= 0:
                intro = (f"Top **{n} blocchi** per profittabilità. "
                         f"Il migliore è **BL {best.get('numero_blocco')}** ({best.get('materiale','')}) "
                         f"con **€{float(marg_v):,.0f}** di margine. "
                         f"{negativi} blocchi in questa lista sono in perdita.")
            else:
                intro = (f"**{n} blocchi in perdita**. "
                         f"Il peggiore è **BL {best.get('numero_blocco')}** ({best.get('materiale','')}) "
                         f"con **–€{abs(float(marg_v)):,.0f}**.")
        except:
            intro = f"Elenco {n} blocchi."

    # 8. Lavorazioni
    elif "tipo" in cols and "costo_totale" in cols:
        best     = rows[0]
        tot_op   = sum(r.get("operazioni") or 0 for r in rows)
        tot_cost = sum(r.get("costo_totale") or 0 for r in rows)
        try:
            intro = (f"**{tot_op} operazioni** di lavorazione per un costo totale di **€{float(tot_cost):,.0f}**. "
                     f"La tipologia più costosa è **{best.get('tipo')}** (€{float(best.get('costo_totale',0)):,.0f}).")
        except:
            intro = f"Lavorazioni suddivise per {n} tipologie."

    # 9. Stato
    elif "stato" in cols and "blocchi" in cols:
        tot_b = sum(r.get("blocchi") or 0 for r in rows)
        intro = f"L'archivio ha **{tot_b} blocchi totali** in {n} categorie di stato."

    # 10. Deposito
    elif "deposito" in cols and "blocchi" in cols:
        best  = rows[0]
        tot_b = sum(r.get("blocchi") or 0 for r in rows)
        intro = (f"Blocchi in **{n} depositi**, totale **{tot_b}**. "
                 f"Il deposito principale è **{best.get('deposito')}** ({best.get('blocchi')} blocchi).")

    # 11. Peso
    elif "peso_medio" in cols:
        best  = rows[0]
        tot_t = sum(r.get("totale_ton") or 0 for r in rows)
        try:
            intro = (f"Peso medio più alto: **{best.get('materiale')}** a **{best.get('peso_medio')} t/blocco**. "
                     f"Peso totale archivio: **{tot_t:,.0f} t**.")
        except:
            intro = "Analisi peso per materiale."

    # 12. Costi per materiale
    elif "costo_totale" in cols and "materiale" in cols:
        best  = rows[0]
        tot_c = sum(r.get("costo_totale") or 0 for r in rows)
        try:
            intro = (f"Investimento totale: **€{float(tot_c):,.0f}**. "
                     f"Materiale con costo più alto: **{best.get('materiale')}** (€{float(best.get('costo_totale',0)):,.0f}).")
        except:
            intro = "Analisi costi per materiale."

    # Fallback
    else:
        intro = f"Ho trovato **{n} risultati** per la tua richiesta."

    # Tabella Markdown
    hdr  = "| " + " | ".join(c.replace("_"," ") for c in cols) + " |"
    sep  = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join("| " + " | ".join(fmt_val(c, r.get(c)) for c in cols) + " |" for r in rows)
    return f"{intro}\n\n{hdr}\n{sep}\n{body}"

def ai_natural_query(question, history=None):
    """
    Usa Claude API con tool use per rispondere in modo conversazionale.
    Claude genera l'SQL, lo esegue, interpreta i risultati e risponde in italiano.
    """
    client = _get_ai_client()
    if not client:
        rows, title = natural_query(question)
        return format_answer(rows, title, question)

    history = history or []

    tools = [{
        "name": "execute_sql",
        "description": (
            "Esegue una query SQL SELECT sul database marmi.db e restituisce i risultati in JSON. "
            "Usa SOLO query SELECT. Limita a max 100 righe con LIMIT. "
            "JOIN tra tabelle: blocchi.rowid = lavorazioni.blocco_id = ricavi.blocco_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Query SQL SQLite valida (solo SELECT)"}
            },
            "required": ["query"]
        }
    }]

    # Costruisce messaggi con contesto storico (alternanza user/assistant garantita)
    msgs = []
    for h in (history or [])[-6:]:
        role = "user" if h.get("role") == "user" else "assistant"
        content = h.get("content", "")
        if content and len(content) < 3000:
            # Evita messaggi consecutivi dello stesso ruolo
            if msgs and msgs[-1]["role"] == role:
                continue
            msgs.append({"role": role, "content": content})
    # L'ultimo messaggio deve essere sempre l'utente corrente
    if msgs and msgs[-1]["role"] == "user":
        msgs.pop()  # rimuovi per evitare doppio user
    msgs.append({"role": "user", "content": question})

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        system=_AI_SYSTEM,
        tools=tools,
        messages=msgs
    )

    # Agentic loop
    for _ in range(5):
        if resp.stop_reason != "tool_use":
            break

        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            sql = block.input.get("query", "").strip()
            if not sql.upper().lstrip().startswith("SELECT"):
                result_str = "Errore: solo query SELECT permesse."
            else:
                try:
                    conn = sqlite3.connect(str(MARMI_DB))
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(sql).fetchall()
                    conn.close()
                    data = [dict(r) for r in rows[:100]]
                    result_str = json.dumps(data, ensure_ascii=False, default=str)
                except Exception as e:
                    result_str = f"Errore SQL: {str(e)}"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str
            })

        msgs = msgs + [
            {"role": "assistant", "content": resp.content},
            {"role": "user",      "content": tool_results}
        ]
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            system=_AI_SYSTEM,
            tools=tools,
            messages=msgs
        )

    text_parts = [b.text for b in resp.content if hasattr(b, "text") and b.text]
    answer = "\n".join(text_parts).strip()
    if not answer:
        rows, title = natural_query(question)
        answer = format_answer(rows, title, question)
    return answer

def build_context(history):
    """Costruisce il contesto degli ultimi N turni per la risposta."""
    if not history:
        return ""
    lines = []
    for m in history[-6:]:  # ultimi 6 messaggi = 3 turni
        role = "Tu" if m["role"] == "user" else "Assistente"
        lines.append(f"{role}: {m['content'][:200]}")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — AUTH
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — CHAT
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    u = current_user()
    conn = get_app_db()
    convs = conn.execute(
        "SELECT id, title, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC",
        (u["id"],)).fetchall()
    conn.close()
    return render_chat([dict(c) for c in convs], u)

@app.route("/api/new_conversation", methods=["POST"])
@login_required
def new_conversation():
    u = current_user()
    conn = get_app_db()
    cur = conn.execute("INSERT INTO conversations (user_id, title) VALUES (?,?)",
                       (u["id"], "Nuova chat"))
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return jsonify(id=cid, title="Nuova chat")

@app.route("/api/conversation/<int:cid>")
@login_required
def get_conversation(cid):
    u = current_user()
    conn = get_app_db()
    # Admin può vedere tutte
    if u["role"] == "admin":
        conv = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    else:
        conv = conn.execute("SELECT * FROM conversations WHERE id=? AND user_id=?",
                            (cid, u["id"])).fetchone()
    if not conv:
        return jsonify(error="Non trovata"), 404
    msgs = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE conversation_id=? ORDER BY id",
        (cid,)).fetchall()
    conn.close()
    return jsonify(messages=[dict(m) for m in msgs])

@app.route("/api/ask", methods=["POST"])
@login_required
def ask():
    u  = current_user()
    data = request.json or {}
    q    = data.get("q", "").strip()
    cid  = data.get("conversation_id")
    if not q: return jsonify(error="Domanda vuota"), 400

    conn = get_app_db()

    # Crea conversazione se non esiste
    if not cid:
        cur = conn.execute("INSERT INTO conversations (user_id,title) VALUES (?,?)",
                           (u["id"], q[:50]))
        conn.commit()
        cid = cur.lastrowid
    else:
        # Aggiorna titolo se primo messaggio
        first = conn.execute("SELECT COUNT(*) cnt FROM messages WHERE conversation_id=?",
                             (cid,)).fetchone()["cnt"]
        if first == 0:
            conn.execute("UPDATE conversations SET title=? WHERE id=?", (q[:50], cid))

    # Salva messaggio utente
    conn.execute("INSERT INTO messages (conversation_id,role,content) VALUES (?,?,?)",
                 (cid, "user", q))
    # Aggiorna timestamp conv
    conn.execute("UPDATE conversations SET updated_at=datetime('now') WHERE id=?", (cid,))
    conn.commit()

    # Genera risposta
    history = [dict(m) for m in conn.execute(
        "SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 10",
        (cid,)).fetchall()]
    history.reverse()

    try:
        answer = ai_natural_query(q, history)
    except Exception as e:
        err_msg = str(e)
        # Mostra errore di autenticazione direttamente
        if "authentication" in err_msg.lower() or "api_key" in err_msg.lower() or "401" in err_msg:
            answer = ("⚠️ **API key non valida o mancante.** "
                      "Controlla il file `.anthropic_key` nella cartella dell'app e inserisci una chiave valida.")
        elif "credit" in err_msg.lower() or "billing" in err_msg.lower():
            answer = ("⚠️ **Credito esaurito sull'account Anthropic.** "
                      "Ricarica il credito su console.anthropic.com per usare l'Agente AI.")
        else:
            # Fallback silenzioso al sistema regex
            rows, title = natural_query(q)
            answer = format_answer(rows, title, q)

    # Salva risposta
    conn.execute("INSERT INTO messages (conversation_id,role,content) VALUES (?,?,?)",
                 (cid, "assistant", answer))
    conn.commit()
    conn.close()

    return jsonify(answer=answer, conversation_id=cid, title=q[:50])

@app.route("/api/conversations")
@login_required
def list_conversations():
    u = current_user()
    conn = get_app_db()
    convs = conn.execute(
        "SELECT id, title, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC",
        (u["id"],)).fetchall()
    conn.close()
    return jsonify([dict(c) for c in convs])

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — ADMIN
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin():
    return ADMIN_HTML

@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    conn = get_app_db()
    # Messaggi totali
    total_msgs   = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    total_convs  = conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"]
    total_users  = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    # Messaggi ultimi 7 giorni per giorno
    days7 = conn.execute("""
        SELECT date(created_at) day, COUNT(*) msgs
        FROM messages WHERE created_at >= datetime('now','-7 days')
        GROUP BY day ORDER BY day
    """).fetchall()
    # Messaggi per utente
    per_user = conn.execute("""
        SELECT u.display_name, u.role, COUNT(m.id) msgs,
               MAX(m.created_at) last_active
        FROM users u
        LEFT JOIN conversations c ON c.user_id=u.id
        LEFT JOIN messages m ON m.conversation_id=c.id
        GROUP BY u.id ORDER BY msgs DESC
    """).fetchall()
    # Top query (primi 100 messaggi utente)
    top_q = conn.execute("""
        SELECT content, COUNT(*) n FROM messages
        WHERE role='user' GROUP BY content ORDER BY n DESC LIMIT 10
    """).fetchall()
    conn.close()
    return jsonify(
        total_messages=total_msgs,
        total_conversations=total_convs,
        total_users=total_users,
        messages_per_day=[dict(r) for r in days7],
        per_user=[dict(r) for r in per_user],
        top_queries=[dict(r) for r in top_q]
    )

@app.route("/api/admin/conversations")
@admin_required
def admin_conversations():
    conn = get_app_db()
    convs = conn.execute("""
        SELECT c.id, u.display_name user, c.title,
               c.created_at, c.updated_at,
               COUNT(m.id) n_messages
        FROM conversations c
        JOIN users u ON u.id=c.user_id
        LEFT JOIN messages m ON m.conversation_id=c.id
        GROUP BY c.id ORDER BY c.updated_at DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(c) for c in convs])

# ─────────────────────────────────────────────────────────────────────────────
# HTML TEMPLATES — Design pulito, bianco, elegante
# ─────────────────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8"><title>Max Marmi — Accesso</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f4f0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.wrap{width:100%;max-width:400px;padding:24px}
.card{background:#fff;border-radius:16px;padding:44px 40px;box-shadow:0 1px 3px rgba(0,0,0,.06),0 8px 32px rgba(0,0,0,.08)}
.brand{text-align:center;margin-bottom:36px}
.brand-name{font-size:1.1rem;font-weight:700;color:#1d1d1f;letter-spacing:-.02em}
.brand-sub{font-size:.8rem;color:#9ca3af;margin-top:4px;letter-spacing:.04em;text-transform:uppercase}
.divider{width:32px;height:2px;background:#111;margin:16px auto}
label{display:block;font-size:.78rem;font-weight:600;color:#6b7280;letter-spacing:.05em;text-transform:uppercase;margin-bottom:6px;margin-top:20px}
input{width:100%;padding:11px 14px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:.95rem;color:#1d1d1f;outline:none;transition:border-color .2s,box-shadow .2s;background:#fff}
input:focus{border-color:#1d1d1f;box-shadow:0 0 0 3px rgba(17,17,17,.06)}
.btn{width:100%;margin-top:28px;padding:12px;background:#1d1d1f;color:#fff;border:none;border-radius:8px;font-size:.9rem;font-weight:600;cursor:pointer;letter-spacing:.01em;transition:background .2s}
.btn:hover{background:#333}
.err{background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;border-radius:8px;padding:10px 14px;font-size:.85rem;margin-top:16px;display:none}
.err.show{display:block}
</style>
</head>
<body>
<div class="wrap">
<div class="card">
  <div class="brand">
    <div class="brand-name">Max Marmi Carrara</div>
    <div class="divider"></div>
    <div class="brand-sub">Agente AI — Archivio Blocchi</div>
  </div>
  <form method="POST" action="/login">
    <label>Username</label>
    <input name="username" type="text" placeholder="es. simone" autocomplete="username">
    <label>Password</label>
    <input name="password" type="password" placeholder="••••••••" autocomplete="current-password">
    <button class="btn" type="submit">Accedi</button>
    <div class="err SHOW_ERR">ERROR_MSG</div>
  </form>
</div>
</div>
</body>
</html>"""

def render_login(error=""):
    html = LOGIN_HTML
    if error:
        html = html.replace("class=\"err SHOW_ERR\"","class=\"err show\"")
        html = html.replace("ERROR_MSG", error)
    else:
        html = html.replace(" SHOW_ERR","").replace("ERROR_MSG","")
    return html

def render_chat(convs, user):
    convs_json = json.dumps(convs, ensure_ascii=False)
    is_admin   = "true" if user["role"] == "admin" else "false"
    ai_badge   = "AI · Powered by Claude" if AI_ENABLED else "Modalità base"
    return CHAT_HTML.replace("{{USER_NAME}}", user["display_name"]) \
                    .replace("{{IS_ADMIN}}", is_admin) \
                    .replace("{{CONVS_JSON}}", convs_json) \
                    .replace("{{AI_BADGE}}", ai_badge)

CHAT_HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8"><title>Agente AI — Max Marmi</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{
  --bg:#ffffff;
  --sidebar:#fafafa;
  --border:#e5e7eb;
  --text:#1d1d1f;
  --muted:#6b7280;
  --light:#f3f4f6;
  --accent:#1d1d1f;
  --accent-light:#f3f4f6;
  --bubble-user:#1d1d1f;
  --bubble-bot:#f9fafb;
}
*{box-sizing:border-box;margin:0;padding:0;scrollbar-width:thin;scrollbar-color:#d1d5db transparent}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Inter',sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;overflow:hidden;font-size:15px}

/* SIDEBAR */
#sidebar{width:256px;min-width:256px;background:var(--sidebar);border-right:1px solid var(--border);display:flex;flex-direction:column}
.sb-top{padding:18px 16px 12px}
.sb-brand{font-size:.82rem;font-weight:700;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:14px}
#new-btn{width:100%;padding:9px 14px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:.82rem;font-weight:600;cursor:pointer;letter-spacing:.01em;text-align:left;transition:background .15s;letter-spacing:.005em}
#new-btn:hover{background:#3a3a3c}
.sb-section{padding:6px 8px 0;flex:1;overflow-y:auto}
.sb-label{font-size:.7rem;font-weight:600;color:#9ca3af;letter-spacing:.08em;text-transform:uppercase;padding:12px 8px 6px}
.conv-item{padding:8px 10px;border-radius:7px;cursor:pointer;font-size:.83rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:all .12s;margin-bottom:1px}
.conv-item:hover{background:var(--light);color:var(--text)}
.conv-item.active{background:var(--light);color:var(--text);font-weight:500}
.conv-date{font-size:.68rem;color:#9ca3af;margin-top:1px}
.sb-footer{padding:12px 14px;border-top:1px solid var(--border);display:flex;align-items:center;gap:10px}
.avatar{width:30px;height:30px;border-radius:50%;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-size:.78rem;font-weight:700;flex-shrink:0}
.user-name{font-size:.83rem;font-weight:600;color:var(--text)}
.user-role{font-size:.7rem;color:var(--muted)}
.sb-nav{padding:8px}
.sb-link{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:7px;font-size:.82rem;color:var(--muted);text-decoration:none;transition:all .12s}
.sb-link:hover{background:var(--light);color:var(--text)}
.sb-link svg{width:14px;height:14px;flex-shrink:0}

/* MAIN */
#main{flex:1;display:flex;flex-direction:column;min-width:0;background:#fff}
#chat-top{height:52px;border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 24px;gap:12px}
#chat-top-title{font-size:.9rem;font-weight:600;color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge-ai{font-size:.68rem;font-weight:600;color:var(--muted);background:var(--light);padding:3px 8px;border-radius:20px;letter-spacing:.04em;border:1px solid var(--border)}

/* MESSAGES */
#messages{flex:1;overflow-y:auto;padding:32px 24px;display:flex;flex-direction:column;gap:24px;max-width:100%}

.empty{margin:auto;text-align:center;max-width:520px}
.empty h2{font-size:1.25rem;font-weight:700;color:var(--text);margin-bottom:8px}
.empty p{font-size:.88rem;color:var(--muted);line-height:1.6}
.chips{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin-top:20px}
.chip{padding:7px 14px;background:#fff;border:1px solid var(--border);border-radius:20px;font-size:.8rem;color:var(--muted);cursor:pointer;transition:all .15s}
.chip:hover{border-color:var(--accent);color:var(--accent);background:var(--light)}

.msg-wrap{display:flex;gap:12px;max-width:760px;width:100%}
.msg-wrap.user{align-self:flex-end;flex-direction:row-reverse}
.msg-wrap.bot{align-self:flex-start}

.av{width:28px;height:28px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.72rem;font-weight:700;margin-top:2px}
.msg-wrap.user .av{background:var(--accent);color:#fff}
.msg-wrap.bot .av{background:var(--light);color:var(--muted);border:1px solid var(--border);font-size:.85rem}

.bubble{padding:12px 16px;border-radius:12px;line-height:1.65;font-size:.9rem;max-width:100%}
.msg-wrap.user .bubble{background:var(--accent);color:#fff;border-bottom-right-radius:3px}
.msg-wrap.bot .bubble{background:var(--bubble-bot);color:var(--text);border:1px solid var(--border);border-bottom-left-radius:3px}

/* Table in bubble */
.bubble table{border-collapse:collapse;margin-top:14px;font-size:.8rem;width:100%;border-radius:8px;overflow:hidden;border:1px solid var(--border)}
.bubble th{background:#f9fafb;padding:8px 12px;text-align:left;font-weight:600;color:var(--muted);font-size:.73rem;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--border)}
.bubble td{padding:7px 12px;border-bottom:1px solid #f3f4f6;color:var(--text)}
.bubble tr:last-child td{border-bottom:none}
.bubble tr:hover td{background:#f9fafb}
.bubble strong{font-weight:600;color:var(--text)}
.msg-wrap.user .bubble strong{color:#fff}

/* INPUT */
#input-zone{padding:16px 24px 28px;border-top:1px solid var(--border);background:#fff}
.input-box{display:flex;align-items:flex-end;gap:10px;background:#fff;border:1px solid #e2e4e8;border-radius:18px;padding:10px 12px 10px 16px;transition:border-color .2s,box-shadow .2s;box-shadow:0 1px 4px rgba(0,0,0,.04)}
.input-box:focus-within{border-color:#c8cad0;box-shadow:0 2px 12px rgba(0,0,0,.07)}
#inp{flex:1;background:transparent;border:none;outline:none;font-size:.9rem;color:var(--text);resize:none;max-height:140px;font-family:inherit;line-height:1.55}
#inp::placeholder{color:#9ca3af}
#send{width:34px;height:34px;border-radius:10px;background:var(--accent);border:none;cursor:pointer;color:#fff;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .15s}
#send:hover{background:#3a3a3c}
.hint{font-size:.72rem;color:#9ca3af;margin-top:8px;text-align:center}

/* Typing */
.dots{display:flex;gap:4px;padding:2px 0}
.dot{width:6px;height:6px;border-radius:50%;background:#d1d5db;animation:pulse 1.4s infinite}
.dot:nth-child(2){animation-delay:.2s}
.dot:nth-child(3){animation-delay:.4s}
@keyframes pulse{0%,60%,100%{opacity:.3}30%{opacity:1}}
</style>
</head>
<body>

<div id="sidebar">
  <div class="sb-top">
    <div class="sb-brand">Agente AI</div>
    <button id="new-btn" onclick="newChat()">+ Nuova conversazione</button>
  </div>
  <div class="sb-section">
    <div class="sb-label">Conversazioni</div>
    <div id="conv-list"></div>
  </div>
  <div id="admin-nav" style="display:none">
    <div class="sb-nav">
      <a class="sb-link" href="/admin">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
        Dashboard admin
      </a>
    </div>
  </div>
  <div class="sb-footer">
    <div class="avatar" id="av-el"></div>
    <div style="flex:1;min-width:0">
      <div class="user-name" id="name-el"></div>
      <div class="user-role" id="role-el"></div>
    </div>
    <a href="/logout" style="font-size:.78rem;color:#9ca3af;text-decoration:none" title="Esci">Esci</a>
  </div>
</div>

<div id="main">
  <div id="chat-top">
    <div id="chat-top-title">Nuova conversazione</div>
    <span class="badge-ai">{{AI_BADGE}}</span>
  </div>
  <div id="messages">
    <div class="empty" id="empty-state">
      <h2>Come posso aiutarti?</h2>
      <p>Fai una domanda sull'archivio blocchi di Max Marmi Carrara. Puoi chiedere in italiano, in modo naturale.</p>
      <div class="chips">
        <div class="chip" onclick="sendChip(this)">Quanti blocchi di Calacatta nel 2012?</div>
        <div class="chip" onclick="sendChip(this)">Qual è il materiale con il fatturato più alto?</div>
        <div class="chip" onclick="sendChip(this)">I 20 blocchi più profittevoli</div>
        <div class="chip" onclick="sendChip(this)">Riepilogo fatturato anno per anno</div>
        <div class="chip" onclick="sendChip(this)">Blocchi che hanno generato perdite</div>
        <div class="chip" onclick="sendChip(this)">Performance dei fornitori</div>
        <div class="chip" onclick="sendChip(this)">Blocchi per deposito</div>
        <div class="chip" onclick="sendChip(this)">Costi di lavorazione per tipo</div>
      </div>
    </div>
  </div>
  <div id="input-zone">
    <div class="input-box">
      <textarea id="inp" rows="1" placeholder="Fai una domanda..."
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send()}"></textarea>
      <button id="send" onclick="send()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      </button>
    </div>
    <div class="hint">Invio per inviare · Shift+Invio per andare a capo</div>
  </div>
</div>

<script>
const USER_NAME = "{{USER_NAME}}";
const IS_ADMIN  = {{IS_ADMIN}};
let convs = {{CONVS_JSON}};
let activeCid = null;

document.getElementById('av-el').textContent   = USER_NAME[0].toUpperCase();
document.getElementById('name-el').textContent = USER_NAME;
document.getElementById('role-el').textContent = IS_ADMIN ? 'Amministratore' : 'Utente';
if(IS_ADMIN) document.getElementById('admin-nav').style.display = 'block';
renderSidebar();

function renderSidebar(){
  const el = document.getElementById('conv-list');
  if(!convs.length){ el.innerHTML='<div style="padding:8px 10px;font-size:.78rem;color:#9ca3af">Nessuna conversazione</div>'; return; }
  el.innerHTML = convs.map(c=>`
    <div class="conv-item${c.id===activeCid?' active':''}" onclick="loadConv(${c.id})">
      <div>${esc(c.title||'Conversazione')}</div>
      <div class="conv-date">${fmtDate(c.updated_at)}</div>
    </div>`).join('');
}

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function fmtDate(s){if(!s)return'';try{return new Date(s).toLocaleDateString('it-IT',{day:'2-digit',month:'short'})}catch{return''}}

async function newChat(){
  const r=await fetch('/api/new_conversation',{method:'POST'});
  const c=await r.json();
  activeCid=c.id; convs.unshift(c); renderSidebar();
  document.getElementById('messages').innerHTML='<div class="empty" id="empty-state"><h2>Come posso aiutarti?</h2><p>Fai una domanda sull\'archivio blocchi.</p></div>';
  document.getElementById('chat-top-title').textContent='Nuova conversazione';
}

async function loadConv(id){
  activeCid=id; renderSidebar();
  const r=await fetch(`/api/conversation/${id}`);
  const d=await r.json();
  const msgs=document.getElementById('messages');
  if(!d.messages||!d.messages.length){msgs.innerHTML='<div class="empty"><h2>Conversazione vuota</h2></div>';return;}
  msgs.innerHTML='';
  d.messages.forEach(m=>addMsg(m.role,m.content));
  msgs.scrollTop=msgs.scrollHeight;
  const conv=convs.find(c=>c.id===id);
  document.getElementById('chat-top-title').textContent=conv?conv.title:'Conversazione';
}

function sendChip(el){document.getElementById('inp').value=el.textContent;send();}

async function send(){
  const inp=document.getElementById('inp');
  const q=inp.value.trim(); if(!q) return;
  inp.value=''; inp.style.height='auto';
  const es=document.getElementById('empty-state'); if(es) es.remove();
  addMsg('user',q);
  const typing=addTyping();
  document.getElementById('messages').scrollTop=99999;
  try{
    const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q,conversation_id:activeCid})});
    const d=await r.json();
    typing.remove();
    if(d.error){addMsg('bot','Errore: '+d.error);return;}
    addMsg('bot',d.answer);
    activeCid=d.conversation_id;
    const idx=convs.findIndex(c=>c.id===d.conversation_id);
    if(idx>=0){convs[idx].title=d.title;convs[idx].updated_at=new Date().toISOString();}
    else convs.unshift({id:d.conversation_id,title:d.title,updated_at:new Date().toISOString()});
    document.getElementById('chat-top-title').textContent=d.title;
    renderSidebar();
  }catch(e){typing.remove();addMsg('bot','Errore di connessione. Riprova.');}
  document.getElementById('messages').scrollTop=99999;
}

function addMsg(role,content){
  const msgs=document.getElementById('messages');
  const wrap=document.createElement('div');
  wrap.className='msg-wrap '+role;
  const av=document.createElement('div'); av.className='av';
  av.textContent=role==='user'?USER_NAME[0].toUpperCase():'A';
  const bub=document.createElement('div'); bub.className='bubble';
  bub.innerHTML=md(content);
  wrap.appendChild(av); wrap.appendChild(bub);
  msgs.appendChild(wrap); return wrap;
}

function addTyping(){
  const msgs=document.getElementById('messages');
  const wrap=document.createElement('div'); wrap.className='msg-wrap bot';
  wrap.innerHTML='<div class="av">A</div><div class="bubble"><div class="dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div></div>';
  msgs.appendChild(wrap); return wrap;
}

function md(text){
  const lines=text.split('\n');
  let out='',inT=false,inTb=false;
  for(let l of lines){
    if(l.startsWith('|')){
      const cells=l.split('|').slice(1,-1).map(c=>c.trim());
      if(!inT){out+='<table>';inT=true;}
      if(cells.every(c=>/^-+$/.test(c))){out+='<tbody>';inTb=true;continue;}
      const tag=inTb?'td':'th';
      out+=`<tr>${cells.map(c=>`<${tag}>${c}</${tag}>`).join('')}</tr>`;
    }else{
      if(inT){if(inTb)out+='</tbody>';out+='</table>';inT=false;inTb=false;}
      let r=l.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
      if(r.trim())out+=r+'<br>';else if(out)out+='<br>';
    }
  }
  if(inT){if(inTb)out+='</tbody>';out+='</table>';}
  return out;
}

document.getElementById('inp').addEventListener('input',function(){
  this.style.height='auto';
  this.style.height=Math.min(this.scrollHeight,140)+'px';
});
</script>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8"><title>Admin — Max Marmi</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb;color:#111827;min-height:100vh}
nav{background:#fff;border-bottom:1px solid #e5e7eb;padding:0 28px;display:flex;align-items:center;gap:8px;height:56px}
.nav-brand{font-size:.82rem;font-weight:700;color:#1d1d1f;letter-spacing:.07em;text-transform:uppercase;margin-right:12px}
.nav-a{font-size:.83rem;color:#6b7280;text-decoration:none;padding:6px 12px;border-radius:7px;transition:all .15s}
.nav-a:hover,.nav-a.on{background:#f3f4f6;color:#1d1d1f}
.nav-ml{margin-left:auto}
.container{max-width:1080px;margin:0 auto;padding:36px 28px}
.page-title{font-size:1.4rem;font-weight:700;letter-spacing:-.02em;margin-bottom:4px}
.page-sub{font-size:.85rem;color:#6b7280;margin-bottom:32px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-bottom:28px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:22px}
.card-label{font-size:.72rem;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px}
.card-value{font-size:1.9rem;font-weight:700;color:#1d1d1f;letter-spacing:-.03em}
.card-sub{font-size:.78rem;color:#9ca3af;margin-top:4px}
.section{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:20px}
.section-title{font-size:.9rem;font-weight:700;margin-bottom:18px;color:#1d1d1f}
table{width:100%;border-collapse:collapse;font-size:.83rem}
th{text-align:left;padding:9px 14px;font-size:.72rem;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #f3f4f6}
td{padding:10px 14px;border-bottom:1px solid #f9fafb;color:#3a3a3c}
tr:last-child td{border:none}
tr:hover td{background:#f9fafb}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.7rem;font-weight:600;letter-spacing:.03em}
.badge.admin{background:#1d1d1f;color:#fff}
.badge.user{background:#f3f4f6;color:#6b7280}
.btn-sm{background:#1d1d1f;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:.75rem;cursor:pointer;font-weight:500}
.btn-sm:hover{background:#3a3a3c}
.bars{display:flex;align-items:flex-end;gap:5px;height:72px;margin:4px 0 8px}
.bar{flex:1;background:#1d1d1f;border-radius:3px 3px 0 0;min-height:2px;position:relative}
.bar:hover::after{content:attr(data-tip);position:absolute;bottom:calc(100%+6px);left:50%;transform:translateX(-50%);background:#1d1d1f;color:#fff;padding:3px 8px;border-radius:5px;font-size:.7rem;white-space:nowrap;z-index:10}
.bar-lbs{display:flex;gap:5px}
.bar-lb{flex:1;font-size:.65rem;color:#9ca3af;text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.panel{position:fixed;top:0;right:-480px;width:460px;height:100vh;background:#fff;border-left:1px solid #e5e7eb;z-index:100;display:flex;flex-direction:column;transition:right .25s ease}
.panel.open{right:0}
.panel-head{padding:20px 22px;border-bottom:1px solid #e5e7eb;display:flex;align-items:flex-start;gap:12px}
.panel-title{font-size:.95rem;font-weight:700;flex:1}
.panel-sub{font-size:.78rem;color:#9ca3af;margin-top:2px}
.panel-msgs{flex:1;overflow-y:auto;padding:20px 22px;display:flex;flex-direction:column;gap:14px}
.pmsg{}
.pmsg-meta{font-size:.7rem;color:#9ca3af;margin-bottom:4px}
.pmsg-text{background:#f9fafb;border:1px solid #f3f4f6;border-radius:8px;padding:10px 14px;font-size:.83rem;line-height:1.55;color:#3a3a3c}
.pmsg.user .pmsg-text{background:#111;color:#fff;border-color:#1d1d1f}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.25);z-index:99;display:none}
.overlay.open{display:block}
</style>
</head>
<body>
<nav>
  <span class="nav-brand">Max Marmi — Admin</span>
  <a class="nav-a" href="/">Chat</a>
  <a class="nav-a on" href="/admin">Dashboard</a>
  <a class="nav-a nav-ml" href="/logout">Esci</a>
</nav>
<div class="container">
  <div class="page-title">Dashboard</div>
  <div class="page-sub" id="pg-sub">Caricamento...</div>
  <div class="grid" id="cards"></div>
  <div class="section">
    <div class="section-title">Messaggi — ultimi 7 giorni</div>
    <div id="chart"></div>
  </div>
  <div class="section">
    <div class="section-title">Attività per utente</div>
    <table><thead><tr><th>Utente</th><th>Ruolo</th><th>Messaggi</th><th>Ultima attività</th></tr></thead><tbody id="utb"></tbody></table>
  </div>
  <div class="section">
    <div class="section-title">Tutte le conversazioni</div>
    <table><thead><tr><th>Utente</th><th>Titolo</th><th>Messaggi</th><th>Ultima att.</th><th></th></tr></thead><tbody id="cvb"></tbody></table>
  </div>
</div>
<div class="overlay" id="ov" onclick="closePanel()"></div>
<div class="panel" id="panel">
  <div class="panel-head">
    <div style="flex:1"><div class="panel-title" id="pt"></div><div class="panel-sub" id="ps"></div></div>
    <button onclick="closePanel()" style="background:none;border:none;cursor:pointer;color:#9ca3af;font-size:1.1rem;line-height:1">&#x2715;</button>
  </div>
  <div class="panel-msgs" id="pm"></div>
</div>
<script>
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
async function load(){
  const [sr,cr]=await Promise.all([fetch('/api/admin/stats'),fetch('/api/admin/conversations')]);
  const st=await sr.json(), cv=await cr.json();
  document.getElementById('pg-sub').textContent=`${st.total_users} utenti · ${st.total_conversations} conversazioni · ${st.total_messages} messaggi`;
  // Cards
  const cc=[
    {l:'Messaggi totali',v:st.total_messages.toLocaleString('it'),s:'tutti gli utenti'},
    {l:'Conversazioni',v:st.total_conversations.toLocaleString('it'),s:'aperte'},
    {l:'Utenti attivi',v:st.per_user.filter(u=>u.msgs>0).length,s:'hanno scritto'},
    {l:'Ultimi 7 giorni',v:st.messages_per_day.reduce((a,r)=>a+r.msgs,0),s:'messaggi recenti'},
  ];
  document.getElementById('cards').innerHTML=cc.map(c=>`<div class="card"><div class="card-label">${c.l}</div><div class="card-value">${c.v}</div><div class="card-sub">${c.s}</div></div>`).join('');
  // Chart
  const days=st.messages_per_day;
  if(days.length){
    const mx=Math.max(...days.map(d=>d.msgs),1);
    document.getElementById('chart').innerHTML=`<div class="bars">${days.map(d=>`<div class="bar" style="height:${Math.round(d.msgs/mx*68)+4}px" data-tip="${d.day}: ${d.msgs}"></div>`).join('')}</div><div class="bar-lbs">${days.map(d=>`<div class="bar-lb">${d.day.slice(5)}</div>`).join('')}</div>`;
  }else{
    document.getElementById('chart').innerHTML='<p style="color:#9ca3af;font-size:.83rem">Nessun messaggio negli ultimi 7 giorni.</p>';
  }
  // Utenti
  document.getElementById('utb').innerHTML=st.per_user.map(u=>`<tr><td><strong>${u.display_name}</strong></td><td><span class="badge ${u.role}">${u.role==='admin'?'Admin':'Utente'}</span></td><td>${u.msgs||0}</td><td>${u.last_active?new Date(u.last_active).toLocaleString('it-IT'):'—'}</td></tr>`).join('');
  // Conversazioni
  document.getElementById('cvb').innerHTML=cv.map(c=>`<tr><td><strong>${c.user}</strong></td><td style="max-width:200px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${esc(c.title||'—')}</td><td>${c.n_messages}</td><td>${new Date(c.updated_at).toLocaleString('it-IT')}</td><td><button class="btn-sm" onclick="openPanel(${c.id},'${esc(c.title||'')}','${esc(c.user)}')">Apri</button></td></tr>`).join('');
}
async function openPanel(id,title,user){
  document.getElementById('pt').textContent=title||'Conversazione';
  document.getElementById('ps').textContent='Utente: '+user;
  const r=await fetch(`/api/conversation/${id}`);
  const d=await r.json();
  document.getElementById('pm').innerHTML=(d.messages||[]).map(m=>`<div class="pmsg ${m.role}"><div class="pmsg-meta">${m.role==='user'?'Utente':'Agente AI'} · ${new Date(m.created_at).toLocaleString('it-IT')}</div><div class="pmsg-text">${esc(m.content).replace(/\\n/g,'<br>')}</div></div>`).join('')||'<p style="color:#9ca3af">Nessun messaggio.</p>';
  document.getElementById('panel').classList.add('open');
  document.getElementById('ov').classList.add('open');
}
function closePanel(){document.getElementById('panel').classList.remove('open');document.getElementById('ov').classList.remove('open');}
load();
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
init_app_db()  # inizializzato sempre, anche con gunicorn

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 7860))
    print(f"\n  Max Marmi Chat — avviato su http://localhost:{PORT}")
    print(f"  Dashboard admin: http://localhost:{PORT}/admin")
    print("  Premi CTRL+C per fermare.\n")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
