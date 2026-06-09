#!/usr/bin/env bash
# Nettoie l'état BLE : déconnecte et supprime tous les bonds Polar H10

MAC="${1:-24:AC:AC:16:DC:63}"

echo ">>> Déconnexion de $MAC…"
bluetoothctl disconnect "$MAC" 2>/dev/null && sleep 1 || true

echo ">>> Suppression du bond…"
bluetoothctl remove "$MAC" 2>/dev/null || true

echo ">>> Redémarrage de l'adaptateur BLE…"
bluetoothctl power off 2>/dev/null && sleep 1
bluetoothctl power on  2>/dev/null && sleep 1

echo ">>> Terminé — adaptateur BLE prêt."
