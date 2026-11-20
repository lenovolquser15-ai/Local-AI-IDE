"""LAN Mesh & Compute Cluster: offload compute tasks to Wi-Fi devices (phones/browsers)
and discover/route LLM inference across LAN PCs (Ollama / LM Studio / llama.cpp).
"""
import collections
import concurrent.futures
import hashlib
import ipaddress
import json
import math
import socket
import threading
import time
import urllib.request
import uuid

_LOCK = threading.RLock()
_QUEUE_COND = threading.Condition(_LOCK)

# Browser / phone workers: worker_id -> dict
_WORKERS = {}
_WORKER_TTL = 18.0  # seconds without poll/heartbeat before expiring

# Pending & in-flight tasks
_PENDING = collections.deque()  # list of task dicts waiting for a worker
_TASKS = {}  # task_id -> task dict (includes Event for waiter)
_TOTAL_TASKS_DONE = 0

# Discovered or manually added LAN LLM nodes: url -> dict
_LLM_NODES = {}

SAFE_TASK_KINDS = ("compute_bench", "text_search", "hash_batch", "json_transform")


def _prune_workers_locked(now=None):
    now = now or time.time()
    dead = [wid for wid, w in _WORKERS.items() if now - w.get("last_seen", 0) > _WORKER_TTL]
    for wid in dead:
        _WORKERS.pop(wid, None)
        # Re-queue any in-flight tasks assigned to a dropped worker
        for task in _TASKS.values():
            if task.get("worker_id") == wid and not task["done"].is_set():
                task["worker_id"] = None
                _PENDING.appendleft(task)
                _QUEUE_COND.notify_all()


def register_worker(info, client_ip="127.0.0.1"):
    """Register or update a browser/mobile compute worker."""
    info = info or {}
    wid = str(info.get("id") or "").strip() or uuid.uuid4().hex[:12]
    now = time.time()
    with _LOCK:
        prev = _WORKERS.get(wid) or {}
        cores = max(1, min(64, int(info.get("cores") or prev.get("cores") or 2)))
        max_threads = max(1, min(cores, int(info.get("max_threads") or prev.get("max_threads") or 1)))
        worker = {
            "id": wid,
            "name": str(info.get("name") or prev.get("name") or ("Устройство " + wid[:4]))[:60],
            "device_type": str(info.get("device_type") or prev.get("device_type") or "browser")[:24],
            "ip": str(client_ip or prev.get("ip") or ""),
            "cores": cores,
            "max_threads": max_threads,
            "memory_gb": info.get("memory_gb", prev.get("memory_gb")),
            "battery_pct": info.get("battery_pct", prev.get("battery_pct")),
            "charging": info.get("charging", prev.get("charging")),
            "webgpu": bool(info.get("webgpu", prev.get("webgpu", False))),
            "paused": bool(info.get("paused", prev.get("paused", False))),
            "last_seen": now,
            "connected_at": prev.get("connected_at", now),
            "tasks_done": prev.get("tasks_done", 0),
            "total_ms": prev.get("total_ms", 0.0),
            "avg_ms": prev.get("avg_ms", 0.0),
            "in_flight": prev.get("in_flight", 0),
        }
        _WORKERS[wid] = worker
        _QUEUE_COND.notify_all()
        return dict(worker)


def heartbeat_worker(worker_id, stats=None):
    """Update worker liveness and telemetry (battery, pause state, threads)."""
    stats = stats or {}
    now = time.time()
    with _LOCK:
        w = _WORKERS.get(worker_id)
        if not w:
            return None
        w["last_seen"] = now
        if "battery_pct" in stats:
            w["battery_pct"] = stats["battery_pct"]
        if "charging" in stats:
            w["charging"] = stats["charging"]
        if "paused" in stats:
            w["paused"] = bool(stats["paused"])
        if "max_threads" in stats and stats["max_threads"]:
            w["max_threads"] = max(1, min(w["cores"], int(stats["max_threads"])))
        if "name" in stats and stats["name"]:
            w["name"] = str(stats["name"])[:60]
        return dict(w)


def unregister_worker(worker_id):
    """Remove a worker and re-queue its unfinished tasks."""
    with _LOCK:
        removed = _WORKERS.pop(worker_id, None)
        for task in _TASKS.values():
            if task.get("worker_id") == worker_id and not task["done"].is_set():
                task["worker_id"] = None
                _PENDING.appendleft(task)
        _QUEUE_COND.notify_all()
        return {"ok": True, "removed": bool(removed)}


def list_workers():
    """Return active workers."""
    with _LOCK:
        _prune_workers_locked()
        out = []
        for w in _WORKERS.values():
            item = dict(w)
            item["online"] = True
            out.append(item)
        out.sort(key=lambda x: x.get("connected_at", 0))
        return out


def active_worker_count():
    with _LOCK:
        _prune_workers_locked()
        return sum(1 for w in _WORKERS.values() if not w.get("paused"))


def poll_task(worker_id, wait=2.0):
    """Long-poll for the next queued task for *worker_id*."""
    wait = max(0.0, min(5.0, float(wait)))
    deadline = time.time() + wait
    with _QUEUE_COND:
        w = _WORKERS.get(worker_id)
        if not w:
            return {"ok": False, "error": "worker not registered", "task": None}
        w["last_seen"] = time.time()
        if w.get("paused"):
            return {"ok": True, "task": None, "paused": True}

        while True:
            w = _WORKERS.get(worker_id)
            if not w or w.get("paused"):
                return {"ok": True, "task": None}
            w["last_seen"] = time.time()

            if _PENDING and w.get("in_flight", 0) < w.get("max_threads", 1):
                task = _PENDING.popleft()
                if task["done"].is_set():
                    continue
                task["worker_id"] = worker_id
                task["worker_name"] = w.get("name", worker_id)
                task["picked_at"] = time.time()
                w["in_flight"] = w.get("in_flight", 0) + 1
                return {
                    "ok": True,
                    "task": {
                        "id": task["id"],
                        "kind": task["kind"],
                        "payload": task["payload"],
                    },
                }

            remaining = deadline - time.time()
            if remaining <= 0:
                return {"ok": True, "task": None}
            _QUEUE_COND.wait(timeout=min(remaining, 0.5))


def submit_result(task_id, worker_id, ok=True, result=None, error=None, duration_ms=None):
    """Receive computed result from a browser/mobile worker."""
    global _TOTAL_TASKS_DONE
    with _LOCK:
        task = _TASKS.get(task_id)
        w = _WORKERS.get(worker_id)
        if w:
            w["last_seen"] = time.time()
            w["in_flight"] = max(0, w.get("in_flight", 1) - 1)
            if ok:
                w["tasks_done"] = w.get("tasks_done", 0) + 1
                ms = float(duration_ms) if duration_ms is not None else (
                    (time.time() - task.get("picked_at", time.time())) * 1000.0 if task else 0.0
                )
                w["total_ms"] = w.get("total_ms", 0.0) + ms
                w["avg_ms"] = round(w["total_ms"] / max(1, w["tasks_done"]), 1)

        if not task:
            return {"ok": False, "error": "unknown task"}
        if task["done"].is_set():
            return {"ok": True, "duplicate": True}

        task["ok"] = bool(ok)
        task["result"] = result
        task["error"] = str(error) if error else None
        task["duration_ms"] = round(float(duration_ms), 1) if duration_ms is not None else round(
            (time.time() - task.get("picked_at", task["created_at"])) * 1000.0, 1
        )
        task["worker_name"] = (w or {}).get("name") or task.get("worker_name") or worker_id
        if ok:
            _TOTAL_TASKS_DONE += 1
        task["done"].set()
        _QUEUE_COND.notify_all()
        return {"ok": True}


def _execute_locally(kind, payload):
    """Safe local fallback when no remote workers are connected."""
    t0 = time.perf_counter()
    if kind == "compute_bench":
        iters = max(1000, min(500000, int((payload or {}).get("iterations", 40000))))
        seed = int((payload or {}).get("seed", 1))
        acc = 0.0
        for i in range(1, iters + 1):
            acc += math.sin(i * 0.001 + seed) * math.cos(i * 0.002)
        return {"checksum": round(acc, 6), "iterations": iters, "seed": seed}, round((time.perf_counter() - t0) * 1000.0, 1)

    if kind == "hash_batch":
        items = (payload or {}).get("items") or []
        out = [hashlib.sha256(str(x).encode("utf-8", "replace")).hexdigest() for x in items]
        return {"hashes": out, "count": len(out)}, round((time.perf_counter() - t0) * 1000.0, 1)

    if kind == "text_search":
        query = str((payload or {}).get("query") or "").lower().split()
        docs = (payload or {}).get("docs") or []
        scored = []
        for idx, doc in enumerate(docs):
            text = str(doc.get("text") if isinstance(doc, dict) else doc)
            low = text.lower()
            score = sum(low.count(term) for term in query if term)
            if score > 0:
                scored.append({
                    "id": doc.get("id", idx) if isinstance(doc, dict) else idx,
                    "score": score,
                    "preview": text[:160],
                })
        scored.sort(key=lambda r: r["score"], reverse=True)
        return {"matches": scored[:10]}, round((time.perf_counter() - t0) * 1000.0, 1)

    if kind == "json_transform":
        rows = (payload or {}).get("rows") or []
        field = (payload or {}).get("field")
        op = (payload or {}).get("op", "count")
        vals = []
        for r in rows:
            if isinstance(r, dict) and field in r:
                try:
                    vals.append(float(r[field]))
                except (TypeError, ValueError):
                    pass
        if op == "sum":
            res = {"sum": round(sum(vals), 4), "count": len(vals)}
        elif op == "avg":
            res = {"avg": round(sum(vals) / len(vals), 4) if vals else 0.0, "count": len(vals)}
        else:
            res = {"count": len(rows)}
        return res, round((time.perf_counter() - t0) * 1000.0, 1)

    return {"error": "unsupported kind: %s" % kind}, round((time.perf_counter() - t0) * 1000.0, 1)


def submit_task(kind, payload, timeout=30.0, allow_local_fallback=True):
    """Submit a single task to the LAN worker pool (or run locally if no workers)."""
    if kind not in SAFE_TASK_KINDS:
        return {"ok": False, "error": "неизвестный тип задачи: %s (допустимо: %s)" % (kind, ", ".join(SAFE_TASK_KINDS))}

    if active_worker_count() == 0:
        if allow_local_fallback:
            res, ms = _execute_locally(kind, payload)
            return {"ok": True, "result": res, "worker": "Локальный ПК (fallback)", "duration_ms": ms, "local": True}
        return {"ok": False, "error": "нет подключённых устройств-воркеров"}

    tid = uuid.uuid4().hex[:12]
    task = {
        "id": tid,
        "kind": kind,
        "payload": payload,
        "created_at": time.time(),
        "worker_id": None,
        "worker_name": None,
        "done": threading.Event(),
        "ok": False,
        "result": None,
        "error": None,
        "duration_ms": 0.0,
    }
    with _QUEUE_COND:
        _TASKS[tid] = task
        _PENDING.append(task)
        _QUEUE_COND.notify_all()

    finished = task["done"].wait(timeout=float(timeout))
    with _LOCK:
        _TASKS.pop(tid, None)
        if not finished:
            try:
                _PENDING.remove(task)
            except ValueError:
                pass
            if allow_local_fallback:
                res, ms = _execute_locally(kind, payload)
                return {"ok": True, "result": res, "worker": "Локальный ПК (timeout fallback)", "duration_ms": ms, "local": True}
            return {"ok": False, "error": "таймаут ожидания ответа от воркера"}

    return {
        "ok": task["ok"],
        "result": task["result"],
        "error": task["error"],
        "worker": task["worker_name"] or task["worker_id"],
        "duration_ms": task["duration_ms"],
        "local": False,
    }


def map_tasks(kind, items, timeout=60.0, allow_local_fallback=True):
    """Distribute a list of payloads across all connected Wi-Fi workers in parallel."""
    if kind not in SAFE_TASK_KINDS:
        return {"ok": False, "error": "неизвестный тип задачи: %s" % kind}
    items = list(items or [])
    if not items:
        return {"ok": True, "results": [], "per_worker": {}, "elapsed_ms": 0.0, "workers_used": 0}

    t0 = time.perf_counter()
    active = active_worker_count()
    if active == 0:
        if not allow_local_fallback:
            return {"ok": False, "error": "нет подключённых устройств в Wi-Fi кластере"}
        results = []
        for payload in items:
            res, ms = _execute_locally(kind, payload)
            results.append({"ok": True, "result": res, "worker": "Локальный ПК", "duration_ms": ms, "local": True})
        elapsed = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "ok": True,
            "results": results,
            "per_worker": {"Локальный ПК": len(items)},
            "elapsed_ms": elapsed,
            "workers_used": 0,
            "executed_locally": True,
        }

    # Enqueue all tasks simultaneously so workers pick them up concurrently
    tasks = []
    with _QUEUE_COND:
        for payload in items:
            tid = uuid.uuid4().hex[:12]
            task = {
                "id": tid,
                "kind": kind,
                "payload": payload,
                "created_at": time.time(),
                "worker_id": None,
                "worker_name": None,
                "done": threading.Event(),
                "ok": False,
                "result": None,
                "error": None,
                "duration_ms": 0.0,
            }
            _TASKS[tid] = task
            _PENDING.append(task)
            tasks.append(task)
        _QUEUE_COND.notify_all()

    deadline = time.time() + float(timeout)
    for task in tasks:
        rem = max(0.0, deadline - time.time())
        task["done"].wait(timeout=rem)

    results = []
    per_worker = {}
    with _LOCK:
        for task in tasks:
            _TASKS.pop(task["id"], None)
            if not task["done"].is_set():
                try:
                    _PENDING.remove(task)
                except ValueError:
                    pass
                if allow_local_fallback:
                    res, ms = _execute_locally(kind, task["payload"])
                    wname = "Локальный ПК (fallback)"
                    results.append({"ok": True, "result": res, "worker": wname, "duration_ms": ms, "local": True})
                    per_worker[wname] = per_worker.get(wname, 0) + 1
                else:
                    results.append({"ok": False, "error": "timeout"})
            else:
                wname = task["worker_name"] or task["worker_id"] or "worker"
                results.append({
                    "ok": task["ok"],
                    "result": task["result"],
                    "error": task["error"],
                    "worker": wname,
                    "duration_ms": task["duration_ms"],
                    "local": False,
                })
                per_worker[wname] = per_worker.get(wname, 0) + 1

    elapsed = round((time.perf_counter() - t0) * 1000.0, 1)
    remote_workers = [k for k in per_worker if not k.startswith("Локальный ПК")]
    return {
        "ok": all(r.get("ok") for r in results),
        "results": results,
        "per_worker": per_worker,
        "elapsed_ms": elapsed,
        "workers_used": len(remote_workers),
        "executed_locally": len(remote_workers) == 0,
    }


def run_cluster_benchmark(chunks=12, iterations=45000, timeout=45.0):
    """Run a visual distributed compute benchmark across connected Wi-Fi devices."""
    chunks = max(2, min(48, int(chunks)))
    iterations = max(5000, min(250000, int(iterations)))
    items = [{"iterations": iterations, "seed": i + 1} for i in range(chunks)]
    res = map_tasks("compute_bench", items, timeout=timeout, allow_local_fallback=True)
    res["chunks"] = chunks
    res["iterations_per_chunk"] = iterations
    return res


# ---------- LAN LLM Nodes (Ollama / LM Studio / llama.cpp on other PCs) ----------

def _normalize_llm_url(url):
    u = str(url or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith("http://") and not u.startswith("https://"):
        u = "http://" + u
    if not u.endswith("/v1"):
        u = u + "/v1"
    return u


def probe_llm_node(base_url, timeout=1.5):
    """Probe an OpenAI-compatible LLM server on the LAN."""
    url = _normalize_llm_url(base_url)
    if not url:
        return {"ok": False, "error": "пустой URL"}
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url + "/models", headers={"User-Agent": "LocalHarness-Cluster"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            raw_models = data.get("data") or []
            models = [m.get("id") for m in raw_models if isinstance(m, dict) and m.get("id")]
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            return {
                "ok": True,
                "url": url,
                "models": models,
                "latency_ms": latency_ms,
                "alive": True,
                "checked_at": time.time(),
            }
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "models": [],
            "alive": False,
            "error": str(exc)[:160],
            "checked_at": time.time(),
        }


def add_llm_node(url, label="", enabled=True):
    """Add or update a LAN LLM node."""
    norm = _normalize_llm_url(url)
    if not norm:
        return {"ok": False, "error": "укажите адрес узла (например 192.168.1.50:11434)"}
    probe = probe_llm_node(norm, timeout=2.0)
    node = {
        "url": norm,
        "label": str(label or norm.replace("http://", "").replace("/v1", ""))[:60],
        "enabled": bool(enabled),
        "alive": probe.get("alive", False),
        "models": probe.get("models", []),
        "latency_ms": probe.get("latency_ms"),
        "error": probe.get("error", ""),
        "checked_at": probe.get("checked_at", time.time()),
    }
    with _LOCK:
        _LLM_NODES[norm] = node
    return {"ok": True, "node": dict(node)}


def remove_llm_node(url):
    norm = _normalize_llm_url(url)
    with _LOCK:
        removed = _LLM_NODES.pop(norm, None) or _LLM_NODES.pop(str(url or "").strip(), None)
    return {"ok": True, "removed": bool(removed)}


def load_nodes_from_config(cfg):
    """Ensure persisted cluster_llm_nodes from config are registered."""
    nodes = (cfg or {}).get("cluster_llm_nodes") or []
    with _LOCK:
        for item in nodes:
            if isinstance(item, dict) and item.get("url"):
                norm = _normalize_llm_url(item["url"])
                if norm and norm not in _LLM_NODES:
                    _LLM_NODES[norm] = {
                        "url": norm,
                        "label": item.get("label") or norm.replace("http://", "").replace("/v1", ""),
                        "enabled": bool(item.get("enabled", True)),
                        "alive": False,
                        "models": item.get("models") or [],
                        "latency_ms": None,
                        "error": "",
                        "checked_at": 0.0,
                    }


def export_nodes_for_config():
    with _LOCK:
        return [
            {"url": n["url"], "label": n["label"], "enabled": n.get("enabled", True)}
            for n in _LLM_NODES.values()
        ]


def list_llm_nodes(cfg=None, refresh=False):
    if cfg:
        load_nodes_from_config(cfg)
    with _LOCK:
        urls = list(_LLM_NODES.keys())
    if refresh and urls:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(urls))) as ex:
            futs = {ex.submit(probe_llm_node, u, 1.5): u for u in urls}
            for fut in concurrent.futures.as_completed(futs):
                u = futs[fut]
                res = fut.result()
                with _LOCK:
                    if u in _LLM_NODES:
                        _LLM_NODES[u]["alive"] = res.get("alive", False)
                        _LLM_NODES[u]["models"] = res.get("models", [])
                        _LLM_NODES[u]["latency_ms"] = res.get("latency_ms")
                        _LLM_NODES[u]["error"] = res.get("error", "")
                        _LLM_NODES[u]["checked_at"] = res.get("checked_at", time.time())
    with _LOCK:
        return [dict(n) for n in _LLM_NODES.values()]


def _check_tcp_and_probe(ip, port, timeout=0.35):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if sock.connect_ex((ip, port)) != 0:
            return None
    except OSError:
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    probe = probe_llm_node("http://%s:%d/v1" % (ip, port), timeout=1.2)
    if probe.get("ok"):
        kind = {11434: "Ollama", 1234: "LM Studio", 8080: "llama.cpp"}.get(port, "LLM")
        probe["label"] = "%s (%s:%d)" % (kind, ip, port)
        return probe
    return None


def scan_lan_llm_nodes(lan_ip, ports=(11434, 1234, 8080), timeout=0.35):
    """Scan the local /24 Wi-Fi subnet for Ollama / LM Studio / llama.cpp instances."""
    try:
        addr = ipaddress.ip_address(str(lan_ip or "").strip())
        if not addr.is_private or addr.is_loopback or addr.version != 4:
            return {"ok": False, "error": "не удалось определить приватную Wi-Fi подсеть (%s)" % lan_ip, "found": []}
        net = ipaddress.ip_network("%s/24" % addr, strict=False)
    except ValueError:
        return {"ok": False, "error": "некорректный LAN IP: %s" % lan_ip, "found": []}

    my_ip = str(addr)
    targets = [(str(host), p) for host in net.hosts() if str(host) != my_ip for p in ports]
    found = []
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as ex:
        futs = [ex.submit(_check_tcp_and_probe, ip, port, timeout) for ip, port in targets]
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            if res:
                found.append(res)
                with _LOCK:
                    _LLM_NODES[res["url"]] = {
                        "url": res["url"],
                        "label": res["label"],
                        "enabled": True,
                        "alive": True,
                        "models": res.get("models", []),
                        "latency_ms": res.get("latency_ms"),
                        "error": "",
                        "checked_at": res.get("checked_at", time.time()),
                    }
    return {
        "ok": True,
        "subnet": str(net),
        "found": found,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
    }


def pick_llm_node(cfg, thermal_status=None):
    """Return a remote LAN LLM node (url, model, label) if thermal offload should route there."""
    cfg = cfg or {}
    if not cfg.get("cluster_enabled", True) or not cfg.get("cluster_offload_on_hot", True):
        return None
    if thermal_status and thermal_status.get("status") not in ("hot", "critical"):
        return None
    load_nodes_from_config(cfg)
    with _LOCK:
        candidates = [
            dict(n) for n in _LLM_NODES.values()
            if n.get("enabled", True) and n.get("alive") and n.get("models")
        ]
    if not candidates:
        return None
    candidates.sort(key=lambda n: n.get("latency_ms") or 9999.0)
    chosen = candidates[0]
    return {
        "url": chosen["url"],
        "model": chosen["models"][0],
        "label": chosen["label"],
        "latency_ms": chosen.get("latency_ms"),
    }


def query_remote_llm(prompt, node_url=None, model=None, timeout=60.0):
    """Send a chat completion prompt directly to a LAN LLM node."""
    with _LOCK:
        if node_url:
            norm = _normalize_llm_url(node_url)
            node = _LLM_NODES.get(norm) or {"url": norm, "models": [model] if model else [], "label": norm}
        else:
            alive = [n for n in _LLM_NODES.values() if n.get("enabled", True) and n.get("alive") and n.get("models")]
            if not alive:
                return {"ok": False, "error": "нет активных LLM-узлов в Wi-Fi сети"}
            alive.sort(key=lambda n: n.get("latency_ms") or 9999.0)
            node = dict(alive[0])

    target_model = model or (node.get("models") or [None])[0]
    if not target_model:
        return {"ok": False, "error": "у узла %s не найдены модели" % node["url"]}

    body = json.dumps({
        "model": target_model,
        "messages": [{"role": "user", "content": str(prompt)}],
        "temperature": 0.4,
        "max_tokens": 1024,
    }).encode("utf-8")
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(
            node["url"] + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "LocalHarness-Cluster"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            return {
                "ok": True,
                "node": node.get("label") or node["url"],
                "model": target_model,
                "text": text,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }
    except Exception as exc:
        return {"ok": False, "node": node["url"], "error": str(exc)}


def status(cfg=None, lan_ip="", port=7788):
    """Full cluster snapshot for the UI and tools."""
    workers = list_workers()
    llm_nodes = list_llm_nodes(cfg, refresh=False)
    total_threads = sum(w.get("max_threads", 1) for w in workers if not w.get("paused"))
    worker_url = "http://%s:%d/worker" % (lan_ip or "127.0.0.1", int(port or 7788))
    return {
        "ok": True,
        "enabled": bool((cfg or {}).get("cluster_enabled", True)),
        "offload_on_hot": bool((cfg or {}).get("cluster_offload_on_hot", True)),
        "lan_access": bool((cfg or {}).get("lan_access", False)),
        "worker_url": worker_url,
        "workers": workers,
        "workers_online": len(workers),
        "total_worker_threads": total_threads,
        "total_tasks_done": _TOTAL_TASKS_DONE,
        "pending_tasks": len(_PENDING),
        "llm_nodes": llm_nodes,
        "llm_nodes_alive": sum(1 for n in llm_nodes if n.get("alive")),
    }


def _reset():
    """Reset in-memory state (for unit tests)."""
    global _TOTAL_TASKS_DONE
    with _LOCK:
        _WORKERS.clear()
        _PENDING.clear()
        for t in _TASKS.values():
            t["done"].set()
        _TASKS.clear()
        _LLM_NODES.clear()
        _TOTAL_TASKS_DONE = 0
