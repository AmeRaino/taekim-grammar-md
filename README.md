# Tae Kim Grammar Markdown Corpus (RAG-Ready)

This project converts the mirrored Tae Kim grammar HTML into a structured Markdown corpus for Retrieval-Augmented Generation (RAG).

## Purpose

The original resource is a single large HTML file (`taekim.html`), which is not ideal for LLM retrieval.

This repo splits the content into many small Markdown files organized by chapter/subchapter so retrieval can load only relevant lessons.

## Source

- Original guide: https://www.guidetojapanese.org/learn/grammar
- Mirror used in this repo: `taekim.html`
- HTML file credit: https://github.com/kenrick95/itazuraneko/tree/master

## Project Structure

- `taekim.html`: source HTML (single page)
- `scripts/split_taekim.py`: splitter script
- `scripts/apkg_to_md.py`: Anki `.apkg` to Markdown converter
- `taekim-md/`: generated Markdown corpus
- `taekim-md/llms.txt`: index for retrieval routing

## Output Format

Generated files are organized like:

- `taekim-md/<chapter>/<subchapter>/<NNN-title>.md`

For Anki exports, generated files are organized like:

- `<output>/<deck>/<subdeck>/<NNN-title>.md`

Each lesson file includes:

- lesson number and title
- chapter/subchapter metadata
- lesson content converted to Markdown

## Regenerate

```bash
python3 scripts/split_taekim.py --input taekim.html --output taekim-md
```

```bash
python3 scripts/apkg_to_md.py \
  --input "Nihongo Kyoushi.apkg" \
  --output nihongo-kyoushi-md \
  --clear-output
```

## Notes

- Some complex tables are kept as HTML inside Markdown to preserve structure.
- This is usually compatible with Markdown renderers and most indexing pipelines.
