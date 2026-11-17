#!/bin/bash
# Run the backfill in the background as a macOS LaunchAgent.
#
#   backfill/service.sh install BACKUP [backfill options...]   create the service and start it
#   backfill/service.sh show BACKUP [backfill options...]      print what install would create
#   backfill/service.sh stop | start | restart                 stop waits for the current Episode
#   backfill/service.sh status                                 service state + backfill progress
#   backfill/service.sh logs                                   follow the backfill log
#   backfill/service.sh uninstall                              stop and remove the service
#
# launchd restarts the backfill if it crashes (after 5 minutes) and starts it again at login,
# until `stop`. A run that finishes normally stays finished. The Mac is kept from idle sleep
# while it runs. Re-run `install` to change the backup file or the options. The service reads
# its settings from backfill/config.env only, never from this shell's environment.
set -euo pipefail

LABEL=com.podcast-addict.backfill
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
PY="$HERE/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
TARGET="$DOMAIN/$LABEL"

# PA_ROOT as backfill.py sees it under launchd: from config.env (quotes stripped, ~ expanded).
pa_root() {
    local root=""
    if [ -f "$HERE/config.env" ]; then
        root="$(sed -n 's/^[[:space:]]*PA_ROOT[[:space:]]*=[[:space:]]*//p' "$HERE/config.env" \
                | tail -1 | sed -e 's/[[:space:]]*$//' -e "s/^[\"']*//" -e "s/[\"']*\$//")"
    fi
    root="${root:-$HOME/podcast-backfill}"
    case "$root" in "~" | "~/"*) root="$HOME${root:1}" ;; esac
    echo "$root"
}
LOGS="$(pa_root)/logs"
# Run backfill.py with the environment the LaunchAgent gives it.
service_env() { env -i HOME="$HOME" PATH="$PATH" PYTHONUNBUFFERED=1 "$@"; }

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
loaded() { launchctl print "$TARGET" >/dev/null 2>&1; }

plist() {
    if [ $# -lt 1 ]; then
        echo "usage: $0 install|show BACKUP [backfill options...]" >&2
        exit 2
    fi
    if [ ! -f "$1" ]; then
        echo "backup not found: $1" >&2
        exit 2
    fi
    local backup
    backup="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
    shift
    if [ ! -x "$PY" ]; then
        echo "missing $PY: create the venv first (backfill/README.md, Setup)" >&2
        exit 2
    fi
    local args="" a
    for a in "$PY" "$HERE/backfill.py" "$backup" "$@"; do
        args+="        <string>$(xml "$a")</string>"$'\n'
    done
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
$args    </array>
    <key>WorkingDirectory</key>
    <string>$(xml "$REPO")</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(xml "$PATH")</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>300</integer>
    <key>ExitTimeOut</key>
    <integer>600</integer>
    <key>StandardOutPath</key>
    <string>$(xml "$LOGS/service.log")</string>
    <key>StandardErrorPath</key>
    <string>$(xml "$LOGS/service.log")</string>
</dict>
</plist>
EOF
}

install() {
    local text
    text="$(plist "$@")"
    stop
    mkdir -p "$LOGS" "$(dirname "$PLIST")"
    printf '%s\n' "$text" > "$PLIST"
    plutil -lint "$PLIST" >/dev/null
    echo "installed $PLIST"
    start
}

start() {
    if [ ! -f "$PLIST" ]; then
        echo "not installed: run $0 install BACKUP" >&2
        exit 2
    fi
    launchctl enable "$TARGET"
    if loaded; then
        # Still loaded after a run that finished: start it again (no-op while it runs).
        launchctl kickstart "$TARGET" 2>/dev/null || true
    else
        launchctl bootstrap "$DOMAIN" "$PLIST"
    fi
    echo "started; follow it with: $0 logs"
}

stop() {
    launchctl disable "$TARGET" 2>/dev/null || true
    if loaded; then
        echo "stopping (the Episode being cleaned is finished first; up to 10 minutes)..."
        launchctl bootout "$TARGET" || true
        local waited=0
        while loaded; do
            if [ "$waited" -ge 660 ]; then
                echo "still loaded after 11 minutes; see: launchctl print $TARGET" >&2
                exit 1
            fi
            sleep 1
            waited=$((waited + 1))
        done
        echo "stopped"
    fi
}

status() {
    if loaded; then
        launchctl print "$TARGET" | grep -E '^\s*(state|pid|last exit code|runs) =' | sed 's/^\s*/service: /'
    elif [ -f "$PLIST" ]; then
        echo "service: stopped"
    else
        echo "service: not installed"
    fi
    service_env "$PY" "$HERE/backfill.py" --status
}

case "${1:-}" in
    install) shift; install "$@" ;;
    show) shift; plist "$@" ;;
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) status ;;
    logs) mkdir -p "$LOGS"; touch "$LOGS/backfill.log"; tail -n 40 -f "$LOGS/backfill.log" ;;
    uninstall) stop; rm -f "$PLIST"; echo "removed $PLIST" ;;
    *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
