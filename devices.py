"""Read every supported 2.4G receiver that is currently plugged in."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait

import atk_keyboard_battery
import atk_mouse_battery
import aula_battery
import delux_battery
import inphic_battery
from delux_battery import Reading

READERS = (delux_battery, aula_battery, inphic_battery, atk_mouse_battery, atk_keyboard_battery)


def read_devices(debug=None) -> list[Reading]:
    readings: list[Reading] = []
    pool = ThreadPoolExecutor(max_workers=len(READERS))
    jobs = [pool.submit(module.read_batteries, debug) for module in READERS]
    done, _pending = wait(jobs, timeout=1.5)
    pool.shutdown(wait=False, cancel_futures=True)
    for job in done:
        try:
            readings.extend(job.result())
        except Exception:
            continue
    return readings


def connected(readings: list[Reading]) -> list[Reading]:
    """Keep devices that actually answered with a battery.

    A plugged-in dongle still enumerates when the mouse or keyboard is off,
    asleep, or out of range. Those reads stay in ``read_devices`` for logs,
    and stay off the home page.
    """
    return [item for item in readings if item.ok and item.level is not None]
