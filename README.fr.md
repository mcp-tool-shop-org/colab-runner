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

# colab-runner

L’exécuteur partagé du studio pour les instances Google Colab payantes : un emplacement unique pour le code de chaque instance Colab.
Il lance une tâche sur une VM Colab dans un processus qui dépasse la durée de vie de l’environnement, surveille son exécution
dans les limites d’un budget, supprime les jetons de tous les éléments qu’il récupère et tente toujours d’arrêter la session.

**État : 0.1.0.** L’exécuteur détaché (élément 1 ci-dessous) est construit. Les éléments 2 à 5 ne sont pas encore construits.

1. **L’exécuteur détaché (0.1.0).** Lance une tâche sur la VM avec un fichier d’état, le surveille avec une politique de réessai, un mode de signalement `--hold-s` et un indicateur qui bascule vers une boucle résidant dans le noyau. La sauvegarde est un hook d’API Python (ci-dessous).
2. **Gardes de démarrage et de carte.** Une assertion de carte lors de la première étape de la VM (capacité de calcul et VRAM par type de carte, refusant `CARD_MISMATCH`), un journal de refus côté environnement avec un disjoncteur par carte
(`CARD_CIRCUIT_OPEN`) et `OMP_NUM_THREADS` défini avant le chargement de torch.
3. **Secrets.** L’itinéraire du fichier de jeton : un fichier 0600, lu uniquement par le processus qui en a besoin, supprimé sur la
VM, effacé de tous les journaux conservés.
4. **Un module de stockage Hugging Face.** Synchronise les résultats vers un préfixe de mise en scène par instance, vérifie les nombres et
sha256 par rapport à un manifeste, puis écrit un reçu `done.json`. Il ne supprime jamais.
5. **`PLATFORM.md` et `facts.json`.** Ce que la plateforme Colab fait réellement, chaque fait daté et référencé,
de sorte qu’un kit vérifie ses hypothèses par rapport à ces faits et qu’une hypothèse incorrecte devient un code de refus, et non une surprise payante.

Les instances utilisent une version épinglée de ce dépôt, une instance par demande d’extraction.

## Installation

colab-runner n’est pas disponible sur PyPI. Une instance utilise une version épinglée ou l’installe à partir de la balise :

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.1.0"
```

- **Python** 3.11 ou version ultérieure. Le package n’a aucune dépendance d’exécution.
- **L’interface de ligne de commande `colab`** (l’interface de ligne de commande Colab de Google), connectée au compte qui paie l’instance. colab-runner
l’appelle ; il ne l’installe pas et ne vous connecte pas. `COLAB_RUNNER_CLI` pointe vers une autre interface de ligne de commande (ci-dessous).
- **Plateformes :** les exécutions CI s’effectuent sur Linux avec Python 3.11 et 3.12. Le studio l’exécute également à partir de Windows.

## Démarrage rapide

L’instance ouvre la session, puis la transmet à `run`, qui la termine en l’arrêtant :

```bash
colab new -s my-window            # the lane's own step: open the session (plus the card's flags)
python -m colab_runner run --session my-window --out-dir runs/w1   --script /content/train.sh --wall-s 3600   --max-rate 13.5 --cap 15 --cleanup-bound-s 600
echo "exit $?"                    # 0 done and stopped; see the exit codes below
```

Les chiffres sont des espaces réservés. Définissez `--max-rate` sur le nombre d’unités attendues par heure pour la carte et `--cap` sur le budget de l’instance en unités de calcul (voir Tarification).

`python -m colab_runner --help` répertorie toutes les commandes ; `run --help` répertorie tous les indicateurs.

## Utilisation à partir d’une instance (0.1.0)

**La session appartient à l’instance.** L’instance ouvre la session Colab et transmet son nom. Une fois que `run` a écrit
`session_open.json` dans le répertoire de sortie, chaque sortie tente d’arrêter la session, et le marqueur n’est supprimé qu’après un arrêt confirmé.
- **Un marqueur qui persiste** signifie que la session est peut-être toujours ouverte : un arrêt qui a échoué, ou un `run` qui a été interrompu immédiatement (SIGKILL, un environnement perdu) avant que son nettoyage ne soit effectué. L’instance exécute ensuite `python -m colab_runner stop --out-dir D`.
- **Avant que le marqueur n’existe** (un refus ou une erreur d’utilisation, les deux entraînant une sortie 1), l’instance exécute `colab stop -s S` elle-même.

**Indicateurs requis de `run` :** `--session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C --cleanup-bound-s N`.
- `--out-dir` ne doit pas encore exister, ou être vide (`OUT_DIR_NOT_EMPTY` sinon). Un répertoire racine du système de fichiers est refusé
(`OUT_DIR_INVALID`) : la zone de quarantaine est un répertoire frère du répertoire de sortie, nommé d’après son chemin résolu. Il en va de même pour un répertoire de sortie qui ne peut pas être créé ou listé.
- `--cleanup-bound-s` doit être au moins égal à la limite de nettoyage de l’exécuteur lui-même (`CLEANUP_BOUND_TOO_SMALL` sinon). Avec les délais d’attente par défaut, cette limite est de 466 s, ou de 986 s avec `--resident` (`Runner.own_cleanup_bound_s`).
- Transmettez `--pre-open-rate` et `--pre-open-assignments` ensemble, juste avant l’ouverture de la session, ou ne transmettez aucun des deux. Sans eux,
l’ensemble du débit du compte est attribué à cette instance, de sorte qu’une session voisine peut dépasser la limite de débit.
- Transmettez un `--job` distinct (par défaut `job`) pour chaque étape partageant un répertoire `--content`. La manière dont une `--job` réutilisée est refusée :
- Chaque exécution a son propre ID d’exécution, `CR_RUN_ID`, un ID différent à chaque fois. Le nom est réservé : `--env CR_RUN_ID=…` (ou `CR_RUN_ID` dans `Runner.run(env=...)`) est refusé (`RUN_ID_RESERVED`), car la réutilisation d’un ID entre les étapes désactiverait ce refus.
- La VM écrit l’ID d’exécution dans `<job>.run_id` avant le début de la tâche. Le fichier d’état de la tâche et chaque enregistrement de surveillance contiennent cet ID.
- Les fichiers de la tâche sont `<job>.pid`, `.status.json`, `.run.out`, `.poll.json` et `.run_id`. Si l’un d’eux est présent sous un autre ID d’exécution (ou sous aucun ID), l’étape se termine avec `start_failed` et `JOB_ALREADY_EXISTS`. Cela est valable que le processus de l’autre tâche soit actif ou non, et avec ou sans fichier d’état.
- Une étape refusée ne touche aucun des fichiers de l’autre exécution, n’interrompt jamais sa tâche et arrête toujours sa propre session. En mode `--resident`, le refus est envoyé à `<job>.refused.json`, et non à `<job>.poll.json`.
- L’exécuteur ne prend que les enregistrements qui contiennent son propre ID d’exécution. Un enregistrement qu’une autre exécution a laissé sous le même `--job` (le `<job>.poll.json` final de la première étape, par exemple) ne termine jamais une étape. En mode `--resident`, si seuls les enregistrements d’une autre exécution apparaissent dans `--setup-stall-s`, l’étape se termine avec `start_failed` et `JOB_ALREADY_EXISTS`. Sans `--resident`, un tel enregistrement est considéré comme une tentative de surveillance ayant échoué.
- Une tentative avant qu’aucun enregistrement n’ait été vu (le délai d’attente, par exemple, pendant qu’une autre exécution maintient `--job`) n’interrompt rien : la cellule d’arrêt répond avec `CR_NOT_OURS`, et ensuite, le `<job>.run.out` de l’autre exécution n’est pas non plus collecté.
- Un nouveau démarrage de la même exécution (dont la réponse a été perdue) n’est pas refusé.
- En mode `--resident`, un `CR_RESIDENT_MAX_S` non fini ou non positif transmis via `--env` est refusé immédiatement (`RESIDENT_MAX_INVALID`, sortie 1).

**Codes de sortie** (convention de l’organisation), pour `run` et `stop --out-dir D` :

| Sortie | Signification | `run` | `stop` |
| --- | --- | --- | --- |
| 0 | ok | `done`, et l’arrêt confirmé | un arrêt confirmé, ou rien à arrêter |
| 1 | erreur utilisateur | un refus (`REFUSED: …`) ou une erreur d’utilisation, avant que le marqueur de session n’ait été écrit | un marqueur qui ne nomme aucune session (`SESSION_MARKER_INVALID`), ou une erreur d’utilisation |
| 2 | erreur d’exécution | toute sortie autre que `done` (quel que soit son arrêt), ou `RUN_ERROR` | un arrêt qui n’a pas été confirmé, ou `STOP_ERROR` |
| 3 | succès partiel | `done`, mais l’arrêt n’a pas été confirmé : `session_open.json` est toujours présent | (non utilisé) |

Le 3, le résultat du travail se trouve dans le répertoire de sortie, et la session doit encore être arrêtée avec `python -m colab_runner stop --out-dir D`.

Un refus après le marqueur n’est pas une sortie 1. Un `--job` réutilisé, par exemple, entraîne une sortie 2 : l’exécuteur a alors pris le contrôle de la session, l’a donc arrêtée, et la dernière ligne est `RUN_END reason=start_failed … code=JOB_ALREADY_EXISTS`, avec une indication indiquant `--job` sur stderr. Aucune ligne `REFUSED:` n’est écrite.

**Refus et erreurs** : ils comportent un code, un message et une indication, et chaque ligne est nettoyée :
- Un refus écrit `REFUSED: CODE: message` comme première ligne sur stderr, puis `hint: …` (ce qu’il faut modifier).
- Une erreur pendant une exécution écrit `RUN_ERROR: CODE: message`, puis `hint: …`. Le code est le code de l’erreur elle-même (`BINARY_DOWNLOAD_TOKEN`, `QUARANTINE_ESCAPE`), ou `UNEXPECTED_ERROR`, avec le type de l’exception dans le message. `stop` écrit `STOP_ERROR: …` de la même manière.
- Une exécution qui se termine par un refus nommé après le marqueur (`JOB_ALREADY_EXISTS`, `BINARY_DOWNLOAD_TOKEN`, `QUARANTINE_ESCAPE`) l’enregistre dans `runner_result.json` sous la forme `refusal: {code, message, hint, retryable}`. `retryable` est faux pour chaque refus de l’entrée de l’appelant : le même appel échoue de la même manière.
- Chaque code et son indication se trouvent dans `colab_runner/errors.py` (`CODES`).

**Niveaux de journalisation** (`run` et `stop`) : `--quiet` n’écrit que les lignes de fin (`RUN_END`, `STOP_END` ou `STOP_NOTHING`, et un refus ou une erreur avec son indication). Par défaut, il ajoute le journal de l’exécuteur sur stderr. `--verbose` ajoute une ligne par sondage. `--debug` ajoute la trace d’une erreur. Un jeton est nettoyé à chaque niveau, y compris la trace.

**Comment une exécution se termine :** `done`, `failed` (sortie non nulle), `oom`, `wall`, `cap`, `setup_stall`, `progress_stall`, `dead`, `unreachable`, `start_failed`, `interrupted` ou `error`.
- Toute fin, sauf `error`, écrit `RUN_END reason=… rc=… stopped=…` comme dernière ligne sur stdout, avec ` code=CODE` ajouté lorsque l’exécution s’est terminée par un refus nommé. Une indication suit sur stderr pour un tel refus, et pour un arrêt qui n’a pas été confirmé.
- Une fin `error` écrit `RUN_ERROR: …` sur stderr et aucune ligne `RUN_END` ; lorsque `session_open.json` est toujours dans le répertoire de sortie, l’indication pour un arrêt qui n’a pas été confirmé suit. Un refus ou une erreur d’utilisation (sortie 1) n’écrit que sur stderr.
- `runner_result.json` dans le répertoire de sortie enregistre chaque fin après que le marqueur a été écrit, y compris `error`. Un téléchargement binaire refusé pendant le nettoyage transforme la fin en `error` et conserve la fin qu’il a remplacée dans `prior_reason` (et dans les détails).

**Ce que `run` écrit dans le répertoire de sortie** (`--job` a par défaut `job`, `--content` a par défaut `/content`) :
- `session_open.json` pendant que la session peut être ouverte ;
- `runner_result.json`, la fin de l’exécution. Son `quarantined` répertorie chaque téléchargement binaire mis en quarantaine pendant l’exécution, et une quarantaine d’un `Runner.download` ultérieur via le même `Runner` réécrit le fichier avec.
- `<job>.run.out`, le journal du travail (n’apparaît que lorsque le travail a écrit des lignes de journal, ou une fois que `--resident` l’a collecté) ;
- avec `--resident`, `<job>.poll.json` (le dernier enregistrement de sondage du travail, tel que téléchargé) et `<job>.refused.json`, le propre enregistrement de refus de cette exécution, lorsque sa cellule résidente refusée en a écrit un (un `JOB_ALREADY_EXISTS` qui n’a expiré que sur les enregistrements d’une autre exécution n’en a pas).
- `stop_confirmed.json` après un arrêt confirmé, ou `stop_pending.json` après un arrêt ayant échoué.

**La commande `colab`** est `colab`, sauf si `COLAB_RUNNER_CLI` indique une autre ligne de commande. Elle est divisée avec `shlex.split`, qui utilise les guillemets POSIX : sous Windows, écrivez le chemin avec des barres obliques ou mettez-le entre guillemets (`"C:\tools\colab.exe"`), car une barre oblique inversée non entre guillemets est supprimée. Une commande qui ne peut pas être divisée (une guillemet non fermée) est refusée (`CLI_INVALID`, sortie 1).

**Ce que le travail doit écrire, afin que les gardes d’attente puissent le voir.** Les chemins se trouvent sur la VM ; le répertoire de sortie est `CR_OUT_DIR`, par défaut `<content>/<job>_out`.
- `phase.txt` : absent, vide ou `setup` pendant l’installation, tout autre chose une fois l’exécution lancée. Les travaux en attente sautent la phase de configuration.
- `setup.log` : pendant la configuration, une exécution dont le `setup.log` cesse de croître pendant `--setup-stall-s` (par défaut 600) se termine par `setup_stall`.
- **Progression :** après la configuration, une exécution se termine par `progress_stall` lorsque rien de ce qu’elle peut voir ne bouge pendant `--progress-stall-s` (par défaut 900). Ce qu’elle voit est la somme du premier entier de chaque fichier correspondant à `CR_PROGRESS_GLOB` (par défaut `<out>/progress/*`), le nombre de fichiers correspondant à `CR_UNITS_GLOB` et les pulsations d’attente.
- Une exécution sans sondage valide pendant `--unreachable-polls` sondages consécutifs (par défaut 10), ou pendant `--unreachable-s` (par défaut 1800), se termine par `unreachable`.

**Secrets et suppression des données lors du téléchargement, en tenant compte des fichiers binaires.** Passez un secret en tant que `--env NAME`, en définissant NAME dans l’environnement du processus. `--env NAME=VALUE`
place la valeur dans la ligne de commande du processus. Les valeurs sont transmises à la VM à l’intérieur du fichier de cellule, et non en tant qu’arguments de l’interface de ligne de commande `colab`.
Chaque ligne de sortie et chaque ligne d’erreur de l’interface de ligne de commande sont nettoyées à l’aide de la fonction `scrub_colab_tokens` de la bibliothèque fournie. Les fichiers et
répertoires téléchargés (y compris les transferts partiels) sont classés uniquement en fonction de leur contenu (un octet NUL ou un échec de décodage strict UTF-8 indique un fichier binaire) :
- Les fichiers texte sont nettoyés directement à l’aide de `scrub_colab_tokens`.
- Les fichiers binaires propres restent identiques, octet par octet. Les fichiers illisibles sont supprimés.
- **La règle pour les fichiers binaires.** Un fichier binaire est analysé à l’aide de paires d’octets des modèles de jetons de la bibliothèque.
- Pour UTF-8, les paires correspondent exactement à ce que les modèles de texte correspondent dans le texte décodé. Chaque caractère que `\s` de Python correspond
est comptabilisé comme un espace, y compris `\x1c`-`\x1f`, NBSP, NEL et EM SPACE.
- La seule différence se trouve à côté d’une lettre non ASCII, où la paire `hf_` correspond et le modèle de texte ne correspond pas : plus,
jamais moins.
- Une règle est plus stricte que pour le texte : dans un fichier binaire, `Bearer` est comptabilisé uniquement lorsque la valeur qui le suit a la forme d’un jeton. Cela signifie
au moins 20 caractères de `[A-Za-z0-9._-]`, en utilisant au moins deux minuscules, majuscules et chiffres.
- Ainsi, un rendu dont l’invite indique « Porteur de la flamme du Nord » est transmis tel quel, octet par octet. Un véritable jeton (un jeton d’accès OAuth,
un JWT, un jeton Hugging Face) est détecté.
- Les fichiers texte conservent les modèles de la bibliothèque inchangés : le même texte dans un fichier texte devient toujours `Bearer [REDACTED] the
North`.
- **Une correspondance n’est jamais réécrite.**
- Le fichier est déplacé tel quel vers la zone de quarantaine, enregistré et nommé dans `runner_result.json` (`quarantined`) uniquement par le chemin et le nom du modèle.
- Le téléchargement est refusé avec `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`). Cela met fin à `Runner.run` en tant que `error` (sortie de l’interface de ligne de commande 2), et la session est toujours arrêtée.
- **Le téléchargement d’un répertoire** est nettoyé fichier par fichier, et un échec ne stoppe jamais le reste : une correspondance dont le déplacement vers la zone de quarantaine
échoue est supprimée, et un sous-répertoire qui ne peut pas être listé est noté. La première erreur de ce type est signalée une fois que chaque autre
fichier a été nettoyé. Seuls les fichiers réguliers sont lus. Un lien symbolique, vers un fichier ou un répertoire, est supprimé, jamais suivi, et tout autre élément qui n’est pas un fichier ou un répertoire régulier (FIFO, socket, périphérique) est supprimé, jamais
lu.
- **Où se trouve la zone de quarantaine.**
- Grâce à `Runner` (et donc `run`), il s’agit du répertoire frère `<out-dir>.quarantine/`. Il se trouve à l’extérieur de `--out-dir`, de sorte que
ni `salvage` ni le regroupement out-dir ne le prennent en compte.
- Un simple `Colab.download` ou `scrub_local` sans `quarantine_dir` déplace le fichier vers un nouveau dossier
`colab-runner-quarantine-*` dans le répertoire temporaire du système (`tempfile.gettempdir()`). Il ne se trouve jamais à côté du fichier, donc
jamais à l’intérieur du dossier dans lequel le fichier a été téléchargé. Son chemin se trouve dans `BinaryDownloadRefused.quarantined`. Passez
`Colab(quarantine_dir=...)` pour choisir l’emplacement.
- Chaque chemin de quarantaine est résolu et vérifié pour s’assurer qu’il se trouve à l’intérieur de son dossier de quarantaine avant le déplacement. Un fichier qui
se trouverait à l’extérieur est supprimé à la place, et le téléchargement échoue avec `QuarantineEscape` (`QUARANTINE_ESCAPE`).
- Le processus ne supprime jamais une zone de quarantaine ; **le processus doit la supprimer et ne jamais la transmettre.**

**Salvage** est uniquement un hook d’API Python : `Runner.run(..., salvage=callable)` l’exécute à chaque fin, sauf `done`, avant l’arrêt.
L’interface de ligne de commande ne dispose pas de fonction de récupération.

**Tarification.** La fenêtre est tarifée à `max(--max-rate, the highest stable window rate)` fois le temps écoulé, à l’aide des fonctions de taux et de limite de la bibliothèque fournie. Un taux stable supérieur à `--max-rate` déclenche `RATE_ABOVE_MAX`, de sorte que chaque fenêtre qui continue de fonctionner est
tarifée à `--max-rate` : il s’agit d’un seuil, et non d’un plafond flexible. Définissez-le sur le taux attendu de la carte ; un seuil flexible `--max-rate` déclenche la limite
plus tôt. Le solde du compte est enregistré et n’affecte jamais le prix de la fenêtre.

## Sécurité et modèle de menace

**Ce que cela affecte.**
- La session Colab que le processus nomme : il télécharge les fichiers de cellule, démarre et arrête le travail qu’il a démarré, télécharge les fichiers
pour lesquels il a été invité et arrête la session. Il ne signale à aucun autre processus qu’un autre processus a démarré (`CR_NOT_OURS`).
- Le répertoire de sortie (`--out-dir`) et son répertoire frère de quarantaine `<out-dir>.quarantine/` sur la machine. Un simple `Colab.download`
sans répertoire de quarantaine utilise un nouveau dossier `colab-runner-quarantine-*` dans le répertoire temporaire du système.
- Les variables d’environnement que le processus nomme avec `--env`, qu’il transmet à la VM à l’intérieur du fichier de cellule.

**Ce que cela n’affecte pas.** Aucun fichier en dehors du répertoire de sortie, de sa zone de quarantaine et de la zone de quarantaine temporaire. Aucune autre session Colab. Aucun paramètre de compte, aucune information d’identification ou aucune facturation : il lit le taux et le solde du compte via l’interface de ligne de commande `colab` et ne les modifie jamais.

**Réseau.** Uniquement via l’interface de ligne de commande `colab`, vers Google Colab. colab-runner n’ouvre aucune connexion de son propre chef.

**Secrets.** Un secret transmis en tant que `--env NAME` ne figure sur aucune ligne de commande. Chaque ligne de sortie, chaque ligne d’erreur, chaque trace et chaque fichier texte téléchargé sont nettoyés pour supprimer les jetons ; un fichier binaire contenant un jeton est placé dans la zone de quarantaine tel quel et le
téléchargement est refusé (ci-dessus). Le processus supprime la zone de quarantaine et ne la transmet jamais.

**Autorisations.** Il s’exécute en tant qu’utilisateur qui l’exécute, avec la connexion `colab` que cet utilisateur a déjà. Il n’a besoin d’aucune élévation.

**Aucune télémétrie.** colab-runner ne collecte ni n’envoie aucune donnée.

Signaler une vulnérabilité : voir [SECURITY.md](SECURITY.md).

## Ce qui n’est pas inclus ici

Aucun nom de bucket, aucun nom de compte, aucun wrapper de lancement ou aucun chemin d’accès local. Un appelant transmet ces éléments via un indicateur ou
une variable d’environnement ; les tests utilisent des espaces réservés.

## Vérifier

```bash
bash verify.sh
```

## Licence

MIT. Voir [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
