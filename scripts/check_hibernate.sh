#!/usr/bin/env bash
# ---------------------------------------------------------------
# Fedora hibernate readiness check
# Prints a PASS/FAIL report for every prerequisite.
# ---------------------------------------------------------------

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; BLD=$'\e[1m'; RST=$'\e[0m'
ok()   { echo "  ${GRN}✅ PASS${RST}  $1"; }
warn() { echo "  ${YLW}⚠️  WARN${RST}  $1"; }
bad()  { echo "  ${RED}❌ FAIL${RST}  $1"; }
hdr()  { echo; echo "${BLD}== $1 ==${RST}"; }

FAILS=0
WARNS=0
note_fail() { FAILS=$((FAILS+1)); bad "$1"; }
note_warn() { WARNS=$((WARNS+1)); warn "$1"; }

echo "${BLD}Fedora hibernate readiness report${RST}"
echo "Date: $(date)"
echo "Kernel: $(uname -r)"
echo "Fedora: $(cat /etc/fedora-release 2>/dev/null || echo '?')"

# -----------------------------------------------------------------
hdr "1. Kernel supports 'disk' sleep state"
if grep -qw disk /sys/power/state; then
    ok "/sys/power/state contains 'disk'  ->  $(cat /sys/power/state)"
else
    note_fail "/sys/power/state = '$(cat /sys/power/state)' (no 'disk' — hibernate unavailable on this kernel)"
fi

# -----------------------------------------------------------------
hdr "2. Resume device is configured"
RESUME=$(cat /sys/power/resume 2>/dev/null || echo "missing")
RESUME_OFF=$(cat /sys/power/resume_offset 2>/dev/null || echo "-")
if [[ "$RESUME" == "0:0" || -z "$RESUME" || "$RESUME" == "missing" ]]; then
    note_fail "/sys/power/resume = '$RESUME' (kernel doesn't know where to resume from)"
else
    ok "/sys/power/resume = '$RESUME'   resume_offset = '$RESUME_OFF'"
fi

# -----------------------------------------------------------------
hdr "3. Swap device(s)"
SWAP_LINES=$(swapon --noheadings --show=NAME,TYPE,SIZE 2>/dev/null)
if [[ -z "$SWAP_LINES" ]]; then
    note_fail "no swap active — hibernate needs a real disk-backed swap"
else
    echo "$SWAP_LINES" | while read -r line; do echo "      $line"; done

    # Reject zram / zswap-only setups
    if echo "$SWAP_LINES" | grep -qi zram; then
        note_warn "zram swap detected — zram lives in RAM, so it CANNOT back hibernate"
    fi
    if echo "$SWAP_LINES" | grep -qi "/dev/"; then
        ok "disk-backed swap device present"
    fi
fi

# -----------------------------------------------------------------
hdr "4. Swap size vs RAM size"
RAM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
SWAP_KB=$(awk '/SwapTotal/ {print $2}' /proc/meminfo)
RAM_G=$(awk -v k="$RAM_KB"  'BEGIN {printf "%.1f", k/1024/1024}')
SWAP_G=$(awk -v k="$SWAP_KB" 'BEGIN{printf "%.1f", k/1024/1024}')
echo "      RAM : ${RAM_G} GiB"
echo "      Swap: ${SWAP_G} GiB"
if (( SWAP_KB == 0 )); then
    note_fail "no active swap (see section 3)"
elif (( SWAP_KB < RAM_KB )); then
    note_warn "swap < RAM — hibernate may work with kernel compression but it's risky. Recommend swap >= RAM."
else
    ok "swap >= RAM — safe for hibernate"
fi

# -----------------------------------------------------------------
hdr "5. Secure Boot / kernel lockdown"
if command -v mokutil >/dev/null 2>&1; then
    SB=$(mokutil --sb-state 2>/dev/null || echo "unknown")
    echo "      mokutil says: $SB"
    if echo "$SB" | grep -qi "SecureBoot enabled"; then
        note_warn "Secure Boot is ENABLED — Fedora 39+ usually blocks hibernate in lockdown"
    else
        ok "Secure Boot is disabled or unavailable"
    fi
else
    warn "mokutil not installed (install: sudo dnf install mokutil)"
fi

LOCKDOWN=$(cat /sys/kernel/security/lockdown 2>/dev/null | awk -F'[][]' '{print $2}')
if [[ -n "$LOCKDOWN" ]]; then
    echo "      kernel lockdown mode: $LOCKDOWN"
    if [[ "$LOCKDOWN" == "confidentiality" || "$LOCKDOWN" == "integrity" ]]; then
        note_fail "kernel lockdown is '$LOCKDOWN' — hibernate will be refused"
    else
        ok "kernel lockdown mode allows hibernate"
    fi
else
    warn "/sys/kernel/security/lockdown not readable (securityfs not mounted?)"
fi

# -----------------------------------------------------------------
hdr "6. Kernel cmdline contains 'resume='"
CMDLINE=$(cat /proc/cmdline)
if echo "$CMDLINE" | grep -q "resume="; then
    RES=$(echo "$CMDLINE" | tr ' ' '\n' | grep '^resume=')
    ok "cmdline has $RES"
else
    note_warn "no 'resume=' on kernel cmdline (needed if /sys/power/resume is 0:0, but harmless if resume is already set by initramfs)"
fi

# -----------------------------------------------------------------
hdr "7. systemd's view of hibernate"
if command -v systemctl >/dev/null 2>&1; then
    OUT=$(systemctl --no-pager --lines=0 status hibernate.target 2>&1)
    if echo "$OUT" | grep -qi "not found\|LoadState=not-found"; then
        note_warn "hibernate.target not present (may be missing on some minimal installs)"
    else
        ok "hibernate.target is present"
    fi

    # What sleep.conf says
    if [[ -f /etc/systemd/sleep.conf ]]; then
        MODES=$(grep -E '^#?HibernateMode' /etc/systemd/sleep.conf | tail -1)
        echo "      sleep.conf: ${MODES:-<not set>}"
    fi
    # AllowHibernation
    ALLOW=$(grep -E '^#?AllowHibernation' /etc/systemd/sleep.conf 2>/dev/null | tail -1)
    echo "      AllowHibernation: ${ALLOW:-<default yes>}"
fi

# -----------------------------------------------------------------
hdr "8. Sudo / rtcwake availability"
if command -v rtcwake >/dev/null 2>&1; then
    ok "rtcwake is installed -> $(command -v rtcwake)"
else
    note_fail "rtcwake not found (install: sudo dnf install util-linux)"
fi

# -----------------------------------------------------------------
hdr "Summary"
if (( FAILS == 0 && WARNS == 0 )); then
    echo "  ${GRN}${BLD}All good — hibernate should work.${RST}"
elif (( FAILS == 0 )); then
    echo "  ${YLW}${BLD}${WARNS} warning(s) — hibernate may work but check them.${RST}"
else
    echo "  ${RED}${BLD}${FAILS} failure(s) — hibernate will NOT work until fixed.${RST}"
fi