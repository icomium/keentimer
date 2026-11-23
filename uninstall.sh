#!/bin/sh
PATH=/opt/bin:/opt/sbin:$PATH

/opt/etc/init.d/S99keentimer stop 2>/dev/null
[ -f /opt/share/keentimer/keentimer.py ] && /opt/bin/python3 /opt/share/keentimer/keentimer.py --release

if [ -f /opt/etc/keentimer/.python_installed ]; then
  opkg remove python3 2>/dev/null
fi

rm -rf /opt/etc/keentimer /opt/share/keentimer
rm -f /opt/etc/init.d/S99keentimer /opt/var/run/keentimer.pid /opt/var/log/keentimer.log
echo "KeenTimer удалён, ограничения со всех клиентов сняты."
