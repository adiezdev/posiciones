# Corpus de pruebas

Aquí van las partituras de verdad con las que se mide el estampado. **No se
suben al repositorio**: son obras con derechos y además pesan. El
`.gitignore` las deja fuera y conserva solo este archivo.

## Cómo montarlo

Una subcarpeta por partitura:

```
corpus/
  marcha-nuestro-padre-jesus/
    partitura.pdf        # el PDF tal cual te llega
    export.mxl           # opcional: el export de Audiveris ya cacheado
    verdad.json          # opcional: solo para sintéticas
  pasodoble-escaneado/
    partitura.pdf
```

Sin `export.mxl`, `metricas.py --omr` llama a Audiveris (hace falta
`AUDIVERIS_BIN`) y lo deja cacheado. Con el export guardado, medir un cambio
tarda segundos en vez de media hora, que es lo que hace viable iterar.

## Qué conviene que haya dentro

La gracia del corpus es que cubra las formas de fallar, no que tenga muchas
partituras. Con dos o tres de cada uno de estos grupos basta:

- grabado limpio moderno (Sibelius, Finale, MuseScore)
- escaneo bueno de partitura impresa
- fotocopia de fotocopia, torcida, con manchas
- apaisada, y vertical
- con `/Rotate` puesto por el escáner
- **particella en Sib clave de sol**, que hoy da números mal sin avisar
- con cambio a clave de do en 4ª a mitad de obra
- con casillas de 1ª y 2ª vez, codas y saltos
- una de muchas páginas, para ver si Audiveris parte el libro

## Cómo se usa

```bash
python metricas.py corpus --json informe.json          # medir
python metricas.py corpus --base informe.json          # comparar tras tocar algo
python metricas.py corpus --debug                      # + PDF de diagnóstico por caso
python depurar.py corpus/una/partitura.pdf corpus/una/export.mxl   # mirar una
```

Sin `verdad.json` no hay error en puntos que medir: las columnas que quedan
son la cobertura (cuántas notas del OMR acaban con número) y el PDF de
diagnóstico, que es donde se ve lo que ha pasado.
