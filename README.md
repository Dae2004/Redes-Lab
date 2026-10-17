# Lab 1 — Inventario, estado y configuración segura con Netmiko

Script que lee `inventario.yaml`, se conecta por SSH (Netmiko) a cada equipo del
laboratorio GNS3, guarda su estado en `reporte_estado.json`.

## Instalación

```bash
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
```

## Credenciales

Nunca se guardan en el código ni en el YAML. Se leen de variables de entorno
`LAB_PASS_<NOMBRE>` (nombre del equipo en mayúsculas, `-` → `_`), por ejemplo
`LAB_PASS_MT_LAB` y `LAB_PASS_CISCO_LAB`.
Si una variable no existe, el script pide la clave con `getpass`.

## Ejecución

```bash
python lab1_inventario.py            # estado + asegurar lo-ssh-N
python lab1_inventario.py            # 2.ª vez: "sin cambios"
python lab1_inventario.py --limpiar  # elimina solo lo-ssh-N y su IP
python lab1_inventario.py --rest     # extra: compara SSH vs. API REST
```

## Red del laboratorio

El proyecto GNS3 `lab01` se conecta al host únicamente por el bridge `virbr0`
(red `default` de libvirt, 192.168.122.0/24, gateway 192.168.122.1 con NAT).
MikroTik: 192.168.122.202 · Cisco 3725: 192.168.122.203 (telnet, porque IOS 12.4
solo ofrece `diffie-hellman-group1-sha1`, eliminado en Paramiko 5).

## Declaración de uso de IA

Usé Claude Code (Anthropic) como asistente durante el laboratorio: para revisar
mi lista de pendientes, reconfigurar la topología de GNS3 (cambiar el acceso del
host de `tap0` a `virbr0` y readdressar los equipos) y redactar partes de este
README. El código del script y las decisiones de diseño las revisé y puedo
explicarlas en la defensa.
