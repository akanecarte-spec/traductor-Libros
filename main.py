import asyncio
import os
import re
import sqlite3
import threading
import time
import zipfile

from pathlib import Path
from html.parser import HTMLParser
from typing import Optional

import flet as ft


APP_NAME = "Traductor Profesional de Libros"
DB_NAME = "books_translator.db"
DEFAULT_MODEL = "gpt-4o-mini"

TARGET_LANGUAGES = {
    "Español": "Spanish",
    "Inglés": "English",
    "Coreano": "Korean",
    "Chino": "Chinese",
}

DEFAULT_TARGET_LANGUAGE = "Español"

MAX_CHUNK_CHARS = 9000


# ============================================================
# DIRECTORIOS
# ============================================================

def get_app_data_dir() -> Path:

    candidates = []

    storage_data = os.environ.get(
        "FLET_APP_STORAGE_DATA"
    )

    if storage_data:
        candidates.append(
            Path(storage_data)
        )

    try:
        candidates.append(
            Path.home() / ".traductor_profesional"
        )
    except Exception:
        pass

    try:
        candidates.append(
            Path.cwd() / ".traductor_profesional"
        )
    except Exception:
        pass

    for directory in candidates:

        try:

            directory.mkdir(
                parents=True,
                exist_ok=True
            )

            return directory

        except Exception:
            continue

    return Path.cwd()


APP_DATA_DIR = get_app_data_dir()

DB_PATH = APP_DATA_DIR / DB_NAME

OUTPUT_DIR = APP_DATA_DIR / "traducciones"

try:
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )
except Exception:
    pass


# ============================================================
# UTILIDADES DE TEXTO
# ============================================================

def normalize_text(text: str) -> str:

    if not text:
        return ""

    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    text = re.sub(
        r"\n[ \t]+",
        "\n",
        text
    )

    text = re.sub(
        r"[ \t]+\n",
        "\n",
        text
    )

    return text.strip()


def split_large_paragraph(
    text: str,
    max_chars: int
) -> list[str]:

    text = text.strip()

    if len(text) <= max_chars:
        return [text]

    sentences = re.split(
        r"(?<=[.!?。！？])\s+",
        text
    )

    parts = []

    current = ""

    for sentence in sentences:

        sentence = sentence.strip()

        if not sentence:
            continue

        if not current:

            current = sentence

            continue

        candidate = (
            current
            + " "
            + sentence
        )

        if len(candidate) <= max_chars:

            current = candidate

        else:

            parts.append(
                current
            )

            current = sentence

    if current:
        parts.append(current)

    final_parts = []

    for part in parts:

        if len(part) <= max_chars:

            final_parts.append(part)

            continue

        words = part.split()

        current = ""

        for word in words:

            candidate = (
                word
                if not current
                else current + " " + word
            )

            if len(candidate) <= max_chars:

                current = candidate

            else:

                if current:
                    final_parts.append(
                        current
                    )

                current = word

        if current:
            final_parts.append(
                current
            )

    return final_parts


def split_text(
    text: str
) -> list[str]:

    text = normalize_text(
        text
    )

    if not text:
        return []

    paragraphs = re.split(
        r"\n{2,}",
        text
    )

    chunks = []

    current = ""

    for paragraph in paragraphs:

        paragraph = paragraph.strip()

        if not paragraph:
            continue

        if len(paragraph) > MAX_CHUNK_CHARS:

            if current:

                chunks.append(
                    current
                )

                current = ""

            chunks.extend(
                split_large_paragraph(
                    paragraph,
                    MAX_CHUNK_CHARS
                )
            )

            continue

        candidate = (
            paragraph
            if not current
            else current + "\n\n" + paragraph
        )

        if len(candidate) <= MAX_CHUNK_CHARS:

            current = candidate

        else:

            if current:
                chunks.append(
                    current
                )

            current = paragraph

    if current:
        chunks.append(
            current
        )

    return chunks


# ============================================================
# DETECCIÓN LOCAL DE IDIOMA
# ============================================================

def detect_language_local(
    text: str
) -> Optional[str]:

    if not text:
        return None

    sample = text[:12000]

    korean = len(
        re.findall(
            r"[\uac00-\ud7af]",
            sample
        )
    )

    chinese = len(
        re.findall(
            r"[\u4e00-\u9fff]",
            sample
        )
    )

    japanese = len(
        re.findall(
            r"[\u3040-\u30ff]",
            sample
        )
    )

    if korean > 20:
        return "Korean"

    if japanese > 20:
        return "Japanese"

    if chinese > 20:
        return "Chinese"

    return None


# ============================================================
# BASE DE DATOS
# ============================================================

class Database:

    def __init__(
        self,
        path: Path
    ):

        self.path = str(path)

        self.lock = threading.RLock()

        self.initialize()

    def connect(self):

        connection = sqlite3.connect(
            self.path,
            timeout=60,
            check_same_thread=False
        )

        connection.row_factory = sqlite3.Row

        connection.execute(
            "PRAGMA journal_mode=WAL"
        )

        connection.execute(
            "PRAGMA synchronous=NORMAL"
        )

        connection.execute(
            "PRAGMA foreign_keys=ON"
        )

        return connection

    def initialize(self):

        with self.lock:

            connection = self.connect()

            try:

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS books (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        titulo TEXT NOT NULL,
                        total_capitulos INTEGER DEFAULT 0,
                        estado TEXT DEFAULT 'pendiente',
                        source_type TEXT DEFAULT '',
                        source_path TEXT DEFAULT '',
                        source_language TEXT DEFAULT 'auto',
                        target_language TEXT DEFAULT 'Español',
                        output_path TEXT DEFAULT '',
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chunks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        book_id INTEGER NOT NULL,
                        capitulo TEXT DEFAULT '',
                        orden INTEGER NOT NULL,
                        texto_original TEXT NOT NULL,
                        texto_traducido TEXT DEFAULT '',
                        estado TEXT DEFAULT 'pendiente',
                        source_ref TEXT DEFAULT '',
                        node_index INTEGER DEFAULT -1,
                        attempts INTEGER DEFAULT 0,
                        last_error TEXT DEFAULT '',
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(book_id)
                            REFERENCES books(id)
                            ON DELETE CASCADE
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chunks_book
                    ON chunks(book_id)
                    """
                )

                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chunks_status
                    ON chunks(book_id, estado)
                    """
                )

                self.migrate(
                    connection
                )

                connection.commit()

            finally:

                connection.close()

    def migrate(
        self,
        connection
    ):

        book_columns = {
            "source_type": "TEXT DEFAULT ''",
            "source_path": "TEXT DEFAULT ''",
            "source_language": "TEXT DEFAULT 'auto'",
            "target_language": "TEXT DEFAULT 'Español'",
            "output_path": "TEXT DEFAULT ''",
            "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
        }

        chunk_columns = {
            "source_ref": "TEXT DEFAULT ''",
            "node_index": "INTEGER DEFAULT -1",
            "attempts": "INTEGER DEFAULT 0",
            "last_error": "TEXT DEFAULT ''",
            "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
        }

        existing_books = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(books)"
            ).fetchall()
        }

        existing_chunks = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(chunks)"
            ).fetchall()
        }

        for name, definition in book_columns.items():

            if name not in existing_books:

                connection.execute(
                    f"""
                    ALTER TABLE books
                    ADD COLUMN {name} {definition}
                    """
                )

        for name, definition in chunk_columns.items():

            if name not in existing_chunks:

                connection.execute(
                    f"""
                    ALTER TABLE chunks
                    ADD COLUMN {name} {definition}
                    """
                )

    def create_book(
        self,
        title,
        source_type,
        source_path,
        source_language,
        target_language
    ):

        with self.lock:

            connection = self.connect()

            try:

                cursor = connection.execute(
                    """
                    INSERT INTO books (
                        titulo,
                        source_type,
                        source_path,
                        source_language,
                        target_language,
                        estado
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        source_type,
                        source_path,
                        source_language,
                        target_language,
                        "preparando"
                    )
                )

                book_id = cursor.lastrowid

                connection.commit()

                return int(book_id)

            finally:

                connection.close()

    def set_total_chunks(
        self,
        book_id,
        total
    ):

        with self.lock:

            connection = self.connect()

            try:

                connection.execute(
                    """
                    UPDATE books
                    SET total_capitulos = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        total,
                        book_id
                    )
                )

                connection.commit()

            finally:

                connection.close()

    def set_book_status(
        self,
        book_id,
        status
    ):

        with self.lock:

            connection = self.connect()

            try:

                connection.execute(
                    """
                    UPDATE books
                    SET estado = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        status,
                        book_id
                    )
                )

                connection.commit()

            finally:

                connection.close()

    def set_source_language(
        self,
        book_id,
        language
    ):

        with self.lock:

            connection = self.connect()

            try:

                connection.execute(
                    """
                    UPDATE books
                    SET source_language = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        language,
                        book_id
                    )
                )

                connection.commit()

            finally:

                connection.close()

    def set_output_path(
        self,
        book_id,
        output_path
    ):

        with self.lock:

            connection = self.connect()

            try:

                connection.execute(
                    """
                    UPDATE books
                    SET output_path = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        output_path,
                        book_id
                    )
                )

                connection.commit()

            finally:

                connection.close()

    def insert_chunk(
        self,
        book_id,
        chapter,
        order_number,
        original,
        source_ref="",
        node_index=-1
    ):

        with self.lock:

            connection = self.connect()

            try:

                connection.execute(
                    """
                    INSERT INTO chunks (
                        book_id,
                        capitulo,
                        orden,
                        texto_original,
                        source_ref,
                        node_index,
                        estado
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        book_id,
                        chapter,
                        order_number,
                        original,
                        source_ref,
                        node_index,
                        "pendiente"
                    )
                )

                connection.commit()

            finally:

                connection.close()

    def get_pending_chunks(
        self,
        book_id,
        limit=1
    ):

        with self.lock:

            connection = self.connect()

            try:

                return connection.execute(
                    """
                    SELECT *
                    FROM chunks
                    WHERE book_id = ?
                    AND estado IN ('pendiente', 'error')
                    ORDER BY orden ASC
                    LIMIT ?
                    """,
                    (
                        book_id,
                        limit
                    )
                ).fetchall()

            finally:

                connection.close()

    def get_all_chunks(
        self,
        book_id
    ):

        with self.lock:

            connection = self.connect()

            try:

                return connection.execute(
                    """
                    SELECT *
                    FROM chunks
                    WHERE book_id = ?
                    ORDER BY orden ASC
                    """,
                    (
                        book_id,
                    )
                ).fetchall()

            finally:

                connection.close()

    def update_chunk_translation(
        self,
        chunk_id,
        translation
    ):

        with self.lock:

            connection = self.connect()

            try:

                connection.execute(
                    """
                    UPDATE chunks
                    SET texto_traducido = ?,
                        estado = 'traducido',
                        last_error = '',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        translation,
                        chunk_id
                    )
                )

                connection.commit()

            finally:

                connection.close()

    def mark_chunk_error(
        self,
        chunk_id,
        error
    ):

        with self.lock:

            connection = self.connect()

            try:

                connection.execute(
                    """
                    UPDATE chunks
                    SET estado = 'error',
                        attempts = attempts + 1,
                        last_error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        str(error)[:3000],
                        chunk_id
                    )
                )

                connection.commit()

            finally:

                connection.close()

    def get_progress(
        self,
        book_id
    ):

        with self.lock:

            connection = self.connect()

            try:

                row = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(
                            CASE
                                WHEN estado = 'traducido'
                                THEN 1
                                ELSE 0
                            END
                        ) AS translated,
                        SUM(
                            CASE
                                WHEN estado = 'error'
                                THEN 1
                                ELSE 0
                            END
                        ) AS errors
                    FROM chunks
                    WHERE book_id = ?
                    """,
                    (
                        book_id,
                    )
                ).fetchone()

                return (
                    int(row["total"] or 0),
                    int(row["translated"] or 0),
                    int(row["errors"] or 0)
                )

            finally:

                connection.close()

    def get_book(
        self,
        book_id
    ):

        with self.lock:

            connection = self.connect()

            try:

                return connection.execute(
                    """
                    SELECT *
                    FROM books
                    WHERE id = ?
                    """,
                    (
                        book_id,
                    )
                ).fetchone()

            finally:

                connection.close()


# ============================================================
# OPENAI
# ============================================================

class OpenAITranslator:

    def __init__(
        self,
        api_key,
        model
    ):

        if not api_key.strip():

            raise ValueError(
                "No se ha configurado la API Key."
            )

        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(
            api_key=api_key.strip()
        )

        self.model = model

    async def detect_language(
        self,
        text
    ):

        local = detect_language_local(
            text
        )

        if local:
            return local

        sample = text[:10000]

        response = await self.client.responses.create(
            model=self.model,
            instructions=(
                "Eres un detector profesional de idiomas. "
                "Identifica el idioma principal del texto."
            ),
            input=f"""
Identifica el idioma principal del siguiente texto.

Puede ser inglés, coreano, chino, español,
japonés u otro idioma.

Responde solamente con el nombre del idioma
en inglés.

No agregues explicaciones.

TEXTO:

{sample}
""",
            store=False
        )

        result = getattr(
            response,
            "output_text",
            ""
        )

        return result.strip() or "Unknown"

    async def translate(
        self,
        text,
        target_language,
        source_language
    ):

        if not text.strip():
            return text

        target = TARGET_LANGUAGES.get(
            target_language,
            target_language
        )

        response = await self.client.responses.create(
            model=self.model,
            instructions=(
                "Eres un traductor profesional de libros "
                "científicos, académicos y literarios."
            ),
            input=f"""
Traduce el siguiente contenido.

IDIOMA DE ORIGEN:
{source_language}

IDIOMA DE DESTINO:
{target}

REGLAS:

1. Traduce todo el contenido.
2. No resumas.
3. No elimines información.
4. No agregues explicaciones.
5. Conserva exactamente el significado.
6. Conserva los párrafos.
7. Conserva saltos de línea.
8. Conserva números.
9. Conserva fórmulas.
10. Conserva unidades.
11. Conserva símbolos científicos.
12. Conserva referencias.
13. Conserva nombres propios cuando corresponda.
14. No traduzcas etiquetas HTML/XHTML.
15. No modifiques etiquetas HTML/XHTML.
16. Conserva marcadores técnicos.
17. Mantén el tono del texto original.
18. Devuelve únicamente la traducción.

TEXTO:

{text}
""",
            store=False
        )

        result = getattr(
            response,
            "output_text",
            ""
        )

        if not result:

            raise RuntimeError(
                "La API no devolvió una traducción."
            )

        return result.strip()


# ============================================================
# TXT
# ============================================================

class TXTImporter:

    @staticmethod
    def extract(path):

        result = []

        with open(
            path,
            "r",
            encoding="utf-8-sig",
            errors="replace"
        ) as file:

            current = []

            for line in file:

                line = line.rstrip("\n")

                if not line.strip():

                    if current:

                        text = "\n".join(
                            current
                        ).strip()

                        if text:

                            for chunk in split_text(
                                text
                            ):

                                result.append(
                                    (
                                        "TXT",
                                        chunk
                                    )
                                )

                        current = []

                    continue

                current.append(line)

            if current:

                text = "\n".join(
                    current
                ).strip()

                if text:

                    for chunk in split_text(
                        text
                    ):

                        result.append(
                            (
                                "TXT",
                                chunk
                            )
                        )

        return result


# ============================================================
# PDF
# ============================================================

class PDFImporter:

    @staticmethod
    def extract(path):

        import fitz

        result = []

        document = fitz.open(
            path
        )

        try:

            for page_number in range(
                len(document)
            ):

                page = document.load_page(
                    page_number
                )

                text = page.get_text(
                    "text",
                    sort=True
                )

                text = normalize_text(
                    text
                )

                if not text:
                    continue

                for chunk in split_text(
                    text
                ):

                    result.append(
                        (
                            f"Página {page_number + 1}",
                            chunk
                        )
                    )

        finally:

            document.close()

        return result


# ============================================================
# XHTML
# ============================================================

class XHTMLTextParser(
    HTMLParser
):

    SKIP_TAGS = {
        "script",
        "style",
        "code",
        "svg",
        "math"
    }

    def __init__(self):

        super().__init__(
            convert_charrefs=False
        )

        self.parts = []

        self.visible_nodes = []

        self.stack = []

    def handle_starttag(
        self,
        tag,
        attrs
    ):

        raw = self.get_starttag_text()

        if raw is None:
            raw = f"<{tag}>"

        self.parts.append(
            raw
        )

        self.stack.append(
            tag.lower()
        )

    def handle_startendtag(
        self,
        tag,
        attrs
    ):

        raw = self.get_starttag_text()

        if raw is None:
            raw = f"<{tag}/>"

        self.parts.append(
            raw
        )

    def handle_endtag(
        self,
        tag
    ):

        self.parts.append(
            f"</{tag}>"
        )

        tag = tag.lower()

        if self.stack:

            if self.stack[-1] == tag:

                self.stack.pop()

            elif tag in self.stack:

                while self.stack:

                    current = self.stack.pop()

                    if current == tag:
                        break

    def handle_data(
        self,
        data
    ):

        index = len(
            self.parts
        )

        if (
            data.strip()
            and not any(
                tag in self.SKIP_TAGS
                for tag in self.stack
            )
        ):

            self.visible_nodes.append(
                {
                    "part_index": index,
                    "text": data
                }
            )

        self.parts.append(
            data
        )

    def handle_entityref(
        self,
        name
    ):

        self.parts.append(
            f"&{name};"
        )

    def handle_charref(
        self,
        name
    ):

        self.parts.append(
            f"&#{name};"
        )

    def handle_comment(
        self,
        data
    ):

        self.parts.append(
            f"<!--{data}-->"
        )

    def handle_decl(
        self,
        decl
    ):

        self.parts.append(
            f"<!{decl}>"
        )

    def handle_pi(
        self,
        data
    ):

        self.parts.append(
            f"<?{data}>"
        )

    def handle_unknown_decl(
        self,
        data
    ):

        self.parts.append(
            f"<![{data}]>"
        )

    def render(
        self,
        translations
    ):

        result = []

        for index, part in enumerate(
            self.parts
        ):

            if index in translations:

                result.append(
                    translations[index]
                )

            else:

                result.append(
                    part
                )

        return "".join(
            result
        )


# ============================================================
# EPUB
# ============================================================

class EPUBImporter:

    XHTML_EXTENSIONS = (
        ".xhtml",
        ".html",
        ".htm"
    )

    @staticmethod
    def is_xhtml(
        name
    ):

        return name.lower().endswith(
            EPUBImporter.XHTML_EXTENSIONS
        )

    @staticmethod
    def extract_text_nodes(
        path
    ):

        nodes = []

        with zipfile.ZipFile(
            path,
            "r"
        ) as archive:

            for filename in archive.namelist():

                if not EPUBImporter.is_xhtml(
                    filename
                ):
                    continue

                try:

                    data = archive.read(
                        filename
                    )

                    text = data.decode(
                        "utf-8",
                        errors="replace"
                    )

                    parser = XHTMLTextParser()

                    parser.feed(
                        text
                    )

                    parser.close()

                    for node_index, node in enumerate(
                        parser.visible_nodes
                    ):

                        value = node["text"]

                        if not value.strip():
                            continue

                        nodes.append(
                            (
                                filename,
                                node_index,
                                value
                            )
                        )

                except Exception:
                    continue

        return nodes


class EPUBBuilder:

    @staticmethod
    def build(
        original_path,
        output_path,
        translations
    ):

        temporary_path = str(
            Path(output_path).with_suffix(
                ".working.epub"
            )
        )

        if os.path.exists(
            temporary_path
        ):

            os.remove(
                temporary_path
            )

        with zipfile.ZipFile(
            original_path,
            "r"
        ) as source:

            with zipfile.ZipFile(
                temporary_path,
                "w"
            ) as target:

                for info in source.infolist():

                    data = source.read(
                        info.filename
                    )

                    if EPUBImporter.is_xhtml(
                        info.filename
                    ):

                        try:

                            text = data.decode(
                                "utf-8",
                                errors="replace"
                            )

                            parser = XHTMLTextParser()

                            parser.feed(
                                text
                            )

                            parser.close()

                            replacements = {}

                            for node_index, node in enumerate(
                                parser.visible_nodes
                            ):

                                key = (
                                    info.filename,
                                    node_index
                                )

                                if key in translations:

                                    replacements[
                                        node["part_index"]
                                    ] = translations[key]

                            if replacements:

                                text = parser.render(
                                    replacements
                                )

                                data = text.encode(
                                    "utf-8"
                                )

                        except Exception:
                            pass

                    target.writestr(
                        info,
                        data
                    )

        os.replace(
            temporary_path,
            output_path
        )


# ============================================================
# EXPORTADORES
# ============================================================

class TXTExporter:

    @staticmethod
    def export(
        chunks,
        output_path
    ):

        with open(
            output_path,
            "w",
            encoding="utf-8"
        ) as file:

            previous_chapter = None

            for chunk in chunks:

                chapter = chunk["capitulo"]

                if (
                    previous_chapter is not None
                    and chapter != previous_chapter
                ):

                    file.write(
                        "\n\n"
                        + "=" * 60
                        + "\n\n"
                    )

                text = (
                    chunk["texto_traducido"]
                    or chunk["texto_original"]
                )

                file.write(
                    text.strip()
                )

                file.write(
                    "\n\n"
                )

                previous_chapter = chapter


class PDFTextExporter:

    @staticmethod
    def export(
        chunks,
        output_path
    ):

        previous_page = None

        with open(
            output_path,
            "w",
            encoding="utf-8"
        ) as file:

            for chunk in chunks:

                page = chunk["capitulo"]

                if (
                    previous_page is not None
                    and page != previous_page
                ):

                    file.write(
                        "\f"
                    )

                text = (
                    chunk["texto_traducido"]
                    or chunk["texto_original"]
                )

                file.write(
                    text.strip()
                )

                file.write(
                    "\n\n"
                )

                previous_page = page


# ============================================================
# WORKER
# ============================================================

class TranslationWorker:

    def __init__(
        self,
        database,
        book_id,
        api_key,
        target_language,
        model,
        callback=None
    ):

        self.database = database

        self.book_id = book_id

        self.api_key = api_key

        self.target_language = target_language

        self.model = model

        self.callback = callback

        self.stop_event = threading.Event()

        self.thread = None

    def start(self):

        self.thread = threading.Thread(
            target=self._thread_main,
            daemon=True
        )

        self.thread.start()

    def stop(self):

        self.stop_event.set()

    def is_running(self):

        return (
            self.thread is not None
            and self.thread.is_alive()
        )

    def emit(
        self,
        event
    ):

        if self.callback:

            try:

                self.callback(
                    event
                )

            except Exception:
                pass

    def _thread_main(self):

        try:

            asyncio.run(
                self._run()
            )

        except Exception as exc:

            self.emit(
                {
                    "type": "error",
                    "message": str(exc)
                }
            )

    async def _run(self):

        translator = OpenAITranslator(
            self.api_key,
            self.model
        )

        book = self.database.get_book(
            self.book_id
        )

        if book is None:

            raise RuntimeError(
                "El libro no existe."
            )

        self.database.set_book_status(
            self.book_id,
            "traduciendo"
        )

        source_language = (
            book["source_language"]
            or "auto"
        )

        if source_language.lower() in {
            "",
            "auto",
            "unknown"
        }:

            all_chunks = (
                self.database.get_all_chunks(
                    self.book_id
                )
            )

            sample = "\n".join(
                chunk["texto_original"]
                for chunk in all_chunks[:5]
            )

            if sample.strip():

                local = detect_language_local(
                    sample
                )

                if local:

                    source_language = local

                else:

                    source_language = (
                        await translator.detect_language(
                            sample
                        )
                    )

                self.database.set_source_language(
                    self.book_id,
                    source_language
                )

                self.emit(
                    {
                        "type": "language",
                        "language": source_language
                    }
                )

        total, translated, errors = (
            self.database.get_progress(
                self.book_id
            )
        )

        self.emit(
            {
                "type": "started",
                "total": total,
                "translated": translated,
                "errors": errors,
                "language": source_language
            }
        )

        while not self.stop_event.is_set():

            pending = (
                self.database.get_pending_chunks(
                    self.book_id,
                    limit=1
                )
            )

            if not pending:
                break

            chunk = pending[0]

            original = chunk[
                "texto_original"
            ]

            success = False

            last_error = ""

            for attempt in range(5):

                if self.stop_event.is_set():
                    break

                try:

                    translation = (
                        await translator.translate(
                            original,
                            self.target_language,
                            source_language
                        )
                    )

                    if not translation.strip():

                        raise RuntimeError(
                            "La traducción llegó vacía."
                        )

                    self.database.update_chunk_translation(
                        chunk["id"],
                        translation
                    )

                    success = True

                    break

                except Exception as exc:

                    last_error = str(
                        exc
                    )

                    await asyncio.sleep(
                        min(
                            2 ** attempt,
                            20
                        )
                    )

            if not success:

                self.database.mark_chunk_error(
                    chunk["id"],
                    last_error
                )

            total, translated, errors = (
                self.database.get_progress(
                    self.book_id
                )
            )

            self.emit(
                {
                    "type": "progress",
                    "total": total,
                    "translated": translated,
                    "errors": errors
                }
            )

        total, translated, errors = (
            self.database.get_progress(
                self.book_id
            )
        )

        if self.stop_event.is_set():

            self.database.set_book_status(
                self.book_id,
                "detenido"
            )

            self.emit(
                {
                    "type": "stopped",
                    "total": total,
                    "translated": translated,
                    "errors": errors
                }
            )

        elif (
            total > 0
            and translated == total
        ):

            self.database.set_book_status(
                self.book_id,
                "completado"
            )

            self.emit(
                {
                    "type": "completed",
                    "total": total,
                    "translated": translated,
                    "errors": errors
                }
            )

        else:

            self.database.set_book_status(
                self.book_id,
                "pendiente"
            )

            self.emit(
                {
                    "type": "partial",
                    "total": total,
                    "translated": translated,
                    "errors": errors
                }
            )


# ============================================================
# APLICACIÓN
# ============================================================

class TranslatorApp:

    def __init__(
        self,
        page
    ):

        self.page = page

        self.database = Database(
            DB_PATH
        )

        self.current_book_id = None

        self.current_file_path = None

        self.current_source_type = None

        self.worker = None

        self.api_key_field = None

        self.model_field = None

        self.target_dropdown = None

        self.file_name_text = None

        self.file_info_text = None

        self.progress_bar = None

        self.progress_text = None

        self.status_text = None

        self.log_view = None

        self.reader_text = None

        self.start_button = None

        self.stop_button = None

        self.file_picker = None

        self.build_page()

    def build_page(self):

        self.page.title = APP_NAME

        self.page.padding = 0

        self.page.bgcolor = "#F5F7FA"

        self.page.theme_mode = ft.ThemeMode.LIGHT

        # ====================================================
        # FILE PICKER
        #
        # ÚNICA ubicación del FilePicker.
        #
        # NO está dentro de controls=[].
        # NO está dentro de Column.
        # NO está dentro de Row.
        # NO está dentro de Container.
        # NO está dentro de page.add().
        # NO se utiliza page.overlay.
        # ====================================================

        self.file_picker = ft.FilePicker()

        self.file_picker.on_result = (
            self.on_file_selected
        )

        self.page.services.append(
            self.file_picker
        )

        # ====================================================
        # CAMPOS
        # ====================================================

        self.api_key_field = ft.TextField(
            label="API Key de OpenAI",
            password=True,
            can_reveal_password=True,
            expand=True
        )

        self.model_field = ft.TextField(
            label="Modelo de IA",
            value=DEFAULT_MODEL,
            expand=True
        )

        self.target_dropdown = ft.Dropdown(
            label="Idioma de destino",
            value=DEFAULT_TARGET_LANGUAGE,
            options=[
                ft.DropdownOption(
                    key="Español",
                    text="Español"
                ),
                ft.DropdownOption(
                    key="Inglés",
                    text="Inglés"
                ),
                ft.DropdownOption(
                    key="Coreano",
                    text="Coreano"
                ),
                ft.DropdownOption(
                    key="Chino",
                    text="Chino"
                )
            ],
            expand=True
        )

        self.file_name_text = ft.Text(
            "Ningún archivo seleccionado",
            size=15
        )

        self.file_info_text = ft.Text(
            "Formatos compatibles: PDF, EPUB y TXT",
            size=12,
            color="#667085"
        )

        self.progress_bar = ft.ProgressBar(
            value=0,
            expand=True
        )

        self.progress_text = ft.Text(
            "0 / 0"
        )

        self.status_text = ft.Text(
            "Esperando archivo..."
        )

        self.log_view = ft.ListView(
            expand=True,
            spacing=5,
            auto_scroll=True
        )

        self.reader_text = ft.Text(
            "",
            selectable=True,
            size=15
        )

        self.start_button = ft.Button(
            content="Iniciar traducción",
            icon=ft.Icons.PLAY_ARROW,
            on_click=self.start_translation
        )

        self.stop_button = ft.Button(
            content="Detener",
            icon=ft.Icons.STOP,
            on_click=self.stop_translation,
            disabled=True
        )

        # ====================================================
        # BARRA DE PESTAÑAS
        # ====================================================

        tab_bar = ft.TabBar(
            tabs=[
                ft.Tab(
                    label="Cargar",
                    icon=ft.Icons.UPLOAD_FILE
                ),
                ft.Tab(
                    label="Progreso",
                    icon=ft.Icons.TRENDING_UP
                ),
                ft.Tab(
                    label="Lector",
                    icon=ft.Icons.MENU_BOOK
                ),
                ft.Tab(
                    label="Configuración",
                    icon=ft.Icons.SETTINGS
                )
            ],
            scrollable=True
        )

        tab_view = ft.TabBarView(
            expand=True,
            controls=[
                self.build_load_view(),
                self.build_progress_view(),
                self.build_reader_view(),
                self.build_settings_view()
            ]
        )

        tabs = ft.Tabs(
            length=4,
            selected_index=0,
            expand=True,
            content=ft.Column(
                expand=True,
                spacing=0,
                controls=[
                    tab_bar,
                    tab_view
                ]
            )
        )

        self.page.add(
            ft.SafeArea(
                expand=True,
                content=tabs
            )
        )

    def make_card(
        self,
        content
    ):

        return ft.Container(
            content=content,
            padding=18,
            bgcolor="#FFFFFF",
            border=ft.Border.all(
                width=1,
                color="#D9DEE8"
            ),
            border_radius=12
        )

    def build_load_view(self):

        select_button = ft.Button(
            content="Seleccionar libro",
            icon=ft.Icons.FOLDER_OPEN,
            on_click=self.select_file
        )

        file_card = self.make_card(
            ft.Column(
                spacing=10,
                controls=[
                    ft.Text(
                        "Archivo seleccionado",
                        size=18,
                        weight=ft.FontWeight.BOLD
                    ),
                    self.file_name_text,
                    self.file_info_text,
                    select_button
                ]
            )
        )

        translation_card = self.make_card(
            ft.Column(
                spacing=12,
                controls=[
                    ft.Text(
                        "Configuración de traducción",
                        size=18,
                        weight=ft.FontWeight.BOLD
                    ),
                    ft.Row(
                        controls=[
                            self.target_dropdown,
                            self.model_field
                        ]
                    ),
                    ft.Text(
                        "Idioma de origen: detección automática",
                        size=12,
                        color="#667085"
                    ),
                    ft.Text(
                        "Destino predeterminado: Español",
                        size=12,
                        color="#667085"
                    ),
                    self.start_button,
                    self.stop_button
                ]
            )
        )

        return ft.Container(
            expand=True,
            padding=20,
            content=ft.ListView(
                expand=True,
                spacing=15,
                controls=[
                    ft.Text(
                        "Traductor Profesional",
                        size=26,
                        weight=ft.FontWeight.BOLD
                    ),
                    ft.Text(
                        "Traducción de libros extensos sin bloquear la interfaz.",
                        size=14,
                        color="#667085"
                    ),
                    file_card,
                    translation_card
                ]
            )
        )

    def build_progress_view(self):

        card = self.make_card(
            ft.Column(
                spacing=15,
                controls=[
                    ft.Text(
                        "Progreso de traducción",
                        size=20,
                        weight=ft.FontWeight.BOLD
                    ),
                    self.status_text,
                    ft.Row(
                        controls=[
                            self.progress_bar,
                            self.progress_text
                        ]
                    ),
                    ft.Divider(),
                    ft.Text(
                        "Registro",
                        size=16,
                        weight=ft.FontWeight.BOLD
                    ),
                    ft.Container(
                        height=350,
                        expand=True,
                        content=self.log_view
                    )
                ]
            )
        )

        return ft.Container(
            expand=True,
            padding=20,
            content=card
        )

    def build_reader_view(self):

        return ft.Container(
            expand=True,
            padding=20,
            content=self.make_card(
                ft.Column(
                    expand=True,
                    controls=[
                        ft.Text(
                            "Lector",
                            size=20,
                            weight=ft.FontWeight.BOLD
                        ),
                        ft.Container(
                            expand=True,
                            content=ft.ListView(
                                expand=True,
                                controls=[
                                    self.reader_text
                                ]
                            )
                        )
                    ]
                )
            )
        )

    def build_settings_view(self):

        save_button = ft.Button(
            content="Guardar configuración",
            icon=ft.Icons.SAVE,
            on_click=self.save_settings
        )

        return ft.Container(
            expand=True,
            padding=20,
            content=self.make_card(
                ft.Column(
                    spacing=15,
                    controls=[
                        ft.Text(
                            "Configuración",
                            size=20,
                            weight=ft.FontWeight.BOLD
                        ),
                        self.api_key_field,
                        ft.Text(
                            "La API Key se utiliza para realizar las traducciones.",
                            size=12,
                            color="#667085"
                        ),
                        save_button
                    ]
                )
            )
        )

    # ========================================================
    # SELECCIONAR ARCHIVO
    # ========================================================

    def select_file(
        self,
        e
    ):

        try:

            self.file_picker.pick_files(
                allow_multiple=False,
                allowed_extensions=[
                    "pdf",
                    "epub",
                    "txt"
                ]
            )

        except Exception as exc:

            self.add_log(
                f"Error al abrir selector: {exc}"
            )

    # ========================================================
    # ARCHIVO SELECCIONADO
    # ========================================================

    def on_file_selected(
        self,
        event
    ):

        files = getattr(
            event,
            "files",
            None
        )

        if not files:
            return

        selected = files[0]

        path = getattr(
            selected,
            "path",
            None
        )

        if not path:

            self.add_log(
                "No se pudo obtener la ruta del archivo."
            )

            return

        self.current_file_path = path

        extension = Path(
            path
        ).suffix.lower()

        self.current_source_type = (
            extension.replace(
                ".",
                ""
            )
        )

        self.file_name_text.value = (
            Path(path).name
        )

        self.file_info_text.value = (
            f"Formato: {extension.upper()} | Archivo preparado"
        )

        self.status_text.value = (
            "Archivo listo."
        )

        self.add_log(
            f"Archivo seleccionado: {Path(path).name}"
        )

        self.safe_update()

    # ========================================================
    # CONFIGURACIÓN
    # ========================================================

    def save_settings(
        self,
        e
    ):

        self.add_log(
            "Configuración actualizada."
        )

        self.status_text.value = (
            "Configuración guardada."
        )

        self.safe_update()

    # ========================================================
    # LOG
    # ========================================================

    def add_log(
        self,
        message
    ):

        timestamp = time.strftime(
            "%H:%M:%S"
        )

        self.log_view.controls.append(
            ft.Text(
                f"[{timestamp}] {message}",
                size=12
            )
        )

        try:

            self.log_view.update()

        except Exception:
            pass

    # ========================================================
    # INICIAR
    # ========================================================

    def start_translation(
        self,
        e
    ):

        if (
            self.worker
            and self.worker.is_running()
        ):

            self.add_log(
                "Ya existe una traducción en ejecución."
            )

            return

        if not self.current_file_path:

            self.status_text.value = (
                "Selecciona primero un archivo."
            )

            self.add_log(
                "No se seleccionó ningún archivo."
            )

            self.safe_update()

            return

        api_key = (
            self.api_key_field.value
            or os.environ.get(
                "OPENAI_API_KEY",
                ""
            )
        ).strip()

        if not api_key:

            self.status_text.value = (
                "Falta la API Key."
            )

            self.add_log(
                "Configura la API Key de OpenAI."
            )

            self.safe_update()

            return

        target_language = (
            self.target_dropdown.value
            or DEFAULT_TARGET_LANGUAGE
        )

        if target_language not in TARGET_LANGUAGES:

            target_language = (
                DEFAULT_TARGET_LANGUAGE
            )

        try:

            self.add_log(
                "Preparando libro..."
            )

            book_id = self.prepare_book(
                self.current_file_path,
                target_language
            )

            self.current_book_id = book_id

            self.start_button.disabled = True

            self.stop_button.disabled = False

            self.status_text.value = (
                "Preparando traducción..."
            )

            self.safe_update()

            model = (
                self.model_field.value
                or DEFAULT_MODEL
            )

            self.worker = TranslationWorker(
                database=self.database,
                book_id=book_id,
                api_key=api_key,
                target_language=target_language,
                model=model,
                callback=self.worker_event
            )

            self.worker.start()

        except Exception as exc:

            self.status_text.value = (
                "Error al preparar el libro."
            )

            self.add_log(
                f"ERROR: {exc}"
            )

            self.safe_update()

    # ========================================================
    # PREPARAR LIBRO
    # ========================================================

    def prepare_book(
        self,
        path,
        target_language
    ):

        extension = Path(
            path
        ).suffix.lower()

        title = Path(
            path
        ).stem

        source_language = "auto"

        if extension == ".txt":

            source_type = "txt"

            extracted = (
                TXTImporter.extract(
                    path
                )
            )

            nodes = []

        elif extension == ".pdf":

            source_type = "pdf"

            extracted = (
                PDFImporter.extract(
                    path
                )
            )

            nodes = []

        elif extension == ".epub":

            source_type = "epub"

            nodes = (
                EPUBImporter.extract_text_nodes(
                    path
                )
            )

            extracted = [
                (
                    filename,
                    text
                )
                for filename, node_index, text
                in nodes
            ]

        else:

            raise ValueError(
                "Formato no compatible. "
                "Usa PDF, EPUB o TXT."
            )

        if not extracted:

            raise ValueError(
                "No se encontró texto en el archivo."
            )

        sample = "\n".join(
            item[1]
            for item in extracted[:5]
        )

        local_language = (
            detect_language_local(
                sample
            )
        )

        if local_language:

            source_language = local_language

        book_id = self.database.create_book(
            title=title,
            source_type=source_type,
            source_path=path,
            source_language=source_language,
            target_language=target_language
        )

        if extension == ".epub":

            for order, (
                filename,
                node_index,
                text
            ) in enumerate(nodes):

                self.database.insert_chunk(
                    book_id=book_id,
                    chapter=filename,
                    order_number=order,
                    original=text,
                    source_ref=filename,
                    node_index=node_index
                )

            total = len(nodes)

        else:

            for order, (
                chapter,
                text
            ) in enumerate(extracted):

                self.database.insert_chunk(
                    book_id=book_id,
                    chapter=chapter,
                    order_number=order,
                    original=text
                )

            total = len(extracted)

        self.database.set_total_chunks(
            book_id,
            total
        )

        self.database.set_book_status(
            book_id,
            "pendiente"
        )

        self.add_log(
            f"Libro: {title}"
        )

        self.add_log(
            f"Bloques: {total}"
        )

        if source_language == "auto":

            self.add_log(
                "Idioma de origen: detección automática mediante IA."
            )

        else:

            self.add_log(
                f"Idioma detectado inicialmente: {source_language}"
            )

        self.add_log(
            f"Idioma de destino: {target_language}"
        )

        return book_id

    # ========================================================
    # EVENTOS WORKER
    # ========================================================

    def worker_event(
        self,
        event
    ):

        try:

            event_type = event.get(
                "type"
            )

            if event_type == "started":

                language = event.get(
                    "language",
                    "auto"
                )

                self.add_log(
                    "Traducción iniciada."
                )

                self.add_log(
                    f"Idioma de origen: {language}"
                )

                self.safe_update()

            elif event_type == "language":

                language = event.get(
                    "language",
                    "Unknown"
                )

                self.add_log(
                    f"IA detectó idioma: {language}"
                )

                self.status_text.value = (
                    f"Idioma de origen: {language}"
                )

                self.safe_update()

            elif event_type == "progress":

                total = event.get(
                    "total",
                    0
                )

                translated = event.get(
                    "translated",
                    0
                )

                errors = event.get(
                    "errors",
                    0
                )

                percentage = (
                    translated / total
                    if total
                    else 0
                )

                self.progress_bar.value = (
                    percentage
                )

                self.progress_text.value = (
                    f"{translated} / {total}"
                )

                self.status_text.value = (
                    f"Traduciendo "
                    f"{percentage * 100:.1f}% "
                    f"| Errores: {errors}"
                )

                self.safe_update()

            elif event_type == "completed":

                self.progress_bar.value = 1

                self.progress_text.value = (
                    f"{event.get('translated', 0)}"
                    f" / "
                    f"{event.get('total', 0)}"
                )

                self.status_text.value = (
                    "Traducción completada."
                )

                self.add_log(
                    "Traducción completada."
                )

                self.start_button.disabled = False

                self.stop_button.disabled = True

                self.export_result()

                self.safe_update()

            elif event_type == "stopped":

                self.status_text.value = (
                    "Traducción detenida."
                )

                self.add_log(
                    "Traducción detenida."
                )

                self.start_button.disabled = False

                self.stop_button.disabled = True

                self.safe_update()

            elif event_type == "partial":

                self.status_text.value = (
                    "Traducción parcialmente completada."
                )

                self.add_log(
                    "Existen bloques pendientes."
                )

                self.start_button.disabled = False

                self.stop_button.disabled = True

                self.safe_update()

            elif event_type == "error":

                message = event.get(
                    "message",
                    "Error desconocido"
                )

                self.status_text.value = (
                    "Error durante la traducción."
                )

                self.add_log(
                    f"ERROR: {message}"
                )

                self.start_button.disabled = False

                self.stop_button.disabled = True

                self.safe_update()

        except Exception:
            pass

    # ========================================================
    # DETENER
    # ========================================================

    def stop_translation(
        self,
        e
    ):

        if self.worker:

            self.worker.stop()

            self.status_text.value = (
                "Solicitando detención..."
            )

            self.add_log(
                "Detención solicitada."
            )

            self.safe_update()

    # ========================================================
    # EXPORTAR
    # ========================================================

    def export_result(self):

        if not self.current_book_id:
            return

        book = self.database.get_book(
            self.current_book_id
        )

        if not book:
            return

        chunks = (
            self.database.get_all_chunks(
                self.current_book_id
            )
        )

        source_type = (
            book["source_type"]
            or ""
        )

        original_path = (
            book["source_path"]
            or ""
        )

        target_language = (
            book["target_language"]
            or DEFAULT_TARGET_LANGUAGE
        )

        safe_name = re.sub(
            r"[^\w\-]+",
            "_",
            Path(original_path).stem
        )

        if source_type == "epub":

            output_path = (
                OUTPUT_DIR
                / f"{safe_name}_{target_language}.epub"
            )

            translations = {}

            for chunk in chunks:

                if chunk["estado"] != "traducido":
                    continue

                key = (
                    chunk["source_ref"],
                    chunk["node_index"]
                )

                translations[key] = (
                    chunk["texto_traducido"]
                )

            EPUBBuilder.build(
                original_path=original_path,
                output_path=str(
                    output_path
                ),
                translations=translations
            )

        elif source_type == "pdf":

            output_path = (
                OUTPUT_DIR
                / f"{safe_name}_{target_language}.txt"
            )

            PDFTextExporter.export(
                chunks,
                str(output_path)
            )

        else:

            output_path = (
                OUTPUT_DIR
                / f"{safe_name}_{target_language}.txt"
            )

            TXTExporter.export(
                chunks,
                str(output_path)
            )

        self.database.set_output_path(
            self.current_book_id,
            str(output_path)
        )

        self.add_log(
            f"Archivo generado: {output_path}"
        )

        self.reader_text.value = (
            self.build_reader_content(
                chunks
            )
        )

        self.safe_update()

    # ========================================================
    # LECTOR
    # ========================================================

    def build_reader_content(
        self,
        chunks
    ):

        parts = []

        for chunk in chunks:

            text = (
                chunk["texto_traducido"]
                or chunk["texto_original"]
            )

            if text:

                parts.append(
                    text.strip()
                )

        return "\n\n".join(
            parts
        )

    # ========================================================
    # ACTUALIZACIÓN
    # ========================================================

    def safe_update(self):

        try:

            self.page.update()

        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

def main(page):

    TranslatorApp(
        page
    )


if __name__ == "__main__":

    ft.run(
        main
    )