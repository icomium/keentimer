# KeenTimer

Веб-страница на порту 3000 для роутеров Keenetic (рассчитано на Hopper SE KN-3812, KeeneticOS 5.1.x): выдача клиентам интернета на ограниченное время.

- Вход по учётной записи роутера.
- Вкладка «Клиенты»: все клиенты слева, клиенты с включённым таймером справа.
- Вкладка «Время»: список со статусом (работает / заблокирован), добавление времени, блокировка.
- Блокировка выполняется через `ip hotspot host <mac> deny`, снятие — `permit`.

## Требования

Entware на USB-накопителе или во внутренней памяти, SSH в Entware.

## Установка (одна команда)

```sh
opkg update && opkg install libcurl curl ca-bundle && curl -fsSL https://raw.githubusercontent.com/icomium/keentimer/main/install.sh | sh
```

libcurl ставится вместе с curl, иначе после обновления curl может появиться ошибка `undefined symbol`.

## Удаление (одна команда)

```sh
curl -fsSL https://raw.githubusercontent.com/icomium/keentimer/main/uninstall.sh | sh
```

При удалении всем управляемым клиентам возвращается доступ (`permit`), файлы и состояние удаляются; `python3` удаляется, только если его поставил установщик.

## Переменные окружения

`KT_PORT` (3000), `KT_WEB_PORT` (80, веб-интерфейс роутера для проверки логина), `KT_RCI_PORT` (79).
