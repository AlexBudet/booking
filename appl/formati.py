# appl/formati.py
"""Come si scrivono nomi e cognomi nelle email e nei WhatsApp della prenotazione online.

Copia di nome_proprio() di SunBooking (appl/formati.py, controparte JavaScript
window.capitalizeName in calendar.js): le due app mostrano gli stessi clienti
degli stessi DB, quindi la regola deve restare identica nei due repo.
"""
import html


def nome_proprio(testo):
    """Nome o cognome con la maiuscola all'inizio di ogni parola.

    "maria grazia" -> "Maria Grazia", "D'ANGELO" -> "D'Angelo",
    "anna-maria" -> "Anna-Maria", "ÉLISE" -> "Élise".

    str.title() e non capitalize(): capitalize() guarda solo la prima lettera
    della stringa e lasciava "Maria grazia" e "D'angelo". title() riparte dopo
    spazio, apostrofo (anche quello tipografico) e trattino, e conosce le
    lettere accentate. Le entita' HTML (&#39;) arrivano dalla nota della
    prenotazione online, salvata con escape(), e vanno decodificate prima,
    se no "&#39;" resta nel testo. None e stringa vuota diventano ''.
    """
    if not testo:
        return ''
    return ' '.join(parola.title() for parola in html.unescape(str(testo)).split())
