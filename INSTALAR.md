# Cómo poner esto en marcha

Al final tendrás una página web en tu red donde sueltas un PDF y te descargas
la partitura con las posiciones escritas debajo de cada nota.

Necesitas una máquina con Docker encendida. Tu NAS vale. La Raspberry Pi no:
es demasiado lenta para esto.

---

## Paso 1. Crear la carpeta y meter los archivos

En el NAS, crea una carpeta. Por ejemplo `/volume1/docker/posiciones`.

Dentro tienen que quedar exactamente así, respetando que `index.html` va
dentro de una subcarpeta llamada `static`:

```
posiciones/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── posiciones.py
├── anotar.py
├── validar.py
├── api.py
├── INSTALAR.md
└── static/
    └── index.html
```

Si `index.html` no está dentro de `static`, la web no cargará.

---

## Paso 2. Abrir una terminal en esa carpeta

Conéctate al NAS por SSH y escribe:

```bash
cd /volume1/docker/posiciones
```

Cambia la ruta si le pusiste otro nombre a la carpeta.

Para comprobar que estás en el sitio correcto:

```bash
ls
```

Tiene que salir la lista de archivos del paso 1.

---

## Paso 3. Construir

```bash
docker compose build
```

**Esto tarda mucho la primera vez.** Entre 15 y 40 minutos según la máquina y
tu conexión, porque compila Audiveris desde cero. Verás cientos de líneas
pasando. Es normal. No cierres la terminal.

Solo pasa la primera vez. Las siguientes son cuestión de segundos.

Cuando termine sin errores, verás algo como `Successfully built` o el símbolo
del sistema otra vez esperando.

---

## Paso 4. Arrancar

```bash
docker compose up -d
```

Esto devuelve el control enseguida. El contenedor queda corriendo en segundo
plano y se reinicia solo si apagas y enciendes el NAS.

---

## Paso 5. Comprobar que está vivo

```bash
curl http://localhost:8080/salud
```

Tiene que responder:

```json
{"ok":true,"audiveris":true,"mscore":true,"datos":"/datos"}
```

Los tres tienen que decir `true`. Si alguno dice `false`, baja a la sección de
problemas.

---

## Paso 6. Entrar desde el navegador

Desde cualquier ordenador o móvil de tu casa, abre:

```
http://IP-DE-TU-NAS:8080
```

Por ejemplo `http://192.168.1.50:8080`. Si no sabes la IP del NAS, en la
terminal del NAS escribe `hostname -I` y usa el primer número que salga.

Ya está. Arrastras el PDF, esperas y le das a descargar.

---

## Cómo se usa

1. Arrastra el PDF sobre el pentagrama, o pulsa encima para buscarlo.
2. Si tu trombón tiene válvula de fa, marca la casilla antes de subir.
3. Espera. Verás una bolita moviéndose por las posiciones 1 a 7 según avanza.
   Una partitura de dos páginas son unos dos o tres minutos.
4. Cuando ponga "Listo", pulsa **Descargar el PDF**.
5. Debajo te dice cuántas notas ha anotado y si hay algo raro. Si te marca
   puntos a revisar, ábrelos y mira esos compases en el original.

Puedes soltar varios PDF a la vez. Se van procesando de uno en uno.

---

## Cosas que conviene que sepas

**El PDF que sale no es tu PDF con números encima.** El programa lee tu
partitura, la entiende y la vuelve a dibujar desde cero con los números
añadidos. La música es la misma, pero la maquetación, la tipografía y los
saltos de línea serán distintos.

**Puede equivocarse.** Lee la partitura de forma automática y a veces se
confunde en una nota. Por eso te avisa de los compases sospechosos. Antes de
llevarla a un ensayo, dale un repaso.

**Los números son una propuesta.** Son la posición más cómoda teniendo en
cuenta la nota anterior y la siguiente, pero un pasaje concreto puede pedir
otra cosa.

---

## Si algo va mal

**`mscore` dice `false`.** El programa que dibuja el PDF no está donde se
espera. Entra al contenedor y búscalo:

```bash
docker compose exec posiciones bash
ls /usr/bin | grep -i score
exit
```

Si sale `musescore3` en vez de `mscore3`, abre `docker-compose.yml`, añade
debajo de `restart:` estas dos líneas y vuelve al paso 4:

```yaml
    environment:
      MSCORE_BIN: musescore3
```

**`audiveris` dice `false`.** La ruta del motor de lectura no coincide. Mira
el final del paso 3: durante la construcción salió una línea con el nombre de
una carpeta. Si no ponía `audiveris`, hay que corregir el `Dockerfile` en las
dos líneas donde aparece esa palabra en minúscula.

**No carga la web pero `curl` funciona.** Es el cortafuegos del NAS. Abre el
puerto 8080 en su panel de configuración.

**El navegador dice que no se puede conectar.** Comprueba que el contenedor
está corriendo:

```bash
docker compose ps
docker compose logs --tail 50
```

**Sube el PDF y dice "No ha salido".** El mensaje en rojo te dice el motivo.
Lo más habitual es que el PDF sea un escaneo de mala calidad o que no contenga
música reconocible.

---

## Comandos que vas a necesitar alguna vez

```bash
cd /volume1/docker/posiciones

docker compose logs -f        # ver qué está pasando (Ctrl+C para salir)
docker compose restart        # reiniciar
docker compose down           # parar
docker compose up -d          # arrancar
```

Si cambias algún archivo `.py` o el `index.html`:

```bash
docker compose build && docker compose up -d
```

Esta vez es rápido.
