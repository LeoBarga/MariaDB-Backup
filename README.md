# mariadb_backup.py

Backup giornaliero di un database MariaDB con rotazione e archiviazione dei binary log per il point-in-time recovery (PITR).

## Struttura dei backup

```
BACKUP_DIR/
├── full/
│   ├── mio_database_20261021_021500.sql.gz
│   └── ...                       (ultimi MAX_BACKUPS)
└── binlogs/
    ├── 2026-10-21/
    │   ├── mariadb-bin.000042.gz
    │   └── ...
    └── ...                       (ultimi BINLOG_RETENTION_DAYS giorni)
```

Ogni ciclo: dump (scritto come `.part` e rinominato solo se completo) → rotazione → `FLUSH BINARY LOGS` e download dei binlog chiusi non ancora archiviati → pulizia archivio binlog. Se il dump fallisce, i vecchi dump **non** vengono ruotati e i binlog vengono archiviati comunque.

## 1. Abilitare i binary log sul server

Su Ubuntu sono disattivati di default. In `/etc/mysql/mariadb.conf.d/50-server.cnf`, sezione `[mysqld]`:

```ini
server_id         = 1
log_bin           = /var/log/mysql/mariadb-bin
log_bin_index     = /var/log/mysql/mariadb-bin.index
binlog_format     = ROW
expire_logs_days  = 10   # rete di sicurezza lato server
```

Poi `sudo systemctl restart mariadb`.

## 2. Utente di backup (MariaDB 10.5+)

```sql
CREATE USER 'backup'@'localhost' IDENTIFIED BY 'password-robusta';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES ON mio_database.* TO 'backup'@'localhost';
GRANT RELOAD, PROCESS, BINLOG MONITOR, REPLICATION SLAVE ON *.* TO 'backup'@'localhost';
-- solo se PURGE_SERVER_BINLOGS=true:
GRANT BINLOG ADMIN ON *.* TO 'backup'@'localhost';
```

`RELOAD` + `BINLOG MONITOR` servono a `--master-data` e `FLUSH BINARY LOGS`, `REPLICATION SLAVE` a scaricare i binlog con `mysqlbinlog --read-from-remote-server` (funziona quindi anche con DB remoto).

## 3. Installazione

```bash
sudo mkdir -p /opt/mariadb-backup
sudo cp mariadb_backup.py /opt/mariadb-backup/
sudo cp .env.example /opt/mariadb-backup/.env
sudo chmod 600 /opt/mariadb-backup/.env      # contiene la password
sudo nano /opt/mariadb-backup/.env
sudo python3 /opt/mariadb-backup/mariadb_backup.py --check
```

## 4. Scheduling

**Cron di sistema (consigliato)** — `sudo crontab -e`:

```cron
15 2 * * * /usr/bin/python3 /opt/mariadb-backup/mariadb_backup.py --env-file /opt/mariadb-backup/.env >/dev/null 2>&1
```

**APScheduler** — `pip install apscheduler`, poi un servizio systemd `/etc/systemd/system/mariadb-backup.service`:

```ini
[Unit]
Description=MariaDB backup daemon
After=mariadb.service

[Service]
ExecStart=/usr/bin/python3 /opt/mariadb-backup/mariadb_backup.py --daemon --env-file /opt/mariadb-backup/.env
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Un lock file impedisce esecuzioni sovrapposte in entrambi i casi.

## 5. Monitoraggio

Ogni fallimento (dump, binlog, configurazione errata, lock occupato, crash imprevisto, scheduler fermo) produce un messaggio con priorità syslog `daemon.crit`, tag `mariadb-backup`, che inizia con `MARIADB BACKUP DOWN:`.

```bash
journalctl -t mariadb-backup -p crit
grep "MARIADB BACKUP DOWN" /var/log/syslog
```

Exit code: `0` ok, `1` errore di backup, `2` errore di configurazione.

Nota: un alert su un errore non copre il caso "lo script non è proprio partito" (cron rimosso, server spento). Conviene aggiungere nel monitoraggio anche un controllo sull'età dell'ultimo file in `full/` (es. alert se > 26 ore).

## 6. Point-in-time recovery

Esempio: ripristinare lo stato al 21/10 alle 14:30.

```bash
# 1. Ripristina l'ultimo dump precedente al momento desiderato
gunzip -c full/mio_database_20261021_021500.sql.gz | mariadb -u root -p

# 2. Leggi la posizione del binlog registrata nel dump
zcat full/mio_database_20261021_021500.sql.gz | grep -m1 "CHANGE MASTER TO"
#   -- CHANGE MASTER TO MASTER_LOG_FILE='mariadb-bin.000042', MASTER_LOG_POS=12345;

# 3. Decomprimi i binlog da quel file in poi (in ordine)
mkdir /tmp/pitr && cp binlogs/*/mariadb-bin.0000{42,43,44}.gz /tmp/pitr/ && gunzip /tmp/pitr/*.gz

# 4. Riapplica gli eventi fino al momento desiderato
mariadb-binlog --start-position=12345 --stop-datetime="2026-10-21 14:30:00" \
  --database=mio_database /tmp/pitr/mariadb-bin.000042 /tmp/pitr/mariadb-bin.000043 /tmp/pitr/mariadb-bin.000044 \
  | mariadb -u root -p
```

`--start-position` si applica solo al primo file. I binlog contengono le modifiche di **tutti** i database del server; `--database` filtra su quello di interesse (con `binlog_format=ROW` il filtro è affidabile). Prova la procedura almeno una volta su un server di test: un backup mai ripristinato non è un backup verificato.
