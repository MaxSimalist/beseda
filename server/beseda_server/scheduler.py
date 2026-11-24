"""Планировщик обслуживания: автоматические резервные копии и перезагрузка сервера.

Настройки хранятся в базе (меняются во вкладке «Настройки» админки):
    auto_backup   "1"/"0"   — делать копии по расписанию (по умолчанию вкл)
    auto_day      "0".."6"  — день недели, 0 — понедельник (по умолчанию 0)
    auto_time     "ЧЧ:ММ"   — время запуска (по умолчанию 00:00)
    auto_tz       число     — смещение часового пояса от UTC (по умолчанию 12 — Камчатка)
    auto_restart  "1"/"0"   — перезагружать сервер после копии (по умолчанию вкл)
    auto_warn_h   число     — предупреждать клиентов за столько часов (0 — не предупреждать)
    auto_keep     число     — сколько последних копий хранить, старые удаляются (по умолчанию 8)

Порядок обслуживания: предупреждение за auto_warn_h часов → за 10 минут → за минуту →
резервная копия → очистка старых копий → перезагрузка (Docker поднимает сервер сам).
"""
import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

log = logging.getLogger("beseda.scheduler")

DAYS = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
DAYS_NOM = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def get_cfg(db) -> dict:
    def s(key, default):
        v = db.get_setting(key)
        return default if v is None else v
    try:
        hh, mm = str(s("auto_time", "00:00")).split(":")[:2]
        t = (int(hh) % 24, int(mm) % 60)
    except (ValueError, TypeError):
        t = (0, 0)
    try:
        return {
            "enabled": str(s("auto_backup", "1")) == "1",
            "day": int(s("auto_day", "0")) % 7,
            "time": t,
            "tz": max(-12.0, min(14.0, float(s("auto_tz", "12")))),
            "restart": str(s("auto_restart", "1")) == "1",
            "warn_h": max(0.0, min(168.0, float(s("auto_warn_h", "24")))),
            "keep": max(1, min(100, int(s("auto_keep", "8")))),
        }
    except (TypeError, ValueError):
        return {"enabled": True, "day": 0, "time": (0, 0), "tz": 12.0,
                "restart": True, "warn_h": 24.0, "keep": 8}


def now_local(tz: float) -> datetime:
    return datetime.now(timezone(timedelta(hours=tz))).replace(tzinfo=None)


def next_run(now: datetime, c: dict) -> datetime:
    """Ближайший момент обслуживания строго позже now."""
    days_ahead = (c["day"] - now.weekday()) % 7
    t = now.replace(hour=c["time"][0], minute=c["time"][1], second=0, microsecond=0) + timedelta(days=days_ahead)
    if t <= now:
        t += timedelta(days=7)
    return t


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def describe(db) -> dict:
    """Для админки: настройки + когда следующий запуск."""
    c = get_cfg(db)
    return {**{k: c[k] for k in ("enabled", "day", "tz", "restart", "warn_h", "keep")},
            "time": f"{c['time'][0]:02d}:{c['time'][1]:02d}",
            "day_name": DAYS_NOM[c["day"]],
            "next": fmt_dt(next_run(now_local(c["tz"]), c)) if c["enabled"] else ""}


async def scheduler_loop(hub, cfg):
    """Тикает раз в 20 секунд: рассылает предупреждения и запускает обслуживание."""
    await asyncio.sleep(15)                       # дать серверу подняться
    fired: set[str] = set()                       # уже сработавшие события (по слоту времени)
    while True:
        try:
            c = get_cfg(hub.db)
            if c["enabled"]:
                now = now_local(c["tz"])
                run_at = next_run(now, c)
                # run_at всегда в будущем; слот — ближайшее прошедшее/текущее время запуска
                prev_at = run_at - timedelta(days=7)
                slot = prev_at.strftime("%Y%m%d%H%M")
                left = (run_at - now).total_seconds()
                since = (now - prev_at).total_seconds()
                if c["restart"]:
                    for warn_left, key, text in (
                        (c["warn_h"] * 3600, "warn",
                         f"Плановое обслуживание: {fmt_dt(run_at)} (в {DAYS[c['day']]}) будет создана "
                         f"резервная копия, после чего сервер перезагрузится примерно на минуту."),
                        (600, "warn10", f"Сервер перезагрузится через 10 минут — плановое обслуживание."),
                        (60, "warn1", "Сервер перезагрузится через минуту!"),
                    ):
                        if 0 < warn_left and left <= warn_left and f"{run_at:%Y%m%d%H%M}:{key}" not in fired:
                            fired.add(f"{run_at:%Y%m%d%H%M}:{key}")
                            hub.broadcast_all({"type": "system_notice", "text": text, "from": "Планировщик"})
                            log.info("планировщик: предупреждение: %s", text)
                if 0 <= since <= 40 and slot not in fired:      # окно запуска — 40 секунд
                    fired.add(slot)
                    await maintenance(hub, cfg, c)
                if len(fired) > 64:                              # не копим метки вечно
                    fired = set(sorted(fired)[-32:])
        except Exception:
            log.exception("планировщик обслуживания")
        await asyncio.sleep(20)


async def maintenance(hub, cfg, c: dict):
    """Резервная копия → очистка старых → перезагрузка (если включена)."""
    from starlette.concurrency import run_in_threadpool

    from .manage import backup as make_backup
    log.info("планировщик: создаю резервную копию")
    try:
        path = await run_in_threadpool(make_backup, cfg)
        log.info("планировщик: копия создана: %s", path)
    except Exception:
        log.exception("планировщик: не удалось создать копию")
        hub.broadcast_all({"type": "system_notice", "from": "Планировщик",
                           "text": "Плановое обслуживание: создать резервную копию не удалось, "
                                   "перезагрузка отменена. Подробности — в журнале сервера."})
        return
    try:                                              # храним только последние keep копий
        files = sorted((cfg.data_dir / "backups").glob("beseda-*.tar.gz"))
        for p in files[:-c["keep"]]:
            p.unlink(missing_ok=True)
            log.info("планировщик: удалена старая копия %s", p.name)
    except Exception:
        log.exception("планировщик: очистка старых копий")
    if c["restart"]:
        hub.broadcast_all({"type": "system_notice", "from": "Планировщик",
                           "text": "Сервер перезагружается — вернёмся примерно через минуту."})
        await asyncio.sleep(2)
        os.kill(os.getpid(), signal.SIGTERM)          # Docker (restart: unless-stopped) поднимет снова
