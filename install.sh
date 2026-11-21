#!/bin/bash
# PiSampler installer - run on a fresh Raspberry Pi OS Lite (64-bit) on a Pi 5:
#
#     sudo ./install.sh
#
# Safe to run again (for example after updating the app). Options, as variables:
#     sudo DAC_OVERLAY=hifiberry-dacplus-std ./install.sh     # different DAC overlay
#     sudo DSI_OVERLAY=vc4-kms-dsi-7inch,dsi0 ./install.sh    # name the display overlay yourself
set -euo pipefail

DAC_OVERLAY="${DAC_OVERLAY:-allo-boss-dac-pcm512x-audio}"
DSI_OVERLAY="${DSI_OVERLAY:-}"      # empty = let the Pi auto-detect the panel (verified working)
SFIZZ_TAG="${SFIZZ_TAG:-1.2.3}"

SRC="$(cd "$(dirname "$0")" && pwd)"
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo:  sudo ./install.sh"
[ "$(dpkg --print-architecture)" = arm64 ] || die "this needs the 64-bit Raspberry Pi OS"
USER_NAME="${SUDO_USER:-}"
[ -n "$USER_NAME" ] && [ "$USER_NAME" != root ] || die "run as your normal user with sudo, not as root"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
CONFIG=/boot/firmware/config.txt
CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CONFIG" ] || die "$CONFIG not found - is this Raspberry Pi OS (Bookworm or newer)?"

# ------------------------------------------------------------------ packages
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
echo "jackd2 jackd/tweak_rt_limits boolean true" | debconf-set-selections
apt-get update
apt-get install -y --no-install-recommends \
    jackd2 a2jmidid fluidsynth alsa-utils \
    sudo cage python3 python3-gi gir1.2-gtk-3.0 python3-jack-client fonts-dejavu-core \
    samba avahi-daemon coreutils util-linux
apt-get install -y --no-install-recommends jack-example-tools 2>/dev/null || true   # jack_lsp, for the doctor
apt-get install -y --no-install-recommends fonts-inter 2>/dev/null || true

# ------------------------------------------------------------------ sfizz
if command -v sfizz_jack >/dev/null; then
    say "sfizz_jack already installed"
elif apt-cache show sfizz >/dev/null 2>&1 && apt-get install -y sfizz && command -v sfizz_jack >/dev/null; then
    say "sfizz installed from the package archive"
else
    say "Building sfizz $SFIZZ_TAG from source (10-15 minutes)"
    apt-get install -y --no-install-recommends build-essential git cmake pkg-config libjack-jackd2-dev ca-certificates
    BUILD=/usr/local/src/sfizz
    rm -rf "$BUILD"
    git clone --depth 1 --recursive --shallow-submodules --branch "$SFIZZ_TAG" \
        https://github.com/sfztools/sfizz.git "$BUILD"
    JOBS=$(nproc); [ "$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)" -lt 3500 ] && JOBS=2
    build_sfizz() {
        rm -rf "$BUILD/build"
        cmake -S "$BUILD" -B "$BUILD/build" -DCMAKE_BUILD_TYPE=Release \
            -DSFIZZ_JACK=ON -DSFIZZ_RENDER=OFF -DSFIZZ_SHARED=OFF \
            -DSFIZZ_TESTS=OFF -DSFIZZ_BENCHMARKS=OFF -DSFIZZ_DEMOS=OFF -DSFIZZ_DEVTOOLS=OFF "$@" \
        && cmake --build "$BUILD/build" -j"$JOBS" --target sfizz_jack
    }
    if ! build_sfizz; then
        say "Retrying the sfizz build with the system's abseil library"
        apt-get install -y --no-install-recommends libabsl-dev
        build_sfizz -DSFIZZ_USE_SYSTEM_ABSEIL=ON || die "sfizz failed to build - see the messages above"
    fi
    BIN=$(find "$BUILD/build" -type f -name sfizz_jack -perm -u+x | head -n1)
    [ -n "$BIN" ] || die "sfizz built but sfizz_jack was not found"
    install -m 755 "$BIN" /usr/local/bin/sfizz_jack
fi

# ------------------------------------------------------------------ boot config
say "Configuring DAC and display in $CONFIG"
[ -f "$CONFIG.pisampler-backup" ] || cp "$CONFIG" "$CONFIG.pisampler-backup"
sed -i '/^# >>> pisampler >>>$/,/^# <<< pisampler <<<$/d' "$CONFIG"
sed -i -E 's/^dtparam=audio=on/#dtparam=audio=on/' "$CONFIG"                 # onboard audio off
if [ -n "$DSI_OVERLAY" ]; then
    sed -i -E 's/^display_auto_detect=1/display_auto_detect=0/' "$CONFIG"
    DSI_LINE="dtoverlay=$DSI_OVERLAY"
else
    sed -i -E 's/^display_auto_detect=0/display_auto_detect=1/' "$CONFIG"
    grep -q '^display_auto_detect=1' "$CONFIG" || AUTO_LINE="display_auto_detect=1"
    DSI_LINE="${AUTO_LINE:-# (display is auto-detected: display_auto_detect=1 above)}"
fi
sed -i -E 's/^dtoverlay=vc4-kms-v3d$/dtoverlay=vc4-kms-v3d,noaudio/' "$CONFIG"  # no HDMI sound cards
grep -q '^dtoverlay=vc4-kms-v3d' "$CONFIG" || KMS_LINE="dtoverlay=vc4-kms-v3d,noaudio"
cat >> "$CONFIG" <<CFG
# >>> pisampler >>>
[all]
${KMS_LINE:-}
dtparam=audio=off
# InnoMaker HiFi DAC HAT (PCM5122)
dtoverlay=$DAC_OVERLAY
# Waveshare 4.3inch DSI QLED 800x480 (panel and touch are auto-detected on the Pi 5)
$DSI_LINE
disable_splash=1
# <<< pisampler <<<
CFG

say "Quiet boot (no text, no blinking cursor, no screen blanking)"
[ -f "$CMDLINE.pisampler-backup" ] || cp "$CMDLINE" "$CMDLINE.pisampler-backup"
LINE="$(head -n1 "$CMDLINE" | sed -E 's/console=tty1/console=tty3/')"
for opt in quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0 irqaffinity=0-1; do
    case " $LINE " in *" $opt "*) ;; *) LINE="$LINE $opt" ;; esac
done
echo "$LINE" > "$CMDLINE"

# ------------------------------------------------------------------ user, realtime
say "Giving $USER_NAME audio/video/input rights"
usermod -aG audio,video,input,render "$USER_NAME"
cat > /etc/security/limits.d/95-pisampler.conf <<LIM
@audio - rtprio 95
@audio - memlock unlimited
LIM
[ -f /etc/default/pisampler ] || cat > /etc/default/pisampler <<'DEF'
# PiSampler settings. Reboot after changing.
# Audio buffer: smaller = less delay, larger = safer against crackles.
# 128 x 2 at 48 kHz is about 5 ms. If you hear crackles try PISAMPLER_NPERIODS=3 or PISAMPLER_PERIOD=256.
PISAMPLER_PERIOD=128
PISAMPLER_NPERIODS=2
# Sound card name; leave empty to auto-detect the DAC.
PISAMPLER_CARD=
# Maximum simultaneous notes for SFZ instruments (lower it if a huge piano crackles).
PISAMPLER_SFZ_VOICES=128
# Screen: dim after this many seconds without a touch (0 = never), and how dim (percent).
PISAMPLER_DIM_AFTER=20
PISAMPLER_DIM_PERCENT=5
DEF
# A desktop sound server would grab the DAC; make sure none starts for this user.
systemctl --global mask pipewire.service pipewire.socket pipewire-pulse.service \
    pipewire-pulse.socket wireplumber.service pulseaudio.service pulseaudio.socket \
    fluidsynth.service >/dev/null 2>&1 || true     # (Debian's fluidsynth package ships its own daemon)

# ------------------------------------------------------------------ app
say "Installing the app"
install -d /opt/pisampler /usr/local/lib/pisampler
install -m 644 "$SRC/app/core.py" "$SRC/app/ui.py" /opt/pisampler/
install -m 755 "$SRC/scripts/jack-start" "$SRC/scripts/usb-mount" "$SRC/scripts/engine-run" \
    "$SRC/scripts/tune" /usr/local/lib/pisampler/
install -m 755 "$SRC/scripts/pisampler-doctor" /usr/local/bin/pisampler-doctor
install -d -o "$USER_NAME" -g "$USER_NAME" "$USER_HOME/instruments"
install -d /media/usb

# An invisible cursor theme for the kiosk compositor (the app shows a real
# cursor by itself while a mouse is being moved).
CUR=/usr/local/share/pisampler/cursors/default/cursors
install -d "$CUR"
python3 - "$CUR/default" <<'PY'
import struct, sys
image = struct.pack("<9I", 36, 0xfffd0002, 24, 1, 1, 1, 0, 0, 0) + struct.pack("<I", 0)   # 1x1 transparent
open(sys.argv[1], "wb").write(b"Xcur" + struct.pack("<3I", 16, 0x10000, 1)
                              + struct.pack("<3I", 0xfffd0002, 24, 28) + image)
PY
for n in left_ptr arrow top_left_arrow; do ln -sf default "$CUR/$n"; done
apt-get install -y --no-install-recommends adwaita-icon-theme >/dev/null 2>&1 || true

cat > /etc/pam.d/pisampler <<'PAM'
auth     required  pam_unix.so nullok
account  required  pam_unix.so
session  required  pam_unix.so
session  required  pam_systemd.so
PAM

for unit in "$SRC"/systemd/*.service "$SRC"/systemd/*.slice; do
    sed "s/@USER@/$USER_NAME/g" "$unit" > "/etc/systemd/system/$(basename "$unit")"
done
rm -f /etc/systemd/system/pisampler-performance.service

# Runtime files (engine command + log) live in RAM, not on the SD card.
echo "d /run/pisampler 0755 $USER_NAME $USER_NAME -" > /etc/tmpfiles.d/pisampler.conf
systemd-tmpfiles --create /etc/tmpfiles.d/pisampler.conf

# The app may start and stop the engine service, nothing else.
cat > /etc/sudoers.d/pisampler <<SUDO
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl restart pisampler-engine.service, /usr/bin/systemctl stop pisampler-engine.service
SUDO
chmod 440 /etc/sudoers.d/pisampler
visudo -cf /etc/sudoers.d/pisampler >/dev/null || die "sudoers rule is invalid"

# ------------------------------------------------------------------ CPU partition
say "Reserving CPUs 2-3 for the audio chain (OS and interrupts stay on 0-1)"
for u in system.slice user.slice; do
    install -d "/etc/systemd/system/$u.d"
    printf '[Slice]\nAllowedCPUs=0-1\n' > "/etc/systemd/system/$u.d/50-pisampler-cpus.conf"
done
install -d /etc/systemd/system/init.scope.d
printf '[Scope]\nAllowedCPUs=0-1\n' > /etc/systemd/system/init.scope.d/50-pisampler-cpus.conf
systemctl disable --now irqbalance >/dev/null 2>&1 || true     # it would undo our interrupt placement

# ------------------------------------------------------------------ USB sticks
say "USB stick auto-mount"
cat > /etc/udev/rules.d/99-pisampler-usb.rules <<'UDEV'
ACTION=="add", SUBSYSTEM=="block", ENV{ID_BUS}=="usb", ENV{ID_FS_USAGE}=="filesystem", RUN+="/usr/local/lib/pisampler/usb-mount add %k"
ACTION=="remove", SUBSYSTEM=="block", ENV{ID_BUS}=="usb", RUN+="/usr/local/lib/pisampler/usb-mount remove %k"
UDEV
udevadm control --reload-rules

# ------------------------------------------------------------------ network share
say "Sharing $USER_HOME/instruments on the network"
cat > /etc/samba/pisampler.conf <<SMB
[instruments]
   comment = PiSampler instruments - drop SFZ folders and SF2 files here
   path = $USER_HOME/instruments
   browseable = yes
   read only = no
   guest ok = yes
   force user = $USER_NAME
   create mask = 0644
   directory mask = 0755
   # Mac support: keep Finder metadata in extended attributes, not in extra files
   # (without this, Finder copies fail with error -8062)
   vfs objects = catia fruit streams_xattr
   fruit:metadata = stream
   fruit:model = MacSamba
   fruit:posix_rename = yes
   fruit:veto_appledouble = no
   fruit:nfs_aces = no
   fruit:wipe_intentionally_left_blank_rfork = yes
   fruit:delete_empty_adfiles = yes
SMB
# Samba needs its log folder even on systems that keep /var/log in RAM.
echo "d /var/log/samba 0755 root root -" > /etc/tmpfiles.d/pisampler-samba.conf
systemd-tmpfiles --create /etc/tmpfiles.d/pisampler-samba.conf
grep -q 'pisampler.conf' /etc/samba/smb.conf || \
    printf '\n# PiSampler\ninclude = /etc/samba/pisampler.conf\n' >> /etc/samba/smb.conf
grep -qiE '^\s*map to guest' /etc/samba/smb.conf || \
    sed -i -E '0,/^\[global\]/s//[global]\n   map to guest = Bad User/' /etc/samba/smb.conf
testparm -s >/dev/null 2>&1 || die "Samba configuration is invalid (run: testparm)"
systemctl enable smbd nmbd avahi-daemon >/dev/null 2>&1 || true
systemctl restart smbd 2>/dev/null || true

# ------------------------------------------------------------------ Wi-Fi
# The Pi's Wi-Fi power saving drops packets and makes the file share crawl.
if [ -d /etc/NetworkManager/conf.d ]; then
    say "Turning off Wi-Fi power saving"
    printf '[connection]\nwifi.powersave = 2\n' > /etc/NetworkManager/conf.d/pisampler-wifi-powersave.conf
fi

# ------------------------------------------------------------------ services
say "Enabling services"
systemctl daemon-reload
systemctl disable getty@tty1.service >/dev/null 2>&1 || true
systemctl disable pisampler-performance >/dev/null 2>&1 || true
systemctl enable pisampler-jack pisampler-midi pisampler-ui
systemctl set-default graphical.target >/dev/null

HOST="$(hostname)"
cat <<DONE

$(printf '\033[1;32m')PiSampler is installed.$(printf '\033[0m')

  DAC overlay : $DAC_OVERLAY
  Display     : ${DSI_OVERLAY:-auto-detected}
  Instruments : $USER_HOME/instruments   (network share: smb://$HOST.local/instruments)

Now restart the Pi:   sudo reboot
After the restart, check everything with:   pisampler-doctor
DONE
