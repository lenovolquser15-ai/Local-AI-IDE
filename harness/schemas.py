"""Tool definitions and system prompts for LocalHarness."""

SPECS = [
    {"profile":"core","name":"shell","desc":"Выполнить команду на Windows через PowerShell; mode='bash' для Git Bash.","params":{"cmd":("string","команда"),"cwd":("string?","рабочая папка"),"timeout":("number","секунды"),"background":("bool","в фоне"),"mode":("string?","powershell|bash|raw")},"required":["cmd"]},
    {"profile":"full","name":"powershell","desc":"Явно выполнить PowerShell-команду на Windows.","params":{"cmd":("string","PowerShell-код"),"cwd":("string?","рабочая папка"),"timeout":("number","секунды"),"background":("bool","в фоне")},"required":["cmd"]},
    {"profile":"full","name":"ssh","desc":"Выполнить команду на VPS через OpenSSH (ключ из ~/.ssh подставляется сам, пароль не нужен).","params":{"command":("string","команда"),"profile":("string?","профиль"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","путь к ключу"),"timeout":("number","секунды"),"background":("bool","в фоне")},"required":["command"]},
    {"profile":"full","name":"ssh_connect","desc":"Проверить SSH-подключение к VPS и узнать ОС удалённой машины.","params":{"profile":("string?","профиль"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","ключ")},"required":[]},
    {"profile":"full","name":"ssh_upload","desc":"Передать локальный файл на VPS через scp.","params":{"local_path":("string","локальный файл"),"remote_path":("string","путь на VPS"),"profile":("string?","профиль"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","ключ"),"timeout":("number","секунды")},"required":["local_path","remote_path"]},
    {"profile":"full","name":"ssh_download","desc":"Скачать файл с VPS через scp.","params":{"remote_path":("string","путь на VPS"),"local_path":("string?","куда сохранить"),"profile":("string?","профиль"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","ключ"),"timeout":("number","секунды")},"required":["remote_path"]},
    {"profile":"full","name":"ssh_profile","desc":"Профили VPS: list — показать сохранённые, save — запомнить host/user/port/key.","params":{"action":("string","list|save"),"name":("string?","имя профиля"),"host":("string?","IP или DNS"),"user":("string?","пользователь"),"port":("number?","порт"),"key":("string?","путь к ключу")},"required":[]},
    {"profile":"core","name":"read_file","desc":"Прочитать файл с номерами строк.","params":{"path":("string","путь"),"offset":("number","первая строка"),"limit":("number","строк")},"required":["path"]},
    {"profile":"core","name":"write_file","desc":"Записать файл целиком или дописать.","params":{"path":("string","путь"),"content":("string","текст"),"mode":("string?","overwrite|append")},"required":["path","content"]},
    {"profile":"core","name":"edit_file","desc":"Заменить буквальный фрагмент файла.","params":{"path":("string","путь"),"find":("string","что заменить"),"replace":("string","на что"),"all":("bool","все совпадения")},"required":["path","find","replace"]},
    {"profile":"core","name":"grep","desc":"Поиск regex по файлам.","params":{"pattern":("string","regex"),"path":("string?","путь"),"glob":("string","маска"),"context":("number","строк вокруг"),"limit":("number","макс")},"required":["pattern"]},
    {"profile":"core","name":"list_dir","desc":"Список файлов в папке.","params":{"path":("string?","папка"),"limit":("number","записей")},"required":[]},
    {"profile":"core","name":"web_search","desc":"Поиск в интернете с перебором источников: DuckDuckGo, Right Dao, Bing, а при нехватке — версии продуктов, Wikipedia, StackOverflow, GitHub, Hacker News, arXiv, npm, PyPI. engine — конкретный источник.","params":{"query":("string","запрос"),"count":("number","ссылок"),"engine":("string?","источник: auto|duckduckgo|ddg_lite|ddg_ia|bing|rightdao|versions|wikipedia|stackexchange|github|hackernews|arxiv|npm|pypi")},"required":["query"]},
    {"profile":"core","name":"web_fetch","desc":"Открыть URL.","params":{"url":("string","URL"),"limit":("number","макс знаков")},"required":["url"]},
    {"profile":"core","name":"python","desc":"Python REPL с постоянным состоянием.","params":{"code":("string","код"),"timeout":("number","секунды")},"required":["code"]},
    {"profile":"core","name":"screenshot","desc":"Снимок экрана: screen — весь, window — окно по title/hwnd, active — переднее окно, rect — область x,y,w,h. Vision-модель получает снимок автоматически.","params":{"region":("string?","screen|window|active|rect"),"title":("string?","заголовок окна"),"hwnd":("number?","хэндл"),"x":("number?","X"),"y":("number?","Y"),"w":("number?","ширина"),"h":("number?","высота"),"delay":("number?","пауза перед снимком, с"),"focus":("bool?","поднять окно перед снимком")},"required":[]},
    {"profile":"core","name":"windows","desc":"Управление окнами Windows.","params":{"action":("string","list|focus|keys|type|click|move"),"title":("string?","заголовок"),"hwnd":("number?","хэндл"),"combo":("string?","клавиши"),"text":("string?","текст"),"x":("number?","X"),"y":("number?","Y")},"required":["action"]},
    {"profile":"core","name":"ask_user","desc":"Спросить пользователя.","params":{"question":("string","вопрос"),"options":("array?","варианты")},"required":["question"]},
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
     {"profile":"full","name":"dataset_append","desc":"Добавить пример instruction/input/output в JSONL-датасет для SFT/LoRA.","params":{"path":("string","JSONL-файл"),"instruction":("string","инструкция"),"input":("string?","входные данные"),"output":("string","правильный ответ")},"required":["path","instruction","output"]},
     {"profile":"full","name":"dataset_validate","desc":"Проверить JSONL-датасет и посчитать корректные/битые строки.","params":{"path":("string","JSONL-файл")},"required":["path"]},
     {"profile":"full","name":"train_model","desc":"Запустить внешний LoRA/SFT-тренер командой в фоне.","params":{"command":("string","команда запуска обучения"),"cwd":("string?","рабочая папка"),"timeout":("number","секунды"),"background":("bool","в фоне")},"required":["command"]},
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
- КРИТЕРИЙ ГОТОВНОСТИ — чеклист: проверь КАЖДЫЙ пункт реальным инструментом (вывод команды, содержимое файла, скриншот), а не по памяти.
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
    style=["Отвечай по-русски и по делу.","Сначала действуй инструментом, потом сообщай результат.","Не говори «готово» без проверки.","Если запрос означает подключение к VPS, извлеки реквизиты и вызови ssh_connect.","Если вызов не сработал — не повторяй его: смени тактику или найди решение через web_search/web_fetch."]
    built = "Ты — LocalHarness, локальный агент.\n\nОКРУЖЕНИЕ:\n"+"\n".join("- %s: %s"%x for x in env.items())+"\n\n"+"\n".join("- "+n for n in notes)+"\n\nИНСТРУМЕНТЫ:\n"+text_protocol_doc(profile)+"\n\nСТИЛЬ:\n"+"\n".join("- "+s for s in style)+"\n"+("" if vision else "Скриншот виден пользователю, но текстовая модель его не видит.")
    custom = str(cfg.get("system_prompt") or "").strip()
    return built + ("\n\nДОПОЛНИТЕЛЬНАЯ ИНСТРУКЦИЯ ПОЛЬЗОВАТЕЛЯ:\n" + custom if custom else "")
