"""Deploy the LocalHarness relay Worker and the connect site to Cloudflare.

Uses the portable Node.js in data/bin/nodejs and a local wrangler install, so the
system Node.js does not have to work. Needs a Cloudflare account: the first run
opens a browser for `wrangler login`.
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
DATA = os.path.join(ROOT, "data")
NODE_DIR = os.path.join(DATA, "bin", "nodejs")
NODE = os.path.join(NODE_DIR, "node.exe" if os.name == "nt" else "node")
NPM_CLI = os.path.join(NODE_DIR, "node_modules", "npm", "bin", "npm-cli.js")
WRANGLER_HOME = os.path.join(DATA, "bin", "wrangler")
WRANGLER = os.path.join(WRANGLER_HOME, "node_modules", "wrangler", "bin", "wrangler.js")
WORKER_DIR = os.path.join(ROOT, "cloudflare", "worker")
SITE_DIR = os.path.join(ROOT, "cloudflare", "site")
CONFIG = os.path.join(ROOT, "config.json")


def env():
    result = dict(os.environ)
    result["PATH"] = NODE_DIR + os.pathsep + result.get("PATH", "")
    result.pop("CLOUDFLARE_API_TOKEN", None)
    return result


def run(args, cwd=None, stdin=None, check=True, capture=True):
    print("+", " ".join(args), flush=True)
    proc = subprocess.run(args, cwd=cwd, env=env(), input=stdin, text=True,
                          capture_output=capture, encoding="utf-8", errors="replace")
    if proc.stdout:
        print(proc.stdout.rstrip(), flush=True)
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr, flush=True)
    if check and proc.returncode != 0:
        raise SystemExit("команда завершилась с кодом %s" % proc.returncode)
    return proc


def node(args, **kw):
    return run([NODE] + args, **kw)


def ensure_node():
    if not os.path.isfile(NODE):
        raise SystemExit("нет портативного Node.js: %s\n"
                         "Он уже нужен только для деплоя; скачай node-v*-win-x64.zip в data/bin/nodejs" % NODE)


def ensure_wrangler():
    if os.path.isfile(WRANGLER):
        return
    print("Устанавливаю wrangler локально (один раз)…", flush=True)
    os.makedirs(WRANGLER_HOME, exist_ok=True)
    node([NPM_CLI, "install", "--prefix", WRANGLER_HOME, "--no-audit", "--no-fund", "wrangler@4"])


def wrangler(*args, **kw):
    cmd = [NODE, WRANGLER] + list(args)
    return run(cmd, **kw)


def whoami():
    proc = wrangler("whoami", check=False)
    text = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0 and "not authenticated" not in text.lower(), text


def secret_from_config():
    try:
        with open(CONFIG, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    remote = cfg.get("remote") or {}
    value = str(remote.get("secret") or "")
    if not value:
        import secrets
        value = secrets.token_hex(32)
    return cfg, remote, value


def save_config(cfg, remote, secret, worker_url, site_url):
    remote.update({"secret": secret, "worker_url": worker_url, "site_url": site_url})
    cfg["remote"] = remote
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)


def write_worker_toml(worker_name):
    text = (
        'name = "%s"\n'
        'main = "worker.js"\n'
        'compatibility_date = "2026-11-15"\n\n'
        '[assets]\n'
        'directory = "../site"\n'
        'binding = "ASSETS"\n'
        'run_worker_first = true\n\n'
        '[[durable_objects.bindings]]\n'
        'name = "ROOMS"\n'
        'class_name = "Rooms"\n\n'
        '[[migrations]]\n'
        'tag = "v1"\n'
        'new_sqlite_classes = ["Rooms"]\n'
    ) % worker_name
    with open(os.path.join(WORKER_DIR, "wrangler.toml"), "w", encoding="utf-8") as fh:
        fh.write(text)


ACCOUNT_ID_RE = re.compile(r"\b([0-9a-f]{32})\b")
WRANGLER_CFG = os.path.join(os.environ.get("APPDATA", ""), "xdg.config", ".wrangler", "config", "default.toml")


def oauth_token():
    try:
        with open(WRANGLER_CFG, "r", encoding="utf-8") as fh:
            match = re.search(r'^oauth_token\s*=\s*"([^"]+)"', fh.read(), re.M)
        return match.group(1) if match else ""
    except OSError:
        return ""


def ensure_subdomain(account_id, preferred):
    """Register the workers.dev subdomain the first time; wrangler cannot do it non-interactively."""
    import requests
    token = oauth_token()
    if not token:
        return
    url = "https://api.cloudflare.com/client/v4/accounts/%s/workers/subdomain" % account_id
    headers = {"Authorization": "Bearer " + token}
    try:
        current = requests.get(url, headers=headers, timeout=30)
        if current.ok:
            name = (current.json().get("result") or {}).get("subdomain")
            if name:
                print("workers.dev уже зарегистрирован: %s" % name, flush=True)
                return name
    except Exception:
        pass
    import secrets
    candidates = [preferred, preferred + "-lh", "lh-" + secrets.token_hex(4)]
    for name in candidates:
        try:
            resp = requests.put(url, headers=headers, json={"subdomain": name}, timeout=30)
        except Exception as exc:
            raise SystemExit("не смог зарегистрировать workers.dev: %s" % exc)
        if resp.ok:
            print("Зарегистрирован workers.dev: %s" % name, flush=True)
            return name
        body = resp.text[:200]
        print("субдомен %s не подошёл: %s" % (name, body), flush=True)
    raise SystemExit("не удалось занять workers.dev субдомен — зарегистрируй вручную: "
                     "https://dash.cloudflare.com/%s/workers/onboarding" % account_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-name", default="lh-router")
    parser.add_argument("--subdomain", default="lenovolquser15")
    args = parser.parse_args()

    ensure_node()
    ensure_wrangler()
    ok, text = whoami()
    if not ok:
        print("Нужен вход в Cloudflare: откроется браузер, войди и разреши доступ.", flush=True)
        wrangler("login", check=False)
        ok, text = whoami()
        if not ok:
            raise SystemExit("вход в Cloudflare не выполнен")
    account = ACCOUNT_ID_RE.search(text)
    if account:
        ensure_subdomain(account.group(1), args.subdomain)

    cfg, remote, secret = secret_from_config()
    write_worker_toml(args.worker_name)

    print("Деплою Worker…", flush=True)
    proc = wrangler("deploy", cwd=WORKER_DIR, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SystemExit("деплой Worker не удался — см. вывод выше")
    match = re.search(r"https://[a-z0-9.-]+\.workers\.dev", output)
    if not match:
        raise SystemExit("не нашёл адрес Worker в выводе wrangler — скопируй его из дашборда")
    worker_url = match.group(0).rstrip(".")

    print("Записываю секрет реле…", flush=True)
    wrangler("secret", "put", "LH_SECRET", cwd=WORKER_DIR, stdin=secret + "\n")

    site_url = worker_url
    save_config(cfg, remote, secret, worker_url, site_url)
    print("\nГотово.")
    print("  реле и сайт:  %s" % worker_url)
    print("  секрет записан в config.json; в репозиторий он не попадает.")


if __name__ == "__main__":
    main()
