# Разработка Impuls

Это руководство для запуска, проверки и сборки проекта. Обычная установка
и использование плеера описаны в [README](../README.md).

## Подготовка Fedora

Impuls использует Python >= 3.11, GTK 4, GStreamer и библиотеку
`yandex-music==3.2.2`. Проверяемая платформа — Fedora 45.

```bash
sudo dnf install git python3 python3-pip python3-gobject python3-cairo \
  python3-requests python3-pysocks python3-typing-extensions \
  gtk4 gobject-introspection adwaita-icon-theme gstreamer1 \
  gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free \
  glib-networking libsecret
git clone https://github.com/AnoY606-git/Impuls.git
cd Impuls
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install 'yandex-music==3.2.2'
.venv/bin/python -m impuls_player
```

`--system-site-packages` нужен для системных GTK/GStreamer bindings. Команда
`impuls`, установленная из RPM, использует `/usr/share/impuls`, а не текущую
копию репозитория. Для проверки правок запускайте модуль из корня проекта.

## Где искать код

- `app.py` связывает компоненты и сохраняет настройки.
- `ui.py` и `style.css` отвечают за GTK-интерфейс.
- `controller.py` координирует действия, фоновые задачи и состояние плеера.
- `service.py` работает с API; `player.py` — с GStreamer.
- `queue.py`, `wave.py` и `prefetch.py` управляют очередью, рекомендациями
  и подготовкой следующего трека.
- `settings.py` хранит настройки, `secrets.py` — токен через Secret Service.
- `visualizations.py`, `moments.py`, `mpris.py` и `tray.py` реализуют
  визуализации, отметки времени и интеграцию с рабочим столом.

Все эти файлы находятся в `impuls_player/`. Сетевые операции не должны
блокировать GTK: результат фоновой задачи возвращается в главный поток
через GLib. Сохраняйте проверки отмены и поколений запросов, чтобы поздний
ответ не менял состояние после переключения трека, списка или аккаунта.

Настройки записываются в `$XDG_CONFIG_HOME/impuls/settings.json`, по умолчанию
`~/.config/impuls/settings.json`. Токен туда не попадает. Не добавляйте его
или подписанные ссылки аудио в логи, тестовые данные и снимки экрана.

## Тесты

Проверки используют стандартный `unittest` и не требуют реального аккаунта:

```bash
.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

Для GTK и D-Bus нужен дисплей и сессионная шина. В среде без рабочего стола:

```bash
sudo dnf install dbus-daemon xorg-x11-server-Xvfb xorg-x11-xauth
GTK_A11Y=none GDK_BACKEND=x11 IMPULS_TEST_DBUS=1 \
  dbus-run-session -- xvfb-run -a -s '-screen 0 1280x1024x24' \
  .venv/bin/python -m unittest discover -s tests -v
```

Без этих компонентов часть проверок пропускается. Контрактные тесты также
требуют установленной `yandex-music` 3.2.2. Два теста максимизации окна требуют
оконного менеджера X11 и пропускаются в чистом Xvfb. Размер виртуального экрана
важен: 640×480 недостаточно для проверок геометрии.

После изменений CSS проверьте Breeze и светлую/тёмную Adwaita: обычное и узкое
окно, мини-плеер, меню, фокус с клавиатуры и состояния кнопок. Тема может
задавать фон, прозрачность и отступы внутренним узлам составных GTK-виджетов.

## Сборка RPM

```bash
sudo dnf install rpm-build redhat-rpm-config curl tar gzip
./scripts/build-rpm.sh
```

В `dist/` появятся RPM, исходный SRPM и `SHA256SUMS`. Скрипт скачивает wheel
`yandex-music` 3.2.2 и проверяет его SHA-256. Если файл уже скачан:

```bash
IMPULS_WHEEL=/absolute/path/yandex_music-3.2.2-py3-none-any.whl ./scripts/build-rpm.sh
```

Собрать только архив исходников, без загрузки wheel и запуска RPM-сборки:

```bash
./scripts/build-rpm.sh --source-only /tmp/impuls-source.tar.gz
```

Архив создаётся по явному списку `source_files` в скрипте. Добавляя модуль,
тест или публичную документацию, обновляйте этот список. Локальные настройки,
секреты, `AGENTS.md`, `docs/reviews/` и `docs/superpowers/` в пакет не входят.

При изменении версии обновляйте её согласованно в `__init__.py`, README,
RPM spec, сборочном скрипте, AppStream и именах артефактов CI.

## Проверки упаковки

```bash
desktop-file-validate data/*.desktop
appstreamcli validate --no-net data/*.metainfo.xml
```

CI собирает пакет в Fedora 45, запускает тесты с виртуальным дисплеем
и проверяет чистую установку без необязательных зависимостей. Отдельно
проверяются импорт включённой библиотеки и наличие аудиоплагинов.

## Внешние компоненты

Доступ к сервису обеспечивает [yandex-music](https://github.com/MarshalX/yandex-music-api).
Для трея нужен хост StatusNotifierItem; управление медиаклавишами работает
через MPRIS. Доступность треков и форматов определяется сервисом и аккаунтом.
Успешные тесты с заглушками не подтверждают работу авторизации на реальном
аккаунте или совместимость с новой версией API.
