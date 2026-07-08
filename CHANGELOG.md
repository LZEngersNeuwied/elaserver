# Changelog

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
