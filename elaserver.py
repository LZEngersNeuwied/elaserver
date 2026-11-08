#!/usr/bin/env python3

"""
ELA-Server

Nimmt Webservice-Aufrufe eines Alarmmonitors entgegen (GET /alarm, GET /standby)
und schaltet dafür zwei Relais-Ausgänge (Licht und Blitzleuchte) über GPIO
an einem Raspberry Pi 5.

Zusätzlich kann bei einem Alarm ein GET-Request an ein externes System
abgesetzt werden (siehe Abschnitt 'alarm_request' in der Konfiguration).
"""

import logging
import logging.handlers
import signal
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit
import glob
import os
import gpiod

from gpiod.line import Direction, Value
import yaml

CONFIG_FILE = 'elaserver.yaml'
LOG_FILE = 'amweb-webhook.log'
LOG_FORMAT = '%(asctime)s %(levelname)s %(message)s'
GPIO_CHIP_NAME = '/dev/gpiochip15'
GPIO_CHIP_LABEL = "pinctrl-rp1"   # RP1 auf dem Raspberry Pi 5
MANDATORY_SERVER_OPTIONS = ['address', 'port']
# Nur erforderlich, wenn 'alarm_request' aktiviert ist.
MANDATORY_ALARM_REQUEST_OPTIONS = ['url', 'api_key', 'keyword']
DEFAULT_LOG_RETENTION_DAYS = 14

# Obergrenze für Response-Bodies des externen Systems, die ins Log geschrieben werden.
MAX_LOGGED_BODY_BYTES = 4096

# GPIO-Pins, die beim Start sicherheitshalber als Ausgang auf "inaktiv"
# gesetzt werden, auch wenn sie aktuell nicht verwendet werden.
UNUSED_GPIO_PINS = [24, 25]

# Wird in load_config_file() mit dem Inhalt von elaserver.yaml befüllt,
# z.B. {'server': {'address': ..., 'port': ...}, 'releais': {...}, 'logging': {...}}
config = {}

# Werden in run() initialisiert und in der Handler-Klasse verwendet.
lights_output = None
flash_output = None
gpio_chip = None


class RelaisOutput:
    """Verwaltet einen einzelnen GPIO-Ausgang (aktiv = LOW / 0, inaktiv = HIGH / 1).

    Die GPIO-Line wird einmalig beim Start angefordert und bis zum Beenden des
    Programms gehalten (statt bei jedem Request neu angefordert/freigegeben zu
    werden). Ein Aufruf von switch_on_for() schaltet den Ausgang ein und plant
    über einen Timer das automatische Ausschalten nach 'duration' Sekunden,
    ohne den aufrufenden Thread (den HTTP-Request-Handler) zu blockieren.
    Ein laufender Timer kann jederzeit über switch_off_now() abgebrochen
    werden (z.B. bei einem "standby"-Signal).
    """

    def __init__(self, chip, gpio_pin, name):
        self.name = name
        self.gpio_pin = gpio_pin
        self._lock = threading.Lock()
        self._timer = None
        self._line = None

        if gpio_pin and gpio_pin > 0:
            try:
                self._line = chip.request_lines(
                    consumer=name,
                    config={
                        gpio_pin: gpiod.LineSettings(
                            direction=Direction.OUTPUT,
                            output_value=Value.ACTIVE,  # inaktiv (HIGH)
                        )
                    },
                )
                logging.info("GPIO-Ausgang '%s' auf Pin %s initialisiert", name, gpio_pin)
            except Exception:
                logging.exception("Konnte GPIO-Ausgang '%s' auf Pin %s nicht initialisieren", name, gpio_pin)
                self._line = None
        else:
            logging.info("Kein GPIO-Pin für Ausgang '%s' konfiguriert - Ausgang deaktiviert", name)

    @property
    def enabled(self):
        return self._line is not None

    def switch_on_for(self, duration):
        """Schaltet den Ausgang sofort ein und nach 'duration' Sekunden automatisch
        wieder aus. Ein bereits laufender Timer für diesen Ausgang wird dabei
        abgebrochen und neu gestartet."""
        if not self.enabled:
            return

        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            try:
                self._line.set_value(self.gpio_pin, Value.INACTIVE)  # aktiv (LOW)
                logging.info("Schalte Ausgang '%s' ein (automatisch aus nach %s Sekunden)", self.name, duration)
            except Exception:
                logging.exception("Fehler beim Einschalten von Ausgang '%s'", self.name)
                return

            self._timer = threading.Timer(duration, self._switch_off)
            self._timer.daemon = True
            self._timer.start()

    def switch_off_now(self):
        """Bricht einen evtl. laufenden Ausschalt-Timer ab und schaltet den
        Ausgang sofort aus."""
        if not self.enabled:
            return

        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._set_off_locked()

    def _switch_off(self):
        """Wird ausschließlich vom Timer aufgerufen."""
        with self._lock:
            self._timer = None
            self._set_off_locked()

    def _set_off_locked(self):
        try:
            self._line.set_value(self.gpio_pin, Value.ACTIVE)  # inaktiv (HIGH)
            logging.info("Schalte Ausgang '%s' aus", self.name)
        except Exception:
            logging.exception("Fehler beim Ausschalten von Ausgang '%s'", self.name)

    def close(self):
        """Schaltet den Ausgang aus und gibt die GPIO-Line frei. Wird beim
        Beenden des Programms aufgerufen."""
        if not self.enabled:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            try:
                self._line.set_value(self.gpio_pin, Value.ACTIVE)  # inaktiv (HIGH)
            except Exception:
                logging.exception("Fehler beim Ausschalten von Ausgang '%s' beim Beenden", self.name)
            try:
                self._line.release()
            except Exception:
                logging.exception("Fehler beim Freigeben von Ausgang '%s'", self.name)


def get_int_option(section, option, default):
    """Liest eine Ganzzahl-Option aus der Konfiguration, mit Fallback auf
    'default', falls die Option fehlt oder ungültig ist."""
    value = config.get(section, {}).get(option)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logging.warning(
            "Ungültiger Wert für %s.%s, verwende Standardwert %s", section, option, default
        )
        return default


def get_str_option(section, option, default=''):
    """Liest eine Text-Option aus der Konfiguration, mit Fallback auf
    'default', falls die Option fehlt oder leer ist."""
    value = config.get(section, {}).get(option)
    if value is None:
        return default
    value = str(value).strip()
    return value or default


def get_gpio_pin(section, option):
    """Liest eine GPIO-Pin-Nummer aus der Konfiguration. 0 bzw. eine fehlende
    Option bedeutet: Ausgang ist deaktiviert."""
    return get_int_option(section, option, 0)


def send_keyword_request(url, keyword):
    """Setzt den GET-Request an das externe System ab. Läuft in einem eigenen
    Thread, damit weder der HTTP-Response an den Alarmmonitor noch das
    Schalten der Relais-Ausgänge auf das externe System warten müssen."""
    api_key = get_str_option('alarm_request', 'api_key')
    timeout = get_int_option('alarm_request', 'timeout_seconds', 5)

    separator = '&' if urlsplit(url).query else '?'
    request_url = f"{url}{separator}{urlencode({'keyword': keyword})}"

    request = urllib.request.Request(
        request_url, method='GET', headers={'x-api-key': api_key}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_LOGGED_BODY_BYTES).decode('utf-8', 'replace').strip()
            logging.info(
                "Keyword-Request an %s beantwortet mit HTTP %s: %s",
                request_url, response.status, body,
            )
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_LOGGED_BODY_BYTES).decode('utf-8', 'replace').strip()
        logging.error(
            "Keyword-Request an %s mit HTTP %s fehlgeschlagen: %s",
            request_url, exc.code, body,
        )
    except Exception:
        logging.exception("Keyword-Request an %s fehlgeschlagen", request_url)


def trigger_keyword_request():
    """Stößt den externen GET-Request bei einem Alarm an. Das Keyword ist fest
    in 'alarm_request.keyword' konfiguriert."""
    if not config.get('alarm_request', {}).get('enabled', False):
        return

    url = get_str_option('alarm_request', 'url')
    if not url:
        logging.warning(
            "alarm_request ist aktiviert, aber alarm_request.url fehlt - kein Keyword-Request gesendet"
        )
        return

    keyword = get_str_option('alarm_request', 'keyword')
    if not keyword:
        logging.warning(
            "alarm_request ist aktiviert, aber alarm_request.keyword fehlt - kein Keyword-Request gesendet"
        )
        return

    threading.Thread(
        target=send_keyword_request, args=(url, keyword), name='keyword-request', daemon=True
    ).start()


class S(BaseHTTPRequestHandler):

    def _set_response(self, code=500):
        self.send_response(code)
        self.send_header('Content-type', 'text/html')
        self.end_headers()

    def do_ela(self, action):
        switch_duration = get_int_option('releais', 'switch_duration', 2)
        flash_duration = get_int_option('releais', 'flash_duration', 10)

        if action == "alarm":
            logging.info(
                "Alarmmeldung eingegangen - Schalte ELA ein (Priority: %s)",
                self.headers.get('Priority'),
            )
            self.do_response("ok")
            lights_output.switch_on_for(switch_duration)
            flash_output.switch_on_for(flash_duration)
            trigger_keyword_request()
        elif action == "standby":
            logging.info("Alarmmeldung beendet - schalte Ausgänge sofort ab")
            self.do_response("ok")
            lights_output.switch_off_now()
            flash_output.switch_off_now()
        else:
            logging.info("Unbekannter Aufruf: %s", action)
            self.do_response("unknown request")

    def do_unknownMethod(self, method):
        logging.info("Nicht unterstützte Methode: %s", method)
        self.do_response("unknown method")

    def do_response(self, response):
        match response:
            case "ok":
                self._set_response(200)
            case _:
                self._set_response(500)

        self.wfile.write(response.encode('utf-8'))

    def do_GET(self):
        # Query-Parameter des Alarmmonitors werden nicht ausgewertet, dürfen
        # aber vorhanden sein - maßgeblich ist allein der Pfad.
        self.do_ela(urlsplit(self.path).path.strip('/'))

    def do_POST(self):
        self.do_unknownMethod("POST")

    def do_PUT(self):
        self.do_unknownMethod("PUT")

    def do_DELETE(self):
        self.do_unknownMethod("DELETE")

    # Unterdrücke Konsolenmeldungen für Requests
    def log_request(self, code='-', size='-'):
        return


def setup_logging():
    # log_retention_days steht in der YAML-Konfiguration, die zu diesem
    # Zeitpunkt bereits eingelesen sein muss (siehe Reihenfolge in run()).
    retention_days = get_int_option('logging', 'log_retention_days', DEFAULT_LOG_RETENTION_DAYS)
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=LOG_FILE, when='midnight', backupCount=retention_days, encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)


def load_config_file():
    """Liest die YAML-Konfigurationsdatei ein. Muss vor setup_logging()
    aufgerufen werden, da die Logging-Konfiguration (log_retention_days)
    selbst aus der Datei stammt. Da das Logging hier noch nicht
    initialisiert ist, wird ein Fehler direkt auf stderr ausgegeben."""
    global config

    file_path = Path(CONFIG_FILE)
    if not file_path.exists():
        print(f"Konfigurationsdatei {CONFIG_FILE} nicht gefunden. Service wird beendet", file=sys.stderr)
        sys.exit(1)

    try:
        with file_path.open('r', encoding='utf-8') as f:
            loaded = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        print(f"Konfigurationsdatei {CONFIG_FILE} enthält ungültiges YAML: {exc}", file=sys.stderr)
        sys.exit(1)

    config = loaded or {}


def validate_config():
    """Prüft die Pflichtangaben in der Konfiguration. Wird erst nach
    setup_logging() aufgerufen, damit Fehler ins Log geschrieben werden."""
    server_section = config.get('server')
    if not isinstance(server_section, dict):
        logging.error("Fehlender Abschnitt 'server' in %s", CONFIG_FILE)
        sys.exit(1)
    for option in MANDATORY_SERVER_OPTIONS:
        if option not in server_section:
            logging.error("Fehlender Konfigurationsparameter %s in 'server'", option)
            sys.exit(1)

    # Der externe Alarm-Request ist optional; fehlt etwas, wird nur gewarnt,
    # damit das Schalten der Ausgänge davon unberührt bleibt.
    if config.get('alarm_request', {}).get('enabled', False):
        for option in MANDATORY_ALARM_REQUEST_OPTIONS:
            if not get_str_option('alarm_request', option):
                logging.warning(
                    "alarm_request ist aktiviert, aber alarm_request.%s fehlt", option
                )

def find_gpio_chip(label: str = GPIO_CHIP_LABEL, fallback: str | None = None) -> str:
    """Sucht den GPIO-Chip anhand seines Labels, unabhängig von der Nummerierung."""
    for path in sorted(glob.glob("/dev/gpiochip*")):
        if not gpiod.is_gpiochip_device(path):
            continue
        try:
            with gpiod.Chip(path) as chip:
                if chip.get_info().label == label:
                    return path
        except OSError:
            continue
    if fallback and os.path.exists(fallback):
        return fallback
    raise FileNotFoundError(f"Kein GPIO-Chip mit Label '{label}' gefunden")

def run(server_class=ThreadingHTTPServer, handler_class=S):
    global lights_output, flash_output, gpio_chip

    load_config_file()
    setup_logging()
    logging.info("Initialisiere Webserver")
    validate_config()

    chip_path = find_gpio_chip(fallback=GPIO_CHIP_NAME)
    logging.info("GPIO-Chip: %s", chip_path)
    gpio_chip = gpiod.Chip(chip_path)

    lights_pin = get_gpio_pin('releais', 'gpio_port_lights_alarm')
    flash_pin = get_gpio_pin('releais', 'gpio_port_flash_alarm')

    # GPIO-Ausgänge werden einmalig hier angefordert und bis zum Beenden des
    # Programms gehalten (statt bei jedem Request neu).
    lights_output = RelaisOutput(gpio_chip, lights_pin, "ALARM_LICHT")
    flash_output = RelaisOutput(gpio_chip, flash_pin, "ALARM_BLITZ")

    # Nicht verwendete Ausgänge ebenfalls sauber auf inaktiv setzen.
    unused_outputs = [
        RelaisOutput(gpio_chip, pin, f"UNUSED_{pin}") for pin in UNUSED_GPIO_PINS
    ]

    server_address = (config['server']['address'], int(config['server']['port']))
    httpd = server_class(server_address, handler_class)
    logging.info(
        'Starte Webserver für http://%s:%s', config['server']['address'], config['server']['port']
    )

    def handle_sigterm(signum, frame):
        logging.info("SIGTERM empfangen, beende Webserver")
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        logging.info("Stoppe Webserver")
        httpd.server_close()
        lights_output.close()
        flash_output.close()
        for unused_output in unused_outputs:
            unused_output.close()
        gpio_chip.close()
        logging.info("Webserver gestoppt")


if __name__ == '__main__':
    run()