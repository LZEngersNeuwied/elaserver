# Changelog

## Version 1.2 (GPIO-Chip)

### Behoben

- Nach einem System-Update (Raspberry-Pi-Kernel 6.12.96+rpt-rpi) startete der
  Dienst nicht mehr und brach mit `FileNotFoundError` beim Öffnen von
  `/dev/gpiochip4` ab. Ab diesem Kernel wird der RP1-GPIO-Controller des
  Pi 5 dynamisch nummeriert, und der bisherige
  Kompatibilitäts-Symlink `gpiochip4 → gpiochip0` wird nicht mehr angelegt.

### Geändert

- Der GPIO-Chip wird nicht mehr über einen festen Gerätepfad geöffnet,
  sondern beim Start anhand seines Labels (`pinctrl-rp1`) unter
  `/dev/gpiochip*` gesucht. Der gefundene Pfad wird beim Start
  protokolliert. Das funktioniert mit allen bisherigen Kernel-Ständen des
  Pi 5:
  - vor 6.6.45: RP1 als `gpiochip4`
  - ab 6.6.45: RP1 als `gpiochip0`, `gpiochip4` nur als Symlink
  - ab 6.12.96: dynamische Nummer (z.B. `gpiochip15`)
  Eine feste Nummer wäre auf älteren Kerneln sogar gefährlich gewesen, weil
  `gpiochip0` dort ein Broadcom-Controller ist und die falschen Pins
  geschaltet worden wären.
- Neuer optionaler Konfigurationsschlüssel `chip_label` (Standard:
  `pinctrl-rp1`). Der bisherige feste Chip-Pfad wird nur noch als Fallback
  verwendet, falls kein Chip mit passendem Label gefunden wird (z.B. auf
  älteren Pi-Modellen ohne RP1).
- Die Pin-Nummern (Offsets) der Relais-Ausgänge bleiben unverändert.

## Version 1.1 (Keyword)

### Hinzugefügt

- Zusätzliche Aktion bei Alarmeingang: ein GET-Request an ein externes System
  mit dem Header `x-api-key` und dem Query-Parameter `keyword`. Beide Werte
  stammen fest aus der Konfiguration; aus dem eingehenden Alarm-Request wird
  nichts übernommen. Der Request läuft in einem eigenen Thread und verzögert
  weder den Response an den Alarmmonitor noch das Schalten der
  Relais-Ausgänge; Fehler des externen Systems werden nur protokolliert.
- Neuer Konfigurationsabschnitt `alarm_request` (`enabled`, `url`, `api_key`,
  `keyword`, `timeout_seconds`). Ist er nicht aktiviert oder unvollständig,
  arbeitet der Dienst unverändert weiter und schreibt lediglich eine Warnung.

### Geändert

- Der Request-Pfad wird jetzt von der Query getrennt ausgewertet
  (`urlsplit`). Bisher wäre ein Aufruf wie `/alarm?einsatznr=4711` als
  unbekannter Pfad abgewiesen worden.

## Version 1.0 (Alarm)

Erste Version aus dem ursprünglichen Skript, welches dabasiert arbeitete.

### Geändert

- Produktivbetrieb nutzt jetzt ein virtualenv (`venv`) statt eines
  systemweiten `pip install --break-system-packages`, analog zum
  iCal-AMWeb-Webservice. `elaserver.service` startet entsprechend über
  `/opt/elaserver/venv/bin/python` statt `/usr/bin/python3`.
- `elaserver.service` startet den Dienst jetzt unter `User=ela` /
  `Group=ela` statt als `root`.
- Konfiguration verwendet jetzt `elaserver.yaml` statt `elaserver.ini`
  (Abhängigkeit `PyYAML` statt der Standardbibliothek `configparser`), um
  konsistent mit anderen Projekten zu sein.
- `HTTPServer` durch `ThreadingHTTPServer` ersetzt, damit eingehende Requests
  sich nicht mehr gegenseitig blockieren.
- Das Schalten der GPIO-Ausgänge (Licht/Blitz) erfolgt nicht mehr blockierend
  per `time.sleep()` im Request-Handler, sondern asynchron über
  `threading.Timer`. Der HTTP-Response wird sofort zurückgegeben.
- Ein eingehendes `standby`-Signal bricht laufende Timer ab und schaltet
  Licht und Blitz sofort ab, statt bis zum Ablauf von `flash_duration`
  (Standardmäßig 900 Sekunden) zu warten.
- GPIO-Lines werden nur noch einmalig beim Start angefordert und bis zum
  Beenden des Dienstes gehalten (statt bei jedem einzelnen Request neu
  angefordert und freigegeben zu werden).
- Logging schreibt jetzt über einen `TimedRotatingFileHandler`
  (tägliche Rotation) statt unbegrenzt in eine einzelne Datei. Die
  Aufbewahrungsfrist ist über `log_retention_days` in `elaserver.ini`
  konfigurierbar (Standard: 14 Tage, falls nicht gesetzt).
- `SIGTERM` (z.B. durch `systemctl stop`) wird jetzt abgefangen, damit der
  Dienst und die GPIO-Ausgänge sauber heruntergefahren werden.
