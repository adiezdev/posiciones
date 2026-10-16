/*
 * Avisar cuando el servidor ya no es el que había al abrir la página.
 *
 * El NAS se actualiza solo con watchtower, así que una pestaña abierta se
 * queda con la versión vieja sin enterarse: sigue pidiendo endpoints que
 * a lo mejor ya no existen, y lo que ve en pantalla no es lo que hay.
 *
 * Lo que NO se hace es recargar sin más. En la página de repaso puede
 * haber correcciones a medias, y una recarga automática se las lleva por
 * delante sin que nadie pueda hacer nada. Así que cada página dice si es
 * buen momento, y si no lo es, el aviso se queda esperando con un botón
 * para guardar antes.
 */
(function (global) {
  "use strict";

  var INTERVALO = 15000;        // cada cuánto se pregunta
  var ESPERA_SEGURA = 6;        // segundos antes de recargar sola
  var ESTILO = [
    ".aviso-version{position:fixed;left:50%;bottom:22px;",
    "transform:translateX(-50%) translateY(140%);",
    "background:#1c1a17;color:#fbfaf7;padding:12px 16px;border-radius:10px;",
    "box-shadow:0 6px 24px rgba(0,0,0,.28);z-index:9999;",
    "display:flex;gap:12px;align-items:center;flex-wrap:wrap;",
    "max-width:min(560px,calc(100vw - 32px));",
    "font:14px/1.45 -apple-system,'Segoe UI',Roboto,Helvetica,sans-serif;",
    "transition:transform .28s ease,opacity .28s ease;opacity:0}",
    ".aviso-version.visible{transform:translateX(-50%) translateY(0);opacity:1}",
    ".aviso-version p{margin:0;flex:1 1 220px}",
    ".aviso-version button{font:inherit;font-weight:600;cursor:pointer;",
    "border-radius:7px;padding:6px 12px;border:1px solid rgba(251,250,247,.35);",
    "background:transparent;color:inherit}",
    ".aviso-version button.principal{background:#fbfaf7;color:#1c1a17;",
    "border-color:#fbfaf7}",
    ".aviso-version button:hover{border-color:#fbfaf7}",
    "@media (prefers-reduced-motion:reduce){.aviso-version{transition:none}}"
  ].join("");

  function estilos() {
    if (document.getElementById("aviso-version-estilo")) return;
    var e = document.createElement("style");
    e.id = "aviso-version-estilo";
    e.textContent = ESTILO;
    document.head.appendChild(e);
  }

  function boton(texto, principal, alPulsar) {
    var b = document.createElement("button");
    b.type = "button";
    b.textContent = texto;
    if (principal) b.className = "principal";
    b.addEventListener("click", alPulsar);
    return b;
  }

  function iniciar(opciones) {
    opciones = opciones || {};
    var hayCambios = opciones.hayCambios || function () { return false; };
    var guardar = opciones.guardar || null;

    var version = null;
    var avisado = false;
    var temporizador = null;
    var cuenta = null;

    function recargar() {
      // El viejo reload(true) para saltarse la caché ya no hace nada en
      // ningún navegador. No hace falta: tanto la página como los
      // estáticos se sirven con ETag, así que al recargar se revalidan y
      // llega lo nuevo.
      global.location.reload();
    }

    function mostrar() {
      estilos();
      var caja = document.createElement("div");
      caja.className = "aviso-version";
      caja.setAttribute("role", "status");
      var texto = document.createElement("p");
      caja.appendChild(texto);

      var pendiente = hayCambios();
      var restante = ESPERA_SEGURA;

      function parar() {
        if (cuenta) { clearInterval(cuenta); cuenta = null; }
      }

      if (pendiente) {
        // Hay trabajo sin guardar: aquí no se recarga nada por su cuenta.
        texto.textContent = "Hay una versión nueva. Tienes cambios sin "
          + "guardar, así que la página no se recargará sola.";
        if (guardar) {
          caja.appendChild(boton("Guardar y recargar", true, function () {
            Promise.resolve(guardar()).then(recargar, function () {
              texto.textContent = "No se han podido guardar los cambios. "
                + "Inténtalo otra vez antes de recargar.";
            });
          }));
        }
        caja.appendChild(boton("Recargar y perderlos", false, recargar));
        caja.appendChild(boton("Ahora no", false, function () {
          parar();
          caja.remove();
        }));
      } else {
        var repintar = function () {
          texto.textContent = "Hay una versión nueva. Recargando en "
            + restante + " s…";
        };
        repintar();
        caja.appendChild(boton("Recargar ahora", true, recargar));
        caja.appendChild(boton("Ahora no", false, function () {
          parar();
          caja.remove();
        }));
        cuenta = setInterval(function () {
          restante -= 1;
          // Puede haber empezado a editar mientras corría la cuenta.
          if (hayCambios()) {
            parar();
            texto.textContent = "Hay una versión nueva. Recarga cuando "
              + "termines lo que estás haciendo.";
            return;
          }
          if (restante <= 0) { parar(); recargar(); return; }
          repintar();
        }, 1000);
      }

      document.body.appendChild(caja);
      requestAnimationFrame(function () { caja.classList.add("visible"); });
    }

    function comprobar() {
      if (avisado) return;
      fetch("/api/version?t=" + Date.now(), { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d || !d.version) return;
          if (version === null) { version = d.version; return; }
          if (d.version === version) return;
          avisado = true;
          if (temporizador) clearInterval(temporizador);
          mostrar();
        })
        .catch(function () {
          // Durante el despliegue el servidor no responde unos segundos.
          // No es un error: es justo la señal de que algo está cambiando.
        });
    }

    comprobar();
    temporizador = setInterval(comprobar, INTERVALO);

    // Mirar al volver a la pestaña: es cuando más probable es que la
    // actualización haya pasado mientras no mirabas.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) comprobar();
    });
  }

  global.VigilarVersion = { iniciar: iniciar };
})(window);
