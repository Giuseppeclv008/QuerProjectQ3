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
| Input: 89 CSV giornalieri, ~86.400 righe × 109 colonne/giorno, ~1,6 GB/mese zippati, 36 teste @1 Hz | `README.md` § Problem Statement |
| Il PLC riporta **stato, non eventi**; ~24,5% righe sono duplicati consecutivi esatti | idem |
| Una chiusura si **ricostruisce dal delta del contatore per testa** | `README.md` § Core Domain |
| `status` è **bitmask**, non enum: reject ⇔ status dispari (`status % 2 = 1`) | `README.md` § Status Semantics |
| Store unico `cap_events` in DuckDB, chiave `UNIQUE(machine_id, head_id, cap_seq)` | `README.md` § Database Design |
| 55.132.433 eventi su 3 mesi · 1.096 reject · head 29 = 117 reject | outline slide 6, 10 |
| Invariante finale: **il modello sceglie le analisi, l'SQL produce ogni numero** | `docs/agent-decision-flow.md` |

Slide 1–3 e 13: script condiviso, chiunque le può dire.

---

## P1 — Ingestion core + semantica del dato

**Possiede:** trasformata di dedup, `CapEvent`, `Pipeline`, lettura CSV, store DuckDB, porting CUDA, semantica `status`, il finding su head 29.

### File in ordine di lettura

1. [`core/include/mas/domain/CapEvent.hpp`](../../core/include/mas/domain/CapEvent.hpp) — i 9 campi, `is_reject`
2. [`core/src/domain/CapEventExtractor.cpp`](../../core/src/domain/CapEventExtractor.cpp) — le 4 transizioni
3. [`core/include/mas/domain/CapEventExtractorFlat.hpp`](../../core/include/mas/domain/CapEventExtractorFlat.hpp) — la stessa trasformata element-wise
4. [`core/src/store/CsvRawReader.cpp`](../../core/src/store/CsvRawReader.cpp) — parsing, header validation
5. [`core/src/store/DuckDbEventStore.cpp`](../../core/src/store/DuckDbEventStore.cpp) — schema, staging+merge, upsert idempotente, PIMPL
6. [`core/cuda/CudaCleaner.cu`](../../core/cuda/CudaCleaner.cu) — 7 stage GPU, `--verify`
7. Test: `tests/test_cap_event.cpp`, `test_cap_event_extractor.cpp`, `test_cap_event_extractor_flat.cpp`, `test_duckdb_event_store.cpp`
8. `README.md` § Core Domain, § Status Semantics, § Database Design
9. `python/oracle.py`, `python/validate_real.py` — l'oracolo indipendente che valida P1

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
| Cosa succede se il contatore si azzera? | Ramo reset: un evento `reset=true, delta=0`. Nel mese reale il reset a metà giorno 16 fa rigiocare cap_seq già visti — la UNIQUE li dedupa (21.872.663 processati → 14.372.237 righe distinte su 28 giorni). |
| Come sapete che il bitmask è giusto? | Confermato dai dati: 1.071 + 24 + 1 = 1.096 reject, esattamente ciò che ritorna la regola "dispari". |
| Come provate che la GPU non sbaglia? | `mas_cuda_clean --verify` fa il differenziale bitwise contro `extract_flat`, esce non-zero e stampa i primi 10 eventi divergenti con tutti e 9 i campi. |

---

## P2 — Runtime distribuito, performance, evidenza

**Possiede:** agenti, ZeroMQ, protocollo di liveness, monolith multithread, sweep di benchmark, chaos E2E, suite di test.

### File in ordine di lettura

1. [`core/src/agent/Message.cpp`](../../core/src/agent/Message.cpp) — WorkItem / WorkResult / Heartbeat / STOP
2. [`core/src/transport/ZmqTransport.cpp`](../../core/src/transport/ZmqTransport.cpp) — PUSH/PULL, timeout, `linger_ms=0`
3. [`core/src/agent/CleaningWorker.cpp`](../../core/src/agent/CleaningWorker.cpp) — contratto di heartbeat, idle-exit
4. [`core/src/agent/Coordinator.cpp`](../../core/src/agent/Coordinator.cpp) — loop a 4 fasi, death sweep, re-dispatch
5. [`core/src/apps/monolith_main.cpp`](../../core/src/apps/monolith_main.cpp) — thread pool con contatore atomico, store per-thread
6. [`scripts/chaos_e2e.sh`](../../scripts/chaos_e2e.sh), [`bench/run_bench.sh`](../../bench/run_bench.sh)
7. [`docs/bench/results.md`](../bench/results.md) — la tabella e i caveat
8. Test: `test_coordinator.cpp`, `test_cleaning_worker.cpp`, `test_zmq_transport.cpp`, `test_message.cpp`, `tests/fakes/FakeTransport.hpp`
9. `README.md` § Resilience, § Distributed Processing Flow, § Testing, § Design Decisions

### Concetti da padroneggiare

- **3 endpoint** separati (work 5591 / results 5592 / heartbeat 5593): il canale HB si drena senza bloccare, quindi la liveness non compete con i risultati.
- Contratto worker: hello HB all'ingresso · 1 HB per tick vuoto (1 s) · 1 HB dopo ogni WorkResult. **Unica finestra silente = durante `clean_file()`.**
- Loop coordinator a 4 fasi: result tick (timeout 200 ms, dà il ritmo) → drain HB → death sweep (soglia 30 s, re-dispatch cap 2) → abort check.
- **Write-off dello store del worker morto**: i suoi item tornano aperti e vanno ai superstiti; l'upsert idempotente assorbe la sovrapposizione, `mas_merge` salta gli store corrotti.
- `linger_ms=0`: le PUSH in connect creano la pipe subito e accodano sotto HWM anche senza peer — il linger infinito causava un teardown da 121 s, trovato dal chaos E2E.
- `ClockFn` iniettabile + `FakeTickSource`: i test di liveness avanzano un orologio finto, **zero sleep**.
- Perché store per-worker: **DuckDB è single-writer**. Da qui discende tutto il resto, compreso il collo di bottiglia.
- Gate sulla registrazione dei worker (`--workers N`): senza, lo slow-joiner di ZMQ PUSH accodava tutti i file al primo worker connesso e lo sweep misurava una pipeline serializzata (sweep #1, scartato).

### Numeri a memoria (28 giorni, mediana di 3)

| | tempo |
|---|---|
| mono-1T | 101,0 s |
| MAS N=8 | 92,6 s totale (clean 29,8 s + **merge 62,8 s**) |
| MAS N=16 | 91,2 s (clean 27,1 s + merge 64,0 s) |

- La **fase clean scala bene**: 101,0 s → 27,1 s = **3,73×**.
- Il **merge no**: 63–65 s, e a differenza di prima è *piatto* in N. End-to-end il MAS si ferma a **1,11×**. Il branch `perf/merge-set-based` lo porta a 22,8 s misurati in isolamento (2,89×).
- mono-MT non batte mai davvero mono-1T a scala mensile (meglio 1,01× a T=4; T=2 è più lento).
- Sweep: 1/7/28 giorni × architetture × 3 ripetizioni = **81/81 run oracle-exact**.
- Test: **90 C++** (14 file GTest) + **229 Python**. In `MAS_BENCH_ONLY=ON` restano 34 test C++ — esclusi per design, non skippati.

### Domande probabili

| Domanda | Risposta breve |
|---|---|
| Perché aggiungere worker non aiuta? | Il merge è seriale e cresce col numero di store. Legge di Amdahl sulla porzione di unificazione. Fix noto (Parquet partizionato o store multi-writer) = roadmap, non fatto. |
| Come rilevate un worker morto senza falsi positivi? | Silenzio > 30 s con HB su canale dedicato non bloccante; il worker batte anche a vuoto, quindi il silenzio significa davvero morto o bloccato in `clean_file()`. |
| Un re-dispatch può duplicare righe? | No: upsert idempotente su `UNIQUE(machine_id, head_id, cap_seq)`. |
| Come testate la morte senza aspettare 30 s reali? | `ClockFn` iniettabile: i test avanzano il clock. Il chaos E2E invece è reale (SIGKILL a un worker, e coordinator morto con worker orfano). |

---

## P3 — Analytics, agente, BOT, limiti

**Possiede:** gli 8 tool, `ToolResult` e provenance, planner/registry/router/narrator, CLI `arol`, rendering report, limiti onesti.

### File in ordine di lettura

1. [`python/analytics/result.py`](../../python/analytics/result.py) — `ToolResult`, gli stati `ok`/`insufficient_data`/`error`
2. [`python/analytics/status.py`](../../python/analytics/status.py) — `REJECT_SQL`, single source con `CapEvent::is_reject`
3. [`python/analytics/store.py`](../../python/analytics/store.py), [`config.py`](../../python/analytics/config.py) — WP5, niente hard-coded
4. `python/analytics/tools/*.py` — gli 8 tool, firma uniforme `(Config, **kwargs) -> ToolResult`
5. [`python/analytics/agent/registry.py`](../../python/analytics/agent/registry.py) — `plan_json_schema()`, `validate_step()`
6. [`python/analytics/agent/planner.py`](../../python/analytics/agent/planner.py), [`router.py`](../../python/analytics/agent/router.py), [`executor.py`](../../python/analytics/agent/executor.py), [`narrator.py`](../../python/analytics/agent/narrator.py)
7. [`python/analytics/cli.py`](../../python/analytics/cli.py) — i 4 comandi, exit code
8. [`python/analytics/report/render.py`](../../python/analytics/report/render.py), [`plots.py`](../../python/analytics/report/plots.py), [`export.py`](../../python/analytics/report/export.py)
9. [`docs/agent-decision-flow.md`](../agent-decision-flow.md) ← **la slide 8 è questo diagramma**
10. [`docs/analytics-methods.md`](../analytics-methods.md), [`docs/reports/`](../reports/), `docs/validation-log.md`

### Concetti da padroneggiare

- Gli 8 tool: `overview`, `success_rates`, `torque_stats`, `capping_speed`, `idle_periods`, `anomalies`, `trend`, `head_correlation`. Funzioni pure, SQL parametrizzato in, risultato tipizzato out.
- **Provenance non decorativa**: `ToolResult` porta stato, valori, periodo, righe scansionate, filtri, assunzioni — e alimenta la sezione *Confidence and limits*. Serve a non far sembrare uguali "100%" e "100% di quattro chiusure".
- **Sotto la CLI nulla solleva eccezioni**: un buco nei dati è uno `status`, non un throw. È ciò che permette a un run non presidiato di produrre comunque un report.
- I 3 vincoli sul planner: **structured outputs** (schema JSON generato dal registry stesso, quindi non può divergere) · **validazione registry** (tool inventato, argomento inesistente, valore fuori range = rifiutato) · **router keyword di fallback** (no key, no rete, rifiuto, JSON malformato, step invalido) con il motivo stampato nei limiti.
- Dettaglio Anthropic: structured outputs richiede `additionalProperties: false` e tutte le property in `required` ⇒ gli argomenti sono un unico oggetto piatto con l'unione dei parametri e i non usati a `null`; `plan.effective_args()` li scarta, e validazione/esecuzione/scelta figure leggono lo step **attraverso quella stessa funzione**, così non possono discordare.
- I 3 verbi `report` **non contengono modello**: piano fisso in `router.py`. Sono il path riproducibile della demo, il fallback offline e il riferimento contro cui si controlla il path agentico.
- Politica di fallimento: problema di **config** ⇒ exit 2 prima di qualsiasi lavoro; buco di **analisi** ⇒ report che nomina il buco.
- Il finding (slide 10, la presenta P1, ma i numeri escono dai tuoi tool): 99,9950% a livello macchina nasconde head 29 con 117 reject su 1.095, contro media per testa 30,4 e 78 della seconda peggiore (head 35).
- **Il non-trovato conta**: nessuna testa supera la soglia Mann-Kendall su coppia o success rate; tutte e 36 correlano > 0,9999 sulla coppia media. Una versione precedente nominava sempre una "testa meno correlata" — aritmetica vera, conclusione falsa.

### Domande probabili

| Domanda | Risposta breve |
|---|---|
| L'LLM calcola qualche numero? | No. Sceglie i tool e scrive la prosa attorno ai risultati. Nessun accesso allo store, nessun ruolo aritmetico. |
| Se l'API è giù? | Router keyword, report comunque prodotto, motivo scritto nella sezione limiti. I 3 verbi `report` non chiamano mai il modello. |
| Come testate l'agente senza token? | Client API finto iniettato: ogni path di fallimento di planner e narrator è pinnato. Zero rete, zero chiavi. |
| Cosa non è coperto? | Il path `ask` con API reale non ha evidenza committata: nessuna chiave nell'ambiente di validazione. Il fallback router sì. |

---

## Metodo di lavoro (3 sessioni)

1. **Sessione 1 (1 h, insieme)** — base comune sopra + giro dei 13 slide, ognuno prende i suoi.
2. **Sessione 2 (individuale)** — leggere i propri file nell'ordine dato, scrivere le proprie slide, preparare **2 domande trappola** per gli altri due.
3. **Sessione 3 (1,5 h, insieme)** — prova completa cronometrata, poi incrocio: P1 interroga P3, P3 interroga P2, P2 interroga P1. Chi non sa una risposta si segna il file da rileggere.

**Handoff da provare a voce** (sono i punti dove si inciampa):

- P1 → P2: "…ricostruito l'evento. Ora: quanto costa farlo su 89 file, e cosa succede se un processo muore."
- P2 → P3: "…lo store è unico e affidabile. Cosa ci si chiede sopra."
- P3 → chiusura: "il modello ha scelto le analisi; l'SQL ha prodotto ogni numero."

## Da correggere prima delle slide

- `outline.md` slide 11 dice **73 test C++ e 201 Python**; `README.md` § Testing dice **90 e 229**. Il conteggio delle macro `TEST`/`TEST_F` in `tests/*.cpp` dà **90**, quindi l'outline è vecchio. Lato Python `grep '^def test_'` dà 225 funzioni (229 con i casi parametrizzati): rilanciare `pytest -q` e allineare l'outline — è il tipo di numero che un docente ricontrolla dal vivo.
