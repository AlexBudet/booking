#appl/models.py
import json
from enum import Enum as PyEnum
import os
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
    is_deleted = db.Column(db.Boolean, default=False)
    is_visible_in_calendar = db.Column(db.Boolean, default=True)
    is_visible_online = db.Column(db.Boolean, default=True)

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

    # Relazioni
    client = db.relationship('Client', backref='appointments')
    operator = db.relationship('Operator', backref='appointments')
    service = db.relationship('Service', backref='appointments')

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