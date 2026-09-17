# -*- coding: utf-8 -*-
"""
gratma_compare_aging.py
=======================
Comparador visual de medidas GRATMA "antes" vs "aging".

Objetivo
--------
Comparar medidas de un mismo chip/array tomadas en dos momentos distintos
(por ejemplo, hace un mes y tras envejecimiento) sin generar decenas de gráficas.

Genera:
  1) 01_resumen_envejecimiento.png
     - VDirac Forward antes vs aging (barras de error horizontales)
     - VDirac Backward antes vs aging (barras de error horizontales)
     - Histéresis |VDirac_B - VDirac_F| antes vs aging
     - Cambio global de la curva (% RMSE normalizado)

  2) 02_sensores_mas_cambiados.png
     - Superposición de las curvas medias antes vs aging únicamente para los
       arrays que más han cambiado (por defecto, los 3 primeros), mostrados
       en orden de número de array.

  3) resumen_envejecimiento.csv
     - Tabla numérica de apoyo con el CAMBIO (delta) por array, con unidades
       incluidas en el nombre de cada columna.

El script NO modifica los TXT originales.

Repeticiones utilizadas
------------------------
Se asume que siempre se introducen medidas de 5 repeticiones por array
(tanto en "antes" como en "aging"). Para el cálculo de medias y de todas
las métricas SOLO se usan las repeticiones 3, 4 y 5 (las tres últimas).
Esto es una constante fija (REPS_A_USAR) y no se guarda ninguna traza de
qué repeticiones se han usado en la salida.

Compatibilidad de TXT
---------------------
- Formato nuevo sin funcionalizar:
    Wafer_Chip_Array1_random_1_PB-S0_01.txt
- Formato nuevo con funcionalizado:
    Wafer_Chip_funcionalizado_Array1_random_1_PB-S0_01.txt
- All_info_ equivalentes (se usan solo si no existe el TXT definitivo)
- Formatos antiguos Id_Vfg__... y All_info_...

Columnas:
- Vfg;Vs;Ig;Is  -> usa Vfg e Is
- Vfg;Id;Ig;Is  -> usa Vfg e Is
- Vfg;Id        -> compatibilidad antigua

Uso normal
----------
    python gratma_compare_aging.py

Se abrirán dos ventanas para seleccionar:
    1. carpeta de medidas antiguas
    2. carpeta de medidas funcionalizadas

Si una de las carpetas termina en '_funcionalizado', el programa la identifica
automáticamente como la carpeta FUNCIONALIZADO aunque se hayan seleccionado al revés.
Los resultados se guardan en la carpeta fija configurada en RUTA_SALIDA_FIJA.

También por terminal:
    python gratma_compare_aging.py --antes "C:\\...\\antes" --aging "C:\\...\\aging"
"""

import argparse
import csv
import glob
import os
import re
import subprocess
import sys
import numpy as np
import matplotlib.pyplot as plt
from collections import defaultdict
from pathlib import Path




# ============================== CONFIGURACIÓN ==============================

BUSCAR_EN_SUBCARPETAS = True
A_MICROAMPERIOS = 1e6
DPI = 220

# Cuántos arrays se detallan en la segunda figura.
TOP_ARRAYS_DETALLE = 3

# Número de puntos de la malla común para comparar curvas.
PUNTOS_INTERPOLACION = 300

# Si True, abre la carpeta de resultados al terminar.
ABRIR_CARPETA_AL_TERMINAR = True

# None = todos los arrays detectados en ambas condiciones.
# Ejemplo: [1, 2, 3, 4]
ARRAYS_A_COMPARAR = None

# Para evitar porcentajes absurdos si el área de la curva antigua es casi cero.
EPS = 1e-12

# Repeticiones que se usan para calcular las medias (siempre se asume que
# existen 5 repeticiones por array; se usan solo las 3 últimas).
REPS_A_USAR = {3, 4, 5}

# Semiancho (en V) de la ventana centrada en el punto de Dirac medio
# (promedio entre "antes" y "aging") usada para el RMSE y el AUC "en torno
# al mínimo". Ajusta este valor si quieres una ventana más ancha o estrecha.
SEMIANCHO_VENTANA_MINIMO = 0.2

# Carpeta fija donde se guardan las figuras y el CSV. No se crea ninguna
# subcarpeta dentro de esta ruta: todos los archivos van directamente aquí,
# distinguidos por el nombre de wafer/chip que llevan en el nombre de archivo.
RUTA_SALIDA_FIJA = r"C:\Users\alefe\Nextcloud\Clean_Room\Biosensors Elsauli\Clasificación\FIGURAS REPORT"

# Sufijos de carpeta reconocidos como condición "post" (después del proceso),
# y el nombre que se muestra en las figuras para cada uno. Se admiten ambos:
# si la carpeta termina en "_aging" las figuras dicen "Aging"; si termina en
# "_funcionalizado" las figuras dicen "Functionalized". Añade más entradas
# aquí si necesitas reconocer otros sufijos.
SUFIJOS_A_ETIQUETAS = {
    "_aging": "Aging",
    "_funcionalizado": "Functionalized",
}

# ===========================================================================


def analizar_argumentos():
    parser = argparse.ArgumentParser(
        description="Compara medidas GRATMA originales frente a medidas aging."
    )
    parser.add_argument("--antes", help="Carpeta con las medidas originales.")
    parser.add_argument("--aging", help="Carpeta con las medidas aging.")
    parser.add_argument(
        "-o",
        "--salida",
        help="Carpeta de salida. Por defecto se crea dentro de la carpeta aging.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=TOP_ARRAYS_DETALLE,
        help="Número de arrays más cambiados que se muestran en detalle.",
    )
    parser.add_argument(
        "--no-abrir",
        action="store_true",
        help="No abrir automáticamente la carpeta de resultados.",
    )
    return parser.parse_args()


def seleccionar_carpeta(titulo, inicial=None):
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        opciones = {"title": titulo, "mustexist": True}
        if inicial and Path(inicial).is_dir():
            opciones["initialdir"] = str(inicial)
        seleccion = filedialog.askdirectory(**opciones)
        root.destroy()
        if seleccion:
            return Path(seleccion).expanduser().resolve()
    except Exception:
        pass
    return None


def resolver_carpeta(ruta, titulo, inicial=None):
    if ruta:
        p = Path(ruta).expanduser().resolve()
        if p.is_dir():
            return p
        print(f"AVISO: la carpeta no existe: {p}")

    p = seleccionar_carpeta(titulo, inicial)
    if p:
        return p

    try:
        texto = input(f"{titulo}\nPega la ruta (Enter para cancelar): ").strip().strip('"')
    except (EOFError, KeyboardInterrupt):
        return None
    if not texto:
        return None
    p = Path(texto).expanduser().resolve()
    return p if p.is_dir() else None


def detectar_sufijo_post(path):
    """
    Devuelve el sufijo reconocido (p.ej. '_aging' o '_funcionalizado') si el
    nombre de la carpeta termina en alguno de los sufijos de SUFIJOS_A_ETIQUETAS,
    o None si no coincide con ninguno.
    """
    nombre = Path(path).name.lower()
    for sufijo in SUFIJOS_A_ETIQUETAS:
        if nombre.endswith(sufijo.lower()):
            return sufijo
    return None


def es_carpeta_aging(path):
    """Devuelve True si el nombre de la carpeta termina en alguno de los
    sufijos reconocidos como condición "post" (ver SUFIJOS_A_ETIQUETAS)."""
    return detectar_sufijo_post(path) is not None


def etiqueta_post_para_carpeta(path):
    """
    Nombre a mostrar en las figuras para la condición "post", según el
    sufijo detectado en la carpeta (ver SUFIJOS_A_ETIQUETAS). Si no se
    reconoce ningún sufijo, se usa "Aging" por defecto.
    """
    sufijo = detectar_sufijo_post(path)
    return SUFIJOS_A_ETIQUETAS.get(sufijo, "Aging")


def detectar_orden_antes_aging(carpeta_1, carpeta_2):
    """
    Usa el convenio del laboratorio: la carpeta de medidas "post" termina en
    alguno de los sufijos de SUFIJOS_A_ETIQUETAS (p.ej. <chip>_aging o
    <chip>_funcionalizado). Si solo una de las dos carpetas cumple ese
    convenio, se usa como carpeta "post" automáticamente.

    Devuelve: (carpeta_antes, carpeta_aging, se_intercambiaron)
    """
    c1_aging = es_carpeta_aging(carpeta_1)
    c2_aging = es_carpeta_aging(carpeta_2)

    if c1_aging and not c2_aging:
        return carpeta_2, carpeta_1, True
    if c2_aging and not c1_aging:
        return carpeta_1, carpeta_2, False

    # Si ambas o ninguna terminan en un sufijo reconocido, respetamos el orden seleccionado.
    return carpeta_1, carpeta_2, False


def abrir_carpeta(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def buscar_archivos(carpeta, patron):
    if BUSCAR_EN_SUBCARPETAS:
        return glob.glob(str(Path(carpeta) / "**" / patron), recursive=True)
    return glob.glob(str(Path(carpeta) / patron))


def normalizar_muestra(nombre):
    """Quita marcadores de etapa al final del identificador de muestra."""
    nombre = str(nombre).strip("_")
    # Los sufijos de SUFIJOS_A_ETIQUETAS (aging, funcionalizado...) aparecen
    # justo antes de _ArrayN.
    sufijos_post = "|".join(s.lstrip("_") for s in SUFIJOS_A_ETIQUETAS)
    nombre = re.sub(rf"_(?:{sufijos_post}|baseline|before|initial)$", "", nombre, flags=re.I)
    return nombre


def extraer_metadata_nuevo(path):
    nombre = Path(path).name
    es_all_info = nombre.lower().startswith("all_info_")
    limpio = nombre[len("All_info_"):] if es_all_info else nombre

    patron = re.compile(
        r"^(?P<muestra>.+)_Array(?P<array>\d+)_random_"
        r"(?P<rep>\d+)_(?P<electrolito>.+)\.txt$",
        re.IGNORECASE,
    )
    m = patron.match(limpio)
    if not m:
        return None

    muestra_cruda = m.group("muestra")
    muestra = normalizar_muestra(muestra_cruda)
    stage = ""
    for sufijo in SUFIJOS_A_ETIQUETAS:
        if re.search(rf"{re.escape(sufijo)}$", muestra_cruda, re.I):
            stage = sufijo.lstrip("_")
            break

    return {
        "muestra": muestra,
        "muestra_cruda": muestra_cruda,
        "array": int(m.group("array")),
        "rep": int(m.group("rep")),
        "electrolito": m.group("electrolito"),
        "stage": stage,
        "all_info": es_all_info,
        "path": Path(path),
    }


def extraer_metadata_antiguo(path):
    nombre = Path(path).name

    m = re.match(
        r"^Id_Vfg__(?P<muestra>.+)_(?P<array>\d+)_"
        r"(?P<extra>.+)_GRATMA-(?P<rep>\d+)\.txt$",
        nombre,
        re.IGNORECASE,
    )
    if m:
        return {
            "muestra": normalizar_muestra(m.group("muestra")),
            "muestra_cruda": m.group("muestra"),
            "array": int(m.group("array")),
            "rep": int(m.group("rep")),
            "electrolito": "",
            "stage": "",
            "all_info": False,
            "path": Path(path),
        }

    m = re.match(
        r"^All_info_(?P<muestra>.+)_(?P<array>\d+)_"
        r"(?P<rep>\d+)_(?P<extra>.+)\.txt$",
        nombre,
        re.IGNORECASE,
    )
    if m:
        return {
            "muestra": normalizar_muestra(m.group("muestra")),
            "muestra_cruda": m.group("muestra"),
            "array": int(m.group("array")),
            "rep": int(m.group("rep")),
            "electrolito": "",
            "stage": "",
            "all_info": True,
            "path": Path(path),
        }

    return None


def clave_archivo(meta):
    return (meta["muestra"], meta["electrolito"], meta["array"], meta["rep"])


def localizar_medidas(carpeta):
    """Localiza una sola fuente por muestra/electrolito/array/repetición."""
    medidas = {}

    # Nuevos: primero definitivos, después All_info como respaldo.
    for path in sorted(buscar_archivos(carpeta, "*_Array*_random_*.txt")):
        meta = extraer_metadata_nuevo(path)
        if not meta or meta["all_info"]:
            continue
        medidas[clave_archivo(meta)] = meta

    for path in sorted(buscar_archivos(carpeta, "All_info_*_Array*_random_*.txt")):
        meta = extraer_metadata_nuevo(path)
        if not meta:
            continue
        medidas.setdefault(clave_archivo(meta), meta)

    # Antiguos.
    for patron in ("Id_Vfg__*GRATMA-*.txt", "All_info_*.txt"):
        for path in sorted(buscar_archivos(carpeta, patron)):
            # Evitar reanalizar All_info nuevos como antiguos.
            if extraer_metadata_nuevo(path) is not None:
                continue
            meta = extraer_metadata_antiguo(path)
            if not meta:
                continue
            medidas.setdefault(clave_archivo(meta), meta)

    return medidas


def convertir_numero(texto):
    return float(texto.strip().replace(",", "."))


def leer_curva(path):
    """
    Devuelve Vfg [V] e Is [uA].

    Para 4 columnas usa siempre Is, tanto si la segunda columna es Id como Vs.
    """
    voltajes = []
    corrientes = []
    indice_v = None
    indice_i = None
    dentro_tabla = False

    with open(path, "r", encoding="utf-8", errors="ignore") as archivo:
        for linea in archivo:
            texto = linea.strip()
            if not texto or texto.startswith("#"):
                continue

            cabecera = [p.strip() for p in texto.split(";")]
            norm = [p.lower() for p in cabecera]

            if "vfg" in norm:
                if "is" in norm:
                    indice_v = norm.index("vfg")
                    indice_i = norm.index("is")
                    dentro_tabla = True
                    continue
                if "id" in norm and len(norm) == 2:
                    # Compatibilidad muy antigua Vfg;Id.
                    indice_v = norm.index("vfg")
                    indice_i = norm.index("id")
                    dentro_tabla = True
                    continue

            if dentro_tabla and indice_v is not None and indice_i is not None:
                columnas = [p.strip() for p in texto.split(";")]
                if len(columnas) <= max(indice_v, indice_i):
                    continue
                try:
                    v = convertir_numero(columnas[indice_v])
                    i = convertir_numero(columnas[indice_i])
                except ValueError:
                    continue
                if np.isfinite(v) and np.isfinite(i):
                    voltajes.append(v)
                    corrientes.append(i)
                continue

            # Fallback sin cabecera.
            partes = texto.replace(",", ".").replace(";", " ").split()
            try:
                numeros = [float(x) for x in partes]
            except ValueError:
                continue
            if len(numeros) >= 4:
                voltajes.append(numeros[0])
                corrientes.append(numeros[3])
            elif len(numeros) >= 2:
                voltajes.append(numeros[0])
                corrientes.append(numeros[1])

    if len(voltajes) < 3:
        raise ValueError("no se encontraron suficientes puntos numéricos")

    v = np.asarray(voltajes, dtype=float)
    i = np.asarray(corrientes, dtype=float) * A_MICROAMPERIOS

    mascara = np.isfinite(v) & np.isfinite(i)
    v = v[mascara]
    i = i[mascara]
    if len(v) < 3:
        raise ValueError("menos de tres puntos válidos")

    return v, i


def separar_forward_backward(v, i):
    v = np.asarray(v, dtype=float)
    i = np.asarray(i, dtype=float)
    if len(v) < 3:
        return (v, i), (np.array([]), np.array([]))

    vertice = int(np.argmax(v))
    forward = (v[: vertice + 1], i[: vertice + 1])
    if vertice >= len(v) - 2:
        backward = (np.array([]), np.array([]))
    else:
        backward = (v[vertice:], i[vertice:])
    return forward, backward


def ordenar_para_interp(v, i):
    orden = np.argsort(v)
    v = np.asarray(v)[orden]
    i = np.asarray(i)[orden]
    v_unica, idx = np.unique(v, return_index=True)
    i_unica = i[idx]
    return v_unica, i_unica


def media_rama(curvas_rama, n=PUNTOS_INTERPOLACION):
    validas = [(v, i) for v, i in curvas_rama if len(v) >= 2]
    if not validas:
        return None

    minimo = max(float(np.min(v)) for v, _ in validas)
    maximo = min(float(np.max(v)) for v, _ in validas)
    if minimo >= maximo:
        return None

    x = np.linspace(minimo, maximo, n)
    ys = []
    for v, i in validas:
        vo, io = ordenar_para_interp(v, i)
        if len(vo) < 2:
            continue
        ys.append(np.interp(x, vo, io))
    if not ys:
        return None

    matriz = np.asarray(ys, dtype=float)
    return {
        "x": x,
        "mean": np.mean(matriz, axis=0),
        "std": np.std(matriz, axis=0),
        "n": len(matriz),
    }


def vdirac(v, i):
    """Mismo criterio que tu script de gráficas: mínimo de Is."""
    if len(v) == 0:
        return np.nan
    return float(v[int(np.argmin(i))])


def cargar_dataset(medidas, etiqueta):
    registros = []
    for clave in sorted(medidas):
        meta = medidas[clave]
        array = meta["array"]
        if ARRAYS_A_COMPARAR is not None and array not in ARRAYS_A_COMPARAR:
            continue
        if meta["rep"] not in REPS_A_USAR:
            continue
        try:
            v, i = leer_curva(meta["path"])
            fwd, bwd = separar_forward_backward(v, i)
        except Exception as exc:
            print(f"  [WARN] {etiqueta} A{array} rep {meta['rep']}: {exc}")
            continue

        registros.append(
            {
                **meta,
                "condicion": etiqueta,
                "v": v,
                "i": i,
                "forward": fwd,
                "backward": bwd,
                "vdirac_f": vdirac(*fwd),
                "vdirac_b": vdirac(*bwd),
            }
        )
    return registros


def agrupar_por_muestra_y_array(registros):
    grupos = defaultdict(list)
    for r in registros:
        clave = (r["muestra"], r["electrolito"], r["array"])
        grupos[clave].append(r)
    return grupos


def media_std_valores(valores):
    arr = np.asarray([x for x in valores if np.isfinite(x)], dtype=float)
    if len(arr) == 0:
        return np.nan, np.nan, 0
    return float(np.mean(arr)), float(np.std(arr)), int(len(arr))


def comparar_medias_curva(media_antes, media_aging, rango=None):
    """
    Compara dos curvas medias en el rango Vfg común (o en la intersección de
    ese rango con `rango`, si se indica -> (vmin, vmax) en voltios).

    Devuelve:
      RMSE normalizado (%) respecto al rango dinámico de la curva antigua,
      correlación de Pearson,
      cambio de área AUC (%).
    """
    if media_antes is None or media_aging is None:
        return np.nan, np.nan, np.nan

    xa = media_antes["x"]
    ya = media_antes["mean"]
    xb = media_aging["x"]
    yb = media_aging["mean"]

    minimo = max(float(np.min(xa)), float(np.min(xb)))
    maximo = min(float(np.max(xa)), float(np.max(xb)))
    if rango is not None:
        minimo = max(minimo, rango[0])
        maximo = min(maximo, rango[1])
    if minimo >= maximo:
        return np.nan, np.nan, np.nan

    x = np.linspace(minimo, maximo, PUNTOS_INTERPOLACION)
    ia = np.interp(x, xa, ya)
    ib = np.interp(x, xb, yb)

    rmse = float(np.sqrt(np.mean((ib - ia) ** 2)))
    rango_y = float(np.max(ia) - np.min(ia))
    if abs(rango_y) < EPS:
        escala = max(float(np.mean(np.abs(ia))), EPS)
    else:
        escala = abs(rango_y)
    rmse_pct = 100.0 * rmse / escala

    if np.std(ia) < EPS or np.std(ib) < EPS:
        corr = np.nan
    else:
        corr = float(np.corrcoef(ia, ib)[0, 1])

    # Compatibilidad con distintas versiones de NumPy.
    # No usamos getattr(..., np.trapz) porque el argumento por defecto se
    # evalúa inmediatamente y NumPy 2.x/3.x puede no exponer np.trapz.
    if hasattr(np, "trapezoid"):
        auc_a = float(np.trapezoid(ia, x))
        auc_b = float(np.trapezoid(ib, x))
    elif hasattr(np, "trapz"):
        auc_a = float(np.trapz(ia, x))
        auc_b = float(np.trapz(ib, x))
    else:
        # Último recurso sin depender de una función concreta de NumPy.
        dx = np.diff(x)
        auc_a = float(np.sum((ia[:-1] + ia[1:]) * 0.5 * dx))
        auc_b = float(np.sum((ib[:-1] + ib[1:]) * 0.5 * dx))
    if abs(auc_a) < EPS:
        auc_pct = np.nan
    else:
        auc_pct = 100.0 * (auc_b - auc_a) / abs(auc_a)

    return rmse_pct, corr, auc_pct


def resumen_array(reg_antes, reg_aging):
    vf_a, vf_a_std, nf_a = media_std_valores([r["vdirac_f"] for r in reg_antes])
    vb_a, vb_a_std, nb_a = media_std_valores([r["vdirac_b"] for r in reg_antes])
    vf_g, vf_g_std, nf_g = media_std_valores([r["vdirac_f"] for r in reg_aging])
    vb_g, vb_g_std, nb_g = media_std_valores([r["vdirac_b"] for r in reg_aging])

    hyst_a_vals = [
        abs(r["vdirac_b"] - r["vdirac_f"])
        for r in reg_antes
        if np.isfinite(r["vdirac_b"]) and np.isfinite(r["vdirac_f"])
    ]
    hyst_g_vals = [
        abs(r["vdirac_b"] - r["vdirac_f"])
        for r in reg_aging
        if np.isfinite(r["vdirac_b"]) and np.isfinite(r["vdirac_f"])
    ]
    hyst_a, hyst_a_std, _ = media_std_valores(hyst_a_vals)
    hyst_g, hyst_g_std, _ = media_std_valores(hyst_g_vals)

    mf_a = media_rama([r["forward"] for r in reg_antes])
    mf_g = media_rama([r["forward"] for r in reg_aging])
    mb_a = media_rama([r["backward"] for r in reg_antes])
    mb_g = media_rama([r["backward"] for r in reg_aging])

    # RMSE / correlación / AUC en todo el rango común.
    rmse_f, corr_f, auc_f = comparar_medias_curva(mf_a, mf_g)
    rmse_b, corr_b, auc_b = comparar_medias_curva(mb_a, mb_g)

    # RMSE / AUC en una ventana centrada en el punto de Dirac medio
    # (promedio entre "antes" y "aging"), de semiancho SEMIANCHO_VENTANA_MINIMO.
    centro_f = np.nanmean([vf_a, vf_g]) if (np.isfinite(vf_a) or np.isfinite(vf_g)) else np.nan
    centro_b = np.nanmean([vb_a, vb_g]) if (np.isfinite(vb_a) or np.isfinite(vb_g)) else np.nan

    ventana_f = (
        (centro_f - SEMIANCHO_VENTANA_MINIMO, centro_f + SEMIANCHO_VENTANA_MINIMO)
        if np.isfinite(centro_f)
        else None
    )
    ventana_b = (
        (centro_b - SEMIANCHO_VENTANA_MINIMO, centro_b + SEMIANCHO_VENTANA_MINIMO)
        if np.isfinite(centro_b)
        else None
    )

    rmse_f_win, _, auc_f_win = comparar_medias_curva(mf_a, mf_g, ventana_f)
    rmse_b_win, _, auc_b_win = comparar_medias_curva(mb_a, mb_g, ventana_b)

    rmses = [x for x in (rmse_f, rmse_b) if np.isfinite(x)]
    cambio_curva = float(np.mean(rmses)) if rmses else np.nan

    return {
        # --- Cambios (lo único que se exporta a CSV) ---
        "delta_vdirac_f_V": vf_g - vf_a if np.isfinite(vf_a) and np.isfinite(vf_g) else np.nan,
        "delta_vdirac_b_V": vb_g - vb_a if np.isfinite(vb_a) and np.isfinite(vb_g) else np.nan,
        "delta_histeresis_V": hyst_g - hyst_a if np.isfinite(hyst_a) and np.isfinite(hyst_g) else np.nan,
        "rmse_forward_full_pct": rmse_f,
        "rmse_backward_full_pct": rmse_b,
        "rmse_forward_window_pct": rmse_f_win,
        "rmse_backward_window_pct": rmse_b_win,
        "auc_forward_full_change_pct": auc_f,
        "auc_backward_full_change_pct": auc_b,
        "auc_forward_window_change_pct": auc_f_win,
        "auc_backward_window_change_pct": auc_b_win,
        "corr_forward": corr_f,
        "corr_backward": corr_b,
        "cambio_curva_pct": cambio_curva,
        # --- Valores intermedios (solo para las gráficas, no van al CSV) ---
        "vdirac_f_antes": vf_a,
        "vdirac_f_antes_std": vf_a_std,
        "vdirac_f_aging": vf_g,
        "vdirac_f_aging_std": vf_g_std,
        "vdirac_b_antes": vb_a,
        "vdirac_b_antes_std": vb_a_std,
        "vdirac_b_aging": vb_g,
        "vdirac_b_aging_std": vb_g_std,
        "histeresis_antes": hyst_a,
        "histeresis_antes_std": hyst_a_std,
        "histeresis_aging": hyst_g,
        "histeresis_aging_std": hyst_g_std,
        "media_forward_antes": mf_a,
        "media_forward_aging": mf_g,
        "media_backward_antes": mb_a,
        "media_backward_aging": mb_g,
        # --- Nº de repeticiones válidas usadas en cada VDirac (solo diagnóstico) ---
        "n_vdirac_f_antes": nf_a,
        "n_vdirac_b_antes": nb_a,
        "n_vdirac_f_aging": nf_g,
        "n_vdirac_b_aging": nb_g,
    }


def construir_comparaciones(reg_antes, reg_aging):
    ga = agrupar_por_muestra_y_array(reg_antes)
    gg = agrupar_por_muestra_y_array(reg_aging)
    claves = sorted(set(ga) & set(gg))

    comparaciones = []
    for muestra, electrolito, array in claves:
        r = resumen_array(ga[(muestra, electrolito, array)], gg[(muestra, electrolito, array)])
        r.update({"muestra": muestra, "electrolito": electrolito, "array": array})
        comparaciones.append(r)
    return comparaciones


def titulo_grupo(muestra, electrolito):
    return f"{muestra} — {electrolito}" if electrolito else muestra


def separar_wafer_chip(muestra):
    """
    Asume el convenio <wafer>_<chip> (el primer guion bajo separa ambas
    partes). Si la muestra no tiene guion bajo, se trata todo como nombre
    de chip y el wafer queda vacío.

    OJO: esto es una suposición sobre el formato del nombre de muestra —
    confírmalo o ajústalo si tu convenio de nombres es distinto.
    """
    partes = muestra.split("_", 1)
    if len(partes) == 2:
        return partes[0], partes[1]
    return "", muestra


def construir_nombre_base(muestra):
    wafer, chip = separar_wafer_chip(muestra)
    nombre = f"{wafer},{chip}" if wafer else chip
    return re.sub(r'[<>:"/\\|?*]+', "_", nombre)


def configurar_ax(ax, grid_axis="y"):
    ax.tick_params(direction="in", top=True, right=True)
    for spine in ax.spines.values():
        spine.set_linewidth(1.2)
    ax.grid(axis=grid_axis, alpha=0.20)


def figura_resumen(comps, salida, titulo, nombre_base, etiqueta_post):
    arrays = [c["array"] for c in comps]
    x_cat = np.arange(len(arrays), dtype=float)
    offset = 0.12
    ancho = 0.35

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    ax1, ax2, ax3, ax4 = axes.flat

    # 1) Dirac Forward (vertical)
    before = [c["vdirac_f_antes"] for c in comps]
    aging = [c["vdirac_f_aging"] for c in comps]
    eb = [c["vdirac_f_antes_std"] for c in comps]
    ea = [c["vdirac_f_aging_std"] for c in comps]
    ax1.errorbar(x_cat - offset, before, yerr=eb, marker="o", linestyle="none", capsize=3, label="Before")
    ax1.errorbar(x_cat + offset, aging, yerr=ea, marker="o", linestyle="none", capsize=3, label=etiqueta_post)
    ax1.set_title("Dirac Point — Forward", fontweight="bold")
    ax1.set_ylabel("VDirac (V)")
    ax1.set_xticks(x_cat, [f"A{a}" for a in arrays])
    ax1.legend(frameon=False)
    configurar_ax(ax1)

    # 2) Dirac Backward (vertical)
    before = [c["vdirac_b_antes"] for c in comps]
    aging = [c["vdirac_b_aging"] for c in comps]
    eb = [c["vdirac_b_antes_std"] for c in comps]
    ea = [c["vdirac_b_aging_std"] for c in comps]
    ax2.errorbar(x_cat - offset, before, yerr=eb, marker="o", linestyle="none", capsize=3, label="Before")
    ax2.errorbar(x_cat + offset, aging, yerr=ea, marker="o", linestyle="none", capsize=3, label=etiqueta_post)
    ax2.set_title("Dirac Point — Backward", fontweight="bold")
    ax2.set_ylabel("VDirac (V)")
    ax2.set_xticks(x_cat, [f"A{a}" for a in arrays])
    ax2.legend(frameon=False)
    configurar_ax(ax2)

    # 3) Histéresis (vertical, sin cambios de orientación)
    h_b = np.asarray([c["histeresis_antes"] for c in comps], dtype=float)
    h_a = np.asarray([c["histeresis_aging"] for c in comps], dtype=float)
    h_b_std = np.asarray([c["histeresis_antes_std"] for c in comps], dtype=float)
    h_a_std = np.asarray([c["histeresis_aging_std"] for c in comps], dtype=float)
    ax3.bar(x_cat - ancho / 2, h_b, width=ancho, yerr=h_b_std, capsize=3, label="Before")
    ax3.bar(x_cat + ancho / 2, h_a, width=ancho, yerr=h_a_std, capsize=3, label=etiqueta_post)
    ax3.set_title("Dirac Hysteresis", fontweight="bold")
    ax3.set_ylabel(r"|VDirac$_B$ - VDirac$_F$| (V)")
    ax3.set_xticks(x_cat, [f"A{a}" for a in arrays])
    ax3.legend(frameon=False)
    configurar_ax(ax3)

    # 4) Cambio global de forma (vertical, sin cambios de orientación)
    cambios = np.asarray([c["cambio_curva_pct"] for c in comps], dtype=float)
    ax4.bar(x_cat, cambios)
    ax4.set_title("Overall Is–Vfg curve change", fontweight="bold")
    ax4.set_ylabel("Normalized RMSE (%)")
    ax4.set_xticks(x_cat, [f"A{a}" for a in arrays])
    configurar_ax(ax4)

    # Etiquetar los tres más cambiados sin imponer umbrales arbitrarios.
    validos = [(idx, val) for idx, val in enumerate(cambios) if np.isfinite(val)]
    top = sorted(validos, key=lambda t: t[1], reverse=True)[:3]
    for idx, val in top:
        ax4.text(idx, val, f" {val:.1f}%", ha="center", va="bottom", fontsize=9, fontweight="bold")

    fig.suptitle(f"{titulo}\nBefore vs {etiqueta_post} comparison", fontsize=18, fontweight="bold")
    fig.text(
        0.5,
        0.012,
        "Error bars: ±1 standard deviation across the last 3 of 5 repetitions. "
        f"RMSE: difference between the mean curves before and after {etiqueta_post.lower()}.",
        ha="center",
        fontsize=9,
    )
    fig.tight_layout(rect=(0, 0.035, 1, 0.94))
    ruta = salida / f"01_{nombre_base}.png"
    fig.savefig(ruta, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    return ruta


def plot_media(ax, media, etiqueta, estilo):
    if media is None:
        return
    x = media["x"]
    y = media["mean"]
    std = media["std"]
    ax.plot(x, y, linestyle=estilo, linewidth=2.0, label=etiqueta)
    ax.fill_between(x, y - std, y + std, alpha=0.12)


def figura_detalle(comps, salida, titulo, top_n, nombre_base, etiqueta_post):
    validas = [c for c in comps if np.isfinite(c["cambio_curva_pct"])]
    ordenadas_por_cambio = sorted(validas, key=lambda c: c["cambio_curva_pct"], reverse=True)
    seleccion = ordenadas_por_cambio[: max(1, min(top_n, len(ordenadas_por_cambio)))]
    if not seleccion:
        return None
    # Se eligen por magnitud de cambio, pero se muestran en orden de array.
    seleccion.sort(key=lambda c: c["array"])

    fig, axes = plt.subplots(len(seleccion), 2, figsize=(13, 4.0 * len(seleccion)), squeeze=False)

    for fila, c in enumerate(seleccion):
        axf, axb = axes[fila]
        array = c["array"]

        plot_media(axf, c["media_forward_antes"], "Before", "-")
        plot_media(axf, c["media_forward_aging"], etiqueta_post, "--")
        axf.set_title(f"A{array} — Forward", fontweight="bold")
        axf.set_xlabel("Vfg (V)")
        axf.set_ylabel("Is (µA)")
        axf.legend(frameon=False)
        configurar_ax(axf)

        plot_media(axb, c["media_backward_antes"], "Before", "-")
        plot_media(axb, c["media_backward_aging"], etiqueta_post, "--")
        axb.set_title(
            f"A{array} — Backward  |  overall change ≈ {c['cambio_curva_pct']:.1f}%",
            fontweight="bold",
        )
        axb.set_xlabel("Vfg (V)")
        axb.set_ylabel("Is (µA)")
        axb.legend(frameon=False)
        configurar_ax(axb)

    fig.suptitle(
        f"{titulo}\nArrays with the largest curve change",
        fontsize=17,
        fontweight="bold",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    ruta = salida / f"02_{nombre_base}.png"
    fig.savefig(ruta, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    return ruta


def guardar_csv(comps, salida, nombre_base):
    ruta = salida / f"03_{nombre_base}.csv"
    campos = [
        "muestra",
        "electrolito",
        "array",
        "delta_vdirac_f_V",
        "delta_vdirac_b_V",
        "delta_histeresis_V",
        "rmse_forward_full_pct",
        "rmse_backward_full_pct",
        "rmse_forward_window_pct",
        "rmse_backward_window_pct",
        "auc_forward_full_change_pct",
        "auc_backward_full_change_pct",
        "auc_forward_window_change_pct",
        "auc_backward_window_change_pct",
        "corr_forward",
        "corr_backward",
    ]
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        writer.writeheader()
        for c in comps:
            fila = {k: c.get(k, "") for k in campos}
            writer.writerow(fila)
    return ruta


def imprimir_resumen(comps):
    print("\nResumen de cambios por array:")
    print("  Array | ΔDirac F (V) | ΔDirac B (V) | ΔHyst (V) | Cambio curva (%)")
    print("  " + "-" * 72)
    for c in comps:
        def fmt(x, nd=4):
            return f"{x:.{nd}f}" if np.isfinite(x) else "N/D"
        print(
            f"  A{c['array']:<5} | {fmt(c['delta_vdirac_f_V']):>12} | "
            f"{fmt(c['delta_vdirac_b_V']):>12} | {fmt(c['delta_histeresis_V']):>10} | "
            f"{fmt(c['cambio_curva_pct'], 1):>15}"
        )

    avisar_repeticiones_insuficientes(comps)


def avisar_repeticiones_insuficientes(comps):
    """
    Aviso de diagnóstico (no se guarda en el CSV): indica si alguna condición
    de un array se calculó con menos de las 3 repeticiones esperadas
    (REPS_A_USAR), lo que puede explicar barras de error nulas o muy chicas.
    """
    esperado = len(REPS_A_USAR)
    etiquetas = {
        "n_vdirac_f_antes": "Dirac F antes",
        "n_vdirac_b_antes": "Dirac B antes",
        "n_vdirac_f_aging": "Dirac F aging",
        "n_vdirac_b_aging": "Dirac B aging",
    }
    avisos = []
    for c in comps:
        for campo, etiqueta in etiquetas.items():
            n = c.get(campo, 0)
            if n < esperado:
                avisos.append(f"  [AVISO] A{c['array']}: {etiqueta} calculado con solo {n}/{esperado} repeticiones válidas")

    if avisos:
        print("\nRepeticiones incompletas detectadas (pueden explicar barras de error pequeñas o ausentes):")
        for aviso in avisos:
            print(aviso)


def main():
    args = analizar_argumentos()

    print("=" * 76)
    print("GRATMA — COMPARADOR DE ENVEJECIMIENTO")
    print("Antes vs Aging | resumen visual por array")
    print("=" * 76)

    carpeta_antes = resolver_carpeta(
        args.antes,
        "Selecciona la carpeta de medidas ANTIGUAS (antes de la funcionalización)",
    )
    if carpeta_antes is None:
        print("Operación cancelada.")
        raise SystemExit(0)

    carpeta_aging = resolver_carpeta(
        args.aging,
        "Selecciona la carpeta de medidas FUNCIONALIZADAS (medidas actuales)",
        carpeta_antes,
    )
    if carpeta_aging is None:
        print("Operación cancelada.")
        raise SystemExit(0)

    # Convenio del laboratorio: la carpeta "post" termina en alguno de los
    # sufijos de SUFIJOS_A_ETIQUETAS (p.ej. _aging o _funcionalizado).
    # Esto evita que una selección accidentalmente invertida produzca la
    # comparación al revés o guarde los resultados en la carpeta antigua.
    carpeta_antes, carpeta_aging, intercambiadas = detectar_orden_antes_aging(
        carpeta_antes, carpeta_aging
    )
    etiqueta_post = etiqueta_post_para_carpeta(carpeta_aging)
    if intercambiadas:
        print(
            f"\n[INFO] Se detectó la carpeta terminada en un sufijo reconocido "
            f"({', '.join(SUFIJOS_A_ETIQUETAS)}). Se ha corregido automáticamente "
            f"el orden Antes/{etiqueta_post}."
        )
    elif not es_carpeta_aging(carpeta_aging):
        print(
            f"\n[AVISO] La carpeta seleccionada como '{etiqueta_post}' no termina en "
            f"ninguno de los sufijos reconocidos ({', '.join(SUFIJOS_A_ETIQUETAS)}). "
            "Se respetará el orden seleccionado."
        )

    salida = (
        Path(args.salida).expanduser().resolve()
        if args.salida
        else Path(RUTA_SALIDA_FIJA)
    )
    salida.mkdir(parents=True, exist_ok=True)

    print(f"\nAntes : {carpeta_antes}")
    print(f"Aging : {carpeta_aging}")
    print(f"Salida: {salida}")

    medidas_antes = localizar_medidas(carpeta_antes)
    medidas_aging = localizar_medidas(carpeta_aging)
    print(f"\nArchivos únicos detectados — antes: {len(medidas_antes)} | aging: {len(medidas_aging)}")

    reg_antes = cargar_dataset(medidas_antes, "antes")
    reg_aging = cargar_dataset(medidas_aging, "aging")
    print(f"Curvas válidas (reps {sorted(REPS_A_USAR)}) — antes: {len(reg_antes)} | aging: {len(reg_aging)}")

    comparaciones = construir_comparaciones(reg_antes, reg_aging)
    if not comparaciones:
        print("\nERROR: no se encontraron arrays equivalentes entre ambas carpetas.")
        print("El emparejamiento se hace por muestra/chip + electrolito + número de array.")
        raise SystemExit(1)

    # Agrupar por muestra/electrolito para no mezclar chips distintos en una figura.
    grupos = defaultdict(list)
    for c in comparaciones:
        grupos[(c["muestra"], c["electrolito"])].append(c)

    for (muestra, electrolito), comps in sorted(grupos.items()):
        comps.sort(key=lambda c: c["array"])
        titulo = titulo_grupo(muestra, electrolito)
        nombre_base = construir_nombre_base(muestra)

        print("\n" + "-" * 76)
        print(f"Comparando: {titulo}")
        print(f"Arrays comunes: {[c['array'] for c in comps]}")
        imprimir_resumen(comps)

        p1 = figura_resumen(comps, salida, titulo, nombre_base, etiqueta_post)
        p2 = figura_detalle(comps, salida, titulo, args.top, nombre_base, etiqueta_post)
        pcsv = guardar_csv(comps, salida, nombre_base)

        print(f"\nGuardado: {p1}")
        if p2:
            print(f"Guardado: {p2}")
        print(f"Guardado: {pcsv}")

    print("\n" + "=" * 76)
    print("HECHO")
    print("La figura 01 es la que debes mirar primero para evaluar el envejecimiento.")
    print("La figura 02 enseña solo los arrays con mayor cambio de curva.")
    print(f"Resultados: {salida}")
    print("=" * 76)

    if ABRIR_CARPETA_AL_TERMINAR and not args.no_abrir:
        abrir_carpeta(salida)


if __name__ == "__main__":
    main()
