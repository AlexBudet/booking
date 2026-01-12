# booking.py
import string
import json
from flask import Blueprint, g, request, jsonify, render_template, render_template_string, session, url_for, current_app
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from appl.models import Appointment, AppointmentSource, Service, Operator, OperatorShift, Client, BusinessInfo
from datetime import date, datetime, timezone, timedelta, time
from sqlalchemy import func, or_
from pytz import timezone as pytz_timezone
import re
import os
import random
import uuid
from markupsafe import escape
import threading
from azure.communication.email import EmailClient
from wbiztool_client import WbizToolClient
import html as html_lib
import time as time_module

# --- UTIL: formato data per email (solo output email, non DB) ---
MONTH_ABBR_IT = {
    1: 'GEN', 2: 'FEB', 3: 'MAR', 4: 'APR', 5: 'MAG', 6: 'GIU',
    7: 'LUG', 8: 'AGO', 9: 'SET', 10: 'OTT', 11: 'NOV', 12: 'DIC'
}

csrf_local = CSRFProtect()

def csrf_token():
    """Ritorna il token CSRF corrente per essere usato nei form HTML inline."""
    return generate_csrf()

def _fmt_date_it_short(date_str: str) -> str:
    """
    Converte 'YYYY-MM-DD' in 'DD MMM YYYY' (es: 2025-10-05 -> 05 OTT 2025).
    Se parsing fallisce restituisce l'input originale.
    Usata SOLO per formattare le date nelle email.
    """
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
        return f"{d.day:02d} {MONTH_ABBR_IT.get(d.month, '')} {d.year}"
    except Exception:
        return date_str
    
def _html_to_text(html_content: str) -> str:
    """Converte un frammento HTML in testo semplice leggibile."""
    try:
        if not html_content:
            return ""
        txt = re.sub(r'(?i)<br\s*/?>', '\n', html_content)
        txt = re.sub(r'(?i)</p>', '\n', txt)
        txt = re.sub(r'<[^>]+>', '', txt)
        txt = html_lib.unescape(txt)
        lines = [l.strip() for l in txt.splitlines()]
        txt = "\n".join([l for l in lines if l])
        return txt.strip()
    except Exception:
        return (html_content or "").strip()

def _now_rome():
    return datetime.now(pytz_timezone('Europe/Rome'))

# Stato semplice per il job mattutino (per-tenant, in memoria)
_MORNING_STATE = {}        # tenant_id -> {"date": date, "queue": [dict], "idx": int, "last_sent_minute": datetime}
_MORNING_LOCKS = {}        # tenant_id -> threading.Lock()
MORNING_POLL_SECONDS = 60   # ogni quanto il tick gira
MORNING_RATE_SECONDS = 60  # ogni quanto inviare un messaggio
WA_MORNING_DEBUG = True  # metti a False quando hai finito i test
PROCESS_START_AT = _now_rome() # Registra l'orario di avvio del processo (serve per capire se il riavvio è avvenuto dopo il cutoff)

WA_OPERATOR_DEBUG = True  # metti a False quando hai finito i test per operatori
_OP_STATE_MAP = {}        # tenant_id -> {"date": date, "queue": [dict], "idx": int, "last_sent_minute": datetime}
_OP_LOCKS = {}            # tenant_id -> threading.Lock()

def _op_dbg(tenant_id, msg):
    if WA_OPERATOR_DEBUG:
        print(f"[WA-OP][{tenant_id}] {msg}")

def _wa_dbg(tenant_id, msg):
    if WA_MORNING_DEBUG:
        print(f"[WA-MORNING][{tenant_id}] {msg}")

def _normalize_msisdn(phone: str) -> str:
    if not phone:
        return ''
    phone = phone.strip()
    phone = re.sub(r'[^\d+]', '', phone)
    return phone

def _tenant_env_prefix(tenant_id: str) -> str:
    raw = str(tenant_id or '').strip().upper().replace('-', '_')
    digits = ''.join(ch for ch in raw if ch.isdigit())
    return f'T{digits}' if digits else raw

def invia_email_azure(to_email, subject, html_content, from_email=None, plain_text=None):
    connection_string = os.environ.get('AZURE_EMAIL_CONNECTION_STRING')
    if not connection_string:
        print("ERROR: AZURE_EMAIL_CONNECTION_STRING not set")
        return False
    sender = (os.environ.get('AZURE_EMAIL_SENDER') or "").strip()
    if not sender:
        print("ERROR: AZURE_EMAIL_SENDER not set")
        return False
    client = EmailClient.from_connection_string(connection_string)
    content = {"subject": subject, "html": html_content}
    content["plainText"] = plain_text or _html_to_text(html_content)
    message = {
        "senderAddress": sender,
        "recipients": {"to": [{"address": to_email}]},
        "content": content
    }
    poller = client.begin_send(message)
    try:
        result = poller.result()
        print(f"[EMAIL] sent id={getattr(result,'message_id',None)} sender={sender}")
        return getattr(result, "status", "Succeeded") == "Succeeded"
    except Exception as e:
        print(f"[EMAIL] ERROR result: {repr(e)}")
        return False

# Cache globale per il client Azure Email (evita di ricreare connessioni)
_azure_email_client = None

def _get_azure_email_client():
    """Ritorna un client Azure Email cached (singleton)."""
    global _azure_email_client
    if _azure_email_client is None:
        connection_string = os.environ.get('AZURE_EMAIL_CONNECTION_STRING')
        if connection_string:
            _azure_email_client = EmailClient.from_connection_string(connection_string)
    return _azure_email_client

def _send_email_sync(to_email, subject, html_content, from_email=None, plain_text=None):
    """
    Invia email usando Azure Communication Services (SINCRONO).
    Include retry con backoff esponenziale + jitter per evitare rate limiting.
    Questa funzione blocca - usare invia_email_async per chiamate non bloccanti.
    """
    # Parametri di retry ottimizzati per Azure
    max_retries = 5
    base_delay = 5  # secondi
    max_delay = 120  # massimo 2 minuti tra retry
    poller_wait_time = 10  # secondi tra ogni polling (evita 429!)
    poller_max_time = 180  # timeout massimo per il polling (3 minuti)
    
    client = _get_azure_email_client()
    if not client:
        print("ERROR: AZURE_EMAIL_CONNECTION_STRING not configured", flush=True)
        return False
    
    sender = (os.environ.get('AZURE_EMAIL_SENDER') or "").strip()
    if not sender:
        print("ERROR: AZURE_EMAIL_SENDER not set", flush=True)
        return False
    
    content = {"subject": subject, "html": html_content}
    content["plainText"] = plain_text or _html_to_text(html_content)
    message = {
        "senderAddress": sender,
        "recipients": {"to": [{"address": to_email}]},
        "content": content
    }
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                print(f"[EMAIL-AZURE] Retry {attempt + 1}/{max_retries} to {to_email}", flush=True)
            else:
                print(f"[EMAIL-AZURE] Sending to {to_email}", flush=True)
            
            poller = client.begin_send(message)
            
            # Polling gentile: aspetta 10 secondi tra ogni check per evitare 429
            time_elapsed = 0
            while not poller.done():
                poller.wait(poller_wait_time)
                time_elapsed += poller_wait_time
                if time_elapsed > poller_max_time:
                    print(f"[EMAIL-AZURE] Polling timeout after {poller_max_time}s", flush=True)
                    break
            
            result = poller.result() if poller.done() else None
            status = getattr(result, 'status', 'Unknown') if result else 'Timeout'
            
            if status == "Succeeded":
                print(f"[EMAIL-AZURE] SENT OK to={to_email}", flush=True)
                return True
            else:
                print(f"[EMAIL-AZURE] Unexpected status: {status}", flush=True)
                return False
            
        except Exception as e:
            error_str = str(e).lower() + repr(e).lower()
            
            # Errori temporanei che meritano retry
            is_retryable = any(err in error_str for err in [
                'toomanyrequests', '429', 'throttl', 'rate limit',
                'temporarily unavailable', '503', '502', '504',
                'timeout', 'connection', 'socket'
            ])
            
            if is_retryable and attempt < max_retries - 1:
                # Backoff esponenziale con jitter (randomizza per evitare burst)
                delay = min(base_delay * (2 ** attempt) + random.uniform(0, 3), max_delay)
                print(f"[EMAIL-AZURE] Temporary error, waiting {delay:.1f}s: {repr(e)[:80]}", flush=True)
                time_module.sleep(delay)
            else:
                print(f"[EMAIL-AZURE] FAILED: {repr(e)}", flush=True)
                return False
    
    return False


def invia_email_async(to_email, subject, html_content, from_email=None, plain_text=None):
    """
    Invia email in background usando un thread separato.
    Ritorna immediatamente senza bloccare la risposta HTTP.
    """
    def _worker():
        _send_email_sync(to_email, subject, html_content, from_email, plain_text)
    
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return True

def to_rome(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(pytz_timezone('Europe/Rome'))

def is_calendar_closed(op_id, inizio, fine, turni_per_operatore, all_apps):
    """
    Restituisce True se la cella (intervallo orario per operatore) NON è selezionabile per prenotazioni.
    Controlla:
    - fuori turno
    - blocchi OFF globali o per operatore
    - sovrapposizione appuntamenti
    """
    def to_naive(dt):
        if dt is not None and getattr(dt, "tzinfo", None) is not None:
            return dt.replace(tzinfo=None)
        return dt

    inizio = to_naive(inizio)
    fine = to_naive(fine)
    shifts = turni_per_operatore.get(op_id, [])
    # Fuori turno
    if not any(s <= inizio.time() and fine.time() <= e for s, e in shifts):
        return True
    for a in all_apps:
        if a.is_cancelled_by_client:  # Salta quelli soft-deleted
            continue
        a_start = to_naive(a.start_time)
        a_end = to_naive(a.start_time + timedelta(minutes=a._duration))
        # Blocco OFF globale
        if a.operator_id is None and a.note and "OFF" in a.note:
            if a_start < fine and a_end > inizio:
                return True
        # Blocco OFF per operatore
        if a.operator_id == op_id and a.note and "OFF" in a.note:
            if a_start < fine and a_end > inizio:
                return True
        # Sovrapposizione appuntamenti
        if a.operator_id == op_id and not (a.note and "OFF" in a.note):
            if a_start < fine and a_end > inizio:
                return True
    return False

def scegli_operatori_automatici(servizi_ids, data_str, ora_str, operatori_possibili, turni_per_operatore, all_apps, operatori_preferiti_ids=[]):
    """
    Restituisce una lista di operatori assegnati (uno per ogni servizio), seguendo le priorità:
    1. Tutti i servizi con lo stesso operatore (priorità alta).
    2. Se non possibile, assegnazione a cascata cercando di raggruppare il maggior numero di servizi
       sullo stesso operatore, preferendo gli operatori_preferiti_ids.
    3. Se neanche a cascata è possibile assegnare tutti i servizi, restituisce [None] * len(servizi_ids).
    """

    servizi_objs = g.db_session.query(Service).filter(Service.id.in_(servizi_ids)).all()
    servizi_map = {s.id: s for s in servizi_objs}
    servizi_operatori_abilitati = {s.id: [op.id for op in s.operators] for s in servizi_objs}
    servizi_durate = [servizi_map[sid].servizio_durata or 30 for sid in servizi_ids]

    # Filtra gli operatori che sono almeno abilitati per uno dei servizi richiesti
    # e ordina gli operatori preferiti per primi
    operatori_rilevanti = sorted(
        [op for op in operatori_possibili if any(op.id in servizi_operatori_abilitati[sid] for sid in servizi_ids)],
        key=lambda op: (op.id not in operatori_preferiti_ids, op.user_nome) # Metti i preferiti per primi
    )

    start_time = datetime.strptime(f"{data_str} {ora_str}", "%Y-%m-%d %H:%M")

    # --- 1. Prova tutti con lo stesso operatore (PRIORITÀ ALTA) ---
    # Inizia dagli operatori preferiti per questa ricerca
    for op in operatori_rilevanti:
        # Se l'operatore non è abilitato per *tutti* i servizi, salta
        if not all(op.id in servizi_operatori_abilitati.get(sid, []) for sid in servizi_ids):
            continue

        slot_corrente_temp = start_time
        ok = True
        for durata_servizio in servizi_durate:
            inizio_temp = slot_corrente_temp
            fine_temp = slot_corrente_temp + timedelta(minutes=durata_servizio)
            
            if is_calendar_closed(op.id, inizio_temp, fine_temp, turni_per_operatore, all_apps):
                ok = False
                break
            slot_corrente_temp = fine_temp # Aggiorna lo slot per il servizio successivo
        
        if ok:
            # Trovato un singolo operatore per tutti i servizi! 🎉
            return [op.id] * len(servizi_ids)

    # --- 2. Assegnazione a cascata con raggruppamento e preferenza operatori (PRIORITÀ SECONDARIA) ---
    operatori_assegnati = []
    slot_corrente = start_time
    
    for i, servizio_id in enumerate(servizi_ids):
        durata_servizio = servizi_durate[i]
        inizio_servizio = slot_corrente
        fine_servizio = slot_corrente + timedelta(minutes=durata_servizio)
        
        found_operator_for_current_service = False
        
        # Prova prima con gli operatori preferiti che sono anche abilitati per questo servizio
        for op in [o for o in operatori_rilevanti if o.id in servizi_operatori_abilitati.get(servizio_id, [])]:
            if not is_calendar_closed(op.id, inizio_servizio, fine_servizio, turni_per_operatore, all_apps):
                operatori_assegnati.append(op.id)
                slot_corrente = fine_servizio
                found_operator_for_current_service = True
                break # Operatore trovato per questo servizio, passa al prossimo
        
        if not found_operator_for_current_service:
            # Se nessuno degli operatori preferiti va bene, prova tutti gli altri operatori rilevanti
            # (non preferiti, ma comunque abilitati e disponibili)
            for op in [o for o in operatori_possibili if o.id in servizi_operatori_abilitati.get(servizio_id, []) and o.id not in operatori_preferiti_ids]:
                if not is_calendar_closed(op.id, inizio_servizio, fine_servizio, turni_per_operatore, all_apps):
                    operatori_assegnati.append(op.id)
                    slot_corrente = fine_servizio
                    found_operator_for_current_service = True
                    break
        
        if not found_operator_for_current_service:
            # Se non si trova nessun operatore per il servizio corrente, la catena fallisce
            return [None] * len(servizi_ids) 
            
    return operatori_assegnati # Ritorna la lista completa di operatori assegnati

booking_bp = Blueprint('booking', __name__)

@booking_bp.route('/')
@booking_bp.route('/booking')
def booking_page(tenant_id):
    # Il tenant_id viene preso dall'URL grazie al prefisso dinamico nel blueprint
    oggi = date.today().strftime('%Y-%m-%d')
    servizi = g.db_session.query(Service).filter(
        Service.servizio_durata != 0,
        ~Service.servizio_nome.ilike('dummy'),
        Service.is_visible_online == True,
        Service.is_deleted == False
    ).order_by(Service.servizio_nome).all()
    operatori = (
        g.db_session.query(Operator)
        .filter(Operator.is_deleted == False, Operator.is_visible == True)
        .order_by(Operator.user_nome)
        .all()
    )
    business_info = g.db_session.query(BusinessInfo).first()

    servizi_json = [{
        'id': s.id, 
        'servizio_nome': s.servizio_nome, 
        'servizio_durata': s.servizio_durata,
        'servizio_prezzo': str(s.servizio_prezzo),
        'operator_ids': [op.id for op in s.operators if op.is_visible and not op.is_deleted],
        'sottocategoria': s.servizio_sottocategoria.nome if s.servizio_sottocategoria else None,
        'servizio_descrizione': s.servizio_descrizione
    } for s in servizi]
    
    operatori_json = [{
        'id': op.id, 
        'nome': op.user_nome
    } for op in operatori]

    csrf_token = generate_csrf()

    return render_template(
        'booking_public.html', 
        servizi_json=servizi_json, 
        operatori_json=operatori_json,
        operatori=operatori,
        oggi=oggi,
        business_info=business_info,
        csrf_token=csrf_token,
        tenant_id=tenant_id
    )

@booking_bp.route('/search-servizi')
def search_servizi(tenant_id):
    q = request.args.get('q', '', type=str)
    # base filter: solo servizi visibili online e non cancellati
    query = g.db_session.query(Service).filter(Service.is_visible_online == True, Service.is_deleted == False)
    if q:
        query = query.filter(Service.servizio_nome.ilike(f"%{q}%"))
    # applica filtro per tenant se presente (gestisce tenant_id numerico o stringa)
    try:
        tid = int(tenant_id) if tenant_id is not None else None
    except Exception:
        tid = tenant_id
    if tid and hasattr(Service, 'tenant_id'):
        query = query.filter(Service.tenant_id == tid)
    risultati = query.order_by(Service.servizio_nome).all()

    return jsonify([
        {
            "id": s.id,
            "servizio_nome": s.servizio_nome,
            "sottocategoria": s.servizio_sottocategoria.nome if s.servizio_sottocategoria else None,
            "servizio_descrizione": s.servizio_descrizione
        }
        for s in risultati
    ])

@booking_bp.route('/orari', methods=['GET'])
def orari_disponibili(tenant_id):
    data_str = request.args.get('data')  # formato: YYYY-MM-DD
    if not data_str:
        return jsonify({"error": "Data non specificata"}), 400
    servizi_raw = request.args.getlist('servizi[]')
    servizi_items = []
    servizi_ids = []
    for s in servizi_raw:
        try:
            item = json.loads(s)
            sid = int(item["servizio_id"])
            servizi_ids.append(sid)
            servizi_items.append(item)
        except Exception:
            continue

    if not servizi_ids:
        return jsonify({"error": "Servizi non trovati"}), 404

    servizi = g.db_session.query(Service).filter(
        Service.id.in_(servizi_ids),
        Service.is_deleted == False,
        Service.is_visible_online == True
    ).all()
    if not servizi:
        return jsonify({"error": "Servizi non trovati"}), 404
    
    servizi_operatori = {s.id: [op.id for op in s.operators] for s in servizi}

    data = datetime.strptime(data_str, "%Y-%m-%d").date()
    business_info = g.db_session.query(BusinessInfo).first()
    apertura = business_info.active_opening_time
    chiusura = business_info.active_closing_time
    closing_days = getattr(business_info, "closing_days_list", [])

    orari = []
    debug_info = []
    now = datetime.now()  # naive Europe/Rome
    slot_operatori = {}  # AGGIUNTA: dict ora -> lista operatori

    # Escludi giorni di chiusura
    if data.strftime('%A') in closing_days:
        debug_info.append(f"Giorno {data.strftime('%A')} in closing_days: nessuno slot disponibile")
        return jsonify({"orari_disponibili": [], "operatori_assegnati": {}, "debug": debug_info})

    # Carica tutti gli operatori disponibili e relativi turni
    operatori_disponibili = g.db_session.query(Operator).filter_by(is_deleted=False, is_visible=True).all()
    operatore_id = request.args.get('operatore_id')

    # Preferenze per-servizio: raccogli gli ID scelti
    preferred_ids = set()
    for item in servizi_items:
        try:
            if item.get("operatore_id") is not None:
                preferred_ids.add(int(item["operatore_id"]))
        except Exception:
            pass

    has_per_service_prefs = len(preferred_ids) > 0

    # NON restringere l'elenco globale agli ID preferiti: la scelta per-servizio
    # va applicata solo dentro la costruzione della catena.
    # Applica solo il filtro globale (colonna singola) se NON ci sono preferenze per-servizio.
    if not has_per_service_prefs and operatore_id:
        operatori_disponibili = [op for op in operatori_disponibili if str(op.id) == str(operatore_id)]

    turni_disponibili = g.db_session.query(OperatorShift).filter(
        OperatorShift.operator_id.in_([o.id for o in operatori_disponibili]),
        OperatorShift.shift_date == data
    ).all()

    # Costruisce una mappa operator_id -> lista di (inizio, fine) turno per più turni
    turni_per_operatore = {}
    for op in operatori_disponibili:
        op_turni = [
            (
                t.shift_start_time if isinstance(t.shift_start_time, time) else apertura,
                t.shift_end_time if isinstance(t.shift_end_time, time) else chiusura
            )
            for t in turni_disponibili if t.operator_id == op.id
        ]
        if not op_turni:
            op_turni = [(apertura, chiusura)]
        op_turni = [
            (max(start, apertura), min(end, chiusura))
            for (start, end) in op_turni
            if max(start, apertura) < min(end, chiusura)
        ]
        if op_turni:
            turni_per_operatore[op.id] = op_turni

    # NUOVO: se ci sono preferenze per-servizio e dopo il filtro non ci sono turni
    # per le operatrici scelte, restituisci subito nessuna disponibilità.
    if has_per_service_prefs and not turni_per_operatore:
        return jsonify({"orari_disponibili": [], "operatori_assegnati": {}, "debug": ["Nessun turno per le operatrici selezionate"]})

    appuntamenti = g.db_session.query(Appointment).filter(
        Appointment.start_time >= datetime.combine(data, time.min),
        Appointment.start_time < datetime.combine(data + timedelta(days=1), time.min),
        Appointment.is_cancelled_by_client == False
    ).all()
    blocchi_off = g.db_session.query(Appointment).filter(
        Appointment.start_time >= datetime.combine(data, time.min),
        Appointment.start_time < datetime.combine(data + timedelta(days=1), time.min),
        or_(
            Appointment.note.ilike('%OFF%'),
            Appointment.service_id == 9999
        )
    ).all()
    for b in blocchi_off:
        if b not in appuntamenti:
            appuntamenti.append(b)

    def to_naive(dt):
        if dt is not None and getattr(dt, "tzinfo", None) is not None:
            return dt.replace(tzinfo=None)
        return dt

    def operatore_disponibile(operator_id, inizio, fine):
        turni = turni_per_operatore.get(operator_id, [])
        if not any(start <= inizio.time() and fine.time() <= end for start, end in turni):
            return False, "fuori turno"
        for app in appuntamenti:
            if app.operator_id is None and app.note and "OFF" in app.note:
                app_start = to_naive(app.start_time)
                app_end = to_naive(app_start + timedelta(minutes=app._duration))
                if app_start < to_naive(fine) and app_end > to_naive(inizio):
                    return False, "blocco OFF globale"
            if str(app.operator_id) == str(operator_id):
                app_start = to_naive(app.start_time)
                app_end = to_naive(app_start + timedelta(minutes=app._duration))
                if app_start < to_naive(fine) and app_end > to_naive(inizio):
                    if app.note and "OFF" in app.note:
                        return False, "blocco OFF"
                    return False, "occupato"
        return True, None

    intervalli_tmp = []
    for op_id, turni in turni_per_operatore.items():
        for t in turni:
            intervalli_tmp.append(t)
    if not intervalli_tmp:
        return jsonify({"orari_disponibili": [], "operatori_assegnati": {}, "debug": ["Nessun turno disponibile"]})
    intervalli_tmp.sort()
    intervalli = []
    for intervallo in intervalli_tmp:
        if not intervalli:
            intervalli.append(intervallo)
        else:
            last_start, last_end = intervalli[-1]
            curr_start, curr_end = intervallo
            if curr_start <= last_end:
                intervalli[-1] = (last_start, max(last_end, curr_end))
            else:
                intervalli.append((curr_start, curr_end))

    durata_totale = sum([s.servizio_durata or 30 for s in servizi])
    durata = timedelta(minutes=durata_totale)
    slot_step = timedelta(minutes=15)

    # Controlla se ci sono preferenze per operatrici DIVERSE
    preferenze_operatori = [item.get("operatore_id") for item in servizi_items]
    preferenze_univoche = set(p for p in preferenze_operatori if p is not None)
    has_diverse_preferenze = len(preferenze_univoche) > 1

    # NUOVO: disabilita il primo pass se esistono preferenze per-servizio
    if not has_diverse_preferenze and not has_per_service_prefs:
        for start, end in intervalli:
            slot = datetime.combine(data, start)
            fine = datetime.combine(data, end)
            while slot + durata <= fine:
                operatori_idonei = []
                for op in operatori_disponibili:
                    slot_corrente_temp = slot
                    ok = True
                    for servizio_item in servizi_items:
                        servizio_id = int(servizio_item.get("servizio_id"))
                        durata_servizio = next((s.servizio_durata or 30 for s in servizi if s.id == servizio_id), 30)
                        durata_td = timedelta(minutes=durata_servizio)
                        inizio = slot_corrente_temp
                        fine_servizio = slot_corrente_temp + durata_td

                        if op.id not in servizi_operatori[servizio_id]:
                            ok = False
                            break
                        disponibile, _ = operatore_disponibile(op.id, inizio, fine_servizio)
                        if not disponibile:
                            ok = False
                            break
                        slot_corrente_temp = fine_servizio
                    if ok:
                        operatori_idonei.append(op)

                if operatori_idonei:
                    preferenze = [servizio_item.get("operatore_id") for servizio_item in servizi_items]
                    preferenze = [int(x) for x in preferenze if x]
                    op_scelto = None
                    if preferenze:
                        if all(x == preferenze[0] for x in preferenze) and any(op.id == preferenze[0] for op in operatori_idonei):
                            op_scelto = next(op for op in operatori_idonei if op.id == preferenze[0])
                    if not op_scelto:
                        op_scelto = random.choice(operatori_idonei)

                    operatori_catena = [op_scelto.id] * len(servizi_items)
                    orari.append(slot.strftime("%H:%M"))
                    slot_operatori[slot.strftime("%H:%M")] = operatori_catena
                slot += slot_step

    # Secondo pass (a cascata): eseguito SEMPRE
    if servizi_items:
        for start, end in intervalli:
            slot = datetime.combine(data, start)
            fine = datetime.combine(data, end)
            while slot + durata <= fine:
                slot_str = slot.strftime("%H:%M")
                if slot_str in slot_operatori:
                    slot += slot_step
                    continue
                assegnati = []
                slot_corrente_temp = slot
                fallito = False
                for servizio_item in servizi_items:
                    servizio_id = int(servizio_item.get("servizio_id"))
                    durata_servizio = next(
                        (s.servizio_durata or 30 for s in servizi if s.id == servizio_id),
                        30
                    )
                    durata_td = timedelta(minutes=durata_servizio)
                    inizio = slot_corrente_temp
                    fine_servizio = slot_corrente_temp + durata_td

                    prefer_op = servizio_item.get("operatore_id")

                    # Costruzione candidati
                    if prefer_op:
                        try:
                            prefer_op_int = int(prefer_op)
                        except Exception:
                            prefer_op_int = None

                        if (
                            prefer_op_int is not None
                            and prefer_op_int in servizi_operatori.get(servizio_id, [])
                        ):
                            candidate_ops = [op for op in operatori_disponibili if op.id == prefer_op_int]
                        else:
                            fallito = True
                            break
                    else:
                        # Nessuna operatrice scelta:
                        # 1) prova PRIMA sulla stessa colonna dell'operatrice assegnata al servizio precedente (se esiste),
                        # 2) se non disponibile, prova su tutte le altre colonne abilitate.
                        candidate_ops = [op for op in operatori_disponibili if op.id in servizi_operatori.get(servizio_id, [])]

                        # Priorità stessa colonna: se esiste assegnazione precedente
                        if assegnati:
                            prev_op_id = assegnati[-1]
                            # se la precedente colonna è abilitata per questo servizio, mettila davanti
                            if prev_op_id in servizi_operatori.get(servizio_id, []):
                                candidate_ops.sort(key=lambda op: (op.id != prev_op_id, op.user_nome))

                    if not candidate_ops:
                        fallito = True
                        break

                    scelto = None
                    for op in candidate_ops:
                        disponibile, _ = operatore_disponibile(op.id, inizio, fine_servizio)
                        if disponibile:
                            scelto = op
                            break

                    if not scelto:
                        fallito = True
                        break

                    assegnati.append(scelto.id)
                    slot_corrente_temp = fine_servizio

                if not fallito and len(assegnati) == len(servizi_items):
                    # Verifica coerenza: dove c'è preferenza, l'ID deve combaciare
                    for idx, servizio_item in enumerate(servizi_items):
                        prefer_op = servizio_item.get("operatore_id")
                        if prefer_op:
                            try:
                                prefer_op_int = int(prefer_op)
                            except Exception:
                                prefer_op_int = None
                            if prefer_op_int is None or assegnati[idx] != prefer_op_int:
                                fallito = True
                                break

                if not fallito and len(assegnati) == len(servizi_items):
                    orari.append(slot_str)
                    slot_operatori[slot_str] = assegnati

                slot += slot_step
                
    orari = sorted(list(set(orari)))

    now = datetime.now(pytz_timezone('Europe/Rome')).replace(second=0, microsecond=0)
    if data == now.date():
        orari = [
            o for o in orari
            if datetime.combine(data, datetime.strptime(o, "%H:%M").time()) >= now.replace(tzinfo=None)
        ]
        slot_operatori = {o: slot_operatori[o] for o in orari}

    if data < now.date():
        return jsonify({
            "orari_disponibili": [],
            "operatori_assegnati": {},
            "debug": debug_info + ["Data selezionata già passata"]
        })

    return jsonify({
        "orari_disponibili": orari,
        "operatori_assegnati": slot_operatori,
        "debug": debug_info
    })

@booking_bp.route('/prenota', methods=['POST'])
def prenota(tenant_id):
    data = request.get_json()
    nome = data.get('nome')
    cognome = data.get('cognome')
    telefono = data.get('telefono')
    email = data.get('email')
    data_str = data.get('data')
    ora = data.get('ora')
    servizi = data.get('servizi', [])
    codice_conferma = data.get('codice_conferma')
    business_info = g.db_session.query(BusinessInfo).first()

    # Usa/crea un client di booking con NOME=BOOKING COGNOME=ONLINE (non usare l'id=9999 dummy)
    booking_client = g.db_session.query(Client).filter_by(cliente_nome="BOOKING", cliente_cognome="ONLINE").first()
    if not booking_client:
        booking_client = Client(
            cliente_nome="BOOKING",
            cliente_cognome="ONLINE",
            cliente_cellulare="",
            cliente_email="",
            cliente_sesso="-",
            is_deleted=False
        )
        g.db_session.add(booking_client)
        g.db_session.flush()
    # mantenere la variabile dummy_client per compatibilità con il codice esistente
    dummy_client = booking_client

    booking_session_id = str(uuid.uuid4())
    operatori_assegnati = data.get('operatori_assegnati')

    # Verifica codice conferma basato sulla sessione (collegato al CSRF/token di invia-codice)
    codice_sessione = session.get('codice_conferma')
    email_sessione = session.get('email_conferma')

    # Se non c'è un codice in sessione, la prenotazione non deve poter procedere
    if not codice_sessione or not email_sessione:
        return jsonify({"error": "Codice di conferma non richiesto"}), 400

    # Se manca il codice nel payload o non coincide con quello in sessione o l'email non corrisponde
    if not codice_conferma or codice_conferma != codice_sessione or email != email_sessione:
        return jsonify({"error": "Codice di conferma errato! Riprova"}), 400

    # Validazione campi base
    if not all([nome, telefono, data_str, ora]) or not servizi or not isinstance(servizi, list):
        return jsonify({"error": "Tutti i campi sono obbligatori"}), 400

    # --- PATCH: Usa la stessa logica di orari_disponibili per validare slot e operatori ---
    servizi_ids = [int(s.get("servizio_id")) for s in servizi]
    servizi_objs = g.db_session.query(Service).filter(
        Service.id.in_(servizi_ids),
        Service.is_deleted == False,
        Service.is_visible_online == True
    ).all()
    servizi_map = {s.id: s for s in servizi_objs}
    servizi_operatori = {s.id: [op.id for op in s.operators] for s in servizi_objs}

    data = datetime.strptime(data_str, "%Y-%m-%d").date()
    business_info = g.db_session.query(BusinessInfo).first()
    apertura = business_info.active_opening_time
    chiusura = business_info.active_closing_time

    # --- CONTROLLA LIMITE DURATA/PREZZO SU BLOCCO ---
    durata_totale = sum([s.servizio_durata or 30 for s in servizi_objs])
    totale_prezzo = sum([float(getattr(s, 'servizio_prezzo', 0) or 0) for s in servizi_objs])

    max_durata = business_info.booking_max_durata or 0
    max_prezzo = business_info.booking_max_prezzo or 0
    rule_type_durata = business_info.booking_rule_type_durata or "none"
    rule_msg_durata = business_info.booking_rule_message_durata or "Limite durata superato!"
    rule_type_prezzo = business_info.booking_rule_type_prezzo or "none"
    rule_msg_prezzo = business_info.booking_rule_message_prezzo or "Limite prezzo superato!"

    contiene_pseudoblocco = any([int(s.get("servizio_id")) == 9999 for s in servizi])

    popup_warning = None
    if contiene_pseudoblocco:
        # Blocco per durata
        if max_durata > 0 and durata_totale > max_durata:
            if rule_type_durata == "block":
                return jsonify({
                    "success": False,
                    "errori": [],
                    "popup_error": rule_msg_durata or "Limite durata superato, blocco non consentito."
                }), 400
            elif rule_type_durata == "warning":
                popup_warning = rule_msg_durata or "Limite durata superato, attenzione."
        # Blocco per prezzo
        if max_prezzo > 0 and totale_prezzo > max_prezzo:
            if rule_type_prezzo == "block":
                return jsonify({
                    "success": False,
                    "errori": [],
                    "popup_error": rule_msg_prezzo or "Limite prezzo superato, blocco non consentito."
                }), 400
            elif rule_type_prezzo == "warning":
                popup_warning = rule_msg_prezzo or "Limite prezzo superato, attenzione."

    operatori_disponibili = g.db_session.query(Operator).filter_by(is_deleted=False, is_visible=True).all()
    turni_disponibili = g.db_session.query(OperatorShift).filter(
        OperatorShift.operator_id.in_([o.id for o in operatori_disponibili]),
        OperatorShift.shift_date == data
    ).all()
    turni_per_operatore = {}
    for op in operatori_disponibili:
        op_turni = [
            (
                t.shift_start_time if isinstance(t.shift_start_time, time) else apertura,
                t.shift_end_time if isinstance(t.shift_end_time, time) else chiusura
            )
            for t in turni_disponibili if t.operator_id == op.id
        ]
        if not op_turni:
            op_turni = [(apertura, chiusura)]
        op_turni = [
            (max(start, apertura), min(end, chiusura))
            for (start, end) in op_turni
            if max(start, apertura) < min(end, chiusura)
        ]
        if op_turni:
            turni_per_operatore[op.id] = op_turni

    appuntamenti = g.db_session.query(Appointment).filter(
        Appointment.start_time >= datetime.combine(data, time.min),
        Appointment.start_time < datetime.combine(data + timedelta(days=1), time.min),
        Appointment.is_cancelled_by_client == False
    ).all()
    blocchi_off = g.db_session.query(Appointment).filter(
        Appointment.start_time >= datetime.combine(data, time.min),
        Appointment.start_time < datetime.combine(data + timedelta(days=1), time.min),
        or_(
            Appointment.note.ilike('%OFF%'),
            Appointment.service_id == 9999
        )
    ).all()
    for b in blocchi_off:
        if b not in appuntamenti:
            appuntamenti.append(b)

    # --- LOGICA IDENTICA A orari_disponibili (solo per coerenza calcolo intervalli, ma senza ricontrollare tutto) ---
    durata_totale = sum([s.servizio_durata or 30 for s in servizi_objs])
    durata = timedelta(minutes=durata_totale)
    slot_step = timedelta(minutes=15)
    intervalli_tmp = []
    for op_id, turni in turni_per_operatore.items():
        for t in turni:
            intervalli_tmp.append(t)
    intervalli_tmp.sort()
    intervalli = []
    for intervallo in intervalli_tmp:
        if not intervalli:
            intervalli.append(intervallo)
        else:
            last_start, last_end = intervalli[-1]
            curr_start, curr_end = intervallo
            if curr_start <= last_end:
                intervalli[-1] = (last_start, max(last_end, curr_end))
            else:
                intervalli.append(intervallo)

    # Verifica di coerenza minima su operatori_assegnati rispetto alla richiesta
    if not isinstance(operatori_assegnati, list) or len(operatori_assegnati) != len(servizi):
        return jsonify({
            "success": False,
            "errori": ["Operatori assegnati mancanti o non coerenti. Ricarica la pagina e riprova."]
        }), 400

    # Dove l'utente ha scelto esplicitamente un operatore per il servizio,
    # operatori_assegnati deve coincidere.
    for idx, servizio_item in enumerate(servizi):
        operatore_id_richiesto = servizio_item.get("operatore_id")
        if operatore_id_richiesto:
            try:
                if int(operatori_assegnati[idx]) != int(operatore_id_richiesto):
                    return jsonify({
                        "success": False,
                        "errori": ["La sequenza di operatori richiesta non è più disponibile per questo slot. Ricarica la pagina e riprova."]
                    }), 400
            except Exception:
                return jsonify({
                    "success": False,
                    "errori": ["Operatori assegnati non validi. Ricarica la pagina e riprova."]
                }), 400

    # A questo punto ci fidiamo della catena calcolata da /orari:
    # creiamo gli appuntamenti usando operatori_assegnati in sequenza,
    # rispettando l'ordine dei servizi e l'orario di partenza scelto (data_str + ora).
    risultati = []
    slot_corrente = datetime.strptime(f"{data_str} {ora}", "%Y-%m-%d %H:%M")

    for idx, servizio_item in enumerate(servizi):
        servizio_id = int(servizio_item.get("servizio_id"))
        durata_servizio = servizi_map[servizio_id].servizio_durata or 30
        durata_td = timedelta(minutes=durata_servizio)
        inizio = slot_corrente
        fine = slot_corrente + durata_td
        try:
            operatore_id = int(operatori_assegnati[idx])
        except Exception:
            return jsonify({
                "success": False,
                "errori": ["Operatori assegnati non validi. Ricarica la pagina e riprova."]
            }), 400

        # ulteriore controllo leggero: l'operatore deve essere abilitato al servizio
        if operatore_id not in servizi_operatori.get(servizio_id, []):
            return jsonify({
                "success": False,
                "errori": ["La sequenza di operatori richiesta non è più disponibile per questo slot. Ricarica la pagina e riprova."]
            }), 400

        servizio = servizi_map.get(servizio_id)
        # per questo singolo servizio: l'utente ha selezionato un operatore?
        operatore_id_richiesto = servizio_item.get("operatore_id")
        desiderata_str = "Sì" if operatore_id_richiesto else "NO"
        note = (
            f"PRENOTATO DA BOOKING ONLINE - Nome: {escape(nome)}, Cognome: {escape(cognome)}, "
            f"Telefono: {escape(telefono)}, Email: {escape(email)} - ha selezionato l'operatrice? {desiderata_str}"
        )
        operatore = g.db_session.get(Operator, operatore_id)
        operatore_nome = f"{escape(operatore.user_nome)}" if operatore else ""
        nuovo = Appointment(
            client_id=dummy_client.id,
            operator_id=operatore_id,
            service_id=servizio_id,
            start_time=inizio,
            _duration=servizio.servizio_durata,
            note=note,
            source=AppointmentSource.web,
            booking_session_id=booking_session_id
        )
        g.db_session.add(nuovo)
        try:
            g.db_session.commit()
        except Exception as e:
            g.db_session.rollback()
            print("DB ERROR DURING COMMIT:", repr(e))
            return jsonify({
                "success": False,
                "errori": ["Errore database: " + str(e)]
            }), 500

        risultati.append({
            "success": True,
            "id": nuovo.id,
            "servizio_id": servizio_id,
            "servizio_nome": servizio.servizio_nome,
            "servizio_durata": servizio.servizio_durata or 30,
            "servizio_prezzo": float(getattr(servizio, 'servizio_prezzo', 0) or 0),
            "data": data_str,
            "ora": inizio.strftime("%H:%M"),
            "operatore_nome": operatore_nome
        })
        slot_corrente = fine

# Invio email di conferma SOLO se ci sono risultati
    if risultati:
        # Costruisci URL assoluto per annullare (riusa booking_session_id come token)
        cancel_url = url_for('booking.cancel_booking', tenant_id=tenant_id, token=booking_session_id, _external=True)
        # Prepara i dati in struttura sicura (Jinja farà escaping automaticamente)
        appuntamenti_data = []
        totale_durata = 0
        totale_prezzo = 0
        for r in risultati:
            servizio_obj = g.db_session.get(Service, r['servizio_id'])
            durata_i = int(servizio_obj.servizio_durata or 30)
            prezzo_i = float(getattr(servizio_obj, 'servizio_prezzo', 0) or 0)
            totale_durata += durata_i
            totale_prezzo += prezzo_i
            appuntamenti_data.append({
                "data": _fmt_date_it_short(r['data']),  # solo per email
                "ora": r['ora'],
                "operatore_nome": r.get('operatore_nome', ''),
                "servizio_nome": r['servizio_nome'],
                "durata": durata_i,
                "prezzo": f"{prezzo_i:.2f}"
            })

        business_info = g.db_session.query(BusinessInfo).first()
        company_name = business_info.business_name if business_info and business_info.business_name else "SunBooking"

        # Template sicuro: Jinja escaperà le variabili automaticamente
        template = """
        <p>Ciao {{ nome }},</p>
        <p>La tua richiesta di prenotazione è stata ricevuta! Riceverai la conferma via Whatsapp in orario lavorativo, o comunque al più presto possibile.</p>
        <ul>
        {% for a in appuntamenti %}
          <li>
            <b>Data:</b> {{ a.data }}<br>
            <b>Ora:</b> {{ a.ora }}<br>
            {% if a.operatore_nome %}<b>Operatore:</b> {{ a.operatore_nome }}<br>{% endif %}
            <b>Servizio:</b> {{ a.servizio_nome }}<br>
            <small>
              Durata: {{ a.durata }} min<br>
              Prezzo: {{ a.prezzo }} €
            </small>
          </li>
        {% endfor %}
        </ul>
        <div style="padding:12px; background:#f2f2f2; margin:20px 0; border-radius:8px;">
        <b>Totale durata:</b> {{ totale_durata }} min &nbsp; | &nbsp; <b>Totale costo:</b> €{{ totale_prezzo }}
        </div>
        <p style="margin-top:16px;">
          Non puoi venire? Puoi annullare qui: <a href="{{ cancel_url }}">Annulla prenotazione</a>
        </p>
        <p>Grazie per aver scelto {{ company_name }}!</p>
        """

        riepilogo = render_template_string(
            template,
            nome=nome,
            appuntamenti=appuntamenti_data,
            totale_durata=totale_durata,
            totale_prezzo=f"{totale_prezzo:.2f}",
            company_name=company_name,
            cancel_url=cancel_url
        )

        try:
            invia_email_async(
                to_email=email,
                subject=f'{company_name} - Nuova prenotazione - {escape(nome)}',
                html_content=riepilogo,
                from_email=None
            )
        except Exception as e:
            print(f"ERROR queueing confirmation email: {repr(e)}")
        
        # Invio email all'admin (stesso approccio sicuro)
        admin_email = business_info.email if business_info and business_info.email else None
        admin_riepilogo = render_template_string(
            """
    <div style="font-size:1.5em;">Nuova prenotazione:</div><div style="font-size:2.8em; color:red;"> {{ nome }} {{ cognome }}</div>
            <div style="font-size:1.3em;">
            <ul>
            {% for a in appuntamenti %}
              <li>
                <b>Data:</b> {{ a.data }} - <b>Ora:</b> {{ a.ora }} - <b>Servizio:</b> {{ a.servizio_nome }}
              </li>
            {% endfor %}
            </ul>
            </div>
            <div style="padding:12px; background:#f2f2f2; margin:20px 0; border-radius:8px; font-size:1.3em;">
            <b>Totale durata:</b> {{ totale_durata }} min &nbsp; | &nbsp; <b>Totale costo:</b> €{{ totale_prezzo }}
            </div>
            """,
            nome=nome,
            cognome=cognome,
            appuntamenti=appuntamenti_data,
            totale_durata=totale_durata,
            totale_prezzo=f"{totale_prezzo:.2f}"
        )
        admin_email = business_info.email if business_info and business_info.email else None
        if admin_email:
            try:
                invia_email_async(
                    to_email=admin_email,
                    subject= f'{company_name} - Nuova prenotazione - {escape(nome)}',
                    html_content=admin_riepilogo,
                    from_email=None
                )
            except Exception as e:
                print(f"ERROR queueing admin email: {repr(e)}")

    return jsonify({
        "success": len(risultati) > 0,
        "prenotazioni": risultati,
        "errori": [],
        "popup_warning": popup_warning
    })

@booking_bp.route('/cancel/<token>', methods=['GET', 'POST'])
def cancel_booking(tenant_id, token):
    """
    Step 1 (GET): mostra pagina di conferma annullamento.
    Step 2 (POST): cancella tutti gli appuntamenti della sessione (booking_session_id == token).
    Idempotente: se il token non esiste più, restituisce 404.
    """
    # valida formato UUID per evitare query inutili
    try:
        uuid.UUID(str(token))
    except Exception:
        return render_template_string("<p>Link non valido.</p>"), 404

    try:
        appts = (
            g.db_session.query(Appointment)
            .filter(Appointment.booking_session_id == str(token))
            .all()
        )
        if not appts:
            return render_template_string("<p>Link non valido o già usato.</p>"), 404

        # Filtra solo appuntamenti FUTURI (non già passati)
        now_rome = _now_rome()
        # Se start_time è naive, è già in ora locale italiana - confronta con now naive
        now_naive = now_rome.replace(tzinfo=None)
        appts_future = [a for a in appts if a.start_time and a.start_time > now_naive]
        if not appts_future:
            return render_template_string("""
                <!doctype html>
                <meta charset="utf-8">
                <title>Annullamento non possibile</title>
                <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:720px;margin:40px auto;padding:20px;">
                  <h2>Annullamento non possibile</h2>
                  <p>La prenotazione non può essere annullata perché l'appuntamento è già passato.</p>
                </div>
            """), 400

        biz = g.db_session.query(BusinessInfo).first()
        company_name = (getattr(biz, 'business_name', None) or "SunBooking")

        # Usa solo appuntamenti futuri per conteggio e cancellazione
        count = len(appts_future)

        if request.method == 'GET':
            # SOLO pagina di conferma, nessuna cancellazione ancora
            first_appt = appts_future[0]
            # start_time è già in ora locale italiana (naive) - non serve conversione
            dt = first_appt.start_time
            data_str = dt.strftime('%d/%m/%Y') if dt else ''
            ora_str = dt.strftime('%H:%M') if dt else ''

            return render_template_string("""
                <!doctype html>
                <meta charset="utf-8">
                <title>Conferma annullamento</title>
                <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:720px;margin:40px auto;padding:20px;">
                  <h2>Vuoi annullare la prenotazione?</h2>
                  {% if data_str and ora_str %}
                    <p><b>Data:</b> {{ data_str }} &nbsp; <b>Ora:</b> {{ ora_str }}</p>
                  {% endif %}
                  <p>Questo annullerà {{ count }} appuntamento/i collegati a questa prenotazione.</p>
                  <form method="post">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <button type="submit" style="background:#c0392b;color:#fff;border:none;padding:10px 18px;border-radius:4px;cursor:pointer;font-size:clamp(1.1rem, 2.6vw, 1.6rem);">
                      Conferma annullamento
                    </button>
                  </form>
                  <p style="margin-top:16px;color:#666;">{{ company_name }}</p>
                </div>
            """, count=count, company_name=company_name,
                 data_str=data_str, ora_str=ora_str, csrf_token=csrf_token)

        # POST: soft-delete solo appuntamenti futuri
        for a in appts_future:
            a.is_cancelled_by_client = True  # Imposta soft-delete
        g.db_session.commit()  # Commit delle modifiche

        # --- INVIO EMAIL NOTIFICA ALL'ADMIN ---
        admin_email = getattr(biz, 'email', None)
        if admin_email:
            # Estrai dati dal primo appuntamento futuro (assumendo sessione multi-servizio)
            first_appt = appts_future[0]
            note = first_appt.note or ""
            # Parsing semplice delle note per estrarre dati cliente (fallback se non presente)
            nome = "N/A"
            cognome = "N/A"
            telefono = "N/A"
            email_cliente = "N/A"
            if "Nome:" in note and "Cognome:" in note:
                try:
                    nome_part = note.split("Nome: ")[1].split(",")[0].strip()
                    cognome_part = note.split("Cognome: ")[1].split(",")[0].strip()
                    telefono_part = note.split("Telefono: ")[1].split(",")[0].strip()
                    email_part = note.split("Email: ")[1].split(" - ")[0].strip()
                    nome = nome_part
                    cognome = cognome_part
                    telefono = telefono_part
                    email_cliente = email_part
                except:
                    pass  # Fallback a N/A

            # Costruisci lista appuntamenti annullati (solo futuri)
            appuntamenti_annullati = []
            totale_durata = 0
            totale_prezzo = 0
            for a in appts_future:
                servizio = g.db_session.get(Service, a.service_id)
                operatore = g.db_session.get(Operator, a.operator_id)
                durata = int(getattr(a, '_duration', 0) or 30)
                prezzo = float(getattr(servizio, 'servizio_prezzo', 0) or 0)
                totale_durata += durata
                totale_prezzo += prezzo
                appuntamenti_annullati.append({
                    "data": _fmt_date_it_short(a.start_time.strftime('%Y-%m-%d') if a.start_time else ''),
                    "ora": a.start_time.strftime('%H:%M') if a.start_time else '',
                    "operatore_nome": getattr(operatore, 'user_nome', '') if operatore else '',
                    "servizio_nome": getattr(servizio, 'servizio_nome', '') if servizio else '',
                    "durata": durata,
                    "prezzo": f"{prezzo:.2f}",
                    "note": a.note or ''
                })

            # Template email distintivo per annullamento (rosso, titolo "Annullamento Prenotazione")
            cancel_template = """
            <div style="font-size:1.5em; color:red;">Annullamento Prenotazione</div>
            <div style="font-size:2.8em; color:red;">{{ nome }} {{ cognome }}</div>
            <div style="font-size:1.1em; margin-bottom:16px;">
                <b>Email:</b> {{ email_cliente }}<br>
                <b>Telefono:</b> {{ telefono }}
            </div>
            <div style="font-size:1.3em;">
            <ul>
            {% for a in appuntamenti %}
              <li>
                <b>Data:</b> {{ a.data }} - <b>Ora:</b> {{ a.ora }} - <b>Servizio:</b> {{ a.servizio_nome }}
                {% if a.operatore_nome %}<br><b>Operatore:</b> {{ a.operatore_nome }}{% endif %}
                <br><small>Durata: {{ a.durata }} min - Prezzo: {{ a.prezzo }} €</small>
                {% if a.note %}<br><small>Note: {{ a.note }}</small>{% endif %}
              </li>
            {% endfor %}
            </ul>
            </div>
            <div style="padding:12px; background:#ffe6e6; margin:20px 0; border-radius:8px; font-size:1.3em; border:1px solid #ffcccc;">
            <b>Totale durata:</b> {{ totale_durata }} min &nbsp; | &nbsp; <b>Totale costo:</b> €{{ totale_prezzo }}
            </div>
            <p style="color:#666;">{{ company_name }} - Annullamento effettuato dal cliente via email.</p>
            """

            riepilogo_admin = render_template_string(
                cancel_template,
                nome=nome,
                cognome=cognome,
                email_cliente=email_cliente,
                telefono=telefono,
                appuntamenti=appuntamenti_annullati,
                totale_durata=totale_durata,
                totale_prezzo=f"{totale_prezzo:.2f}",
                company_name=company_name
            )

            try:
                invia_email_async(
                    to_email=admin_email,
                    subject=f'{company_name} - Annullamento Prenotazione - {nome} {cognome}',
                    html_content=riepilogo_admin,
                    from_email=None
                )
            except Exception as e:
                print(f"[CANCEL] ERROR sending admin email: {repr(e)}")

        return render_template_string("""
            <!doctype html>
            <meta charset="utf-8">
            <title>Prenotazione annullata</title>
            <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:720px;margin:40px auto;padding:20px;">
              <h2>Prenotazione annullata</h2>
              <p>La tua prenotazione è stata annullata con successo.</p>
              <p>Appuntamenti cancellati: {{ count }}</p>
              <p style="color:#666;">{{ company_name }}</p>
            </div>
        """, count=count, company_name=company_name)
    except Exception as e:
        g.db_session.rollback()
        print(f"[CANCEL] error: {repr(e)}")
        return render_template_string("<p>Errore durante la cancellazione.</p>"), 500
    
@booking_bp.route('/invia-codice', methods=['POST'])
def invia_codice(tenant_id):
    business_info = g.db_session.query(BusinessInfo).first()
    company_name = business_info.business_name if business_info and business_info.business_name else "SunBooking"

    cooldown = 300  # 5 minuti
    now_ts = datetime.now().timestamp()
    last_sent = session.get('last_code_sent_at', 0)
    attempts = session.get('code_send_attempts', 0)

    # se l'ultimo invio è passato oltre il cooldown, azzera il contatore
    if last_sent and now_ts - last_sent >= cooldown:
        attempts = 0
        session['code_send_attempts'] = 0

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Dati mancanti."}), 400

    email = data.get('email', '').strip()
    nome = data.get('nome', '').strip()
    cognome = data.get('cognome', '').strip()
    telefono = data.get('telefono', '').strip()

    if not all([email, nome, cognome, telefono]):
        return jsonify({"success": False, "error": "Tutti i campi sono obbligatori."}), 400

    if '@' not in email or '.' not in email.split('@')[-1]:
        return jsonify({"success": False, "error": "Indirizzo email non valido."}), 400

    # Blocca SOLO dal 3° tentativo se entro cooldown
    if attempts >= 2 and last_sent and now_ts - last_sent < cooldown:
        remaining = int(cooldown - (now_ts - last_sent))
        return jsonify({"success": False, "error": f"Puoi inviare un nuovo codice tra {remaining} secondi."}), 429

    # Procedi con l'invio: genera codice, registra timestamp, e aggiorna tentativi
    codice = ''.join(random.choices(string.digits, k=6))
    now_ts = datetime.now().timestamp()
    session['codice_conferma'] = codice
    session['email_conferma'] = email
    session['last_code_sent_at'] = now_ts
    session['code_send_attempts'] = attempts + 1

    # Costruisci email anti-spam con struttura professionale
    html_content = f"""<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Codice di conferma</title>
</head>
<body style="margin:0; padding:0; font-family: Arial, Helvetica, sans-serif; background-color: #f4f4f4;">
    <!--[if mso]>
    <table role="presentation" width="600" align="center" cellpadding="0" cellspacing="0" border="0">
    <tr><td>
    <![endif]-->
    
    <!-- Preheader nascosto per anteprima email -->
    <div style="display:none; max-height:0; overflow:hidden;">
        Il tuo codice: {escape(codice)} - Valido per 10 minuti
    </div>
    
    <table role="presentation" style="max-width:600px; margin:20px auto; background:#ffffff; border-radius:8px; border:1px solid #e0e0e0;" cellpadding="0" cellspacing="0" width="100%">
        <tr>
            <td style="padding:30px 40px;">
                <h2 style="color:#333333; margin:0 0 20px 0; font-size:22px;">Conferma prenotazione</h2>
                
                <p style="color:#555555; font-size:15px; line-height:1.6; margin:0 0 20px 0;">
                    {escape(nome)} {escape(cognome)}, ecco il codice per completare la prenotazione:
                </p>
                
                <div style="background:#f8f9fa; border:2px dashed #007bff; border-radius:8px; padding:20px; text-align:center; margin:25px 0;">
                    <span style="font-size:32px; font-weight:bold; letter-spacing:8px; color:#007bff;">{escape(codice)}</span>
                </div>
                
                <p style="color:#555555; font-size:14px; line-height:1.6; margin:20px 0 0 0;">
                    Inserisci questo codice nella pagina di prenotazione.<br>
                    Il codice scade tra 10 minuti.
                </p>
            </td>
        </tr>
        <tr>
            <td style="padding:20px 40px; background:#f8f9fa; border-top:1px solid #e0e0e0;">
                <p style="color:#888888; font-size:12px; margin:0; line-height:1.5;">
                    <strong>{escape(company_name)}</strong><br>
                    Se non hai richiesto questo codice, ignora questa email.<br>
                    Per assistenza contattaci telefonicamente.
                </p>
            </td>
        </tr>
    </table>
    
    <!--[if mso]>
    </td></tr>
    </table>
    <![endif]-->
</body>
</html>"""

    # Plain text version (IMPORTANTE per evitare spam!)
    plain_text = f"""{nome} {cognome}, ecco il codice per completare la prenotazione su {company_name}:

CODICE: {codice}

Inserisci questo codice nella pagina di prenotazione.
Il codice scade tra 10 minuti.

Se non hai richiesto questo codice, ignora questa email.

{company_name}"""

    # Invia email con ENTRAMBE le versioni (HTML + plain text)
    try:
        invia_email_async(
            to_email=email,
            subject=f'Codice {codice} - {company_name}',  # Codice nel subject aiuta!
            html_content=html_content,
            plain_text=plain_text,  # AGGIUNGI QUESTO!
            from_email=None
        )
        return jsonify({"success": True})
    except Exception as e:
        print("ERROR queueing email:", repr(e))
        return jsonify({"success": False, "error": "Errore durante l'invio dell'email."}), 500

def _get_wbiztool_creds(tenant_id: str):
    suffix = _tenant_env_prefix(tenant_id)  # es: T1
    k_api = f'WBIZTOOL_API_KEY_{suffix}'
    k_cli = f'WBIZTOOL_CLIENT_ID_{suffix}'
    k_wa  = f'WBIZTOOL_WHATSAPP_CLIENT_ID_{suffix}'

    api_key = os.environ.get(k_api)
    client_id = os.environ.get(k_cli)
    wa_client_id = os.environ.get(k_wa)

    if not (api_key and client_id and wa_client_id):
        _wa_dbg(tenant_id, f"Credenziali WBIZTOOL assenti per {suffix}. Nessun fallback.")
        return None

    try:
        creds = {
            "api_key": str(api_key).strip(),
            "client_id": int(str(client_id).strip()),
            "wa_client_id": int(str(wa_client_id).strip())
        }
        _wa_dbg(tenant_id, f"Credenziali WBIZTOOL caricate (suffix={suffix})")
        return creds
    except Exception as e:
        _wa_dbg(tenant_id, f"Env WBIZTOOL {suffix} non parseabili: {repr(e)}")
        return None

def _prepare_wbiz_phone(phone: str):
    """Ritorna (numero_pulito, country_code) stile calendar.py"""
    numero_pulito = re.sub(r'\D', '', str(phone or ''))
    if numero_pulito.startswith('00'):
        numero_pulito = numero_pulito.lstrip('0')
    if not numero_pulito:
        return '', ''
    
    # MODIFICA: Aggiungi 39 solo se non c'è già E se la lunghezza suggerisce un numero italiano senza prefisso (<= 10 cifre)
    # I numeri internazionali (es. 41...) o italiani con prefisso (39...) sono solitamente > 10 cifre.
    if not numero_pulito.startswith('39') and len(numero_pulito) <= 10:
        numero_pulito = '39' + numero_pulito
        
    country_code = '39' if numero_pulito.startswith('39') else numero_pulito[:2]
    return numero_pulito, country_code

def _services_bullet_for_contiguous_block(session, appt) -> str:
    """
    Restituisce una lista puntata dei servizi per il blocco contiguo dell'appuntamento:
    include appuntamenti dello stesso cliente nello stesso giorno che si toccano o si sovrappongono,
    escludendo OFF e servizio 9999.
    """
    try:
        if not appt or not appt.client_id or not appt.start_time:
            return ""
        day = appt.start_time.date()
        # carica tutti gli appt del cliente nel giorno (escludi OFF e 9999)
        appts = (
            session.query(Appointment)
            .filter(
                Appointment.client_id == appt.client_id,
                Appointment.start_time >= datetime.combine(day, time.min),
                Appointment.start_time < datetime.combine(day + timedelta(days=1), time.min),
                ~Appointment.note.ilike('%OFF%'),
                Appointment.service_id != 9999,
                Appointment.is_cancelled_by_client == False
            )
            .order_by(Appointment.start_time.asc())
            .all()
        )
        if not appts:
            return ""

        # prepara start/end per catena contigua
        starts, ends = [], []
        for a in appts:
            start = a.start_time
            dur_min = int(getattr(a, "_duration", 0) or 0)
            if dur_min <= 0:
                svc = session.get(Service, a.service_id) if a.service_id else None
                dur_min = int(getattr(svc, "servizio_durata", 0) or 30)
            ends.append(start + timedelta(minutes=dur_min))
            starts.append(start)

        # trova indice dell'appuntamento corrente
        try:
            idx = next(i for i, a in enumerate(appts) if a.id == appt.id)
        except StopIteration:
            return ""

        # espandi a sinistra/destra finché contigui (overlap o adiacenti)
        lower = idx
        while lower - 1 >= 0 and ends[lower - 1] >= starts[lower]:
            lower -= 1
        upper = idx
        while upper + 1 < len(appts) and starts[upper + 1] <= ends[upper]:
            upper += 1

        # costruisci elenco servizi (in ordine)
        lines = []
        for i in range(lower, upper + 1):
            svc = session.get(Service, appts[i].service_id) if appts[i].service_id else None
            label = ((getattr(svc, "servizio_nome", "") or "").strip() if svc else "")
            if label:
                lines.append(f"• {label}")
        return "\n".join(lines)
    except Exception:
        return ""

def _render_morning_text(session, template: str, item: dict) -> str:
    """Sostituisce {{nome}}, {{cognome}}, {{data}}, {{ora}}, {{azienda}}"""
    try:
        appt = session.get(Appointment, item["appointment_id"])
        cli = session.get(Client, item["client_id"]) if item.get("client_id") else None
        biz = session.query(BusinessInfo).first()
        dt = getattr(appt, 'start_time', None)
        data_str = dt.strftime('%d/%m/%Y') if dt else ''
        ora_str = dt.strftime('%H:%M') if dt else ''
        nome = (getattr(cli, 'cliente_nome', '') or '').strip()
        cognome = (getattr(cli, 'cliente_cognome', '') or '').strip()
        azienda = (getattr(biz, 'business_name', '') or '').strip()
        nome_fmt = " ".join([w.capitalize() for w in nome.split()])
        servizi_str = _services_bullet_for_contiguous_block(session, appt)

        txt = (template or "")
        return (txt.replace('{{nome}}', nome_fmt)
                   .replace('{{cognome}}', cognome)
                   .replace('{{data}}', data_str)
                   .replace('{{ora}}', ora_str)
                   .replace('{{azienda}}', azienda)
                   .replace('{{servizi}}', ("\n" + servizi_str + "\n") if servizi_str else ""))
    except Exception:
        return template or ''

def _send_wbiztool_message(creds: dict, to_phone: str, text: str) -> bool:
    """Invia con WbizToolClient come in calendar.py"""
    try:
        numero_pulito, country_code = _prepare_wbiz_phone(to_phone)
        if not numero_pulito:
            print("[WBIZTOOL] Numero vuoto dopo normalizzazione")
            return False

        client = WbizToolClient(api_key=creds["api_key"], client_id=creds["client_id"])
        resp = client.send_message(
            phone=numero_pulito,
            msg=text or "",
            msg_type=0,
            whatsapp_client=creds["wa_client_id"],
            country_code=country_code
        )
        # Esiti possibili: dict {"status":1} oppure oggetto/Response 2xx
        if isinstance(resp, dict):
            ok = resp.get("status") == 1
            if not ok:
                print(f"[WBIZTOOL] send failed dict: {resp}")
            return ok
        status = getattr(resp, 'status_code', None)
        if status is None:
            # fallback best-effort
            return True
        ok = 200 <= status < 300
        if not ok:
            body = getattr(resp, 'text', None) or getattr(resp, 'content', None)
            print(f"[WBIZTOOL] http failed {status} body={body}")
        return ok
    except Exception as e:
        print(f"[WBIZTOOL] ERROR: {repr(e)}")
        return False
    
def _build_today_targets(session, start_from=None) -> list:
    """
    Seleziona gli appuntamenti odierni ordinati, esclusi OFF e servizio 9999,
    esclude il client finto BOOKING/ONLINE. Deduplica blocchi contigui per cliente.
    Ritorna una lista di dict: {"appointment_id", "client_id", "phone"}.
    Regola: il cellulare viene SEMPRE letto dal record Client usando client_id.
    Non si estrae nulla dalla nota.
    """
    today = _now_rome().date()
    start = datetime.combine(today, time.min)
    end = datetime.combine(today + timedelta(days=1), time.min)

    # Se richiesto, limita dalla fascia oraria indicata (stesso giorno)
    if isinstance(start_from, datetime) and start_from.date() == today and start_from > start:
        start = start_from

    # Client finto "BOOKING ONLINE" da escludere
    booking_dummy = session.query(Client).filter_by(cliente_nome="BOOKING", cliente_cognome="ONLINE").first()
    dummy_id = booking_dummy.id if booking_dummy else None

    # Query semplice sugli appuntamenti del giorno, senza join sui client né filtri su cliente_cellulare
    q = session.query(Appointment).filter(
        Appointment.start_time >= start,
        Appointment.start_time < end,
        ~Appointment.note.ilike('%OFF%'),
        Appointment.service_id != 9999,
        Appointment.is_cancelled_by_client == False
    )
    if dummy_id:
        q = q.filter(Appointment.client_id != dummy_id)

    q = q.order_by(Appointment.start_time.asc())
    apps = q.all()

    targets = []
    last_end_by_client = {}

    for a in apps:
        c_id = a.client_id
        # durata in minuti; fallback 0
        dur_min = int(getattr(a, "_duration", 0) or 0)
        a_start = a.start_time
        a_end = a_start + timedelta(minutes=dur_min)

        last_end = last_end_by_client.get(c_id)
        if last_end and a_start <= last_end:
            # blocco contiguo per lo stesso cliente: salta (già coperto)
            last_end_by_client[c_id] = max(last_end, a_end)
            continue

        # Preleva SEMPRE il cellulare dal record Client (client_id)
        client = session.get(Client, c_id) if c_id else None
        phone_raw = getattr(client, 'cliente_cellulare', '') if client else ''
        phone = _normalize_msisdn(phone_raw)

        # Log per debug se il DB contiene valore vuoto/placeholder
        if not phone_raw:
            _wa_dbg(c_id or "?", f"cliente {c_id} ha cliente_cellulare vuoto in DB")
        elif phone_raw == '000000000':
            _wa_dbg(c_id or "?", f"cliente {c_id} ha cliente_cellulare placeholder '000000000' in DB")

        targets.append({
            "appointment_id": a.id,
            "client_id": c_id,
            "phone": phone  # valore normalizzato preso dal DB (può essere '' o '000000000' normalizzato)
        })
        last_end_by_client[c_id] = a_end

    return targets

def process_morning_tick(app, tenant_id: str):
    """
    Ogni 60s: al minuto del reminder costruisce la coda del giorno.
    Poi invia 1 messaggio al minuto finché la coda non è vuota.
    """
    lock = _MORNING_LOCKS.get(tenant_id)
    if lock is None:
        lock = threading.Lock()
        _MORNING_LOCKS[tenant_id] = lock

    with lock:
        SessionFactory = app.config['DB_SESSIONS'][tenant_id]
        session = SessionFactory()
        try:
            biz = session.query(BusinessInfo).first()
            if not biz or not getattr(biz, 'whatsapp_morning_reminder_enabled', False):
                _wa_dbg(tenant_id, "disabilitato o BusinessInfo assente")
                _MORNING_STATE.pop(tenant_id, None)
                return

            reminder_time = getattr(biz, 'whatsapp_morning_reminder_time', time(8, 0))
            msg_text = getattr(biz, 'whatsapp_message_morning', None)
            if not msg_text or not isinstance(reminder_time, time):
                _wa_dbg(tenant_id, "config mancante (msg/time)")
                return

            now = _now_rome()
            now_time = now.time()
            if getattr(now_time, 'tzinfo', None) is not None:
                now_time = now_time.replace(tzinfo=None)

            st = _MORNING_STATE.get(tenant_id)
            has_active_queue = bool(st and st.get("date") == now.date() and st.get("idx", 0) < len(st.get("queue", [])))
            is_reminder_minute = (now_time.hour == reminder_time.hour and now_time.minute == reminder_time.minute)

            # Costruisci la coda solo al minuto del reminder
            if is_reminder_minute and (not st or st.get("date") != now.date()):
                queue = _build_today_targets(session, start_from=None)
                if not queue:
                    _wa_dbg(tenant_id, "coda vuota per oggi")
                    return
                _MORNING_STATE[tenant_id] = {
                    "date": now.date(),
                    "queue": queue,
                    "idx": 0,
                    "last_sent_minute": None
                }
                st = _MORNING_STATE[tenant_id]
                _wa_dbg(tenant_id, f"coda costruita: {len(queue)} target")

            # Se non è il minuto del reminder e non c'è coda attiva, esci
            if not (is_reminder_minute or has_active_queue):
                _wa_dbg(tenant_id, f"skip: no reminder minute and no active queue. ora={now_time.strftime('%H:%M')}")
                return

            # Se non c'è più nulla da inviare, reset
            if not st or st.get("idx", 0) >= len(st.get("queue", [])):
                _wa_dbg(tenant_id, "nessun messaggio da inviare")
                _MORNING_STATE.pop(tenant_id, None)
                return

            # 1 invio per minuto (ancorato al minuto intero)
            current_slot = now.replace(second=0, microsecond=0)
            last_slot = st.get("last_sent_minute")
            can_send = (last_slot is None) or (current_slot > last_slot)

            _wa_dbg(tenant_id, f"tick: idx={st['idx']}/{len(st['queue'])}, last_slot={last_slot}, current_slot={current_slot}, can_send={can_send}")

            if can_send:
                item = st["queue"][st["idx"]]
                st["idx"] += 1  # avanza sempre, anche su errore

                try:
                    text_to_send = _render_morning_text(session, msg_text, item)
                except Exception as e:
                    _wa_dbg(tenant_id, f"render error appt_id={item.get('appointment_id')}: {repr(e)}")
                    text_to_send = msg_text or ""

                creds = _get_wbiztool_creds(tenant_id)
                ok = False
                if creds:
                    try:
                        ok = _send_wbiztool_message(creds, item["phone"], text_to_send)
                    except Exception as e:
                        _wa_dbg(tenant_id, f"send raised exception appt_id={item.get('appointment_id')}: {repr(e)}")
                        ok = False
                else:
                    _wa_dbg(tenant_id, "credenziali mancanti")

                _wa_dbg(tenant_id, f"inviato={ok} appt_id={item['appointment_id']} -> {item['phone']}")
                st["last_sent_minute"] = current_slot  # blocca ulteriori invii in questo minuto

            # Fine coda -> reset
            if st.get("idx", 0) >= len(st.get("queue", [])):
                _wa_dbg(tenant_id, "tutti i messaggi inviati: reset")
                _MORNING_STATE.pop(tenant_id, None)
            else:
                _MORNING_STATE[tenant_id] = st

            session.commit()
        except Exception as e:
            session.rollback()
            print(f"[WA-MORNING][{tenant_id}] error: {repr(e)}")
            raise
        finally:
            try:
                session.close()
            finally:
                try:
                    SessionFactory.remove()
                except Exception:
                    pass


#===== INVIO WHATSAPP OPERATORI ================================
def _normalize_for_wbiz(numero: str):
    raw = (str(numero or '')).strip().replace(' ', '')
    if not raw:
        return None, None  # numero, country
    if raw.startswith('+'):
        numero_norm = raw
    elif raw and raw[0].isdigit():
        if raw.startswith('3'):
            numero_norm = ('+' + raw) if len(raw) > 10 else ('+39' + raw)
        else:
            numero_norm = '+' + raw
    else:
        numero_norm = raw

    numero_pulito = re.sub(r'\D', '', numero_norm or '')
    if numero_pulito.startswith('00'):
        numero_pulito = numero_pulito.lstrip('0')
    if not numero_pulito:
        return None, None
    country_code = '39' if numero_pulito.startswith('39') else numero_pulito[:2]
    return numero_pulito, country_code

def _fmt_data_italiana(dt):
    giorni = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"]
    mesi = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    return f"{giorni[dt.weekday()]} {dt.day} {mesi[dt.month - 1]}"

def _build_operator_targets_for_tomorrow(session, require_phone: bool = True):  # AGGIUNTO: parametro session
    tomorrow = datetime.now().date() + timedelta(days=1)
    
    # Query operators who are active, visible, not machines, and opted for WhatsApp notifications
    operators = session.query(Operator).filter(
        Operator.is_deleted == False,
        Operator.is_visible == True,
        Operator.user_tipo != 'macchinario',
        Operator.notify_turni_via_whatsapp == True
    ).all()
    
    targets = []
    for op in operators:
        phone, country = _normalize_for_wbiz(op.user_cellulare)
        if require_phone and (not phone or len(phone) < 4):
            continue
        
        # Fetch the shift for tomorrow
        shift = session.query(OperatorShift).filter(
            OperatorShift.operator_id == op.id,
            OperatorShift.shift_date == tomorrow
        ).first()
        
        if not shift:
            continue  # Skip operators with no shift for tomorrow

        if shift.shift_start_time == shift.shift_end_time:
            continue  # Skip day off
        
        # Fetch appointments for tomorrow, excluding cancelled ones
        appointments = session.query(Appointment).filter(
            Appointment.operator_id == op.id,
            Appointment.is_cancelled_by_client == False,
            func.date(Appointment.start_time) == tomorrow
        ).order_by(Appointment.start_time.asc()).all()
        
        schedule_items = []
        first_app_label = None
        first_app_time = None
        pausa_label = None
        pausa_time = None
        
        for appt in appointments:
            # Filter out appointments outside the shift time
            if shift:
                shift_start = shift.shift_start_time
                shift_end = shift.shift_end_time
                appt_time = appt.start_time.time()
                if appt_time < shift_start or appt_time >= shift_end:
                    continue
            
            # Determine if it's an OFF slot
            client = appt.client
            service = session.get(Service, appt.service_id) if appt.service_id else None
            
            client_is_dummy = (
                client is None or
                (client.cliente_nome or '').strip().lower() == 'dummy' and
                (client.cliente_cognome or '').strip().lower() == 'dummy'
            )
            
            service_is_dummy = (
                service is None or
                (getattr(service, 'servizio_nome', '') or '').strip().lower() == 'dummy' or
                (getattr(service, 'servizio_tag', '') or '').strip().lower() == 'dummy'
            )
            
            is_off = client_is_dummy or service_is_dummy
            
            if is_off:
                titolo = (appt.note or '').strip()
                label = titolo if titolo else 'OFF'
                duration = appt.duration if isinstance(appt.duration, int) else None
                if label.upper() == 'PAUSA':
                    pausa_label = label
                    pausa_time = appt.start_time.strftime('%H:%M')
            else:
                # Prefer name over tag for service label
                if service:
                    label = (getattr(service, 'servizio_nome', '') or '').strip() or (getattr(service, 'servizio_tag', '') or '').strip()
                else:
                    label = ''
                duration = None
            
            # Set first appointment if not set and not off
            if first_app_label is None and not is_off:
                first_app_label = label or ''
                first_app_time = appt.start_time.strftime('%H:%M')
            
            schedule_items.append({
                "ora": appt.start_time.strftime('%H:%M'),
                "label": label,
                "is_off": is_off,
                "durata": duration
            })
        
        targets.append({
            "operator_id": op.id,
            "operatore_nome": (op.user_nome or "").strip(),  # First name only
            "phone": phone,
            "country_code": country,
            "date": str(tomorrow),
            "shift_start": shift.shift_start_time.strftime('%H:%M') if shift else None,
            "shift_end": shift.shift_end_time.strftime('%H:%M') if shift else None,
            "schedule": schedule_items,
            "primo_app_label": first_app_label,
            "primo_app_time": first_app_time,
            "pausa_label": pausa_label,
            "pausa_time": pausa_time,
        })
    
    return targets

def _render_operator_msg(tpl: str, target: dict):
    tpl = (tpl or "")
    
    lines = []
    for x in target.get('schedule', []):
        if not x:
            continue
        if x.get('is_off'):
            dur = x.get('durata')
            dur_txt = f" ({dur} minuti)" if (isinstance(dur, int) and dur > 0) else ""
            lines.append(f"- {x.get('ora')} {x.get('label')}{dur_txt}")
        else:
            lines.append(f"- {x.get('ora')} {x.get('label')}")
    
    data_it = _fmt_data_italiana(datetime.strptime(target["date"], "%Y-%m-%d"))

    pausa_section = ""
    if target.get("pausa_time"):
        pausa_section = f"Pausa alle {target.get('pausa_time')}"
    
    return (tpl
        .replace("{{operatore}}", target.get("operatore_nome", ""))
        .replace("{{data}}", data_it)
        .replace("{{ora_inizio}}", target.get("shift_start") or "OFF")
        .replace("{{ora_fine}}", target.get("shift_end") or "OFF")
        .replace("{{ora_primo_app}}", target.get("primo_app_time") or "N/D")
        .replace("{{primo_app}}", target.get("primo_app_label") or "N/D")
        .replace("{{ora_pausa}}", target.get("pausa_time") or "")
        .replace("{{pausa}}", target.get("pausa_label") or "")
        .replace("{{sezione_pausa}}", pausa_section)
    )

def preview_operator_notifications(session):  # NOTA: Questa funzione ora prende session come parametro? No, è una funzione helper, ma nel contesto del route, usa g.db_session
    bi = session.query(BusinessInfo).first()
    tpl_default = (  # CAMBIATO: {{pausa_section}} -> {{sezione_pausa}}
    "Ciao {{operatore}},\n\n"
    "Domani {{data}} il tuo turno sarà: {{ora_inizio}}-{{ora_fine}}\n\n"
    "{{sezione_pausa}}"  # CAMBIATO
    "Il primo impegno della giornata sarà alle {{ora_primo_app}} e sarà {{primo_app}}\n\n"
    "Buon lavoro!"
    )
    tpl = (getattr(bi, 'operator_whatsapp_message_template', '') or tpl_default)

    targets = _build_operator_targets_for_tomorrow(session, require_phone=False)

    full = str(request.args.get('full', '') or '').lower() in ('1', 'true', 'yes', 'on')
    preview = []
    for t in targets:
        msg = _render_operator_msg(tpl, t)
        item = {
            "operator_id": t["operator_id"],
            "operatore": t["operatore_nome"],
            "phone": t.get("phone") or "(nessun numero)",
            "date": t["date"],
            "msg_preview": msg[:240] + ("..." if len(msg) > 240 else "")
        }
        if full:
            item["msg_full"] = msg
        preview.append(item)

    return jsonify({
        "enabled": bool(getattr(bi, 'operator_whatsapp_notification_enabled', False)),
        "count": len(preview),
        "items": preview
    })

@booking_bp.route('/operator-notifications/preview', methods=['GET'])  # ASSUMO il nome del route basato sul contesto
def preview_route(tenant_id):
    session = g.db_session  # USA g.db_session nel route
    return preview_operator_notifications(session)
    
def process_operator_tick(app, tenant_id: str):
    """
    Ogni 60s: al minuto del reminder costruisce la coda degli operatori per DOMANI.
    Poi invia 1 messaggio al minuto finché la coda non è vuota.
    Logica multi-tenant allineata a process_morning_tick.
    """
    lock = _OP_LOCKS.get(tenant_id)
    if lock is None:
        lock = threading.Lock()
        _OP_LOCKS[tenant_id] = lock

    with lock:
        SessionFactory = app.config['DB_SESSIONS'][tenant_id]
        session = SessionFactory()
        try:
            biz = session.query(BusinessInfo).first()
            if not biz or not getattr(biz, 'operator_whatsapp_notification_enabled', False):
                _op_dbg(tenant_id, "disabilitato o BusinessInfo assente")
                _OP_STATE_MAP.pop(tenant_id, None)
                return

            reminder_time = getattr(biz, 'operator_whatsapp_notification_time', time(20, 0))
            msg_text = getattr(biz, 'operator_whatsapp_message_template', None)
            if not msg_text or not isinstance(reminder_time, time):
                _op_dbg(tenant_id, "config mancante (msg/time)")
                return

            now = _now_rome()
            now_time = now.time()
            if getattr(now_time, 'tzinfo', None) is not None:
                now_time = now_time.replace(tzinfo=None)

            st = _OP_STATE_MAP.get(tenant_id)
            has_active_queue = bool(st and st.get("date") == now.date() and st.get("idx", 0) < len(st.get("queue", [])))
            is_reminder_minute = (now_time.hour == reminder_time.hour and now_time.minute == reminder_time.minute)

            # Costruisci la coda SOLO al minuto del reminder e una volta al giorno
            if is_reminder_minute and (not st or st.get("date") != now.date()):
                queue = _build_operator_targets_for_tomorrow(session, require_phone=True)
                if not queue:
                    _op_dbg(tenant_id, "coda vuota per domani")
                    return
                _OP_STATE_MAP[tenant_id] = {
                    "date": now.date(),
                    "queue": queue,
                    "idx": 0,
                    "last_sent_minute": None
                }
                st = _OP_STATE_MAP[tenant_id]
                _op_dbg(tenant_id, f"coda operatori costruita: {len(queue)} target")

            if not (is_reminder_minute or has_active_queue):
                _op_dbg(tenant_id, f"skip: no reminder minute and no active queue. ora={now_time.strftime('%H:%M')}")
                return

            if not st or st.get("idx", 0) >= len(st.get("queue", [])):
                _op_dbg(tenant_id, "nessun messaggio da inviare")
                _OP_STATE_MAP.pop(tenant_id, None)
                return

            current_slot = now.replace(second=0, microsecond=0)
            last_slot = st.get("last_sent_minute")
            can_send = (last_slot is None) or (current_slot > last_slot)

            _op_dbg(tenant_id, f"tick: idx={st['idx']}/{len(st['queue'])}, last_slot={last_slot}, current_slot={current_slot}, can_send={can_send}")

            if can_send:
                item = st["queue"][st["idx"]]
                st["idx"] += 1

                try:
                    text_to_send = _render_operator_msg(msg_text, item)
                except Exception as e:
                    _op_dbg(tenant_id, f"render error operator_id={item.get('operator_id')}: {repr(e)}")
                    text_to_send = msg_text or ""

                creds = _get_wbiztool_creds(tenant_id)
                ok = False
                if creds:
                    try:
                        ok = _send_wbiztool_message(creds, item["phone"], text_to_send)
                    except Exception as e:
                        _op_dbg(tenant_id, f"send error: {repr(e)}")
                        ok = False
                else:
                    _op_dbg(tenant_id, "credenziali mancanti")

                _op_dbg(tenant_id, f"inviato={ok} operator_id={item['operator_id']} -> {item['phone']}")
                st["last_sent_minute"] = current_slot

            if st.get("idx", 0) >= len(st.get("queue", [])):
                _op_dbg(tenant_id, "tutti i messaggi operatori inviati: reset")
                _OP_STATE_MAP.pop(tenant_id, None)
            else:
                _OP_STATE_MAP[tenant_id] = st

            session.commit()
        except Exception as e:
            session.rollback()
            print(f"[WA-OP][{tenant_id}] error: {repr(e)}")
            raise
        finally:
            try:
                session.close()
            finally:
                try:
                    SessionFactory.remove()
                except Exception:
                    pass

@booking_bp.route('/operator-notifications/tick', methods=['POST'])
def operator_notifications_tick(tenant_id):
    """
    Endpoint da richiamare ogni minuto (Logic App/Function/WebJob).
    Costruisce la coda degli operatori (domani) al minuto configurato e invia 1 messaggio/minuto.
    """
    try:
        process_operator_tick(current_app, tenant_id)
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@booking_bp.route('/operator-notifications/trigger', methods=['POST'])
def operator_notifications_trigger(tenant_id):
    """
    Invio immediato (forzato) di tutti i messaggi operatori per domani.
    Non rispetta il rate di 1/minuto. Utile per test.
    """
    try:
        SessionFactory = current_app.config['DB_SESSIONS'][tenant_id]
        session = SessionFactory()
        biz = session.query(BusinessInfo).first()
        if not biz:
            return jsonify({"success": False, "error": "BusinessInfo assente"}), 400

        tpl = getattr(biz, 'operator_whatsapp_message_template', None) or \
            "Ciao {{operatore}},\n\nDomani {{data}} il tuo turno sarà: {{ora_inizio}} - {{ora_fine}}\n\n{{sezione_pausa}}\n\nIl primo impegno della giornata sarà alle {{ora_primo_app}} e sarà {{primo_app}}\n\nBuon lavoro :)"
        queue = _build_operator_targets_for_tomorrow(session, require_phone=True)
        creds = _get_wbiztool_creds(tenant_id)

        sent = 0
        results = []
        for item in queue:
            try:
                text = _render_operator_msg(tpl, item)
            except Exception:
                text = tpl or ""
            ok = False
            if creds:
                try:
                    ok = _send_wbiztool_message(creds, item["phone"], text)
                except Exception as e:
                    ok = False
                    results.append({"operator_id": item["operator_id"], "ok": False, "error": str(e)})
                    continue
            else:
                results.append({"operator_id": item["operator_id"], "ok": False, "error": "credenziali mancanti"})
                continue
            if ok:
                sent += 1
            results.append({"operator_id": item["operator_id"], "ok": ok})

        try:
            session.close()
        finally:
            try:
                SessionFactory.remove()
            except Exception:
                pass

        return jsonify({"success": True, "sent": sent, "total": len(queue), "results": results}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500