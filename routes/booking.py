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

def scegli_operatori_automatici(servizi_ids, data_str, ora_str, operatori_possibili, turni_per_operatore, all_apps):
    """
    Restituisce una lista di operatori assegnati (uno per ogni servizio).
    1. Prova a mettere tutti i servizi con lo stesso operatore.
    2. Se non è possibile, restituisce solo None (NON distribuisce più tra operatori per debug).
    """

    servizi_objs = Service.query.filter(Service.id.in_(servizi_ids)).all()
    servizi_map = {s.id: s for s in servizi_objs}
    servizi_operatori = {s.id: [op.id for op in s.operators] for s in servizi_objs}
    servizi_durate = [servizi_map[sid].servizio_durata or 30 for sid in servizi_ids]

    operatori_possibili = [
        op for op in operatori_possibili
        if all(op.id in servizi_operatori[sid] for sid in servizi_ids)
    ]

    data_giorno = datetime.strptime(data_str, "%Y-%m-%d").date()
    start_time = datetime.strptime(f"{data_str} {ora_str}", "%Y-%m-%d %H:%M")

    def is_calendar_closed(op_id, inizio, fine):
        shifts = turni_per_operatore.get(op_id, [])
        if not any(s <= inizio.time() and fine.time() <= e for s, e in shifts):
            return True
        for a in all_apps:
            a_start = a.start_time.replace(tzinfo=None)
            a_end = a_start + timedelta(minutes=a._duration)
            if a.operator_id is None and a.note and "OFF" in a.note:
                if a_start < fine and a_end > inizio:
                    return True
            if a.operator_id == op_id:
                if a_start < fine and a_end > inizio:
                    return True
        return False

    # 1. Prova tutti con lo stesso operatore
    for op in operatori_possibili:
        slot_corrente = start_time
        ok = True
        for durata in servizi_durate:
            inizio = slot_corrente
            fine = slot_corrente + timedelta(minutes=durata)
            if is_calendar_closed(op.id, inizio, fine):
                ok = False
                break
            slot_corrente = fine
        if ok:
            return [op.id] * len(servizi_ids)

    # 2. Disabilitato per debug: NON assegnare a cascata
    # slot_corrente = start_time
    # operatori_assegnati = []
    # i = 0
    # while i < len(servizi_ids):
    #     found = False
    #     for op in operatori_possibili:
    #         # Prova a incastrare più servizi possibile su questo operatore da posizione i
    #         slot = slot_corrente
    #         j = i
    #         while j < len(servizi_ids):
    #             durata = servizi_durate[j]
    #             inizio = slot
    #             fine = slot + timedelta(minutes=durata)
    #             if is_calendar_closed(op.id, inizio, fine):
    #                 break
    #             slot = fine
    #             j += 1
    #         # Se almeno uno lo mette, assegnali tutti all'operatore e continua
    #         if j > i:
    #             operatori_assegnati += [op.id] * (j - i)
    #             slot_corrente = slot
    #             i = j
    #             found = True
    #             break
    #     if not found:
    #         # Nessun operatore libero per il servizio i
    #         operatori_assegnati.append(None)
    #         slot_corrente += timedelta(minutes=servizi_durate[i])
    #         i += 1
    # return operatori_assegnati

    # Return None per tutti se non fattibile tutta la catena su uno stesso operatore
    return [None] * len(servizi_ids)

booking_bp = Blueprint('booking', __name__)

@booking_bp.route('/')
@booking_bp.route('/booking')
def booking_page():
        # Mostra solo servizi visibili online
    servizi = Service.query.filter_by(is_visible_online=True, is_deleted=False).all()
    operatori = Operator.query.filter_by(is_visible=True, is_deleted=False).all()

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
        operatori_json=operatori_json
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
                ok = True
                for servizio_item in servizi_items:
                    servizio_id = int(servizio_item.get("servizio_id"))
                    durata_servizio = next((s.servizio_durata or 30 for s in servizi if s.id == servizio_id), 30)
                    durata_td = timedelta(minutes=durata_servizio)
                    inizio = slot_corrente
                    fine_servizio = slot_corrente + durata_td
                    risultato, motivo = operatore_disponibile(op.id, inizio, fine_servizio)
                    if not risultato:
                        ok = False
                        break
                    slot_corrente = fine_servizio
                if ok:
                    orari.append(slot.strftime("%H:%M"))
                    slot_operatori[slot.strftime("%H:%M")] = [op.id] * len(servizi_items)
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
    booking_session_id = str(uuid.uuid4()) # Genera un ID di sessione unico per la prenotazione

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

    operatori_assegnati = data.get("operatori_assegnati", [])
    if len(operatori_assegnati) != len(servizi):
        # Assegnazione automatica se non specificata dal frontend
        operatori_assegnati = scegli_operatori_automatici(
            servizi_ids=[int(s.get("servizio_id")) for s in servizi],
            data_str=data_str,
            ora_str=ora,
            operatori_possibili=operatori_disponibili,
            turni_per_operatore=turni_per_operatore,
            all_apps=all_apps
        )
        if len(operatori_assegnati) != len(servizi):
            return jsonify({
                "success": False,
                "prenotazioni": [],
                "errori": ["Non è stato possibile assegnare operatori a tutti i servizi richiesti."]
            })

    risultati = []
    errori = []

    start_time = datetime.strptime(f"{data_str} {ora}", "%Y-%m-%d %H:%M")
    data_date = start_time.date()
    business_info = BusinessInfo.query.first()
    apertura = business_info.active_opening_time
    chiusura = business_info.active_closing_time

    operatori_disponibili = Operator.query.filter_by(is_deleted=False, is_visible=True).all()
    turni_raw = OperatorShift.query.filter(
        OperatorShift.operator_id.in_([o.id for o in operatori_disponibili]),
        OperatorShift.shift_date == data_date
    ).all()
    turni_per_operatore = {}
    for t in turni_raw:
        start = max(t.shift_start_time or apertura, apertura)
        end   = min(t.shift_end_time   or chiusura, chiusura)
        if start < end:
            turni_per_operatore.setdefault(t.operator_id, []).append((start, end))
    for op in operatori_disponibili:
        if op.id not in turni_per_operatore:
            turni_per_operatore[op.id] = [(apertura, chiusura)]

    day_start = datetime.combine(data_date, time.min)
    day_end   = datetime.combine(data_date + timedelta(days=1), time.min)
    all_apps  = Appointment.query.filter(
        Appointment.start_time >= day_start,
        Appointment.start_time <  day_end
    ).all()

    def to_naive(dt):
        """Converte un datetime aware in naive, oppure lo restituisce se già naive."""
        if dt is not None and getattr(dt, "tzinfo", None) is not None:
            return dt.replace(tzinfo=None)
        return dt

    def operatore_disponibile(op_id, inizio, fine):
        # 1. Verifica turno
        shifts = turni_per_operatore.get(op_id, [])
        if not any(s <= inizio.time() and fine.time() <= e for s, e in shifts):
            return False
        # 2. Verifica sovrapposizioni appuntamenti e blocchi OFF
        for a in all_apps:
            a_start = to_naive(a.start_time)
            a_end   = to_naive(a_start + timedelta(minutes=a._duration))
            # OFF globali
            if a.operator_id is None and a.note and "OFF" in a.note:
                if a_start < to_naive(fine) and a_end > to_naive(inizio):
                    return False
            if a.operator_id == op_id:
                if a_start < to_naive(fine) and a_end > to_naive(inizio):
                    return False
        return True

    risultati = []
    slot_corrente = start_time
    servizi_ids = [int(s.get("servizio_id")) for s in servizi]
    servizi_objs = Service.query.filter(Service.id.in_(servizi_ids)).all()
    servizi_map = {s.id: s for s in servizi_objs}
    servizi_operatori = {s.id: [op.id for op in s.operators] for s in servizi_objs}

    def is_calendar_closed(op_id, inizio, fine, turni_per_operatore, all_apps):
        # Converto tutto a naive
        inizio = to_naive(inizio)
        fine = to_naive(fine)
        shifts = turni_per_operatore.get(op_id, [])
        if not any(s <= inizio.time() and fine.time() <= e for s, e in shifts):
            return True
        for a in all_apps:
            a_start = to_naive(a.start_time)
            a_end   = to_naive(a.start_time + timedelta(minutes=a._duration))
            if a.operator_id is None and a.note and "OFF" in a.note:
                if a_start < fine and a_end > inizio:
                    return True
            if a.operator_id == op_id and a.note and "OFF" in a.note:
                if a_start < fine and a_end > inizio:
                    return True
        return False

    for idx, servizio_item in enumerate(servizi):
        servizio_id = int(servizio_item.get("servizio_id"))
        operatore_id = int(operatori_assegnati[idx])
        servizio = servizi_map.get(servizio_id)
        if not servizio:
            errori.append(f"Servizio {servizio_id} non trovato")
            break
        if operatore_id not in servizi_operatori.get(servizio_id, []):
            errori.append(f"L'operatore selezionato non è abilitato per il servizio {servizio.servizio_nome}")
            break
        durata_servizio = servizio.servizio_durata or 30
        durata_td = timedelta(minutes=durata_servizio)
        inizio = slot_corrente
        fine = slot_corrente + durata_td
        
        if is_calendar_closed(operatore_id, inizio, fine, turni_per_operatore, all_apps):
            errori.append(f"Non puoi prenotare su una cella calendar-closed per il servizio {servizio.servizio_nome} alle {inizio.strftime('%H:%M')}")
            break

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
        SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY') or 'INSERISCI_LA_TUA_KEY'
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
        except Exception as e:
            print("ERRORE INVIO EMAIL DI CONFERMA:", e)

    # Dopo la prenotazione, elimina il codice dalla sessione per sicurezza
    session.pop('codice_conferma', None)
    session.pop('email_conferma', None)

    return jsonify({
        "success": len(risultati) > 0,
        "prenotazioni": risultati,
        "errori": errori
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