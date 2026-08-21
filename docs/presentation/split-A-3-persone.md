# Divisione A — 3 persone, per tier tecnologico

Riferimento slide: [`outline.md`](outline.md) (13 slide).
Assegnazione: **P1 = 4, 6, 10 · P2 = 5, 11 · P3 = 7, 8, 9, 12 · corali = 1, 2, 3, 13.**

| | Ruolo | Slide | Minuti indicativi |
|---|---|---|---|
| P1 | Ingestion core + semantica del dato | 4, 6, 10 | ~7 |
| P2 | Runtime distribuito, performance, evidenza sperimentale | 5, 11 | ~5 |
| P3 | Analytics, agente, BOT, limiti | 7, 8, 9, 12 | ~8 |

Demo (slide 13): parla P3, guida il terminale P2.

---

## 0. Base comune — tutti e tre

Da sapere a memoria, indipendentemente dal ruolo. Chiunque deve poter rispondere.

| Fatto | Fonte |
|---|---|
| Input: 89 CSV giornalieri, ~86.400 righe × 109 colonne/giorno, ~1,6 GB/mese **non** compressi (gli zip mensili sono 26–42 MB), 36 teste @1 Hz | `README.md` § Problem Statement |
| Il PLC riporta **stato, non eventi**; ~24,5% righe sono duplicati consecutivi esatti | idem |
| Una chiusura si **ricostruisce dal delta del contatore per testa** | `README.md` § Core Domain |
| `status` è **bitmask**, non enum: reject ⇔ bit 0 impostato (`status % 2 != 0` — la forma `!= 0` copre anche status negativi) | `README.md` § Status Semantics |
| Store unico `cap_events` in DuckDB, chiave `UNIQUE(machine_id, head_id, ts)` — **non** `cap_seq`: il contatore PLC rigioca valori dopo il reset, e la vecchia chiave scartava chiusure reali | `README.md` § Database Design |
| 55.132.433 eventi su 3 mesi · 1.096 reject · head 29 = 117 reject | outline slide 6, 10 |
| Invariante finale: **il modello sceglie le analisi, l'SQL produce ogni numero** | `docs/agent-decision-flow.md` |

Slide 1–3 e 13: script condiviso, chiunque le può dire.

---

## P1 — Ingestion core + semantica del dato

**Possiede:** trasformata di dedup, `CapEvent`, `Pipeline`, lettura CSV, i due backend di store (DuckDB e Parquet), porting CUDA, semantica `status`, il finding su head 29.

### File in ordine di lettura

1. [`core/include/mas/domain/CapEvent.hpp`](../../core/include/mas/domain/CapEvent.hpp) — i 9 campi, `is_reject`
2. [`core/src/domain/CapEventExtractor.cpp`](../../core/src/domain/CapEventExtractor.cpp) — le 4 transizioni
3. [`core/include/mas/domain/CapEventExtractorFlat.hpp`](../../core/include/mas/domain/CapEventExtractorFlat.hpp) — la stessa trasformata element-wise
4. [`core/include/mas/domain/DeltaPolicy.hpp`](../../core/include/mas/domain/DeltaPolicy.hpp) — `saturated_delta()`, unica definizione `__host__ __device__` del delta: CPU, GPU e oracolo non possono divergere
5. [`core/include/mas/domain/RowParse.hpp`](../../core/include/mas/domain/RowParse.hpp) + [`RowParse.cpp`](../../core/src/domain/RowParse.cpp) — unica policy di validità della riga per entrambi i loader (CSV reader e path CUDA): strip del CR finale, numero di campi, range check per cella
6. [`core/src/store/CsvRawReader.cpp`](../../core/src/store/CsvRawReader.cpp) — parsing, header validation
7. [`core/src/store/DuckDbEventStore.cpp`](../../core/src/store/DuckDbEventStore.cpp) — schema, staging+merge, upsert idempotente, PIMPL, rifiuto all'apertura di uno store con la chiave vecchia
8. [`core/src/store/ParquetEventStore.cpp`](../../core/src/store/ParquetEventStore.cpp) + [`AtomicPublish.hpp`](../../core/include/mas/store/AtomicPublish.hpp) — il secondo backend (`clean --format parquet`, `mas_export`) e la pubblicazione senza finestra in cui si veda un file parziale
9. [`core/cuda/CudaCleaner.cu`](../../core/cuda/CudaCleaner.cu) — 8 stage GPU, `--verify`
10. Test: `tests/test_cap_event.cpp`, `test_cap_event_extractor.cpp`, `test_cap_event_extractor_flat.cpp`, `test_duckdb_event_store.cpp`
11. `README.md` § Core Domain, § Status Semantics, § Database Design
12. `python/oracle.py`, `python/validate_real.py` — l'oracolo indipendente che valida P1

### Concetti da padroneggiare

- Le 4 transizioni: increment (delta 1) · aggregated (delta >1, carosello avanzato) · reset (`c < last`) · held (nessun evento).
- **Perché la trasformata è element-wise e non uno scan**: ogni ramo di `process()` termina con `last = c`, quindi lo stato non risale mai oltre una riga. È la precondizione di CUDA e della versione `numpy.diff`.
- Perché l'estrattore stateful resta accanto a `extract_flat`: il flat serve come **oracolo** del port GPU, non come sostituto.
- Idempotenza: `INSERT OR IGNORE` + chiave UNIQUE ⇒ rilanciare un day-file non raddoppia mai.
- PIMPL su `DuckDbEventStore`: `duckdb.hpp` non entra in nessun'altra TU.
- Bitmask: `65 = 64+1` (Bad Closure + reject), `9 = 8+1` (No InTorque + reject). La vecchia regola `status == 65` **sottocontava**: febbraio 748 reject, non 732.
- Il tasso di successo **esclude i cicli No-Load**: una testa che ha solo ciclato a vuoto ha fatto zero capping, non 0%.
- Parsing float correttamente arrotondato ovunque: pandas e un parse GPU naïf sbagliano di 1 ulp su valori tipo `2.002`.

### Domande probabili

| Domanda | Risposta breve |
|---|---|
| Perché non usate una colonna "cap applicato"? | Non esiste. Il PLC pubblica stato; l'evento è ricostruito dal delta contatore. |
| Cosa succede se il contatore si azzera? | Ramo reset: un evento `reset=true, delta=0`. Nel mese reale il reset a metà giorno 16 fa rigiocare cap_seq già visti — sono **chiusure fisiche distinte** (18.721 delle collisioni di testa 1 portano torque diversa). La vecchia chiave su `cap_seq` le scartava (21.872.663 eventi → 14.372.237 righe, 34% di febbraio perso); per questo l'identità oggi è `ts` — una testa chiude al più una volta per poll — e lo store rifiuta all'apertura uno store con la chiave vecchia. |
| Come sapete che il bitmask è giusto? | Confermato dai dati: 1.071 + 24 + 1 = 1.096 reject, esattamente ciò che ritorna la regola "dispari". |
| Come provate che la GPU non sbaglia? | `mas_cuda_clean --verify` fa il differenziale bitwise contro `extract_flat`, esce non-zero e stampa i primi 10 eventi divergenti con tutti e 9 i campi. |

---

## P2 — Runtime distribuito, performance, evidenza

**Possiede:** agenti, ZeroMQ, protocollo di liveness, monolith multithread, sweep di benchmark, chaos E2E, suite di test.

### File in ordine di lettura

1. [`core/src/agent/Message.cpp`](../../core/src/agent/Message.cpp) — i 6 frame: WorkItem / WorkResult / Heartbeat / **CLAIM** / **BYE** / STOP
2. [`core/src/transport/ZmqTransport.cpp`](../../core/src/transport/ZmqTransport.cpp) — PUSH/PULL, timeout, linger **per socket** (HB 0, sink dei risultati 300 ms)
3. [`core/src/agent/CleaningWorker.cpp`](../../core/src/agent/CleaningWorker.cpp) — contratto di heartbeat, CLAIM alla presa in carico, BYE all'uscita, idle-exit
4. [`core/include/mas/store/BeatingStore.hpp`](../../core/include/mas/store/BeatingStore.hpp) — il battito che continua *durante* `clean_file()`
5. [`core/src/agent/Coordinator.cpp`](../../core/src/agent/Coordinator.cpp) — gate di registrazione, loop a fasi, death sweep, re-dispatch per holder
6. [`core/src/apps/monolith_main.cpp`](../../core/src/apps/monolith_main.cpp) — thread pool con contatore atomico, store per-thread
7. [`scripts/chaos_e2e.sh`](../../scripts/chaos_e2e.sh), [`bench/run_bench.sh`](../../bench/run_bench.sh)
8. [`docs/bench/results.md`](../bench/results.md) — la tabella e i caveat
9. Test: `test_coordinator.cpp`, `test_cleaning_worker.cpp`, `test_zmq_transport.cpp`, `test_message.cpp`, `tests/fakes/FakeTransport.hpp`
10. `README.md` § Resilience, § Distributed Processing Flow, § Testing, § Design Decisions

### Concetti da padroneggiare

- **3 endpoint** separati (work 5591 / results 5592 / heartbeat 5593): il canale HB si drena senza bloccare, quindi la liveness non compete con i risultati.
- **CLAIM e BYE** (`03df3ca`, commit marcato breaking) sono i due frame che rendono onesta la resilienza. Il socket di lavoro è PUSH anonimo round-robin: senza CLAIM il coordinator non sa **chi tiene cosa**, e ogni morte forzava il re-dispatch di *tutti* gli item aperti, consumando il cap per item su morti che non c'entravano. BYE distingue l'uscita volontaria per idle dalla morte: store intatto, risultati contati, niente riaperto — senza quel frame tre uscite ordinarie facevano fallire il run.
- Contratto worker: hello HB all'ingresso · 1 HB per tick vuoto (1 s) · 1 HB dopo ogni WorkResult · **1 HB al più ogni `kBeatEvery` = 1 s anche mentre pulisce**, tramite `BeatingStore` più il callback passato dentro `clean_fn` (così batte anche il path parquet, che porta il proprio store). **Non esiste più una finestra silente**: 30 s di silenzio significano davvero morto o bloccato.
- Loop coordinator, **prima** il gate di registrazione: con `--workers N` (`expected_workers`) il primo dispatch aspetta che N worker si siano registrati (hello HB o risultato), fino a `registration_timeout` = 10 s — poi parte degradato se almeno uno si è fatto vivo, aborta se nessuno. **Poi** il tick: un frame di lifecycle per giro (un risultato **o** un CLAIM **o** un BYE, timeout 200 ms sul sink dei risultati che dà il ritmo) → drain HB → death sweep (soglia 30 s) → abort check.
- **Re-dispatch per holder, non a tappeto**: a una morte ripartono gli item *claimed* dal morto, e quelli pagano il cap (2 rinvii oltre il primo); le sue completions riaperte e gli item mai reclamati ripartono **senza** pagare, e gli item in mano a worker vivi non si toccano. Superato il cap l'item è fallito in modo permanente — protezione poison-item.
- **Write-off dello store del worker morto**: le sue righe si ricreano negli store dei superstiti, l'upsert idempotente assorbe la sovrapposizione, `mas_merge` salta gli store corrotti.
- Linger **per socket**, non uno globale: heartbeat a `linger_ms=0` (liveness fire-and-forget, senza valore a processo finito — è ciò che tiene il teardown dell'orfano dentro il budget, il linger accoppiato a 60 s dava 121 s, trovato dal chaos E2E), sink dei risultati a `kResultSinkLingerMs` = **300 ms** perché il BYE viene spinto e il socket chiuso subito dopo: a linger 0 quel frame si perde e il coordinator legge una partenza annunciata come una morte.
- `ClockFn` iniettabile + `FakeTickSource`: i test di liveness avanzano un orologio finto, **zero sleep**.
- Perché store per-worker: **DuckDB è single-writer**. Da qui discende tutto il resto, compreso il collo di bottiglia.
- Il gate sulla registrazione non è comodità: senza, lo slow-joiner di ZMQ PUSH accodava tutti i file al primo worker connesso e lo sweep misurava una pipeline serializzata (sweep #1, scartato).

### Numeri a memoria (28 giorni, mediana di 3)

| | tempo |
|---|---|
| mono-1T | 537,8 s |
| mono-MT T=8 | 157,3 s (clean 86,0 s + merge 71,3 s) |
| MAS N=8 | 182,6 s totale (clean 111,9 s + **merge 70,7 s**) |
| MAS N=16 | 140,4 s (clean 75,6 s + merge 64,8 s) |

- La **fase clean scala bene**: 537,8 s → 74,9 s = **7,2×**.
- Il **merge no**: 65–73 s, e a differenza di prima è *piatto* in N. End-to-end il MAS arriva a **3,83×**: è il merge seriale a separare 7,2× da 3,83×, non un difetto di scaling.
- mono-MT **batte** mono-1T a scala mensile: T=8 fa 3,42×. Resta sotto MAS N=16 (3,83×).
- Attenzione al confronto con le slide vecchie: questi numeri vengono dal resweep su i7-13700H raffreddato attivamente. Il vecchio 1,11× era il rapporto misurato su M2, dove mono-1T faceva 101,8 s; la baseline si muove di 5,28× fra le due macchine, il mix di costo no.
- Sweep: 1/7/28 giorni × architetture × 3 ripetizioni = **81/81 run oracle-exact**.
- Test: **203 C++** (21 file GTest) + **306 Python**. In `MAS_BENCH_ONLY=ON` **non viene costruito nessun test**: la suite tira googletest dalla rete e il contratto della build bench è "non scarica niente".

### Domande probabili

| Domanda | Risposta breve |
|---|---|
| Perché aggiungere worker aiuta sempre meno? | Il merge è seriale ed è *piatto* in N (65–73 s): non cresce più col numero di store — quello era il vecchio difetto — ma resta un costo fisso. Legge di Amdahl sulla porzione di unificazione: clean 7,2×, end-to-end 3,83×. Fix noto (Parquet partizionato o store multi-writer) = roadmap, non fatto. |
| Come rilevate un worker morto senza falsi positivi? | Silenzio > 30 s con HB su canale dedicato non bloccante. Il worker batte a vuoto, dopo ogni risultato **e durante la pulizia** (`BeatingStore`, al più ogni secondo), quindi non esiste un silenzio legittimo: 30 s di nulla significano morto o bloccato. |
| E se un worker esce da solo perché non arriva più lavoro? | Manda un **BYE**. È una partenza, non una morte: nessuna completion riaperta, nessun re-dispatch, `workers_died` non si gonfia. Perché quel frame esca serve un linger reale sul sink dei risultati (300 ms). |
| Alla morte di un worker rilanciate tutto? | No, solo ciò che quel worker aveva **claimed** — il CLAIM parte sul socket dei risultati nel momento in cui l'item è preso in carico. Quelli pagano il cap di 2 rinvii; completions riaperte e item mai reclamati ripartono senza pagare; quelli in mano a worker vivi restano dove sono. |
| Un re-dispatch può duplicare righe? | No: upsert idempotente su `UNIQUE(machine_id, head_id, ts)`. |
| Come testate la morte senza aspettare 30 s reali? | `ClockFn` iniettabile: i test avanzano il clock. Il chaos E2E invece è reale (SIGKILL a un worker, e coordinator morto con worker orfano). |

---

## P3 — Analytics, agente, BOT, limiti

**Possiede:** gli 8 tool, `ToolResult` e provenance, planner/registry/router/narrator, CLI `arol`, rendering report, i report committati e i limiti onesti (slide 12).

### File in ordine di lettura

1. [`python/analytics/result.py`](../../python/analytics/result.py) — `ToolResult`, gli stati `ok`/`insufficient_data`/`error`
2. [`python/analytics/status.py`](../../python/analytics/status.py) — `REJECT_SQL`, single source con `CapEvent::is_reject`
3. [`python/analytics/store.py`](../../python/analytics/store.py), [`config.py`](../../python/analytics/config.py) — WP5, niente hard-coded: `mad_k`, `mad_floor`, `idle_min_seconds`, `idle_max_gap_seconds`, `max_anomaly_items`, `narrator_max_items`
4. `python/analytics/tools/*.py` — gli 8 tool, firma uniforme `(Config, **kwargs) -> ToolResult`
5. [`python/analytics/agent/registry.py`](../../python/analytics/agent/registry.py) — `plan_json_schema()`, `validate_step()`
6. [`python/analytics/agent/planner.py`](../../python/analytics/agent/planner.py), [`router.py`](../../python/analytics/agent/router.py), [`executor.py`](../../python/analytics/agent/executor.py), [`narrator.py`](../../python/analytics/agent/narrator.py)
7. [`python/analytics/cli.py`](../../python/analytics/cli.py) — i 4 comandi, exit code
8. [`python/analytics/report/render.py`](../../python/analytics/report/render.py), [`plots.py`](../../python/analytics/report/plots.py), [`export.py`](../../python/analytics/report/export.py)
9. [`docs/agent-decision-flow.md`](../agent-decision-flow.md) ← **la slide 8 è questo diagramma**
10. [`docs/reports/ask-live-sample/`](../reports/ask-live-sample/) — il run `ask` committato, col suo `trace.json`; [`docs/reports/README.md`](../reports/README.md) per la tabella di staleness (oggi vuota)
11. `python/tests/test_anthropic_schema_live.py` (gate sulla chiave) e `python/tests/test_backend_parity.py` (DuckDB vs Parquet) — le due evidenze che la slide 12 cita
12. [`docs/analytics-methods.md`](../analytics-methods.md), [`docs/reports/`](../reports/), `docs/validation-log.md`

### Concetti da padroneggiare

- Gli 8 tool: `overview`, `success_rates`, `torque_stats`, `capping_speed`, `idle_periods`, `anomalies`, `trend`, `head_correlation`. Funzioni pure, SQL parametrizzato in, risultato tipizzato out.
- **Provenance non decorativa**: `ToolResult` porta stato, valori, periodo, righe scansionate, filtri, assunzioni — e alimenta la sezione *Confidence and limits*. Serve a non far sembrare uguali "100%" e "100% di quattro chiusure".
- **Sotto la CLI nulla solleva eccezioni**: un buco nei dati è uno `status`, non un throw. È ciò che permette a un run non presidiato di produrre comunque un report.
- I 3 vincoli sul planner: **structured outputs** (schema JSON generato dal registry stesso, quindi non può divergere) · **validazione registry** (tool inventato, argomento inesistente, valore fuori range = rifiutato) · **router keyword di fallback** (no key, no rete, rifiuto, JSON malformato, step invalido) con il motivo stampato nei limiti.
- Dettaglio Anthropic: structured outputs richiede `additionalProperties: false` e tutte le property in `required` ⇒ gli argomenti sono un unico oggetto piatto con l'unione dei parametri e i non usati a `null`; `plan.effective_args()` li scarta, e validazione/esecuzione/scelta figure leggono lo step **attraverso quella stessa funzione**, così non possono discordare.
- I 3 verbi `report` **non contengono modello**: piano fisso in `router.py`. Sono il path riproducibile della demo, il fallback offline e il riferimento contro cui si controlla il path agentico.
- Politica di fallimento: problema di **config** ⇒ exit 2 prima di qualsiasi lavoro; buco di **analisi** ⇒ report che nomina il buco.
- Il finding (slide 10, la presenta P1, ma i numeri escono dai tuoi tool): 99,9950% a livello macchina nasconde head 29 con 117 reject su 1.095, contro media per testa 30,4 e 78 della seconda peggiore (head 35).
- **Il non-trovato conta**, ma con l'ambito giusto: nessuna testa supera la soglia Mann-Kendall su coppia o success rate, e tutte e 36 correlano > 0,9999 sulla coppia media — cioè **nessuna è fuori passo nella forma**. Sul **livello** non dice nulla: Pearson è invariante a un offset per testa, quindi una testa che gira stabilmente più bassa muovendosi con le altre prende ~1 ed è riportata come allineata. Il controllo che lo escluderebbe è la mediana per testa (`torque_stats by head`), e il report lo scrive fra i next check. Una versione precedente nominava sempre una "testa meno correlata" — aritmetica vera, conclusione falsa.
- **Il path agentico live è provato su un modello locale, non su quello hosted** (slide 12): [`docs/reports/ask-live-sample/`](../reports/ask-live-sample/) è un run `ask` committato su qwen2.5:7b sotto Ollama — plan source `llm`, **uno** step validato dal registry (`head_correlation(by='day')`), executor → renderer end-to-end sullo store vero. Resta non verificata solo l'**accettazione dello schema da parte dell'API Anthropic**: nessuna chiave è mai stata usata, e `test_anthropic_schema_live.py` manda gli schemi ed è gated proprio su quella.
- **Il 7B pianifica ma non narra**: ogni narrazione che ha prodotto è stata respinta dal rilevatore di bullet e sostituita dal riassunto deterministico, col motivo stampato nei limiti — 3 su 3 a luglio, 2 su 2 ad agosto, e nel run committato si legge "it announced findings rather than stating them". La prosa del campione è quella del template.
- **CSV è solo l'ingestion grezza, non lo store**: `clean --format parquet` scrive uno store Parquet, `mas_export` lo esporta, e gli stessi 8 tool leggono entrambi i backend (`test_backend_parity.py`). Leggere JSON o Parquet *come telemetria grezza* è un fratello del reader, non lavoro d'agente.
- **I report committati sono stati rigenerati il 2026-08-19** su uno store ricostruito dai tre zip mensili (55.132.433 righe, fingerprint identico a quello registrato). Due numeri si sono mossi e vanno saputi spiegare: un **buco nei dati ora chiude una run di idle** (`6e1b9be`, knob `idle_max_gap_seconds` = 600 s) — febbraio passa da 11.551,3 a 7.228,1 head-hours mentre i periodi *salgono* da 22.459 a 25.046, che è esattamente ciò che implica spezzare le run sui buchi; e il **floor sulla scala di deviazione** (`mad_floor` = 0,01 Nm) porta le anomalie di febbraio da 1.612.634 a 162.019 hit, cioè da una banda robusta che segnalava ~10,9% del mese a ~1,1%.
- Il campione itemizzato delle anomalie copre **tutto il periodo** e non le sue prime ore; cap a `max_anomaly_items` = 5000, col totale ricalcolato via `COUNT(*)` esatto solo quando il campione si riempie davvero.

### Domande probabili

| Domanda | Risposta breve |
|---|---|
| L'LLM calcola qualche numero? | No. Sceglie i tool e scrive la prosa attorno ai risultati. Nessun accesso allo store, nessun ruolo aritmetico. |
| Se l'API è giù? | Router keyword, report comunque prodotto, motivo scritto nella sezione limiti. I 3 verbi `report` non chiamano mai il modello. |
| Come testate l'agente senza token? | Client API finto iniettato: ogni path di fallimento di planner e narrator è pinnato. Zero rete, zero chiavi. |
| Cosa non è coperto? | Solo l'**accettazione dello schema da parte dell'API Anthropic**: nessuna chiave è mai stata usata, `test_anthropic_schema_live.py` manda gli schemi ed è gated su quella. Il path `ask` live **ha** evidenza committata su modello locale (`docs/reports/ask-live-sample/`, qwen2.5:7b), e il fallback router pure. |
| Il modello scrive davvero la prosa? | Sul 7B no: il rilevatore di bullet ha respinto ogni sua narrazione (3 su 3 a luglio, 2 su 2 ad agosto) e il report è caduto sul riassunto deterministico, dicendolo nei limiti. È il degrado previsto — la narrazione perde leggibilità, i numeri no. |
| Perché il tempo di idle è calato del 37% rispetto ai report vecchi? | Perché un buco nei dati ora chiude la run invece di essere contato come idle (`idle_max_gap_seconds` = 600 s): febbraio 11.551,3 → 7.228,1 head-hours, coi periodi che *salgono* a 25.046. Lo store non ha righe per una macchina spenta, quindi una run illimitata riportava il fermo come idling. |
| Perché le anomalie di febbraio sono passate da 1,6 M a 162 k? | `mad_floor` = 0,01 Nm. Un sensore quantizzato collassava la banda robusta a rumore e la banda finiva per segnalare ~10,9% del mese; col floor scende a ~1,1%. |

---

## Metodo di lavoro (3 sessioni)

1. **Sessione 1 (1 h, insieme)** — base comune sopra + giro dei 13 slide, ognuno prende i suoi.
2. **Sessione 2 (individuale)** — leggere i propri file nell'ordine dato, scrivere le proprie slide, preparare **2 domande trappola** per gli altri due.
3. **Sessione 3 (1,5 h, insieme)** — prova completa cronometrata, poi incrocio: P1 interroga P3, P3 interroga P2, P2 interroga P1. Chi non sa una risposta si segna il file da rileggere.

**Handoff da provare a voce** (sono i punti dove si inciampa):

- P1 → P2: "…ricostruito l'evento. Ora: quanto costa farlo su 89 file, e cosa succede se un processo muore."
- P2 → P3: "…lo store è unico e affidabile. Cosa ci si chiede sopra."
- P3 → chiusura: "il modello ha scelto le analisi; l'SQL ha prodotto ogni numero."
