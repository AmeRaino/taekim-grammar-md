# Tae Kim Split Markdown

Generated from `taekim.html` for RAG-friendly retrieval.

## Structure

- `llms.txt`: global index of all lessons and file locations.
- `chapter/subchapter/*.md`: one lesson per file (234 total).

## Regenerate

```bash
python3 scripts/split_taekim.py --input taekim.html --output taekim-md
```
