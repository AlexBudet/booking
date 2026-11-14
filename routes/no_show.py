"""NO-SHOW e ANNULLAMENTI ALL'ULTIMO: chi salta gli appuntamenti.

Un "salto" e':
  - un no-show: appuntamento segnato NON ARRIVATO in Agenda (stessa persona,
    stesso giorno = un salto solo, anche se i servizi erano tre);
  - un annullamento all'ultimo: prenotazione online annullata dal link
    dell'e-mail meno di N ore prima dell'inizio (N = booking_noshow_ore_tardivo).
    L'ora dell'annullamento e' in appuntamenti.annullato_il, scritta dal sito
    prenotazioni: prima di quella colonna non si sapeva, quindi conta solo da
    allora in poi.

Chi prenota online si riconosce dal CELLULARE oppure dall'E-MAIL (basta uno dei
due): si confrontano con i recapiti della scheda cliente dell'appuntamento e
con quelli scritti nella nota delle prenotazioni online ("Telefono: ...,
Email: ..."), che restano anche quando la prenotazione non e' ancora stata
associata a una scheda.

Blocco online (Tools > Booking Web): almeno `soglia` salti negli ultimi `mesi`
mesi -> il sito prenotazioni non accetta la prenotazione e chiede di
contattare il centro. Lo sblocco a mano (clienti.prenotazioni_online_sblocco_il)
fa contare solo i salti successivi.

Tutto qui legge con SQL semplice, senza i modelli: lo stesso file serve a Tosca
e al sito prenotazioni.

⚠️ DUE COPIE IDENTICHE dalla riga "import html": SunBooking
appl/services/no_show.py e booking routes/no_show.py. Se cambi una, cambia
l'altra.
"""
import html
import re
from calendar import monthrange
from datetime import timedelta

from sqlalchemy import DateTime, text

NO_SHOW = 'no_show'
ANNULLATO_TARDI = 'annullato_tardi'


# --------------------------------------------------------------- recapiti
def normalizza_cellulare(valore):
    """Solo le cifre, senza prefisso internazionale italiano. None per i
    segnaposto ("", "0", "0000000000"): non sono un numero di nessuno."""
    cifre = re.sub(r'\D', '', str(valore or ''))
    if cifre.startswith('0039'):
        cifre = cifre[4:]
    elif cifre.startswith('39') and len(cifre) in (11, 12):
        # +39 davanti a un cellulare di 9-10 cifre. Un cellulare di 10 cifre
        # che inizia per 39 (391, 392...) resta com'e'.
        cifre = cifre[2:]
    if len(cifre) < 8 or len(set(cifre)) == 1:
        return None
    return cifre


def normalizza_email(valore):
    e = str(valore or '').strip().lower()
    if '@' not in e or '.' not in e.split('@')[-1]:
        return None
    return e


_RE_NOTA_TEL = re.compile(r'Telefono:\s*([^,]*)')
_RE_NOTA_EMAIL = re.compile(r'Email:\s*(\S+)')
_RE_NOTA_NOME = re.compile(r'Nome:\s*([^,]*),\s*Cognome:\s*([^,]*)')


def recapiti_dalla_nota(nota):
    """(cellulare, email, nome) dalla nota di una prenotazione online. La nota
    e' salvata con escape(): si decodifica prima di leggerla."""
    testo = html.unescape(str(nota or ''))
    if 'Telefono:' not in testo and 'Email:' not in testo:
        return None, None, None
    m_tel = _RE_NOTA_TEL.search(testo)
    m_mail = _RE_NOTA_EMAIL.search(testo)
    m_nome = _RE_NOTA_NOME.search(testo)
    nome = None
    if m_nome:
        nome = ' '.join(x.strip() for x in m_nome.groups() if x and x.strip()) or None
    return (normalizza_cellulare(m_tel.group(1)) if m_tel else None,
            normalizza_email(m_mail.group(1).rstrip('.,;')) if m_mail else None,
            nome)


# ----------------------------------------------------------------- regole
def regole(biz):
    """Impostazioni di Tools > Booking Web, con i valori di partenza se mancano."""
    def intero(nome, predefinito, minimo, massimo):
        try:
            v = int(getattr(biz, nome, None))
        except (TypeError, ValueError):
            return predefinito
        return max(minimo, min(massimo, v))
    return {
        'annullo_attivo': bool(getattr(biz, 'booking_annullo_blocco_attivo', False)),
        'annullo_ore': intero('booking_annullo_blocco_ore', 24, 1, 168),
        'blocco_attivo': bool(getattr(biz, 'booking_noshow_blocco_attivo', False)),
        'soglia': intero('booking_noshow_soglia', 2, 1, 20),
        'mesi': intero('booking_noshow_mesi', 12, 1, 60),
        'ore_tardivo': intero('booking_noshow_ore_tardivo', 24, 1, 168),
    }


# ------------------------------------------------- messaggio "troppo tardi"
# Cosa legge il cliente quando clicca "Annulla la prenotazione" nell'e-mail ma
# mancano meno ore del limite. Si scrive in Tools > Booking Web
# (business_info.booking_annullo_messaggio); vuoto = il testo qui sotto.
# Stessa sintassi dei messaggi WhatsApp: {{tag}}.
MESSAGGIO_ANNULLO_PREDEFINITO = (
    "Gentile {{nome}} {{cognome}},\n"
    "la prenotazione del {{data}} alle {{ora}} per {{servizi}} non si può più annullare online: "
    "mancano meno di {{ore_limite}} ore all'appuntamento.\n\n"
    "Se non puoi venire, chiama {{nome_istituto}} al numero {{telefono_istituto}} per disdire. "
    "Avvisarci per tempo ci permette di offrire l'orario a qualcun altro, e la disdetta fatta "
    "per telefono non viene conteggiata nel tuo storico.\n\n"
    "Dopo {{soglia}} appuntamenti saltati o annullati all'ultimo momento, le prenotazioni online "
    "potrebbero essere possibili solo contattando direttamente il centro.\n\n"
    "Grazie per la collaborazione!"
)

# (tag, cosa diventa): per i pulsanti e la legenda in Booking Web.
TAG_ANNULLO = (
    ('nome', 'nome del cliente'),
    ('cognome', 'cognome del cliente'),
    ('data', "data dell'appuntamento"),
    ('ora', "ora dell'appuntamento"),
    ('servizi', 'servizi prenotati'),
    ('nome_istituto', 'nome del centro (Info Azienda)'),
    ('telefono_istituto', 'telefono del centro (Info Azienda)'),
    ('ore_limite', 'ore del limite di annullamento'),
    ('soglia', 'appuntamenti saltati che bloccano le prenotazioni online'),
)

_RE_TAG = re.compile(r'\{\{\s*(\w+)\s*\}\}')


def testo_annullo(biz):
    """Il messaggio scritto in Booking Web, o quello predefinito se vuoto."""
    testo = (getattr(biz, 'booking_annullo_messaggio', None) or '').strip()
    return testo or MESSAGGIO_ANNULLO_PREDEFINITO


def compila_testo(testo, valori):
    """Sostituisce ogni {{tag}} (anche {{ tag }}) con valori[tag]; un tag
    sconosciuto resta com'e', cosi' chi scrive vede l'errore di battitura."""
    return _RE_TAG.sub(lambda m: valori.get(m.group(1), m.group(0)), str(testo or ''))


def nome_cognome_dalla_nota(nota):
    """(nome, cognome) dalla nota di una prenotazione online, o (None, None)."""
    m = _RE_NOTA_NOME.search(html.unescape(str(nota or '')))
    if not m:
        return None, None
    return (m.group(1).strip() or None), (m.group(2).strip() or None)


def _senza_fuso(quando):
    """Data e ora senza fuso orario. In PostgreSQL appuntamenti.start_time e'
    'timestamp with time zone' (ora italiana registrata come +00), mentre
    annullato_il e prenotazioni_online_sblocco_il sono senza fuso (ora italiana):
    sottrarre o confrontare le due forme manda Python in errore (TypeError), ed
    e' successo al primo annullamento dal link (14/11/2026: tile NO-SHOW in
    errore 500, controllo del blocco online saltato). Tolto il fuso, sono tutte
    ore italiane e si confrontano fra loro."""
    if quando is not None and getattr(quando, 'tzinfo', None) is not None:
        return quando.replace(tzinfo=None)
    return quando


def mesi_fa(quando, mesi):
    """Lo stesso giorno di `mesi` mesi prima (il 31 diventa l'ultimo del mese)."""
    totale = quando.year * 12 + (quando.month - 1) - int(mesi)
    anno, mese = divmod(totale, 12)
    giorno = min(quando.day, monthrange(anno, mese + 1)[1])
    return quando.replace(year=anno, month=mese + 1, day=giorno)


# ----------------------------------------------------------------- eventi
def carica_eventi(sessione, ore_tardivo, dal=None, al=None, client_id=None):
    """Salti con inizio appuntamento in [dal, al) (None = senza limite), solo
    di una scheda se c'e' client_id.
    Ogni evento: {tipo, quando, giorno, client_id, nome, cellulari, email}.
    client_id e' None se la scheda dell'appuntamento non ha recapiti propri
    (es. prenotazione online non ancora associata a un cliente)."""
    filtri = ["(a.stato = 'NON_ARRIVATO' OR "
              "(a.is_cancelled_by_client = true AND a.annullato_il IS NOT NULL))"]
    parametri = {}
    if client_id is not None:
        filtri.append('a.client_id = :cid')
        parametri['cid'] = int(client_id)
    if dal is not None:
        filtri.append('a.start_time >= :dal')
        parametri['dal'] = dal
    if al is not None:
        filtri.append('a.start_time < :al')
        parametri['al'] = al
    righe = sessione.execute(text(
        "SELECT a.client_id, a.start_time, a.booking_session_id, a.note, a.stato, "
        "       a.annullato_il, c.cliente_nome, c.cliente_cognome, "
        "       c.cliente_cellulare, c.cliente_email "
        "FROM appuntamenti a LEFT JOIN clienti c ON c.id = a.client_id "
        "WHERE " + ' AND '.join(filtri)
    ).columns(start_time=DateTime, annullato_il=DateTime), parametri).fetchall()
    return eventi_da_righe(righe, ore_tardivo)


# Schede segnaposto: non sono una persona (prenotazioni online non ancora
# associate, blocchi OFF). Chi c'e' dietro si legge dalla nota.
_SCHEDE_SEGNAPOSTO = {('booking', 'online'), ('dummy', 'dummy'), ('cliente', 'booking')}


def scheda_segnaposto(nome, cognome):
    return ((nome or '').strip().lower(), (cognome or '').strip().lower()) in _SCHEDE_SEGNAPOSTO


def eventi_da_righe(righe, ore_tardivo):
    limite = timedelta(hours=int(ore_tardivo))
    eventi = {}
    for r in righe:
        inizio = _senza_fuso(r.start_time)
        annullato_il = _senza_fuso(r.annullato_il)
        if inizio is None:
            continue
        tel_c = normalizza_cellulare(r.cliente_cellulare)
        mail_c = normalizza_email(r.cliente_email)
        tel_n, mail_n, nome_n = recapiti_dalla_nota(r.note)
        # Una scheda vera conta anche senza recapiti validi (es. cellulare
        # "0000000000"): online non la si riconosce, ma in Agenda e nel report si'.
        scheda_vera = r.client_id is not None and not scheda_segnaposto(r.cliente_nome, r.cliente_cognome)
        stato = getattr(r.stato, 'name', r.stato)
        if stato == 'NON_ARRIVATO':
            tipo = NO_SHOW
            chi = r.client_id if scheda_vera else (tel_n or mail_n)
            if chi is None:
                continue    # nessun modo di sapere chi fosse
            chiave = (NO_SHOW, chi, inizio.date())
        else:
            if annullato_il is None:
                continue
            anticipo = inizio - annullato_il
            if anticipo < timedelta(0) or anticipo >= limite:
                continue
            tipo = ANNULLATO_TARDI
            chiave = (ANNULLATO_TARDI, r.booking_session_id or (r.client_id, inizio))
        if scheda_vera:
            nome = ' '.join(x for x in ((r.cliente_nome or '').strip(), (r.cliente_cognome or '').strip()) if x)
        else:
            nome = nome_n
        ev = eventi.get(chiave)
        if ev is None:
            ev = eventi[chiave] = {
                'tipo': tipo, 'quando': inizio, 'giorno': inizio.date(),
                'client_id': r.client_id if scheda_vera else None,
                'nome': nome or 'Cliente', 'cellulari': set(), 'email': set(),
            }
        elif inizio < ev['quando']:
            ev['quando'] = inizio
        ev['cellulari'].update(x for x in (tel_c, tel_n) if x)
        ev['email'].update(x for x in (mail_c, mail_n) if x)
    return list(eventi.values())


def corrisponde(evento, cellulare, email):
    return bool((cellulare and cellulare in evento['cellulari'])
                or (email and email in evento['email']))


# ---------------------------------------------------------------- sblocchi
def carica_sblocchi(sessione):
    """[(cellulare, email, quando)] dei clienti sbloccati a mano."""
    righe = sessione.execute(text(
        "SELECT cliente_cellulare, cliente_email, prenotazioni_online_sblocco_il "
        "FROM clienti WHERE prenotazioni_online_sblocco_il IS NOT NULL "
        "AND is_deleted IS NOT TRUE"
    ).columns(prenotazioni_online_sblocco_il=DateTime)).fetchall()
    return [(normalizza_cellulare(r[0]), normalizza_email(r[1]), _senza_fuso(r[2])) for r in righe]


def sblocco_per(sblocchi, cellulare, email):
    date = [q for tel, mail, q in sblocchi
            if q and ((cellulare and tel == cellulare) or (email and mail == email))]
    return max(date) if date else None


# ------------------------------------------------------------------- blocco
def valuta(eventi, sblocchi, cellulare, email, soglia):
    """Salti che contano per chi ha questo cellulare / e-mail (dopo l'ultimo
    sblocco) e se bastano a bloccarlo."""
    sblocco = sblocco_per(sblocchi, cellulare, email)
    validi = [e for e in eventi
              if corrisponde(e, cellulare, email) and (sblocco is None or e['quando'] > sblocco)]
    validi.sort(key=lambda e: e['quando'])
    return {
        'conteggio': len(validi),
        'no_show': sum(1 for e in validi if e['tipo'] == NO_SHOW),
        'annullati_tardi': sum(1 for e in validi if e['tipo'] == ANNULLATO_TARDI),
        'bloccato': len(validi) >= int(soglia),
        'sblocco': sblocco,
        'eventi': validi,
    }


def controlla_prenotazione(sessione, biz, cellulare, email, adesso):
    """Per il sito prenotazioni: None se chi prenota puo' farlo, altrimenti il
    risultato di valuta(). `adesso` e' l'ora italiana senza fuso."""
    r = regole(biz)
    if not r['blocco_attivo']:
        return None
    tel = normalizza_cellulare(cellulare)
    mail = normalizza_email(email)
    if not tel and not mail:
        return None
    eventi = carica_eventi(sessione, r['ore_tardivo'], dal=mesi_fa(adesso, r['mesi']))
    esito = valuta(eventi, carica_sblocchi(sessione), tel, mail, r['soglia'])
    return esito if esito['bloccato'] else None


# ------------------------------------------------------- spazio in agenda
# Un no-show NON occupa piu' l'agenda (richiesta del 14/11/2026). Di tutti i
# servizi saltati da una persona in un giorno resta occupato solo il PRIMO
# QUARTO D'ORA del primo servizio in ordine di orario (a parita' di orario il
# primo inserito, cioe' l'id piu' basso), nella colonna di quel servizio: tutto
# il resto torna prenotabile, in Agenda e online. In Agenda quel quarto d'ora
# e' il riquadro nero "NO-SHOW · 3 servizi · 3h30": la stessa scelta la fa
# applicaCompressioneNoShow in calendar.js.
#
# I dati NON cambiano: durate e servizi restano quelli prenotati, cosi' i
# report e la classifica dei no-show continuano a contarli per intero, e
# togliendo il no-show i blocchi tornano come prima.
QUARTO_NO_SHOW = 15


def e_no_show(appuntamento):
    stato = getattr(appuntamento, 'stato', None)
    return getattr(stato, 'name', stato) == 'NON_ARRIVATO'


def _persona(appuntamento):
    """Chi ha saltato. Di norma la scheda cliente; una prenotazione online non
    ancora associata sta su una scheda segnaposto condivisa da tutti, e li' la
    persona e' la sessione di prenotazione (o l'appuntamento da solo)."""
    cliente = getattr(appuntamento, 'client', None)
    if cliente is not None and scheda_segnaposto(getattr(cliente, 'cliente_nome', ''),
                                                  getattr(cliente, 'cliente_cognome', '')):
        sessione = getattr(appuntamento, 'booking_session_id', None)
        return ('sessione', sessione) if sessione else ('appuntamento', appuntamento.id)
    return ('cliente', appuntamento.client_id)


def intervalli_occupati(appuntamenti):
    """[(appuntamento, inizio, fine)]: lo spazio che ogni appuntamento occupa
    davvero in agenda. I no-show che non sono il primo della loro persona non
    compaiono; il primo occupa al massimo QUARTO_NO_SHOW minuti.

    `appuntamenti` deve essere il giorno INTERO, tutte le colonne: il primo
    servizio saltato si sceglie fra tutti, anche quelli di altre operatrici.
    Gli annullati vanno tolti prima, come gia' fa chi chiama."""
    primi = {}
    for a in appuntamenti:
        if a.start_time is None or not e_no_show(a):
            continue
        chiave = (_persona(a), a.start_time.date())
        primo = primi.get(chiave)
        if primo is None or (a.start_time, a.id or 0) < (primo.start_time, primo.id or 0):
            primi[chiave] = a
    primi_ids = {id(a) for a in primi.values()}

    risultato = []
    for a in appuntamenti:
        if a.start_time is None:
            continue
        durata = int(getattr(a, '_duration', None) or getattr(a, 'duration', None) or 0)
        if e_no_show(a):
            if id(a) not in primi_ids:
                continue
            durata = min(durata, QUARTO_NO_SHOW) if durata > 0 else QUARTO_NO_SHOW
        risultato.append((a, a.start_time, a.start_time + timedelta(minutes=durata)))
    return risultato
