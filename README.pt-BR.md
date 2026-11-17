<p align="center">
  <a href="README.ja.md">日本語</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.md">English</a>
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

O executor compartilhado do estúdio para instâncias pagas do Google Colab: um local para o código que cada instância do Colab usa.
Ele inicia um trabalho em uma VM do Colab em um processo que perdura além do tempo de execução, monitora-o
dentro de um limite de gastos, remove tokens de tudo o que traz de volta e sempre tenta interromper a sessão.

**Status: 0.1.0.** O executor independente (item 1 abaixo) foi criado. Os itens 2 a 5 ainda não foram criados.

1. **O executor independente (0.1.0).** Inicia um trabalho na VM com um arquivo de status, verifica-o com repetição, um modo de "heartbeat" `--hold-s`
e um sinalizador que alterna para um loop residente no kernel. A recuperação é um hook da API Python (abaixo).
2. **Proteções de inicialização e placa de vídeo.** Uma verificação da placa de vídeo no primeiro passo da VM (capacidade de computação e VRAM por tipo de placa,
rejeitando `CARD_MISMATCH`), um log de rejeição do lado da instância com um disjuntor por placa
(`CARD_CIRCUIT_OPEN`) e `OMP_NUM_THREADS` definido antes do carregamento do PyTorch.
3. **Segredos.** O caminho do arquivo de token: um arquivo 0600, lido apenas pelo processo que precisa dele, apagado na
VM, removido de todos os logs armazenados.
4. **Um módulo do Hugging Face Storage Bucket.** Sincroniza as saídas para um prefixo de armazenamento por instância, verifica as contagens e
sha256 em relação a um manifesto e, em seguida, grava um recibo `done.json`. Nunca exclui.
5. **`PLATFORM.md` e `facts.json`.** O que a plataforma Colab realmente faz, cada fato datado e com fonte,
para que um kit verifique suas suposições em relação a eles e uma suposição incorreta se torne um código de rejeição, e não uma surpresa paga.

As instâncias usam uma versão fixa deste repositório, uma instância por solicitação de pull.

## Instalação

colab-runner não está em PyPI. Uma instância usa uma versão fixa ou a instala a partir da tag:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.1.0"
```

- **Python** 3.11 ou posterior. O pacote não tem dependências de tempo de execução.
- **A linha de comando `colab`** (a CLI do Google Colab), conectado à conta que paga pela instância. colab-runner
a chama; não a instala nem faz login por você. `COLAB_RUNNER_CLI` aponta para outra linha de comando (abaixo).
- **Plataformas:** O CI é executado no Linux com Python 3.11 e 3.12. O estúdio também o executa no Windows.

## Início rápido

A instância abre a sessão e, em seguida, a entrega para `run`, que a encerra interrompendo-a:

```bash
colab new -s my-window            # the lane's own step: open the session (plus the card's flags)
python -m colab_runner run --session my-window --out-dir runs/w1   --script /content/train.sh --wall-s 3600   --max-rate 13.5 --cap 15 --cleanup-bound-s 600
echo "exit $?"                    # 0 done and stopped; see the exit codes below
```

As figuras são espaços reservados. Defina `--max-rate` para as unidades esperadas da placa por hora e `--cap` para o orçamento da instância
em unidades de computação (veja Preços).

`python -m colab_runner --help` lista todos os comandos; `run --help` lista todos os sinalizadores.

## Usando-o em uma instância (0.1.0)

**A sessão pertence à instância.** A instância abre a sessão do Colab e passa seu nome. Depois que `run` gravar
`session_open.json` no diretório de saída, todas as saídas tentarão interromper a sessão, e o marcador será removido somente após uma interrupção confirmada.
- **Um marcador que permanece** significa que a sessão ainda pode estar aberta: uma interrupção que falhou ou um `run` que foi interrompido abruptamente (SIGKILL, uma instância perdida) antes que sua limpeza fosse executada. A instância então executa `python -m colab_runner stop --out-dir D`.
- **Antes que o marcador exista** (uma rejeição ou um erro de uso, ambos com saída 1), a instância executa `colab stop -s S`.

**Flags obrigatórios de `run`:** `--session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C --cleanup-bound-s N`.
- `--out-dir` não deve existir ainda, ou estar vazio (`OUT_DIR_NOT_EMPTY`, caso contrário). Um diretório raiz do sistema de arquivos é recusado (`OUT_DIR_INVALID`): a área de quarentena é um diretório irmão do diretório de saída, nomeado a partir de seu caminho resolvido. O mesmo se aplica a um diretório de saída que não pode ser criado ou listado.
- `--cleanup-bound-s` deve ser igual ou maior que o limite de limpeza do executor (`CLEANUP_BOUND_TOO_SMALL`, caso contrário). Nas chamadas com tempo limite padrão, esse limite é de 466 s, ou 986 s com `--resident` (`Runner.own_cleanup_bound_s`).
- Passe `--pre-open-rate` e `--pre-open-assignments` juntos, logo antes de a sessão ser aberta, ou nenhum dos dois. Sem eles, toda a taxa da conta é atribuída a esta janela, de modo que uma sessão vizinha pode exceder o limite de taxa.
- Passe um `--job` distinto (padrão `job`) para cada etapa que compartilhe um diretório `--content`. Como uma `--job` reutilizada é recusada:
- Cada execução tem seu próprio ID de execução, `CR_RUN_ID`, um novo a cada vez. O nome é reservado: `--env CR_RUN_ID=…` (ou `CR_RUN_ID` em `Runner.run(env=...)`) é recusado (`RUN_ID_RESERVED`), pois o uso de um mesmo ID em diferentes etapas desativaria essa recusa entre elas.
- A VM grava o ID de execução em `<job>.run_id` antes que o trabalho comece. O arquivo de status do trabalho e cada registro de sondagem contêm esse ID.
- Os arquivos do trabalho são `<job>.pid`, `.status.json`, `.run.out`, `.poll.json` e `.run_id`. Se algum deles estiver presente sob outro ID de execução (ou sob nenhum), a etapa termina `start_failed` com `JOB_ALREADY_EXISTS`. Isso se mantém, independentemente de o processo do outro trabalho estar ativo ou inativo, e com ou sem um arquivo de status.
- Uma etapa recusada não acessa nenhum dos arquivos da outra execução, nunca interrompe seu trabalho e ainda assim interrompe sua própria sessão. No modo `--resident`, a recusa é direcionada para `<job>.refused.json`, nunca para `<job>.poll.json`.
- O executor coleta apenas os registros que contêm seu próprio ID de execução. Um registro que outra execução deixou no mesmo `--job` (o `<job>.poll.json` final da etapa 1, por exemplo) nunca termina uma etapa. No modo `--resident`, se apenas os registros de outra execução aparecerem dentro de `--setup-stall-s`, a etapa termina `start_failed` com `JOB_ALREADY_EXISTS`. Sem `--resident`, esse registro é considerado uma sondagem com falha.
- Uma interrupção antes que qualquer registro seja visto (o tempo limite, por exemplo, enquanto o trabalho de outra execução mantém o `--job`) ainda não interrompe nada: a célula de interrupção responde `CR_NOT_OURS` e, em seguida, o `<job>.run.out` da outra execução também não é coletado.
- Uma nova tentativa de início da mesma execução (aquela cuja resposta foi perdida) não é recusada.
- No modo `--resident`, um `CR_RESIDENT_MAX_S` não finito ou não positivo passado via `--env` é recusado imediatamente (`RESIDENT_MAX_INVALID`, saída 1).

**Códigos de saída** (a convenção da organização), para `run` e `stop --out-dir D`:

| Saída | Significado | `run` | `stop` |
| --- | --- | --- | --- |
| 0 | ok | `done`, e a interrupção confirmada | uma interrupção confirmada ou nada para interromper |
| 1 | erro do usuário | uma rejeição (`REFUSED: …`) ou um erro de uso, antes que o marcador da sessão fosse gravado | um marcador que não nomeia nenhuma sessão (`SESSION_MARKER_INVALID`) ou um erro de uso |
| 2 | erro de tempo de execução | qualquer finalização diferente de `done` (independentemente do que sua interrupção fez) ou `RUN_ERROR` | uma interrupção que não foi confirmada ou `STOP_ERROR` |
| 3 | sucesso parcial | `done`, mas a interrupção não foi confirmada: `session_open.json` ainda está lá | (não usado) |

Na saída 3, o resultado do trabalho está no diretório de saída e a sessão ainda deve ser interrompida com `python -m colab_runner stop --out-dir D`.

Uma recusa após o marcador não é uma saída 1. Um `--job` reutilizado, por exemplo, resulta em uma saída 2: o executor estava responsável pela sessão naquele momento, então a interrompeu, e a última linha é `RUN_END reason=start_failed … code=JOB_ALREADY_EXISTS` com uma dica que indica
`--job` no stderr. Nenhuma linha `REFUSED:` é escrita.

**Recusas e erros** têm um código, uma mensagem e uma dica, e cada linha é limpa:
- Uma recusa escreve `REFUSED: CODE: message` como a primeira linha no stderr, seguida por `hint: …` (o que deve ser alterado).
- Um erro durante uma execução escreve `RUN_ERROR: CODE: message`, seguido por `hint: …`. O código é o próprio código do erro
(`BINARY_DOWNLOAD_TOKEN`, `QUARANTINE_ESCAPE`), ou `UNEXPECTED_ERROR` com o tipo da exceção na mensagem. `stop` escreve `STOP_ERROR: …`
da mesma forma.
- Uma execução que termina em uma recusa nomeada após o marcador (`JOB_ALREADY_EXISTS`, `BINARY_DOWNLOAD_TOKEN`,
`QUARANTINE_ESCAPE`) registra isso em
`runner_result.json` como `refusal: {code, message, hint, retryable}`. `retryable` é falso para cada recusa da
entrada do chamador: a mesma chamada falha da mesma forma novamente.
- Cada código e sua dica estão em `colab_runner/errors.py` (`CODES`).

**Níveis de registro** (`run` e `stop`): `--quiet` escreve apenas as linhas finais (`RUN_END`, `STOP_END` ou `STOP_NOTHING`, e uma
recusa ou um erro com sua dica). O padrão adiciona o registro do executor no stderr. `--verbose` adiciona uma linha por consulta. `--debug` adiciona o rastreamento de pilha de um erro. Um token é limpo em cada nível, incluindo o rastreamento de pilha.

**Como uma execução termina:** `done`, `failed` (saída diferente de zero), `oom`, `wall`, `cap`, `setup_stall`, `progress_stall`, `dead`,
`unreachable`, `start_failed`, `interrupted` ou `error`.
- Cada término, exceto `error`, escreve `RUN_END reason=… rc=… stopped=…` como a última linha no stdout, com ` code=CODE` adicionado quando
a execução terminou em uma recusa nomeada. Uma dica é exibida no stderr para essa recusa e para uma interrupção que não foi confirmada.
- Um término `error` escreve `RUN_ERROR: …` no stderr e nenhuma linha `RUN_END`; quando `session_open.json` ainda está no diretório de saída,
a dica para uma interrupção que não foi confirmada é exibida. Uma recusa ou um erro de uso (saída 1) são escritos apenas no stderr.
- `runner_result.json` no diretório de saída registra cada término após a escrita do marcador, incluindo `error`. Um download binário
recusado durante a limpeza transforma o término em `error` e mantém o término que ele substituiu em `prior_reason` (e nos detalhes).

**O que `run` grava no diretório de saída** (`--job` tem como padrão `job`, `--content` tem como padrão `/content`):
- `session_open.json` enquanto a sessão pode estar aberta;
- `runner_result.json`, o fim da execução. Seu `quarantined` lista cada arquivo binário baixado e colocado em quarentena durante a execução, e uma quarentena de uma `Runner.download` posterior, através do mesmo `Runner`, reescreve o arquivo com ele;
- `<job>.run.out`, o log do trabalho (aparece apenas depois que o trabalho escreveu linhas de log, ou depois que `--resident` o coletou);
- quando `--resident` está definido, `<job>.poll.json` (o registro de sondagem mais recente do trabalho, conforme o executor o baixou) e `<job>.refused.json`, o registro de recusa desta execução, quando sua célula residente recusada escreveu um (um `JOB_ALREADY_EXISTS` que apenas excedeu o tempo limite em registros de outra execução não tem nenhum);
- `stop_confirmed.json` após uma parada confirmada, ou `stop_pending.json` após uma falha.

**O comando `colab`** é `colab`, a menos que `COLAB_RUNNER_CLI` especifique outro comando. Ele é dividido com `shlex.split`,
que usa aspas POSIX: no Windows, escreva o caminho com barras ou coloque-o entre aspas (`"C:\tools\colab.exe"`), pois uma
barra invertida não entre aspas é removida. Um comando que não pode ser dividido (uma aspa não fechada) é recusado (`CLI_INVALID`, saída 1).

**O que o trabalho deve gravar, para que os mecanismos de proteção contra interrupções possam vê-lo.** Os caminhos estão na VM; o diretório de saída é `CR_OUT_DIR`, por padrão `<content>/<job>_out`.
- `phase.txt`: ausente, vazio ou `setup` durante a instalação, qualquer outra coisa depois que estiver em execução. Os trabalhos iniciados com `--hold-s` ignoram a fase de configuração.
- `setup.log`: durante a configuração, uma execução cujo `setup.log` para de aumentar por `--setup-stall-s` (padrão 600) termina `setup_stall`.
- **Progresso:** após a configuração, uma execução termina `progress_stall` quando nada que ela possa ver se move por `--progress-stall-s` (padrão 900). O que ela vê é a soma do primeiro inteiro em cada arquivo que corresponda a `CR_PROGRESS_GLOB` (padrão `<out>/progress/*`), a contagem de arquivos que correspondem a `CR_UNITS_GLOB` e os sinais de pulso.
- Uma execução sem uma sondagem válida por `--unreachable-polls` sondagens consecutivas (padrão 10), ou por `--unreachable-s` (padrão 1800), termina `unreachable`.

**Segredos e limpeza de download segura para arquivos binários.** Passe um segredo como `--env NAME`, com NAME definido no ambiente do canal. `--env NAME=VALUE` coloca o valor na linha de comando do executor. Os valores chegam à VM dentro do arquivo da célula do notebook carregado, nunca como argumentos para a CLI `colab`.
Cada linha de saída da CLI e linha de erro é limpa com a biblioteca fornecida `scrub_colab_tokens`. Os arquivos e diretórios baixados (incluindo transferências parciais) são classificados apenas pelo conteúdo (um byte NUL ou uma decodificação estrita com falha UTF-8 é binário):
- Os arquivos de texto são limpos no local com `scrub_colab_tokens`.
- Os arquivos binários limpos permanecem idênticos em termos de bytes. Os arquivos ilegíveis são removidos.
- **A regra binária.** Um arquivo binário é verificado com pares de bytes dos padrões de token da biblioteca.
- Em UTF-8, os pares correspondem exatamente ao que os padrões de texto correspondem no texto decodificado. Cada caractere que `\s` do Python corresponde conta como um espaço, incluindo `\x1c`-`\x1f`, NBSP, NEL e ESPAÇO EM.
- A única diferença é que está ao lado de uma letra não ASCII, onde o par `hf_` corresponde e o padrão de texto não. Portanto, a regra binária pode detectar mais do que a regra de texto, nunca menos.
- Uma regra é mais rigorosa do que o texto: em um arquivo binário, `Bearer` conta apenas quando o valor após ele tem a forma de um token. Isso significa pelo menos 20 caracteres de `[A-Za-z0-9._-]`, usando pelo menos dois de letras minúsculas, maiúsculas e dígitos.
- Portanto, uma renderização cujo prompt diz `Flame Bearer of the North` é transmitida com bytes idênticos. Um token de portador real (um token de acesso OAuth, um JWT, um token do Hugging Face) é detectado.
- Os arquivos de texto mantêm os padrões da biblioteca inalterados: o mesmo texto em um arquivo de texto ainda se torna `Bearer [REDACTED] the North`.
- **Uma correspondência nunca é reescrita.**
- O arquivo é movido inalterado para a quarentena, registrado e nomeado em `runner_result.json` (`quarantined`) apenas pelo caminho e nome do padrão.
- O download é recusado com `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`). Isso encerra `Runner.run` como `error` (saída da CLI 2), e a sessão ainda é interrompida.
- **O download de um diretório** é limpo de tokens, arquivo por arquivo, e uma falha nunca interrompe o restante: uma correspondência cuja movimentação para a quarentena falha é removida, e um subdiretório que não pode ser listado é anotado. O primeiro erro é gerado quando todos os arquivos restantes forem limpos. Apenas os arquivos regulares são lidos. Um link simbólico, para um arquivo ou para um diretório, é removido, nunca seguido, e qualquer outra entrada que não seja um arquivo regular ou um diretório (um FIFO, um socket, um dispositivo) é removida, nunca lida.
- **Onde está a quarentena.**
- Através de `Runner` (e, portanto, `run`), é o diretório irmão `<out-dir>.quarantine/`. Está fora de `--out-dir`, portanto, nem `salvage` nem o agrupamento de out-dir o capturam.
- Um `Colab.download` ou `scrub_local` simples, sem `quarantine_dir`, move o arquivo para uma nova pasta `colab-runner-quarantine-*` sob o diretório temporário do sistema (`tempfile.gettempdir()`). Nunca é colocado ao lado do arquivo, portanto, nunca dentro da pasta em que o arquivo foi baixado. Seu caminho está em `BinaryDownloadRefused.quarantined`. Passe `Colab(quarantine_dir=...)` para escolher o local.
- Cada caminho de quarentena é resolvido e verificado para garantir que permaneça dentro de sua pasta de quarentena antes da movimentação. Um arquivo que seria colocado fora dela é removido em vez disso, e o download falha com `QuarantineEscape` (`QUARANTINE_ESCAPE`).
- O colab-runner nunca exclui uma quarentena. **O canal de chamada deve excluí-la e nunca enviá-la.**

**Salvage** é apenas um hook da API Python: `Runner.run(..., salvage=callable)` o executa em cada final, mas não em `done`, antes da interrupção.
A CLI não tem salvamento.

**Preços.** A janela tem o preço de `max(--max-rate, the highest stable window rate)` vezes o tempo decorrido, através das funções de taxa e limite da biblioteca fornecida. Uma taxa estável acima de `--max-rate` aciona `RATE_ABOVE_MAX`, portanto, cada janela que continua em execução
tem o preço de `--max-rate`: é um limite mínimo, não um limite máximo flexível. Defina-o para a taxa esperada do cartão; um limite flexível `--max-rate` aciona o limite
antecipadamente. O saldo da conta é registrado e nunca define o preço da janela.

## Segurança e modelo de ameaças

**O que ele afeta.**
- A sessão do Colab: carrega arquivos de células, inicia e encerra o processo que iniciou, faz o download dos arquivos solicitados e encerra a sessão. Nunca envia um sinal para um processo que pertença a outra execução do colab-runner (`CR_NOT_OURS`).
- O diretório de saída (`--out-dir`) e o diretório de quarentena irmão `<out-dir>.quarantine/` no sistema. Uma chamada direta a `Colab.download`, sem diretório de quarentena, usa uma nova pasta `colab-runner-quarantine-*` no diretório temporário do sistema.
- As variáveis de ambiente que o processo usa com `--env`, as quais são passadas para a VM dentro do arquivo de célula.

**O que ele não afeta.** Nenhum arquivo fora do diretório de saída, sua quarentena e a quarentena temporária. Nenhuma outra sessão do Colab. Nenhuma configuração de conta, credenciais ou faturamento: ele lê a taxa e o saldo da conta através da linha de comando `colab` e nunca os altera.

**Rede.** Apenas através da linha de comando `colab`, para o Google Colab. colab-runner não abre nenhuma conexão própria.

**Segredos.** Um segredo transmitido como `--env NAME` não aparece em nenhuma linha de comando. Todas as linhas de saída, linhas de erro, rastreamentos e arquivos de texto baixados são limpos, removendo-se os tokens; um arquivo binário que contenha um token é colocado em quarentena sem alterações e o download é recusado (acima). O colab-runner nunca exclui a quarentena: o processo que o invocou deve excluí-la e nunca deve transmiti-la.

**Permissões.** Ele é executado como o usuário que o executa, com o login `colab` que esse usuário já possui. Não precisa de
elevação.

**Sem telemetria.** colab-runner não coleta nem envia nada.

Relatando uma vulnerabilidade: consulte [SECURITY.md](SECURITY.md).

## O que não está aqui

Nenhum nome de bucket, nome de conta, wrappers de inicialização ou caminhos locais. Um chamador passa esses dados por meio de sinalizador ou
variável de ambiente; os testes usam espaços reservados.

## Verificar

```bash
bash verify.sh
```

## Licença

MIT. Consulte [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
