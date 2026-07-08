# elaserver

Nimmt Webservice-Aufrufe des Alarmmonitors (AMWeb) entgegen und schaltet
darüber zwei Relais-Ausgänge (Licht und Blitzleuchte der ELA) über GPIO an
einem Raspberry Pi 5.

Der Dienst stellt zwei Endpunkte bereit: `GET /alarm` schaltet Licht- und
Blitz-Ausgang ein; beide schalten sich nach den in `elaserver.yaml`
konfigurierten Zeiten (`switch_duration` / `flash_duration`) automatisch
wieder aus. `GET /standby` schaltet beide Ausgänge sofort ab und bricht
laufende Timer ab. Alle anderen Pfade und HTTP-Methoden werden mit
`unknown request` bzw. `unknown method` beantwortet.

Das Schalten der Ausgänge läuft asynchron im Hintergrund (`threading.Timer`);
der HTTP-Response wird sofort zurückgegeben, sodass mehrere Anfragen
(z. B. `alarm` gefolgt von `standby`) unabhängig von laufenden Wartezeiten
verarbeitet werden.

## Setup (lokal / zum Testen)

```bash
cd elaserver
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp elaserver.yaml.example elaserver.yaml
# elaserver.yaml an die eigene Verkabelung/Adressierung anpassen

python3 elaserver.py
```

Testaufruf aus einem zweiten Terminal: `curl http://localhost:8080/alarm`

Da der Dienst beim Start eine echte `gpiochip4`-Schnittstelle erwartet, lässt
er sich nur auf einem Raspberry Pi 5 (oder einem Gerät mit passendem
GPIO-Chip) vollständig starten. Ohne diese Hardware bricht der Start beim
Öffnen des GPIO-Chips ab.

## Konfiguration

Alle Einstellungen liegen in `elaserver.yaml` (Vorlage: `elaserver.yaml.example`):

- `server.address` / `server.port` — Adresse und Port des Webservice (Standard: `localhost` / `8080`)
- `releais.gpio_port_lights_alarm` / `releais.gpio_port_flash_alarm` — GPIO-Pins (BCM-Nummerierung) der beiden Relais-Ausgänge; `0` bzw. Weglassen deaktiviert den jeweiligen Ausgang
- `releais.switch_duration` / `releais.flash_duration` — Sekunden, die Licht- bzw. Blitz-Ausgang nach einem Alarm automatisch eingeschaltet bleiben, bevor sie sich selbst abschalten
- `logging.log_retention_days` — Anzahl Tage, die rotierte Logdateien aufbewahrt werden (Standard: 14, Rotation täglich um Mitternacht)

Ein `standby`-Aufruf schaltet beide Ausgänge unabhängig von den konfigurierten
Zeiten sofort ab.

Die tatsächliche `elaserver.yaml` ist standortspezifisch (Adresse, Pins,
Zeiten) und daher bewusst **nicht** Teil des Git-Repositories (siehe
`.gitignore`). Anders als bei anderen Diensten enthält die Konfiguration
keine sensiblen Daten, daher ist kein Override per Umgebungsvariable
vorgesehen.

## Produktivbetrieb (systemd)

1. Projekt nach `/opt/elaserver` kopieren
2. Virtualenv dort anlegen (`python3 -m venv venv && venv/bin/pip install -r requirements.txt`)
3. Systembenutzer anlegen

   ```bash
   sudo useradd \
    --system \
    --home /opt/elaserver \
    --shell /usr/sbin/nologin \
    ela
   ```

   Zusätzlich benötigt `ela` Zugriff auf die GPIO-Hardware: Auf Raspberry Pi
   OS gehört `/dev/gpiochip4` der Gruppe `gpio`.

   ```bash
   sudo usermod -aG gpio ela
   ```

   Prüfen, ob die Gruppe `gpio` existiert und `/dev/gpiochip4` ihr gehört:
   `getent group gpio` und `ls -l /dev/gpiochip4`. Gehört das Gerät auf dem
   jeweiligen System einer anderen Gruppe, muss `ela` stattdessen dieser
   Gruppe hinzugefügt werden.

4. Berechtigungen setzen

   ```bash
   sudo chown -R ela:ela /opt/elaserver && \
   sudo find /opt/elaserver -type d -exec chmod 755 {} \; && \
   sudo find /opt/elaserver -type f -exec chmod 644 {} \; && \
   sudo chmod 755 /opt/elaserver/venv/bin/python && \
   sudo chown -R ela:ela /opt/elaserver/venv
   ```

5. `elaserver.service` nach `/etc/systemd/system/` kopieren
6. Danach:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now elaserver
sudo systemctl status elaserver
```

`elaserver.service` enthält bereits `User=ela` und `Group=ela`; da `ela`
gemäß Schritt 3 Mitglied der Gruppe `gpio` ist, übernimmt systemd diese
zusätzliche Gruppenmitgliedschaft automatisch beim Start.

Logs: `journalctl -u elaserver -f` (Start/Stopp/Fehler) sowie
`/opt/elaserver/amweb-webhook.log` (Alarm- und Standby-Ereignisse, rotiert
gemäß `log_retention_days`).

### Hinweis zur Netzwerk-Sicherheit

Der Dienst prüft eingehende Anfragen nicht auf Authentizität. Das ist im
Rahmen des internen Netzes, in dem der Dienst betrieben wird, bewusst so
vorgesehen. Sollte der Dienst je über ein weniger vertrauenswürdiges Netz
erreichbar sein, sollte vorher ein Zugriffsschutz (z. B. Firewall-Regeln oder
ein Shared Secret) ergänzt werden.

## Projektstruktur

```
elaserver.py             Der eigentliche Dienst
elaserver.yaml.example   Vorlage für die Konfiguration
elaserver.service        systemd-Unit-Datei
requirements.txt         Python-Abhängigkeiten
CHANGELOG.md             Übersicht der Optimierungen gegenüber der Vorversion
LICENSE                  MIT-Lizenz
```
