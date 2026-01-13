# 📧 Raccomandazioni Azure Email Rate Limiting

## Problema Identificato

Il codice attuale crea thread illimitati per l'invio email (`invia_email_async`), causando burst massivi quando arrivano molte prenotazioni contemporaneamente. Questo porta a errori **429 TooManyRequests** continui.

---

## ✅ Già Implementato

1. **Exponential backoff con jitter** (linea 246-260)
2. **Retry SDK disabilitato** (linea 145)
3. **Client singleton cached** (`_get_azure_email_client`)
4. **Delay conservativo** (65s > limite Azure di 60s)
5. **Queue WhatsApp** con rate limiting 1 msg/minuto

---

## ❌ Cosa Manca (CRITICO)

### 1. **Queue per Email con Rate Limiting**
**Problema:** Thread illimitati = burst incontrollato  
**Soluzione:** Implementare una coda email globale con worker single-threaded

```python
import queue
import threading

# Coda globale thread-safe
_EMAIL_QUEUE = queue.Queue()
_EMAIL_WORKER_STARTED = False
_EMAIL_LOCK = threading.Lock()

def _email_worker():
    """Worker che processa 1 email ogni 65 secondi"""
    while True:
        try:
            task = _EMAIL_QUEUE.get()
            if task is None:  # segnale di shutdown
                break
            
            to_email, subject, html_content, from_email, plain_text = task
            _send_email_sync(to_email, subject, html_content, from_email, plain_text)
            
            # Rate limiting: 1 email ogni 65 secondi
            time_module.sleep(65)
        except Exception as e:
            print(f"[EMAIL-WORKER] ERROR: {repr(e)}", flush=True)

def invia_email_async(to_email, subject, html_content, from_email=None, plain_text=None):
    """Accoda email invece di creare thread illimitati"""
    global _EMAIL_WORKER_STARTED
    
    # Avvia worker una sola volta
    with _EMAIL_LOCK:
        if not _EMAIL_WORKER_STARTED:
            worker = threading.Thread(target=_email_worker, daemon=True, name="EmailWorker")
            worker.start()
            _EMAIL_WORKER_STARTED = True
    
    # Accoda per invio sequenziale
    _EMAIL_QUEUE.put((to_email, subject, html_content, from_email, plain_text))
    return True
```

**Benefici:**
- ✅ Elimina burst: max 1 email/65s
- ✅ Ordine garantito (FIFO)
- ✅ Nessun race condition
- ✅ Backpressure automatica (la coda cresce ma non crasha)

---

### 2. **Lettura Header Retry-After**
**Problema:** Ignori il valore reale che Azure ti comunica  
**Soluzione:** Estrai e usa `Retry-After` dalla risposta HTTP

```python
except Exception as e:
    # Estrai Retry-After dall'exception Azure
    retry_after = None
    if hasattr(e, 'response') and e.response:
        retry_after = e.response.headers.get('Retry-After')
    
    if is_rate_limited:
        if retry_after:
            try:
                delay = int(retry_after) + 5  # +5s di buffer
                print(f"[EMAIL-AZURE] Retry-After: {delay}s", flush=True)
            except:
                delay = base_delay * (2 ** attempt)
        else:
            delay = base_delay * (2 ** attempt)
```

---

### 3. **Monitoring Strutturato (Application Insights)**

**Problema:** Log solo in console, nessuna metrica  
**Soluzione:** Traccia eventi 429 in Azure

```python
# Aggiungi a inizio file
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace

# In app startup (main.py)
configure_azure_monitor(
    connection_string=os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
)
tracer = trace.get_tracer(__name__)

# In _send_email_sync
with tracer.start_as_current_span("send_email") as span:
    span.set_attribute("to_email", to_email)
    span.set_attribute("attempt", attempt)
    
    if is_rate_limited:
        span.set_status(trace.StatusCode.ERROR, "Rate limited")
        span.record_exception(e)
```

Installa: `pip install azure-monitor-opentelemetry`

---

### 4. **Azure Service Bus (RACCOMANDATO per produzione)**

**Problema:** Queue in-memory = perdi email se il processo crasha  
**Soluzione:** Queue persistente con Azure Service Bus

```python
from azure.servicebus import ServiceBusClient, ServiceBusMessage

def invia_email_async(to_email, subject, html_content, from_email=None, plain_text=None):
    """Accoda su Azure Service Bus"""
    connection_str = os.environ.get('SERVICEBUS_CONNECTION_STRING')
    queue_name = "emails"
    
    with ServiceBusClient.from_connection_string(connection_str) as client:
        with client.get_queue_sender(queue_name) as sender:
            message = ServiceBusMessage(json.dumps({
                "to": to_email,
                "subject": subject,
                "html": html_content,
                "from": from_email,
                "plain_text": plain_text
            }))
            sender.send_messages(message)

# Worker separato (Azure Function o processo)
def process_email_queue():
    with ServiceBusClient.from_connection_string(connection_str) as client:
        with client.get_queue_receiver(queue_name) as receiver:
            for message in receiver:
                data = json.loads(str(message))
                _send_email_sync(**data)
                receiver.complete_message(message)
                time_module.sleep(65)  # rate limiting
```

**Benefici:**
- ✅ Persistenza (no perdita dati su crash)
- ✅ Scalabilità (worker multipli)
- ✅ Retry automatico con dead letter queue
- ✅ Monitoring nativo in Azure Portal

---

## 🎯 Piano di Implementazione (Priorità)

### **FASE 1 - IMMEDIATE (oggi)** ⚡
1. Implementa **Email Queue Worker** (30 min)
   - Sostituisci thread illimitati con queue + worker singolo
   - Rate limit fisso: 1 email/65s

### **FASE 2 - SHORT TERM (questa settimana)** 📅
2. Aggiungi **Retry-After parsing** (15 min)
3. Aggiungi **Application Insights** per monitoring (30 min)

### **FASE 3 - MEDIUM TERM (prossimo sprint)** 🚀
4. Migra a **Azure Service Bus** per queue persistente
5. Crea worker separato (Azure Function o Container)

---

## 💰 Aumentare i Limiti Azure

### Tier Azure Communication Services

| Tier | Limite | Costo |
|------|--------|-------|
| **Free** | 1 email/min | Gratis (primo anno) |
| **Standard** | 60 email/min | ~€0.10/1000 email |
| **Premium** | 300+ email/min | Custom pricing |

**Come fare upgrade:**
1. Azure Portal → Communication Services
2. Seleziona la risorsa
3. Settings → Pricing tier → Upgrade to Standard
4. Aggiungi carta di credito

⚠️ **ATTENZIONE:** Anche con upgrade, devi implementare rate limiting per evitare bollette surprise!

---

## 📊 Testing

```python
# Test della coda (aggiungi a booking.py per debug)
@booking_bp.route('/test_email_queue', methods=['GET'])
def test_email_queue():
    """Testa la coda inviando 10 email rapidamente"""
    for i in range(10):
        invia_email_async(
            to_email="test@example.com",
            subject=f"Test {i+1}",
            html_content=f"<p>Email di test {i+1}</p>"
        )
    return jsonify({
        "message": "10 email accodate",
        "queue_size": _EMAIL_QUEUE.qsize()
    })
```

---

## ✅ Checklist Finale

- [ ] Implementata Email Queue con worker single-threaded
- [ ] Rate limit: max 1 email/65s garantito
- [ ] Parsing header Retry-After
- [ ] Logging strutturato con timestamp
- [ ] Monitoring su Application Insights
- [ ] Testing con burst di email
- [ ] Upgrade tier Azure (se necessario)
- [ ] (Opzionale) Migrazione a Service Bus

---

## 📚 Risorse

- [Azure Communication Services Quotas](https://learn.microsoft.com/en-us/azure/communication-services/concepts/service-limits)
- [Exponential Backoff Best Practices](https://learn.microsoft.com/en-us/azure/architecture/best-practices/retry-service-specific)
- [Azure Service Bus Queues](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-queues-topics-subscriptions)
