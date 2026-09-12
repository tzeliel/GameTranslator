# Game Translator — traductor/parcheador automático de diálogos de videojuegos

Detecta los archivos de texto/diálogo de la carpeta de un juego, los traduce
y parchea el juego para jugar en español: **o bien añade español como idioma
nuevo** (si el juego ya soporta varios idiomas), **o bien sustituye el texto
original directamente** (con copia de seguridad automática), según lo que dé
menos problemas en cada caso concreto.

## Instalación

```bash
pip install -r requirements.txt
```

En Linux, si no tienes selector de carpeta gráfico:
```bash
sudo apt install python3-tk
```

## Uso rápido

```bash
# Interactivo: te abre un selector de carpeta
python game_translator.py

# Directo, apuntando a la carpeta del juego
python game_translator.py --game-dir "C:\Games\Zarya-1"

# SIEMPRE recomendado la primera vez: simula sin escribir nada
python game_translator.py --game-dir "C:\Games\Zarya-1" --dry-run

# Deshacer un parcheo in-place anterior (restaura los .bak)
python game_translator.py --game-dir "C:\Games\Zarya-1" --restore
```

La primera vez que traduzcas un par de idiomas (p. ej. inglés→español) con el
motor por defecto (`argos`), se descargará el modelo de traducción — eso sí
necesita internet una vez. Después funciona 100% offline.

## Qué hace exactamente

1. **Escanea** recursivamente la carpeta del juego buscando formatos de texto
   reconocibles (ver lista abajo), ignorando `node_modules`, `.git`, cachés de
   shaders, etc.
2. **Extrae solo texto "humano"**: usa heurísticas (longitud, ratio de letras,
   detecta IDs tipo `NPC_001` o `start_game`, rutas de archivo, colores hex...)
   para no tocar claves técnicas, ni corromper el archivo.
3. **Detecta el idioma de origen** automáticamente (o se lo indicas con
   `--source-lang en`).
4. **Traduce** con el motor que elijas (`--engine argos|http|noop`).
5. **Decide cómo parchear**:
   - Si detecta carpetas o columnas por idioma (`en/`, `Localization/english`,
     `tl/english`, una columna `EN` en un CSV...) → **modo `add-language`**:
     duplica esa estructura como español, **sin tocar los archivos
     originales**. Es el modo más seguro y el que se usa por defecto cuando
     es posible.
   - Si no hay ninguna estructura de idiomas (el texto está embebido
     directamente, como en muchos RPG Maker sin plugin de idiomas) → **modo
     `patch-inplace`**: sobreescribe esos archivos, pero antes crea siempre
     una copia `archivo.ext.bak` y un manifiesto (`.game_translator_manifest.json`)
     para poder deshacerlo con `--restore`.
   - Puedes forzar uno u otro con `--mode add-language` / `--mode patch-inplace`.
6. **Cachea traducciones** (`.game_translator_cache.json`) para no repetir
   trabajo si vuelves a ejecutar la herramienta (frases repetidas en diálogos
   son muy comunes) y para poder reanudar si se corta a mitad.

## Formatos soportados

| Motor / formato                         | Extensión / patrón                          |
|------------------------------------------|----------------------------------------------|
| RPG Maker MV/MZ                          | `data/*.json`, `www/data/*.json` (Map*.json, CommonEvents.json, Actors.json...) |
| Ren'Py                                   | `*.rpy` (diálogo directo y esqueletos `translate <idioma> ... old/new`) |
| Unity — I2 Localization / similares      | `*.csv`, `*.tsv` con columnas por idioma      |
| Localización JSON genérica               | `*.json` (clave: texto, incluso anidado)      |
| .NET / Unity resources                   | `*.resx`                                      |
| gettext                                  | `*.po`, `*.pot`                               |
| INI / key=value                          | `*.ini`, `*.lang`                             |
| Texto plano / Yarn Spinner / Ink         | `*.txt`, `*.yarn`, `*.ink`                    |

## Ejemplos con los juegos que mencionas

- **Aliya**, **Zarya-1**: si son (o parecen) RPG Maker MV/MZ, sus diálogos
  están casi seguro en `www/data/Map*.json` y `www/data/CommonEvents.json` —
  la herramienta los detecta automáticamente por ruta y estructura. Al no
  tener carpetas de idioma, normalmente caerán en `patch-inplace` (con backup).
- **Eclipse: Special Forces**: si usa Unity, revisa si trae una carpeta tipo
  `StreamingAssets/Localization` con CSV — en ese caso irá por `add-language`
  añadiendo una columna `ES`. Si el texto está embebido en escenas de Unity
  (`.assets`/`.bundle`), esta herramienta **no puede tocarlo**: eso requiere
  un extractor específico de Unity (AssetStudio, UABE) que la exporte primero
  a un formato de texto plano/JSON, y luego sí puedes pasarle esos archivos
  extraídos a esta herramienta.
- Si el juego usa **Ren'Py** y el launcher del propio juego trae la opción
  "Generate Translations" (o ya existe `game/tl/<idioma>/`), genera primero el
  esqueleto en `tl/spanish` desde el propio juego/SDK y luego ejecuta esta
  herramienta — rellenará automáticamente los `new ""` vacíos.

## Lo que esta herramienta NO puede hacer (y por qué)

Sé honesto conmigo mismo (y contigo): **no existe un método universal** que
funcione con el 100% de los juegos, porque no todos guardan el texto igual:

- **Archivos binarios/serializados propietarios** (Unity `.assets`, `.bundle`,
  `.unity3d`; RPG Maker VX/Ace `.rgss`/`.rgssad` empaquetados; motores
  propios con formatos a medida) — necesitan primero un extractor específico
  de ese motor/empaquetado. Esta herramienta trabaja sobre texto plano/JSON/
  XML/CSV, no sobre binarios.
- **Juegos con DRM o que prohíben la modificación de sus archivos en el
  EULA** — parchear esos recursos puede incumplir los términos de servicio
  del juego; revisa la licencia antes de aplicar el parche.
- **Calidad de la traducción automática**: un traductor automático (offline
  u online) no sustituye una traducción humana — hará el idioma jugable, pero
  pueden quedar frases raras, sobre todo con juegos de palabras o jerga.
- La extracción de "qué es diálogo y qué no" es **heurística**: revisa el
  resultado (usa `--dry-run` primero) antes de dar por bueno un parcheo
  masivo, especialmente en RPG Maker, donde algunos códigos de evento pueden
  variar entre plugins.

## Flujo recomendado paso a paso

1. Haz una copia completa de la carpeta del juego (por si acaso).
2. `python game_translator.py --game-dir "<carpeta>" --dry-run` y revisa el
   resumen (cuántos archivos detecta, de qué tipo).
3. Ejecuta sin `--dry-run`.
4. Prueba el juego. Si algo no cuadra, `--restore` deshace un `patch-inplace`
   (el modo `add-language` nunca toca los originales, así que no hace falta
   restaurar nada: basta con borrar la carpeta de idioma generada).
