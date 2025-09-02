# SunBooking - Prenotazioni Online

Applicazione Flask per la gestione delle prenotazioni online di Sun Express 3, pronta per il deploy su Azure Web Services.

## Requisiti

- Python 3.9+
- Azure Web App (App Service)
- Database PostgreSQL su Azure

## Installazione

1. Clona il repository:

2. Installa le dipendenze:

3. Configura le variabili d'ambiente (puoi usare un file `.env` in locale):


## Deploy su Azure

- Carica tutti i file del progetto su Azure Web App.
- Imposta le variabili d'ambiente dal portale Azure (se non usi `.env`).
- Assicurati che il database sia accessibile dagli IP di uscita della Web App.
- Il file principale è `app.py` (o `main.py`).

## Struttura del progetto

```
SunBooking/
├── app.py
├── requirements.txt
├── README.md
├── appl/
│   ├── models.py
│   ├── routes/
│   │   └── booking.py
│   ├── templates/
│   │   └── booking_public.html
│   ├── static/
```

## Note

- Tutte le chiavi e password devono essere gestite tramite variabili d'ambiente.