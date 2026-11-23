#!/bin/sh
set -e
PATH=/opt/bin:/opt/sbin:$PATH
REPO="https://raw.githubusercontent.com/icomium/keentimer/main"
D=/opt/share/keentimer

[ -f "$D/keentimer.py" ] || { echo "KeenTimer не установлен. Используйте install.sh"; exit 1; }

for f in keentimer.py index.html; do
  curl -fsSL "$REPO/$f" -o "$D/$f.new" && mv "$D/$f.new" "$D/$f"
done
curl -fsSL "$REPO/S99keentimer" -o /opt/etc/init.d/S99keentimer.new && mv /opt/etc/init.d/S99keentimer.new /opt/etc/init.d/S99keentimer
chmod +x "$D/keentimer.py" /opt/etc/init.d/S99keentimer
/opt/etc/init.d/S99keentimer restart

echo "KeenTimer обновлён. Список клиентов и таймеры сохранены."
