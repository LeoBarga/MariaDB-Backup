#!/usr/bin/env python3
"""
mariadb_backup.py - Backup automatico di un database MariaDB con supporto PITR.

Funzioni:
  * dump completo giornaliero (mysqldump, compresso gzip, timestamp nel nome)
  * rotazione: mantiene solo gli ultimi MAX_BACKUPS dump
  * archiviazione dei binary log (via mysqlbinlog --read-from-remote-server)
    con conservazione per BINLOG_RETENTION_DAYS giorni
  * logging su file (con rotazione) + syslog; ogni fallimento genera un
    messaggio CRITICAL "MARIADB BACKUP DOWN" rilevabile dal monitoraggio

Modalità d'uso:
  mariadb_backup.py                  esegue un ciclo e termina (per cron/systemd)
  mariadb_backup.py --daemon         resta attivo e schedula con APScheduler
  mariadb_backup.py --check          verifica configurazione e connessione

Dipendenze: solo libreria standard Python 3.8+ e i client MariaDB
(mysqldump/mysql/mysqlbinlog). APScheduler serve solo per --daemon.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import logging
import logging.handlers
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

APP_NAME = "mariadb-backup"
DEFAULT_ENV_FILE = Path(__file__).resolve().with_name(".env")
DEFAULT_LOG_FILE = "/var/log/mariadb-backup/backup.log"
CRITICAL_TAG = "MARIADB BACKUP DOWN"

log = logging.getLogger(APP_NAME)
_syslog_ok = False


# --------------------------------------------------------------------------- #
# Configurazione
# --------------------------------------------------------------------------- #
class ConfigError(Exception):
    pass


class StepError(Exception):
    """Errore atteso in uno step del job (messaggio già leggibile)."""


def load_env_file(path: Path) -> None:
    """Parser minimale di file .env: KEY=VALUE, commenti con #, virgolette opzionali.
    Le variabili già presenti nell'ambiente hanno la precedenza."""
    if not path.is_file():
        return
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"{path}:{lineno}: riga non valida (atteso KEY=VALUE)")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_str(name: str, default: Optional[str] = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"variabile obbligatoria mancante: {name}")
    return value or ""


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} deve essere un intero (valore: {raw!r})")
    if value < minimum:
        raise ConfigError(f"{name} deve essere >= {minimum} (valore: {value})")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if raw.lower() in ("1", "true", "yes", "on", "si", "sì"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} deve essere true/false (valore: {raw!r})")


def _find_binary(env_name: str, candidates: List[str]) -> str:
    explicit = os.environ.get(env_name)
    for name in ([explicit] if explicit else candidates):
        path = shutil.which(name)
        if path:
            return path
    raise ConfigError(
        f"eseguibile non trovato ({explicit or ' / '.join(candidates)}); "
        f"installa mariadb-client o imposta {env_name}"
    )


@dataclass
class Config:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    backup_dir: Path
    max_backups: int
    binlog_enabled: bool
    binlog_retention_days: int
    purge_server_binlogs: bool
    server_binlog_keep_days: int
    min_free_mb: int
    schedule_time: str
    mysqldump_bin: str
    mysql_bin: str
    mysqlbinlog_bin: str
    dump_extra_args: List[str] = field(default_factory=list)

    @property
    def full_dir(self) -> Path:
        return self.backup_dir / "full"

    @property
    def binlog_dir(self) -> Path:
        return self.backup_dir / "binlogs"

    @classmethod
    def from_env(cls) -> "Config":
        binlog_enabled = _env_bool("BINLOG_ENABLED", True)
        schedule_time = _env_str("SCHEDULE_TIME", "02:15")
        try:
            dt.datetime.strptime(schedule_time, "%H:%M")
        except ValueError:
            raise ConfigError(f"SCHEDULE_TIME deve essere HH:MM (valore: {schedule_time!r})")

        return cls(
            db_host=_env_str("DB_HOST", "localhost"),
            db_port=_env_int("DB_PORT", 3306, minimum=1),
            db_name=_env_str("DB_NAME", required=True),
            db_user=_env_str("DB_USER", required=True),
            db_password=_env_str("DB_PASSWORD", ""),
            backup_dir=Path(_env_str("BACKUP_DIR", required=True)).expanduser(),
            max_backups=_env_int("MAX_BACKUPS", 5, minimum=1),
            binlog_enabled=binlog_enabled,
            binlog_retention_days=_env_int("BINLOG_RETENTION_DAYS", 7, minimum=1),
            purge_server_binlogs=_env_bool("PURGE_SERVER_BINLOGS", False),
            server_binlog_keep_days=_env_int("SERVER_BINLOG_KEEP_DAYS", 3, minimum=1),
            min_free_mb=_env_int("MIN_FREE_MB", 1024),
            schedule_time=schedule_time,
            mysqldump_bin=_find_binary("MYSQLDUMP_BIN", ["mariadb-dump", "mysqldump"]),
            mysql_bin=_find_binary("MYSQL_BIN", ["mariadb", "mysql"]),
            mysqlbinlog_bin=(
                _find_binary("MYSQLBINLOG_BIN", ["mariadb-binlog", "mysqlbinlog"])
                if binlog_enabled else ""
            ),
            dump_extra_args=shlex.split(_env_str("DUMP_EXTRA_ARGS", "")),
        )


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging(log_file: Path, verbose: bool) -> None:
    global _syslog_ok
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    log.addHandler(console)

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as exc:
        log.error("impossibile aprire il file di log %s: %s", log_file, exc)

    _syslog_ok = False
    if os.path.exists("/dev/log"):
        try:
            sh = _SafeSysLogHandler(
                address="/dev/log", facility=logging.handlers.SysLogHandler.LOG_DAEMON
            )
            sh.setLevel(logging.WARNING)
            sh.setFormatter(logging.Formatter(f"{APP_NAME}[{os.getpid()}]: %(levelname)s %(message)s"))
            log.addHandler(sh)
            _syslog_ok = True
        except OSError as exc:
            log.warning("syslog non disponibile (/dev/log): %s", exc)
    else:
        log.warning("syslog non disponibile (/dev/log assente): uso il comando 'logger' come fallback")


def _logger_cmd(priority: str, message: str) -> None:
    """Fallback: comando `logger` di util-linux (scrive anche nel journal)."""
    try:
        subprocess.run(["logger", "-p", priority, "-t", APP_NAME, message],
                       check=False, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - ultimo tentativo, non deve mai sollevare
        pass


class _SafeSysLogHandler(logging.handlers.SysLogHandler):
    """SysLogHandler che, se il socket non risponde, ripiega su `logger`
    invece di stampare un traceback e perdere il messaggio."""

    def handleError(self, record: logging.LogRecord) -> None:
        prio = "daemon.crit" if record.levelno >= logging.CRITICAL else (
            "daemon.err" if record.levelno >= logging.ERROR else "daemon.warning")
        _logger_cmd(prio, f"{record.levelname} {record.getMessage()}")


def notify_critical(message: str) -> None:
    """Invia un messaggio CRITICAL su file, stderr e syslog (priorità daemon.crit)."""
    full = f"{CRITICAL_TAG}: {message}"
    log.critical(full)
    if not _syslog_ok:
        _logger_cmd("daemon.crit", full)


# --------------------------------------------------------------------------- #
# Utilità
# --------------------------------------------------------------------------- #
def _quote_cnf(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@contextmanager
def credentials_file(cfg: Config) -> Iterator[str]:
    """File di opzioni temporaneo (0600): la password non compare mai in `ps`."""
    fd, path = tempfile.mkstemp(prefix=".mariadb-backup-", suffix=".cnf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("[client]\n")
            f.write(f"host={_quote_cnf(cfg.db_host)}\n")
            f.write(f"port={cfg.db_port}\n")
            f.write(f"user={_quote_cnf(cfg.db_user)}\n")
            if cfg.db_password:
                f.write(f"password={_quote_cnf(cfg.db_password)}\n")
        os.chmod(path, 0o600)
        yield path
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@contextmanager
def run_lock(lock_path: Path) -> Iterator[None]:
    """Impedisce esecuzioni sovrapposte (es. cron + esecuzione manuale)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StepError(f"un'altra esecuzione è in corso (lock: {lock_path})")
        fh.write(str(os.getpid()))
        fh.flush()
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _tail(text: str, limit: int = 800) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def mysql_query(cfg: Config, cnf: str, sql: str, timeout: int = 120) -> List[List[str]]:
    cmd = [cfg.mysql_bin, f"--defaults-extra-file={cnf}", "-N", "-B", "-e", sql]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StepError(f"timeout eseguendo: {sql}")
    if res.returncode != 0:
        raise StepError(f"query fallita ({sql}): {_tail(res.stderr)}")
    return [line.split("\t") for line in res.stdout.splitlines() if line]


def check_free_space(path: Path, min_free_mb: int) -> None:
    if min_free_mb <= 0:
        return
    free_mb = shutil.disk_usage(path).free // (1024 * 1024)
    if free_mb < min_free_mb:
        raise StepError(f"spazio insufficiente in {path}: {free_mb} MB liberi (minimo {min_free_mb} MB)")
    log.debug("spazio libero in %s: %d MB", path, free_mb)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# Step 1: dump completo
# --------------------------------------------------------------------------- #
def run_full_backup(cfg: Config, cnf: str) -> Path:
    cfg.full_dir.mkdir(parents=True, exist_ok=True)
    check_free_space(cfg.full_dir, cfg.min_free_mb)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    final = cfg.full_dir / f"{cfg.db_name}_{ts}.sql.gz"
    part = final.with_name(final.name + ".part")

    cmd = [
        cfg.mysqldump_bin,
        f"--defaults-extra-file={cnf}",
        "--single-transaction",   # snapshot consistente InnoDB senza bloccare le scritture
        "--quick",
        "--routines", "--triggers", "--events",
        "--hex-blob",
        "--default-character-set=utf8mb4",
    ]
    if cfg.binlog_enabled:
        # Scrive (commentata) la posizione del binlog nel dump: è il punto
        # di partenza per il replay dei binlog nel point-in-time recovery.
        cmd.append("--master-data=2")
    cmd += cfg.dump_extra_args
    cmd += ["--databases", cfg.db_name]

    log.info("avvio dump di '%s' -> %s", cfg.db_name, final.name)
    log.debug("comando: %s", " ".join(shlex.quote(c) for c in cmd))

    tail = b""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        try:
            with open(part, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as gz:
                assert proc.stdout is not None
                for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
                    gz.write(chunk)
                    tail = (tail + chunk)[-1024:]
                gz.close()
                raw.flush()
                os.fsync(raw.fileno())
            rc = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            part.unlink(missing_ok=True)
            raise
        err.seek(0)
        stderr = err.read().decode("utf-8", "replace")

    if rc != 0:
        part.unlink(missing_ok=True)
        raise StepError(f"mysqldump terminato con codice {rc}: {_tail(stderr)}")
    if b"-- Dump completed" not in tail:
        part.unlink(missing_ok=True)
        raise StepError("dump incompleto: manca il marker '-- Dump completed' a fine file")
    if stderr.strip():
        log.warning("mysqldump stderr: %s", _tail(stderr))

    os.replace(part, final)
    _fsync_dir(final.parent)
    size_mb = final.stat().st_size / (1024 * 1024)
    log.info("dump completato: %s (%.1f MB compressi)", final.name, size_mb)
    return final


# --------------------------------------------------------------------------- #
# Step 2: rotazione dump
# --------------------------------------------------------------------------- #
def rotate_full_backups(cfg: Config) -> None:
    if not cfg.full_dir.is_dir():
        return
    # Il timestamp YYYYmmdd_HHMMSS rende l'ordinamento per nome cronologico
    backups = sorted(cfg.full_dir.glob(f"{cfg.db_name}_*.sql.gz"))
    to_delete = backups[:-cfg.max_backups]
    for old in to_delete:
        old.unlink()
        log.info("rotazione: eliminato %s", old.name)
    # File .part orfani lasciati da esecuzioni interrotte
    cutoff = dt.datetime.now().timestamp() - 86400
    for orphan in cfg.full_dir.glob("*.part"):
        if orphan.stat().st_mtime < cutoff:
            orphan.unlink()
            log.info("rimosso file parziale orfano %s", orphan.name)
    log.info("dump presenti: %d (massimo %d)", len(backups) - len(to_delete), cfg.max_backups)


# --------------------------------------------------------------------------- #
# Step 3: archiviazione binary log
# --------------------------------------------------------------------------- #
def archive_binlogs(cfg: Config, cnf: str) -> int:
    rows = mysql_query(cfg, cnf, "SELECT @@log_bin")
    if not rows or rows[0][0] not in ("1", "ON"):
        raise StepError(
            "il binary log non è attivo sul server (log_bin=OFF): "
            "il point-in-time recovery non è possibile. Vedi README."
        )

    # Chiude il binlog corrente così che tutto fino a "adesso" sia archiviabile
    mysql_query(cfg, cnf, "FLUSH BINARY LOGS")
    names = [r[0] for r in mysql_query(cfg, cnf, "SHOW BINARY LOGS")]
    if len(names) < 2:
        log.info("nessun binary log chiuso da archiviare")
        return 0
    closed = names[:-1]  # l'ultimo è quello attivo, ancora in scrittura

    cfg.binlog_dir.mkdir(parents=True, exist_ok=True)
    already = {p.name[:-3] for p in cfg.binlog_dir.rglob("*.gz") if p.is_file()}
    pending = [n for n in closed if n not in already]
    if not pending:
        log.info("binary log già tutti archiviati (%d sul server)", len(closed))
        return 0

    check_free_space(cfg.binlog_dir, cfg.min_free_mb)
    day_dir = cfg.binlog_dir / dt.date.today().isoformat()
    incoming = cfg.binlog_dir / ".incoming"
    day_dir.mkdir(exist_ok=True)
    incoming.mkdir(exist_ok=True)

    for name in pending:
        cmd = [
            cfg.mysqlbinlog_bin,
            f"--defaults-extra-file={cnf}",
            "--read-from-remote-server",
            "--raw",
            f"--result-file={incoming}/",  # prefisso: il file si chiamerà <incoming>/<name>
            name,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            raise StepError(f"timeout scaricando il binlog {name}")
        raw = incoming / name
        if res.returncode != 0 or not raw.is_file():
            raw.unlink(missing_ok=True)
            raise StepError(f"mysqlbinlog fallito su {name}: {_tail(res.stderr)}")

        dest = day_dir / f"{name}.gz"
        tmp = dest.with_name(dest.name + ".part")
        with open(raw, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp, dest)
        raw.unlink()
        log.info("binlog archiviato: %s/%s", day_dir.name, dest.name)

    _fsync_dir(day_dir)
    log.info("archiviati %d nuovi binary log", len(pending))
    return len(pending)


def cleanup_binlog_archive(cfg: Config) -> None:
    if not cfg.binlog_dir.is_dir():
        return
    cutoff = dt.date.today() - dt.timedelta(days=cfg.binlog_retention_days)
    for d in sorted(cfg.binlog_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            day = dt.date.fromisoformat(d.name)
        except ValueError:
            log.debug("ignoro directory non riconosciuta: %s", d)
            continue
        if day < cutoff:
            shutil.rmtree(d)
            log.info("retention binlog: eliminata %s (> %d giorni)", d.name, cfg.binlog_retention_days)


def purge_server_binlogs(cfg: Config, cnf: str) -> None:
    """Opzionale: libera spazio sul server. Eseguito SOLO se l'archiviazione
    di questo ciclo è riuscita, quindi nessun binlog non archiviato va perso."""
    sql = f"PURGE BINARY LOGS BEFORE NOW() - INTERVAL {cfg.server_binlog_keep_days} DAY"
    mysql_query(cfg, cnf, sql)
    log.info("server: eliminati binlog più vecchi di %d giorni", cfg.server_binlog_keep_days)


# --------------------------------------------------------------------------- #
# Job
# --------------------------------------------------------------------------- #
def run_job(cfg: Config) -> bool:
    """Esegue un ciclo completo. Non solleva mai: ritorna True/False."""
    started = dt.datetime.now()
    errors: List[str] = []

    def step(name: str, func, *args) -> bool:
        try:
            func(*args)
            return True
        except StepError as exc:
            log.error("[%s] %s", name, exc)
            errors.append(f"{name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - errore imprevisto, va loggato con traceback
            log.error("[%s] errore imprevisto: %s\n%s", name, exc, traceback.format_exc())
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
        return False

    log.info("=== inizio backup '%s' su %s:%d ===", cfg.db_name, cfg.db_host, cfg.db_port)
    try:
        cfg.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with run_lock(cfg.backup_dir / ".backup.lock"), credentials_file(cfg) as cnf:
            # Gli step sono indipendenti: se il dump fallisce si archiviano
            # comunque i binlog (e viceversa), per non perdere copertura PITR.
            dump_ok = step("dump", run_full_backup, cfg, cnf)
            if dump_ok:
                step("rotazione", rotate_full_backups, cfg)
            else:
                log.warning("rotazione saltata: il dump non è riuscito, non elimino quelli vecchi")

            if cfg.binlog_enabled:
                archived_ok = step("binlog", archive_binlogs, cfg, cnf)
                step("retention-binlog", cleanup_binlog_archive, cfg)
                if cfg.purge_server_binlogs:
                    if archived_ok:
                        step("purge-server", purge_server_binlogs, cfg, cnf)
                    else:
                        log.warning("purge dei binlog sul server saltato: archiviazione fallita")
    except StepError as exc:
        errors.append(str(exc))
        log.error("%s", exc)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")
        log.error("errore imprevisto: %s\n%s", exc, traceback.format_exc())

    elapsed = (dt.datetime.now() - started).total_seconds()
    if errors:
        notify_critical(
            f"db={cfg.db_name} host={cfg.db_host} - {len(errors)} errore/i in {elapsed:.0f}s: "
            + " | ".join(errors)
        )
        return False
    log.info("=== backup completato con successo in %.0fs ===", elapsed)
    return True


def run_check(cfg: Config) -> bool:
    try:
        with credentials_file(cfg) as cnf:
            version = mysql_query(cfg, cnf, "SELECT VERSION()")[0][0]
            log.info("connessione OK - server %s", version)
            if cfg.binlog_enabled:
                rows = mysql_query(cfg, cnf, "SELECT @@log_bin, @@binlog_format")
                log.info("log_bin=%s binlog_format=%s", rows[0][0], rows[0][1])
                if rows[0][0] not in ("1", "ON"):
                    log.error("log_bin non attivo: il PITR non funzionerà")
                    return False
            grants = mysql_query(cfg, cnf, "SHOW GRANTS")
            for g in grants:
                log.info("grant: %s", g[0])
        cfg.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        check_free_space(cfg.backup_dir, cfg.min_free_mb)
        log.info("directory di backup OK: %s", cfg.backup_dir)
        return True
    except (StepError, OSError) as exc:
        log.error("check fallito: %s", exc)
        return False


def run_daemon(cfg: Config) -> int:
    try:
        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        notify_critical("modalità --daemon richiesta ma APScheduler non è installato (pip install apscheduler)")
        return 2

    hour, minute = (int(x) for x in cfg.schedule_time.split(":"))
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_job, CronTrigger(hour=hour, minute=minute), args=[cfg],
        id="mariadb_backup", max_instances=1, coalesce=True, misfire_grace_time=3600,
    )

    def on_event(event) -> None:
        if event.code == EVENT_JOB_MISSED:
            notify_critical(f"esecuzione schedulata saltata ({event.scheduled_run_time})")
        elif event.exception:
            notify_critical(f"il job è terminato con eccezione: {event.exception}")

    scheduler.add_listener(on_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED)

    def stop(signum, _frame) -> None:
        log.info("ricevuto segnale %d, arresto scheduler", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log.info("daemon avviato: backup giornaliero alle %s", cfg.schedule_time)
    try:
        scheduler.start()
    except Exception as exc:  # noqa: BLE001
        notify_critical(f"scheduler terminato inaspettatamente: {exc}")
        return 1
    log.info("daemon arrestato")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Backup MariaDB con rotazione e archiviazione binlog (PITR).")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE,
                        help=f"file .env da caricare (default: {DEFAULT_ENV_FILE})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true", help="resta attivo e schedula con APScheduler")
    mode.add_argument("--check", action="store_true", help="verifica configurazione e connessione, poi esce")
    parser.add_argument("-v", "--verbose", action="store_true", help="log DEBUG anche su console")
    args = parser.parse_args()

    env_error: Optional[str] = None
    try:
        load_env_file(args.env_file)
    except (ConfigError, OSError) as exc:
        env_error = str(exc)

    setup_logging(Path(os.environ.get("LOG_FILE", DEFAULT_LOG_FILE)), args.verbose)

    if env_error:
        notify_critical(f"errore nel file di configurazione: {env_error}")
        return 2
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        notify_critical(f"configurazione non valida: {exc}")
        return 2

    if cfg.binlog_enabled and cfg.binlog_retention_days < cfg.max_backups:
        log.warning(
            "BINLOG_RETENTION_DAYS (%d) < MAX_BACKUPS (%d): per i dump più vecchi "
            "non ci saranno binlog sufficienti a fare point-in-time recovery",
            cfg.binlog_retention_days, cfg.max_backups,
        )

    if args.check:
        return 0 if run_check(cfg) else 1
    if args.daemon:
        return run_daemon(cfg)
    return 0 if run_job(cfg) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - rete di sicurezza finale
        try:
            notify_critical(f"crash non gestito: {exc}\n{traceback.format_exc()}")
        finally:
            sys.exit(1)
