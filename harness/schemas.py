"""Tool definitions and system prompts for LocalHarness."""

SPECS = [
    {"profile":"core","name":"shell","desc":"PowerShell-команда; mode='bash' — Git Bash.","params":{"cmd":("string","команда"),"cwd":("string?","папка"),"timeout":("number","секунды"),"background":("bool","в фоне"),"mode":("string?","powershell|bash|raw")},"required":["cmd"]},
    {"profile":"full","name":"powershell","desc":"PowerShell-команда.","params":{"cmd":("string","код"),"cwd":("string?","папка"),"timeout":("number","секунды"),"background":("bool","в фоне")},"required":["cmd"]},
    {"profile":"full","name":"ssh","desc":"SSH-команда на VPS; ключ из ~/.ssh подставляется сам.","params":{"command":("string","команда или скрипт"),"profile":("string?","профиль VPS"),"host":("string?","IP/DNS"),"user":("string?","логин"),"port":("number?","порт"),"key":("string?","ключ"),"timeout":("number","секунды"),"background":("bool","в фоне")},"required":["command"]},
    {"profile":"full","name":"ssh_connect","desc":"Проверить SSH и собрать сводку о VPS.","params":{"profile":("string?","профиль VPS"),"host":("string?","IP/DNS"),"user":("string?","логин"),"port":("number?","порт"),"key":("string?","ключ"),"sysinfo":("bool?","ОС, RAM, диск, docker")},"required":[]},
    {"profile":"full","name":"ssh_upload","desc":"scp: файл или папка на VPS.","params":{"local_path":("string","локальный путь"),"remote_path":("string","путь на VPS"),"profile":("string?","профиль VPS"),"host":("string?","IP/DNS"),"user":("string?","логин"),"port":("number?","порт"),"key":("string?","ключ"),"timeout":("number","секунды"),"recursive":("bool?","папку рекурсивно")},"required":["local_path","remote_path"]},
    {"profile":"full","name":"ssh_download","desc":"scp: файл или папка с VPS.","params":{"remote_path":("string","путь на VPS"),"local_path":("string?","куда сохранить"),"profile":("string?","профиль VPS"),"host":("string?","IP/DNS"),"user":("string?","логин"),"port":("number?","порт"),"key":("string?","ключ"),"timeout":("number","секунды"),"recursive":("bool?","папку рекурсивно")},"required":["remote_path"]},
    {"profile":"full","name":"ssh_profile","desc":"Профили VPS: list|save|delete|pubkey.","params":{"action":("string","list|save|delete|pubkey"),"name":("string?","имя"),"host":("string?","IP/DNS"),"user":("string?","логин"),"port":("number?","порт"),"key":("string?","ключ")},"required":[]},
    {"profile":"full","name":"ssh_tunnel","desc":"SSH-туннель портов: open|close|list.","params":{"action":("string","open|close|list"),"local_port":("number?","локальный порт"),"remote_port":("number?","порт на VPS"),"remote_host":("string?","адрес на VPS"),"profile":("string?","профиль VPS"),"host":("string?","IP/DNS"),"user":("string?","логин"),"port":("number?","порт"),"key":("string?","ключ"),"pid":("number?","PID для close")},"required":["action"]},
    {"profile":"core","name":"read_file","desc":"Файл с номерами строк.","params":{"path":("string","путь"),"offset":("number","с строки"),"limit":("number","строк")},"required":["path"]},
    {"profile":"core","name":"write_file","desc":"Записать файл целиком или дописать.","params":{"path":("string","путь"),"content":("string","текст"),"mode":("string?","overwrite|append")},"required":["path","content"]},
    {"profile":"core","name":"edit_file","desc":"Заменить буквальный фрагмент.","params":{"path":("string","путь"),"find":("string","найти"),"replace":("string","заменить"),"all":("bool","все")},"required":["path","find","replace"]},
    {"profile":"core","name":"grep","desc":"Regex-поиск по файлам.","params":{"pattern":("string","regex"),"path":("string?","где"),"glob":("string","маска"),"context":("number","строк вокруг"),"limit":("number","макс")},"required":["pattern"]},
    {"profile":"core","name":"list_dir","desc":"Файлы в папке.","params":{"path":("string?","папка"),"limit":("number","записей")},"required":[]},
    {"profile":"core","name":"git","desc":"Git: status|diff|log|show|add|commit|branch; push/pull — через shell.","params":{"action":("string","status|diff|log|show|add|commit|branch"),"cwd":("string?","репозиторий"),"path":("string?","файл/папка"),"message":("string?","сообщение коммита"),"all":("bool?","add -A"),"staged":("bool?","diff индекса"),"stat":("bool?","кратко"),"rev":("string?","revision"),"limit":("number?","для log"),"name":("string?","ветка")},"required":["action"]},
    {"profile":"core","name":"web_search","desc":"Поиск: DDG, Bing, RightDao, Camoufox + вики/хабр/стек/GitHub/npm/PyPI.","params":{"query":("string","запрос"),"count":("number","ссылок"),"engine":("string?","auto|duckduckgo|ddg_lite|ddg_ia|bing|rightdao|camoufox|versions|habr|wikipedia|stackexchange|github|hackernews|arxiv|npm|pypi")},"required":["query"]},
    {"profile":"core","name":"web_fetch","desc":"Открыть URL.","params":{"url":("string","URL"),"limit":("number","макс знаков")},"required":["url"]},
    {"profile":"core","name":"python","desc":"Python REPL, состояние сохраняется.","params":{"code":("string","код"),"timeout":("number","секунды")},"required":["code"]},
    {"profile":"core","name":"screenshot","desc":"Снимок экрана; vision-модель видит его сама.","params":{"region":("string?","screen|window|active|rect"),"title":("string?","заголовок окна"),"hwnd":("number?","хэндл"),"x":("number?","X"),"y":("number?","Y"),"w":("number?","ширина"),"h":("number?","высота"),"delay":("number?","пауза, с"),"focus":("bool?","поднять окно")},"required":[]},
    {"profile":"core","name":"windows","desc":"Окна Windows: list|focus|keys|type|click|move.","params":{"action":("string","list|focus|keys|type|click|move"),"title":("string?","заголовок"),"hwnd":("number?","хэндл"),"combo":("string?","клавиши"),"text":("string?","текст"),"x":("number?","X"),"y":("number?","Y")},"required":["action"]},
    {"profile":"core","name":"ask_user","desc":"Спросить пользователя.","params":{"question":("string","вопрос"),"options":("array?","варианты")},"required":["question"]},
    {"profile":"core","name":"check_goal_point","desc":"Отметить пункт чеклиста после реальной проверки.","params":{"index":("number","номер, с 1"),"done":("bool?","выполнено"),"evidence":("string?","чем подтверждено")},"required":["index"]},
    {"profile":"core","name":"finish_goal","desc":"Закрыть цель только после проверки.","params":{"achieved":("bool","достигнута"),"summary":("string","итог"),"evidence":("string","доказательство")},"required":["achieved","summary"]},
    {"profile":"full","name":"glob","desc":"Найти файлы по маске.","params":{"pattern":("string","маска"),"path":("string?","папка"),"limit":("number","макс")},"required":["pattern"]},
    {"profile":"full","name":"jobs","desc":"Фоновые задачи: list|log|kill.","params":{"action":("string","list|log|kill"),"pid":("number?","PID"),"lines":("number","строк")},"required":["action"]},
    {"profile":"full","name":"sysinfo","desc":"Машина: overview|processes|ports|disks|env|gpu.","params":{"action":("string","overview|processes|ports|disks|env|gpu")},"required":[]},
    {"profile":"full","name":"browser","desc":"Браузер (Chrome CDP или stealth Camoufox): tabs|open|read|click|fill|screenshot|scroll|evaluate.","params":{"action":("string","tabs|open|read|click|fill|screenshot|scroll|evaluate"),"url":("string?","URL"),"selector":("string?","CSS"),"value":("string?","значение"),"tab_id":("string?","вкладка"),"engine":("string?","auto|chrome|camoufox")},"required":["action"]},
    {"profile":"full","name":"download","desc":"Скачать URL в файл.","params":{"url":("string","ссылка"),"path":("string?","путь")},"required":["url"]},
    {"profile":"full","name":"kill_process","desc":"Остановить процесс.","params":{"pid":("number?","PID"),"name":("string?","имя")},"required":[]},
    {"profile":"full","name":"clipboard","desc":"Буфер обмена: get|set.","params":{"action":("string","get|set"),"text":("string?","текст")},"required":[]},
    {"profile":"full","name":"lmstudio","desc":"LM Studio: status|start|stop|load|unload|ps.","params":{"action":("string","status|start|stop|load|unload|ps"),"model":("string?","модель")},"required":["action"]},
    {"profile":"full","name":"remember","desc":"Записать заметку в память.","params":{"text":("string","текст")},"required":["text"]},
    {"profile":"full","name":"model_info","desc":"Модели LM Studio: установленные и загруженные.","params":{},"required":[]},
    {"profile":"full","name":"model_estimate","desc":"RAM/VRAM, скорость и риск лагов для модели.","params":{"model":("string?","имя или путь"),"context_length":("number?","токенов")},"required":[]},
    {"profile":"full","name":"dataset_append","desc":"Строка instruction/input/output в JSONL для SFT/LoRA.","params":{"path":("string","JSONL-файл"),"instruction":("string","инструкция"),"input":("string?","вход"),"output":("string","ответ")},"required":["path","instruction","output"]},
    {"profile":"full","name":"dataset_validate","desc":"Проверить JSONL-датасет.","params":{"path":("string","JSONL-файл")},"required":["path"]},
    {"profile":"full","name":"train_model","desc":"Запустить LoRA/SFT-тренер в фоне.","params":{"command":("string","команда обучения"),"cwd":("string?","папка"),"timeout":("number","секунды"),"background":("bool","в фоне")},"required":["command"]},
    {"profile":"full","name":"game_control","desc":"Игры/GUI: клавиши (скан-коды), мышь, look(dx,dy), drag, focus, скриншот; actions=[...] — пачкой.","params":{"action":("string?","press|hold|release|combo|click|double_click|down|up|move|look|drag|scroll|wait|sequence|focus|screenshot|check"),"window":("string?","окно/процесс/hwnd"),"actions":("array?","пачка действий"),"key":("string?","клавиша"),"combo":("string?","shift+w"),"button":("string?","left|right|middle"),"x":("number?","X"),"y":("number?","Y"),"dx":("number?","поворот камеры X"),"dy":("number?","поворот камеры Y"),"x2":("number?","конец drag X"),"y2":("number?","конец drag Y"),"duration":("number?","нажатие, с"),"relative":("bool?","от окна"),"amount":("number?","колесо"),"seconds":("number?","wait, с"),"screenshot":("bool?","снимок после")},"required":[]},
    {"profile":"full","name":"generate_image","desc":"Картинка через ComfyUI/SD WebUI (в Настройках).","params":{"prompt":("string","промпт (англ.)"),"negative_prompt":("string?","исключить"),"width":("number?","ширина"),"height":("number?","высота"),"steps":("number?","шаги"),"seed":("number?","сид")},"required":["prompt"]},
    {"profile":"full","name":"recall","desc":"Поиск в долговременной памяти.","params":{"query":("string?","запрос")},"required":[]},
    {"profile":"full","name":"cluster_run","desc":"Wi-Fi кластер устройств: status|map|bench|llm (разгрузка задач на телефоны/ПК в локальной сети).","params":{"action":("string","status|map|bench|llm"),"kind":("string?","compute_bench|text_search|hash_batch|json_transform"),"items":("array?","массив подзадач"),"prompt":("string?","промпт для LAN LLM"),"node_url":("string?","URL узла"),"chunks":("number?","чанков для bench")},"required":["action"]},
]
PROFILES=("core","full")
MEMORY_TOOLS=("remember","recall")

def entries(profile="core", cfg=None):
    from . import plugins
    base=SPECS if profile=="full" else [s for s in SPECS if s["profile"]=="core"]
    if not (cfg or {}).get("memory_enabled", False):
        base=[s for s in base if s["name"] not in MEMORY_TOOLS]
    try: return base+plugins.entries()
    except Exception: return base

# Имена, где описание дублирует само имя: в JSON-схеме это лишние токены в каждом запросе.
PLAIN_PARAMS={"cmd","command","path","local_path","remote_path","url","query","pattern","content","find",
              "replace","text","prompt","question","code","cwd","timeout","limit","offset","count","lines",
              "message","instruction","input","output","name","host","user","port","model","x","y","x2","y2",
              "dx","dy","width","height","steps","seed","seconds","pid","title","hwnd","value","selector",
              "tab_id","window","negative_prompt"}

def native_tools(profile="core", cfg=None):
    out=[]
    for s in entries(profile, cfg):
        props={}
        for k,v in s["params"].items():
            prop={"type":{"string":"string","number":"number","bool":"boolean","array":"array"}.get(v[0].rstrip("?"),"string")}
            if k not in PLAIN_PARAMS:
                prop["description"]=v[1]
            props[k]=prop
        params={"type":"object","properties":props}
        if s["required"]:
            params["required"]=s["required"]
        out.append({"type":"function","function":{"name":s["name"],"description":s["desc"],"parameters":params}})
    return out

def text_protocol_doc(profile="core", cfg=None):
    return "\n".join("- %s(%s) — %s"%(s["name"],", ".join(s["params"]),s["desc"]) for s in entries(profile, cfg))

PROTOCOL_INSTRUCTION='''Работай через инструменты. Отвечай одним JSON-объектом без markdown: {"tool":"shell","args":{"cmd":"Get-ChildItem"},"note":"что делаю"}. Когда задача готова: {"final":"итог"}. Не выдумывай результат.'''
GOAL_INSTRUCTION='''РЕЖИМ ЦЕЛИ:
- Цель не закончена, пока не вызван finish_goal.
- ЦЕЛЬ разбита на чеклист пунктов (он в сообщении). Проверяй КАЖДЫЙ пункт реальным инструментом (вывод команды, содержимое файла, скриншот), а не по памяти.
- Когда пункт реально проверен и выполнен — отметь его: check_goal_point(index=<номер>, evidence=<что показала проверка>). Без проверки галочку не ставь.
- finish_goal закрывай, когда все пункты отмечены; если какой-то недостижим — честно скажи об этом в summary.
- Сначала действие, потом проверка. Для GUI/сайтов проверка = screenshot: посмотри на снимок, найди ошибки или нужное состояние и сделай вывод вслух, прежде чем продолжать.
- finish_goal(achieved=true) только с evidence: процитируй реальный вывод проверки или опиши, что видно на скриншоте. Закрытие без проверки будет отклонено.
- Если что-то сломалось — прочитай ошибку (текстом или со скриншота), исправь и проверь снова.
- Если инструмент падает по второму разу или ты повторяешь один и тот же вызов — ищи решение в интернете: web_search, затем web_fetch по найденной ссылке. Ограничений на сеть нет, а повтор того же вызова не даст ничего.
- web_search ищет сразу по нескольким источникам (DuckDuckGo, Right Dao, Bing, версии продуктов, Wikipedia, StackOverflow, GitHub, Hacker News, arXiv, npm, PyPI); если обычная выдача пустая, он сам открывает DuckDuckGo в скрытом Firefox через Camoufox и обходит капчу. Если и это пусто — уточни запрос или укажи engine.
- Вопрос «какая последняя версия X» — сразу engine=versions: там точная версия, а не обсуждения.
- Если цель связана с VPS/SSH: извлеки host/user/port/key из запроса или профиля (ssh_profile action='list'), сразу вызови ssh_connect; недостающий параметр спроси через ask_user, не угадывай.
- После подключения: ssh для команд, ssh_upload/ssh_download для файлов. Не утверждай, что подключился, пока не получил реальный вывод команды.'''

def build_system_prompt(cfg, profile="core", vision=False, tools_doc=True):
    import os
    from .config import ROOT
    env={"os":"Windows","home":os.path.expanduser("~"),"workspace":cfg.get("workspace"),"harness":ROOT}
    profiles = cfg.get("ssh_profiles") or {}
    if profiles:
        env["vps_profiles"] = "; ".join("%s: %s@%s:%s" % (k, v.get("user") or "root", v.get("host"), v.get("port") or 22)
                                        for k, v in profiles.items())
    notes=["Для локальной автоматизации используй PowerShell.","Для VPS используй SSH-ключ или ssh-agent; пароль не сохраняй. Сохранённые профили — ssh_profile action='list'."]
    style=["Отвечай по-русски и по делу.","Сначала действуй инструментом, потом сообщай результат.","Не говори «готово» без проверки.","Если запрос означает подключение к VPS, извлеки реквизиты и вызови ssh_connect.","Если вызов не сработал — не повторяй его: смени тактику или найди решение через web_search/web_fetch.","Состояние репозитория проверяй инструментом git (status/diff/log); сохраняй работу через git add и git commit. Push и pull — только по явной просьбе пользователя."]
    tools_part = ("\n\nИНСТРУМЕНТЫ:\n" + text_protocol_doc(profile, cfg)) if tools_doc else ""
    built = "Ты — LocalHarness, локальный агент.\n\nОКРУЖЕНИЕ:\n"+"\n".join("- %s: %s"%x for x in env.items())+"\n\n"+"\n".join("- "+n for n in notes)+tools_part+"\n\nСТИЛЬ:\n"+"\n".join("- "+s for s in style)+"\n"+("" if vision else "Скриншот виден пользователю, но текстовая модель его не видит.")
    if cfg.get("memory_enabled", False):
        try:
            from . import memory as mem_mod
            mem_text = mem_mod.system_prompt_block()
            if mem_text:
                built += "\n\n" + mem_text
        except Exception:
            pass
    custom = str(cfg.get("system_prompt") or "").strip()
    return built + ("\n\nДОПОЛНИТЕЛЬНАЯ ИНСТРУКЦИЯ ПОЛЬЗОВАТЕЛЯ:\n" + custom if custom else "")
