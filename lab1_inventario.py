#!/usr/bin/env python3

import argparse
import getpass
import json
import os
import re
from datetime import datetime
from pathlib import Path

import yaml
from netmiko import (
    ConnectHandler,
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

BASE = Path(__file__).resolve().parent
LOGS = BASE / "logs"
REPORTE = BASE / "reporte_estado.json"


# ---------------------------------------------------------------------------
# R1 / R2: inventario y credenciales
# ---------------------------------------------------------------------------

CAMPOS_OBLIGATORIOS = ("nombre", "host", "device_type", "usuario", "rol")


def cargar_inventario(ruta):
    """Lee el YAML y valida que cada equipo tenga los campos mínimos."""
    with open(ruta, encoding="utf-8") as f:
        datos = yaml.safe_load(f)
    for equipo in datos["equipos"]:
        faltan = [c for c in CAMPOS_OBLIGATORIOS if c not in equipo]
        if faltan:
            raise ValueError(f"{equipo.get('nombre', '?')}: faltan campos {faltan}")
        if equipo["rol"] not in ("lectura", "escritura"):
            raise ValueError(f"{equipo['nombre']}: rol debe ser lectura o escritura")
    return datos


def obtener_clave(nombre):
    """LAB_PASS_<NOMBRE> (mayúsculas, '-' -> '_'); si no existe, getpass."""
    variable = "LAB_PASS_" + nombre.upper().replace("-", "_")
    clave = os.environ.get(variable)
    if not clave:
        clave = getpass.getpass(f"Clave de {nombre} ({variable} no definida): ")
    return clave


# ---------------------------------------------------------------------------
# R4: parsers (texto -> diccionarios)
# ---------------------------------------------------------------------------

def texto_a_dict(texto):
    """Convierte líneas 'clave: valor' (formato de RouterOS print) a dict."""
    resultado = {}
    ultima = None
    for linea in texto.splitlines():
        if ":" in linea:
            clave, valor = linea.split(":", 1)
            ultima = clave.strip()
            resultado[ultima] = valor.strip()
        elif ultima and linea.strip():

            resultado[ultima] += linea.strip()
    return resultado


PAR_CLAVE_VALOR = re.compile(r'([\w.-]+)=("[^"]*"|\S*)')


def terse_a_lista(texto):
    """'print terse' produce un registro por línea con pares clave=valor."""
    registros = []
    for linea in texto.splitlines():
        pares = {k: v.strip('"') for k, v in PAR_CLAVE_VALOR.findall(linea)}
        if pares:
            registros.append(pares)
    return registros


def ros_nombre(texto):
    return texto_a_dict(texto).get("name")


def ros_uptime(texto):
    return texto_a_dict(texto).get("uptime")


def ros_interfaces(texto):
    return [
        {"interfaz": r.get("interface"), "ip": r.get("address")}
        for r in terse_a_lista(texto)
        if "address" in r
    ]


def ros_ruta_defecto(texto):
    rutas = terse_a_lista(texto)
    return rutas[0].get("gateway") if rutas else None


def ios_nombre(texto):
    m = re.search(r"^hostname\s+(\S+)", texto, re.M)
    return m.group(1) if m else None


def ios_uptime(texto):
    m = re.search(r"uptime is (.+)", texto)
    return m.group(1).strip() if m else None


def ios_interfaces(texto):
    interfaces = []
    for linea in texto.splitlines():
        partes = linea.split()
        if len(partes) < 6 or partes[0] == "Interface":
            continue
        interfaces.append({
            "interfaz": partes[0],
            "ip": None if partes[1] == "unassigned" else partes[1],
            "estado": " ".join(partes[4:-1]),
            "protocolo": partes[-1],
        })
    return interfaces


def ios_ruta_defecto(texto):
    m = re.search(r"\*\s+(\d+\.\d+\.\d+\.\d+)", texto)
    return m.group(1) if m else None

COMANDOS = {
    "mikrotik_routeros": {
        "nombre": ("/system identity print", ros_nombre),
        "uptime": ("/system resource print", ros_uptime),
        "interfaces": ("/ip address print terse", ros_interfaces),
        "ruta_defecto": ("/ip route print terse where dst-address=0.0.0.0/0", ros_ruta_defecto),
    },
    "cisco_ios": {
        "nombre": ("show running-config | include hostname", ios_nombre),
        "uptime": ("show version | include uptime", ios_uptime),
        "interfaces": ("show ip interface brief", ios_interfaces),
        "ruta_defecto": ("show ip route 0.0.0.0", ios_ruta_defecto),
    },
}

COMANDOS["cisco_ios_telnet"] = COMANDOS["cisco_ios"]


def recolectar_estado(conn, device_type):
    estado = {}
    for dato, (comando, parser) in COMANDOS[device_type].items():
        estado[dato] = parser(conn.send_command(comando))
    if not estado["nombre"]:
        estado["nombre"] = conn.find_prompt().rstrip("#>")
    return estado


# ---------------------------------------------------------------------------
# R6: cambio idempotente en el MikroTik
# ---------------------------------------------------------------------------

def ros(conn, comando):
    """Ejecuta un comando RouterOS y devuelve la salida sin espacios."""
    return conn.send_command(comando).strip()


def ros_cantidad(conn, find):
    """Cuántos objetos coinciden con un [find ...]: ej. '/interface bridge find name=x'."""
    salida = ros(conn, f":put [:len [{find}]]")
    return int(salida) if salida.isdigit() else 0


def asegurar_loopback(conn, n, nombre_estudiante):
    """Crea lo-ssh-N y 10.253.0.N/32 solo si faltan. Devuelve la lista de cambios."""
    interfaz = f"lo-ssh-{n}"
    ip = f"10.253.0.{n}/32"
    cambios = []

    if ros_cantidad(conn, f'/interface bridge find name="{interfaz}"') == 0:
        ros(conn, f'/interface bridge add name="{interfaz}" comment="{nombre_estudiante}"')
        cambios.append(f"creado bridge {interfaz}")
    else:
        comentario = ros(conn, f':put [/interface bridge get [find name="{interfaz}"] comment]')
        if comentario != nombre_estudiante:
            ros(conn, f'/interface bridge set [find name="{interfaz}"] comment="{nombre_estudiante}"')
            cambios.append(f"actualizado comentario de {interfaz}")

    en_otra = ros_cantidad(
        conn, f'/ip address find address="{ip}" and interface!="{interfaz}"'
    )
    if en_otra:
        raise RuntimeError(f"{ip} ya existe en otra interfaz; no se modifica")
    if ros_cantidad(conn, f'/ip address find address="{ip}" and interface="{interfaz}"') == 0:
        ros(conn, f'/ip address add address={ip} interface="{interfaz}" comment="{nombre_estudiante}"')
        cambios.append(f"agregada IP {ip}")

    return cambios


def verificar_loopback(conn, n):
    """Lectura posterior al cambio: confirma bridge e IP."""
    interfaz = f"lo-ssh-{n}"
    salida = conn.send_command(f'/ip address print terse where interface="{interfaz}"')
    ips = [r.get("address") for r in terse_a_lista(salida)]
    return {
        "bridge_existe": ros_cantidad(conn, f'/interface bridge find name="{interfaz}"') == 1,
        "ips": ips,
        "ok": f"10.253.0.{n}/32" in ips,
    }


def limpiar_loopback(conn, n):
    """Elimina SOLO la IP y el bridge lo-ssh-N (filtrando por nombre exacto)."""
    interfaz = f"lo-ssh-{n}"
    cambios = []
    if ros_cantidad(conn, f'/ip address find interface="{interfaz}"'):
        ros(conn, f'/ip address remove [find interface="{interfaz}"]')
        cambios.append(f"eliminadas IPs de {interfaz}")
    if ros_cantidad(conn, f'/interface bridge find name="{interfaz}"'):
        ros(conn, f'/interface bridge remove [find name="{interfaz}"]')
        cambios.append(f"eliminado bridge {interfaz}")
    return cambios


# ---------------------------------------------------------------------------
# Extra: estado del MikroTik por API REST
# ---------------------------------------------------------------------------

def estado_rest(host, usuario, clave):
    import requests
    import urllib3

    urllib3.disable_warnings()
    base = f"https://{host}/rest"

    def get(ruta, **params):
        r = requests.get(f"{base}{ruta}", auth=(usuario, clave), params=params,
                         verify=False, timeout=10)
        r.raise_for_status()
        return r.json()

    rutas = get("/ip/route", **{"dst-address": "0.0.0.0/0"})
    return {
        "nombre": get("/system/identity").get("name"),
        "uptime": get("/system/resource").get("uptime"),
        "interfaces": [{"interfaz": a.get("interface"), "ip": a.get("address")}
                       for a in get("/ip/address")],
        "ruta_defecto": rutas[0].get("gateway") if rutas else None,
    }


def comparar(ssh, rest):
    """Compara campo por campo; el uptime cambia segundos entre consultas."""
    claves_ip = lambda lst: sorted((i["interfaz"], i["ip"]) for i in lst)
    return {
        "nombre": ssh["nombre"] == rest["nombre"],
        "interfaces": claves_ip(ssh["interfaces"]) == claves_ip(rest["interfaces"]),
        "ruta_defecto": ssh["ruta_defecto"] == rest["ruta_defecto"],
    }


# ---------------------------------------------------------------------------
# R3: procesamiento por equipo con manejo de errores
# ---------------------------------------------------------------------------

def procesar_equipo(equipo, estudiante, args):
    resultado = {
        "nombre": equipo["nombre"],
        "host": equipo["host"],
        "device_type": equipo["device_type"],
        "rol": equipo["rol"],
        "fecha_hora": datetime.now().isoformat(timespec="seconds"),
        "estado": "ok",
    }
    clave = obtener_clave(equipo["nombre"])
    parametros = {
        "device_type": equipo["device_type"],
        "host": equipo["host"],
        "username": equipo["usuario"],
        "password": clave,
        "session_log": str(LOGS / f"{equipo['nombre']}.log"),
        **equipo.get("opciones_netmiko", {}),
    }
    try:
        with ConnectHandler(**parametros) as conn:
            if equipo["rol"] == "escritura" and equipo["device_type"] == "mikrotik_routeros":
                n = estudiante["numero"]
                if args.limpiar:
                    cambios = limpiar_loopback(conn, n)
                    resultado["configuracion"] = {"accion": "limpiar", "cambios": cambios or "sin cambios"}
                else:
                    cambios = asegurar_loopback(conn, n, estudiante["nombre"])
                    resultado["configuracion"] = {
                        "accion": "asegurar",
                        "cambios": cambios or "sin cambios",
                        "verificacion": verificar_loopback(conn, n),
                    }

            resultado["datos"] = recolectar_estado(conn, equipo["device_type"])

        if args.rest and equipo["device_type"] == "mikrotik_routeros":
            try:
                rest = estado_rest(equipo["host"], equipo["usuario"], clave)
                resultado["rest"] = {"datos": rest,
                                     "coincide": comparar(resultado["datos"], rest)}
            except Exception as e:
                resultado["rest"] = {"error": str(e)}

    except NetmikoTimeoutException as e:
        resultado["estado"] = f"timeout: {e}".splitlines()[0]
    except NetmikoAuthenticationException:
        resultado["estado"] = "error de autenticación"
    except Exception as e:
        resultado["estado"] = f"error: {e}"
    return resultado


# ---------------------------------------------------------------------------
# R5: reporte
# ---------------------------------------------------------------------------

def imprimir_tabla(resultados):
    fila = "{:<12} {:<15} {:<28} {:>6}  {:<15}"
    print()
    print(fila.format("EQUIPO", "IP", "ESTADO", "IFACES", "GATEWAY"))
    print("-" * 80)
    for r in resultados:
        datos = r.get("datos", {})
        print(fila.format(
            r["nombre"], r["host"], r["estado"][:28],
            len(datos.get("interfaces", [])) if datos else "-",
            datos.get("ruta_defecto") or "-",
        ))
    for r in resultados:
        if "configuracion" in r:
            print(f"\n[{r['nombre']}] {r['configuracion']['accion']}: {r['configuracion']['cambios']}")
            if "verificacion" in r["configuracion"]:
                print(f"  verificación: {r['configuracion']['verificacion']}")
        if "rest" in r:
            print(f"[{r['nombre']}] SSH vs REST: {r['rest'].get('coincide', r['rest'])}")


def main():
    parser = argparse.ArgumentParser(description="Lab 1 — Netmiko")
    parser.add_argument("--inventario", default=BASE / "inventario.yaml")
    parser.add_argument("--limpiar", action="store_true", help="elimina solo lo-ssh-N")
    parser.add_argument("--rest", action="store_true", help="extra: comparar con API REST")
    args = parser.parse_args()

    LOGS.mkdir(exist_ok=True)
    inventario = cargar_inventario(args.inventario)

    resultados = []
    for equipo in inventario["equipos"]:
        print(f"Conectando a {equipo['nombre']} ({equipo['host']})...")
        resultados.append(procesar_equipo(equipo, inventario["estudiante"], args))

    with open(REPORTE, "w", encoding="utf-8") as f:
        json.dump(resultados, f, indent=2, ensure_ascii=False)
    imprimir_tabla(resultados)
    print(f"\nReporte guardado en {REPORTE.name}")


if __name__ == "__main__":
    main()
