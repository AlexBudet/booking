# filepath: /Users/alessio.budettagmail.com/Documents/SunBooking/appl/routes/booking.py
import random
import string
import json
from flask import Blueprint, request, jsonify, render_template, session
from appl.models import Appointment, AppointmentSource, Service, Operator, OperatorShift, Client, BusinessInfo, db
from datetime import datetime, timezone, timedelta, time
from sqlalchemy import and_, cast, DateTime, or_
from pytz import timezone as pytz_timezone
import sendgrid
import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv
import uuid

# Carica le variabili d'ambiente dal file .env
load_dotenv()

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

    servizi_objs = Service.query.filter(Service.id.in_(servizi_ids)).all()
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
def booking_page():
        # Mostra solo servizi visibili online
    servizi = Service.query.filter_by(is_visible_online=True, is_deleted=False).all()
    operatori = Operator.query.filter_by(is_visible=True, is_deleted=False).all()
    business_info = BusinessInfo.query.first()

    servizi_json = [
        {
            "id": s.id,
            "servizio_nome": s.servizio_nome,
            "servizio_durata": s.servizio_durata,
            "servizio_prezzo": s.servizio_prezzo,
            # Ricostruisci la lista degli operatori associati
            "operator_ids": [op.id for op in s.operators]
        }
        for s in servizi
    ]

    operatori_json = [
        {
            "id": op.id,
            "nome": op.user_nome,
            "cognome": op.user_cognome
        }
        for op in operatori
    ]

    return render_template(
        "booking_public.html",
        servizi=servizi,
        operatori=operatori,
        servizi_json=servizi_json,
        operatori_json=operatori_json,
        business_info=business_info
    )

@booking_bp.route('/orari', methods=['GET'])
def orari_disponibili():
    data_str = request.args.get('data')  # formato: YYYY-MM-DD
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

    servizi = Service.query.filter(Service.id.in_(servizi_ids)).all()
    if not servizi:
        return jsonify({"error": "Servizi non trovati"}), 404
    
    servizi_operatori = {s.id: [op.id for op in s.operators] for s in servizi}

    data = datetime.strptime(data_str, "%Y-%m-%d").date()
    business_info = BusinessInfo.query.first()
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
    operatori_disponibili = Operator.query.filter_by(is_deleted=False, is_visible=True).all()
    turni_disponibili = OperatorShift.query.filter(
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

    appuntamenti = Appointment.query.filter(
        Appointment.start_time >= datetime.combine(data, time.min),
        Appointment.start_time < datetime.combine(data + timedelta(days=1), time.min)
    ).all()
    blocchi_off = Appointment.query.filter(
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
    for op in operatori_disponibili:
        for start, end in intervalli:
            slot = datetime.combine(data, start)
            fine = datetime.combine(data, end)
            while slot + durata <= fine:
                slot_corrente = slot
                operatori_catena = []
                ok = True
                for servizio_item in servizi_items:
                    servizio_id = int(servizio_item.get("servizio_id"))
                    durata_servizio = next((s.servizio_durata or 30 for s in servizi if s.id == servizio_id), 30)
                    durata_td = timedelta(minutes=durata_servizio)
                    inizio = slot_corrente
                    fine_servizio = slot_corrente + durata_td

                    # Cerca un operatore disponibile per questo servizio
                    trovato = False
                    for op in operatori_disponibili:
                        risultato, motivo = operatore_disponibile(op.id, inizio, fine_servizio)
                        if risultato and op.id in [op_id for op_id in servizi_operatori[servizio_id]]:
                            operatori_catena.append(op.id)
                            trovato = True
                            break
                    if not trovato:
                        ok = False
                        break
                    slot_corrente = fine_servizio

                if ok:
                    orari.append(slot.strftime("%H:%M"))
                    slot_operatori[slot.strftime("%H:%M")] = operatori_catena
                slot += slot_step

    orari = sorted(list(set(orari)))
    return jsonify({
        "orari_disponibili": orari,
        "operatori_assegnati": slot_operatori,
        "debug": debug_info
    })

@booking_bp.route('/prenota', methods=['POST'])
def prenota():
    data = request.get_json()
    nome = data.get('nome')
    cognome = data.get('cognome')
    telefono = data.get('telefono')
    email = data.get('email')
    data_str = data.get('data')
    ora = data.get('ora')
    servizi = data.get('servizi', [])
    codice_conferma = data.get('codice_conferma')
    dummy_client = Client.get_dummy_booking()
    booking_session_id = str(uuid.uuid4())

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
    servizi_objs = Service.query.filter(Service.id.in_(servizi_ids)).all()
    servizi_map = {s.id: s for s in servizi_objs}
    servizi_operatori = {s.id: [op.id for op in s.operators] for s in servizi_objs}

    data = datetime.strptime(data_str, "%Y-%m-%d").date()
    business_info = BusinessInfo.query.first()
    apertura = business_info.active_opening_time
    chiusura = business_info.active_closing_time

    operatori_disponibili = Operator.query.filter_by(is_deleted=False, is_visible=True).all()
    turni_disponibili = OperatorShift.query.filter(
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

    appuntamenti = Appointment.query.filter(
        Appointment.start_time >= datetime.combine(data, time.min),
        Appointment.start_time < datetime.combine(data + timedelta(days=1), time.min)
    ).all()
    blocchi_off = Appointment.query.filter(
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

    orari = []
    slot_operatori = {}
    for start, end in intervalli:
        slot = datetime.combine(data, start)
        fine = datetime.combine(data, end)
        while slot + durata <= fine:
            slot_corrente = slot
            operatori_catena = []
            ok = True
            for idx, servizio_item in enumerate(servizi):
                servizio_id = int(servizio_item.get("servizio_id"))
                durata_servizio = servizi_map[servizio_id].servizio_durata or 30
                durata_td = timedelta(minutes=durata_servizio)
                inizio = slot_corrente
                fine_servizio = slot_corrente + durata_td

                operatore_scelto = servizio_item.get("operatore_id")
                if operatore_scelto:
                    possibili_operatori = [op for op in operatori_disponibili if op.id == int(operatore_scelto)]
                else:
                    preferiti = [op for op in operatori_disponibili if op.id in [
                        int(s.get("operatore_id")) for s in servizi if s.get("operatore_id")
                    ]]
                    altri = [op for op in operatori_disponibili if op not in preferiti]
                    possibili_operatori = preferiti + altri

                trovato = False
                for op in possibili_operatori:
                    turni = turni_per_operatore.get(op.id, [])
                    if not any(start <= inizio.time() and fine_servizio.time() <= end for start, end in turni):
                        continue
                    busy = False
                    for app in appuntamenti:
                        app_start = app.start_time.replace(tzinfo=None)
                        app_end = app_start + timedelta(minutes=app._duration)
                        if app.operator_id is None and app.note and "OFF" in app.note:
                            if app_start < fine_servizio and app_end > inizio:
                                busy = True
                                break
                        if str(app.operator_id) == str(op.id):
                            if app_start < fine_servizio and app_end > inizio:
                                busy = True
                                break
                    if busy:
                        continue
                    if op.id in servizi_operatori[servizio_id]:
                        operatori_catena.append(op.id)
                        trovato = True
                        break
                if not trovato:
                    ok = False
                    break
                slot_corrente = fine_servizio
            if ok:
                orari.append(slot.strftime("%H:%M"))
                slot_operatori[slot.strftime("%H:%M")] = operatori_catena
            slot += slot_step

    # --- FINE LOGICA IDENTICA ---

    # Controlla che lo slot richiesto sia tra quelli disponibili
    if ora not in orari:
        return jsonify({"success": False, "errori": ["Lo slot richiesto non è più disponibile. Ricarica la pagina e riprova."]}), 400

    # Controlla che i servizi con operatore scelto siano assegnati a quell'operatore
    slot_ops = slot_operatori[ora]
    for idx, servizio_item in enumerate(servizi):
        operatore_id = servizio_item.get("operatore_id")
        if operatore_id:
            if slot_ops[idx] != int(operatore_id):
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
        operatore_id = slot_operatori[ora][idx]
        servizio = servizi_map.get(servizio_id)
        note = f"PRENOTATO DA BOOKING ONLINE - Nome: {nome}, Cognome: {cognome}, Telefono: {telefono}, Email: {email}"
        operatore = Operator.query.get(operatore_id)
        operatore_nome = f"{operatore.user_nome} {operatore.user_cognome}" if operatore else ""
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
        db.session.add(nuovo)
        db.session.commit()
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
        SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')
        try:
            sg = SendGridAPIClient(SENDGRID_API_KEY)
            appuntamenti_html = ""
            totale_durata = 0
            totale_prezzo = 0
            for r in risultati:
                servizio = Service.query.get(r['servizio_id'])
                durata = int(servizio.servizio_durata or 30)
                prezzo = float(getattr(servizio, 'servizio_prezzo', 0) or 0)
                totale_durata += durata
                totale_prezzo += prezzo
                appuntamenti_html += f"""
                    <li>
                        <b>Data:</b> {r['data']}<br>
                        <b>Ora:</b> {r['ora']}<br>
                        {f"<b>Operatore:</b> {r['operatore_nome']}<br>" if r['operatore_nome'] else ""}
                        <b>Servizio:</b> {r['servizio_nome']}<br>
                        <small>
                            Durata: {durata} min<br>
                            Prezzo: {prezzo:.2f} €
                        </small>
                    </li>
                """
            riepilogo = f"""
                <p>Ciao {nome},</p>
                <p>La tua prenotazione è stata confermata!</p>
                <ul>
                    {appuntamenti_html}
                </ul>
                <div style="padding:12px; background:#f2f2f2; margin:20px 0; border-radius:8px;">
                <b>Totale durata:</b> {totale_durata} min &nbsp; | &nbsp; <b>Totale costo:</b> €{totale_prezzo:.2f}
                </div>
                <p>Grazie per aver scelto Sun Express 3!</p>
            """
            message = Mail(
                from_email='noreply@sunexpressbeauty.com',
                to_emails=email,
                subject='SunBooking - Conferma appuntamento',
                html_content=riepilogo
            )
            sg.send(message)

            # Invio email all'admin
            business_info = BusinessInfo.query.first()
            admin_email = business_info.email if business_info and business_info.email else None
            admin_riepilogo = f"""
                <h3>nuova prenotazione: {nome} {cognome}</h3>
                <div style="font-size:1.3em;">
                <ul>
                    {appuntamenti_html}
                </ul>
                </div>
                <div style="padding:12px; background:#f2f2f2; margin:20px 0; border-radius:8px; font-size:1.3em;">
                <b>Totale durata:</b> {totale_durata} min &nbsp; | &nbsp; <b>Totale costo:</b> €{totale_prezzo:.2f}
                </div>
            """
            admin_message = Mail(
                from_email='noreply@sunexpressbeauty.com',
                to_emails=admin_email,
                subject=f'Nuova prenotazione - {nome}',
                html_content=admin_riepilogo
            )
            sg.send(admin_message)

        except Exception as e:
            print("ERRORE INVIO EMAIL DI CONFERMA:", e)

    return jsonify({
        "success": len(risultati) > 0,
        "prenotazioni": risultati,
        "errori": []
    })

@booking_bp.route('/invia-codice', methods=['POST'])
def invia_codice():
    SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')

    print(">>> invia_codice chiamato")
    data = request.get_json()
    print("Dati ricevuti:", data)

    # Controllo presenza dati e campi obbligatori
    if not data:
        print("ERRORE: Nessun JSON ricevuto")
        return jsonify({"success": False, "error": "Nessun JSON ricevuto"}), 400

    email = data.get('email', '').strip()
    nome = data.get('nome', '').strip()
    cognome = data.get('cognome', '').strip()
    telefono = data.get('telefono', '').strip()

    print("Email:", email)
    print("Nome:", nome)
    print("Cognome:", cognome)
    print("Telefono:", telefono)

    if not all([email, nome, cognome, telefono]):
        print("ERRORE: Campi mancanti")
        return jsonify({"success": False, "error": "Tutti i campi sono obbligatori"}), 400

    # Controllo email semplice
    if '@' not in email or '.' not in email.split('@')[-1]:
        print("ERRORE: Email non valida")
        return jsonify({"success": False, "error": "Indirizzo email non valido"}), 400

    # Genera codice di conferma
    codice = ''.join(random.choices(string.digits, k=6))
    session['codice_conferma'] = codice
    session['email_conferma'] = email

    # Prepara il messaggio
    message = Mail(
        from_email='noreply@sunexpressbeauty.com',
        to_emails=email,
        subject='SunBooking - Il tuo codice di conferma',
        html_content=f"""
            <p>Ciao {nome},</p>
            <p>Il tuo codice di conferma è: <b>{codice}</b></p>
            <p>Inseriscilo nella pagina di prenotazione per completare la conferma.</p><br><br>
            <p>Ignora questa email se non hai effettuato tu la prenotazione.</p><br><br>
            <p>Grazie!</p>
            <p>Il team di SunBooking</p>
        """
    )

    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        print(response.status_code)
        return jsonify({"success": True})
    except Exception as e:
        print("ERRORE INVIO EMAIL:", e)
        return jsonify({"success": False, "error": "Errore invio email"}), 500