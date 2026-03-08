#!/usr/bin/env python3
"""Convert an Anki .apkg export into a Markdown corpus."""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


FIELD_SEPARATOR = "\x1f"
DECK_SEPARATOR = "\x1f"


def slugify(text: str, max_len: int = 80) -> str:
    norm = unicodedata.normalize("NFKC", text).strip().lower()
    norm = norm.replace("/", " ").replace("\\", " ")
    norm = re.sub(r"[^\w\s-]", "", norm, flags=re.UNICODE)
    norm = norm.replace("_", "-")
    norm = re.sub(r"\s+", "-", norm).strip("-")
    norm = re.sub(r"-+", "-", norm)
    return (norm[:max_len].rstrip("-") or "section")


class InlineHtmlToMarkdown(HTMLParser):
    """Minimal HTML-to-Markdown converter for Anki field content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.href_stack: List[str] = []
        self.list_stack: List[str] = []
        self.in_pre = False

    def handle_starttag(self, tag: str, attrs: List[tuple[str, str | None]]) -> None:
        attrs_dict = {key: (value or "") for key, value in attrs}

        if tag == "br":
            self.out.append("\n")
        elif tag == "p":
            self._newline(2)
        elif tag in {"ul", "ol"}:
            self.list_stack.append(tag)
            self._newline(1)
        elif tag == "li":
            self._newline(1)
            indent = "  " * max(0, len(self.list_stack) - 1)
            marker = "1. " if self.list_stack and self.list_stack[-1] == "ol" else "- "
            self.out.append(f"{indent}{marker}")
        elif tag in {"strong", "b"}:
            self.out.append("**")
        elif tag in {"em", "i"}:
            self.out.append("*")
        elif tag == "a":
            self.href_stack.append(attrs_dict.get("href", ""))
            self.out.append("[")
        elif tag == "pre":
            self._newline(2)
            self.out.append("```\n")
            self.in_pre = True
        elif tag == "img":
            src = attrs_dict.get("src", "")
            alt = attrs_dict.get("alt", "")
            if src:
                self._newline(1)
                self.out.append(f"![{alt}]({src})")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "li"}:
            self._newline(1)
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._newline(1)
        elif tag in {"strong", "b"}:
            self.out.append("**")
        elif tag in {"em", "i"}:
            self.out.append("*")
        elif tag == "a":
            href = self.href_stack.pop() if self.href_stack else ""
            self.out.append(f"]({href})")
        elif tag == "pre":
            if self.in_pre:
                if not "".join(self.out).endswith("\n"):
                    self.out.append("\n")
                self.out.append("```\n")
                self.in_pre = False

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self.in_pre:
            self.out.append(data)
            return

        text = html.unescape(data).replace("\xa0", " ")
        text = re.sub(r"[ \t]+", " ", text)
        if text.strip():
            self.out.append(text)

    def markdown(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n +", "\n", text)
        return text.strip()

    def _newline(self, count: int) -> None:
        if not self.out:
            return
        text = "".join(self.out)
        trailing = len(text) - len(text.rstrip("\n"))
        if trailing < count:
            self.out.append("\n" * (count - trailing))


def html_to_markdown(raw: str) -> str:
    parser = InlineHtmlToMarkdown()
    parser.feed(raw)
    return parser.markdown()


def clean_text(raw: str) -> str:
    return html_to_markdown(raw).strip()


def split_paragraphs(raw: str) -> List[str]:
    cleaned = clean_text(raw)
    if not cleaned:
        return []
    return [part.strip() for part in re.split(r"\n{2,}", cleaned) if part.strip()]


@dataclass
class NoteRecord:
    note_id: int
    deck_names: List[str]
    tags: List[str]
    notetype: str
    fields: Dict[str, str]


def ensure_zstd() -> str:
    zstd_path = shutil.which("zstd")
    if not zstd_path:
        raise SystemExit("Missing required binary: zstd")
    return zstd_path


def add_unicase_collation(conn: sqlite3.Connection) -> None:
    def unicase(left: str, right: str) -> int:
        left_folded = left.casefold()
        right_folded = right.casefold()
        if left_folded < right_folded:
            return -1
        if left_folded > right_folded:
            return 1
        return 0

    conn.create_collation("unicase", unicase)


def materialize_collection(apkg_path: Path) -> Path:
    ensure_zstd()
    tmp_dir = Path(tempfile.mkdtemp(prefix="apkg-md-"))
    with zipfile.ZipFile(apkg_path) as archive:
        members = set(archive.namelist())
        if "collection.anki21b" in members:
            compressed = tmp_dir / "collection.anki21b"
            compressed.write_bytes(archive.read("collection.anki21b"))
            sqlite_path = tmp_dir / "collection.sqlite3"
            subprocess.run(
                ["zstd", "-d", "-q", "-o", str(sqlite_path), str(compressed)],
                check=True,
            )
            return sqlite_path

        if "collection.anki2" in members:
            sqlite_path = tmp_dir / "collection.sqlite3"
            sqlite_path.write_bytes(archive.read("collection.anki2"))
            return sqlite_path

    raise SystemExit("No supported Anki collection found in archive")


def load_note_records(db_path: Path) -> List[NoteRecord]:
    conn = sqlite3.connect(db_path)
    add_unicase_collation(conn)

    notetypes = {
        row[0]: row[1]
        for row in conn.execute("select id, name from notetypes")
    }
    fields_by_notetype: Dict[int, List[str]] = {}
    for ntid, name in conn.execute("select ntid, name from fields order by ntid, ord"):
        fields_by_notetype.setdefault(ntid, []).append(name)

    deck_names = {
        row[0]: row[1]
        for row in conn.execute("select id, name from decks")
    }

    note_decks: Dict[int, List[str]] = {}
    for note_id, deck_name in conn.execute(
        """
        select c.nid, d.name
        from cards c
        join decks d on d.id = c.did
        group by c.nid, d.name
        order by c.nid, d.name
        """
    ):
        note_decks.setdefault(note_id, []).append(deck_name)

    records: List[NoteRecord] = []
    for note_id, mid, tags, flds in conn.execute(
        "select id, mid, tags, flds from notes order by id"
    ):
        field_names = fields_by_notetype.get(mid, [])
        values = flds.split(FIELD_SEPARATOR)
        mapped_fields = {
            name: values[index] if index < len(values) else ""
            for index, name in enumerate(field_names)
        }
        records.append(
            NoteRecord(
                note_id=note_id,
                deck_names=note_decks.get(note_id, []),
                tags=[tag for tag in tags.split() if tag],
                notetype=notetypes.get(mid, str(mid)),
                fields=mapped_fields,
            )
        )

    return records


def deck_path_parts(deck_names: Sequence[str]) -> List[str]:
    if not deck_names:
        return ["uncategorized"]

    primary = deck_names[0]
    parts = [part for part in primary.split(DECK_SEPARATOR) if part]
    if not parts:
        return ["uncategorized"]
    return [slugify(part) for part in parts]


def render_generic(record: NoteRecord) -> str:
    title = clean_text(record.fields.get(next(iter(record.fields), ""), "")) or f"Note {record.note_id}"
    lines = [
        f"# {title}",
        "",
        f"- Note ID: {record.note_id}",
        f"- Note Type: {record.notetype}",
        f"- Decks: {', '.join(name.replace(DECK_SEPARATOR, ' / ') for name in record.deck_names) or 'Uncategorized'}",
        f"- Tags: {', '.join(record.tags) if record.tags else 'None'}",
        "",
        "---",
        "",
    ]

    for field_name, raw_value in record.fields.items():
        value = clean_text(raw_value)
        if not value:
            continue
        lines.extend([f"## {field_name}", "", value, ""])

    return "\n".join(lines).strip() + "\n"


def pair_examples(fields: Dict[str, str]) -> Iterable[tuple[str, str]]:
    for index in range(1, 11):
        sentence = clean_text(fields.get(f"例文{index}", ""))
        translation = clean_text(fields.get(f"例文{index}_TL", ""))
        if sentence or translation:
            yield sentence, translation


def render_kyoushi(record: NoteRecord, ordinal: int) -> str:
    fields = record.fields
    title = clean_text(fields.get("文型", "")) or f"Lesson {ordinal:03d}"
    meaning = split_paragraphs(fields.get("意味", ""))
    english = split_paragraphs(fields.get("英訳", ""))
    connection = clean_text(fields.get("接続", ""))
    notes = split_paragraphs(fields.get("備考", ""))
    jlpt = clean_text(fields.get("JLPTレベル", ""))
    deck_label = " / ".join(name.replace(DECK_SEPARATOR, " / ") for name in record.deck_names) or "Uncategorized"

    lines = [
        f"# {ordinal:03d}. {title}",
        "",
        f"- Note ID: {record.note_id}",
        f"- Deck: {deck_label}",
        f"- Note Type: {record.notetype}",
        f"- JLPT: {jlpt or 'Unknown'}",
        f"- Tags: {', '.join(record.tags) if record.tags else 'None'}",
        "",
        "---",
        "",
    ]

    if meaning:
        lines.extend(["## Meaning", ""])
        lines.extend([f"- {item}" for item in meaning])
        lines.append("")

    if english:
        lines.extend(["## English", ""])
        lines.extend([f"- {item}" for item in english])
        lines.append("")

    if connection:
        lines.extend(["## Structure", "", connection, ""])

    if notes:
        lines.extend(["## Notes", ""])
        lines.extend([f"- {item}" for item in notes])
        lines.append("")

    examples = list(pair_examples(fields))
    if examples:
        lines.extend(["## Examples", ""])
        for index, (sentence, translation) in enumerate(examples, start=1):
            if sentence:
                lines.append(f"{index}. {sentence}")
            if translation:
                lines.append(f"   - {translation}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_record(record: NoteRecord, ordinal: int) -> str:
    if record.notetype == "kyoushi":
        return render_kyoushi(record, ordinal)
    return render_generic(record)


def write_markdown(records: Sequence[NoteRecord], output_dir: Path) -> int:
    deck_counters: Dict[tuple[str, ...], int] = {}
    written = 0

    for record in records:
        parts = deck_path_parts(record.deck_names)
        key = tuple(parts)
        deck_counters[key] = deck_counters.get(key, 0) + 1
        ordinal = deck_counters[key]

        title = clean_text(record.fields.get("文型", "")) or clean_text(
            next(iter(record.fields.values()), "")
        )
        title_slug = slugify(title)
        if title_slug == "section":
            title_slug = f"note-{record.note_id}"
        file_name = f"{ordinal:03d}-{title_slug}.md"
        target_dir = output_dir.joinpath(*parts)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / file_name
        target_path.write_text(render_record(record, ordinal), encoding="utf-8")
        written += 1

    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Path to the .apkg file")
    parser.add_argument("--output", required=True, help="Output directory for Markdown files")
    parser.add_argument(
        "--clear-output",
        action="store_true",
        help="Remove the output directory before generating files",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()

    if not input_path.is_file():
        raise SystemExit(f"Input file not found: {input_path}")

    if args.clear_output and output_dir.exists():
        shutil.rmtree(output_dir)

    db_path = materialize_collection(input_path)
    records = load_note_records(db_path)
    written = write_markdown(records, output_dir)
    print(f"Wrote {written} markdown files to {output_dir}")


if __name__ == "__main__":
    main()
