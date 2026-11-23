#!/bin/sh
set -e
PATH=/opt/bin:/opt/sbin:$PATH
REPO="https://raw.githubusercontent.com/icomium/keentimer/main"

[ -x /opt/bin/opkg ] || { echo "Entware не найден (/opt/bin/opkg). Сначала установите Entware."; exit 1; }

mkdir -p /opt/etc/keentimer /opt/share/keentimer/fonts
opkg update
opkg install libcurl curl ca-bundle
if ! opkg list-installed | grep -q "^python3 "; then
  opkg install python3
  touch /opt/etc/keentimer/.python_installed
fi

curl -fsSL "$REPO/keentimer.py" -o /opt/share/keentimer/keentimer.py
curl -fsSL "$REPO/index.html" -o /opt/share/keentimer/index.html
curl -fsSL "$REPO/S99keentimer" -o /opt/etc/init.d/S99keentimer
for f in Onest-cyrillic Onest-latin JetBrainsMono-cyrillic JetBrainsMono-latin; do
  curl -fsSL "$REPO/fonts/$f.woff2" -o "/opt/share/keentimer/fonts/$f.woff2" 2>/dev/null || rm -f "/opt/share/keentimer/fonts/$f.woff2"
done
chmod +x /opt/share/keentimer/keentimer.py /opt/etc/init.d/S99keentimer
/opt/etc/init.d/S99keentimer restart

echo "Готово: http://<IP-роутера>:3000 (вход — учётка роутера)"
