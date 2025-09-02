# Payload minimo per impostazioni SMTP per-tenant.
# NON committare password reali in questo file: usare tenant_settings_local.py (ignored) o secret manager.

TENANTS = {
    "negozio1": {
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": 587,
        "SMTP_USER": "suncityef80@gmail.com",
        "SMTP_PASS": "",               # riempire in tenant_settings_local.py o tramite secrets
        "SMTP_USE_SSL": False,
        "FROM_EMAIL": "info@negozio1.example"
    },
    "negozio2": {
        "SMTP_HOST": "smtp.partner.example",
        "SMTP_PORT": 465,
        "SMTP_USER": "partner@example",
        "SMTP_PASS": "",
        "SMTP_USE_SSL": True,
        "FROM_EMAIL": "info@negozio2.example"
    }
}

def get_tenant_settings(tenant_id):
    """
    Restituisce la dict di settings per tenant_id o None.
    tenant_id deve corrispondere alle chiavi in TENANTS (es. "negozio1").
    """
    if not tenant_id:
        return None
    return TENANTS.get(str(tenant_id))

# Permetti override locali non tracciati (crea appl/tenant_settings_local.py con TENANTS_LOCAL dict)
try:
    from .tenant_settings_local import TENANTS as TENANTS_LOCAL  # type: ignore
    if isinstance(TENANTS_LOCAL, dict):
        TENANTS.update(TENANTS_LOCAL)
except Exception:
    pass