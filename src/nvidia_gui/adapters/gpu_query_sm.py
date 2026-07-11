"""nvidia-smi adapter — live GPU telemetry."""

from __future__ import annotations

import logging
import shutil
import subprocess

from ..application.ports import GpuQueryPort
from ..domain.models import GpuSnapshot

logger = logging.getLogger(__name__)

# the columns we pull, in the order returned. ``--format=csv,noheader,nounits``
# gives clean tab-separated-ish values split by ", ".
_QUERY = (
    "name,driver_version,temperature.gpu,utilization.gpu,utilization.memory,"
    "power.draw,power.limit,clocks.gr,clocks.mem,memory.used,memory.total,"
    "fan.speed"
).split(",")


class NvidiaSmiGpu(GpuQueryPort):
    """Synchronous nvidia-smi poll. Call off the GTK main loop.

    tolerant of any missing column (e.g. laptops report no fan) — missing
    fields become "" rather than crashing the UI.
    """

    def __init__(self) -> None:
        self._bin = shutil.which("nvidia-smi") or "nvidia-smi"

    def snapshot(self) -> GpuSnapshot:
        fields = self._run()
        if not fields:
            return GpuSnapshot()
        # pad/truncate to the keys we expect
        keys = [
            "gpu_name", "driver_version", "temperature_c", "gpu_util_pct",
            "mem_util_pct", "power_draw_w", "power_limit_w", "gr_clock_mhz",
            "mem_clock_mhz", "mem_used_mb", "mem_total_mb", "fan_pct",
        ]
        vals = (fields + [""] * len(keys))[: len(keys)]
        # nvidia-smi reports "[N/A]" for missing numeric fields; normalise to ""
        vals = ["" if v.strip() in ("[N/A]", "") else v.strip() for v in vals]
        return GpuSnapshot(**dict(zip(keys, vals)))  # type: ignore[arg-type]

    def _run(self) -> list[str]:
        fmt = ",".join(_QUERY)
        try:
            out = subprocess.run(
                [self._bin, f"--query-gpu={fmt}", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("nvidia-smi unavailable: %s", exc)
            return []
        line = out.stdout.strip().splitlines()
        if not line:
            logger.debug("nvidia-smi empty stdout: %s", out.stderr[:120])
            return []
        # csv,noheader splits on ", "
        return [c.strip() for c in line[0].split(",")]
