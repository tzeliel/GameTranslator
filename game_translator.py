#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game_translator.py
===================

Herramienta para detectar automáticamente archivos de diálogo/texto de un
videojuego, traducirlos y parchear el juego para añadir (o sustituir por)
español.

IMPORTANTE - LEE ESTO PRIMERO
------------------------------
No existe un método 100% universal para "cualquier juego": cada motor
(RPG Maker, Ren'Py, Unity, Godot, motores propios...) guarda el texto de
forma distinta, y algunos lo empaquetan en formatos binarios/cifrados que
esta herramienta NO puede tocar sin un extractor específico de ese motor.

Lo que SÍ hace esta herramienta, de forma real y probada:
  - Escanea recursivamente la carpeta del juego.
  - Detecta y parsea los formatos de texto/diálogo más comunes en juegos
    indie y AA (ver lista de formatos soportados más abajo).
  - Extrae SOLO las cadenas que parecen texto humano (heurística de
    idioma/longitud/puntuación), dejando intactos IDs, claves, rutas,
    números, tags de formato, etc.
  - Traduce con un motor de traducción automática configurable:
      * Offline (por defecto): Argos Translate (sin coste, sin API key,
        pero requiere descargar el modelo de idioma la primera vez, lo
        cual SÍ necesita conexión a internet una única vez).
      * Online (opcional): un backend HTTP simple (LibreTranslate propio,
        DeepL, etc.) si le pasas --engine http --http-endpoint ...
  - Decide automáticamente el modo de parcheo:
      * "add-language": si detecta estructura de carpetas/columnas por
        idioma (ej. tl/english, Localization/en, i18n/en.json, columnas
        CSV "EN"), DUPLICA esa estructura como español, sin tocar el
        original. Es el modo "que menos problemas da".
      * "patch-inplace": si no hay estructura de idiomas (el texto está
        embebido directamente en el único set de archivos del juego),
        sobreescribe esos archivos generando SIEMPRE una copia de
        seguridad (.bak) y un manifiesto para poder deshacer el cambio.
  - Cachea traducciones para no repetir trabajo y poder reanudar si se
    corta a mitad.

Formatos soportados out-of-the-box
-----------------------------------
  - RPG Maker MV/MZ            (data/*.json: Map*.json, CommonEvents.json,
                                 Actors.json, Items.json, System.json, etc.)
  - Ren'Py                     (*.rpy, bloques `translate <lang> ...`)
  - Unity I2 Localization      (CSV con columnas de idioma)
  - JSON de localización       (genérico: {"clave": "texto"} o anidado)
  - .resx (.NET)                (<data name="..."><value>texto</value></data>)
  - gettext                    (*.po / *.pot)
  - INI / .lang key=value      (clave=texto)
  - Texto plano / Yarn / Ink   (*.txt, *.yarn, *.ink) con heurística de línea

Instalación
-----------
    pip install argostranslate langdetect chardet tqdm

Uso
---
    # Modo interactivo (te pide la carpeta con un selector gráfico):
    python game_translator.py

    # Modo directo:
    python game_translator.py --game-dir "C:\\Juegos\\Zarya-1" --target es

    # Simulación sin escribir nada (recomendado la primera vez):
    python game_translator.py --game-dir "./MiJuego" --dry-run

    # Forzar modo de parcheo:
    python game_translator.py --game-dir "./MiJuego" --mode patch-inplace
    python game_translator.py --game-dir "./MiJuego" --mode add-language

    # Deshacer un parcheo in-place anterior:
    python game_translator.py --game-dir "./MiJuego" --restore

Autor: generado para uso personal / fan-translation. Respeta siempre los
términos de licencia y EULA del juego que estés modificando: parchear
archivos de un juego con DRM, empaquetado propietario o que prohíba
expresamente la modificación de sus recursos puede violar sus términos de
servicio. Esta herramienta no rompe cifrados ni DRM.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

# --------------------------------------------------------------------------
# Dependencias opcionales: el script debe poder arrancar sin ellas para dar
# un mensaje de error claro en vez de un traceback feo.
# --------------------------------------------------------------------------
try:
    import chardet
except ImportError:
    chardet = None

try:
    from langdetect import detect as _langdetect_detect
    from langdetect import DetectorFactory
    DetectorFactory.seed = 0
except ImportError:
    _langdetect_detect = None

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable=None, total=None, desc=None, **kwargs):
        # Fallback minimalista si no está tqdm instalado.
        if iterable is None:
            return _DummyBar(total, desc)
        return _iter_with_progress(iterable, total, desc)

    class _DummyBar:
        def __init__(self, total, desc):
            self.total = total
            self.n = 0
            self.desc = desc or ""
        def update(self, n=1):
            self.n += n
            print(f"\r{self.desc}: {self.n}/{self.total or '?'}", end="", flush=True)
        def close(self):
            print()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self.close()

    def _iter_with_progress(iterable, total, desc):
        items = list(iterable)
        bar = _DummyBar(total or len(items), desc)
        for it in items:
            yield it
            bar.update(1)
        bar.close()


LOG_PREFIX = "[game-translator]"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}")


# ==========================================================================
# 1. SELECCIÓN DE CARPETA
# ==========================================================================

def pick_folder_gui() -> Optional[str]:
    """Abre un selector de carpetas nativo (Tkinter). Devuelve None si no
    hay entorno gráfico disponible o el usuario cancela."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None

    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Selecciona la carpeta del videojuego")
        root.destroy()
        return folder or None
    except Exception:
        return None


def resolve_game_dir(cli_arg: Optional[str]) -> Path:
    if cli_arg:
        p = Path(cli_arg).expanduser().resolve()
    else:
        picked = pick_folder_gui()
        if picked:
            p = Path(picked).expanduser().resolve()
        else:
            raw = input("Ruta de la carpeta del juego: ").strip().strip('"')
            p = Path(raw).expanduser().resolve()

    if not p.exists() or not p.is_dir():
        log(f"ERROR: '{p}' no es una carpeta válida.")
        sys.exit(1)
    return p


# ==========================================================================
# 2. HEURÍSTICA DE "¿ES TEXTO TRADUCIBLE?"
# ==========================================================================

_CODE_LIKE_RE = re.compile(r"^[\\/\.\w\-\:\{\}\[\]<>@#$%^&*+=~`|]+$")
_HAS_LETTER_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")
_ALL_CAPS_ID_RE = re.compile(r"^[A-Z0-9_]{2,}$")
_SNAKE_OR_CAMEL_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*(_[a-zA-Z0-9]+)+$")  # start_game, npc_01_intro
_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}|%\w|<[^<>]+>|\[[^\[\]]+\]")


def looks_like_translatable_text(value: str, min_len: int = 2) -> bool:
    """Heurística: True si 'value' parece texto humano y no un identificador,
    ruta de archivo, número, color hexadecimal, clave técnica, etc."""
    if not isinstance(value, str):
        return False
    s = value.strip()
    if len(s) < min_len:
        return False
    if not _HAS_LETTER_RE.search(s):
        return False
    if _ALL_CAPS_ID_RE.match(s):
        return False
    if " " not in s and _SNAKE_OR_CAMEL_ID_RE.match(s):
        # Identificador tipo "start_game", "npc_01_intro": sin espacios y con
        # guion bajo -> casi seguro es una clave técnica, no diálogo real.
        return False
    # Quita placeholders tipo {0} <b> %s [color] antes de evaluar la letra/ratio
    stripped = _PLACEHOLDER_RE.sub(" ", s)
    if not _HAS_LETTER_RE.search(stripped):
        return False
    letters = sum(ch.isalpha() for ch in stripped)
    if letters / max(len(stripped), 1) < 0.35:
        return False
    # Rutas de archivo o extensiones típicas de asset
    if re.match(r"^[\w\-/\\]+\.(png|jpg|jpeg|ogg|wav|mp3|json|xml|ttf|otf|prefab|mat|shader)$", s, re.I):
        return False
    # Números / coordenadas / colores
    if re.match(r"^[\d\.\-,\s#xXA-Fa-f]+$", s):
        return False
    return True


# ==========================================================================
# 3. TRADUCTOR (backend intercambiable)
# ==========================================================================

class TranslationCache:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}
        self._dirty = 0

    def key(self, text: str, src: str, tgt: str) -> str:
        h = hashlib.sha256(f"{src}|{tgt}|{text}".encode("utf-8")).hexdigest()
        return h

    def get(self, text: str, src: str, tgt: str) -> Optional[str]:
        return self.data.get(self.key(text, src, tgt))

    def set(self, text: str, src: str, tgt: str, translated: str) -> None:
        self.data[self.key(text, src, tgt)] = translated
        self._dirty += 1
        if self._dirty >= 25:
            self.flush()

    def flush(self) -> None:
        if self._dirty:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=0), encoding="utf-8")
            self._dirty = 0


class Translator:
    """Interfaz común. translate() debe devolver la cadena traducida o la
    original si falla (nunca debe lanzar y tirar todo el proceso abajo)."""

    def __init__(self, target_lang: str = "es"):
        self.target_lang = target_lang

    def translate(self, text: str, source_lang: str) -> str:
        raise NotImplementedError

    def detect_language(self, sample_text: str) -> str:
        if _langdetect_detect is None:
            return "en"  # por defecto asumimos inglés, el caso más común
        try:
            return _langdetect_detect(sample_text)
        except Exception:
            return "en"


class ArgosTranslator(Translator):
    """Traducción offline con Argos Translate. Descarga el modelo de idioma
    la primera vez que se usa un par de idiomas (requiere internet solo
    para esa descarga inicial; después funciona sin conexión)."""

    def __init__(self, target_lang: str = "es"):
        super().__init__(target_lang)
        try:
            import argostranslate.package
            import argostranslate.translate
            self._pkg = argostranslate.package
            self._tr = argostranslate.translate
        except ImportError as e:
            raise RuntimeError(
                "Falta 'argostranslate'. Instálalo con: pip install argostranslate"
            ) from e
        self._ensured_pairs: set[tuple[str, str]] = set()

    def _ensure_language_pair(self, source_lang: str) -> None:
        pair = (source_lang, self.target_lang)
        if pair in self._ensured_pairs:
            return
        installed = self._tr.get_installed_languages()
        from_ok = any(l.code == source_lang for l in installed)
        to_ok = any(l.code == self.target_lang for l in installed)
        if not (from_ok and to_ok):
            log(f"Descargando modelo de traducción {source_lang}->{self.target_lang} "
                f"(solo la primera vez, requiere internet)...")
            self._pkg.update_package_index()
            available = self._pkg.get_available_packages()
            match = next(
                (p for p in available
                 if p.from_code == source_lang and p.to_code == self.target_lang),
                None,
            )
            if match is None:
                raise RuntimeError(
                    f"No hay modelo Argos disponible para {source_lang}->{self.target_lang}. "
                    f"Prueba a especificar --source-lang manualmente (en, fr, de, ja, etc.)."
                )
            download_path = match.download()
            self._pkg.install_from_path(download_path)
        self._ensured_pairs.add(pair)

    def translate(self, text: str, source_lang: str) -> str:
        if source_lang == self.target_lang:
            return text
        try:
            self._ensure_language_pair(source_lang)
            installed = self._tr.get_installed_languages()
            src = next(l for l in installed if l.code == source_lang)
            tgt = next(l for l in installed if l.code == self.target_lang)
            translation = src.get_translation(tgt)
            return translation.translate(text)
        except Exception as e:
            log(f"  ! Aviso: no se pudo traducir '{text[:40]}...': {e}")
            return text


class HttpTranslator(Translator):
    """Backend HTTP genérico (por ejemplo un servidor LibreTranslate propio,
    o un proxy a DeepL/Google que tú mismo montes). Espera un endpoint que
    reciba JSON {q, source, target} y devuelva {translatedText}."""

    def __init__(self, target_lang: str, endpoint: str, api_key: Optional[str] = None):
        super().__init__(target_lang)
        self.endpoint = endpoint
        self.api_key = api_key
        import urllib.request
        self._urllib = urllib.request

    def translate(self, text: str, source_lang: str) -> str:
        if source_lang == self.target_lang:
            return text
        try:
            payload = json.dumps({
                "q": text, "source": source_lang, "target": self.target_lang,
                "api_key": self.api_key,
            }).encode("utf-8")
            req = self._urllib.Request(
                self.endpoint, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with self._urllib.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("translatedText", text)
        except Exception as e:
            log(f"  ! Aviso: fallo HTTP traduciendo '{text[:40]}...': {e}")
            return text


class NoopTranslator(Translator):
    """Backend de prueba: no traduce, solo marca el texto. Útil para probar
    el pipeline de extracción/parcheo sin depender de ningún motor real."""

    def translate(self, text: str, source_lang: str) -> str:
        if source_lang == self.target_lang:
            return text
        return f"[ES] {text}"


def build_translator(engine: str, target_lang: str, http_endpoint: Optional[str],
                      http_key: Optional[str]) -> Translator:
    if engine == "argos":
        return ArgosTranslator(target_lang)
    if engine == "http":
        if not http_endpoint:
            raise SystemExit("--engine http requiere --http-endpoint")
        return HttpTranslator(target_lang, http_endpoint, http_key)
    if engine == "noop":
        return NoopTranslator(target_lang)
    raise SystemExit(f"Motor de traducción desconocido: {engine}")


# ==========================================================================
# 4. DETECCIÓN DE ARCHIVOS Y ESTRUCTURA DE IDIOMAS
# ==========================================================================

LANGUAGE_FOLDER_HINTS = {
    "en": ["en", "english", "eng"], "es": ["es", "spanish", "espanol", "español"],
    "fr": ["fr", "french", "francais"], "de": ["de", "german", "deutsch"],
    "it": ["it", "italian", "italiano"], "pt": ["pt", "portuguese", "portugues"],
    "ja": ["ja", "jp", "japanese"], "ko": ["ko", "kr", "korean"],
    "ru": ["ru", "russian"], "zh": ["zh", "cn", "chinese"],
}
_ALL_LANG_HINT_NAMES = {n for names in LANGUAGE_FOLDER_HINTS.values() for n in names}

# Ren'Py identifica sus carpetas de idioma por el nombre completo en inglés
# (game/tl/spanish, game/tl/french...), no por el código ISO. El resto de
# formatos (JSON, CSV, .resx...) sí suelen usar códigos cortos ("es", "fr").
RENPY_LANG_FOLDER_NAMES = {
    "es": "spanish", "en": "english", "fr": "french", "de": "german",
    "it": "italian", "pt": "portuguese", "ja": "japanese", "ko": "korean",
    "ru": "russian", "zh": "chinese",
}


def target_folder_name(kind: str, target_lang: str) -> str:
    if kind == "renpy":
        return RENPY_LANG_FOLDER_NAMES.get(target_lang, target_lang)
    return target_lang

DIALOGUE_EXTENSIONS = {
    ".json", ".xml", ".resx", ".csv", ".tsv", ".po", ".pot",
    ".ini", ".lang", ".txt", ".yarn", ".ink", ".rpy",
}

IGNORE_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".svn", "cache",
    "shadercache", "shader cache", "crashreports",
}

# Extensiones que casi seguro NO son diálogo aunque coincidan por descuido.
IGNORE_FILE_HINTS = re.compile(
    r"(package(-lock)?\.json$|tsconfig|\.eslintrc|manifest\.json$|"
    r"project\.json$|\.csproj|appsettings\.json$)", re.I,
)


@dataclass
class ScannedFile:
    path: Path
    kind: str            # "rpgmaker_json" | "renpy" | "i2_csv" | "json" | ...
    lang_hint: Optional[str] = None        # código de idioma (es/en/fr...) detectado
    lang_hint_folder: Optional[str] = None  # nombre literal de la carpeta ("english")
    confidence: float = 0.5


def _dir_is_ignored(dirname: str) -> bool:
    return dirname.lower() in IGNORE_DIR_NAMES


def classify_file(path: Path) -> Optional[str]:
    """Devuelve una 'kind' string o None si el archivo no parece de diálogo."""
    ext = path.suffix.lower()
    name = path.name.lower()

    if ext not in DIALOGUE_EXTENSIONS:
        return None
    if IGNORE_FILE_HINTS.search(str(path)):
        return None

    if ext == ".rpy":
        return "renpy"
    if ext in (".po", ".pot"):
        return "gettext"
    if ext == ".resx":
        return "resx"
    if ext in (".ini", ".lang"):
        return "kv"
    if ext in (".csv", ".tsv"):
        return "i2_csv"
    if ext in (".yarn", ".ink"):
        return "plain_script"

    if ext == ".json":
        parts_lower = [p.lower() for p in path.parts]
        if "www" in parts_lower and "data" in parts_lower:
            return "rpgmaker_json"
        if "data" in parts_lower and re.match(
            r"^(map\d+|commonevents|actors|items|weapons|armors|skills|"
            r"enemies|troops|classes|system|states)\.json$", name,
        ):
            return "rpgmaker_json"
        return "json"

    if ext == ".xml":
        return "xml"

    if ext == ".txt":
        return "plain_script"

    return None


def detect_language_folder(dirname: str) -> Optional[str]:
    d = dirname.lower().replace("-", "").replace("_", "")
    for code, names in LANGUAGE_FOLDER_HINTS.items():
        for n in names:
            if d == n.replace("-", "").replace("_", ""):
                return code
    return None


def scan_game_dir(game_dir: Path) -> list[ScannedFile]:
    results: list[ScannedFile] = []
    for root, dirs, files in os.walk(game_dir):
        dirs[:] = [d for d in dirs if not _dir_is_ignored(d)]
        for fname in files:
            fpath = Path(root) / fname
            kind = classify_file(fpath)
            if kind is None:
                continue
            lang_hint = None
            lang_hint_folder = None
            for part in fpath.relative_to(game_dir).parts[:-1]:
                lh = detect_language_folder(part)
                if lh:
                    lang_hint = lh
                    lang_hint_folder = part
            results.append(ScannedFile(path=fpath, kind=kind, lang_hint=lang_hint,
                                        lang_hint_folder=lang_hint_folder))
    return results


def has_language_folder_structure(files: list[ScannedFile]) -> bool:
    return any(f.lang_hint for f in files)


# ==========================================================================
# 5. LECTURA/ESCRITURA DE TEXTO CON DETECCIÓN DE ENCODING
# ==========================================================================

def read_text_smart(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    encoding = "utf-8"
    if chardet is not None:
        guess = chardet.detect(raw)
        if guess and guess.get("confidence", 0) > 0.6 and guess.get("encoding"):
            encoding = guess["encoding"]
    try:
        return raw.decode(encoding), encoding
    except Exception:
        return raw.decode("utf-8", errors="replace"), "utf-8"


def write_text_smart(path: Path, content: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(content.encode(encoding))
    except Exception:
        path.write_bytes(content.encode("utf-8"))


# ==========================================================================
# 6. EXTRACTORES / TRADUCTORES POR FORMATO
#    Cada función recibe el texto original + un callback translate_fn(str)->str
#    y devuelve el nuevo contenido de archivo ya traducido.
# ==========================================================================

TranslateFn = Callable[[str], str]

# --- JSON genérico (localización simple o anidada) ----------------------

def translate_json_generic(content: str, translate_fn: TranslateFn) -> str:
    obj = json.loads(content)

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str) and looks_like_translatable_text(node):
            return translate_fn(node)
        return node

    new_obj = walk(obj)
    return json.dumps(new_obj, ensure_ascii=False, indent=2)


# --- RPG Maker MV/MZ JSON -------------------------------------------------
# Códigos de evento habituales que contienen texto mostrable al jugador.
_RPGMAKER_TEXT_CODES = {401, 405, 102, 108, 320, 324, 325, 355}
_RPGMAKER_NAME_KEYS = {"name", "description", "message1", "message2",
                       "message3", "message4", "note", "profile"}

def translate_rpgmaker_json(content: str, translate_fn: TranslateFn) -> str:
    obj = json.loads(content)

    def translate_event_list(event_list):
        for cmd in event_list:
            if not isinstance(cmd, dict):
                continue
            code = cmd.get("code")
            params = cmd.get("parameters")
            if code in _RPGMAKER_TEXT_CODES and isinstance(params, list):
                for i, p in enumerate(params):
                    if isinstance(p, str) and looks_like_translatable_text(p):
                        params[i] = translate_fn(p)
                    elif isinstance(p, list):
                        for j, sub in enumerate(p):
                            if isinstance(sub, str) and looks_like_translatable_text(sub):
                                p[j] = translate_fn(sub)

    def walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k == "list" and isinstance(v, list):
                    translate_event_list(v)
                elif k in _RPGMAKER_NAME_KEYS and isinstance(v, str) and looks_like_translatable_text(v):
                    node[k] = translate_fn(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return json.dumps(obj, ensure_ascii=False)


# --- Ren'Py (.rpy) --------------------------------------------------------
# Dos casos reales:
#   (a) Script base sin sistema de traducción: líneas de diálogo directas
#       `personaje "texto"` o `"texto"` -> se traducen in-place (patch-inplace).
#   (b) Esqueleto de traducción generado por el propio Ren'Py SDK
#       ("Generate Translations" en el launcher), con bloques:
#           translate spanish strings_xxxx:
#               old "Texto original"     <- NUNCA se toca, es la referencia
#               new ""                    <- se rellena con la traducción
#       Este es el caso "add-language": el archivo ya existe vacío en
#       game/tl/<idioma>/ y solo hace falta completar los `new`.
_RENPY_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')

def _extract_renpy_string(line: str) -> Optional[str]:
    m = _RENPY_STRING_RE.search(line)
    if not m:
        return None
    return m.group(1).replace('\\"', '"')


def translate_renpy(content: str, translate_fn: TranslateFn) -> str:
    out_lines = []
    pending_old_text: Optional[str] = None

    for line in content.splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue

        if stripped.startswith("old "):
            # Nunca se modifica: es el texto original de referencia.
            pending_old_text = _extract_renpy_string(line)
            out_lines.append(line)
            continue

        if stripped.startswith("new "):
            current_new = _extract_renpy_string(line) or ""
            needs_fill = (not current_new.strip()) or (current_new == pending_old_text)
            if needs_fill and pending_old_text and looks_like_translatable_text(pending_old_text):
                translated = translate_fn(pending_old_text).replace('"', '\\"')
                indent = line[:len(line) - len(line.lstrip())]
                out_lines.append(f'{indent}new "{translated}"')
            else:
                out_lines.append(line)
            pending_old_text = None
            continue

        # Diálogo directo (script base sin old/new), p.ej.: e "Hola."
        is_plain_dialogue = bool(re.match(r'^[\w\.]*\s*"', stripped)) and not stripped.startswith(
            ("label ", "menu", "init ", "translate ", "screen ", "style ",
             "default ", "define ", "image ", "python", "$"))

        if not is_plain_dialogue:
            out_lines.append(line)
            continue

        def _sub(m: re.Match) -> str:
            original = m.group(1)
            text = original.replace('\\"', '"')
            if not looks_like_translatable_text(text):
                return m.group(0)
            translated = translate_fn(text).replace('"', '\\"')
            return f'"{translated}"'

        new_line = _RENPY_STRING_RE.sub(_sub, line, count=1)
        out_lines.append(new_line)

    return "\n".join(out_lines)


# --- gettext .po / .pot ---------------------------------------------------

def translate_gettext(content: str, translate_fn: TranslateFn) -> str:
    lines = content.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("msgid "):
            out.append(line)
            m = re.match(r'^msgid "(.*)"$', line)
            msgid_text = m.group(1) if m else ""
            i += 1
            # posibles líneas de continuación de msgid (multi-línea)
            while i < len(lines) and lines[i].startswith('"'):
                out.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].startswith("msgstr "):
                msgstr_match = re.match(r'^msgstr "(.*)"$', lines[i])
                current = msgstr_match.group(1) if msgstr_match else ""
                if msgid_text and (not current) and looks_like_translatable_text(msgid_text):
                    translated = translate_fn(msgid_text).replace('"', '\\"')
                    out.append(f'msgstr "{translated}"')
                else:
                    out.append(lines[i])
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


# --- .resx (.NET) ----------------------------------------------------------

def translate_resx(content: str, translate_fn: TranslateFn) -> str:
    root = ET.fromstring(content)
    for data_el in root.findall("data"):
        value_el = data_el.find("value")
        if value_el is not None and value_el.text and looks_like_translatable_text(value_el.text):
            value_el.text = translate_fn(value_el.text)
    return ET.tostring(root, encoding="unicode")


def translate_xml_generic(content: str, translate_fn: TranslateFn) -> str:
    root = ET.fromstring(content)
    for el in root.iter():
        if el.text and looks_like_translatable_text(el.text):
            el.text = translate_fn(el.text)
        for attr_name, attr_val in list(el.attrib.items()):
            if attr_name.lower() in ("text", "value", "label", "message") and \
               looks_like_translatable_text(attr_val):
                el.set(attr_name, translate_fn(attr_val))
    return ET.tostring(root, encoding="unicode")


# --- INI / .lang key=value --------------------------------------------------

_KV_LINE_RE = re.compile(r"^([^=;#\[][^=]*?)\s*=\s*(.*)$")

def translate_kv(content: str, translate_fn: TranslateFn) -> str:
    out_lines = []
    for line in content.splitlines():
        m = _KV_LINE_RE.match(line)
        if m and looks_like_translatable_text(m.group(2)):
            key, val = m.group(1), m.group(2)
            # respeta comillas si las hay
            quoted = val.startswith('"') and val.endswith('"') and len(val) >= 2
            inner = val[1:-1] if quoted else val
            translated = translate_fn(inner)
            new_val = f'"{translated}"' if quoted else translated
            out_lines.append(f"{key}={new_val}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


# --- CSV / TSV estilo Unity I2 Localization --------------------------------

def translate_i2_csv(content: str, translate_fn: TranslateFn, delimiter: str = ",") -> tuple[str, bool]:
    """Devuelve (nuevo_contenido, columna_anadida). Si ya existe una columna
    'es'/'spanish' rellena huecos; si no, añade una columna nueva al final."""
    import csv
    import io

    reader = csv.reader(io.StringIO(content), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return content, False
    header = rows[0]
    header_lower = [h.strip().lower() for h in header]

    es_col_idx = None
    for i, h in enumerate(header_lower):
        if h in ("es", "spanish", "espanol", "español", "es-es", "es_es"):
            es_col_idx = i
            break

    src_col_idx = None
    for i, h in enumerate(header_lower):
        if h in ("en", "english", "eng", "en-us", "en_us"):
            src_col_idx = i
            break
    if src_col_idx is None and len(header) > 1:
        src_col_idx = 1  # primera columna de idioma habitual tras la "Key"

    added_column = False
    if es_col_idx is None:
        header.append("ES")
        es_col_idx = len(header) - 1
        added_column = True

    out_rows = [header]
    for row in rows[1:]:
        row = list(row)
        while len(row) <= es_col_idx:
            row.append("")
        current_es = row[es_col_idx].strip()
        source_text = row[src_col_idx].strip() if src_col_idx is not None and src_col_idx < len(row) else ""
        if (not current_es) and looks_like_translatable_text(source_text):
            row[es_col_idx] = translate_fn(source_text)
        out_rows.append(row)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    writer.writerows(out_rows)
    return buf.getvalue(), added_column


# --- texto plano / Yarn / Ink (heurística de línea) -------------------------

_SCRIPT_COMMAND_PREFIXES = ("#", "::", "->", "===", "---", "//", "<<", ">>",
                            "@", "[[", "title:", "tags:", "position:")

def translate_plain_script(content: str, translate_fn: TranslateFn) -> str:
    out_lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(_SCRIPT_COMMAND_PREFIXES):
            out_lines.append(line)
            continue
        if ":" in stripped and re.match(r"^[\w ]+:\s*$", stripped):
            out_lines.append(line)  # cabecera tipo "Speaker:" sin texto
            continue
        # Preserva un prefijo "Personaje: " si existe
        m = re.match(r"^(\s*[\w' ]{1,30}:\s*)(.*)$", line)
        if m and looks_like_translatable_text(m.group(2)):
            out_lines.append(m.group(1) + translate_fn(m.group(2)))
        elif looks_like_translatable_text(stripped):
            leading_ws = line[:len(line) - len(line.lstrip())]
            out_lines.append(leading_ws + translate_fn(stripped))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


DISPATCH: dict[str, Callable[[str, TranslateFn], str]] = {
    "json": translate_json_generic,
    "rpgmaker_json": translate_rpgmaker_json,
    "renpy": translate_renpy,
    "gettext": translate_gettext,
    "resx": translate_resx,
    "xml": translate_xml_generic,
    "kv": translate_kv,
    "plain_script": translate_plain_script,
    # "i2_csv" se maneja aparte porque devuelve una tupla extra
}


# ==========================================================================
# 7. PIPELINE PRINCIPAL
# ==========================================================================

@dataclass
class RunConfig:
    game_dir: Path
    target_lang: str = "es"
    source_lang: Optional[str] = None
    mode: str = "auto"          # auto | add-language | patch-inplace
    dry_run: bool = False
    engine: str = "argos"
    http_endpoint: Optional[str] = None
    http_key: Optional[str] = None
    max_files: Optional[int] = None


@dataclass
class RunStats:
    files_scanned: int = 0
    files_translated: int = 0
    files_skipped: int = 0
    strings_translated: int = 0
    errors: list[str] = field(default_factory=list)


MANIFEST_NAME = ".game_translator_manifest.json"


def load_manifest(game_dir: Path) -> dict:
    p = game_dir / MANIFEST_NAME
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {"backups": {}}
    return {"backups": {}}


def save_manifest(game_dir: Path, manifest: dict) -> None:
    (game_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def restore_from_backup(cfg: RunConfig) -> None:
    manifest = load_manifest(cfg.game_dir)
    backups = manifest.get("backups", {})
    if not backups:
        log("No hay backups registrados para restaurar (¿usaste --mode patch-inplace antes?).")
        return
    for rel_path, bak_rel in backups.items():
        original = cfg.game_dir / rel_path
        backup = cfg.game_dir / bak_rel
        if backup.exists():
            shutil.copyfile(backup, original)
            log(f"Restaurado: {rel_path}")
        else:
            log(f"  ! Backup no encontrado para {rel_path}, se omite.")
    log("Restauración completada.")


def make_translate_fn(translator: Translator, cache: TranslationCache,
                       source_lang: str, target_lang: str, stats: RunStats) -> TranslateFn:
    def fn(text: str) -> str:
        cached = cache.get(text, source_lang, target_lang)
        if cached is not None:
            return cached
        translated = translator.translate(text, source_lang)
        cache.set(text, source_lang, target_lang, translated)
        stats.strings_translated += 1
        return translated
    return fn


def guess_source_lang(scanned: list[ScannedFile], translator: Translator,
                       override: Optional[str]) -> str:
    if override:
        return override
    sample_chunks: list[str] = []
    for sf in scanned[:15]:
        try:
            text, _ = read_text_smart(sf.path)
            sample_chunks.append(text[:500])
        except Exception:
            continue
    sample = " ".join(sample_chunks)[:3000]
    if not sample.strip():
        return "en"
    lang = translator.detect_language(sample)
    # normaliza variantes tipo en-US -> en
    return lang.split("-")[0]


_RENPY_TRANSLATE_LABEL_RE_TEMPLATE = r'(^|\n)(\s*translate\s+){0}\b'


def retarget_renpy_language_label(content: str, old_folder_word: str, new_folder_word: str) -> str:
    """Cuando duplicamos game/tl/<idioma_origen>/*.rpy a game/tl/<idioma_destino>/,
    las etiquetas `translate <idioma_origen> ...:` dentro del archivo también
    deben pasar a `translate <idioma_destino> ...:`, si no Ren'Py no asocia el
    bloque al nuevo idioma aunque el archivo esté en la carpeta correcta."""
    if not old_folder_word or old_folder_word == new_folder_word:
        return content
    pattern = re.compile(_RENPY_TRANSLATE_LABEL_RE_TEMPLATE.format(re.escape(old_folder_word)))
    return pattern.sub(rf'\1\2{new_folder_word}', content)


def process_file(sf: ScannedFile, cfg: RunConfig, translate_fn: TranslateFn,
                  manifest: dict, stats: RunStats, target_path: Optional[Path] = None) -> None:
    """target_path: si se da, escribe el resultado ahí en lugar de sobre
    sf.path (usado en modo add-language). Si es None, sobreescribe in-place
    con backup (modo patch-inplace)."""
    try:
        content, encoding = read_text_smart(sf.path)

        if sf.kind == "i2_csv":
            delim = "\t" if sf.path.suffix.lower() == ".tsv" else ","
            new_content, _added = translate_i2_csv(content, translate_fn, delim)
        else:
            handler = DISPATCH.get(sf.kind)
            if handler is None:
                stats.files_skipped += 1
                return
            new_content = handler(content, translate_fn)

            if sf.kind == "renpy" and target_path is not None and sf.lang_hint_folder:
                rel_target_parts = target_path.relative_to(cfg.game_dir).parts
                rel_source_parts = sf.path.relative_to(cfg.game_dir).parts
                target_folder_word = None
                for src_part, tgt_part in zip(rel_source_parts, rel_target_parts):
                    if src_part == sf.lang_hint_folder and src_part != tgt_part:
                        target_folder_word = tgt_part
                        break
                if target_folder_word:
                    new_content = retarget_renpy_language_label(
                        new_content, sf.lang_hint_folder, target_folder_word
                    )

        if new_content == content:
            stats.files_skipped += 1
            return

        if cfg.dry_run:
            log(f"  (dry-run) traduciría: {sf.path.relative_to(cfg.game_dir)}")
            stats.files_translated += 1
            return

        if target_path is not None:
            write_text_smart(target_path, new_content, encoding)
        else:
            rel = str(sf.path.relative_to(cfg.game_dir))
            bak_rel = rel + ".bak"
            bak_path = cfg.game_dir / bak_rel
            if not bak_path.exists():
                bak_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(sf.path, bak_path)
            manifest["backups"][rel] = bak_rel
            write_text_smart(sf.path, new_content, encoding)

        stats.files_translated += 1

    except Exception as e:
        stats.errors.append(f"{sf.path}: {e}")
        log(f"  ! Error procesando {sf.path.name}: {e}")


def run(cfg: RunConfig) -> RunStats:
    stats = RunStats()
    log(f"Escaneando: {cfg.game_dir}")
    scanned = scan_game_dir(cfg.game_dir)
    stats.files_scanned = len(scanned)
    if cfg.max_files:
        scanned = scanned[: cfg.max_files]

    if not scanned:
        log("No se ha encontrado ningún archivo de diálogo reconocible en esta carpeta.")
        log("Puede que el juego empaquete el texto en un formato binario propio "
            "(p. ej. archivos .assets de Unity, .rgss/.rgssad de RPG Maker VX/Ace, "
            "o un formato cifrado). Esta herramienta no puede procesar esos casos "
            "sin un extractor específico para ese motor.")
        return stats

    log(f"Encontrados {len(scanned)} archivos candidatos.")
    by_kind: dict[str, int] = {}
    for sf in scanned:
        by_kind[sf.kind] = by_kind.get(sf.kind, 0) + 1
    for k, c in sorted(by_kind.items(), key=lambda x: -x[1]):
        log(f"  - {k}: {c} archivo(s)")

    use_add_language = (
        cfg.mode == "add-language"
        or (cfg.mode == "auto" and has_language_folder_structure(scanned))
    )
    if cfg.mode == "auto":
        log(f"Modo detectado automáticamente: "
            f"{'add-language (más seguro)' if use_add_language else 'patch-inplace (con backup)'}")

    try:
        translator = build_translator(cfg.engine, cfg.target_lang, cfg.http_endpoint, cfg.http_key)
    except RuntimeError as e:
        log(f"ERROR: {e}")
        sys.exit(1)
    source_lang = guess_source_lang(scanned, translator, cfg.source_lang)
    log(f"Idioma origen detectado/usado: '{source_lang}' -> destino: '{cfg.target_lang}'")

    cache = TranslationCache(cfg.game_dir / ".game_translator_cache.json")
    manifest = load_manifest(cfg.game_dir)
    translate_fn = make_translate_fn(translator, cache, source_lang, cfg.target_lang, stats)

    for sf in tqdm(scanned, desc="Traduciendo", total=len(scanned)):
        target_path = None
        if use_add_language and sf.lang_hint:
            rel_parts = list(sf.path.relative_to(cfg.game_dir).parts)
            folder_name = target_folder_name(sf.kind, cfg.target_lang)
            for i, part in enumerate(rel_parts[:-1]):
                if detect_language_folder(part) == sf.lang_hint:
                    rel_parts[i] = folder_name
                    break
            target_path = cfg.game_dir.joinpath(*rel_parts)
        elif use_add_language and not sf.lang_hint:
            # No pertenece a ninguna carpeta de idioma detectada: lo dejamos
            # tal cual (probablemente sea un archivo compartido, no de idioma).
            stats.files_skipped += 1
            continue
        process_file(sf, cfg, translate_fn, manifest, stats, target_path=target_path)

    cache.flush()
    if not cfg.dry_run and not use_add_language:
        save_manifest(cfg.game_dir, manifest)

    return stats


# ==========================================================================
# 8. CLI
# ==========================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Detecta y traduce automáticamente los diálogos de un videojuego, "
                     "y lo parchea para añadir/sustituir el idioma español.",
    )
    p.add_argument("--game-dir", help="Carpeta del juego. Si se omite, se abre un selector.")
    p.add_argument("--target", default="es", help="Código de idioma destino (por defecto: es)")
    p.add_argument("--source-lang", default=None,
                    help="Forzar idioma de origen (en, fr, de, ja...). Si se omite, se autodetecta.")
    p.add_argument("--mode", choices=["auto", "add-language", "patch-inplace"], default="auto",
                    help="auto (por defecto): decide según la estructura del juego.")
    p.add_argument("--engine", choices=["argos", "http", "noop"], default="argos",
                    help="Motor de traducción. 'argos' = offline (recomendado). "
                         "'http' = tu propio endpoint. 'noop' = prueba sin traducir de verdad.")
    p.add_argument("--http-endpoint", default=None, help="URL del endpoint si --engine http")
    p.add_argument("--http-key", default=None, help="API key opcional para --engine http")
    p.add_argument("--dry-run", action="store_true",
                    help="Solo muestra qué se traduciría, no escribe nada.")
    p.add_argument("--restore", action="store_true",
                    help="Restaura los .bak de un parcheo in-place anterior y sale.")
    p.add_argument("--max-files", type=int, default=None,
                    help="Límite de archivos a procesar (para pruebas rápidas).")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    game_dir = resolve_game_dir(args.game_dir)

    cfg = RunConfig(
        game_dir=game_dir,
        target_lang=args.target,
        source_lang=args.source_lang,
        mode=args.mode,
        dry_run=args.dry_run,
        engine=args.engine,
        http_endpoint=args.http_endpoint,
        http_key=args.http_key,
        max_files=args.max_files,
    )

    if args.restore:
        restore_from_backup(cfg)
        return 0

    if not args.dry_run and args.mode != "add-language":
        log("Aviso: si el modo resultante es 'patch-inplace' se sobreescribirán archivos del "
            "juego (con copia .bak). Se recomienda cerrar el launcher del juego y, si puedes, "
            "hacer una copia completa de la carpeta antes de continuar.")

    t0 = time.time()
    stats = run(cfg)
    dt = time.time() - t0

    print()
    log("===== RESUMEN =====")
    log(f"Archivos detectados:   {stats.files_scanned}")
    log(f"Archivos traducidos:   {stats.files_translated}")
    log(f"Archivos sin cambios:  {stats.files_skipped}")
    log(f"Cadenas traducidas:    {stats.strings_translated}")
    log(f"Tiempo total:          {dt:.1f}s")
    if stats.errors:
        log(f"Errores ({len(stats.errors)}):")
        for e in stats.errors[:20]:
            log(f"  - {e}")
    if args.dry_run:
        log("Esto ha sido una simulación (--dry-run). Ejecuta sin ese flag para aplicar los cambios.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
