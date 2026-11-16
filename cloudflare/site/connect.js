(function () {
  var router = String(window.location.origin || "").replace(/\/+$/, "");
  var form = document.getElementById("form");
  var input = document.getElementById("code");
  var button = document.getElementById("go");
  var status = document.getElementById("status");

  function normalize(value) {
    var text = String(value || "").toUpperCase().replace(/[^0-9A-Z]/g, "");
    text = text.replace(/O/g, "0").replace(/[IL]/g, "1").replace(/U/g, "V");
    return text.slice(0, 10);
  }
  function pretty(code) {
    return code.length > 5 ? code.slice(0, 5) + "-" + code.slice(5) : code;
  }
  function say(text, kind) {
    status.textContent = text || "";
    status.className = "status" + (kind ? " " + kind : "");
  }

  input.addEventListener("input", function () {
    input.value = pretty(normalize(input.value));
  });

  async function connect() {
    var code = normalize(input.value);
    if (code.length !== 10) {
      say("Нужен код из 10 символов", "bad");
      return;
    }
    if (!router) {
      say("Сайт ещё не связан с реле: задай адрес Worker в настройках приложения", "bad");
      return;
    }
    button.disabled = true;
    say("Ищу компьютер…");
    try {
      var res = await fetch(router + "/api/rooms/" + code, { cache: "no-store" });
      var data = {};
      try { data = await res.json(); } catch (err) { /* keep empty */ }
      if (!res.ok || !data.ok) {
        var message = data.error || (res.status === 404 ? "Код не найден или истёк" : "Ошибка " + res.status);
        throw new Error(message);
      }
      say("Открываю LocalHarness…", "ok");
      var base = String(data.url || router).replace(/\/+$/, "");
      window.location.replace(base + "/api/remote/claim?rt=" + encodeURIComponent(data.token));
    } catch (err) {
      say(err && err.message ? err.message : "Реле недоступно", "bad");
      button.disabled = false;
    }
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    connect();
  });

  var hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  var preset = normalize(hash.get("c") || "");
  if (preset) {
    input.value = pretty(preset);
    if (window.history && history.replaceState) history.replaceState(null, "", window.location.pathname);
    connect();
  } else {
    input.focus();
  }
})();
