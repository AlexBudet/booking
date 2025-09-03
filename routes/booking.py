# filepath: /Users/alessio.budettagmail.com/Documents/SunBooking/appl/routes/booking.py
import string
import json
from collections import Counter
from flask import Blueprint, g, request, jsonify, render_template, render_template_string, session
from flask_wtf.csrf import generate_csrf
from appl.models import Appointment, AppointmentSource, Service, Operator, OperatorShift, Client, BusinessInfo, Subcategory, db
from datetime import date, datetime, timezone, timedelta, time
from sqlalchemy import and_, cast, DateTime, or_
from pytz import timezone as pytz_timezone
import os
import re
import random
import smtplib
from email.message import EmailMessage
import uuid
from markupsafe import escape
import threading
from azure.communication.email import EmailClient

def invia_email_azure(to_email, subject, html_content, from_email=None):
    connection_string = os.environ.get('AZURE_EMAIL_CONNECTION_STRING')
    client = EmailClient.from_connection_string(connection_string)
    message = {
        "senderAddress": from_email or "donotreply@8a979827-fa9b-4b2d-b7f8-52cc9565a0d9.azurecomm.net",
        "recipients": {"to": [{"address": to_email}]},
        "content": {"subject": subject, "html": html_content}
    }
    poller = client.begin_send(message)
    result = poller.result()
    return True

def invia_email_async(to_email, subject, html_content, from_email=None):
    try:
        connection_string = os.environ.get('AZURE_EMAIL_CONNECTION_STRING')
        if not connection_string:
            print("ERROR: AZURE_EMAIL_CONNECTION_STRING not set")
            return False
        client = EmailClient.from_connection_string(connection_string)
        message = {
            "senderAddress": from_email or "donotreply@8a979827-fa9b-4b2d-b7f8-52cc9565a0d9.azurecomm.net",
            "recipients": {"to": [{"address": to_email}]},
            "content": {"subject": subject, "html": html_content}
        }
        poller = client.begin_send(message)
        result = poller.result()
        print(f"Email sent successfully: {result.message_id}")
        return True
    except Exception as e:
        print(f"ERROR sending email: {repr(e)}")
        return False

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
        ~Service.servizio_nome.ilike('dummy')
    ).order_by(Service.servizio_nome).all()
    operatori = g.db_session.query(Operator).order_by(Operator.user_nome).all()
    business_info = g.db_session.query(BusinessInfo).first()

    servizi_json = [{
        'id': s.id, 
        'servizio_nome': s.servizio_nome, 
        'servizio_durata': s.servizio_durata,
        'servizio_prezzo': str(s.servizio_prezzo),
        'operator_ids': [op.id for op in s.operators],
        'sottocategoria': s.servizio_sottocategoria.nome if s.servizio_sottocategoria else None
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
            "sottocategoria": s.servizio_sottocategoria.nome if s.servizio_sottocategoria else None
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

    servizi = g.db_session.query(Service).filter(Service.id.in_(servizi_ids)).all()
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
    if operatore_id:
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

    appuntamenti = g.db_session.query(Appointment).filter(
        Appointment.start_time >= datetime.combine(data, time.min),
        Appointment.start_time < datetime.combine(data + timedelta(days=1), time.min)
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

    # Prova solo slot dove un singolo operatore può coprire TUTTI i servizi richiesti in sequenza
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

                    # L'operatore deve essere abilitato e disponibile per ogni servizio della catena
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

    orari = sorted(list(set(orari)))

    # FILTRO: escludi orari già passati se la data è oggi
    now = datetime.now(pytz_timezone('Europe/Rome')).replace(second=0, microsecond=0)
    if data == now.date():
        orari = [
            o for o in orari
            if datetime.combine(data, datetime.strptime(o, "%H:%M").time()) >= now.replace(tzinfo=None)
        ]
        slot_operatori = {o: slot_operatori[o] for o in orari}

    # FILTRO: escludi completamente le date precedenti a oggi
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

    # Verifica codice conferma
    if 'codice_conferma' in session:
        codice_sessione = session.get('codice_conferma')
        email_sessione = session.get('email_conferma')
        if not codice_conferma or codice_conferma != codice_sessione or email != email_sessione:
            return jsonify({"error": "Codice di conferma errato! Riprova"}), 400
    else:
        return jsonify({"error": "Codice di conferma non richiesto"}), 400

    # Validazione campi base
    if not all([nome, telefono, data_str, ora]) or not servizi or not isinstance(servizi, list):
        return jsonify({"error": "Tutti i campi sono obbligatori"}), 400

    # --- PATCH: Usa la stessa logica di orari_disponibili per validare slot e operatori ---
    servizi_ids = [int(s.get("servizio_id")) for s in servizi]
    servizi_objs = g.db_session.query(Service).filter(Service.id.in_(servizi_ids)).all()
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
        Appointment.start_time < datetime.combine(data + timedelta(days=1), time.min)
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

    # --- LOGICA IDENTICA A orari_disponibili ---
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
                intervalli.append((curr_start, curr_end))

    # Verifica che operatori_assegnati sia coerente
    if not isinstance(operatori_assegnati, list) or len(operatori_assegnati) != len(servizi):
        return jsonify({"success": False, "errori": ["Operatori assegnati mancanti o non coerenti. Ricarica la pagina e riprova."]}), 400

    for idx, servizio_item in enumerate(servizi):
        operatore_id = servizio_item.get("operatore_id")
        if operatore_id:
            if operatori_assegnati[idx] != int(operatore_id):
                return jsonify({"success": False, "errori": ["La sequenza di operatori richiesta non è più disponibile per questo slot. Ricarica la pagina e riprova."]}), 400

    # Ora salva la prenotazione
    risultati = []
    slot_corrente = datetime.strptime(f"{data_str} {ora}", "%Y-%m-%d %H:%M")
    for idx, servizio_item in enumerate(servizi):
        servizio_id = int(servizio_item.get("servizio_id"))
        durata_servizio = servizi_map[servizio_id].servizio_durata or 30
        durata_td = timedelta(minutes=durata_servizio)
        inizio = slot_corrente
        fine = slot_corrente + durata_td
        operatore_id = operatori_assegnati[idx]
        servizio = servizi_map.get(servizio_id)
        note = f"PRENOTATO DA BOOKING ONLINE - Nome: {escape(nome)}, Cognome: {escape(cognome)}, Telefono: {escape(telefono)}, Email: {escape(email)}"
        operatore = g.db_session.get(Operator, operatore_id)
        operatore_nome = f"{escape(operatore.user_nome)} {escape(operatore.user_cognome)}" if operatore else ""
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
            return jsonify({"success": False, "errori": ["Errore database: " + str(e)]}), 500
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
                "data": r['data'],
                "ora": r['ora'],
                "operatore_nome": r.get('operatore_nome', ''),
                "servizio_nome": r['servizio_nome'],
                "durata": durata_i,
                "prezzo": f"{prezzo_i:.2f}"
            })

        # Template sicuro: Jinja escaperà le variabili automaticamente
        template = """
        <p>Ciao {{ nome }},</p>
        <p>La tua prenotazione è stata confermata!</p>
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
        <p>Grazie per aver scelto Sun Express 3!</p>
        """

        riepilogo = render_template_string(
            template,
            nome=nome,
            appuntamenti=appuntamenti_data,
            totale_durata=totale_durata,
            totale_prezzo=f"{totale_prezzo:.2f}"
        )

        # Usa l'email del business come mittente se presente
        business_info = g.db_session.query(BusinessInfo).first()
        from_addr = business_info.email if business_info and business_info.email else os.environ.get('SMTP_USER', 'noreply@sunexpressbeauty.com')
        invia_email_async(
            to_email=email,
            subject=f'{company_name} - Conferma appuntamento!',
            html_content=riepilogo,
            from_email=None
        )

        # Invio email all'admin (stesso approccio sicuro)
        admin_email = business_info.email if business_info and business_info.email else None
        admin_riepilogo = render_template_string(
            """
            <h3>nuova prenotazione: {{ nome }} {{ cognome }}</h3>
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
        if admin_email:
            admin_from = business_info.email if business_info and business_info.email else os.environ.get('SMTP_USER', 'noreply@sunexpressbeauty.com')
            invia_email_async(
                to_email=admin_email,
                subject=f'Nuova prenotazione - {escape(nome)}',
                html_content=admin_riepilogo,
                from_email=None
            )

    return jsonify({
        "success": len(risultati) > 0,
        "prenotazioni": risultati,
        "errori": [],
        "popup_warning": popup_warning
    })

@booking_bp.route('/invia-codice', methods=['POST'])
def invia_codice(tenant_id):
    business_info = g.db_session.query(BusinessInfo).first()
    company_name = business_info.business_name if business_info and business_info.business_name else "SunBooking"
    last_sent = session.get('last_code_sent_at', 0)
    if datetime.now().timestamp() - last_sent < 60:
        return jsonify({"success": False, "error": "Puoi inviare un nuovo codice tra un minuto."}), 429

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

    codice = ''.join(random.choices(string.digits, k=6))
    session['codice_conferma'] = codice
    session['email_conferma'] = email
    session['last_code_sent_at'] = datetime.now().timestamp()

    html_content = f"""
        <p>Ciao {escape(nome)},</p>
        <p>Il tuo codice di conferma one-time-code è: <b>{escape(codice)}</b></p>
        <p>Inseriscilo nella pagina di prenotazione per completare la conferma.</p>
        <p>Grazie!</p>
    """

    # Queue email in background to avoid blocking gunicorn worker
    try:
        invia_email_async(
            to_email=email,
            subject=f'{company_name} - Il tuo codice di conferma',
            html_content=html_content,
            from_email=None
        )
        return jsonify({"success": True})
    except Exception as e:
        print("ERROR queueing email:", repr(e))
        return jsonify({"success": False, "error": "Errore durante l'invio dell'email."}), 500