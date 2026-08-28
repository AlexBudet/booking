# routes/usage.py
"""Registrazione dei consumi (WhatsApp, e-mail) sul database del tenant.

Perche' esiste anche qui e non solo nel CRM: la maggior parte degli invii che
costano parte DA QUESTA applicazione - promemoria mattutini, conferme di
prenotazione, codici di accesso, riepiloghi errori. Finora finivano solo nei
print() del processo, che su Azure durano un'ora e mezza e poi spariscono: non
esisteva alcun modo di sapere quanti messaggi si consumano davvero.

La tabella usage_events sta nel database del tenant ed e' la stessa che scrive
il CRM: il pannello owner somma le due sorgenti e la colonna `origine`
distingue chi ha inviato.

Due scelte volute:

1. INSERT in SQL diretto invece che via automap. L'app riflette le tabelle una
   volta sola all'avvio (main.py: base.prepare); una tabella creata dopo non
   comparirebbe fra le classi mappate fino al riavvio successivo. Con l'SQL
   scritto a mano l'unica condizione e' che la tabella esista.

2. Sessione propria dal SessionFactory del tenant, come log_ticker_error. Gli
   invii partono da thread in background dove non c'e' request context e
   g.db_session non esiste.

Non solleva mai: se non si riesce a contare un invio, l'invio e' comunque gia'
avvenuto e far fallire il chiamante peggiorerebbe soltanto le cose.
"""

from sqlalchemy import text

_SQL_INSERT = text("""
    INSERT INTO usage_events (canale, tipo, origine, esito, errore)
    VALUES (:canale, :tipo, 'booking', :esito, :errore)
""")


def registra_uso(app, tenant_id, canale, tipo='altro', esito='ok', errore=None):
    """Conta un invio. `esito` accetta True/False oltre a 'ok'/'errore'.

    I fallimenti si contano come i successi: un tentativo respinto e' comunque
    una chiamata all'API, e sui fallimenti si paga il tempo anche quando non si
    paga il messaggio.
    """
    if not app or not tenant_id:
        return
    try:
        sessions = app.config.get('DB_SESSIONS') or {}
        SessionFactory = sessions.get(tenant_id)
        if SessionFactory is None:
            return
        session_db = SessionFactory()
        try:
            session_db.execute(_SQL_INSERT, {
                'canale': str(canale)[:20],
                'tipo': str(tipo)[:40],
                'esito': 'ok' if esito in ('ok', True) else 'errore',
                'errore': (str(errore)[:300] if errore else None),
            })
            session_db.commit()
        except Exception:
            session_db.rollback()
            raise
        finally:
            try:
                session_db.close()
            finally:
                try:
                    SessionFactory.remove()
                except Exception:
                    pass
    except Exception as e:
        print(f"[USAGE][{tenant_id}] impossibile registrare il consumo "
              f"{canale}/{tipo}: {repr(e)}")
