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
Si una variable no existe, el script pide la clave con `getpass`.

## Ejecución

```bash
python lab1_inventario.py            # estado + asegurar lo-ssh-N
python lab1_inventario.py            # 2.ª vez: "sin cambios"
python lab1_inventario.py --limpiar  # elimina solo lo-ssh-N y su IP
python lab1_inventario.py --rest     # extra: compara SSH vs. API REST
```
