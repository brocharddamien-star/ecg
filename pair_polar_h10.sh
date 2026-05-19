#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# pair_polar_h10.sh — Appairage Polar H10 + lancement de l'app
# Usage : ./pair_polar_h10.sh [adresse_mac]
# ─────────────────────────────────────────────────────────────────────────────

POLAR_MAC="${1:-24:AC:AC:16:DC:63}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PATH="$SCRIPT_DIR/polar_app.py"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }
step()  { echo -e "\n${YELLOW}──${NC} $*"; }

# ── 1. Prérequis ──────────────────────────────────────────────────────────────
step "Vérification des prérequis…"
command -v bluetoothctl &>/dev/null || error "bluetoothctl non trouvé"
command -v systemctl    &>/dev/null || error "systemctl non trouvé"
if ! systemctl is-active --quiet bluetooth; then
    warn "bluetoothd arrêté — démarrage…"
    sudo systemctl start bluetooth && sleep 2
fi
info "bluetoothd actif"

# ── 2. Nettoyage ──────────────────────────────────────────────────────────────
step "Nettoyage du bond existant…"
bluetoothctl remove "$POLAR_MAC" 2>/dev/null \
    && info "Ancien bond supprimé" || warn "Pas de bond existant — OK"

# ── 3. Restart bluetoothd ─────────────────────────────────────────────────────
step "Redémarrage de bluetoothd…"
sudo systemctl restart bluetooth && sleep 3
info "bluetoothd redémarré"

# ── 4. Instruction utilisateur ────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}┌──────────────────────────────────────────────────────────┐${NC}"
echo -e "${YELLOW}│  ACTION REQUISE sur le Polar H10                         │${NC}"
echo -e "${YELLOW}│                                                          │${NC}"
echo -e "${YELLOW}│  1. Maintenez le bouton latéral ~7 s                    │${NC}"
echo -e "${YELLOW}│     jusqu'au clignotement RAPIDE du LED                 │${NC}"
echo -e "${YELLOW}│                                                          │${NC}"
echo -e "${YELLOW}│  2. Appuyez sur ENTRÉE ci-dessous                       │${NC}"
echo -e "${YELLOW}│     (le LED doit encore clignoter rapidement)           │${NC}"
echo -e "${YELLOW}└──────────────────────────────────────────────────────────┘${NC}"
echo ""
read -rp "Appuyez sur ENTRÉE quand le LED clignote rapidement…"

# ── 5. Scan + pair dans un seul flux bluetoothctl ─────────────────────────────
step "Scan + appairage en cours (45 s max)…"

PAIR_LOG=$(mktemp)

# Toutes les commandes dans une seule session bluetoothctl :
# scan → détection → pair → trust → quit
{
    echo "power on"
    echo "agent on"
    echo "default-agent"
    echo "scan on"
    # Attendre la détection du H10 dans le log (poll sur fichier)
    for i in $(seq 1 20); do
        sleep 1
        grep -q "$POLAR_MAC" "$PAIR_LOG" 2>/dev/null && break
    done
    echo "scan off"
    sleep 1
    echo "pair $POLAR_MAC"
    sleep 15          # laisser le temps au H10 de répondre
    echo "trust $POLAR_MAC"
    sleep 2
    echo "quit"
} | bluetoothctl 2>&1 | tee "$PAIR_LOG" | grep --line-buffered \
    -E "NEW|CHG.*Connected|pair|Pair|trust|Trust|Failed|Auth|successful|quit|Polar" &

BTCTL_PID=$!

# Attendre la fin (trust = succès, AuthenticationFailed = échec)
RESULT="timeout"
for i in $(seq 1 45); do
    sleep 1
    if grep -q "Trusted: yes\|trust succeeded" "$PAIR_LOG" 2>/dev/null; then
        RESULT="ok"; break
    fi
    if grep -q "AuthenticationFailed\|Failed to pair" "$PAIR_LOG" 2>/dev/null; then
        RESULT="failed"; break
    fi
done

kill "$BTCTL_PID" 2>/dev/null || true
wait "$BTCTL_PID" 2>/dev/null || true

echo ""  # fin des dots éventuels

# ── 6. Résultat ───────────────────────────────────────────────────────────────
case "$RESULT" in
    ok)
        info "Appairage et trust réussis !"
        ;;
    failed)
        rm -f "$PAIR_LOG"
        echo ""
        error "AuthenticationFailed — le H10 n'était plus en mode appairage.\nRelancez le script et appuyez sur ENTRÉE pendant que le LED clignote."
        ;;
    timeout)
        # Vérifier quand même si le trust est passé malgré le timeout
        INFO=$(bluetoothctl info "$POLAR_MAC" 2>/dev/null)
        if echo "$INFO" | grep -q "Trusted: yes"; then
            warn "Timeout détecté mais trust OK — on continue"
        else
            rm -f "$PAIR_LOG"
            error "Timeout — aucun résultat après 45 s. Réessayez."
        fi
        ;;
esac

rm -f "$PAIR_LOG"

# ── 7. Vérification finale ────────────────────────────────────────────────────
step "Vérification du bond…"
sleep 1
INFO=$(bluetoothctl info "$POLAR_MAC" 2>/dev/null)
PAIRED_S=$(echo  "$INFO" | grep "Paired:"  | awk '{print $2}')
TRUSTED_S=$(echo "$INFO" | grep "Trusted:" | awk '{print $2}')

echo "  Paired  : ${PAIRED_S:-inconnu}"
echo "  Trusted : ${TRUSTED_S:-inconnu}"

# Trust forcé si manquant
if [[ "$TRUSTED_S" != "yes" ]]; then
    warn "Trust manquant — correction…"
    bluetoothctl trust "$POLAR_MAC" &>/dev/null && info "Trusted = yes"
fi

# ── 8. Lancement de l'application ─────────────────────────────────────────────
step "Lancement de l'application ECG…"

if [[ ! -f "$APP_PATH" ]]; then
    warn "polar_app.py non trouvé dans $SCRIPT_DIR"
    read -rp "Chemin complet vers polar_app.py : " APP_PATH
    [[ -f "$APP_PATH" ]] || error "Fichier introuvable : $APP_PATH"
fi

info "Démarrage de $APP_PATH"
echo ""
exec python3 "$APP_PATH"
