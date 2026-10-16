# Cómo se trabaja en esto

Este proyecto va de colocar un número debajo de cada nota de un PDF que
nadie ha escrito pensando en nosotros. Casi todo son heurísticas, y con
heurísticas hay una regla que no se puede saltar: **antes de tocar un
umbral, hay que poder medir si el cambio mejora o empeora.** Durante mucho
tiempo no se podía, y el historial del repositorio lo enseña: el mismo
parámetro sube y baja cuatro veces en commits seguidos, porque cada arreglo
se validaba abriendo un PDF y mirándolo, y arreglar una partitura rompía
otra sin que nadie se enterara.

Esto es lo que hay ahora para no volver a eso.

## El ciclo

```bash
python pruebas/correr.py          # ¿he roto algo?
# ...tocas lo que sea...
python pruebas/correr.py          # ¿lo he roto ahora?
```

Sale 1 si algo ha empeorado respecto a `pruebas/base.json`, y dice qué y
cuánto. La CI lo corre en cada push.

Cuando una mejora es de verdad —la has medido y sabes por qué sube— se
actualiza la referencia:

```bash
python pruebas/correr.py --actualizar-base
git diff pruebas/base.json        # mirarlo antes de commitear
```

Actualizar la base sin mirar el diff deja la red sin sentido.

## Las piezas

### `pruebas/sintetico.py` — partituras con verdad conocida

Fabrica un PDF, el MusicXML que Audiveris habría exportado de él y un
`verdad.json` con la posición exacta de cada cabeza en puntos. Como sabemos
la respuesta, se puede medir el error en puntos en vez de opinar.

Y sabe estropearlas a propósito. Cada defecto reproduce un fallo visto en
PDFs reales:

```bash
python pruebas/sintetico.py /tmp/caso --rotate 90      # escáner que gira la hoja
python pruebas/sintetico.py /tmp/caso --cropbox 18     # CropBox != MediaBox
python pruebas/sintetico.py /tmp/caso --sin-dx 0.5     # Audiveris sin coordenadas
python pruebas/sintetico.py /tmp/caso --sistemas 6     # página apretada
```

La geometría del generador se deriva aparte de la del código que prueba
(`superponer.a_hoja`), a propósito: si se copiara, los dos se equivocarían
igual y la prueba no se enteraría.

### `metricas.py` — la báscula

```bash
python metricas.py --sinteticos                   # batería estándar, sin Audiveris
python metricas.py corpus --omr --debug           # partituras de verdad
python metricas.py --sinteticos --base pruebas/base.json
```

Columnas:

| columna | qué es |
|---|---|
| `notas` | notas en el MusicXML (más las que se hayan rescatado de la imagen) |
| `estamp` | números que acaban dibujados |
| `cob%` | `estamp / notas`. Lo que baja cuando "se come notas" |
| `desc` | números calculados y luego tirados |
| `err_med`, `err_p95` | distancia del número a su cabeza, en puntos |
| `ok%` | notas con su número a menos de medio espacio |
| `pent` | páginas con los pentagramas medidos / páginas con números |
| `modo` | respecto a qué interpreta el `default-x` |
| `enc%` | fracción de números que caen dentro de la caja de la página |

`pent` es la columna que más avisa: cuando una página no mide bien sus
pentagramas, cae a la escala que declara Audiveris —que se desvía hasta un
10 %— y a partir de ahí todo lo demás va mal.

### `depurar.py` — ver qué ha visto la máquina

```bash
python depurar.py partitura.pdf export.mxl -o diagnostico.pdf
```

Pinta encima del original todo lo que ha medido: los pentagramas que ha
encontrado, las cabezas que reconoce, dónde ha puesto cada número, cuáles ha
reajustado y de dónde venían, y cuáles ha descartado y en qué sitio. Con
`--verdad` añade la respuesta correcta en magenta.

Cuando falta un número, esto responde en cinco segundos a la pregunta de
siempre: ¿no lo leyó Audiveris, o lo leyó y nos lo hemos comido nosotros?

Si `preparar()` se rinde, dice el motivo exacto en vez de devolver un `None`
mudo: `varias_partes:2`, `encaje_bajo:0.42(sistema)`,
`ninguna_nota_trae_default_x`...

### `corpus/` — las partituras de verdad

Fuera del repositorio (tienen derechos y pesan). Ver `corpus/README.md`.

## Estado medido hoy

Batería sintética, 1.421 notas:

| | |
|---|---|
| geometría (girada 90/180/270, CropBox recortada, página apretada, una sola página) | **100 % de cobertura, 100 % bien situadas** |
| 20 % de notas sin `default-x` | 92 % de cobertura, 95 % bien situadas |
| 50 % de notas sin `default-x` | **75 % de cobertura, 56 % bien situadas** |

La última fila es el número a batir, y es el mecanismo real de "se come
notas": cuando Audiveris no da la coordenada de una nota, hoy se estima
interpolando linealmente por tiempo entre la primera y la última nota del
sistema con coordenada. En un sistema con notas repartidas de forma desigual
eso se desvía mucho, el número cae lejos de su cabeza, y el filtro de
deduplicación acaba tirándolo.

Dos formas de atacarlo, en orden de interés:

1. **Leer el `.omr` de Audiveris** en vez de reconstruir coordenadas desde
   el MusicXML. Ese archivo trae la caja de cada cabeza en píxeles de hoja y
   las cinco líneas de cada pentagrama punto a punto. No hay nada que
   estimar, y se van unas 900 líneas de heurística.
2. Mientras tanto, estimar la `x` que falta **por compás** en vez de por
   sistema: la barra de compás y su anchura sí se conocen siempre, así que
   el error se acota a un compás en lugar de a un sistema entero.
