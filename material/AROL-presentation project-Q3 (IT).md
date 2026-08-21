# Progetto Q3 — Agentic AI per l'analisi della telemetria su tappatrici AROL

Sintesi in italiano di `AROL-presentation project-Q3.pdf`.

---

## 1. Contesto e obiettivo

Costruire un'applicazione **AI agentica** che legge la telemetria delle tappatrici AROL e,
su richiesta dell'utente, produce **report di analisi ripetibili e spiegabili**.

Una tappatrice ha più **teste** (head): ogni testa avvita autonomamente un tappo su una bottiglia.

Le macchine espongono molte famiglie di dati — stato e allarmi, cadenza di produzione e cause di
fermo, indicatori OEE, consumi energetici, diagnostica di coppia/chiusura.
**Il progetto usa solo la diagnostica di coppia/chiusura.** Tutto il resto è fuori perimetro.

---

## 2. I dati

Formato: **una riga al secondo**, tre colonne per ogni testa (fino a 48 teste).

| timestamp | H01 Count | H01 AppTorque | H01 Status | H02 Count | H02 AppTorque | H02 Status | … |
|---|---|---|---|---|---|---|---|
| 2026-05-20T02:26:12Z | 23553 | 2.54 | 0 | 23499 | 2.51 | 0 | … |
| 2026-05-20T02:26:13Z | 23553 | 2.54 | 0 | 23500 | 1.83 | 64 | … |
| 2026-05-20T02:26:14Z | 23554 | 2.53 | 0 | 23500 | 1.83 | 64 | … |

- **Count** — contatore cumulato delle chiusure fatte da quella testa
- **AppTorque** — coppia applicata nell'ultima chiusura (Nm)
- **Status** — esito della chiusura: `0` = ok, altri valori = anomalia

**Punto chiave:** le righe sono *campionamenti*, non *eventi*. La stessa chiusura resta ripetuta
su più righe finché il contatore non avanza. Gli eventi vanno **ricostruiti** (deduplica),
altrimenti ogni conteggio è gonfiato.

---

## 3. Cosa costruire

### 3.1 Ingestion e normalizzazione

- Caricare uno o più *pool* di dataset (CSV / JSON / Parquet), normalizzarli in uno schema interno unico
- Validazione: valori mancanti, coerenza dei timestamp, metadati sulle unità di misura
- **Rilevare quali teste hanno chiuso**, confrontando il contatore con il record precedente
- Per ogni chiusura rilevata, raccogliere i campi collegati (coppia, stato)
- Calcolare il **timestamp di ogni chiusura**
- Calcolare la **velocità di tappatura** (pezzi/ora) con media incrementale

### 3.2 Analitiche di base (deterministiche)

Sono i *tool* affidabili che l'agente richiama:

- **Trend** sulla coppia: medie mobili, rilevamento di drift
- **Anomalie**: soglie e deviazioni statistiche
- **Correlazioni** tra segnali selezionati (es. testa 1 vs testa 5)
- **Macchina ferma**: condizione "No Load" prolungata su tutte le teste

### 3.3 Agente AI ("report agent")

- Interpreta la richiesta (es. *"report settimanale delle anomalie"*, *"spiega perché è aumentato il fermo macchina"*)
- **Decide quali tool eseguire e in che ordine**
- Produce un report strutturato:
  `obiettivo → dati usati → analisi eseguite → risultati → confidenza e limiti → verifiche successive`

### 3.4 Interfaccia bot

Scegliere **una** opzione, privilegiando robustezza e deployability:

- **CLI** (preferita, iterazione rapida): `report anomalies`, `report drift`, `report kpi`
- UI web minimale con download del report (opzionale)
- servizio locale con endpoint REST

### 3.5 Qualità ingegneristica

Dataset configurabili (**nessun path hard-coded**) · logging e gestione errori · test su almeno
un pool di dati · istruzioni di build/run documentate.

---

## 4. Consegne

- Repository del codice (struttura pulita, esecuzione riproducibile)
- Template di report (Markdown / HTML / export PDF) + report di esempio già generati
- Documentazione tecnica: architettura, schema dati, metodi analitici, flusso decisionale dell'agente
- **Demo end-to-end**: carica un pool di dati e genera almeno **due tipi diversi di report**
- Presentazione finale (10–15 slide)

## 5. Criteri di valutazione

Correttezza e robustezza dell'ingestion/normalizzazione · utilità delle analitiche (gestione dei
segnali, evidenza di drift/anomalie) · qualità agentica (flusso d'uso dei tool chiaro, output
spiegabili, errori gestiti con eleganza) · ingegneria del software (test, struttura,
documentazione, riproducibilità) · chiarezza della demo e comunicazione tecnica.

---

## 6. Domande che il bot deve reggere

**Esplorazione** — quante operazioni nel mese? quante chiusure per testa? intervallo temporale
coperto? valori di coppia mancanti o non validi?

**Qualità / tasso di successo** — percentuale di chiusure riuscite? quante fallite? tasso per
testa? quale testa ha il tasso più basso?

**Coppia** — coppia media delle riuscite? distribuzione? valori fuori dal range operativo atteso?
confronto riuscite vs fallite? quale testa ha la variabilità maggiore?

**Tempo e trend** — evoluzione del tasso di successo? spaccato giornaliero riuscite/fallite?
fasce orarie con fallimenti anomali? la coppia media è cambiata nel mese? correlazione tra ora
del giorno e probabilità di fallimento?

**Filtri condizionali** — chiusure fallite con coppia sotto soglia; quante sopra X Nm? tutti gli
eventi della testa 3 con esito negativo; conteggio riuscite dopo rimozione duplicati.

**Diagnostica comparativa** — quale testa si comporta diversamente? quale ha un numero anomalo di
fallimenti? confronto testa 1 vs testa 2? quale contribuisce di più ai fallimenti? coppia più
alta = più successo?

**Spiegazione (comportamento agentico)** — perché il tasso cala in certi giorni? perché la testa 4
fallisce di più? riassunto dei problemi principali? quali segnali monitorare più da vicino?

**Visualizzazione** — coppia nel tempo · istogramma della coppia · tasso di successo per testa ·
chiusure fallite nel tempo · dashboard riassuntiva.

**Meta / trasparenza** — quali preprocessing applicati? come sono stati rilevati i duplicati?
quali assunzioni nella pulizia dati? quali feature classificano una chiusura riuscita?

---

## 7. Output attesi

Pattern comune: **deduplica → filtra/raggruppa → aggrega → spiega a parole**.

**A. Qualità complessiva** — *"che percentuale di operazioni è riuscita nel periodo?"*
Conta solo eventi **unici**, classifica per stato.
`1.248.320 eventi · 1.164.870 riusciti · 83.450 falliti · 93,3% di successo`

**B. Confronto per testa** — *"tasso di successo di ogni testa?"*
Raggruppa per ID testa, calcola il tasso, **evidenzia gli scostamenti**.

| Testa | Chiusure totali | Riuscite | Tasso |
|---|---|---|---|
| Head 1 | 208.430 | 198.900 | 95,4% |
| Head 4 | 208.190 | 199.840 | 96,0% |
| Head 5 | 207.180 | 183.250 | 88,5% |

Spiegazione attesa dal bot — questo il tono di tutti i report:
> *"La testa 5 mostra un tasso di successo sensibilmente più basso delle altre e richiede approfondimento."*

**C. Statistiche di coppia** — *"coppia media delle operazioni riuscite?"*
Filtra `status = riuscito`, escludi duplicati, aggrega.
`media 2,41 Nm · min 1,85 · max 3,12 · dev.std 0,22` — servono tutte e quattro.

---

**Nota.** Non è un chatbot plug-and-play: richiede lavoro ingegneristico reale su dati, tooling e
affidabilità. Il valore sta nella ricostruzione corretta degli eventi e in analitiche
deterministiche solide; l'agente è orchestrazione sopra, non la sostanza.
