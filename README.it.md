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

Esegui uno script su una GPU Google Colab a pagamento dalla tua macchina, con un limite di spesa definito, e ricevi i risultati. Descrivi il lavoro in un piccolo file; colab-runner apre la sessione, esegue il lavoro, recupera i risultati, interrompe la sessione e registra i costi.

## Cosa fa

- **Un file di specifiche per ogni lavoro.** Input, comandi di configurazione ed esecuzione, output, tipo di GPU, limite di spesa e limite di tempo, tutto in `colab-window.toml`. Nessuno script di avvio specifico per ogni lavoro.
- **Un limite di spesa che definisci tu.** Il costo della sessione è calcolato in base alla tariffa × tempo trascorso sulla tua macchina. Quando viene raggiunto il limite o il limite di tempo, il lavoro viene interrotto e la sessione chiusa.
- **Provalo prima di pagare.** `--dry` verifica le specifiche senza effettuare chiamate a Colab. `--local-cpu` esegue l'intero lavoro sulla tua macchina, nello stesso modo in cui lo eseguirebbe sulla VM.
- **La sessione viene sempre interrotta.** Qualsiasi metodo di esecuzione cerca di interrompere la sessione. Se la tua macchina si blocca durante l'esecuzione, `stop --out-dir` la chiuderà in seguito.
- **I token vengono rimossi dai risultati.** I log e i file scaricati vengono ripuliti dai token di accesso prima di essere salvati.
- **Una registrazione di ogni esecuzione.** `outcome.json` nella cartella di output indica come è terminata l'esecuzione e quali sono stati i costi.

## Installazione

colab-runner non è disponibile su PyPI. Installalo da un tag di rilascio:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.2.0"
```

Sono necessari Linux o WSL, Python 3.11 o successivo e l'interfaccia a riga di comando `colab` di Google, con accesso all'account che effettua il pagamento. colab-runner non ha altre dipendenze.

## Avvio rapido

Scrivi una specifica (un esempio completo è disponibile in [examples/merchant_league_seats](examples/merchant_league_seats/README.md)):

```toml
schema_version = 1

[project]
repo = "you/your-repo"
study = "first-try"
job = "train_01"

[compute]
card = "L4"        # GPU type: T4, L4, G4 or A100
shape = "standard"
max_rate = 2.5     # expected compute units per hour
cap = 5.0          # never spend more than this
wall_s = 3600      # never run longer than this

[inputs]
root = "."
files = [
  { src = "train.py", dst = "train.py" },
  { src = "requirements.txt", dst = "requirements.txt" },
]

[job]
setup = ["pip install -r requirements.txt"]
run = ["python3 train.py"]
cpu_dry_run = ["python3 train.py --smoke"]

[outputs]
collect = ["model", "metrics.json"]
```

Quindi, verificala, eseguila in prova e poi eseguila:

```bash
python -m colab_runner launch --spec colab-window.toml --out-dir runs/try --dry
python -m colab_runner launch --spec colab-window.toml --out-dir runs/cpu --local-cpu
python -m colab_runner launch --spec colab-window.toml --out-dir runs/w1 --cap 5 --max-rate 2.5
python -m colab_runner status --out-dir runs/w1
```

I risultati vengono salvati in `runs/w1/collected/`, con `outcome.json` accanto. Per un avvio effettivo, sono necessari `--cap` e `--max-rate` nella riga di comando, quindi un'esecuzione a pagamento è sempre una scelta consapevole.

## Quando un'esecuzione termina

| Uscita | Significato |
| --- | --- |
| 0 | Il lavoro è terminato, i risultati sono stati recuperati e la sessione è stata interrotta. |
| 1 | Rifiutato prima dell'apertura: correggi le specifiche o i flag (il messaggio indica cosa). |
| 2 | L'esecuzione è terminata prematuramente (il lavoro è fallito, è stato raggiunto il limite di spesa o di tempo, si è verificato un blocco o è diventato irraggiungibile). La sessione viene interrotta; se l'interruzione non riesce, il messaggio indica di eseguire `stop`. |
| 3 | Il lavoro è terminato, ma l'interruzione non è stata confermata. Esegui `python -m colab_runner stop --out-dir D`. |

Ogni rifiuto ed errore stampa un codice, un messaggio e un suggerimento. `python -m colab_runner --help` elenca i comandi e `launch --help` tutti i flag.

**Maggiori dettagli:** l'associazione a una sessione aperta, cosa il lavoro dovrebbe scrivere per rendere visibile l'avanzamento, come vengono gestiti i download e i prezzi, sono tutti descritti in [docs/reference.md](docs/reference.md).

## Sicurezza e modello di minaccia

**Cosa influisce.**
- La sessione Colab: carica i file delle celle, avvia e interrompe il processo avviato, scarica i file richiesti e termina la sessione. Non invia mai un segnale a un processo che appartiene a un’altra esecuzione di colab-runner (`CR_NOT_OURS`).
- La directory di output (`--out-dir`) e la relativa directory di quarantena `<out-dir>.quarantine/` sull’ambiente di test. Una chiamata diretta a `Colab.download` senza directory di quarantena utilizza una nuova cartella `colab-runner-quarantine-*` all’interno della directory temporanea del sistema.
- Le variabili d’ambiente che la sessione Colab utilizza con `--env`, e che vengono passate alla macchina virtuale all’interno del file della cella.

**Cosa non viene toccato.** Nessun file al di fuori della directory "out", della sua area di quarantena e dell'area di quarantena temporanea. Nessuna altra sessione Colab. Nessuna impostazione dell'account, credenziale o fatturazione: legge la tariffa e il saldo dell'account tramite l'interfaccia a riga di comando `colab` e non li modifica mai.

**Rete.** Solo tramite l'interfaccia a riga di comando `colab`, verso Google Colab. colab-runner non apre alcuna connessione autonoma.

**Segreti.** Un segreto passato come `--env NAME` non viene mai incluso in nessuna riga di comando. Ogni riga di output, riga di errore, traccia di stack e file di testo scaricato viene ripulito dai token; un file binario contenente un token viene messo in quarantena senza modifiche e il download viene rifiutato (come indicato sopra). colab-runner non elimina mai l'area di quarantena: il processo chiamante deve eliminarla e non deve mai trasmetterla.

**Autorizzazioni.** Viene eseguito come l'utente che lo esegue, con l'accesso `colab` già effettuato da quell'utente. Non richiede privilegi elevati.

**Nessuna telemetria.** colab-runner non raccoglie né invia dati.

Per segnalare una vulnerabilità, consulta [SECURITY.md](SECURITY.md).

## Cosa non è incluso

Nessun nome di bucket, nome di account, script di avvio o percorso locale. Il processo chiamante passa questi dati tramite flag o variabili d'ambiente; i test utilizzano segnaposto.

## Verifica

```bash
bash verify.sh
```

## Licenza

MIT. Consulta [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
