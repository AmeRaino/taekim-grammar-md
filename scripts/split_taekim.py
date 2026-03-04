#!/usr/bin/env python3
"""Split taekim.html into hierarchy of markdown files for RAG."""

from __future__ import annotations

import argparse
import html
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class TocEntry:
    number: int
    title: str
    chapter: str
    subchapter: Optional[str]


def strip_tags(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    return " ".join(text.split()).strip()


def slugify(text: str, max_len: int = 80) -> str:
    norm = unicodedata.normalize("NFKD", text)
    norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
    norm = norm.lower()
    norm = re.sub(r"[^a-z0-9\s-]", "", norm)
    norm = re.sub(r"\s+", "-", norm).strip("-")
    norm = re.sub(r"-+", "-", norm)
    return (norm[:max_len].rstrip("-") or "section")


def parse_toc_hierarchy(full_html: str) -> Dict[int, TocEntry]:
    # The file has two "Before you start" h2 headers. The first block is TOC/meta.
    first = full_html.find("<h2>Before you start</h2>")
    second = full_html.find("<h2>Before you start</h2>", first + 1)
    if first < 0 or second < 0:
        raise ValueError("Could not locate TOC boundary in taekim.html")
    toc_html = full_html[:second]

    token_re = re.compile(
        r"(<h2>.*?</h2>|<h4>.*?</h4>|<li class=\"toc\">\s*\d+\s*<a[^>]*>.*?</a>\s*</li>)",
        re.S,
    )

    current_chapter: Optional[str] = None
    current_subchapter: Optional[str] = None
    by_number: Dict[int, TocEntry] = {}

    for token in token_re.findall(toc_html):
        if token.startswith("<h2>"):
            current_chapter = strip_tags(token)
            current_subchapter = None
            continue

        if token.startswith("<h4>"):
            current_subchapter = strip_tags(token)
            continue

        m = re.search(r"<li class=\"toc\">\s*(\d+)\s*<a[^>]*>(.*?)</a>", token, re.S)
        if not m:
            continue

        number = int(m.group(1))
        title = strip_tags(m.group(2))
        if not current_chapter:
            current_chapter = "Uncategorized"

        by_number[number] = TocEntry(
            number=number,
            title=title,
            chapter=current_chapter,
            subchapter=current_subchapter,
        )

    return by_number


def extract_section_raw_html(full_html: str) -> Dict[int, Tuple[str, str]]:
    pattern = re.compile(r"<h4 id=\"(\d+) ([^\"]+)\">(.*?)</h4>", re.S)
    matches = list(pattern.finditer(full_html))
    if not matches:
        raise ValueError("No numbered sections found")

    sections: Dict[int, Tuple[str, str]] = {}

    for i, match in enumerate(matches):
        num = int(match.group(1))
        header_inner = match.group(3)
        title = strip_tags(header_inner)
        # Strip optional numeric prefix from heading text, keep canonical title.
        title = re.sub(r"^\d+\s+", "", title)

        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_html)
        raw = full_html[start:end]
        sections[num] = (title, raw)

    return sections


class HtmlToMarkdown(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.href_stack: List[str] = []
        self.list_stack: List[str] = []
        self.in_pre = False
        self.in_table = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = {k: (v or "") for k, v in attrs}

        if tag in {"h2", "h3", "h4"}:
            self._newline(2)
            level = {"h2": "## ", "h3": "### ", "h4": "#### "}[tag]
            self.out.append(level)
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
        elif tag == "br":
            self.out.append("\n")
        elif tag in {"strong", "b"}:
            self.out.append("**")
        elif tag in {"em", "i"}:
            self.out.append("*")
        elif tag == "a":
            self.href_stack.append(attrs_dict.get("href", ""))
            self.out.append("[")
        elif tag == "hr":
            self._newline(2)
            self.out.append("---")
            self._newline(2)
        elif tag in {"table", "tr", "th", "td", "caption"}:
            # Preserve table markup to avoid losing alignment-sensitive charts.
            self.in_table = True
            self._newline(1)
            self.out.append(f"<{tag}>")
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
        if tag in {"h2", "h3", "h4", "p", "li", "ul", "ol", "table", "tr", "caption"}:
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
        if tag in {"table", "tr", "th", "td", "caption"}:
            self.out.append(f"</{tag}>")
            if tag == "table":
                self.in_table = False

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self.in_pre:
            self.out.append(data)
            return
        text = html.unescape(data)
        text = text.replace("\xa0", " ")
        if not self.in_table:
            text = re.sub(r"[ \t]+", " ", text)
        if text.strip() or self.in_table:
            self.out.append(text)

    def markdown(self) -> str:
        text = "".join(self.out)
        # Cleanup whitespace and over-separation.
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n +", "\n", text)
        return text.strip() + "\n"

    def _newline(self, n: int) -> None:
        if not self.out:
            return
        needed = n
        while needed > 0:
            if not "".join(self.out).endswith("\n"):
                self.out.append("\n")
                needed -= 1
            else:
                # Count current trailing newlines.
                tail = "".join(self.out)[-4:]
                trailing = len(tail) - len(tail.rstrip("\n"))
                if trailing >= n:
                    return
                self.out.append("\n")
                needed -= 1


def convert_html_to_markdown(section_html: str) -> str:
    parser = HtmlToMarkdown()
    parser.feed(section_html)
    return parser.markdown()


def build_output(
    sections: Dict[int, Tuple[str, str]],
    toc: Dict[int, TocEntry],
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    entries: List[Tuple[int, Path, TocEntry]] = []

    for number in sorted(sections):
        fallback_title, raw = sections[number]
        meta = toc.get(number)

        title = meta.title if meta else fallback_title
        chapter = meta.chapter if meta else "Uncategorized"
        subchapter = meta.subchapter if meta else None

        chapter_dir = slugify(chapter)
        parts = [out_dir, chapter_dir]
        if subchapter:
            parts.append(slugify(subchapter))
        target_dir = Path(*parts)
        target_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{number:03d}-{slugify(title)}.md"
        file_path = target_dir / filename

        md = convert_html_to_markdown(raw)
        header = [
            f"# {number}. {title}",
            "",
            f"- Chapter: {chapter}",
            f"- Subchapter: {subchapter or '-'}",
            "",
            "---",
            "",
        ]
        file_path.write_text("\n".join(header) + md, encoding="utf-8")

        if not meta:
            meta = TocEntry(number=number, title=title, chapter=chapter, subchapter=subchapter)
        entries.append((number, file_path.relative_to(out_dir), meta))

    llms_lines = [
        "# Tae Kim Grammar Guide (Split Index)",
        "",
        "This index lists each lesson file for retrieval and chunked loading.",
        "",
    ]

    current_chapter = None
    current_subchapter = None
    for number, rel_path, meta in entries:
        if meta.chapter != current_chapter:
            current_chapter = meta.chapter
            current_subchapter = None
            llms_lines.extend([f"## {current_chapter}", ""])
        if meta.subchapter != current_subchapter:
            current_subchapter = meta.subchapter
            if current_subchapter:
                llms_lines.extend([f"### {current_subchapter}", ""])
        llms_lines.append(f"- {number:03d} {meta.title} -> {rel_path.as_posix()}")

    (out_dir / "llms.txt").write_text("\n".join(llms_lines).strip() + "\n", encoding="utf-8")

    readme = """# Tae Kim Split Markdown

Generated from `taekim.html` for RAG-friendly retrieval.

## Structure

- `llms.txt`: global index of all lessons and file locations.
- `chapter/subchapter/*.md`: one lesson per file (234 total).

## Regenerate

```bash
python3 scripts/split_taekim.py --input taekim.html --output taekim-md
```
"""
    (out_dir / "README.md").write_text(readme, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Split taekim HTML into markdown hierarchy")
    parser.add_argument("--input", default="taekim.html", help="Path to source taekim HTML")
    parser.add_argument("--output", default="taekim-md", help="Output directory")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_dir = Path(args.output)

    full_html = in_path.read_text(encoding="utf-8", errors="ignore")
    toc = parse_toc_hierarchy(full_html)
    sections = extract_section_raw_html(full_html)
    build_output(sections, toc, out_dir)

    print(f"Wrote {len(sections)} lesson files to {out_dir}")


if __name__ == "__main__":
    main()
