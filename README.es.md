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

# colab-runner

El entorno compartido del estudio para las instancias de Google Colab de pago: un único lugar para el código que utiliza cada instancia de Colab.
Inicia un trabajo en una máquina virtual de Colab en un proceso que perdura más que la instancia, supervisa el proceso
dentro de un límite de gasto, elimina los tokens de todo lo que recupera y siempre intenta detener la sesión.

**Estado: 0.1.0.** El entorno independiente (elemento 1 a continuación) está construido. Los elementos 2 a 5 aún no están construidos.

1. **El entorno independiente (0.1.0).** Inicia un trabajo en la máquina virtual con un archivo de estado, lo supervisa con reintentos, un modo de señalización `--hold-s` y
una marca que cambia a un bucle residente en el kernel. La recuperación es un enlace de API de Python (a continuación).
2. **Protecciones de inicio y tarjeta.** Una comprobación de la tarjeta en el primer paso de la máquina virtual (capacidad de cálculo y VRAM por tipo de tarjeta,
rechazando `CARD_MISMATCH`), un registro de rechazo del lado de la instancia con un interruptor de circuito por tarjeta
(`CARD_CIRCUIT_OPEN`) y `OMP_NUM_THREADS` configurado antes de que se cargue torch.
3. **Secretos.** La ruta del archivo de tokens: un archivo 0600, leído solo por el proceso que lo necesita, destruido en la
máquina virtual y eliminado de todos los registros guardados.
4. **Un módulo de almacenamiento de Hugging Face.** Sincroniza las salidas con un prefijo de almacenamiento por instancia, verifica los recuentos y
sha256 con un manifiesto y, luego, escribe un recibo `done.json`. Nunca elimina nada.
5. **`PLATFORM.md` y `facts.json`.** Lo que la plataforma de Colab realmente hace, cada hecho con fecha y fuente,
para que un kit verifique sus suposiciones con ellos y una suposición incorrecta se convierta en un código de rechazo, no en una sorpresa de pago.

Las instancias utilizan una versión específica de este repositorio, una instancia por solicitud de extracción.

## Instalación

colab-runner no está en PyPI. Una instancia utiliza una versión específica o la instala desde la etiqueta:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.1.0"
```

- **Python** 3.11 o posterior. El paquete no tiene dependencias en tiempo de ejecución.
- **La línea de comandos `colab`** (la CLI de Colab de Google), con la sesión iniciada en la cuenta que paga por la instancia. colab-runner
la llama; no la instala ni inicia la sesión por usted. `COLAB_RUNNER_CLI` apunta a otra línea de comandos (a continuación).
- **Plataformas:** Las pruebas de CI se ejecutan en Linux con Python 3.11 y 3.12. El estudio también lo ejecuta desde Windows.

## Inicio rápido

La instancia abre la sesión y luego la pasa a `run`, que la finaliza deteniéndola:

```bash
colab new -s my-window            # the lane's own step: open the session (plus the card's flags)
python -m colab_runner run --session my-window --out-dir runs/w1   --script /content/train.sh --wall-s 3600   --max-rate 13.5 --cap 15 --cleanup-bound-s 600
echo "exit $?"                    # 0 done and stopped; see the exit codes below
```

Las cifras son marcadores de posición. Establezca `--max-rate` en las unidades esperadas por hora de la tarjeta y `--cap` en el presupuesto de la instancia en unidades de cálculo (consulte Precios).

`python -m colab_runner --help` enumera cada comando; `run --help` enumera cada marca.

## Usándolo desde una instancia (0.1.0)

**La sesión pertenece a la instancia.** La instancia abre la sesión de Colab y pasa su nombre. Una vez que `run` ha escrito
`session_open.json` en el directorio de salida, cada salida intenta detener la sesión y el marcador se elimina solo después de una detención confirmada.
- **Un marcador que permanece** significa que la sesión aún puede estar abierta: una detención que falló o una `run` que se interrumpió por completo (SIGKILL, una instancia perdida) antes de que se ejecutara su limpieza. La instancia luego ejecuta `python -m colab_runner stop --out-dir D`.
- **Antes de que exista el marcador** (un rechazo o un error de uso, ambos con código de salida 1), la instancia ejecuta `colab stop -s S`.

**Banderas requeridas de `run`:** `--session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C --cleanup-bound-s N`.
- `--out-dir` no debe existir aún, o debe estar vacío (`OUT_DIR_NOT_EMPTY` en caso contrario). Se rechaza una raíz del sistema de archivos
(`OUT_DIR_INVALID`): la cuarentena es un directorio hermano del directorio de salida, cuyo nombre se deriva de su ruta resuelta. Lo mismo ocurre con un directorio de salida que
no se puede crear o listar.
- `--cleanup-bound-s` debe ser igual o mayor que el límite de limpieza del ejecutor (`CLEANUP_BOUND_TOO_SMALL` en caso contrario). En los tiempos de espera predeterminados,
ese límite es de 466 s, o 986 s con `--resident` (`Runner.own_cleanup_bound_s`).
- Pase `--pre-open-rate` y `--pre-open-assignments` juntos, justo antes de que se abra la sesión, o ninguno de los dos. Sin ellos,
toda la tasa de la cuenta se atribuye a esta ventana, por lo que una sesión vecina puede superar el límite de tasa.
- Pase un `--job` distinto (predeterminado `job`) para cada paso que comparta un directorio `--content`. Cómo se rechaza un `--job` reutilizado:
- Cada ejecución tiene su propio ID de ejecución, `CR_RUN_ID`, que es nuevo cada vez. El nombre está reservado: se rechaza `--env CR_RUN_ID=…` (o
`CR_RUN_ID` en `Runner.run(env=...)`) (`RUN_ID_RESERVED`), ya que el uso de un mismo ID en diferentes pasos desactivaría
este rechazo entre ellos.
- La máquina virtual escribe el ID de ejecución en `<job>.run_id` antes de que comience el trabajo. El archivo de estado del trabajo y cada registro de sondeo lo contienen.
- Los archivos del trabajo son `<job>.pid`, `.status.json`, `.run.out`, `.poll.json` y `.run_id`. Si alguno de ellos está presente bajo
otro ID de ejecución (o bajo ninguno), el paso termina `start_failed` con `JOB_ALREADY_EXISTS`. Esto se mantiene independientemente de si el proceso del otro trabajo está
activo o inactivo, y con o sin un archivo de estado.
- Un paso rechazado no toca ninguno de los archivos de la otra ejecución, nunca mata su trabajo y, aun así, detiene su propia sesión. En
el modo `--resident`, el rechazo se envía a `<job>.refused.json`, nunca a `<job>.poll.json`.
- El ejecutor solo toma los registros que contienen su propio ID de ejecución. Un registro que otra ejecución dejó bajo el mismo `--job` (el `<job>.poll.json` final del paso 1, por ejemplo) nunca termina un paso. En el modo `--resident`, si solo aparecen los registros de otra ejecución dentro de
`--setup-stall-s`, el paso termina `start_failed` con `JOB_ALREADY_EXISTS`. Sin `--resident`, dicho registro se considera
un sondeo fallido.
- Un evento que ocurre antes de que se haya visto algún registro (el tiempo de espera, por ejemplo, mientras el trabajo de otra ejecución mantiene el `--job`) aún así no detiene nada:
la celda de detención responde `CR_NOT_OURS` y, a continuación, el `<job>.run.out` de la otra ejecución tampoco se recopila.
- No se rechaza un nuevo intento del mismo trabajo (uno cuya respuesta se perdió).
- En el modo `--resident`, se rechaza de inmediato un `CR_RESIDENT_MAX_S` no finito o no positivo que se pasa a través de `--env`
(`RESIDENT_MAX_INVALID`, salida 1).

**Códigos de salida** (la convención de la organización), para `run` y `stop --out-dir D`:

| Salida | Significado | `run` | `stop` |
| --- | --- | --- | --- |
| 0 | ok | `done`, y la detención se confirmó | una detención confirmada, o nada que detener |
| 1 | error de usuario | un rechazo (`REFUSED: …`) o un error de uso, antes de que se escribiera el marcador de sesión | un marcador que no nombra ninguna sesión (`SESSION_MARKER_INVALID`), o un error de uso |
| 2 | error en tiempo de ejecución | cualquier finalización que no sea `done` (cualquiera que haya sido su detención), o `RUN_ERROR` | una detención que no se confirmó, o `STOP_ERROR` |
| 3 | éxito parcial | `done`, pero la detención no se confirmó: `session_open.json` todavía está ahí | (no se utiliza) |

Al finalizar la tarea en la salida 3, el resultado del trabajo se encuentra en el directorio de salida y la sesión aún debe detenerse con `python -m colab_runner stop --out-dir D`.

Un rechazo después del marcador no es un código de salida 1. Un `--job` reutilizado, por ejemplo, resulta en un código de salida 2: el ejecutor era el propietario de la sesión en ese momento, por lo que la detuvo, y la última línea es `RUN_END reason=start_failed … code=JOB_ALREADY_EXISTS` con una pista que indica
`--job` en stderr. No se escribe ninguna línea `REFUSED:`.

**Rechazos y errores** tienen un código, un mensaje y una pista, y cada línea se limpia:
- Un rechazo escribe `REFUSED: CODE: message` como la primera línea en stderr, luego `hint: …` (qué cambiar).
- Un error durante una ejecución escribe `RUN_ERROR: CODE: message`, luego `hint: …`. El código es el propio del error
(`BINARY_DOWNLOAD_TOKEN`, `QUARANTINE_ESCAPE`), o `UNEXPECTED_ERROR` con el tipo de excepción en el mensaje. `stop` escribe `STOP_ERROR: …`
de la misma manera.
- Una ejecución que termina en un rechazo con nombre después del marcador (`JOB_ALREADY_EXISTS`, `BINARY_DOWNLOAD_TOKEN`,
`QUARANTINE_ESCAPE`) lo registra en
`runner_result.json` como `refusal: {code, message, hint, retryable}`. `retryable` es falso para cada rechazo de
la entrada del llamador: la misma llamada falla de la misma manera nuevamente.
- Cada código y su pista se encuentran en `colab_runner/errors.py` (`CODES`).

**Niveles de registro** (`run` y `stop`): `--quiet` escribe solo las líneas finales (`RUN_END`, `STOP_END` o `STOP_NOTHING`, y un
rechazo o un error con su pista). El valor predeterminado agrega el registro del ejecutor en stderr. `--verbose` agrega una línea por consulta. `--debug` agrega el
seguimiento de pila de un error. Se limpia un token en cada nivel, incluido el seguimiento de pila.

**Cómo termina una ejecución:** `done`, `failed` (código de salida distinto de cero), `oom`, `wall`, `cap`, `setup_stall`, `progress_stall`, `dead`,
`unreachable`, `start_failed`, `interrupted` o `error`.
- Cada finalización, excepto `error`, escribe `RUN_END reason=… rc=… stopped=…` como la última línea en stdout, con ` code=CODE` agregado cuando
la ejecución terminó en un rechazo con nombre. Una pista sigue en stderr para dicho rechazo, y para una detención que no se confirmó.
- Una finalización `error` escribe `RUN_ERROR: …` en stderr y no escribe ninguna línea `RUN_END`; cuando `session_open.json` aún está en el directorio de salida,
la pista para una detención que no se confirmó sigue. Un rechazo o un error de uso (código de salida 1) se escribe solo en stderr.
- `runner_result.json` en el directorio de salida registra cada finalización después de que se escribió el marcador, incluido `error`. Una descarga binaria
rechazada durante la limpieza convierte la finalización en `error` y mantiene la finalización que reemplazó en `prior_reason` (y en el detalle).

**Qué escribe `run` en el directorio de salida** (`--job` tiene como valor predeterminado `job`, `--content` tiene como valor predeterminado `/content`):
- `session_open.json` mientras la sesión puede estar abierta;
- `runner_result.json`, el final de la ejecución. Su `quarantined` enumera cada archivo binario descargado que se puso en cuarentena durante la ejecución, y una
cuarentena de una `Runner.download` posterior a través del mismo `Runner` reescribe el archivo con ella;
- `<job>.run.out`, el registro del trabajo (aparece solo una vez que el trabajo ha escrito líneas de registro, o una vez que `--resident` lo ha recopilado);
- cuando se establece `--resident`, `<job>.poll.json` (el registro de sondeo más reciente del trabajo, tal como lo descargó el ejecutor) y `<job>.refused.json`, el registro de rechazo de esta ejecución, cuando su celda residente rechazada escribió uno (un `JOB_ALREADY_EXISTS` que solo se agotó en los registros de otra
ejecución no tiene ninguno);
- `stop_confirmed.json` después de una detención confirmada, o `stop_pending.json` después de una detención fallida.

**El comando `colab`** es `colab` a menos que `COLAB_RUNNER_CLI` especifique otro comando. Se divide con `shlex.split`,
que utiliza las comillas POSIX: en Windows, escriba la ruta con barras diagonales o colóquela entre comillas (`"C:\tools\colab.exe"`), ya que una
barra diagonal sin comillas se elimina. Uno que no se puede dividir (una comilla sin cerrar) se rechaza (`CLI_INVALID`, código de salida 1).

**Qué debe escribir el trabajo para que los guardianes de inactividad puedan verlo.** Las rutas están en la máquina virtual; el directorio de salida es `CR_OUT_DIR`, por defecto
`<content>/<job>_out`.
- `phase.txt`: ausente, vacío o `setup` durante la instalación, cualquier otra cosa una vez que se está ejecutando. Una vez que el trabajo sale de la configuración, colab-runner conserva la última fase conocida si lee un `phase.txt` vacío o ausente y nunca vuelve a la configuración; si `phase.txt` vuelve a leer `setup` después de una fase de ejecución, colab-runner reinicia el reloj de inactividad de configuración. Los trabajos iniciados con `--hold-s` omiten la fase de configuración.
- `setup.log`: durante la configuración, una ejecución cuyo `setup.log` deja de crecer durante `--setup-stall-s` (predeterminado 600) termina `setup_stall`.
- **Progreso:** después de la configuración, una ejecución termina `progress_stall` cuando nada de lo que puede ver se mueve durante `--progress-stall-s` (predeterminado 900).
Lo que ve es la suma del primer entero de cada archivo que coincide con `CR_PROGRESS_GLOB` (predeterminado `<out>/progress/*`), el recuento
de archivos que coinciden con `CR_UNITS_GLOB` y los latidos del corazón de espera.
- Una ejecución sin un sondeo válido durante `--unreachable-polls` sondeos consecutivos (predeterminado 10), o durante `--unreachable-s` (predeterminado 1800), termina
`unreachable`.

**Escriba `phase.txt` de forma atómica.** El kit escribe `phase.txt`; colab-runner lo lee. Reescribir `phase.txt` directamente (por ejemplo, `echo run > phase.txt`) trunca el archivo primero, por lo que colab-runner puede leer un archivo vacío. Escriba la nueva fase en un archivo temporal en el mismo directorio y luego renombre el archivo temporal a `phase.txt`:

```bash
printf 'run\n' > "$CR_OUT_DIR/phase.tmp" && mv "$CR_OUT_DIR/phase.tmp" "$CR_OUT_DIR/phase.txt"
```

```python
import os
from pathlib import Path

out = Path(os.environ["CR_OUT_DIR"])
tmp = out / "phase.tmp"
tmp.write_text("run\n", encoding="utf-8")
os.replace(tmp, out / "phase.txt")
```

**Secretos y limpieza de descargas seguras para archivos binarios.** Pase un secreto como `--env NAME`, con NAME establecido en el entorno del canal. `--env NAME=VALUE` coloca el valor en la línea de comandos del ejecutor. Los valores llegan a la VM dentro del archivo de celda del cuaderno cargado, nunca como argumentos para la CLI `colab`.
Cada línea de salida de la CLI y cada línea de error se limpian con la biblioteca incluida `scrub_colab_tokens`. Los archivos y directorios descargados (incluidas las transferencias parciales) se clasifican solo por su contenido (un byte NUL o una decodificación estricta fallida UTF-8 es binario):
- Los archivos de texto se limpian en su lugar con `scrub_colab_tokens`.
- Los archivos binarios limpios se dejan idénticos en bytes. Los archivos ilegibles se eliminan.
- **La regla binaria.** Un archivo binario se analiza con pares de bytes de los patrones de tokens de la biblioteca.
- En UTF-8, los pares coinciden exactamente con lo que los patrones de texto coinciden en el texto decodificado. Cada carácter que `\s` de Python coincide se cuenta como un espacio, incluidos `\x1c`-`\x1f`, NBSP, NEL y EM SPACE.
- La única diferencia es que está junto a una letra no ASCII, donde el par `hf_` coincide y el patrón de texto no. Por lo tanto, la regla binaria puede detectar más que la regla de texto, pero nunca menos.
- Una regla es más estricta que el texto: en un archivo binario, `Bearer` solo se cuenta cuando el valor que le sigue tiene la forma de un token. Esto significa al menos 20 caracteres de `[A-Za-z0-9._-]`, utilizando al menos dos de minúsculas, mayúsculas y dígitos.
- Por lo tanto, una representación cuyo mensaje indica `Flame Bearer of the North` se transmite idéntica en bytes. Se detecta un token de portador real (un token de acceso OAuth, un JWT, un token de Hugging Face).
- Los archivos de texto conservan los patrones de la biblioteca sin cambios: el mismo texto en un archivo de texto sigue siendo `Bearer [REDACTED] the North`.
- **Una coincidencia nunca se reescribe.**
- El archivo se mueve sin cambios a la cuarentena, se registra y se nombra en `runner_result.json` (`quarantined`) solo por la ruta y el nombre del patrón.
- La descarga se rechaza con `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`). Esto finaliza `Runner.run` como `error` (salida de la CLI 2) y la sesión aún se detiene.
- **La descarga de un directorio** se limpia de tokens archivo por archivo, y un fallo nunca detiene el resto: una coincidencia cuyo movimiento a la cuarentena falla se elimina, y se anota un subdirectorio que no se puede listar. El primer error de este tipo se genera una vez que se han limpiado todos los archivos restantes. Solo se leen los archivos regulares. Un enlace simbólico, a un archivo o a un directorio, se elimina, nunca se sigue, y cualquier otra entrada que no sea un archivo regular o un directorio (un FIFO, un socket, un dispositivo) se elimina, nunca se lee.
- **Dónde está la cuarentena.**
- A través de `Runner` (y, por lo tanto, `run`), es el directorio hermano `<out-dir>.quarantine/`. Está fuera de `--out-dir`, por lo que ni `salvage` ni la agrupación de out-dir lo detectan.
- Un `Colab.download` o `scrub_local` sin `quarantine_dir` mueve el archivo a una nueva carpeta `colab-runner-quarantine-*` debajo del directorio temporal del sistema (`tempfile.gettempdir()`). Nunca se coloca junto al archivo, por lo que nunca está dentro de la carpeta en la que se descargó el archivo. Su ruta está en `BinaryDownloadRefused.quarantined`. Pase `Colab(quarantine_dir=...)` para elegir el lugar.
- Cada ruta de cuarentena se resuelve y se comprueba para asegurarse de que permanece dentro de su carpeta de cuarentena antes del movimiento. En cambio, se elimina un archivo que terminaría fuera de ella y la descarga falla con `QuarantineEscape` (`QUARANTINE_ESCAPE`).
- colab-runner nunca elimina una cuarentena. **El canal que lo llama debe eliminarlo y nunca debe enviarlo.**

**Salvage** es solo un enlace de API de Python: `Runner.run(..., salvage=callable)` lo ejecuta en cada final, excepto `done`, antes de la detención.
La CLI no tiene salvamento.

**Precios.** La ventana tiene un precio de `max(--max-rate, the highest stable window rate)` veces el tiempo transcurrido, a través de la función de tarifa y límite de la biblioteca incluida. Una tarifa estable por encima de `--max-rate` activa `RATE_ABOVE_MAX`, por lo que cada ventana que sigue ejecutándose tiene un precio de `--max-rate`: es un mínimo, no un límite flexible. Establézcalo en la tarifa esperada de la tarjeta; un límite flexible `--max-rate` activa el límite
temprano. El saldo de la cuenta se registra y nunca establece el precio de la ventana.

## Seguridad y modelo de amenazas

**Qué afecta.**
- La sesión de Colab: carga los archivos de las celdas, inicia y finaliza el trabajo que comenzó, descarga los archivos que se le solicitaron y detiene la sesión. Nunca envía una señal a un proceso que pertenezca a otra ejecución de colab-runner (`CR_NOT_OURS`).
- El directorio de salida (`--out-dir`) y su directorio de cuarentena hermano `<out-dir>.quarantine/` en el sistema. Una llamada directa a `Colab.download` sin directorio de cuarentena utiliza una nueva carpeta `colab-runner-quarantine-*` dentro del directorio temporal del sistema.
- Las variables de entorno que utiliza la sesión con `--env`, y que se pasan a la máquina virtual dentro del archivo de la celda.

**Qué no afecta.** Ningún archivo fuera del directorio de salida, su cuarentena y la cuarentena temporal. Ninguna otra sesión de Colab. Ninguna configuración de cuenta, credenciales o facturación: lee la tarifa y el saldo de la cuenta a través de la línea de comandos `colab` y nunca los cambia.

**Red.** Solo a través de la línea de comandos `colab`, a Google Colab. colab-runner no abre ninguna conexión por sí mismo.

**Secretos.** Un secreto transmitido como `--env NAME` no aparecerá en ninguna línea de comandos. Cada línea de salida, línea de error, registro de seguimiento y archivo de texto descargado se limpia para eliminar los tokens; un archivo binario que contenga un token se aísla sin modificaciones y la descarga se rechaza (como se indica anteriormente). Colab-runner nunca elimina el archivo aislado: la función que lo llama debe eliminarlo y nunca debe enviarlo.

**Permisos.** Se ejecuta como el usuario que lo ejecuta, con la sesión `colab` que ese usuario ya tiene. No necesita
elevación.

**No hay telemetría.** colab-runner no recopila ni envía nada.

Informar de una vulnerabilidad: consulte [SECURITY.md](SECURITY.md).

## Qué no está aquí

Ningún nombre de bucket, nombre de cuenta, envoltorios de inicio o rutas locales. Un llamador los pasa mediante una marca o
variable de entorno; las pruebas utilizan marcadores de posición.

## Verificar

```bash
bash verify.sh
```

## Licencia

MIT. Consulte [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
