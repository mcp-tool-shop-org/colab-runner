<p align="center">
  <a href="README.ja.md">日本語</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | <a href="README.md">English</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.pt-BR.md">Português (BR)</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/mcp-tool-shop-org/brand/main/logos/colab-runner/readme.png" alt="Colab Runner" width="400">
</p>

<p align="center">
  <a href="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml"><img src="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://mcp-tool-shop-org.github.io/colab-runner/"><img src="https://img.shields.io/badge/Landing_Page-live-blue" alt="Landing Page"></a>
</p>

Exécutez un script sur une instance GPU Google Colab payante depuis votre propre machine, avec une limite de dépenses définie, et récupérez les résultats. Vous décrivez la tâche dans un petit fichier ; colab-runner ouvre la session, exécute la tâche, récupère les résultats, arrête la session et enregistre le coût.

## Ce que cela fait

- **Un seul fichier de spécification par tâche.** Entrées, commandes de configuration et d’exécution, sorties, type de GPU, limite de dépenses et limite de temps, le tout dans
`colab-window.toml`. Pas de scripts de lancement spécifiques à chaque tâche.
- **Une limite de dépenses que vous définissez.** Le coût est calculé en fonction du tarif multiplié par le temps écoulé sur votre machine. Lorsque la limite est atteinte,
soit la limite de dépenses, soit la limite de temps, la tâche est interrompue et la session est fermée.
- **Testez avant de payer.** `--dry` vérifie la spécification sans effectuer d’appels à Colab. `--local-cpu` exécute la tâche complète sur votre
machine, de la même manière qu’elle serait exécutée sur la machine virtuelle.
- **La session est toujours interrompue.** Toutes les méthodes d’interruption d’une exécution tentent d’interrompre la session. Si votre machine tombe en panne
pendant l’exécution, `stop --out-dir` la fermera ultérieurement.
- **Les jetons sont exclus des résultats.** Les journaux et les fichiers téléchargés sont purgés de leurs jetons d’accès avant d’être enregistrés.
- **Un historique de chaque exécution.** `outcome.json` dans le dossier de sortie indique comment l’exécution s’est terminée et quel en a été le coût.
- **La publication vous appartient.** colab-runner ne télécharge jamais rien. Pour partager les résultats, utilisez `hf upload` de Hugging Face
(voir [docs/reference.md](docs/reference.md)).

## Installation

colab-runner ne se trouve pas dans PyPI. Installez-le à partir d’une étiquette de version :

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.2.0"
```

Vous avez besoin de Linux ou de WSL, de Python 3.11 ou d’une version ultérieure, et de l’outil en ligne de commande `colab` de Google, avec une connexion au compte qui effectue le paiement. colab-runner n’a aucune autre dépendance.

## Démarrage rapide

Travaillez dans le dépôt Git de votre projet : les fichiers situés en dehors sont considérés comme des données protégées et sont refusés. Rédigez une spécification (une spécification complète se trouve dans [examples/merchant_league_seats](examples/merchant_league_seats/README.md)) :

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

`--local-cpu` exécute les commandes de configuration et de tâche sur votre machine, il a donc besoin de tout ce qu’elles requièrent (ici, pip :
`sudo apt install python3-pip` sur un système Ubuntu ou WSL propre). Ensuite, vérifiez, testez et exécutez :

```bash
python -m colab_runner launch --spec colab-window.toml --out-dir runs/try --dry
python -m colab_runner launch --spec colab-window.toml --out-dir runs/cpu --local-cpu
python -m colab_runner launch --spec colab-window.toml --out-dir runs/w1 --cap 5 --max-rate 2.5
python -m colab_runner status --out-dir runs/w1
```

Les résultats sont enregistrés dans `runs/w1/collected/`, avec `outcome.json` à côté. Un lancement en direct nécessite `--cap` et `--max-rate` dans la ligne de commande, de sorte qu’une exécution payante est toujours un choix délibéré.

## À la fin d’une exécution

| Sortie | Signification |
| --- | --- |
| 0 | La tâche est terminée, les résultats ont été récupérés et la session est arrêtée. |
| 1 | Refusé avant même le début : corrigez la spécification ou les indicateurs (le message indique ce qu’il faut faire). |
| 2 | L’exécution s’est terminée prématurément (la tâche a échoué, la limite a été atteinte, la limite de temps a été dépassée, l’exécution s’est bloquée ou la cible est inaccessible). La session est arrêtée ; si l’arrêt a échoué, le message indique qu’il faut exécuter `stop`. |
| 3 | La tâche est terminée, mais l’arrêt n’a pas été confirmé. Exécutez `python -m colab_runner stop --out-dir D`. |

Chaque refus et chaque erreur affiche un code, un message et un indice. `python -m colab_runner --help` liste les commandes, et `launch --help` tous les indicateurs.

**Plus de détails :** la connexion à une session que vous avez ouverte vous-même, ce que la tâche doit écrire pour que la progression soit visible, la manière dont les téléchargements sont purgés et la tarification sont décrits dans [docs/reference.md](docs/reference.md).

## Sécurité et modèle de menace

**Ce que cela affecte.**
- La session Colab que vous nommez : elle télécharge les fichiers de cellules, démarre et arrête la tâche qu’elle a lancée, télécharge les fichiers demandés et arrête la session. Elle n’envoie jamais de signal à un processus qui appartient à une autre exécution de colab-runner (`CR_NOT_OURS`).
- Le répertoire de sortie (`--out-dir`) et son répertoire de quarantaine associé (`<out-dir>.quarantine/`) sur votre machine. Un répertoire `Colab.download` sans répertoire de quarantaine utilise un nouveau dossier `colab-runner-quarantine-*` sous le répertoire temporaire du système.
- Les variables d’environnement que vous nommez avec `--env`, qu’elle transmet à la machine virtuelle à l’intérieur du fichier de cellule.

**Ce que cela ne touche pas.** Aucun fichier en dehors du répertoire de sortie, de sa zone de quarantaine et de la zone de quarantaine temporaire. Aucune autre session Colab. Aucun paramètre de compte, aucune information d’identification ou aucune information de facturation : il lit le tarif et le solde du compte via l’outil en ligne de commande `colab` et ne les modifie jamais.

**Réseau.** Uniquement via l’outil en ligne de commande `colab`, vers Google Colab. colab-runner n’ouvre aucune connexion de son propre chef.

**Secrets.** Un secret transmis sous forme de `--env NAME` n’apparaît sur aucune ligne de commande. Chaque ligne de sortie, chaque ligne d’erreur, chaque trace de pile et chaque fichier texte téléchargé sont purgés de leurs jetons ; un fichier binaire contenant un jeton est mis en quarantaine sans modification et le téléchargement est refusé (ci-dessus). colab-runner ne supprime jamais la quarantaine : vous devez la supprimer et ne jamais la distribuer.

**Autorisations.** Il s’exécute en tant qu’utilisateur qui l’exécute, avec la connexion `colab` que cet utilisateur a déjà. Il n’a besoin d’aucune élévation de privilèges.

**Pas de télémétrie.** colab-runner ne collecte ni ne transmet aucune donnée.

Pour signaler une vulnérabilité : consultez [SECURITY.md](SECURITY.md).

## Ce qui n’est pas inclus ici

Aucun nom de bucket, aucun nom de compte, aucun script de lancement ou aucun chemin d’accès local. L’appelant transmet ces éléments par le biais d’indicateurs ou de variables d’environnement ; les tests utilisent des espaces réservés.

## Vérification

```bash
bash verify.sh
```

## Licence

MIT. Voir [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
