#appl/models.py
import json
from enum import Enum as PyEnum
from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Time, Text
from sqlalchemy.sql import func
from sqlalchemy import JSON, DateTime
from sqlalchemy.dialects.postgresql import ENUM
from appl import db
from datetime import timedelta, datetime
from werkzeug.security import generate_password_hash

service_operator = db.Table(
    'service_operator',
    db.Column('service_id', db.Integer, db.ForeignKey('servizi.id'), primary_key=True),
    db.Column('operator_id', db.Integer, db.ForeignKey('operatori.id'), primary_key=True)
)

class RuoloUtente(PyEnum):
    owner = "owner"
    admin = "admin"
    user = "user"

class ServiceCategory(PyEnum):
    Solarium = "Solarium"
    Estetica = "Estetica"

class WeekDay(PyEnum):
    Monday = "Lunedì"
    Tuesday = "Martedì"
    Wednesday = "Mercoledì"
    Thursday = "Giovedì"
    Friday = "Venerdì"
    Saturday = "Sabato"
    Sunday = "Domenica"

class AppointmentStatus(PyEnum):
    DEFAULT = 0         # Cliente non ancora arrivato
    IN_ISTITUTO = 1     # Cliente in istituto
    PAGATO = 2          # Pagato
    NON_ARRIVATO = 3    # Non arrivato

class AppointmentSource(PyEnum):
    gestionale = "gestionale"
    web = "web"

class PacchettoTipo(PyEnum):
    Servizi = "servizi"        # Pacchetto classico con sedute
    Prepagata = "prepagata"    # Carta prepagata/Gift Card

class User(db.Model):
    __tablename__ = 'utenti'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)
    ruolo = db.Column(
        ENUM(RuoloUtente, name="ruolo_utente", create_type=True),
        nullable=False,
        default=RuoloUtente.user
    )

class Subcategory(db.Model):
    __tablename__ = 'sottocategorie'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nome = db.Column(db.String(50), nullable=False)
    categoria = db.Column(Enum(ServiceCategory), nullable=False)  # Usa Enum di SQLAlchemy
    is_deleted = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<Subcategory {self.nome}>"

class Operator(db.Model):
    __tablename__ = 'operatori'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_nome = db.Column(db.String(50))
    user_cognome = db.Column(db.String(50))
    user_cellulare = db.Column(db.String, nullable=False, server_default='0')
    user_tipo = db.Column(Enum('estetista', 'macchinario', name='user_tipo_enum'), nullable=False)
    is_deleted = db.Column(db.Boolean, default=False)
    is_visible = db.Column(db.Boolean, default=True)
    order = db.Column(db.Integer, default=0)  # Add the 'order' column
    use_twenty_minutes = db.Column(db.Boolean, default=False)
    notify_turni_via_whatsapp = db.Column(db.Boolean, default=False)

    services = db.relationship(
    'Service',
    secondary=service_operator,
    back_populates='operators'
    )

    def __repr__(self):
        return f"<Operator {self.user_nome} {self.user_cognome}>"
    
class OperatorShift(db.Model):
    __tablename__ = 'operator_shifts'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    operator_id = db.Column(db.Integer, ForeignKey('operatori.id'), nullable=False)
    shift_date = db.Column(db.Date, nullable=False)
    shift_start_time = db.Column(Time, nullable=False)
    shift_end_time = db.Column(Time, nullable=False)

    def __repr__(self):
        return f"<OperatorShift id: {self.id}, operator_id: {self.operator_id}, date: {self.shift_date}, start: {self.shift_start_time}, end: {self.shift_end_time}>"

class Client(db.Model):
    __tablename__ = 'clienti'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    cliente_nome = db.Column(db.String, nullable=False)
    cliente_cognome = db.Column(db.String, nullable=False)
    cliente_cellulare = db.Column(db.String, nullable=False)
    cliente_email = db.Column(db.String, nullable=True)
    cliente_data_nascita = db.Column(db.Date, nullable=True)
    cliente_sesso = db.Column(db.String(1), nullable=False)
    is_deleted = db.Column(db.Boolean, default=False)
    note = db.Column(db.String(1000), nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now())

    def __repr__(self):
        return f"<Cliente {self.cliente_nome} {self.cliente_cognome}>"

    @classmethod
    def get_dummy(cls):
        dummy = cls.query.filter_by(cliente_nome="dummy", cliente_cognome="dummy").first()
        if not dummy:
            dummy = cls(
                cliente_nome="dummy",
                cliente_cognome="dummy",
                cliente_cellulare="0000000000",
                cliente_sesso="-"  # oppure "F" in base alle tue esigenze
            )
            db.session.add(dummy)
            db.session.commit()
        return dummy
    
    @classmethod
    def get_dummy_booking(cls):
        dummy = cls.query.filter_by(cliente_nome="cliente", cliente_cognome="booking").first()
        if not dummy:
            dummy = cls(
                cliente_nome="cliente",
                cliente_cognome="booking",
                cliente_cellulare="0",
                cliente_sesso="-",
                is_deleted=False
            )
            db.session.add(dummy)
            db.session.commit()
        return dummy

class Service(db.Model):
    __tablename__ = 'servizi'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    servizio_nome = db.Column(db.String(30), nullable=False)
    servizio_tag = db.Column(db.String(12), nullable=True)
    servizio_durata = db.Column(db.Integer, nullable=False)
    servizio_prezzo = db.Column(db.Float, nullable=False)
    servizio_categoria = db.Column(Enum(ServiceCategory), nullable=False)
    servizio_sottocategoria_id = db.Column(db.Integer, db.ForeignKey('sottocategorie.id'), nullable=True)
    servizio_sottocategoria = db.relationship('Subcategory', backref='servizi')
    servizio_descrizione = db.Column(db.String(2000), nullable=True)
    servizio_disclaimer = db.Column(db.String(3000), nullable=True)
    is_deleted = db.Column(db.Boolean, default=False)
    is_visible_in_calendar = db.Column(db.Boolean, default=True)
    is_visible_online = db.Column(db.Boolean, default=True)
    # Numero massimo di appuntamenti contemporanei per questo servizio
    # NULL o 0 = nessun limite (comportamento attuale)
    # 1 = solo un appuntamento alla volta (es. unico macchinario)
    # 2+ = fino a N appuntamenti contemporanei
    max_concurrent = db.Column(db.Integer, nullable=True, default=None)
    # Nome della risorsa per messaggi più chiari
    # es. "Macchinario Radiofrequenza", "Lettino Pressoterapia"
    resource_name = db.Column(db.String(100), nullable=True, default=None)

    operators = db.relationship(
    'Operator',
    secondary=service_operator,
    back_populates='services'
    )

    def __repr__(self):
        return f"<Service {self.servizio_nome}>"

    @classmethod
    def get_dummy(cls):
        dummy = cls.query.filter_by(servizio_nome="dummy").first()
        if not dummy:
            dummy = cls(
                servizio_nome="dummy",
                servizio_tag="dummy",
                servizio_durata=0,
                servizio_prezzo=0.0,
                servizio_categoria=ServiceCategory.Estetica  # oppure ServiceCategory.Solarium
            )
            db.session.add(dummy)
            db.session.commit()
        return dummy

class Appointment(db.Model):
    __tablename__ = 'appuntamenti'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    last_edit = db.Column(db.DateTime(timezone=True))
    client_id = db.Column(db.Integer, db.ForeignKey('clienti.id'), nullable=False)
    operator_id = db.Column(db.Integer, db.ForeignKey('operatori.id'), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('servizi.id'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    _duration = db.Column("duration", db.Integer, nullable=False)  # Rinominata in _duration
    colore = db.Column(db.String(7), nullable=True)  # Codice colore HEX (es. #FF5733)
    colore_font = db.Column(db.String(20), nullable=True) # Codice colore più esteso
    note = db.Column(db.String(1000), nullable=True)
    source = db.Column(db.Enum(AppointmentSource, native_enum=False), default=AppointmentSource.gestionale, nullable=False)
    stato = db.Column(db.Enum(AppointmentStatus, native_enum=False), 
                      default=AppointmentStatus.DEFAULT, 
                      nullable=False)
    booking_session_id = db.Column(db.String(64), nullable=True, index=True) 
    is_cancelled_by_client = db.Column(db.Boolean, default=False)
    pacchetto_seduta_id = db.Column(db.Integer, db.ForeignKey('pacchetto_sedute.id'), nullable=True)

    # Relazioni
    client = db.relationship('Client', backref='appointments')
    operator = db.relationship('Operator', backref='appointments')
    service = db.relationship('Service', backref='appointments')
    pacchetto_seduta = db.relationship('PacchettoSeduta', backref='appointment', uselist=False)

    # Proprietà calcolate
    @property
    def duration(self):  # Property pubblica
        return self._duration

    @duration.setter
    def duration(self, new_duration):
        if not isinstance(new_duration, int) or new_duration <= 0:
            raise ValueError("duration deve essere un intero positivo")
        self._duration = new_duration

    @property
    def end_time(self):
        return self.start_time + timedelta(minutes=self._duration)

    @end_time.setter
    def end_time(self, new_end_time):
        if not isinstance(new_end_time, datetime):
            raise ValueError("end_time deve essere un oggetto datetime")
        self._duration = int((new_end_time - self.start_time).total_seconds() // 60)

    def __repr__(self):
        return f"<Appointment {self.id}>"

class BusinessInfo(db.Model):
    __tablename__ = 'business_info'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_name = db.Column(String(100), nullable=False)
    website = db.Column(String(200), nullable=True)
    address = db.Column(String(200), nullable=True)
    cap = db.Column(String(10), nullable=True)
    province = db.Column(String(50), nullable=True)
    city = db.Column(String(100), nullable=True)
    vat_code = db.Column(String(50), nullable=True)         # P.IVA/Codice Fiscale
    pec_code = db.Column(String(100), nullable=True)        # PEC/Codice Univoco
    phone = db.Column(String(30), nullable=True)
    mobile = db.Column(String(30), nullable=True)
    email = db.Column(String(100), nullable=True)
    opening_time = db.Column(db.Time, nullable=False)
    closing_time = db.Column(db.Time, nullable=False)
    active_opening_time = db.Column(db.Time, nullable=False, default=datetime.strptime("08:00","%H:%M").time())
    active_closing_time = db.Column(db.Time, nullable=False, default=datetime.strptime("20:00","%H:%M").time())
    closing_days = db.Column(Text, nullable=True)
    is_deleted = db.Column(Boolean, default=False)
    vat_percentage = db.Column(db.Float, default=22.0)  # IVA di default al 22%
    printer_ip = db.Column(db.String(64), default="192.168.1.155")
    whatsapp_modal_disable = db.Column(db.Boolean, default=False)
    whatsapp_message = db.Column(db.Text, nullable=True)
    whatsapp_message_auto = db.Column(db.Text) 
    whatsapp_message_morning = db.Column(db.Text) 
    whatsapp_morning_reminder_enabled = db.Column(db.Boolean, default=False)
    whatsapp_morning_reminder_time = db.Column(db.Time, default=datetime.strptime("08:00", "%H:%M").time())
    booking_max_durata = db.Column(db.Integer, default=0)
    booking_rule_type_durata = db.Column(db.String(20), default="none")
    booking_rule_message_durata = db.Column(db.String(255), default="none")
    booking_max_prezzo = db.Column(db.Float, default=0)
    booking_rule_type_prezzo = db.Column(db.String(20), default="none")
    booking_rule_message_prezzo = db.Column(db.String(255), default="none")
    operator_whatsapp_notification_enabled = db.Column(db.Boolean, default=False)
    operator_whatsapp_notification_time = db.Column(db.Time, default=datetime.strptime("20:00", "%H:%M").time())
    operator_whatsapp_message_template = db.Column(db.Text, nullable=True)
    whatsapp_template_pacchetti = db.Column(db.Text, nullable=True)
    whatsapp_template_pacchetti_disclaimer = db.Column(db.Text, nullable=True)
    whatsapp_template_prepagate = db.Column(db.Text, nullable=True)
    pacchetti_giorni_abbandono = db.Column(db.Integer, nullable=True, default=90)
    # Campi Marketing
    marketing_message_template = db.Column(db.Text, nullable=True)
    new_client_welcome_enabled = db.Column(db.Boolean, default=False)
    new_client_welcome_message = db.Column(db.Text, nullable=True)
    google_review_link = db.Column(db.String(500), nullable=True)
    new_client_delay_send = db.Column(db.Boolean, default=False)
    new_client_delay_hours = db.Column(db.Integer, default=2)
    marketing_max_daily_sends = db.Column(db.Integer, default=30)

    # Unipile WhatsApp Account ID (salvato dopo connessione)
    unipile_account_id = db.Column(db.String(100), nullable=True)

    # Logo negozio (immagine ottimizzata, max 200px altezza)
    logo_image = db.deferred(db.Column(db.LargeBinary, nullable=True))
    logo_mime_type = db.Column(db.String(50), nullable=True)  # es. image/png, image/webp
    logo_filename = db.Column(db.String(255), nullable=True)   # nome file originale
    logo_visible_in_booking_page = db.Column(db.Boolean, default=True)  # Se True, mostra il logo nella pagina booking_public

    # Preset turni giornalieri salvati come array JSON
    # Formato: [{"name":"Turno lungo","start":"09:00","end":"18:00","breakStart":"13:00","breakDuration":"60"}, ...]
    shift_presets = db.Column(db.Text, nullable=True, default='[]')

    @property
    def closing_days_list(self):
        """Ritorna una lista di stringhe (es. ["Domenica","Sabato"]) se presente, altrimenti vuota."""
        if not self.closing_days:
            return []
        return json.loads(self.closing_days)

    @closing_days_list.setter
    def closing_days_list(self, days):
        """Salva una lista di stringhe come JSON nella colonna `closing_days`."""
        if not days:
            self.closing_days = None
        else:
            self.closing_days = json.dumps(days)

    def __repr__(self):
        return f"<BusinessInfo {self.business_name}>"

class MarketingTemplate(db.Model):
    __tablename__ = 'marketing_templates'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nome = db.Column(db.String(100), nullable=False)
    testo = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now())
    
    def to_dict(self):
        return {
            'id': self.id,
            'nome': self.nome,
            'testo': self.testo
        }

class Receipt(db.Model):
    __tablename__ = 'scontrini'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    created_at = db.Column(db.DateTime, server_default=func.now())
    total_amount = db.Column(db.Float, nullable=False)
    is_fiscale = db.Column(db.Boolean, default=True)
    voci = db.Column(JSON, nullable=False)
    numero_progressivo = db.Column(db.String, nullable=False)

    # Collegamenti
    cliente_id = db.Column(db.Integer, db.ForeignKey('clienti.id'), nullable=True)
    operatore_id = db.Column(db.Integer, db.ForeignKey('operatori.id'), nullable=True)
    cliente = db.relationship('Client', backref='scontrini')
    operatore = db.relationship('Operator', backref='scontrini')
    
class LoginAttempt(db.Model):
    __tablename__ = 'login_attempts'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    key = db.Column(db.String(200), nullable=False, unique=True, index=True)  # username o IP o combinazione username|ip
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_attempt = db.Column(db.DateTime, nullable=True)
    locked_until = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<LoginAttempt {self.key} attempts={self.attempts}>"
    

######### SEZIONE PACCHETTI #########
# Nuovi enum
class PacchettoStatus(PyEnum):
    Preventivo = "preventivo"
    Attivo = "attivo"
    Completato = "completato"
    Abbandonato = "abbandonato"
    Eliminato = "eliminato"  # Soft delete

class ScontoTipo(PyEnum):
    Percentuale = "percentuale"
    Ogni_N_Omaggio = "ogni_n_omaggio"

# Promo salvate per i pacchetti
class PromoPacchetto(db.Model):
    __tablename__ = 'promo_pacchetti'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nome = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(50), nullable=False)  # 'percentuale' o 'sedute_omaggio'
    soglia = db.Column(db.Integer, nullable=True)  # Ogni N sedute
    percentuale = db.Column(db.Integer, nullable=True)  # % sconto (se tipo=percentuale)
    sedute_omaggio = db.Column(db.Integer, nullable=True)  # Num sedute omaggio (se tipo=sedute_omaggio)
    attiva = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, server_default=func.now())
    
    def to_dict(self):
        return {
            'id': self.id,
            'nome': self.nome,
            'tipo': self.tipo,
            'soglia': self.soglia,
            'percentuale': self.percentuale,
            'sedute_omaggio': self.sedute_omaggio,
            'attiva': self.attiva
        }

class SedutaStatus(PyEnum):
    Presente = 1      # Solo presente
    Pianificata = 2   # Pianificata (con data)
    Saltata = 3       # Saltata
    Effettuata = 4    # Effettuata (trigger invio WhatsApp automatico)

# Tabella associazione pacchetti-operatori
pacchetto_operator = db.Table(
    'pacchetto_operator',
    db.Column('pacchetto_id', db.Integer, db.ForeignKey('pacchetti.id'), primary_key=True),
    db.Column('operator_id', db.Integer, db.ForeignKey('operatori.id'), primary_key=True)
)

# Classe Pacchetto
class Pacchetto(db.Model):
    __tablename__ = 'pacchetti'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clienti.id'), nullable=False)
    nome = db.Column(db.String(100), nullable=False)
    data_sottoscrizione = db.Column(db.Date, nullable=False)
    note = db.Column(db.Text, nullable=True)

    # Tipo pacchetto
    tipo = db.Column(
        ENUM(
            PacchettoTipo, 
            name="pacchetto_tipo_enum", 
            create_type=False,  # L'enum esiste già nel DB
            values_callable=lambda x: [e.value for e in x]  # Usa i valori, non i nomi
        ),
        nullable=False,
        default=PacchettoTipo.Servizi
    )
    
    # Campi per Carta Prepagata
    credito_iniziale = db.Column(db.Numeric(10, 2), nullable=True)  # Importo caricato
    credito_residuo = db.Column(db.Numeric(10, 2), nullable=True)   # Saldo disponibile
    data_scadenza = db.Column(db.Date, nullable=True)               # Scadenza carta
    beneficiario_nome = db.Column(db.String(100), nullable=True)    # Nome beneficiario (se diverso da client)
    
    # Vincoli utilizzo prepagata (JSON)
    # Formato: {"tipo": "tutti" | "categoria" | "sottocategoria" | "servizi", 
    #           "categoria": "Solarium" | "Estetica",
    #           "sottocategoria_id": 5,
    #           "servizi_ids": [1, 2, 3]}
    vincoli_utilizzo = db.Column(db.JSON, nullable=True)

    status = db.Column(
        ENUM(PacchettoStatus, name="pacchetto_status_enum", create_type=True),
        nullable=False,
        default=PacchettoStatus.Preventivo
    )
    history = db.Column(db.Text, nullable=True)  # Storico semplice come testo
    costo_totale_lordo = db.Column(db.Numeric(10, 2), nullable=False)  # Lordo
    costo_totale_scontato = db.Column(db.Numeric(10, 2), nullable=True)  # Scontato

    # Consenso informato firmato (PDF caricato)
    consenso_pdf = db.deferred(db.Column(db.LargeBinary, nullable=True))
    consenso_pdf_nome = db.Column(db.String(255), nullable=True)
    consenso_pdf_data = db.Column(db.DateTime, nullable=True)
    
    # Relazioni
    client = db.relationship('Client', backref='pacchetti')
    preferred_operators = db.relationship(
        'Operator',
        secondary=pacchetto_operator,
        backref='pacchetti'
    )
    sedute = db.relationship('PacchettoSeduta', backref='pacchetto', cascade='all, delete-orphan')
    rate = db.relationship('PacchettoRata', backref='pacchetto', cascade='all, delete-orphan')
    sconto_regole = db.relationship('PacchettoScontoRegola', backref='pacchetto', cascade='all, delete-orphan')
    pagamento_regole = db.relationship('PacchettoPagamentoRegola', backref='pacchetto', cascade='all, delete-orphan')

# Classe PacchettoSeduta (semplificata, diretta)
class PacchettoSeduta(db.Model):
    """Singola seduta del pacchetto, collegata direttamente a Pacchetto e Service"""
    __tablename__ = 'pacchetto_sedute'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pacchetto_id = db.Column(db.Integer, db.ForeignKey('pacchetti.id'), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('servizi.id'), nullable=False)
    
    # Ordine cronologico delle sedute nel pacchetto (per sovrapporre tipi di servizio)
    ordine = db.Column(db.Integer, nullable=False)
    
    # Data trattamento
    data_trattamento = db.Column(db.DateTime, nullable=True)
    
    # Operatore (da class Operator)
    operatore_id = db.Column(db.Integer, db.ForeignKey('operatori.id'), nullable=True)
    
    # Stato seduta
    stato = db.Column(
        db.Integer,  # Usa int per matchare
        nullable=False,
        default=SedutaStatus.Presente
    )
    
    # Nota seduta
    nota = db.Column(db.Text)
    
    # Relazioni
    service = db.relationship('Service', backref='pacchetto_sedute')
    operatore = db.relationship('Operator', backref='pacchetto_sedute')

# Classe PacchettoRata
class PacchettoRata(db.Model):
    __tablename__ = 'pacchetto_rate'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pacchetto_id = db.Column(db.Integer, db.ForeignKey('pacchetti.id'), nullable=False)
    importo = db.Column(db.Numeric(10, 2), nullable=False)
    data_scadenza = db.Column(db.Date, nullable=True)
    is_pagata = db.Column(db.Boolean, default=False)  # Stato: attesa/pagata
    data_pagamento = db.Column(db.DateTime, nullable=True)

# Classe PacchettoScontoRegola
class PacchettoScontoRegola(db.Model):
    __tablename__ = 'pacchetto_sconto_regole'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pacchetto_id = db.Column(db.Integer, db.ForeignKey('pacchetti.id'), nullable=False)
    sconto_tipo = db.Column(
        ENUM(ScontoTipo, name="sconto_tipo_enum", create_type=True),
        nullable=False
    )
    sconto_valore = db.Column(db.Numeric(10, 2), nullable=True)  # Per Percentuale: valore %; per Ogni_N_Omaggio: N
    omaggi_extra = db.Column(db.Integer, nullable=True)  # Per Ogni_N_Omaggio: numero omaggi
    descrizione = db.Column(db.String(255), nullable=True)

# Classe PacchettoPagamentoRegola
class PacchettoPagamentoRegola(db.Model):
    __tablename__ = 'pacchetto_pagamento_regole'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pacchetto_id = db.Column(db.Integer, db.ForeignKey('pacchetti.id'), nullable=False)
    formula_pagamenti = db.Column(db.Boolean, nullable=False)  # True: rate; False: saldo immediato
    numero_rate = db.Column(db.Integer, nullable=False)  # Numero di rate
    descrizione = db.Column(db.String(255), nullable=True)

class MovimentoPrepagata(db.Model):
    """Traccia ogni utilizzo/ricarica della carta prepagata"""
    __tablename__ = 'movimenti_prepagata'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pacchetto_id = db.Column(db.Integer, db.ForeignKey('pacchetti.id'), nullable=False)
    data_movimento = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    tipo_movimento = db.Column(db.String(20), nullable=False)  # 'ricarica' o 'utilizzo'
    importo = db.Column(db.Numeric(10, 2), nullable=False)
    saldo_dopo = db.Column(db.Numeric(10, 2), nullable=False)
    descrizione = db.Column(db.String(255), nullable=True)  # es. "Servizio: Massaggio"
    receipt_id = db.Column(db.Integer, db.ForeignKey('scontrini.id'), nullable=True)
    
    # Relazioni
    pacchetto = db.relationship('Pacchetto', backref='movimenti_prepagata')
    receipt = db.relationship('Receipt', backref='movimenti_prepagata')

class MarketingInvio(db.Model):
    """Traccia ogni invio WhatsApp marketing per rispettare limite giornaliero"""
    __tablename__ = 'marketing_invii'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clienti.id'), nullable=False)
    data_invio = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    messaggio = db.Column(db.Text, nullable=True)
    stato = db.Column(db.String(20), nullable=False, default='inviato')  # inviato, errore, pending
    errore = db.Column(db.String(500), nullable=True)
    
    # Relazioni
    client = db.relationship('Client', backref='marketing_invii')