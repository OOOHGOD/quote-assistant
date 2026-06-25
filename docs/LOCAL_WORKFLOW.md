# Local Quote Workflow

This workflow uses local PDFs and local Excel templates only. It does not use
Google Drive or any other cloud storage as a document store.

External services are limited to:

- PaddleOCR API for OCR and layout parsing.
- DeepSeek API for structured quote extraction.

## Conda Setup

```powershell
conda env create -f environment.yml
conda activate quote-assistant
```

Set API credentials in the shell that runs the workflow:

```powershell
$env:PADDLEOCR_TOKEN = "your-paddleocr-token"
$env:PADDLEOCR_MODEL = "PaddleOCR-VL"
$env:DEEPSEEK_API_KEY = "your-deepseek-key"
$env:DEEPSEEK_MODEL = "deepseek-v4-flash"
```

## Commands

Import a local Excel template:

```powershell
python -m quote_assistant.cli import-template --template .\templates\your-template.xlsx
```

Activate a reviewed mapping:

```powershell
python -m quote_assistant.cli activate-template --mapping-json .\templates\template_mapping.draft.json --reviewer "Reviewer" --confirm-format-immutable
```

Run a local PDF through PaddleOCR and DeepSeek:

```powershell
python -m quote_assistant.cli run-local --pdf .\samples\quote-normal.pdf --reviewer "Reviewer"
```

Approve and export in one pass when validation has no blocking issues:

```powershell
python -m quote_assistant.cli run-local --pdf .\samples\quote-normal.pdf --reviewer "Reviewer" --approve --export
```

Run acceptance checks:

```powershell
python -m quote_assistant.cli acceptance
```

## Data Flow

```text
local PDF
  -> PaddleOCR async API
  -> local OCR artifacts under data/jobs/<job_id>/ocr/
  -> DeepSeek quote extraction agent
  -> structured quote JSON
  -> validation and local review state
  -> fixed local Excel template export
```

Generated files stay under the local project directory:

- `data/jobs/<job_id>/source.pdf`
- `data/jobs/<job_id>/ocr/ocr.md`
- `data/jobs/<job_id>/ocr/ocr.jsonl`
- `data/jobs/<job_id>/job.json`
- `output/quote-<job_id>.xlsx` or `.xlsm`
