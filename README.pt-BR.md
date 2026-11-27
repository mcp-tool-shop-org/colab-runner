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

Execute um script em uma GPU Google Colab paga a partir da sua própria máquina, com um limite de gastos definido, e receba os resultados. Você descreve a tarefa em um pequeno arquivo; o colab-runner abre a sessão, executa a tarefa, traz os resultados, encerra a sessão e registra o custo.

## O que ele faz

- **Um arquivo de especificação por tarefa.** Entradas, comandos de configuração e execução, saídas, tipo de GPU, limite de gastos e limite de tempo, tudo em `colab-window.toml`. Sem scripts de execução por tarefa.
- **Um limite de gastos que você define.** O custo é calculado como taxa × tempo decorrido na sua máquina. Quando atinge o limite ou o limite de tempo, a tarefa é interrompida e a sessão é encerrada.
- **Teste antes de pagar.** `--dry` verifica a especificação sem fazer chamadas ao Colab. `--local-cpu` executa a tarefa inteira na sua máquina, da mesma forma que seria executada na VM.
- **A sessão é sempre interrompida.** Todas as formas de término de uma execução tentam interromper a sessão. Se a sua máquina falhar durante a execução, `stop --out-dir` a encerrará posteriormente.
- **Os tokens são removidos dos resultados.** Os logs e os arquivos baixados são limpos para remover os tokens de acesso antes de serem salvos.
- **Um registro de cada execução.** `outcome.json` na pasta de saída indica como a execução terminou e qual foi o custo.

## Instalação

O colab-runner não está em PyPI. Instale-o a partir de uma tag de lançamento:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.2.0"
```

Você precisa de Linux ou WSL, Python 3.11 ou posterior e a ferramenta de linha de comando `colab` do Google, com a conta que fará o pagamento. O colab-runner não tem outras dependências.

## Início rápido

Trabalhe dentro do repositório Git do seu projeto: os arquivos fora dele são considerados dados protegidos e são rejeitados. Escreva uma especificação (uma especificação completa está em [examples/merchant_league_seats](examples/merchant_league_seats/README.md)):

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

`--local-cpu` executa os comandos de configuração e de tarefa na sua máquina, portanto, precisa de tudo o que eles solicitam (aqui, pip:
`sudo apt install python3-pip` em um Ubuntu ou WSL recém-instalado). Em seguida, teste-o, simule-o e execute-o:

```bash
python -m colab_runner launch --spec colab-window.toml --out-dir runs/try --dry
python -m colab_runner launch --spec colab-window.toml --out-dir runs/cpu --local-cpu
python -m colab_runner launch --spec colab-window.toml --out-dir runs/w1 --cap 5 --max-rate 2.5
python -m colab_runner status --out-dir runs/w1
```

Os resultados serão salvos em `runs/w1/collected/`, com `outcome.json` ao lado deles. Para uma execução real, são necessários `--cap` e `--max-rate` na linha de comando, portanto, uma execução paga é sempre uma escolha deliberada.

## Quando uma execução termina

| Saída | Significado |
| --- | --- |
| 0 | A tarefa foi concluída, os resultados foram recebidos e a sessão foi encerrada. |
| 1 | Recusado antes que algo fosse aberto: corrija a especificação ou os parâmetros (a mensagem indica o que). |
| 2 | A execução terminou prematuramente (a tarefa falhou, atingiu o limite de gastos, o limite de tempo, travou ou ficou inacessível). A sessão é encerrada; se o encerramento falhar, a mensagem indica para executar `stop`. |
| 3 | A tarefa foi concluída, mas o encerramento não foi confirmado. Execute `python -m colab_runner stop --out-dir D`. |

Cada recusa e erro imprime um código, uma mensagem e uma dica. `python -m colab_runner --help` lista os comandos e `launch --help` todos os parâmetros.

**Mais detalhes:** anexar a uma sessão que você abriu, o que a tarefa deve escrever para que o progresso seja visível, como os downloads são limpos e a precificação, tudo está em [docs/reference.md](docs/reference.md).

## Segurança e modelo de ameaças

**O que ele afeta.**
- A sessão do Colab que você define: ele carrega arquivos de células, inicia e encerra a tarefa que iniciou, baixa os arquivos solicitados e encerra a sessão. Ele nunca envia um sinal para um processo que pertence a outra execução do colab-runner (`CR_NOT_OURS`).
- O diretório de saída (`--out-dir`) e seu diretório irmão de quarentena `<out-dir>.quarantine/` na sua máquina. Um diretório `Colab.download` sem um diretório de quarentena usa uma nova pasta `colab-runner-quarantine-*` sob o diretório temporário do sistema.
- As variáveis de ambiente que você define com `--env`, que ele passa para a VM dentro do arquivo de célula.

**O que ele não acessa.** Nenhum arquivo fora do diretório de saída, sua quarentena e a quarentena temporária. Nenhuma outra sessão do Colab. Nenhuma configuração de conta, credenciais ou faturamento: ele lê a taxa e o saldo da conta por meio da ferramenta de linha de comando `colab` e nunca os altera.

**Rede.** Apenas por meio da ferramenta de linha de comando `colab`, para o Google Colab. O colab-runner não abre nenhuma conexão por conta própria.

**Segredos.** Um segredo passado como `--env NAME` não aparece em nenhuma linha de comando. Cada linha de saída, linha de erro, rastreamento e arquivo de texto baixado são limpos de tokens; um arquivo binário com um token é colocado em quarentena sem alterações e o download é rejeitado (acima). O colab-runner nunca exclui a quarentena: você deve excluí-la e nunca enviá-la.

**Permissões.** Ele é executado como o usuário que o executa, com a autenticação `colab` que esse usuário já possui. Não precisa de privilégios elevados.

**Sem telemetria.** O colab-runner não coleta nem envia nada.

Para relatar uma vulnerabilidade, consulte [SECURITY.md](SECURITY.md).

## O que não está aqui

Nenhum nome de bucket, nome de conta, wrapper de execução ou caminho local. A tarefa que chama passa esses dados por meio de um parâmetro ou variável de ambiente; os testes usam espaços reservados.

## Verificação

```bash
bash verify.sh
```

## Licença

MIT. Consulte [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
