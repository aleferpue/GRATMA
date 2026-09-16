
import os
import random
import re
import threading
import time
from datetime import datetime

import serial


# =============================================================================
# PARÁMETROS DE USUARIO / MEDIDA
# =============================================================================

FOLDER_PATH = r"C:\Users\rodri\OneDrive\Escritorio\GRATMA\gratma_aging\depuracion"

SENSORS = [1, 2, 3, 4, 5, 6, 7, 8]

VD = 50          # mV
VGINIT = 0       # mV
VGEND = 1200     # mV
VGSWEEP = 15     # mV
FBWD = 1         # 0: forward | 1: forward + backward
NUM_SEQUENCES = 5

STABILIZE_S = 600  #Antes estaba a 180
BETWEEN_SENSORS_S = 10

BAUDRATE = 115200
SERIAL_TIMEOUT_S = 1
IV_SILENCE_TIMEOUT_S = 300
IV_TOTAL_TIMEOUT_S = 600

MEASUREMENT_MODE = "random"
# MEASUREMENT_STAGE = "aging"
ELECTROLYTE = "PB-S0_01"

# Evita sobrescribir por accidente una medida ya existente.
ALLOW_OVERWRITE = False


# =============================================================================
# MÁQUINA DE ESTADOS
# =============================================================================

# La máquina de estados se limita a elegir la configuración eléctrica.

STATE_GND_UNSELECTED = "GND_UNSELECTED"
MEASUREMENT_STATE = STATE_GND_UNSELECTED

# Estado actual conocido: los sensores no medidos se ponen a tierra mediante um 1.
STATE_COMMANDS = {
    STATE_GND_UNSELECTED: ["um 1"],
}

# Secuencia de inicialización

# Aplicamos 0V sobre todos los drenadores
# Aplicamos 0.8V sobre todas las puertas
# 0 -> VG y 1 -> VD

INITIALIZATION_COMMANDS = [
    "sw 0 255",
    "sw 1 255",
    "sv 1 0 0",
    "sv 1 1 0",
    "sv 0 0 0",
    "sv 0 1 0",
]


# =============================================================================
# CONTROL GLOBAL DE ERRORES
# =============================================================================

PRINT_LOCK = threading.Lock()
# STOP_EVENT se usa únicamente para una cancelación global manual (Ctrl+C).
# Un fallo de un GRATMA NO detiene a los demás equipos.
STOP_EVENT = threading.Event()


class GratmaError(RuntimeError):
    pass


def print_status(message, port=None):
    """Solo información útil para el operador."""
    with PRINT_LOCK:
        print(f"[{port}] {message}" if port else message)


def request_global_stop():
    """Solicita detener todos los equipos, reservado para Ctrl+C."""
    STOP_EVENT.set()


def check_stop():
    """Aborta el hilo solo cuando el usuario ha solicitado una parada global."""
    if STOP_EVENT.is_set():
        raise GratmaError("Ejecución cancelada manualmente.")


# =============================================================================
# DATOS DE LOS EQUIPOS Y CARPETAS
# =============================================================================


def required_input(text):
    while True:
        value = input(f"{text}: ").strip()
        if value:
            return value
        print_status("El campo no puede quedar vacío.")


def sanitize(value):
    return re.sub(r'[<>:"/\\|?*]+', "_", value.strip())


def ask_devices():
    """Pregunta nº de GRATMA, COM, wafer y chip."""
    while True:
        value = required_input("Número de equipos a medir en paralelo")
        if value.isdigit() and int(value) >= 1:
            number = int(value)
            break
        print_status("Introduce un número entero mayor o igual que 1.")

    devices = []
    for index in range(1, number + 1):
        print_status(f"\n--- Equipo {index}/{number} ---")
        port = required_input("Puerto COM").upper()
        wafer = sanitize(required_input("Wafer"))
        chip = sanitize(required_input("Chip"))

        # Si el usuario escribe WAFER_CHIP, evitamos repetir el wafer.
        prefix = f"{wafer}_"
        if chip.upper().startswith(prefix.upper()):
            chip = chip[len(prefix):]

        devices.append({
            "port": port,
            "wafer": wafer,
            "chip": chip,
            "serial": None,
            "folder": None,
            "setup_log": None,
            "saved": 0,
            "error": None,
        })

    # Un mismo COM o carpeta de chip nunca se comparten entre equipos.
    ports = [d["port"] for d in devices]
    chips = [d["chip"].upper() for d in devices]
    if len(set(ports)) != len(ports):
        raise GratmaError("Hay puertos COM repetidos.")
    if len(set(chips)) != len(chips):
        raise GratmaError("Hay nombres de chip repetidos.")

    return devices


def chip_folder(device):
    return os.path.join(FOLDER_PATH, f"{device['chip']}_{MEASUREMENT_STAGE}")


def measurement_filename(device, sensor, sequence):
    return (
        f"{device['wafer']}_{device['chip']}_{MEASUREMENT_STAGE}_"
        f"Array{sensor}_{MEASUREMENT_MODE}_{sequence}_{ELECTROLYTE}.txt"
    )


def all_info_filename(final_filename):
    return f"All_info_{os.path.splitext(final_filename)[0]}.txt"


def prepare_folders(devices):
    """Crea la carpeta de cada chip y un All_info de inicialización."""
    os.makedirs(FOLDER_PATH, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prepared = []

    for device in devices:
        try:
            device["folder"] = chip_folder(device)
            os.makedirs(device["folder"], exist_ok=True)

            setup_name = (
                f"All_info_setup_{device['wafer']}_{device['chip']}_{stamp}.txt"
            )
            device["setup_log"] = os.path.join(device["folder"], setup_name)
            with open(device["setup_log"], "w", encoding="utf-8") as file:
                file.write("# INICIALIZACION_GRATMA\n")
                file.write(f"# puerto={device['port']}\n")
                file.write(f"# wafer={device['wafer']}\n")
                file.write(f"# chip={device['chip']}\n")
                file.write(f"# estado={MEASUREMENT_STATE}\n\n")

            # Se muestra la ruta real para saber desde el inicio dónde se guardará el chip.
            print_status(f"Carpeta de salida: {device['folder']}", device["port"])
            prepared.append(device)

        except OSError as exc:
            device["error"] = GratmaError(
                f"No se pudo preparar la carpeta del chip {device['chip']}: {exc}"
            )
            print_status(f"ERROR CRÍTICO: {device['error']}", device["port"])

    return prepared


def check_existing_measurements(devices):
    """Excluye solo el equipo que sobrescribiría un TXT definitivo."""
    if ALLOW_OVERWRITE:
        return list(devices)

    ready = []
    for device in devices:
        existing_path = None

        for sequence in range(1, NUM_SEQUENCES + 1):
            for sensor in SENSORS:
                path = os.path.join(
                    device["folder"],
                    measurement_filename(device, sensor, sequence),
                )
                if os.path.exists(path):
                    existing_path = path
                    break
            if existing_path:
                break

        if existing_path:
            device["error"] = GratmaError(
                "Ya existe una medida y ALLOW_OVERWRITE=False: "
                f"{existing_path}"
            )
            write_setup_log(device, f"ERROR CRÍTICO: {device['error']}")
            print_status(f"ERROR CRÍTICO: {device['error']}", device["port"])
        else:
            ready.append(device)

    return ready


def write_setup_log(device, text):
    """Toda la respuesta técnica de UM/SW/SV va al All_info de setup."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    with open(device["setup_log"], "a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {text}\n")


# =============================================================================
# ORDEN ALEATORIO DE SENSORES
# =============================================================================


def random_sensor_order(rng):

    """
    Se aleatorizan por separado 1-4 y 5-8 y luego se alternan ambos grupos.
    Así NO se mide siempre en el mismo orden y se evita condicionar las medidas.
    """

    top = [1, 2, 3, 4]
    bottom = [5, 6, 7, 8]
    rng.shuffle(top)
    rng.shuffle(bottom)

    order = []
    for top_sensor, bottom_sensor in zip(top, bottom):
        order.extend([top_sensor, bottom_sensor])
    return order


def sensor_bitmask(sensor):
    if sensor not in SENSORS:
        raise GratmaError(f"Sensor no válido: {sensor}")
    return 1 << (sensor - 1)


# =============================================================================
# COMUNICACIÓN E INICIALIZACIÓN DEL GRATMA
# =============================================================================


def decode_line(raw):
    """No se ignoran bytes corruptos: una respuesta dañada detiene la medida."""
    try:
        return raw.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise GratmaError("Respuesta serie no ASCII/corrupta.") from exc


def firmware_error(line):
    """
    Detecta solo mensajes EXPLÍCITOS de fallo del firmware.

    No considera error que una línea normal de diagnóstico contenga campos como
    "VsError:..." o "Target:... Measured:... Error:...".
    Solo devuelve True cuando la propia línea comienza claramente como un
    mensaje ERROR/WARNING/FAIL, con o sin prefijo [..] o (GRATMA).
    """
    text = line.strip()
    error_words = r"(?:ERROR|FAILED|FAIL|WARNING|WARN|FATAL)"

    patterns = (
        rf"^{error_words}\b",
        rf"^\[{error_words}\](?:\s|:|$)",
        rf"^\(GRATMA\)\s*{error_words}\b",
    )

    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def send_command(device, command, wait=0.4):
    """Envía UM/SW/SV; la respuesta se guarda en All_info, no en terminal."""
    check_stop()
    ser = device["serial"]

    try:
        ser.reset_input_buffer()
        ser.write((command + "\n").encode("ascii"))
        ser.flush()
    except (serial.SerialException, OSError) as exc:
        raise GratmaError(
            f"No se pudo enviar '{command}' por {device['port']}: {exc}"
        ) from exc

    write_setup_log(device, f">>> {command}")
    deadline = time.monotonic() + wait + 0.8

    while time.monotonic() < deadline:
        check_stop()
        try:
            raw = ser.readline()
        except (serial.SerialException, OSError) as exc:
            raise GratmaError(
                f"Fallo leyendo respuesta a '{command}': {exc}"
            ) from exc

        if not raw:
            break

        line = decode_line(raw)
        if line:
            write_setup_log(device, line)
            if firmware_error(line):
                raise GratmaError(
                    f"Respuesta de error a '{command}': {line}"
                )


def configure_electrical_state(device):
    """Máquina de estados mínima: estado elegido -> comandos confirmados."""
    if MEASUREMENT_STATE not in STATE_COMMANDS:
        raise GratmaError(f"Estado no implementado: {MEASUREMENT_STATE}")

    # Estado actual: um 1.
    for command in STATE_COMMANDS[MEASUREMENT_STATE]:
        send_command(device, command)

    # Secuencia SW/SV conocida.
    for command in INITIALIZATION_COMMANDS:
        send_command(device, command)


def open_devices(devices):
    """Abre cada COM de forma independiente y devuelve los equipos disponibles."""
    opened = []

    for device in devices:
        try:
            device["serial"] = serial.Serial(
                device["port"], BAUDRATE, timeout=SERIAL_TIMEOUT_S
            )
            opened.append(device)
            write_setup_log(device, "Puerto abierto correctamente.")

        except (serial.SerialException, OSError) as exc:
            device["error"] = GratmaError(
                f"No se pudo abrir {device['port']}: {exc}"
            )
            write_setup_log(device, f"ERROR CRÍTICO: {device['error']}")
            print_status(f"ERROR CRÍTICO: {device['error']}", device["port"])
            device["serial"] = None

    return opened


def close_devices(devices):
    for device in devices:
        if device["serial"] is not None:
            try:
                device["serial"].close()
            except Exception:
                pass
            device["serial"] = None


def interruptible_sleep(seconds):
    """Espera que puede abortarse si el usuario pulsa Ctrl+C."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_stop()
        time.sleep(min(0.25, end - time.monotonic()))


# =============================================================================
# 7. IV: RECEPCIÓN, EXTRACCIÓN Y VALIDACIÓN
# =============================================================================

NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"

# Formato habitual:
# Sensor 1 Point 1 (rep 1): Vfg = ..., Is = ..., Vs = ..., Ig = ...
REAL_POINT = re.compile(
    rf"Sensor\s+(\d+)\s+Point\s+\d+"
    rf"(?:\s+\(rep(?:=|\s+)\d+\))?\s*:\s*"
    rf"Vfg\s*=\s*({NUMBER})V,\s*"
    rf"Is\s*=\s*({NUMBER})A,\s*"
    rf"Vs\s*=\s*({NUMBER})V,\s*"
    rf"Ig\s*=\s*({NUMBER})A",
    re.IGNORECASE,
)

# Formato alternativo DATA: Vfg Is Vs Ig
DATA_POINT = re.compile(
    rf"DATA\s+type=1\s+sensor=S(\d+)\s+rep=\d+\s+"
    rf"(?:fwd|bwd)\s+seq=\S+\s+"
    rf"({NUMBER})\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})",
    re.IGNORECASE,
)

NUMERIC_ROW = re.compile(rf"^{NUMBER};{NUMBER};{NUMBER};{NUMBER}$")
SWEEP_COMPLETED = ("measurement sweep completed", "measurement ok - result ready",)


def metadata(device, sensor, sequence, order, parallel_ports):
    return {
        "fecha_hora_inicio": datetime.now().isoformat(timespec="seconds"),
        "puerto": device["port"],
        "wafer": device["wafer"],
        "chip": device["chip"],
        "sensor": sensor,
        "secuencia": sequence,
        "orden_aleatorio": ",".join(map(str, order)),
        "puertos_en_paralelo": ",".join(parallel_ports),
        "estado_electrico": MEASUREMENT_STATE,
        "VD_mV": VD,
        "VGINIT_mV": VGINIT,
        "VGEND_mV": VGEND,
        "VGSWEEP_mV": VGSWEEP,
        "FBWD": FBWD,
        "estabilizacion_s": STABILIZE_S,
        "espera_entre_sensores_s": BETWEEN_SENSORS_S,
        "electrolito": ELECTROLYTE,
    }


def write_metadata(file, data):
    file.write("# PARAMETROS_INICIALES_GRATMA\n")
    for key, value in data.items():
        file.write(f"# {key}={value}\n")
    file.write("\n")


def execute_iv(device, sensor, raw_path, meta):
    """
    Ejecuta IV y guarda TODA la respuesta en All_info.
    Si no aparece SWEEP_COMPLETED o existe timeout/error, falla inmediatamente.
    """
    check_stop()
    bitmask = sensor_bitmask(sensor)
    command = f"iv {VD} {VGINIT} {VGEND} {VGSWEEP} {bitmask} {FBWD} 1"
    ser = device["serial"]

    try:
        ser.reset_input_buffer()
        ser.write((command + "\n").encode("ascii"))
        ser.flush()
    except (serial.SerialException, OSError) as exc:
        raise GratmaError(f"No se pudo enviar IV para S{sensor}: {exc}") from exc

    lines = []
    start = time.monotonic()
    last_data = start
    reset_done = False

    with open(raw_path, "w", encoding="utf-8") as raw_file:
        write_metadata(raw_file, meta)
        raw_file.write(f"# COMANDO_IV={command}\n\n")

        while True:
            check_stop()
            now = time.monotonic()

            if now - start > IV_TOTAL_TIMEOUT_S:
                raise GratmaError(
                    f"S{sensor}: IV supera {IV_TOTAL_TIMEOUT_S}s de tiempo total."
                )
            if now - last_data > IV_SILENCE_TIMEOUT_S:
                raise GratmaError(
                    f"S{sensor}: {IV_SILENCE_TIMEOUT_S}s sin recibir datos."
                )

            try:
                raw = ser.readline()
            except (serial.SerialException, OSError) as exc:
                raise GratmaError(f"S{sensor}: fallo de lectura: {exc}") from exc

            if not raw:
                continue

            line = decode_line(raw)
            if not line:
                continue

            last_data = time.monotonic()
            lines.append(line)
            raw_file.write(line + "\n")
            raw_file.flush()

            if "cannot start sweep - system not ready (state=4)" in line.lower():
                if reset_done:
                    raise GratmaError(
                        f"S{sensor}: el GRATMA sigue en state=4 después del reset."
                    )

                print_status(
                    f"S{sensor}: state=4. Ejecutando reset y reintentando.",
                    device["port"],
                )

                try:
                    ser.write(b"reset\n")
                    ser.flush()
                    interruptible_sleep(2)
                    ser.reset_input_buffer()
                    ser.write((command + "\n").encode("ascii"))
                    ser.flush()
                except (serial.SerialException, OSError) as exc:
                    raise GratmaError(
                        f"S{sensor}: fallo durante reset/reintento: {exc}"
                    ) from exc

                reset_done = True
                lines = []
                start = time.monotonic()
                last_data = start
                continue

            if firmware_error(line):
                raise GratmaError(f"S{sensor}: el GRATMA devuelve: {line}")

            if any(message in line.lower() for message in SWEEP_COMPLETED):
                return lines


def parse_iv(lines, sensor):
    """Extrae solo Vfg;Vs;Ig;Is reales. Nunca transforma Id en Vs."""
    points = []

    # 1. Preferencia: valores nombrados por el GRATMA.
    for line in lines:
        match = REAL_POINT.search(line)
        if match and int(match.group(1)) == sensor:
            # Recibido: Vfg, Is, Vs, Ig -> guardado: Vfg, Vs, Ig, Is
            points.append(tuple(float(match.group(i)) for i in (2, 4, 5, 3)))

    if points:
        return points, "GRATMA/IV_SWEEP"

    # 2. Formato DATA: Vfg, Is, Vs, Ig.
    for line in lines:
        match = DATA_POINT.search(line)
        if match and int(match.group(1)) == sensor:
            points.append(tuple(float(match.group(i)) for i in (2, 4, 5, 3)))

    if points:
        return points, "DATA"

    # 3. Tabla aceptada solo si declara explícitamente Vfg;Vs;Ig;Is.
    collecting = False
    for line in lines:
        text = line.strip()
        if text == "Vfg;Vs;Ig;Is":
            collecting = True
            continue
        if collecting:
            if NUMERIC_ROW.fullmatch(text):
                points.append(tuple(float(value) for value in text.split(";")))
            else:
                break

    if points:
        return points, "tabla Vfg;Vs;Ig;Is"

    raise GratmaError(
        f"S{sensor}: no existen datos Vfg;Vs;Ig;Is válidos. "
        "No se renombra Id como Vs."
    )


def validate_iv(points, sensor):
    """Nada se guarda como válido antes de superar estas comprobaciones."""
    span = VGEND - VGINIT

    if VGSWEEP <= 0 or span < 0 or span % VGSWEEP != 0:
        raise GratmaError("Configuración VGINIT/VGEND/VGSWEEP no válida.")

    forward = span // VGSWEEP + 1
    if FBWD == 0:
        expected = {forward}
    elif FBWD == 1:
        # PENDIENTE DE VERIFICAR con el firmware: el punto de retorno puede
        # aparecer una sola vez o repetirse al iniciar el backward.
        expected = {2 * forward - 1, 2 * forward}
    else:
        raise GratmaError(f"FBWD no soportado: {FBWD}")

    if len(points) not in expected:
        raise GratmaError(
            f"S{sensor}: {len(points)} puntos recibidos; "
            f"se esperaban provisionalmente {sorted(expected)}."
        )

    for point_number, point in enumerate(points, 1):
        for name, value in zip(("Vfg", "Vs", "Ig", "Is"), point):
            if value != value or value in (float("inf"), float("-inf")):
                raise GratmaError(
                    f"S{sensor}, punto {point_number}: {name}={value} no es válido."
                )


def save_valid_measurement(path, meta, points, source):
    """Escritura atómica: primero .part, después .txt definitivo."""
    part_path = path + ".part"

    try:
        with open(part_path, "w", encoding="utf-8") as file:
            write_metadata(file, meta)
            file.write(f"# fuente_datos={source}\n\n")
            file.write("Vfg;Vs;Ig;Is\n")
            for vfg, vs, ig, is_value in points:
                file.write(f"{vfg};{vs};{ig};{is_value}\n")
            file.flush()
            os.fsync(file.fileno())

        if os.path.exists(path) and not ALLOW_OVERWRITE:
            raise GratmaError(f"El TXT ya existe y no se sobrescribirá: {path}")

        os.replace(part_path, path)

    except Exception:
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass
        raise


def mark_failed_all_info(raw_path):
    """El All_info fallido se conserva, pero queda claramente marcado."""
    if not os.path.exists(raw_path):
        return
    root, ext = os.path.splitext(raw_path)
    try:
        os.replace(raw_path, root + ".FAILED" + ext)
    except OSError:
        pass


# =============================================================================
# FLUJO DE MEDIDA DE CADA GRATMA
# =============================================================================


def measure_device(device, parallel_ports):
    """
    Flujo principal de un equipo:
    orden aleatorio -> espera -> IV -> parseo -> validación -> guardado.
    """
    rng = random.Random()  # seed automática; NO se fija una semilla repetible.
    first_measurement = True

    try:
        for sequence in range(1, NUM_SEQUENCES + 1):
            check_stop()

            # Recalcular un orden aleatorio NUEVO para cada secuencia.
            order = random_sensor_order(rng)
            print_status(
                f"Secuencia {sequence}/{NUM_SEQUENCES} | orden aleatorio: {order}",
                device["port"],
            )
            write_setup_log(device, f"Orden secuencia {sequence}: {order}")

            for sensor in order:
                check_stop()

                if not first_measurement:
                    interruptible_sleep(BETWEEN_SENSORS_S)
                first_measurement = False

                final_name = measurement_filename(device, sensor, sequence)
                final_path = os.path.join(device["folder"], final_name)
                raw_path = os.path.join(
                    device["folder"], all_info_filename(final_name)
                )
                meta = metadata(device, sensor, sequence, order, parallel_ports)

                try:
                    # 1. Medir.
                    raw_lines = execute_iv(device, sensor, raw_path, meta)

                    # 2. Extraer Vfg;Vs;Ig;Is.
                    points, source = parse_iv(raw_lines, sensor)

                    # 3. Validar antes de crear el TXT definitivo.
                    validate_iv(points, sensor)

                    # 4. Guardar solo una medida ya válida.
                    save_valid_measurement(final_path, meta, points, source)
                    device["saved"] += 1

                except Exception:
                    mark_failed_all_info(raw_path)
                    raise

    except Exception as exc:
        # El error invalida únicamente este GRATMA. Los demás continúan.
        device["error"] = exc
        write_setup_log(device, f"ERROR CRÍTICO: {exc}")
        print_status(f"ERROR CRÍTICO: {exc}", device["port"])


# =============================================================================
# PROGRAMA PRINCIPAL
# =============================================================================


def show_configuration(devices):
    print_status("\n" + "=" * 64)
    print_status("GRATMA I-V — CARACTERIZACIÓN")
    print_status("=" * 64)
    print_status(f"Equipos: {len(devices)}")
    for device in devices:
        print_status(
            f"{device['port']} | wafer {device['wafer']} | chip {device['chip']}"
        )
    print_status(f"Sensores: {SENSORS}")
    print_status(f"Secuencias: {NUM_SEQUENCES}")
    print_status(f"Estado eléctrico: {MEASUREMENT_STATE}")
    print_status(f"Carpeta base: {FOLDER_PATH}")
    print_status("=" * 64)


def main():
    STOP_EVENT.clear()

    devices = []
    active_devices = []
    threads = []

    try:
        devices = ask_devices()
        show_configuration(devices)

        print_status("\nPreparando carpetas...")
        ready_devices = prepare_folders(devices)
        ready_devices = check_existing_measurements(ready_devices)
        if not ready_devices:
            raise GratmaError("No queda ningún equipo disponible para medir.")

        print_status("Abriendo puertos...")
        active_devices = open_devices(ready_devices)
        if not active_devices:
            raise GratmaError("No se ha podido abrir ningún puerto COM.")

        # Margen conservado del programa anterior tras abrir los COM.
        interruptible_sleep(2)

        print_status("Inicializando GRATMA...")
        initialized_devices = []
        for device in active_devices:
            try:
                # UM/SW/SV se ejecutan UNA SOLA VEZ al principio para este equipo.
                configure_electrical_state(device)
                initialized_devices.append(device)
            except Exception as exc:
                device["error"] = exc
                write_setup_log(device, f"ERROR CRÍTICO: {exc}")
                print_status(f"ERROR CRÍTICO durante inicialización: {exc}", device["port"])
                try:
                    device["serial"].close()
                except Exception:
                    pass
                device["serial"] = None

        active_devices = initialized_devices
        if not active_devices:
            raise GratmaError("Ningún GRATMA ha superado la inicialización.")

        print_status("Inicialización completada en los equipos disponibles.")

        print_status(
            f"Estabilizando {STABILIZE_S}s ({STABILIZE_S / 60:.1f} min)..."
        )
        interruptible_sleep(STABILIZE_S)
        print_status("Estabilización completada.")

        print_status("Iniciando medidas I-V...")
        parallel_ports = [device["port"] for device in active_devices]

        for device in active_devices:
            thread = threading.Thread(
                target=measure_device,
                args=(device, parallel_ports),
                name=f"GRATMA-{device['port']}",
            )
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

        expected_files = len(SENSORS) * NUM_SEQUENCES

        print_status("\n" + "=" * 64)
        print_status("RESUMEN")
        print_status("=" * 64)

        for device in devices:
            if device["error"] is None and device["saved"] == expected_files:
                print_status(
                    f"{device['port']} | {device['wafer']}_{device['chip']} | "
                    f"OK | {device['saved']}/{expected_files} medidas válidas"
                )
            elif device["error"] is not None:
                print_status(
                    f"{device['port']} | {device['wafer']}_{device['chip']} | "
                    f"ERROR | {device['saved']}/{expected_files} medidas | "
                    f"{device['error']}"
                )
            else:
                print_status(
                    f"{device['port']} | {device['wafer']}_{device['chip']} | "
                    f"INCOMPLETO | {device['saved']}/{expected_files} medidas"
                )

    except KeyboardInterrupt:
        request_global_stop()
        print_status("\nERROR: ejecución interrumpida manualmente.")
        for thread in threads:
            thread.join()

    except Exception as exc:
        # Solo los errores globales de configuración/preparación llegan aquí.
        print_status("\n" + "=" * 64)
        print_status("CARACTERIZACIÓN DETENIDA")
        print_status(f"Causa: {exc}")
        print_status("=" * 64)

    finally:
        close_devices(devices)


if __name__ == "__main__":
    main()
