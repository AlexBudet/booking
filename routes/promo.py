"""PROMO DEI SERVIZI: quando un appuntamento e' "in promo" e quanto costa.

Un servizio e' in promo quando in Tools > Servizi ha la spunta PROMO
(servizi.is_promo). Le condizioni si impostano dall'ingranaggio accanto alla
spunta e stanno in servizi.promo_regole (JSON in un TEXT, NULL = nessuna
condizione: promo sempre valida e senza sconto, come prima che esistessero).

LE REGOLE, NELL'ORDINE IN CUI SI CONTROLLANO (dentro()):
  1. periodo: il GIORNO dell'appuntamento sta fra `dal` e `al` (inclusi; uno
     dei due o tutti e due possono mancare);
  2. giorni della settimana: il giorno e' fra quelli scelti (nessuno = tutti);
  3. fasce orarie (nessuna = tutto il giorno), secondo il `criterio`:
       'inizio' (predefinito): il servizio INIZIA dentro una fascia, cioe'
                 dalle <= inizio < alle. 12:30 in 9:00-13:00 e' in promo anche
                 se finisce alle 13:30;
       'tutto':  il servizio inizia E finisce dentro la stessa fascia, cioe'
                 dalle <= inizio e fine <= alle.
Con piu' servizi in fila (prenotazione online di piu' servizi, o blocchi
consecutivi in Agenda) ogni servizio si giudica col SUO orario.

PREZZO: dentro la promo si applica lo sconto (percentuale con arrotondamento
facoltativo all'euro, oppure un nuovo prezzo); fuori dalla promo vale sempre il
prezzo di listino.

FUORI DALLA PROMO (`fuori`, una scelta per ogni promo):
  'pieno':     il servizio si prenota sempre, a prezzo di listino;
  'no_online': online si propongono solo giorni e orari della promo e, finito
               il periodo, il servizio sparisce dal sito; in Agenda si prenota
               comunque, con un avviso.

Chi usa queste regole:
  - Tosca: Tools > Servizi (salvataggio), Agenda (avviso, prezzo nel tooltip
    "Porta in cassa"), Cassa (la riga arriva gia' col prezzo promo);
  - sito prenotazioni: elenco servizi, orari proposti, ricontrollo in /prenota,
    prezzi nel riepilogo e nelle e-mail.

Solo Python puro, niente modelli ne' Flask: lo stesso file serve ai due repo.

⚠️ DUE COPIE IDENTICHE dalla riga "import json": SunBooking
appl/services/promo.py e booking routes/promo.py. Se cambi una, cambia l'altra.
"""
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation

CRITERIO_INIZIO = 'inizio'
CRITERIO_TUTTO = 'tutto'
FUORI_PIENO = 'pieno'
FUORI_NO_ONLINE = 'no_online'
SCONTO_NESSUNO = 'nessuno'
SCONTO_PERCENTUALE = 'percentuale'
SCONTO_PREZZO = 'prezzo'
ARROTONDA_NO = 'nessuno'
ARROTONDA_DIFETTO = 'difetto'
ARROTONDA_ECCESSO = 'eccesso'

MAX_FASCE = 4

# isoweekday(): 1 = lunedi' ... 7 = domenica
GIORNI_BREVI = {1: 'lun', 2: 'mar', 3: 'mer', 4: 'gio', 5: 'ven', 6: 'sab', 7: 'dom'}
_MESI = ['GEN', 'FEB', 'MAR', 'APR', 'MAG', 'GIU', 'LUG', 'AGO', 'SET', 'OTT', 'NOV', 'DIC']


def regole_vuote():
    """Promo senza condizioni: sempre valida, senza sconto, prenotabile sempre."""
    return {
        'dal': None, 'al': None, 'giorni': [], 'fasce': [],
        'criterio': CRITERIO_INIZIO,
        'sconto': {'tipo': SCONTO_NESSUNO, 'valore': None, 'arrotondamento': ARROTONDA_NO},
        'fuori': FUORI_PIENO,
    }


# ------------------------------------------------------------- conversioni
def _data(valore):
    if isinstance(valore, datetime):
        return valore.date()
    if isinstance(valore, date):
        return valore
    testo = str(valore or '').strip()[:10]
    if not testo:
        return None
    return datetime.strptime(testo, '%Y-%m-%d').date()


def _ora(valore):
    if isinstance(valore, time):
        return valore
    testo = str(valore or '').strip()
    if testo == '24:00':
        return time(23, 59, 59)
    return datetime.strptime(testo[:5], '%H:%M').time()


def _hhmm(t):
    if t >= time(23, 59, 59):
        return '24:00'
    return t.strftime('%H:%M')


def _decimale(valore):
    if valore is None or str(valore).strip() == '':
        return None
    return Decimal(str(valore).replace(',', '.').strip())


def _senza_fuso(dt):
    if dt is not None and getattr(dt, 'tzinfo', None) is not None:
        return dt.replace(tzinfo=None)
    return dt


# ----------------------------------------------------------------- lettura
def leggi(testo):
    """Regole salvate (TEXT JSON) -> dizionario completo, pronto per dentro().

    Tollerante: NULL, JSON rotto o campi strani tornano ai valori predefiniti
    del campo. Un errore qui non deve mai fermare Agenda, Cassa o il sito: nel
    caso peggiore la promo vale sempre e senza sconto, cioe' come prima che le
    condizioni esistessero."""
    regole = regole_vuote()
    if not testo:
        return regole
    try:
        dati = json.loads(testo) if isinstance(testo, str) else dict(testo)
    except (ValueError, TypeError):
        return regole
    if not isinstance(dati, dict):
        return regole
    try:
        regole['dal'] = _data(dati.get('dal'))
    except (ValueError, TypeError):
        pass
    try:
        regole['al'] = _data(dati.get('al'))
    except (ValueError, TypeError):
        pass
    giorni = set()
    for g in dati.get('giorni') or []:
        try:
            g = int(g)
        except (ValueError, TypeError):
            continue
        if 1 <= g <= 7:
            giorni.add(g)
    regole['giorni'] = [] if len(giorni) == 7 else sorted(giorni)
    fasce = []
    for f in dati.get('fasce') or []:
        try:
            dalle, alle = (f.get('dalle'), f.get('alle')) if isinstance(f, dict) else (f[0], f[1])
            dalle, alle = _ora(dalle), _ora(alle)
        except (ValueError, TypeError, IndexError, KeyError, AttributeError):
            continue
        if dalle < alle:
            fasce.append((dalle, alle))
    regole['fasce'] = sorted(fasce)[:MAX_FASCE]
    if dati.get('criterio') in (CRITERIO_INIZIO, CRITERIO_TUTTO):
        regole['criterio'] = dati['criterio']
    if dati.get('fuori') in (FUORI_PIENO, FUORI_NO_ONLINE):
        regole['fuori'] = dati['fuori']
    sconto = dati.get('sconto') if isinstance(dati.get('sconto'), dict) else {}
    tipo = sconto.get('tipo')
    try:
        valore = _decimale(sconto.get('valore'))
    except (InvalidOperation, ValueError):
        valore = None
    if tipo == SCONTO_PERCENTUALE and valore is not None and 0 < valore < 100:
        regole['sconto'] = {'tipo': tipo, 'valore': valore,
                            'arrotondamento': sconto.get('arrotondamento')
                            if sconto.get('arrotondamento') in (ARROTONDA_DIFETTO, ARROTONDA_ECCESSO)
                            else ARROTONDA_NO}
    elif tipo == SCONTO_PREZZO and valore is not None and valore >= 0:
        regole['sconto'] = {'tipo': tipo, 'valore': valore, 'arrotondamento': ARROTONDA_NO}
    return regole


def serializza(regole):
    """Dizionario di leggi()/valida() -> TEXT da salvare. None se non c'e'
    nessuna condizione: la colonna resta NULL, come per le promo di prima."""
    if regole == regole_vuote():
        return None
    sconto = regole['sconto']
    return json.dumps({
        'dal': regole['dal'].isoformat() if regole['dal'] else None,
        'al': regole['al'].isoformat() if regole['al'] else None,
        'giorni': list(regole['giorni']),
        'fasce': [{'dalle': _hhmm(a), 'alle': _hhmm(b)} for a, b in regole['fasce']],
        'criterio': regole['criterio'],
        'sconto': {'tipo': sconto['tipo'],
                   'valore': str(sconto['valore']) if sconto['valore'] is not None else None,
                   'arrotondamento': sconto['arrotondamento']},
        'fuori': regole['fuori'],
    }, ensure_ascii=False)


def per_pagina(regole):
    """Le regole in forma JSON-friendly per i form e le API (date e ore testo)."""
    sconto = regole['sconto']
    return {
        'dal': regole['dal'].isoformat() if regole['dal'] else '',
        'al': regole['al'].isoformat() if regole['al'] else '',
        'giorni': list(regole['giorni']),
        'fasce': [{'dalle': _hhmm(a), 'alle': _hhmm(b)} for a, b in regole['fasce']],
        'criterio': regole['criterio'],
        'sconto': {'tipo': sconto['tipo'],
                   'valore': float(sconto['valore']) if sconto['valore'] is not None else None,
                   'arrotondamento': sconto['arrotondamento']},
        'fuori': regole['fuori'],
    }


# -------------------------------------------------------------- validazione
def valida(dati, prezzo_listino):
    """Dati del form dell'ingranaggio -> (regole, errori).

    Qui si rifiuta quello che leggi() scarterebbe in silenzio: chi salva deve
    sapere che cosa non va. errori vuoto = si puo' salvare."""
    errori = []
    regole = regole_vuote()
    dati = dati if isinstance(dati, dict) else {}

    for chiave, etichetta in (('dal', 'Dal'), ('al', 'Al')):
        try:
            regole[chiave] = _data(dati.get(chiave))
        except (ValueError, TypeError):
            errori.append(f'Data "{etichetta}" non valida.')
    if regole['dal'] and regole['al'] and regole['al'] < regole['dal']:
        errori.append('La data "Al" viene prima della data "Dal".')

    giorni = set()
    for g in dati.get('giorni') or []:
        try:
            g = int(g)
        except (ValueError, TypeError):
            errori.append('Giorno della settimana non valido.')
            continue
        if not 1 <= g <= 7:
            errori.append('Giorno della settimana non valido.')
            continue
        giorni.add(g)
    regole['giorni'] = [] if len(giorni) == 7 else sorted(giorni)

    fasce = []
    for i, f in enumerate(dati.get('fasce') or [], start=1):
        try:
            dalle, alle = (f.get('dalle'), f.get('alle')) if isinstance(f, dict) else (f[0], f[1])
            if not str(dalle or '').strip() and not str(alle or '').strip():
                continue                      # riga lasciata vuota: si ignora
            dalle, alle = _ora(dalle), _ora(alle)
        except (ValueError, TypeError, IndexError, KeyError, AttributeError):
            errori.append(f'Fascia oraria {i}: orari non validi (servono "dalle" e "alle").')
            continue
        if dalle >= alle:
            errori.append(f'Fascia oraria {i}: "dalle" deve venire prima di "alle".')
            continue
        fasce.append((dalle, alle))
    if len(fasce) > MAX_FASCE:
        errori.append(f'Al massimo {MAX_FASCE} fasce orarie.')
    regole['fasce'] = sorted(fasce)[:MAX_FASCE]

    criterio = dati.get('criterio') or CRITERIO_INIZIO
    if criterio not in (CRITERIO_INIZIO, CRITERIO_TUTTO):
        errori.append('Criterio orario non valido.')
    else:
        regole['criterio'] = criterio

    fuori = dati.get('fuori') or FUORI_PIENO
    if fuori not in (FUORI_PIENO, FUORI_NO_ONLINE):
        errori.append('Scelta "fuori promo" non valida.')
    else:
        regole['fuori'] = fuori

    sconto = dati.get('sconto') if isinstance(dati.get('sconto'), dict) else {}
    tipo = sconto.get('tipo') or SCONTO_NESSUNO
    try:
        valore = _decimale(sconto.get('valore'))
    except (InvalidOperation, ValueError):
        valore = None
        if tipo != SCONTO_NESSUNO:
            errori.append('Valore dello sconto non valido.')
            tipo = None
    listino = Decimal(str(prezzo_listino or 0))
    if tipo == SCONTO_NESSUNO:
        pass
    elif tipo == SCONTO_PERCENTUALE:
        if valore is None or not (0 < valore < 100):
            errori.append('Lo sconto in percentuale va da 1 a 99.')
        else:
            arr = sconto.get('arrotondamento') or ARROTONDA_NO
            if arr not in (ARROTONDA_NO, ARROTONDA_DIFETTO, ARROTONDA_ECCESSO):
                arr = ARROTONDA_NO
            regole['sconto'] = {'tipo': tipo, 'valore': valore.quantize(Decimal('0.01')),
                                'arrotondamento': arr}
            if prezzo_scontato(prezzo_listino, regole) >= float(listino):
                errori.append('Con questo arrotondamento il prezzo non scende sotto il listino.')
    elif tipo == SCONTO_PREZZO:
        if valore is None or valore < 0:
            errori.append('Scrivi il nuovo prezzo (0 o più).')
        elif valore >= listino:
            errori.append(f'Il nuovo prezzo deve essere più basso del listino ({euro(listino)}).')
        else:
            regole['sconto'] = {'tipo': tipo, 'valore': valore.quantize(Decimal('0.01')),
                                'arrotondamento': ARROTONDA_NO}
    elif tipo is not None:
        errori.append('Tipo di sconto non valido.')

    return regole, errori


# -------------------------------------------------------------- le regole
def dentro(regole, inizio, durata_minuti):
    """True se un servizio che inizia a `inizio` (datetime, ora italiana) e
    dura `durata_minuti` rientra nella promo. Ordine e regole nel docstring."""
    inizio = _senza_fuso(inizio)
    if inizio is None:
        return False
    giorno = inizio.date()
    if regole['dal'] and giorno < regole['dal']:
        return False
    if regole['al'] and giorno > regole['al']:
        return False
    if regole['giorni'] and giorno.isoweekday() not in regole['giorni']:
        return False
    if not regole['fasce']:
        return True
    ora = inizio.time()
    fine = inizio + timedelta(minutes=int(durata_minuti or 0))
    for dalle, alle in regole['fasce']:
        if regole['criterio'] == CRITERIO_TUTTO:
            if dalle <= ora and fine <= datetime.combine(giorno, alle):
                return True
        elif dalle <= ora < alle:
            return True
    return False


def ha_sconto(regole):
    return regole['sconto']['tipo'] in (SCONTO_PERCENTUALE, SCONTO_PREZZO)


def prezzo_scontato(prezzo_listino, regole):
    """Prezzo DENTRO la promo (float, 2 decimali). Senza sconto = listino."""
    listino = Decimal(str(prezzo_listino or 0))
    sconto = regole['sconto']
    if sconto['tipo'] == SCONTO_PREZZO and sconto['valore'] is not None:
        nuovo = Decimal(str(sconto['valore']))
    elif sconto['tipo'] == SCONTO_PERCENTUALE and sconto['valore'] is not None:
        grezzo = listino * (Decimal('1') - Decimal(str(sconto['valore'])) / Decimal('100'))
        if sconto['arrotondamento'] == ARROTONDA_DIFETTO:
            nuovo = grezzo.to_integral_value(rounding=ROUND_DOWN)
        elif sconto['arrotondamento'] == ARROTONDA_ECCESSO:
            nuovo = grezzo.to_integral_value(rounding=ROUND_CEILING)
        else:
            nuovo = grezzo
    else:
        nuovo = listino
    # Mai piu' del listino: se il listino e' stato abbassato dopo aver fissato
    # il "nuovo prezzo", la promo non deve far pagare di piu'.
    nuovo = min(max(nuovo, Decimal('0')), listino)
    return float(nuovo.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def prezzo(is_promo, regole, prezzo_listino, inizio, durata_minuti):
    """(prezzo, in_promo) di UN servizio a quell'orario. in_promo e' True solo
    se il servizio e' spuntato PROMO e l'orario rientra nelle condizioni."""
    listino = float(prezzo_listino or 0)
    if not is_promo or not dentro(regole, inizio, durata_minuti):
        return listino, False
    return prezzo_scontato(listino, regole), True


def prezzo_scontato_servizio(servizio, inizio, durata_minuti):
    """Prezzo promo di un servizio (oggetto con is_promo, promo_regole,
    servizio_prezzo) per un appuntamento a quell'orario, SOLO se e' davvero
    sotto il listino; altrimenti None (listino). Mai un'eccezione: chi chiama
    (Agenda, Cassa, sito) non si deve fermare per una promo."""
    try:
        if not getattr(servizio, 'is_promo', False):
            return None
        listino = float(getattr(servizio, 'servizio_prezzo', 0) or 0)
        valore, in_promo = prezzo(True, leggi(getattr(servizio, 'promo_regole', None)),
                                  listino, inizio, durata_minuti)
        return valore if in_promo and valore < listino else None
    except Exception:
        return None


def finita(regole, oggi):
    """True se il periodo della promo e' finito (dopo `al`)."""
    return bool(regole['al'] and _data(oggi) > regole['al'])


def stato_online(is_promo, regole, oggi):
    """Come si presenta il servizio sul sito di prenotazione:
       'promo'    nella sezione PROMO, con condizioni e prezzo;
       'normale'  nella sua sottocategoria, a prezzo di listino;
       'nascosto' non prenotabile online (promo 'no_online' finita)."""
    if not is_promo:
        return 'normale'
    if finita(regole, oggi):
        return 'nascosto' if regole['fuori'] == FUORI_NO_ONLINE else 'normale'
    return 'promo'


def vincola_online(is_promo, regole):
    """True se online il servizio si prenota SOLO dentro la promo."""
    return bool(is_promo) and regole['fuori'] == FUORI_NO_ONLINE


# ------------------------------------------------------------------- testi
def euro(valore):
    v = Decimal(str(valore or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if v == v.to_integral_value():
        return f'€{int(v)}'
    return '€' + f'{v:.2f}'.replace('.', ',')


def data_breve(d):
    return f'{d.day:02d} {_MESI[d.month - 1]} {d.year}'


def _elenco(voci):
    voci = list(voci)
    if len(voci) <= 1:
        return ''.join(voci)
    return ', '.join(voci[:-1]) + ' e ' + voci[-1]


def _ora_breve(t):
    if t >= time(23, 59, 59):
        return '24:00'
    return f'{t.hour}:{t.minute:02d}'


def testo_giorni(regole):
    giorni = regole['giorni']
    if not giorni:
        return ''
    # Giorni consecutivi da 3 in su: "da lun a ven"
    if len(giorni) >= 3 and giorni == list(range(giorni[0], giorni[-1] + 1)):
        return f'da {GIORNI_BREVI[giorni[0]]} a {GIORNI_BREVI[giorni[-1]]}'
    return _elenco(GIORNI_BREVI[g] for g in giorni)


def testo_fasce(regole):
    return _elenco(f'{_ora_breve(a)}–{_ora_breve(b)}' for a, b in regole['fasce'])


def testo_periodo(regole):
    dal, al = regole['dal'], regole['al']
    if dal and al:
        return f'dal {data_breve(dal)} al {data_breve(al)}'
    if dal:
        return f'dal {data_breve(dal)}'
    if al:
        return f'fino al {data_breve(al)}'
    return ''


def testo_condizioni(regole):
    """Le condizioni in una riga, per il cliente e per l'Agenda:
    "mar e mer · 9:00–13:00 · dal 16 NOV 2026 al 30 NOV 2026". Vuoto se la
    promo vale sempre."""
    parti = [testo_giorni(regole)]
    fasce = testo_fasce(regole)
    if fasce:
        if regole['criterio'] == CRITERIO_TUTTO:
            parti.append(f'appuntamento tutto tra {fasce}')
        else:
            parti.append(f'inizio tra {fasce}')
    parti.append(testo_periodo(regole))
    return ' · '.join(p for p in parti if p)


def testo_prezzo(regole, prezzo_listino):
    """"€30 invece di €34" (vuoto senza sconto)."""
    if not ha_sconto(regole):
        return ''
    scontato = prezzo_scontato(prezzo_listino, regole)
    if scontato >= float(prezzo_listino or 0):
        return ''
    return f'{euro(scontato)} invece di {euro(prezzo_listino)}'
