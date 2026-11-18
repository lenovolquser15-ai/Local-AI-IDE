"""System Hardware and Diagnostic Tools Plugin for LocalHarness.

Inspects CPU, RAM, GPU, storage, and top memory-consuming processes.
"""
import os
import psutil

NAME = "system_stats"


def _sys_info(cfg, args):
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.abspath(os.sep))
    cpu_pct = psutil.cpu_percent(interval=0.2)
    cores = psutil.cpu_count(logical=True)

    gpu_info = "не обнаружено"
    try:
        import comtypes.client
        wmi = comtypes.client.CoGetObject("winmgmts:")
        gpus = [g.Name for g in wmi.ExecQuery("SELECT Name FROM Win32_VideoController")]
        if gpus:
            gpu_info = ", ".join(gpus)
    except Exception:
        pass

    return {
        "ok": True,
        "cpu_usage_pct": cpu_pct,
        "cpu_cores": cores,
        "ram_total_gb": round(vm.total / (1024 ** 3), 1),
        "ram_free_gb": round(vm.available / (1024 ** 3), 1),
        "ram_used_pct": vm.percent,
        "disk_free_gb": round(disk.free / (1024 ** 3), 1),
        "disk_total_gb": round(disk.total / (1024 ** 3), 1),
        "gpu": gpu_info,
    }


def _sys_top_procs(cfg, args):
    count = min(max(int(args.get("count") or 5), 1), 20)
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            mem_mb = round((p.info["memory_info"].rss or 0) / (1024 * 1024), 1)
            procs.append({
                "pid": p.info["pid"],
                "name": p.info["name"],
                "memory_mb": mem_mb,
            })
        except Exception:
            continue

    procs.sort(key=lambda x: x["memory_mb"], reverse=True)
    return {"ok": True, "top_memory_processes": procs[:count]}


TOOLS = [
    {
        "name": "sys_info",
        "desc": "Сводка о нагрузке системы: процессор, свободная RAM, диск и видеокарта",
        "params": {},
        "required": [],
        "fn": _sys_info,
    },
    {
        "name": "sys_top_procs",
        "desc": "Список самых прожорливых процессов по оперативной памяти",
        "params": {
            "count": ("number?", "количество процессов в выдаче (по умолчанию 5)"),
        },
        "required": [],
        "fn": _sys_top_procs,
    },
]
