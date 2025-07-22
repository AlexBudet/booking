import json
from enum import Enum as PyEnum
from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Time, Text
from sqlalchemy.sql import func
from sqlalchemy import JSON, DateTime
from appl import db
from datetime import timedelta, datetime

# Enum necessari
class ServiceCategory(PyEnum):
    Solarium = "Solarium"
    Estetica = "Estetica"

class AppointmentStatus(PyEnum):
    DEFAULT = 0
    IN_ISTITUTO = 1
    PAGATO = 2
    NON_ARRIVATO = 3

class AppointmentSource(PyEnum):
    gestionale = "gestionale"
    web = "web"

# Tabella di relazione servizi <-> operatori
service_operator = db.Table(
    'service_operator',
    db.Column('service_id', db.Integer, db.ForeignKey('servizi.id'), primary_key=True),
    db.Column('operator_id', db.Integer, db.ForeignKey('operatori.id'), primary_key=True)
)

class Operator(db.Model):
    __tablename__ = 'operatori'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_nome = db.Column(db.String(50))
    user_cognome = db.Column(db.String(50))
    user_tipo = db.Column(Enum('estetista', 'macchinario', name='user_tipo_enum'), nullable=False)
    is_deleted = db.Column(db.Boolean, default=False)
    is_visible = db.Column(db.Boolean, default=True)
    order = db.Column(db.Integer, default=0)
    use_twenty_minutes = db.Column(db.Boolean, default=False)
    services = db.relationship('Service', secondary=service_operator, back_populates='operators')

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
    is_deleted = db.Column(db.Boolean, default=False)
    is_visible_in_calendar = db.Column(db.Boolean, default=True)
    is_visible_online = db.Column(db.Boolean, default=True)
    operators = db.relationship('Operator', secondary=service_operator, back_populates='services')

    def __repr__(self):
        return f"<Service {self.servizio_nome}>"

class Appointment(db.Model):
    __tablename__ = 'appuntamenti'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    last_edit = db.Column(db.DateTime(timezone=True))
    client_id = db.Column(db.Integer, db.ForeignKey('clienti.id'), nullable=False)
    operator_id = db.Column(db.Integer, db.ForeignKey('operatori.id'), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('servizi.id'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    _duration = db.Column("duration", db.Integer, nullable=False)
    note = db.Column(db.String(1000), nullable=True)
    source = db.Column(db.Enum(AppointmentSource, native_enum=False), default=AppointmentSource.gestionale, nullable=False)
    stato = db.Column(db.Enum(AppointmentStatus, native_enum=False), default=AppointmentStatus.DEFAULT, nullable=False)
    booking_session_id = db.Column(db.String(64), nullable=True, index=True)

    client = db.relationship('Client', backref='appointments')
    operator = db.relationship('Operator', backref='appointments')
    service = db.relationship('Service', backref='appointments')

    @property
    def duration(self):
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
    vat_code = db.Column(String(50), nullable=True)
    pec_code = db.Column(String(100), nullable=True)
    phone = db.Column(String(30), nullable=True)
    mobile = db.Column(String(30), nullable=True)
    email = db.Column(String(100), nullable=True)
    opening_time = db.Column(db.Time, nullable=False)
    closing_time = db.Column(db.Time, nullable=False)
    active_opening_time = db.Column(db.Time, nullable=False, default=datetime.strptime("08:00","%H:%M").time())
    active_closing_time = db.Column(db.Time, nullable=False, default=datetime.strptime("20:00","%H:%M").time())
    closing_days = db.Column(Text, nullable=True)
    is_deleted = db.Column(Boolean, default=False)
    vat_percentage = db.Column(db.Float, default=22.0)
    printer_ip = db.Column(db.String(64), default="192.168.1.155")
    whatsapp_message = db.Column(db.Text, nullable=True)

    @property
    def closing_days_list(self):
        if not self.closing_days:
            return []
        return json.loads(self.closing_days)

    @closing_days_list.setter
    def closing_days_list(self, days):
        if not days:
            self.closing_days = None
        else:
            self.closing_days = json.dumps(days)

    def __repr__(self):
        return f"<BusinessInfo {self.business_name}>"