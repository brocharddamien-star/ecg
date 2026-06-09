#!/usr/bin/env bash
# Lance le Polar Recorder en se connectant automatiquement au capteur.
# Usage: ./polar.sh [MAC]
#   MAC par défaut : 24:AC:AC:16:DC:63 (Polar H10 16DC633A)

MAC="${1:-24:AC:AC:16:DC:63}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo ">>> Polar Recorder — connexion à $MAC"

# Disconnect if already connected, then remove bond so BlueZ does a fresh pairing.
# NOTE: if H10 is in zombie ECG state (STOP+START both return err=6),
#       physically reset the H10 (hold button ~5-7 s) before running this script.
CONNECTED=$(bluetoothctl info "$MAC" 2>/dev/null | grep "Connected: yes")
if [ -n "$CONNECTED" ]; then
    echo ">>> Déconnexion…"
    bluetoothctl disconnect "$MAC" 2>/dev/null || true
    sleep 1
fi
echo ">>> Suppression du bond précédent…"
bluetoothctl remove "$MAC" 2>/dev/null || true
sleep 1

echo ">>> Lancement de l'application…"
exec python3 "$DIR/polar_recorder.py" --device "$MAC"
