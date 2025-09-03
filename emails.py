import os
import json
from cryptography.fernet import Fernet
from email.message import EmailMessage
import smtplib
import logging

# Configura logging sicuro (solo errori generici, niente dati sensibili)
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Chiave segreta (da env - NON hardcodare!)
SECRET_KEY = os.environ.get('EMAIL_SECRET_KEY')
if not SECRET_KEY:
    raise ValueError("EMAIL_SECRET_KEY non impostata! Imposta la variabile d'ambiente.")
cipher = Fernet(SECRET_KEY.encode())

# Config SMTP TEMPORANEA con password in chiaro (NON IN PRODUZIONE!)
SMTP_CONFIGS = {
    1: {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": "suncityef80@gmail.com",
        "pass": "ghej vcqk gzlw lafe",  # Password per app in chiaro
        "use_ssl": False,
        "from_email": "suncityef80@gmail.com"
    },
    2: {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": "sunexpress3@gmail.com",
        "pass": "wfjn yqbw zkth txku",  # Password per app in chiaro
        "use_ssl": False,
        "from_email": "sunexpress3@gmail.com"
    }
}

def decrypt_payload(encrypted_payload):
    """Decritta il payload criptato e restituisce i dati. Gestisce errori senza esporre info."""
    try:
        decrypted = cipher.decrypt(encrypted_payload.encode())
        return json.loads(decrypted.decode())
    except Exception as e:
        logger.error(f"Errore decrittazione payload: {type(e).__name__} - possibile tampering")
        return None

def get_smtp_config_for_tenant(tenant_idx):
    """Restituisce config SMTP per tenant, o None se tenant non supportato."""
    try:
        tenant_idx = int(tenant_idx)  # Converti a intero
    except (ValueError, TypeError):
        logger.error(f"Tenant {tenant_idx} non valido (non numerico)")
        return None
    
    if tenant_idx not in SMTP_CONFIGS:
        logger.error(f"Tenant {tenant_idx} non supportato per SMTP")
        return None
    
    cfg = SMTP_CONFIGS[tenant_idx]
    # TEMPORANEO: Usa password in chiaro, senza decriptazione
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "user": cfg["user"],
        "pass": cfg["pass"],  # In chiaro
        "use_ssl": cfg["use_ssl"],
        "from_email": cfg["from_email"]
    }

def send_email_from_payload(encrypted_payload):
    """
    Decritta il payload, ottiene config SMTP per tenant e invia l'email.
    Payload atteso: {"tenant_idx": 1, "to_email": "...", "subject": "...", "html_content": "..."}
    """
    data = decrypt_payload(encrypted_payload)
    if not data:
        return False
    
    tenant_idx = data.get("tenant_idx")
    to_email = data.get("to_email")
    subject = data.get("subject")
    html_content = data.get("html_content")
    
    if not all([tenant_idx, to_email, subject, html_content]):
        logger.error("Payload incompleto per invio email")
        return False
    
    # Ottieni config SMTP sicura per tenant
    cfg = get_smtp_config_for_tenant(tenant_idx)
    if not cfg:
        return False
    
    smtp_host = cfg["host"]
    smtp_port = cfg["port"]
    smtp_user = cfg["user"]
    smtp_pass = cfg["pass"]
    smtp_use_ssl = cfg["use_ssl"]
    from_email = cfg["from_email"]
    
    if not all([smtp_host, smtp_user, smtp_pass]):
        logger.error("Config SMTP incompleta per tenant")
        return False
    
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_email
    msg['To'] = to_email
    msg.set_content(html_content, subtype='html')
    
    try:
        if smtp_use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as smtp:
                smtp.login(smtp_user, smtp_pass)
                smtp.send_message(msg, from_addr=smtp_user)
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as smtp:
                smtp.starttls()
                smtp.login(smtp_user, smtp_pass)
                smtp.send_message(msg, from_addr=smtp_user)
        logger.info(f"Email inviata con successo a {to_email} per tenant {tenant_idx}")
        return True
    except Exception as e:
        logger.error(f"Errore SMTP per tenant {tenant_idx}: {type(e).__name__} - invio fallito")
        return False