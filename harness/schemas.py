"""Tool definitions and system prompts for LocalHarness."""

SPECS = [
    {"profile":"core","name":"shell","desc":"Выполнить команду на Windows через PowerShell; mode='bash' для Git Bash.","params":{"cmd":("string","команда"),"cwd":("string?","рабочая папка"),"timeout":("number","секунды"),"background":("bool","в фоне"),"mode":("string?","powershell|bash|raw")},"required":["cmd"]},
    {"profile":"full","name":"powershell","desc":"Явно выполнить PowerShell-команду на Windows.","params":{"cmd":("string","PowerShell-код"),"cwd":("string?","рабочая папка"),"timeout":("number","секунды"),"background":("bool","в фоне")},"required":["cmd"]},
    {"profile":"full","name":"ssh","desc":"Выполнить команду на VPS через OpenSSH (ключ из ~/.ssh подставляется сам, пароль не нужен; многострочные bash-скрипты передаются безопасно).","params":{"command":("string","команда или скрипт"),"profile":("string?","профиль"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","путь к ключу"),"timeout":("number","секунды"),"background":("bool","в фоне")},"required":["command"]},
    {"profile":"full","name":"ssh_connect","desc":"Проверить SSH-подключение к VPS и получить сводку о системе (ОС, дистрибутив, uptime, память, диск, docker).","params":{"profile":("string?","профиль"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","ключ"),"sysinfo":("bool?","собрать инфо о ресурсах сервера")},"required":[]},
    {"profile":"full","name":"ssh_upload","desc":"Передать локальный файл или директорию на VPS через scp.","params":{"local_path":("string","локальный файл или папка"),"remote_path":("string","путь на VPS"),"profile":("string?","профиль"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","ключ"),"timeout":("number","секунды"),"recursive":("bool?","передать папку рекурсивно")},"required":["local_path","remote_path"]},
    {"profile":"full","name":"ssh_download","desc":"Скачать файл или директорию с VPS через scp.","params":{"remote_path":("string","путь на VPS"),"local_path":("string?","куда сохранить"),"profile":("string?","профиль"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","ключ"),"timeout":("number","секунды"),"recursive":("bool?","скачать рекурсивно")},"required":["remote_path"]},
    {"profile":"full","name":"ssh_profile","desc":"Профили VPS: list — показать сохранённые и системные из ~/.ssh/config, save — запомнить host/user/port/key, delete — удалить профиль, pubkey — показать публичный ключ для авторизации на VPS.","params":{"action":("string","list|save|delete|pubkey"),"name":("string?","имя профиля"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","путь к ключу")},"required":[]},
    {"profile":"full","name":"ssh_tunnel","desc":"SSH-туннель (проброс портов на VPS): open — открыть локальный порт к удалённому сервису (БД, web, API на 127.0.0.1 на VPS), close — закрыть, list — список активных туннелей.","params":{"action":("string","open|close|list"),"local_port":("number?","локальный порт"),"remote_port":("number?","удалённый порт на VPS"),"remote_host":("string?","удалённый адрес на VPS, по умолчанию 127.0.0.1"),"profile":("string?","профиль"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","ключ"),"pid":("number?","PID для close")},"required":["action"]},
    {"profile":"core","name":"read_file","desc":"Прочитать файл с номерами строк.","params":{"path":("string","путь"),"offset":("number","первая строка"),"limit":("number","строк")},"required":["path"]},
    {"profile":"core","name":"write_file","desc":"Записать файл целиком или дописать.","params":{"path":("string","путь"),"content":("string","текст"),"mode":("string?","overwrite|append")},"required":["path","content"]},
    {"profile":"core","name":"edit_file","desc":"Заменить буквальный фрагмент файла.","params":{"path":("string","путь"),"find":("string","что заменить"),"replace":("string","на что"),"all":("bool","все совпадения")},"required":["path","find","replace"]},
    {"profile":"core","name":"grep","desc":"Поиск regex по файлам.","params":{"pattern":("string","regex"),"path":("string?","путь"),"glob":("string","маска"),"context":("number","строк вокруг"),"limit":("number","макс")},"required":["pattern"]},
    {"profile":"core","name":"list_dir","desc":"Список файлов в папке.","params":{"path":("string?","папка"),"limit":("number","записей")},"required":[]},
    {"profile":"core","name":"git","desc":"Git в рабочей папке: status — изменения и ветка, diff — отличия (staged — по индексу, stat — сводка), log — история, show — коммит, add — в индекс (path или all), commit — закоммитить индекс (message), branch — список веток или переключение/создание по name. Push/pull — только по явной просьбе через shell.","params":{"action":("string","status|diff|log|show|add|commit|branch"),"cwd":("string?","папка репозитория"),"path":("string?","файл или папка"),"message":("string?","сообщение коммита"),"all":("bool?","git add -A"),"staged":("bool?","diff по индексу"),"stat":("bool?","сводка diff"),"rev":("string?","revision/branch"),"limit":("number?","для log, по умолчанию 15"),"name":("string?","ветка для branch")},"required":["action"]},
    {"profile":"core","name":"web_search","desc":"Поиск в интернете с перебором источников: DuckDuckGo, Right Dao, Bing, а при нехватке — версии продуктов, Habr, Wikipedia, StackOverflow, GitHub, Hacker News, arXiv, npm, PyPI. engine — конкретный источник.","params":{"query":("string","запрос"),"count":("number","ссылок"),"engine":("string?","источник: auto|duckduckgo|ddg_lite|ddg_ia|bing|rightdao|versions|habr|wikipedia|stackexchange|github|hackernews|arxiv|npm|pypi")},"required":["query"]},
    {"profile":"core","name":"web_fetch","desc":"Открыть URL.","params":{"url":("string","URL"),"limit":("number","макс знаков")},"required":["url"]},
    {"profile":"core","name":"python","desc":"Python REPL с постоянным состоянием.","params":{"code":("string","код"),"timeout":("number","секунды")},"required":["code"]},
    {"profile":"core","name":"screenshot","desc":"Снимок экрана: screen — весь, window — окно по title/hwnd, active — переднее окно, rect — область x,y,w,h. Vision-модель получает снимок автоматически.","params":{"region":("string?","screen|window|active|rect"),"title":("string?","заголовок окна"),"hwnd":("number?","хэндл"),"x":("number?","X"),"y":("number?","Y"),"w":("number?","ширина"),"h":("number?","высота"),"delay":("number?","пауза перед снимком, с"),"focus":("bool?","поднять окно перед снимком")},"required":[]},
    {"profile":"core","name":"windows","desc":"Управление окнами Windows.","params":{"action":("string","list|focus|keys|type|click|move"),"title":("string?","заголовок"),"hwnd":("number?","хэндл"),"combo":("string?","клавиши"),"text":("string?","текст"),"x":("number?","X"),"y":("number?","Y")},"required":["action"]},
    {"profile":"core","name":"ask_user","desc":"Спросить пользователя.","params":{"question":("string","вопрос"),"options":("array?","варианты")},"required":["question"]},
    {"profile":"core","name":"check_goal_point","desc":"Отметить пункт чеклиста цели выполненным. Только после реальной проверки этого пункта инструментом; evidence — что показала проверка.","params":{"index":("number","номер пункта, с 1"),"done":("bool?","выполнено, по умолчанию да"),"evidence":("string?","что показала проверка")},"required":["index"]},
    {"profile":"core","name":"finish_goal","desc":"Закрыть цель только после проверки.","params":{"achieved":("bool","достигнута"),"summary":("string","итог"),"evidence":("string","доказательство")},"required":["achieved","summary"]},
    {"profile":"full","name":"glob","desc":"Найти файлы по маске.","params":{"pattern":("string","маска"),"path":("string?","папка"),"limit":("number","макс")},"required":["pattern"]},
    {"profile":"full","name":"jobs","desc":"Фоновые задачи.","params":{"action":("string","list|log|kill"),"pid":("number?","PID"),"lines":("number","строк")},"required":["action"]},
    {"profile":"full","name":"sysinfo","desc":"Информация о машине.","params":{"action":("string","overview|processes|ports|disks|env|gpu")},"required":[]},
    {"profile":"full","name":"browser","desc":"Управление Chrome через CDP.","params":{"action":("string","tabs|open|read|click|fill"),"url":("string?","URL"),"selector":("string?","CSS"),"value":("string?","значение"),"tab_id":("string?","вкладка")},"required":["action"]},
    {"profile":"full","name":"download","desc":"Скачать URL в файл.","params":{"url":("string","ссылка"),"path":("string?","путь")},"required":["url"]},
    {"profile":"full","name":"kill_process","desc":"Остановить процесс.","params":{"pid":("number?","PID"),"name":("string?","имя")},"required":[]},
    {"profile":"full","name":"clipboard","desc":"Буфер обмена.","params":{"action":("string","get|set"),"text":("string?","текст")},"required":[]},
    {"profile":"full","name":"lmstudio","desc":"Управление LM Studio.","params":{"action":("string","status|start|stop|load|unload|ps"),"model":("string?","модель")},"required":["action"]},
    {"profile":"full","name":"remember","desc":"Записать заметку.","params":{"text":("string","текст")},"required":["text"]},
     {"profile":"full","name":"model_info","desc":"Показать установленные и загруженные модели LM Studio.","params":{},"required":[]},
     {"profile":"full","name":"model_estimate","desc":"Оценить требования модели к памяти (RAM/VRAM), ожидаемую скорость (токенов/сек) и риск лагов интерфейса.","params":{"model":("string?","имя или путь модели"),"context_length":("number?","размер контекста в токенах, по умолчанию 8192")},"required":[]},
     {"profile":"full","name":"dataset_append","desc":"Добавить пример instruction/input/output в JSONL-датасет для SFT/LoRA.","params":{"path":("string","JSONL-файл"),"instruction":("string","инструкция"),"input":("string?","входные данные"),"output":("string","правильный ответ")},"required":["path","instruction","output"]},
     {"profile":"full","name":"dataset_validate","desc":"Проверить JSONL-датасет и посчитать корректные/битые строки.","params":{"path":("string","JSONL-файл")},"required":["path"]},
     {"profile":"full","name":"train_model","desc":"Запустить внешний LoRA/SFT-тренер командой в фоне.","params":{"command":("string","команда запуска обучения"),"cwd":("string?","рабочая папка"),"timeout":("number","секунды"),"background":("bool","в фоне")},"required":["command"]},
     {"profile":"full","name":"game_control","desc":"Игровой режим и управление играми/GUI на Windows: нажатия клавиш (WASD, пробел, хоткеи с удержанием/скан-кодами для DirectX), движения и клики мыши, относительный поворот камеры в 3D/FPS играх (dx, dy), перетаскивание, фокусировка окна игры и мгновенные скриншоты. Поддерживает последовательность действий actions=[...] за один вызов.","params":{"action":("string?","press|hold|release|combo|click|double_click|down|up|move|look|drag|scroll|wait|sequence|focus|screenshot|check"),"window":("string?","заголовок окна, процесс (.exe) или hwnd"),"actions":("array?","список действий для выполнения подряд"),"key":("string?","клавиша (w, a, s, d, space, shift, ctrl, 1-9, enter, esc и др.)"),"combo":("string?","комбинация клавиш, например shift+w или ctrl+space"),"button":("string?","кнопка мыши: left|right|middle"),"x":("number?","координата X"),"y":("number?","координата Y"),"dx":("number?","смещение камеры X для 3D/FPS игр"),"dy":("number?","смещение камеры Y для 3D/FPS игр"),"x2":("number?","конечная X для drag"),"y2":("number?","конечная Y для drag"),"duration":("number?","длительность нажатия/удержания в секундах, по умолчанию 0.08"),"relative":("bool?","координаты x,y относительно окна игры"),"amount":("number?","прокрутка колеса, по умолчанию 120"),"seconds":("number?","пауза для action=wait"),"screenshot":("bool?","сделать скриншот окна после выполнения действий")},"required":[]},
     {"profile":"full","name":"generate_image","desc":"Генерация изображений через локальный ComfyUI или Stable Diffusion WebUI. Включается в Настройках (image_gen_enabled).","params":{"prompt":("string","описание желаемого изображения на английском"),"negative_prompt":("string?","что исключить из генерации"),"width":("number?","ширина, по умолчанию 512"),"height":("number?","высота, по умолчанию 512"),"steps":("number?","шаги, по умолчанию 20"),"seed":("number?","сид")},"required":["prompt"]},
     {"profile":"full","name":"recall","desc":"Поиск в долговременной памяти фактов о пользователе, среде и проектах.","params":{"query":("string?","поисковый запрос")},"required":[]},
]
PROFILES=("core","full")

def entries(profile="core"):
    from . import plugins
    base=SPECS if profile=="full" else [s for s in SPECS if s["profile"]=="core"]
    try: return base+plugins.entries()
    except Exception: return base

def native_tools(profile="core"):
    out=[]
    for s in entries(profile):
        props={k:{"type":{"string":"string","number":"number","bool":"boolean","array":"array"}.get(v[0].rstrip("?"),"string"),"description":v[1]} for k,v in s["params"].items()}
        out.append({"type":"function","function":{"name":s["name"],"description":s["desc"],"parameters":{"type":"object","properties":props,"required":s["required"]}}})
    return out

def text_protocol_doc(profile="core"):
    return "\n".join("- %s(%s) — %s"%(s["name"],", ".join(s["params"]),s["desc"]) for s in entries(profile))

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
- web_search ищет сразу по нескольким источникам (DuckDuckGo, Right Dao, Bing, версии продуктов, Wikipedia, StackOverflow, GitHub, Hacker News, arXiv, npm, PyPI) и сам переключается на следующий, если текущий молчит или отдаёт выдачу не по теме; если выдача пустая — уточни запрос или укажи engine.
- Вопрос «какая последняя версия X» — сразу engine=versions: там точная версия, а не обсуждения.
- Если цель связана с VPS/SSH: извлеки host/user/port/key из запроса или профиля (ssh_profile action='list'), сразу вызови ssh_connect; недостающий параметр спроси через ask_user, не угадывай.
- После подключения: ssh для команд, ssh_upload/ssh_download для файлов. Не утверждай, что подключился, пока не получил реальный вывод команды.'''

def build_system_prompt(cfg, profile="core", vision=False):
    import os
    from .config import ROOT
    env={"os":"Windows","home":os.path.expanduser("~"),"workspace":cfg.get("workspace"),"harness":ROOT}
    profiles = cfg.get("ssh_profiles") or {}
    if profiles:
        env["vps_profiles"] = "; ".join("%s: %s@%s:%s" % (k, v.get("user") or "root", v.get("host"), v.get("port") or 22)
                                        for k, v in profiles.items())
    notes=["Для локальной автоматизации используй PowerShell.","Для VPS используй SSH-ключ или ssh-agent; пароль не сохраняй. Сохранённые профили — ssh_profile action='list'."]
    style=["Отвечай по-русски и по делу.","Сначала действуй инструментом, потом сообщай результат.","Не говори «готово» без проверки.","Если запрос означает подключение к VPS, извлеки реквизиты и вызови ssh_connect.","Если вызов не сработал — не повторяй его: смени тактику или найди решение через web_search/web_fetch.","Состояние репозитория проверяй инструментом git (status/diff/log); сохраняй работу через git add и git commit. Push и pull — только по явной просьбе пользователя."]
    built = "Ты — LocalHarness, локальный агент.\n\nОКРУЖЕНИЕ:\n"+"\n".join("- %s: %s"%x for x in env.items())+"\n\n"+"\n".join("- "+n for n in notes)+"\n\nИНСТРУМЕНТЫ:\n"+text_protocol_doc(profile)+"\n\nСТИЛЬ:\n"+"\n".join("- "+s for s in style)+"\n"+("" if vision else "Скриншот виден пользователю, но текстовая модель его не видит.")
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
