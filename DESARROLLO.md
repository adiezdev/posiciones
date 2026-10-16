# Cómo se trabaja en esto

Este proyecto va de colocar un número debajo de cada nota de un PDF que
nadie ha escrito pensando en nosotros. Casi todo son heurísticas, y con
heurísticas hay una regla que no se puede saltar: **antes de tocar un
umbral, hay que poder medir si el cambio mejora o empeora.** Durante mucho
tiempo no se podía, y el historial lo enseña: el mismo parámetro sube y baja
cuatro veces en commits seguidos, porque cada arreglo se validaba abriendo
un PDF y mirándolo, y arreglar una partitura rompía otra sin que nadie se
enterara.

## El ciclo

```bash
python pruebas/correr.py          # ¿he roto algo?
# ...tocas lo que sea...
python pruebas/correr.py          # ¿lo he roto ahora?
```

Sale 1 si algo ha empeorado respecto a `pruebas/base.json`, y dice qué y
cuánto. La CI lo corre en cada push. Cuando una mejora es de verdad —la has
medido y sabes por qué sube— se actualiza la referencia:

```bash
python pruebas/correr.py --actualizar-base
git diff pruebas/base.json        # mirarlo antes de commitear
```

Actualizar la base sin mirar el diff deja la red sin sentido.

## El camino de un PDF

```
tu PDF
  │
  ├─ normalizar.py   descifra y endereza si es un escaneo torcido
  │
  ├─ Audiveris       -batch -export -save  →  MusicXML + .omr
  │
  ├─ omr.py          lee del .omr la geometría que Audiveris ya midió
  │
  ├─ superponer.py   coloca cada número y lo estampa sobre tu PDF
  │                  (si no puede, se redibuja con MuseScore: vía B)
  │
  └─ /revisar        repaso a mano de lo que haya quedado mal
```

## Las piezas

### `pruebas/sintetico.py` — partituras con verdad conocida

Fabrica un PDF, el MusicXML que Audiveris habría exportado de él y un
`verdad.json` con la posición exacta de cada cabeza en puntos. Como sabemos
la respuesta, se mide el error en puntos en vez de opinar.

Y sabe estropearlas a propósito. Cada defecto reproduce un fallo visto en
PDFs reales:

```bash
python pruebas/sintetico.py /tmp/caso --rotate 90          # escáner que gira la hoja
python pruebas/sintetico.py /tmp/caso --cropbox 18         # CropBox != MediaBox
python pruebas/sintetico.py /tmp/caso --sin-dx 0.5         # Audiveris sin coordenadas
python pruebas/sintetico.py /tmp/caso --inclinar 1.4       # fotocopia torcida
python pruebas/sintetico.py /tmp/caso --borde-negro        # tapa del escáner abierta
python pruebas/sintetico.py /tmp/caso --clave sol --transposicion sib
python pruebas/sintetico.py /tmp/caso --cabezas-texto      # cabezas como glifo
```

Dos decisiones de diseño que conviene no deshacer:

- **La geometría del generador se deriva aparte** de la del código que
  prueba (`superponer.a_hoja`, `superponer._midi_en_clave`). Si se copiara,
  los dos se equivocarían igual y la prueba no se enteraría.
- **Los ritmos van mezclados y el espaciado sigue una ley de potencia**,
  como el de un grabador. Con todo negras, tiempo y distancia son
  proporcionales y cualquier estimador acierta por casualidad.

### `metricas.py` — la báscula

```bash
python metricas.py --sinteticos                   # batería estándar, sin Audiveris
python metricas.py corpus --omr --debug           # partituras de verdad
python metricas.py --sinteticos --base pruebas/base.json
```

| columna | qué es |
|---|---|
| `notas` | notas en el MusicXML, más las rescatadas de la imagen |
| `estamp` | números que acaban dibujados |
| `cob%` | `estamp / notas`. Lo que baja cuando "se come notas" |
| `dud` | números con poca certeza: se estampan en gris |
| `err_med`, `err_p95` | distancia del número a su cabeza, en puntos |
| `ok%` | notas con su número a menos de medio espacio |
| `num%` | números que son una posición posible para la nota **que suena** |
| `incl` | inclinación detectada y corregida, en grados |
| `pent` | páginas con pentagramas medidos / páginas con números |
| `geom` | de dónde sale la geometría: del `.omr` o de mirar la imagen |

`ok%` y `num%` miden cosas distintas y las dos hacen falta. Un número puede
estar perfectamente colocado debajo de su nota y ser el número equivocado,
que es lo que pasaba con las particellas en Sib.

`pent` es la que más avisa: una página que pierde sus pentagramas cae a la
escala que declara Audiveris, que se desvía hasta un 10 %.

### `depurar.py` — ver qué ha visto la máquina

```bash
python depurar.py partitura.pdf export.mxl -o diagnostico.pdf
python depurar.py partitura.pdf export.mxl --omr libro.omr --verdad verdad.json
```

Pinta encima del original los pentagramas encontrados, las cabezas
reconocidas, dónde ha ido cada número, cuáles se reajustaron y de dónde
venían, y cuáles se descartaron. Responde en cinco segundos a la pregunta de
siempre: **¿esta nota no la leyó Audiveris, o la leyó y nos la comimos
nosotros?**

Si `preparar()` se rinde, dice el motivo en vez de un `None` mudo:
`encaje_bajo:0.42(sistema)`, `ninguna_nota_trae_default_x`,
`varias_partes_con_musica:2`...

### `omr.py` — la geometría que Audiveris ya conoce

Audiveris con `-save` deja un `.omr`: un ZIP con un `sheet#N.xml` por hoja
donde está cada pentagrama con sus líneas y cada cabeza con su caja, en
píxeles. Es lo que `superponer.py` se pasa mil líneas intentando deducir.

**Este esquema no está documentado**: sale de las anotaciones JAXB del
código de Audiveris 5.10.2 y puede cambiar entre versiones. Por eso `omr.py`
lee de forma tolerante, valida lo leído y devuelve `None` a la mínima; y
`superponer` lo vuelve a comprobar contra la página antes de usarlo. Si el
formato cambia, se mide la imagen como siempre.

Para comprobarlo contra tu Audiveris:

```bash
python depurar.py partitura.pdf export.mxl --omr libro.omr
```

### `/revisar` — el repaso a mano

Ningún reconocimiento óptico acierta el cien por cien. Lo que sí se puede
garantizar es que repasar sea barato: la partitura con los números encima,
pinchas uno para cambiarlo, pinchas en una nota sin número para añadirlo, y
se vuelve a generar el PDF. **Lo que no se toca sale exactamente igual**,
porque la hoja de marcas son coordenadas ya resueltas y no se recalcula
nada.

Los números en gris son los que la herramienta ha colocado con menos
certeza: son los que conviene mirar primero.

### `corpus/` — las partituras de verdad

Fuera del repositorio (tienen derechos y pesan). Ver `corpus/README.md`.

## Estado medido

Batería sintética, 21 casos, unas 2.700 notas: **100 % de cobertura** y
**99,7 % de notas bien situadas**, salvo el caso `borde-negro` sin `.omr`,
que es justo la demostración de para qué sirve el `.omr`.

Lo que movió cada fase, medido:

| | antes | después |
|---|---|---|
| 50 % de notas sin `default-x`, bien situadas | 56 % | 99 % |
| 80 % de notas sin `default-x`, bien situadas | 37 % | 98 % |
| Franja negra de escáner, bien situadas | 10 % | 100 % (con `.omr`) |
| Particella en Sib clave de sol, números correctos | 21 % | 100 % |
| Escaneo torcido 1,4°, pentagramas detectados | 0 de 4 | 4 de 4 |
| Cabezas de texto, error p95 | — | 0,02 pt |

## Lo que esto NO prueba

Conviene tenerlo presente para no fiarse de más:

- **El sintético es demasiado limpio.** Líneas rectas, sin ruido, sin
  manchas, sin ligaduras ni matices. Valida la aritmética —reconstrucción de
  coordenadas, giros, CropBox, claves, calibración— que es donde estaban los
  bugs, pero no la visión sobre una fotocopia de verdad. Para eso hace falta
  el corpus.
- **La geometría que declara el sintético es exacta.** Cuando la detección
  de pentagramas falla, el respaldo acierta igualmente y el fallo no se ve en
  `ok%`. Por eso `metricas.py` vigila `pent` aparte: en una partitura real
  ese respaldo se desvía hasta un 10 %.
- **El lector del `.omr` se prueba contra un `.omr` que escribimos
  nosotros.** Ejercita el ZIP, la lectura tolerante, la validación y el paso
  de píxeles a puntos, pero no confirma que el esquema sea el que escribe tu
  Audiveris. Eso solo lo dice `depurar.py --omr` sobre un archivo real.
- **La vía B (redibujar con MuseScore) casi no está medida.** Necesita
  MuseScore y music21, que la batería no instala a propósito para que la CI
  tarde segundos.

## Lo que queda

- Acordes: hoy se anota solo la nota más aguda. Apilar los dos números.
- Alteraciones accidentales dentro del compás, no solo la armadura, al leer
  de la imagen las notas que el OMR no vio.
- `_fantasmas` se apaga cuando encuentra demasiadas notas candidatas, que es
  justo cuando más falta haría. Es un compromiso deliberado —meter números
  falsos es peor que no meter ninguno— pero con el `.omr` debería dejar de
  hacer falta del todo.
- Procesar las hojas en paralelo: hoy un PDF de veinte páginas bloquea la
  cola entera.
