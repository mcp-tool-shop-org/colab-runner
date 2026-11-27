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

- **Un file di configurazione per ogni attività.** Input, comandi di configurazione e avvio, output, tipo di GPU, limite di spesa e limite di tempo, tutto in
`colab-window.toml`. Nessuno script di avvio specifico per ogni attività.
- **Un limite di spesa che si imposta.** Il costo viene calcolato come tariffa × tempo di esecuzione sulla propria macchina. Quando viene raggiunto il limite
o il limite di tempo, l'attività viene interrotta e la sessione chiusa.
- **Provatelo prima di pagare.** `--dry` verifica il file di configurazione senza effettuare chiamate a Colab. `--local-cpu` esegue l'intera attività sulla
propria macchina, nello stesso modo in cui la eseguirebbe sulla macchina virtuale.
- **La sessione viene sempre interrotta.** Qualsiasi sia il modo in cui un'esecuzione può terminare, si cerca di interrompere la sessione. Se la propria macchina si blocca
a metà esecuzione, `stop --out-dir` la chiuderà in seguito.
- **I token vengono esclusi dai risultati.** I log e i file scaricati vengono ripuliti dai token di accesso prima di essere salvati.
- **Una registrazione di ogni esecuzione.** `outcome.json` nella cartella di output indica come è terminata l'esecuzione e quanto è costata.
- **La pubblicazione è a vostra discrezione.** colab-runner non carica mai nulla. Per condividere gli output, utilizzate `hf upload` di Hugging Face
(vedere [docs/reference.md](docs/reference.md)).

## Installazione

colab-runner non è disponibile su PyPI. Installalo da un tag di rilascio:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.2.0"
```

Sono necessari Linux o WSL, Python 3.11 o successivo e l'interfaccia a riga di comando `colab` di Google, con accesso all'account che effettua il pagamento. colab-runner non ha altre dipendenze.

## Avvio rapido

Lavora all’interno del repository Git del tuo progetto: i file al di fuori di esso sono considerati dati protetti e vengono rifiutati. Scrivi una specifica (una specifica completa è disponibile in [examples/merchant_league_seats](examples/merchant_league_seats/README.md)):

```toml
schema_version = 1

[project]
repo = "you/your-repo"   # required: your repo, for the records
study = "first-try"      # required: a name for this line of work
job = "train_01"

[compute]
card = "L4"        # CPU, T4, L4, G4 or A100
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
setup = ["python3 -m pip install -r requirements.txt"]
run = ["python3 train.py"]
cpu_dry_run = ["python3 train.py --smoke"]

[outputs]
collect = ["model", "metrics.json"]
```

`--local-cpu` esegue i comandi di configurazione e di esecuzione sulla tua macchina, quindi necessita di tutto ciò che questi comandi richiedono (qui, pip:
`sudo apt install python3-pip` su un sistema Ubuntu o WSL appena installato). Quindi, verifica, prova e esegui:

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
- La sessione Colab che hai specificato: carica i file delle celle, avvia e interrompe il processo che ha avviato, scarica i file richiesti e termina la sessione. Non invia mai un segnale a un processo che appartiene a un’altra esecuzione di colab-runner (`CR_NOT_OURS`).
- La directory di output (`--out-dir`) e la sua directory di quarantena associata `<out-dir>.quarantine/` sulla tua macchina. Una configurazione minima `Colab.download` senza una directory di quarantena utilizza una nuova cartella `colab-runner-quarantine-*` all’interno della directory temporanea del sistema.
- Le variabili d’ambiente che hai specificato con `--env`, che vengono passate alla macchina virtuale all’interno del file della cella.

**Cosa non viene toccato.** Nessun file al di fuori della directory "out", della sua area di quarantena e dell'area di quarantena temporanea. Nessuna altra sessione Colab. Nessuna impostazione dell'account, credenziale o fatturazione: legge la tariffa e il saldo dell'account tramite l'interfaccia a riga di comando `colab` e non li modifica mai.

**Rete.** Solo tramite l'interfaccia a riga di comando `colab`, verso Google Colab. colab-runner non apre alcuna connessione autonoma.

**Segreti.** Un segreto passato come `--env NAME` non compare mai in nessuna riga di comando. Ogni riga di output, riga di errore, traccia di errore e file di testo scaricato viene ripulito dai token; un file binario che contiene un token viene messo in quarantena senza modifiche e il download viene rifiutato (come indicato sopra). colab-runner non elimina mai la quarantena: devi eliminarla tu e non devi mai inviarla.

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
