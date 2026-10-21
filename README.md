# mariadbbackup.py

Script Python per il backup automatico di un database MariaDB su Ubuntu Server, con point-in-time recovery (PITR), crittografia dei backup e controllo degli accessi.

## Funzionalità

- **Backup completo** del database con `mysqldump`, consistente e senza bloccare le scritture (`--single-transaction`), compresso con gzip e con timestamp nel nome.
- **Rotazione automatica**: vengono conservati solo gli ultimi `MAX_BACKUPS` dump.
- **Archiviazione dei binary log** per il point-in-time recovery, conservati per `BINLOG_RETENTION_DAYS` giorni. Permettono di ripristinare il database a qualsiasi istante tra un backup e l'altro.
- **Crittografia** con chiave pubblica (`age`): sul server c'è solo la chiave che cifra, quella che decifra resta altrove.
- **Checksum SHA-256** di ogni archivio, verificabili senza la chiave privata.
- **Autenticazione senza password** tramite `unix_socket`, oppure con password per database remoti.
- **Controllo accessi**: lo script gira solo come utente di servizio dedicato. Avviarlo o fermarlo richiede `sudo`.
- **Alert su syslog** a livello critical per ogni errore, permesso insicuro o arresto del backup, rilevabili dal monitoraggio.
- **Logging su file** con rotazione automatica, più syslog.
- **Esecuzione** singola (per cron) oppure come daemon con APScheduler (per systemd).
- **Robustezza**:
  - un dump incompleto o interrotto non diventa mai un backup "valido";
  - se il dump fallisce, i backup vecchi non vengono eliminati;
  - un lock impedisce esecuzioni sovrapposte;
  - lo spazio libero su disco viene controllato prima di scrivere.

## Struttura dei backup

```
BACKUP_DIR/                                        (700, solo dbbackup)
├── full/
│   ├── mio_database_20261021_021500.sql.gz.age          (600, cifrato)
│   ├── mio_database_20261021_021500.sql.gz.age.sha256   (checksum)
│   └── ...                       (ultimi MAX_BACKUPS)
└── binlogs/
    ├── 2026-10-21/
    │   ├── mariadb-bin.000042.gz.age
    │   ├── mariadb-bin.000042.gz.age.sha256
    │   └── ...
    └── ...                       (ultimi BINLOG_RETENTION_DAYS giorni)
```

Con `ENCRYPTION_ENABLED=false` i file hanno estensione `.gz` invece di `.gz.age`.

## Come funziona un ciclo di backup

1. **Dump.** `mysqldump` viene compresso e cifrato al volo, senza che il dump passi mai in chiaro dal disco. Il file viene scritto come `.part` e rinominato solo se il dump è terminato correttamente e contiene il marker finale `-- Dump completed`. Nel dump viene registrata la posizione esatta nel binary log, punto di partenza per il point-in-time recovery.
2. **Rotazione.** Restano gli ultimi `MAX_BACKUPS` dump, insieme ai loro checksum. Se il dump del giorno è fallito, la rotazione viene saltata. Anche eventuali dump non cifrati presenti nella directory rientrano nella rotazione e vengono eliminati con il tempo.
3. **Binary log.**
   - `FLUSH BINARY LOGS` chiude il binlog corrente.
   - I binlog chiusi non ancora archiviati vengono scaricati con `mysqlbinlog --read-from-remote-server`, poi compressi e cifrati.
   - Il binlog ancora attivo non viene mai archiviato, perché sarebbe una copia parziale.
4. **Pulizia binlog.** Le directory giornaliere più vecchie di `BINLOG_RETENTION_DAYS` vengono eliminate. Se `PURGE_SERVER_BINLOGS=true`, e solo se l'archiviazione del ciclo è riuscita, vengono eliminati anche dal server i binlog più vecchi di `SERVER_BINLOG_KEEP_DAYS`.

Gli step sono indipendenti: se il dump fallisce, i binlog vengono archiviati comunque, per non perdere copertura PITR. Qualsiasi errore genera un alert critical (vedi *Monitoraggio*).

## Autenticazione

| Modalità | Cosa c'è da rubare | Quando usarla |
|---|---|---|
| `AUTH_MODE=socket` | **niente**: non esiste una password. MariaDB verifica tramite il kernel che il processo giri come l'utente di sistema `dbbackup`, e solo root può diventare quell'utente | database sullo **stesso server** (consigliata) |
| `AUTH_MODE=password` | la password salvata nel `.env` | database su un **altro host** |

Con `unix_socket` il segreto non esiste, quindi non può finire in un file, in un backup del filesystem, in un log o nella lista dei processi. In più l'utente MariaDB può connettersi solo in locale.

In modalità password, la password viene passata ai client MariaDB tramite un file temporaneo con permessi `600`, cancellato a fine ciclo. Non compare mai negli argomenti dei processi, che sono visibili con `ps`.

Lo script rifiuta di partire se in modalità `socket` trova una `DB_PASSWORD` impostata, oppure un `DB_USER` diverso dall'utente di sistema.

## Crittografia: perché age e non "AES o SHA"

**SHA non cifra.** SHA-256 è una funzione di hash: produce un'impronta del file e serve a verificare che non sia corrotto, ma non nasconde il contenuto. Nello script è usato proprio così, nei file `.sha256`.

**AES con una chiave salvata sul server protegge poco.** Con la cifratura simmetrica la stessa chiave cifra e decifra, quindi deve stare sul server perché lo script possa usarla. Lo scenario in cui servono i backup, cioè server compromesso o ransomware, è anche quello in cui l'attaccante trova la chiave accanto ai file cifrati.

**La scelta: cifratura a chiave pubblica con [age](https://age-encryption.org).**
- Sul server c'è solo la **chiave pubblica**, che sa solo cifrare. La **chiave privata**, che decifra, resta fuori dal server: password manager, chiavetta in cassaforte o macchina dell'amministratore.
- Chi ruba il server, i backup o una loro copia trova solo dati illeggibili, compresi i backup dei giorni precedenti.
- age usa X25519 per lo scambio di chiave e ChaCha20-Poly1305 per i dati. È cifratura **autenticata**: un archivio alterato o troncato non viene decifrato in silenzio, ma dà errore. Il livello di sicurezza è equivalente ad AES-256-GCM, cambia solo l'algoritmo.
- È un singolo comando nei repository Ubuntu (`apt install age`), senza keyring né trust database da gestire come GPG.

**Cosa resta in chiaro.** I binary log scaricati dal server passano per qualche istante in chiaro in `binlogs/.incoming/`, una directory `700` di `dbbackup`, e vengono cancellati subito dopo la cifratura. Eventuali residui di un'esecuzione interrotta vengono rimossi al ciclo successivo. I dump non toccano mai il disco in chiaro. Ricorda che MariaDB conserva comunque in chiaro i propri dati e binlog sullo stesso server.

**Rischio principale: perdere la chiave privata.** Senza la chiave privata i backup sono **irrecuperabili per sempre**. Per questo si usano due chiavi: una principale e una di emergenza, conservate in posti diversi.

## Requisiti

- Ubuntu Server con MariaDB 10.5 o successivo
- Python 3.8+ (solo libreria standard)
- Client MariaDB: `mariadb-dump`/`mysqldump`, `mariadb`/`mysql`, `mariadb-binlog`/`mysqlbinlog` (pacchetto `mariadb-client`)
- `age` se la crittografia è attiva (`sudo apt install age`)
- `apscheduler` solo per la modalità `--daemon` (`pip install apscheduler`)

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

Se non vuoi il point-in-time recovery, imposta `BINLOG_ENABLED=false` e salta questo passaggio.

## 2. Utente di sistema e utente MariaDB

### Modalità socket (consigliata)

Utente di sistema dedicato, senza shell e senza password: nessuno può farci login, solo root può eseguire comandi come lui.

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin dbbackup
```

Utente MariaDB con lo **stesso nome**, autenticato via `unix_socket`:

```sql
CREATE USER 'dbbackup'@'localhost' IDENTIFIED VIA unix_socket;
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES ON mio_database.* TO 'dbbackup'@'localhost';
GRANT RELOAD, PROCESS, BINLOG MONITOR, REPLICATION SLAVE ON *.* TO 'dbbackup'@'localhost';
-- solo se PURGE_SERVER_BINLOGS=true:
GRANT BINLOG ADMIN ON *.* TO 'dbbackup'@'localhost';
```

Nel `.env`: `AUTH_MODE=socket`, nessuna `DB_PASSWORD`.

### Modalità password (database remoto)

L'utente di sistema `dbbackup` serve comunque, perché lo script gira solo come lui.

```sql
CREATE USER 'backup'@'IP_DEL_SERVER_DI_BACKUP' IDENTIFIED BY 'password-lunga-e-casuale';
-- stessi GRANT di sopra, sostituendo l'utente
```

Limita l'utente all'IP del server di backup, non usare `'%'`. Genera la password con `openssl rand -base64 32`. Su rete non fidata abilita TLS sul server MariaDB, perché dump e binlog viaggiano in chiaro prima di essere cifrati.

`RELOAD` + `BINLOG MONITOR` servono a registrare la posizione del binlog nel dump e a `FLUSH BINARY LOGS`, `REPLICATION SLAVE` a scaricare i binlog con `mysqlbinlog --read-from-remote-server`.

## 3. Chiavi di crittografia

Da fare **su un computer sicuro, non sul server**:

```bash
sudo apt install age

age-keygen -o backup-principale.key     # stampa: Public key: age1....
age-keygen -o backup-emergenza.key      # seconda chiave, da custodire altrove

# facoltativo ma consigliato: proteggere ogni chiave privata con una passphrase
age -p -o backup-principale.key.age backup-principale.key && shred -u backup-principale.key
age -p -o backup-emergenza.key.age backup-emergenza.key && shred -u backup-emergenza.key
```

Conserva le chiavi private in due luoghi diversi, per esempio un password manager aziendale e una chiavetta in cassaforte. **Non copiarle sul server.**

Sul server metti solo le **due chiavi pubbliche**, una per riga. Ogni backup sarà decifrabile con l'una o con l'altra. Crea il file con `sudo nano /opt/mariadb-backup/recipients.txt`:

```
# chiave principale
age1...........
# chiave di emergenza
age1...........
```

Poi imposta i permessi:

```bash
sudo chown root:root /opt/mariadb-backup/recipients.txt
sudo chmod 644 /opt/mariadb-backup/recipients.txt
```

Il file delle chiavi pubbliche deve essere di root e non scrivibile da altri, altrimenti il backup non parte. Chi potesse sostituirlo farebbe cifrare i backup con una chiave sua, rendendoli leggibili a lui e illeggibili a te. Lo script rifiuta anche di partire se nel file trova per errore una chiave **privata**.

## 4. Installazione (permessi obbligatori)

```bash
sudo apt install age mariadb-client

# codice e configurazione: di root, non modificabili dal servizio
sudo install -d -o root -g root -m 755 /opt/mariadb-backup
sudo install -o root -g root -m 755 mariadbbackup.py /opt/mariadb-backup/mariadbbackup.py
sudo install -o root -g dbbackup -m 640 .env.example /opt/mariadb-backup/.env
sudo nano /opt/mariadb-backup/.env
# recipients.txt: vedi sezione 3

# dati e log: del servizio, invisibili agli altri utenti
sudo install -d -o dbbackup -g dbbackup -m 700 /var/backups/mariadb
sudo install -d -o dbbackup -g dbbackup -m 750 /var/log/mariadb-backup

# verifica, eseguita come utente di servizio (include una cifratura di prova)
sudo -u dbbackup /usr/bin/python3 /opt/mariadb-backup/mariadbbackup.py --env-file /opt/mariadb-backup/.env --check
```

Lo script imposta `umask 077`: ogni file creato è `600` e ogni directory `700`, quindi leggibile solo da `dbbackup`.

Lo script controlla questi permessi a ogni avvio. **Il backup non parte**, e viene emesso un CRITICAL, se:
- il `.env`, lo script o il file delle chiavi pubbliche non sono di root;
- uno di questi file, o la sua directory, è scrivibile da altri utenti;
- il `.env` è leggibile da tutti.

Lo script e la configurazione appartengono a root e non all'utente `dbbackup`. Così anche se il processo del backup venisse compromesso, non potrebbe modificare il proprio codice, la configurazione o le chiavi di destinazione.

## 5. Configurazione (`.env`)

Il file viene letto da `--env-file` (default: `.env` nella stessa directory dello script). I valori del file **hanno la precedenza** sulle variabili d'ambiente: la configurazione valida è solo quella nel file protetto. Sintassi `CHIAVE=valore`, righe con `#` ignorate, virgolette opzionali.

| Parametro | Default | Descrizione |
|---|---|---|
| `RUN_AS_USER` | `dbbackup` | Unico utente di sistema autorizzato a eseguire lo script |
| `AUTH_MODE` | `socket` | `socket` (unix_socket, senza password) o `password` |
| `DB_SOCKET` | `/run/mysqld/mysqld.sock` | Socket di MariaDB (modalità socket) |
| `DB_HOST` | `localhost` | Host del database (modalità password) |
| `DB_PORT` | `3306` | Porta del database (modalità password) |
| `DB_USER` | utente di sistema | In modalità socket deve coincidere con l'utente di sistema; obbligatorio in modalità password |
| `DB_PASSWORD` | — | Obbligatoria in modalità password, vietata in modalità socket |
| `DB_NAME` | **obbligatorio** | Database da salvare |
| `BACKUP_DIR` | **obbligatorio** | Directory di destinazione dei backup |
| `MAX_BACKUPS` | `5` | Numero di dump completi da conservare |
| `BINLOG_ENABLED` | `true` | Archiviazione dei binary log (PITR) |
| `BINLOG_RETENTION_DAYS` | `7` | Giorni di binlog archiviati da conservare. Con dump giornalieri dovrebbe essere ≥ `MAX_BACKUPS`, altrimenti i dump più vecchi non sono coperti dal PITR (lo script lo segnala con un warning) |
| `PURGE_SERVER_BINLOGS` | `false` | Elimina dal server i binlog vecchi, solo dopo un'archiviazione riuscita |
| `SERVER_BINLOG_KEEP_DAYS` | `3` | Giorni di binlog da lasciare sul server se il purge è attivo |
| `ENCRYPTION_ENABLED` | `true` | Crittografia degli archivi con age |
| `AGE_RECIPIENTS_FILE` | obbligatorio se cifratura attiva | File con le chiavi pubbliche, una per riga |
| `AGE_BIN` | `age` nel `PATH` | Percorso esplicito di age |
| `LOG_FILE` | `/var/log/mariadb-backup/backup.log` | File di log (ruotato a 10 MB, 5 copie) |
| `MIN_FREE_MB` | `1024` | Spazio libero minimo richiesto prima di scrivere (`0` = nessun controllo) |
| `SCHEDULE_TIME` | `02:15` | Orario giornaliero del backup in modalità `--daemon` (HH:MM, ora locale) |
| `DUMP_EXTRA_ARGS` | — | Opzioni aggiuntive per mysqldump, es. `--ignore-table=mio_database.log_table` |
| `MYSQLDUMP_BIN`, `MYSQL_BIN`, `MYSQLBINLOG_BIN` | ricerca automatica | Percorsi espliciti dei client MariaDB |

## 6. Utilizzo

```
mariadbbackup.py [--env-file FILE] [--daemon | --check | --verify] [-v]
```

| Opzione | Funzione |
|---|---|
| *(nessuna)* | Esegue un ciclo di backup completo e termina. È la modalità per cron |
| `--daemon` | Resta attivo ed esegue il backup ogni giorno alle `SCHEDULE_TIME`, tramite APScheduler. È la modalità per systemd |
| `--check` | Verifica configurazione, permessi, connessione al database, stato dei binary log, privilegi dell'utente, spazio su disco e crittografia (con una cifratura di prova), poi termina |
| `--verify` | Ricalcola lo SHA-256 di ogni archivio e lo confronta con il suo `.sha256`, poi termina. Non richiede la chiave privata |
| `--env-file FILE` | File di configurazione da usare |
| `-v`, `--verbose` | Mostra in console anche i messaggi di debug |

Exit code:
- `0` ok;
- `1` errore di backup, interruzione o verifica fallita;
- `2` errore di configurazione o permessi;
- `3` utente non autorizzato.

## 7. Scheduling

Lo script va eseguito **come `dbbackup`** e solo come lui. Lanciato da qualsiasi altro utente, **root compreso**, viene rifiutato con exit code `3`. Root viene rifiutato perché creerebbe file di proprietà di root e userebbe l'utente MariaDB sbagliato.

### Opzione A — servizio systemd con APScheduler (consigliata)

`pip install apscheduler`, poi `/etc/systemd/system/mariadb-backup.service`:

```ini
[Unit]
Description=MariaDB backup daemon
After=mariadb.service

[Service]
User=dbbackup
Group=dbbackup
ExecStart=/usr/bin/python3 /opt/mariadb-backup/mariadbbackup.py --daemon --env-file /opt/mariadb-backup/.env
Restart=on-failure
UMask=0077

# Hardening: il processo può scrivere solo nelle directory dei backup e dei log
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/backups/mariadb /var/log/mariadb-backup
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo chmod 644 /etc/systemd/system/mariadb-backup.service
sudo systemctl daemon-reload
sudo systemctl enable --now mariadb-backup
```

Se il daemon è attivo ma l'esecuzione parte in ritardo, per esempio dopo una sospensione del sistema, viene eseguita comunque entro un'ora. Oltre quel limite viene saltata e segnalata con un alert. Le esecuzioni perse mentre il servizio era spento non vengono recuperate: le copre il controllo sull'età dell'ultimo backup (vedi *Monitoraggio*).

### Opzione B — cron di sistema

File `/etc/cron.d/mariadb-backup`, di root con permessi `644`. Il sesto campo è l'utente:

```cron
15 2 * * * dbbackup /usr/bin/python3 /opt/mariadb-backup/mariadbbackup.py --env-file /opt/mariadb-backup/.env >/dev/null 2>&1
```

In entrambi i casi un lock file impedisce esecuzioni sovrapposte. Un backup lanciato a mano mentre un altro è in corso viene rifiutato con un alert.

## 8. Chi può avviare e fermare i backup

| Azione | Chi può farla | Perché |
|---|---|---|
| `systemctl start/stop/restart/disable mariadb-backup` | solo root (`sudo`) | è un servizio di sistema: per un utente normale systemd/polkit rifiuta l'operazione o chiede la password di amministratore |
| uccidere il processo (`kill`, `pkill`) | solo root | il kernel consente di inviare segnali solo ai processi del proprio utente, e nessuno può fare login come `dbbackup` |
| modificare o eliminare `/etc/cron.d/mariadb-backup` o il file `.service` | solo root | file di root, non scrivibili da altri |
| modificare script, `.env` o chiavi pubbliche | solo root | file di root; e se i permessi vengono allentati il backup si blocca con un CRITICAL |
| lanciare lo script a mano | solo root, tramite `sudo -u dbbackup ...` | gli altri utenti non possono leggere il `.env` e vengono rifiutati |
| leggere i backup | **nessuno sul server**, nemmeno root | file `600` in directory `700` e, soprattutto, cifrati: senza la chiave privata, che non è sul server, sono illeggibili |

Comandi per gli amministratori:

```bash
sudo systemctl status mariadb-backup
sudo systemctl stop mariadb-backup        # genera un CRITICAL: è voluto
sudo systemctl start mariadb-backup
sudo -u dbbackup /usr/bin/python3 /opt/mariadb-backup/mariadbbackup.py --env-file /opt/mariadb-backup/.env   # backup manuale immediato
```

**Delegare a un operatore senza dargli root.** Se un utente deve poter gestire *solo* il backup, crea con `sudo visudo -f /etc/sudoers.d/mariadb-backup` una regola limitata:

```
operatore ALL=(root) NOPASSWD: /usr/bin/systemctl start mariadb-backup, /usr/bin/systemctl stop mariadb-backup, /usr/bin/systemctl status mariadb-backup
operatore ALL=(dbbackup) NOPASSWD: /usr/bin/python3 /opt/mariadb-backup/mariadbbackup.py --env-file /opt/mariadb-backup/.env
```

**Limite da conoscere:** chi è nel gruppo `sudo` è amministratore e può fare tutto, quindi anche fermare i backup. La protezione non lo impedisce, ma lo rende **visibile**. Mantieni il gruppo `sudo` ristretto. Nemmeno root, però, può leggere i backup già fatti senza la chiave privata.

## 9. Monitoraggio

Ogni problema produce un messaggio con priorità syslog `daemon.crit`, tag `mariadb-backup`, che inizia con `MARIADB BACKUP DOWN:`. Il messaggio arriva:
- nel file di log;
- nel syslog e nel journal di systemd;
- se il socket di syslog non è raggiungibile, tramite il comando `logger`.

Casi che generano l'alert:
- dump, cifratura o archiviazione dei binlog falliti;
- configurazione non valida o permessi di installazione insicuri;
- binary log disattivati sul server;
- spazio su disco insufficiente;
- esecuzione già in corso (lock occupato);
- crash imprevisto o errore dello scheduler;
- **qualsiasi arresto**: servizio fermato con `systemctl stop`, processo terminato da un segnale, backup interrotto a metà. In quest'ultimo caso il file parziale viene eliminato e i backup vecchi restano;
- verifica di integrità fallita.

Un `systemctl restart` legittimo genera quindi un alert: è voluto, così nessun arresto passa inosservato.

I tentativi di esecuzione da utenti non autorizzati sono registrati come `WARNING` nel syslog, utili come traccia di audit. Non generano CRITICAL, altrimenti chiunque potrebbe produrre falsi allarmi.

```bash
journalctl -t mariadb-backup -p crit
journalctl -t mariadb-backup -p warning     # include i tentativi rifiutati
grep "MARIADB BACKUP DOWN" /var/log/syslog
```

**Verifica di integrità.** Conviene schedulare `--verify` una volta alla settimana, aggiungendo in `/etc/cron.d/mariadb-backup`:

```cron
30 4 * * 0 dbbackup /usr/bin/python3 /opt/mariadb-backup/mariadbbackup.py --env-file /opt/mariadb-backup/.env --verify >/dev/null 2>&1
```

I file `.sha256` sono nel formato standard, quindi si possono verificare anche a mano con `sha256sum -c`. Il checksum rileva **corruzione** del disco o della copia. Non protegge da chi ha già accesso in scrittura ai backup, cioè `dbbackup` o root: quella protezione viene dai permessi.

**Backup mai partito.** Un alert su un errore non copre il caso in cui lo script non parte affatto, per esempio server spento o cron rimosso da root. Conviene aggiungere nel monitoraggio anche un controllo sull'età dell'ultimo file in `full/` (es. alert se > 26 ore).

## 10. Ripristino e point-in-time recovery

Il ripristino richiede la **chiave privata**. Portala sul server solo per il tempo necessario, in una directory di root, e cancellala alla fine. In alternativa decifra i file su un'altra macchina e trasferisci i dati.

Esempio: ripristinare lo stato al 21/10 alle 14:30.

```bash
cd /var/backups/mariadb
sudo install -d -m 700 /root/pitr
# copia la chiave privata in /root/pitr/key.txt
# (se l'avevi protetta con passphrase: age -d -o /root/pitr/key.txt backup-principale.key.age)

# 0. Verifica che gli archivi siano integri
sudo -u dbbackup /usr/bin/python3 /opt/mariadb-backup/mariadbbackup.py --env-file /opt/mariadb-backup/.env --verify

# 1. Ripristina l'ultimo dump precedente al momento desiderato
sudo bash -c 'age -d -i /root/pitr/key.txt full/mio_database_20261021_021500.sql.gz.age | gunzip | mariadb'

# 2. Leggi la posizione del binlog registrata nel dump
sudo bash -c 'age -d -i /root/pitr/key.txt full/mio_database_20261021_021500.sql.gz.age | gunzip | grep -m1 "CHANGE MASTER TO"'
#   -- CHANGE MASTER TO MASTER_LOG_FILE='mariadb-bin.000042', MASTER_LOG_POS=12345;

# 3. Decifra e decomprimi i binlog da quel file in poi
sudo bash -c 'for n in 42 43 44; do
  f=$(ls binlogs/*/mariadb-bin.0000$n.gz.age)
  age -d -i /root/pitr/key.txt "$f" | gunzip > /root/pitr/mariadb-bin.0000$n
done'

# 4. Riapplica gli eventi fino al momento desiderato
sudo bash -c 'mariadb-binlog --start-position=12345 --stop-datetime="2026-10-21 14:30:00" \
  --database=mio_database /root/pitr/mariadb-bin.000042 /root/pitr/mariadb-bin.000043 /root/pitr/mariadb-bin.000044 \
  | mariadb'

# 5. Elimina la chiave privata e i file in chiaro
sudo shred -u /root/pitr/key.txt && sudo rm -rf /root/pitr
```

Per ripristinare solo l'ultimo dump, senza point-in-time recovery, bastano i passi 1 e 5.

Il ripristino si fa come root MariaDB (`sudo mariadb`, anch'esso via `unix_socket`), non con l'utente di backup, che ha volutamente solo permessi di lettura.

`--start-position` si applica solo al primo file. I binlog contengono le modifiche di **tutti** i database del server; `--database` filtra su quello di interesse (con `binlog_format=ROW` il filtro è affidabile).




Si consiglia di provare la procedura almeno una volta su un server di test e di ripeterla periodicamente, sopratutto considerando che con backup cifrati è l'unico modo per verificare il funzionamente delle chiavi.