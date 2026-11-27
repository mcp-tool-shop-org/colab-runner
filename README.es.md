<p align="center">
  <a href="README.ja.md">日本語</a> | <a href="README.zh.md">中文</a> | <a href="README.md">English</a> | <a href="README.fr.md">Français</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.pt-BR.md">Português (BR)</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/mcp-tool-shop-org/brand/main/logos/colab-runner/readme.png" alt="Colab Runner" width="400">
</p>

<p align="center">
  <a href="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml"><img src="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://mcp-tool-shop-org.github.io/colab-runner/"><img src="https://img.shields.io/badge/Landing_Page-live-blue" alt="Landing Page"></a>
</p>

Ejecute un script en una GPU de Google Colab de pago desde su propia máquina, con un límite de gasto establecido y obtenga los resultados. Usted describe el trabajo en un archivo pequeño; colab-runner abre la sesión, ejecuta el trabajo, trae los resultados, detiene la sesión y registra el costo.

## Qué hace

- **Un archivo de especificaciones por tarea.** Entradas, comandos de configuración y ejecución, salidas, tipo de GPU, límite de gasto y límite de tiempo, todo en
`colab-window.toml`. No se utilizan scripts de inicio por tarea.
- **Un límite de gasto que usted establece.** El costo se calcula como tarifa × tiempo transcurrido en su máquina. Cuando se alcanza el límite
o el límite de tiempo, la tarea se detiene y la sesión se cierra.
- **Pruébelo antes de pagar.** `--dry` verifica las especificaciones sin realizar llamadas a Colab. `--local-cpu` ejecuta toda la tarea en su
máquina, de la misma manera que se ejecuta en la máquina virtual.
- **La sesión siempre se detiene.** Cualquier forma en que pueda finalizar una ejecución intenta detener la sesión. Si su máquina falla
durante la ejecución, `stop --out-dir` la cierra más tarde.
- **Los tokens no se incluyen en los resultados.** Los registros y los archivos descargados se eliminan para eliminar los tokens de acceso antes de guardarlos.
- **Se registra cada ejecución.** `outcome.json` en la carpeta de salida indica cómo finalizó la ejecución y cuál fue su costo.
- **La publicación es suya.** colab-runner nunca sube nada. Para compartir los resultados, utilice `hf upload` de Hugging Face
(consulte [docs/reference.md](docs/reference.md)).

## Instalación

colab-runner no está en PyPI. Instálelo desde una etiqueta de lanzamiento:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.2.0"
```

Necesita Linux o WSL, Python 3.11 o posterior, y la herramienta de línea de comandos `colab` de Google, con la sesión iniciada en la cuenta que realizará el pago. colab-runner no tiene otras dependencias.

## Inicio rápido

Trabaje dentro del repositorio git de su proyecto: los archivos que estén fuera de él se consideran datos protegidos y se rechazarán. Escriba una especificación (una especificación completa se encuentra en [examples/merchant_league_seats](examples/merchant_league_seats/README.md)):

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

`--local-cpu` ejecuta los comandos de configuración y de trabajo en su máquina, por lo que necesita todo lo que estos comandos requieran (aquí, pip:
`sudo apt install python3-pip` en un Ubuntu o WSL nuevo). Luego, verifíquelo, pruébelo y ejecútelo:

```bash
python -m colab_runner launch --spec colab-window.toml --out-dir runs/try --dry
python -m colab_runner launch --spec colab-window.toml --out-dir runs/cpu --local-cpu
python -m colab_runner launch --spec colab-window.toml --out-dir runs/w1 --cap 5 --max-rate 2.5
python -m colab_runner status --out-dir runs/w1
```

Los resultados se guardan en `runs/w1/collected/`, con `outcome.json` junto a ellos. Para un lanzamiento en vivo, se necesitan `--cap` y `--max-rate` en la línea de comandos, por lo que una ejecución de pago siempre es una elección deliberada.

## Cuando termina una ejecución

| Salida | Significado |
| --- | --- |
| 0 | El trabajo se completó, los resultados se recuperaron y la sesión se detuvo. |
| 1 | Se rechazó antes de que se abriera algo: corrija la especificación o las opciones (el mensaje indica qué). |
| 2 | La ejecución terminó antes de tiempo (el trabajo falló, se alcanzó el límite, el límite de tiempo, se detuvo o no se pudo acceder). La sesión se detiene; si la detención falló, el mensaje indica que se ejecute `stop`. |
| 3 | El trabajo se completó, pero la detención no se confirmó. Ejecute `python -m colab_runner stop --out-dir D`. |

Cada rechazo y error imprime un código, un mensaje y una sugerencia. `python -m colab_runner --help` enumera los comandos y `launch --help` todas las opciones.

**Más detalles:** adjuntar a una sesión que usted abrió, qué debe escribir el trabajo para que el progreso sea visible, cómo se eliminan las descargas y los precios, todo esto se encuentra en [docs/reference.md](docs/reference.md).

## Seguridad y modelo de amenazas

**Qué afecta.**
- La sesión de Colab que especifique: carga los archivos de las celdas, inicia y detiene el trabajo que inició, descarga los archivos que se le solicitaron y detiene la sesión. Nunca envía una señal a un proceso que pertenezca a otra ejecución de colab-runner (`CR_NOT_OURS`).
- El directorio de salida (`--out-dir`) y su directorio de cuarentena hermano `<out-dir>.quarantine/` en su máquina. Un directorio `Colab.download` sin directorio de cuarentena utiliza una nueva carpeta `colab-runner-quarantine-*` dentro del directorio temporal del sistema.
- Las variables de entorno que especifique con `--env`, que se pasan a la máquina virtual dentro del archivo de la celda.

**Qué no toca.** Ningún archivo fuera del directorio "out", su cuarentena y la cuarentena temporal. Ninguna otra sesión de Colab. Ninguna configuración de cuenta, credenciales o facturación: lee la tarifa y el saldo de la cuenta a través de la herramienta de línea de comandos `colab` y nunca los cambia.

**Red.** Solo a través de la herramienta de línea de comandos `colab`, a Google Colab. colab-runner no abre ninguna conexión por sí solo.

**Secretos.** Un secreto que se pasa como `--env NAME` no aparecerá en ninguna línea de comando. Cada línea de salida, cada línea de error, cada rastreo de pila y cada archivo de texto descargado se eliminan para eliminar los tokens; un archivo binario que contenga un token se pone en cuarentena sin cambios y la descarga se rechaza (arriba). colab-runner nunca elimina la cuarentena: usted debe eliminarla y nunca enviarla.

**Permisos.** Se ejecuta como el usuario que lo ejecuta, con la sesión `colab` en la que ese usuario ya ha iniciado sesión. No necesita elevación de privilegios.

**No hay telemetría.** colab-runner no recopila ni envía nada.

Para informar sobre una vulnerabilidad, consulte [SECURITY.md](SECURITY.md).

## Qué no está aquí

Ningún nombre de bucket, nombre de cuenta, scripts de inicio o rutas locales. El programa que lo llama pasa esos datos mediante opciones o variables de entorno; las pruebas utilizan marcadores de posición.

## Verificación

```bash
bash verify.sh
```

## Licencia

MIT. Consulte [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
