#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# polar_diag.sh — Diagnostic Polar H10 BLE / BlueZ
# Usage : bash polar_diag.sh [adresse_mac]
# Exemple : bash polar_diag.sh 24:AC:AC:16:DC:63
# ─────────────────────────────────────────────────────────────────────────────

MAC="${1:-24:AC:AC:16:DC:63}"
SEP="─────────────────────────────────────────────────────────"

echo ""
echo "$SEP"
echo "  Polar H10 — Diagnostic BLE/BlueZ"
echo "  MAC cible : $MAC"
echo "$SEP"
echo ""

# ── 1. Versions ───────────────────────────────────────────────────────────────
echo "[ 1/6 ] Versions Python / bleak / BlueZ"
echo "$SEP"
python3 --version 2>&1
python3 -c "import bleak; print('bleak  :', bleak.__version__)" 2>&1
python3 -c "import PyQt5; from PyQt5.QtCore import QT_VERSION_STR; print('PyQt5  :', QT_VERSION_STR)" 2>&1
bluetoothctl --version 2>&1
echo ""

# ── 2. Adaptateur Bluetooth ───────────────────────────────────────────────────
echo "[ 2/6 ] Adaptateur Bluetooth"
echo "$SEP"
hciconfig 2>/dev/null || echo "(hciconfig non disponible)"
echo ""
bluetoothctl show 2>/dev/null | head -20
echo ""

# ── 3. État du service BlueZ ──────────────────────────────────────────────────
echo "[ 3/6 ] Service BlueZ (systemd)"
echo "$SEP"
systemctl status bluetooth --no-pager -l 2>/dev/null | head -20
echo ""

# ── 4. Appareil connu dans BlueZ ─────────────────────────────────────────────
echo "[ 4/6 ] Appareil $MAC dans la base BlueZ"
echo "$SEP"
bluetoothctl info "$MAC" 2>/dev/null || echo "Appareil non trouvé dans la base bluetoothctl."
echo ""

# ── 5. Services D-Bus exposés (si connecté) ───────────────────────────────────
echo "[ 5/6 ] Objets D-Bus BlueZ (services GATT — nécessite connexion active)"
echo "$SEP"
MAC_DBUS=$(echo "$MAC" | tr ':' '_')
echo "Path D-Bus attendu : /org/bluez/hci0/dev_${MAC_DBUS}"
echo ""

# Lister tous les objets et filtrer ceux du H10
dbus-send --system \
    --dest=org.bluez \
    --print-reply \
    / \
    org.freedesktop.DBus.ObjectManager.GetManagedObjects \
    2>/dev/null \
  | grep -E "(dev_${MAC_DBUS}|fb005c8[012]|2902)" \
  | sed 's/^/  /' \
  || echo "  (aucun résultat — appareil non connecté ou dbus-send indisponible)"
echo ""

# ── 6. Introspection backend bleak ────────────────────────────────────────────
echo "[ 6/6 ] Introspection backend bleak (PMD_CONTROL)"
echo "$SEP"
python3 - <<'PYEOF'
import asyncio, sys

PMD_CONTROL = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA    = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
MAC         = sys.argv[1] if len(sys.argv) > 1 else "24:AC:AC:16:DC:63"

try:
    from bleak import BleakClient
except ImportError:
    print("bleak non installé.")
    sys.exit(1)

async def inspect():
    print(f"Connexion à {MAC}…")
    try:
        async with BleakClient(MAC) as client:
            print(f"Connecté : {client.is_connected}")

            # Lister services
            for svc in client.services:
                for ch in svc.characteristics:
                    if ch.uuid.lower() in (PMD_CONTROL.lower(), PMD_DATA.lower()):
                        print(f"\nCHAR {ch.uuid}  props={ch.properties}  handle={ch.handle}")
                        print(f"  type(ch)         = {type(ch)}")
                        print(f"  attrs            = {[a for a in dir(ch) if not a.startswith('__')]}")
                        for desc in ch.descriptors:
                            print(f"  DESC {desc.uuid}  handle={desc.handle}")
                            print(f"    type(desc)   = {type(desc)}")
                            dattrs = [a for a in dir(desc) if not a.startswith('__')]
                            print(f"    attrs        = {dattrs}")
                            # Tenter lecture CCCD
                            try:
                                val = await client.read_gatt_descriptor(desc.handle)
                                print(f"    valeur CCCD  = {val.hex()}")
                            except Exception as e:
                                print(f"    lecture err  = {e}")

            # Inspecter backend
            backend = client._backend
            print(f"\nBackend type : {type(backend)}")
            battrs = [a for a in dir(backend) if not a.startswith('__')]
            print(f"Backend attrs : {battrs}")

            for attr in battrs:
                val = getattr(backend, attr, None)
                if val and 'char' in attr.lower():
                    print(f"  {attr} = {type(val)} len={len(val) if hasattr(val,'__len__') else '?'}")

    except Exception as e:
        print(f"Erreur : {type(e).__name__}: {e}")

asyncio.run(inspect())
PYEOF

echo ""
echo "$SEP"
echo "  Diagnostic terminé."
echo "  Partagez ce log complet pour analyse."
echo "$SEP"
echo ""
