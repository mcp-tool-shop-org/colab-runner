<p align="center">
  <a href="README.ja.md">日本語</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.md">English</a> | <a href="README.pt-BR.md">Português (BR)</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/mcp-tool-shop-org/brand/main/logos/colab-runner/readme.png" alt="Colab Runner" width="400">
</p>

<p align="center">
  <a href="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml"><img src="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://mcp-tool-shop-org.github.io/colab-runner/"><img src="https://img.shields.io/badge/Landing_Page-live-blue" alt="Landing Page"></a>
</p>

# colab-runner

Lo strumento condiviso dello studio per le istanze Google Colab a pagamento: un unico punto di accesso per il codice di ogni istanza Colab utilizzata. Avvia un job su una VM Colab in un processo che sopravvive alla configurazione, monitora l'esecuzione entro un limite di spesa, elimina i token da tutto ciò che riporta e cerca sempre di interrompere la sessione.

**Stato: 0.1.0.** Lo strumento indipendente (elemento 1 di seguito) è stato creato. Gli elementi da 2 a 5 non sono ancora stati creati.

1. **Lo strumento indipendente (0.1.0).** Avvia un job sulla VM con un file di stato, lo controlla con un meccanismo di ripetizione, una modalità di heartbeat `--hold-s` e un flag che attiva un ciclo residente nel kernel. Il salvataggio è un hook Python-API (di seguito).
2. **Protezioni per l'avvio e la scheda grafica.** Un controllo sulla VM al primo passaggio (capacità di calcolo e VRAM per tipo di scheda grafica, rifiuta `CARD_MISMATCH`), un registro di rifiuto lato configurazione con un interruttore automatico per scheda grafica (`CARD_CIRCUIT_OPEN`) e `OMP_NUM_THREADS` impostato prima del caricamento di torch.
3. **Segreti.** Il percorso del file dei token: un file con permessi 0600, leggibile solo dal processo che ne ha bisogno, eliminato sulla VM e rimosso da tutti i log salvati.
4. **Un modulo Hugging Face Storage Bucket.** Sincronizza gli output con un prefisso di staging per ogni istanza, verifica i conteggi e sha256 rispetto a un file manifest, quindi scrive una ricevuta `done.json`. Non elimina mai.
5. **`PLATFORM.md` e `facts.json`.** Ciò che la piattaforma Colab fa effettivamente, con ogni informazione datata e con indicazione della fonte, in modo che un kit verifichi le proprie ipotesi rispetto a tali informazioni e un errore diventi un codice di rifiuto, non una sorpresa a pagamento.

Le istanze utilizzano una versione specifica di questo repository, una istanza per ogni richiesta di pull.

## Installazione

colab-runner non è disponibile su PyPI. Un'istanza utilizza una versione specifica o la installa dal tag:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.1.0"
```

- **Python** 3.11 o successivo. Il pacchetto non ha dipendenze in fase di esecuzione.
- **La riga di comando `colab`** (Google Colab CLI), con accesso all'account che paga per l'istanza. colab-runner la chiama; non la installa né esegue l'accesso per te. `COLAB_RUNNER_CLI` punta a un'altra riga di comando (di seguito).
- **Piattaforme:** i test CI vengono eseguiti su Linux con Python 3.11 e 3.12. Lo studio lo esegue anche su Windows.

## Avvio rapido

L'istanza apre la sessione, quindi la passa a `run`, che la termina interrompendola:

```bash
colab new -s my-window            # the lane's own step: open the session (plus the card's flags)
python -m colab_runner run --session my-window --out-dir runs/w1   --script /content/train.sh --wall-s 3600   --max-rate 13.5 --cap 15 --cleanup-bound-s 600
echo "exit $?"                    # 0 done and stopped; see the exit codes below
```

Le cifre sono segnaposto. Imposta `--max-rate` sulle unità orarie previste per la scheda grafica e `--cap` sul budget dell'istanza in unità di calcolo (vedi Prezzi).

`python -m colab_runner --help` elenca ogni comando; `run --help` elenca ogni flag.

## Utilizzo da un'istanza (0.1.0)

**La sessione appartiene all'istanza.** L'istanza apre la sessione Colab e ne passa il nome. Una volta che `run` ha scritto `session_open.json` nella directory di output, ogni uscita tenta di interrompere la sessione e il marcatore viene rimosso solo dopo un'interruzione confermata.
- **Un marcatore che rimane** significa che la sessione potrebbe essere ancora aperta: un'interruzione non riuscita o un processo `run` terminato immediatamente (SIGKILL, un'istanza persa) prima che venga eseguita la pulizia. L'istanza esegue quindi `python -m colab_runner stop --out-dir D`.
- **Prima che il marcatore esista** (un rifiuto o un errore di utilizzo, entrambi con codice di uscita 1), l'istanza esegue `colab stop -s S`.

**Flag obbligatorie di `run`:** `--session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C --cleanup-bound-s N`.
- `--out-dir` non deve ancora esistere, oppure deve essere vuoto (altrimenti `OUT_DIR_NOT_EMPTY`). Una directory radice del filesystem non è consentita (`OUT_DIR_INVALID`): la directory di quarantena è una directory "sorella" della directory di output, il cui nome deriva dal percorso risolto. Lo stesso vale per una directory di output che non può essere creata o elencata.
- `--cleanup-bound-s` deve essere uguale o superiore al limite di pulizia del runner (altrimenti `CLEANUP_BOUND_TOO_SMALL`). Con i timeout predefiniti, questo limite è di 466 s, oppure 986 s con `--resident` (`Runner.own_cleanup_bound_s`).
- Passare `--pre-open-rate` e `--pre-open-assignments` insieme, subito prima dell'apertura della sessione, oppure non passarli affatto. In assenza, l'intero limite di velocità dell'account viene attribuito a questa finestra, quindi una sessione "vicina" potrebbe superare il limite di velocità.
- Passare un valore distinto di `--job` (predefinito `job`) per ogni passaggio che condivide una directory `--content`. Il motivo per cui un `--job` riutilizzato non è consentito:
- Ogni esecuzione ha il proprio ID di esecuzione, `CR_RUN_ID`, che è nuovo ogni volta. Il nome è riservato: `--env CR_RUN_ID=…` (o `CR_RUN_ID` in `Runner.run(env=...)`) non è consentito (`RUN_ID_RESERVED`), poiché l'uso dello stesso ID in più passaggi disabiliterebbe questo controllo tra di essi.
- La VM scrive l'ID di esecuzione in `<job>.run_id` prima dell'avvio del job. Il file di stato del job e ogni record di sondaggio lo contengono.
- I file del job sono `<job>.pid`, `.status.json`, `.run.out`, `.poll.json` e `.run_id`. Se uno di essi è presente con un ID di esecuzione diverso (o con nessuno), il passaggio termina `start_failed` con `JOB_ALREADY_EXISTS`. Questo vale indipendentemente dal fatto che il processo dell'altro job sia attivo o meno, e con o senza un file di stato.
- Un passaggio non consentito non tocca nessuno dei file dell'altra esecuzione, non interrompe mai il suo job e interrompe comunque la propria sessione. In modalità `--resident`, il rifiuto viene inviato a `<job>.refused.json`, mai a `<job>.poll.json`.
- Il runner prende solo i record che contengono il proprio ID di esecuzione. Un record lasciato da un'altra esecuzione nella stessa directory `--job` (ad esempio, il file `<job>.poll.json` finale del passaggio 1) non interrompe mai un passaggio. In modalità `--resident`, se nella directory `--setup-stall-s` compaiono solo i record di un'altra esecuzione, il passaggio termina `start_failed` con `JOB_ALREADY_EXISTS`. In assenza di `--resident`, tale record viene considerato un sondaggio non riuscito.
- Un errore prima che sia stato visualizzato un record (ad esempio, il timeout, mentre il job di un'altra esecuzione detiene il `--job`) non interrompe comunque nulla: la cella di interruzione risponde con `CR_NOT_OURS`, e quindi il `<job>.run.out` dell'altra esecuzione non viene nemmeno raccolto.
- Un nuovo tentativo di avvio della stessa esecuzione (quella la cui risposta è andata persa) non viene rifiutato.
- In modalità `--resident`, un valore `CR_RESIDENT_MAX_S` non finito o non positivo passato tramite `--env` viene rifiutato immediatamente (`RESIDENT_MAX_INVALID`, uscita 1).

**Codici di uscita** (convenzione dell'organizzazione), per `run` e `stop --out-dir D`:

| Uscita | Significato | `run` | `stop` |
| --- | --- | --- | --- |
| 0 | ok | `done` e l'interruzione è stata confermata | un'interruzione confermata o nulla da interrompere |
| 1 | errore dell'utente | un rifiuto (`REFUSED: …`) o un errore di utilizzo, prima che fosse stato scritto il marcatore della sessione | un marcatore che non fa riferimento a nessuna sessione (`SESSION_MARKER_INVALID`) o un errore di utilizzo |
| 2 | errore di runtime | qualsiasi uscita diversa da `done` (indipendentemente da ciò che ha fatto l'interruzione) o `RUN_ERROR` | un'interruzione che non è stata confermata o `STOP_ERROR` |
| 3 | successo parziale | `done`, ma l'interruzione non è stata confermata: `session_open.json` è ancora presente | (non utilizzato) |

All'uscita 3, il risultato del job si trova nella directory di output e la sessione deve comunque essere interrotta con `python -m colab_runner stop --out-dir D`.

Un rifiuto dopo il marker non è un'uscita con codice 1. Un `--job` riutilizzato, ad esempio, causa un'uscita con codice 2: a quel punto, il runner gestisce la sessione, quindi la interrompe, e l'ultima riga è `RUN_END reason=start_failed … code=JOB_ALREADY_EXISTS` con un suggerimento che indica
`--job` su stderr. Nessuna riga `REFUSED:` viene scritta.

**Rifiuti ed errori** hanno un codice, un messaggio e un suggerimento, e ogni riga viene ripulita:
- Un rifiuto scrive `REFUSED: CODE: message` come prima riga su stderr, quindi `hint: …` (cosa modificare).
- Un errore durante un'esecuzione scrive `RUN_ERROR: CODE: message`, quindi `hint: …`. Il codice è lo stesso dell'errore
(`BINARY_DOWNLOAD_TOKEN`, `QUARANTINE_ESCAPE`), oppure `UNEXPECTED_ERROR` con il tipo di eccezione nel messaggio. `stop` scrive `STOP_ERROR: …`
allo stesso modo.
- Un'esecuzione che termina con un rifiuto specifico dopo il marker (`JOB_ALREADY_EXISTS`, `BINARY_DOWNLOAD_TOKEN`,
`QUARANTINE_ESCAPE`) lo registra in
`runner_result.json` come `refusal: {code, message, hint, retryable}`. `retryable` è falso per ogni rifiuto dell'
input del chiamante: la stessa chiamata fallisce nello stesso modo.
- Ogni codice e il suo suggerimento sono in `colab_runner/errors.py` (`CODES`).

**Livelli di log** (`run` e `stop`): `--quiet` scrive solo le righe finali (`RUN_END`, `STOP_END` o `STOP_NOTHING`, e un
rifiuto o un errore con il relativo suggerimento). L'impostazione predefinita aggiunge il log del runner su stderr. `--verbose` aggiunge una riga per ogni polling. `--debug` aggiunge il
traceback di un errore. Un token viene ripulito a ogni livello, incluso il traceback.

**Come termina un'esecuzione:** `done`, `failed` (uscita diversa da zero), `oom`, `wall`, `cap`, `setup_stall`, `progress_stall`, `dead`,
`unreachable`, `start_failed`, `interrupted` o `error`.
- Ogni fine, tranne `error`, scrive `RUN_END reason=… rc=… stopped=…` come ultima riga su stdout, con ` code=CODE` aggiunto quando
l'esecuzione termina con un rifiuto specifico. Un suggerimento segue su stderr per tale rifiuto e per un'interruzione che non è stata confermata.
- Una fine `error` scrive `RUN_ERROR: …` su stderr e nessuna riga `RUN_END`; quando `session_open.json` è ancora nella directory di output,
segue il suggerimento per un'interruzione che non è stata confermata. Un rifiuto o un errore di utilizzo (uscita 1) scrive solo su stderr.
- `runner_result.json` nella directory di output registra ogni fine dopo che il marker è stato scritto, incluso `error`. Un download binario
rifiutato durante la pulizia trasforma la fine in `error` e mantiene la fine che ha sostituito in `prior_reason` (e nei dettagli).

**Cosa `run` scrive nella directory di output** (`--job` ha come valore predefinito `job`, `--content` ha come valore predefinito `/content`):
- `session_open.json` mentre la sessione potrebbe essere aperta;
- `runner_result.json`, la fine dell'esecuzione. Il suo `quarantined` elenca ogni file binario scaricato e messo in quarantena durante l'esecuzione, e una quarantena da un `Runner.download` successivo attraverso lo stesso `Runner` sovrascrive il file.
- `<job>.run.out`, il log del job (appare solo dopo che il job ha scritto righe di log, o dopo che `--resident` le ha raccolte);
- quando `--resident` è impostato, `<job>.poll.json` (l'ultimo record di sondaggio del job, come scaricato dal runner) e `<job>.refused.json`, il record di rifiuto di questa esecuzione, quando la sua cella residente rifiutata ne ha scritto uno (un `JOB_ALREADY_EXISTS` che ha solo superato il timeout sui record di un'altra esecuzione).
- `stop_confirmed.json` dopo un arresto confermato, oppure `stop_pending.json` dopo un arresto non riuscito.

**Il comando `colab`** è `colab` a meno che `COLAB_RUNNER_CLI` non specifichi un altro comando. Viene suddiviso con `shlex.split`,
che utilizza le virgolette POSIX: su Windows, scrivi il percorso con barre oblique o racchiudilo tra virgolette (`"C:\tools\colab.exe"`), poiché una
barra rovesciata non racchiusa tra virgolette viene eliminata. Un comando che non può essere suddiviso (una virgoletta non chiusa) viene rifiutato (`CLI_INVALID`, uscita 1).

**Cosa il job deve scrivere, in modo che i controlli di stallo possano visualizzarlo.** I percorsi si trovano sulla VM; la directory di output è `CR_OUT_DIR`, per impostazione predefinita `<content>/<job>_out`.
- `phase.txt`: assente, vuoto o `setup` durante l'installazione, qualsiasi altra cosa una volta in esecuzione. I job avviati con `--hold-s` saltano la fase di configurazione.
- `setup.log`: durante la configurazione, un'esecuzione il cui `setup.log` smette di aumentare per `--setup-stall-s` (predefinito 600) termina `setup_stall`.
- **Progresso:** dopo la configurazione, un'esecuzione termina `progress_stall` quando nulla di ciò che può vedere si muove per `--progress-stall-s` (predefinito 900).
Ciò che vede è la somma del primo numero intero in ogni file corrispondente a `CR_PROGRESS_GLOB` (predefinito `<out>/progress/*`), il numero di file corrispondenti a `CR_UNITS_GLOB` e i battiti di cuore di attesa.
- Un'esecuzione senza un buon sondaggio per `--unreachable-polls` sondaggi consecutivi (predefinito 10), o per `--unreachable-s` (predefinito 1800), termina `unreachable`.

**Segreti e pulizia dei download per garantire la sicurezza binaria.** Passa un segreto come `--env NAME`, con NAME impostato nell'ambiente del processo. `--env NAME=VALUE`
inserisce il valore nella riga di comando del processo. I valori raggiungono la VM all'interno del file della cella del notebook caricato, mai come argomenti per la CLI `colab`.
Ogni riga di output della CLI e ogni riga di errore vengono ripulite con la libreria fornita `scrub_colab_tokens`. I file e le
directory scaricati (inclusi i trasferimenti parziali) vengono classificati solo in base al contenuto (un byte NUL o un tentativo di decodifica UTF-8 non riuscito indica un file binario):
- I file di testo vengono ripuliti direttamente con `scrub_colab_tokens`.
- I file binari puliti rimangono identici byte per byte. I file illeggibili vengono rimossi.
- **La regola binaria.** Un file binario viene scansionato con coppie di byte dei modelli di token della libreria.
- In UTF-8, le coppie corrispondono esattamente a ciò che i modelli di testo corrispondono nel testo decodificato. Ogni carattere che `\s` di Python corrisponde
viene considerato uno spazio, inclusi `\x1c`-`\x1f`, NBSP, NEL e EM SPACE.
- L'unica differenza è che si trova accanto a una lettera non ASCII, dove la coppia `hf_` corrisponde e il modello di testo no. Quindi,
la regola binaria può rilevare più elementi rispetto alla regola del testo, ma mai meno.
- Una regola è più rigorosa rispetto al testo: in un file binario, `Bearer` conta solo quando il valore successivo ha la forma di un token. Ciò significa
almeno 20 caratteri di `[A-Za-z0-9._-]`, utilizzando almeno due tra lettere minuscole, maiuscole e cifre.
- Quindi, un rendering il cui prompt dice `Flame Bearer of the North` viene trasmesso byte per byte. Un vero token di autenticazione (un token di accesso OAuth,
un JWT, un token di Hugging Face) viene rilevato.
- I file di testo mantengono invariati i modelli della libreria: lo stesso testo in un file di testo continua a diventare `Bearer [REDACTED] the
North`.
- **Un rilevamento non viene mai sovrascritto.**
- Il file viene spostato invariato nella quarantena, registrato e denominato in `runner_result.json` (`quarantined`) solo in base al percorso e al nome del modello.
- Il download viene rifiutato con `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`). Ciò interrompe `Runner.run` come `error` (uscita CLI 2) e la sessione viene comunque interrotta.
- **Il download di una directory** viene ripulito token per token, e un singolo errore non interrompe il resto: un rilevamento il cui spostamento nella quarantena
fallisce viene rimosso e una sottodirectory che non può essere elencata viene annotata. Il primo errore di questo tipo viene segnalato una volta che tutti i file rimanenti sono stati ripuliti. Vengono letti solo i file regolari. Un collegamento simbolico, a un file o a una directory, viene eliminato, mai seguito, e qualsiasi altra voce che non sia un file regolare o una directory (un FIFO, un socket, un dispositivo) viene eliminata, mai letta.
- **Dove si trova la quarantena.**
- Tramite `Runner` (e quindi `run`), è la directory sorella `<out-dir>.quarantine/`. Si trova all'esterno di `--out-dir`, quindi
né `salvage` né il raggruppamento out-dir lo rilevano.
- Una semplice `Colab.download` o `scrub_local` senza `quarantine_dir` sposta il file in una nuova cartella
`colab-runner-quarantine-*` nella directory temporanea del sistema (`tempfile.gettempdir()`). Non si trova mai accanto al file, quindi
mai all'interno della cartella in cui è stato scaricato il file. Il suo percorso è in `BinaryDownloadRefused.quarantined`. Passa
`Colab(quarantine_dir=...)` per scegliere il luogo.
- Ogni percorso di quarantena viene risolto e verificato per assicurarsi che rimanga all'interno della sua cartella di quarantena prima dello spostamento. Un file che
si troverebbe all'esterno viene invece rimosso e il download fallisce con `QuarantineEscape` (`QUARANTINE_ESCAPE`).
- colab-runner non elimina mai una quarantena. **Il processo chiamante deve eliminarla e non deve mai inviarla.**

**Salvage** è solo un hook dell'API Python: `Runner.run(..., salvage=callable)` lo esegue su ogni processo, tranne `done`, prima dell'arresto.
La CLI non ha la funzione Salvage.

**Prezzi.** La finestra ha un costo pari a `max(--max-rate, the highest stable window rate)` volte il tempo trascorso, tramite le funzioni di tariffa e limite della libreria fornita. Una tariffa stabile superiore a `--max-rate` attiva `RATE_ABOVE_MAX`, quindi ogni finestra che continua a essere eseguita ha un costo di `--max-rate`: è un limite minimo, non un limite massimo flessibile. Impostalo sulla tariffa prevista per la carta; un limite flessibile `--max-rate` attiva il limite
prima. Il saldo dell'account viene registrato e non influisce sul prezzo della finestra.

## Sicurezza e modello di minaccia

**Cosa influisce.**
- La sessione Colab: carica i file delle celle, avvia e interrompe il processo avviato, scarica i file richiesti e termina la sessione. Non invia mai un segnale a un processo che appartiene a un’altra esecuzione di colab-runner (`CR_NOT_OURS`).
- La directory di output (`--out-dir`) e la relativa directory di quarantena `<out-dir>.quarantine/` sull’ambiente di test. Una directory `Colab.download` senza directory di quarantena utilizza una nuova cartella `colab-runner-quarantine-*` all’interno della directory temporanea del sistema.
- Le variabili d’ambiente che la sessione Colab utilizza con `--env`, e che vengono passate alla macchina virtuale all’interno del file della cella.

**Cosa non influisce.** Nessun file al di fuori della directory di output, della sua quarantena e della quarantena temporanea. Nessun'altra sessione Colab. Nessuna impostazione dell'account, credenziale o fatturazione: legge la tariffa e il saldo dell'account tramite la riga di comando `colab` e non li modifica mai.

**Rete.** Solo tramite la riga di comando `colab`, verso Google Colab. colab-runner non apre alcuna connessione propria.

**Segreti.** Un segreto trasmesso come `--env NAME` non compare in nessuna riga di comando. Ogni riga di output, riga di errore, traccia di errore e file di testo scaricato viene ripulito dai token; un file binario che contiene un token viene messo in quarantena senza modifiche e il download viene rifiutato (come indicato sopra). Colab-runner non elimina mai la quarantena: la procedura chiamante deve eliminarla e non deve mai trasmetterla.

**Autorizzazioni.** Viene eseguito come l'utente che lo esegue, con l'accesso `colab` già effettuato da tale utente. Non richiede alcuna elevazione dei privilegi.

**Nessuna telemetria.** colab-runner non raccoglie né invia dati.

Segnalazione di una vulnerabilità: vedere [SECURITY.md](SECURITY.md).

## Cosa non è presente

Nessun nome di bucket, nome di account, wrapper di avvio o percorso locale. Un chiamante li passa tramite flag o
variabile d'ambiente; i test utilizzano segnaposto.

## Verifica

```bash
bash verify.sh
```

## Licenza

MIT. Vedere [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
