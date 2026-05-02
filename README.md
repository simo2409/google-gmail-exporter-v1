# google-gmail-exporter-v1

Script Python che recupera email da Gmail tramite API e le salva localmente, organizzate per mittente.

## GitHub
https://github.com/simo2409/google-gmail-exporter-v1

## Come funziona

Le ricerche da eseguire sono definite in `config.json`. Per ogni ricerca lo script:

1. Cerca nella casella Gmail tutte le email del mittente indicato
2. Controlla quali email sono già presenti nella cartella di destinazione (skip automatico)
3. Scarica le email mancanti e le salva nel formato configurato

Per ogni email salvata con `save_html: true` vengono anche scaricate tutte le immagini presenti nell'HTML (URL esterni, allegati CID, data URI) e i riferimenti `src` vengono riscritti ai file locali.

### Struttura output

```
{output_path}/
  {msg_id}_{subject}.json          # metadati + body completo
  {msg_id}_{subject}/
      {msg_id}_{subject}.html      # HTML con immagini localizzate
      {hash}.jpg / .png / ...      # immagini scaricate
```

## Setup

### 1. Credenziali Google

Lo script cerca le credenziali OAuth in questo ordine:

1. **`credentials.json` nella directory dello script** (locale, priorità alta)
2. **`~/.config/llmwiki/obs-llmwiki-simone-personal-v1/credentials.json`** (condivisa con tutti i tool llmwiki, fallback)

Il token OAuth viene salvato accanto al file di credenziali usato:
- credenziali locali → `token.json` nella directory dello script
- credenziali condivise → `token-gmail.json` in `~/.config/llmwiki/obs-llmwiki-simone-personal-v1/`

Per ottenere le credenziali:

- Vai su [Google Cloud Console](https://console.cloud.google.com)
- Abilita la **Gmail API**
- Crea credenziali OAuth 2.0 (tipo: Desktop app)
- Scarica il JSON e salvalo nel percorso desiderato (crea la directory se non esiste)
- In **OAuth consent screen → Test users**, aggiungi il tuo account Gmail

### 2. Installazione dipendenze

```bash
uv sync
```

### 3. Prima esecuzione

```bash
uv run main.py
```

Al primo avvio si apre il browser per autorizzare l'accesso. Il token viene salvato accanto alle credenziali usate e riutilizzato nelle esecuzioni successive.

## Configurazione

Le ricerche sono definite in `config.json`:

```json
{
  "searches": [
    {
      "sender": "newsletter@example.com",
      "max_results": 500,
      "output_path": "emails",
      "save_json": true,
      "save_html": true
    }
  ]
}
```

| Parametro | Tipo | Descrizione |
|---|---|---|
| `sender` | string | Indirizzo email del mittente da cercare |
| `max_results` | int / null | Numero massimo di email da recuperare. `null` = nessun limite |
| `output_path` | string | Cartella di destinazione (relativa allo script o assoluta) |
| `save_json` | bool | Salva i metadati e il body in un file `.json` |
| `save_html` | bool | Crea una cartella con l'HTML e le immagini scaricate |

È possibile definire più ricerche nello stesso `config.json`: verranno eseguite in sequenza.

## File da non committare

```
credentials.json   # credenziali OAuth locali — non condividere
token.json         # token OAuth locale — non condividere
emails/            # email scaricate — dati personali
```

Le credenziali e il token condivisi risiedono in `~/.config/llmwiki/obs-llmwiki-simone-personal-v1/` e non fanno parte di questo repository.
