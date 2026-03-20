# pipeline_thinking.py — VLM pipeline with thinking mode ON
# Sends /think system message, extracts reasoning_content from response,
# saves markdown + Excel (with token counts) + per-run log files.
#
# ============================================================
# RUN MODES
# ============================================================
#
# --- IMAGE INPUT (no PDF conversion, use pre-existing images/) ---
#
# Mode 1: Single image file
#   python src/pipeline_thinking.py --input images/caris/caris_report/page_1.png
#
# Mode 2: All images in one pdf_stem directory
#   python src/pipeline_thinking.py --input images/caris/caris_report/
#
# Mode 3: All images under one source
#   python src/pipeline_thinking.py --input images/caris/
#
# Mode 4: All images under all sources (entire images/ tree)
#   python src/pipeline_thinking.py
#
# --- PDF INPUT (converts PDF pages to images first, then processes) ---
#
# Mode 5: Single PDF file
#   python src/pipeline_thinking.py --input pdf_docs/caris/report.pdf
#
# Mode 6: All PDFs in a source directory
#   python src/pipeline_thinking.py --input pdf_docs/caris/
#
# Mode 7: All PDFs under all sources (entire pdf_docs/ tree)
#   python src/pipeline_thinking.py --pdf_mode
#
# ============================================================
# OUTPUT
# ============================================================
# Markdown : output/thinking_markdown_{tag}/{source}/{pdf_stem}/page_N.md
#            (saved only on successful API call; iter-2 success overwrites iter-1 error)
# Excel    : output/thinking_excel_{tag}/{source}/{pdf_stem}.xlsx
#            Columns: pdf_name | page_number | iteration | reasoning_content
#                     | markdown_response | completion_tokens | prompt_tokens
#                     | total_tokens | num_chars_reasoning
#            One row per attempt (max 2 rows per page if first attempt fails).
# Log      : output/thinking_logs_{tag}/{source}/{pdf_stem}.log
#            One line per attempt: page_path | iteration | completion_tokens
#                                  | prompt_tokens | total_tokens | num_chars_reasoning
# ============================================================

import argparse
import asyncio
import base64
import importlib
import os
import re
import sys
from pathlib import Path

import fitz  # pymupdf
import openpyxl
from openai import AsyncOpenAI

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

PROMPT_REGISTRY = {
    "v4":     ("prompts.modified_ocr_prompt_v4",           "PARSE_PROMPT"),
    "v5":     ("prompts.modified_ocr_prompt_v5",           "PARSE_PROMPT"),
    "v6":     ("prompts.modified_ocr_prompt_v6",           "PARSE_PROMPT"),
    "tag_v0": ("prompts.modified_ocr_prompt_tag_v0",       "PARSE_PROMPT"),
    "v17":    ("prompts.nonNGS_vlm_30_10_25_gen_sum_v17",  "OCR_PROMPT"),
    "v18":    ("prompts.nonNGS_vlm_30_10_25_gen_sum_v18",  "OCR_PROMPT"),
}

BASE_DIR = REPO_ROOT
PDF_DOCS_DIR = BASE_DIR / "pdf_docs"
IMAGES_DIR = BASE_DIR / "images"
MARKDOWN_DIR = BASE_DIR / "output" / "thinking_markdown_default"
EXCEL_DIR = BASE_DIR / "output" / "thinking_excel_default"
LOG_DIR = BASE_DIR / "output" / "thinking_logs_default"

VLM_BASE_URL = "http://10.164.2.18:8001/v1"
VLM_API_KEY = "dummy"
VLM_MODEL = "nvidia/nemotron-nano-12b-v2-vl"
VLM_MAX_TOKENS = 30000
CONCURRENCY = 5
DPI = 150


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def encode_image(image_path: Path) -> str:
    """Read a PNG and return a base64 data URL."""
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# ---------------------------------------------------------------------------
# PDF → Images
# ---------------------------------------------------------------------------

def pdf_to_images(pdf_path: Path, source: str) -> list[Path]:
    """Convert each page of a PDF to a PNG image. Returns list of image paths."""
    pdf_stem = pdf_path.stem
    out_dir = IMAGES_DIR / source / pdf_stem
    os.makedirs(out_dir, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    image_paths = []
    mat = fitz.Matrix(DPI / 72, DPI / 72)

    for page_num, page in enumerate(doc, start=1):
        pixmap = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        img_path = out_dir / f"page_{page_num}.png"
        pixmap.save(str(img_path))
        image_paths.append(img_path)

    doc.close()
    print(f"  Converted {len(image_paths)} pages: {pdf_path.name}")
    return image_paths


def collect_pdfs(input_path: Path) -> list[tuple[Path, str]]:
    """Return list of (pdf_path, source_name) for a file or directory."""
    if input_path.is_file():
        if input_path.suffix.lower() != ".pdf":
            print(f"ERROR: {input_path} is not a PDF file")
            sys.exit(1)
        return [(input_path, input_path.parent.name)]

    if input_path.is_dir():
        pdfs = sorted(input_path.glob("*.pdf"))
        if not pdfs:
            print(f"No PDF files found in {input_path}")
            sys.exit(1)
        return [(p, input_path.name) for p in pdfs]

    print(f"ERROR: {input_path} does not exist")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Image collection
# ---------------------------------------------------------------------------

def collect_images(input_path: Path) -> list[tuple[Path, str, str]]:
    """
    Return list of (image_path, source, pdf_stem) from a file or directory.

    Accepts:
      - A single .png file  : images/{source}/{pdf_stem}/page_N.png
      - A pdf_stem dir      : images/{source}/{pdf_stem}/
      - A source dir        : images/{source}/   (all pdf_stems underneath)
      - The images/ root    : images/            (all sources + pdf_stems)
    """
    input_path = input_path.resolve()
    images_dir = IMAGES_DIR.resolve()

    def _make_entry(p: Path) -> tuple[Path, str, str] | None:
        try:
            rel = p.relative_to(images_dir)
        except ValueError:
            return None
        parts = rel.parts
        if len(parts) != 3:
            return None
        source, pdf_stem, _ = parts
        return (p, source, pdf_stem)

    if input_path.is_file():
        if input_path.suffix.lower() != ".png":
            print(f"ERROR: {input_path} is not a PNG file")
            sys.exit(1)
        entry = _make_entry(input_path)
        if entry is None:
            print(f"ERROR: {input_path} must be under {images_dir}/{{source}}/{{pdf_stem}}/")
            sys.exit(1)
        return [entry]

    if input_path.is_dir():
        pngs = sorted(p for p in input_path.rglob("*.png")
                      if ".ipynb_checkpoints" not in p.parts)
        if not pngs:
            print(f"No PNG files found under {input_path}")
            sys.exit(1)
        entries = [e for p in pngs if (e := _make_entry(p)) is not None]
        return entries

    print(f"ERROR: {input_path} does not exist")
    sys.exit(1)


# ---------------------------------------------------------------------------
# VLM call
# ---------------------------------------------------------------------------

async def process_page(
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    image_path: Path,
    source: str,
    pdf_stem: str,
    page_num: int,
    prompt_text: str,
    temp: float,
    top_p: float,
) -> list[tuple[int, int, str, str, int, int, int, int]]:
    """
    Send one page image to the VLM with thinking ON.  Retries once on failure.

    Returns a list of row tuples — one per attempt:
      (page_num, iteration, markdown, reasoning,
       completion_tokens, prompt_tokens, total_tokens, num_chars_reasoning)

    iteration=1 always present; iteration=2 appended only when attempt 1 fails.
    On exception the row records the error message in the reasoning field and
    zeros for all token counts; no markdown file is saved for failed attempts.
    """
    image_url = encode_image(image_path)
    rows: list[tuple[int, int, str, str, int, int, int, int]] = []

    for iteration in range(1, 3):  # attempt 1, then (if needed) attempt 2
        try:
            async with semaphore:
                response = await client.chat.completions.create(
                    model=VLM_MODEL,
                    max_tokens=VLM_MAX_TOKENS,
                    temperature=temp,
                    top_p=top_p,
                    messages=[
                        {"role": "system", "content": "/think"},
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": image_url}},
                                {"type": "text", "text": prompt_text},
                            ],
                        },
                    ],
                )

            message = response.choices[0].message
            markdown = message.content or ""
            reasoning = getattr(message, "reasoning_content", None) or ""

            usage = response.usage
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", 0) or 0
            num_chars_reasoning = len(reasoning)

            # Save markdown only on success (iter-2 success overwrites iter-1 .md if any)
            md_dir = MARKDOWN_DIR / source / pdf_stem
            os.makedirs(md_dir, exist_ok=True)
            md_path = md_dir / f"page_{page_num}.md"
            md_path.write_text(markdown, encoding="utf-8")

            print(
                f"    [iter {iteration}] Saved {md_path.relative_to(BASE_DIR)}"
                f"  [completion={completion_tokens}, prompt={prompt_tokens},"
                f" total={total_tokens}, reasoning_chars={num_chars_reasoning}]"
            )

            rows.append((page_num, iteration, markdown, reasoning,
                         completion_tokens, prompt_tokens, total_tokens, num_chars_reasoning))
            break  # success — no retry needed

        except Exception as exc:
            err_msg = str(exc)
            print(f"    [iter {iteration}] Page {page_num} FAILED: {err_msg}"
                  + ("  — retrying..." if iteration == 1 else "  — max retries reached."))
            rows.append((page_num, iteration, "", f"ERROR: {err_msg}", 0, 0, 0, 0))
            # loop continues to iteration 2 if this was attempt 1

    return rows


# ---------------------------------------------------------------------------
# Per-pdf_stem orchestration
# ---------------------------------------------------------------------------

async def process_pdf_stem(
    image_paths: list[Path],
    source: str,
    pdf_stem: str,
    prompt_text: str,
    temp: float,
    top_p: float,
    prompt_key: str = "",
) -> None:
    """Process all pages for one pdf_stem concurrently, write Excel and log.

    Each page produces 1 row (success on first try) or 2 rows (retry after failure).
    The iteration column records which attempt each row belongs to (1 or 2).
    """
    client = AsyncOpenAI(base_url=VLM_BASE_URL, api_key=VLM_API_KEY)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    # Build page_num → image_path lookup for log writing
    page_image_map = {page_num: img_path for page_num, img_path in enumerate(image_paths, start=1)}

    tasks = [
        process_page(client, semaphore, img_path, source, pdf_stem, page_num,
                     prompt_text, temp, top_p)
        for page_num, img_path in enumerate(image_paths, start=1)
    ]
    # Each result is a list of row tuples for one page; flatten into a single list
    per_page_results = await asyncio.gather(*tasks)
    all_rows = [row for page_rows in per_page_results for row in page_rows]

    # Sort by (page_num, iteration) so the sheet reads in order
    all_rows_sorted = sorted(all_rows, key=lambda x: (x[0], x[1]))

    # ------------------------------------------------------------------
    # Write Excel
    # Columns: pdf_name | page_number | iteration | reasoning_content
    #        | markdown_response | completion_tokens | prompt_tokens
    #        | total_tokens | num_chars_reasoning
    # ------------------------------------------------------------------
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Results"
    ws.append([
        "pdf_name", "page_number", "iteration",
        "reasoning_content", "markdown_response",
        "completion_tokens", "prompt_tokens", "total_tokens", "num_chars_reasoning",
    ])
    for page_num, iteration, markdown, reasoning, comp_tok, prompt_tok, total_tok, n_chars in all_rows_sorted:
        ws.append([pdf_stem, page_num, iteration, reasoning, markdown,
                   comp_tok, prompt_tok, total_tok, n_chars])

    excel_dir = EXCEL_DIR / source
    os.makedirs(excel_dir, exist_ok=True)
    excel_path = excel_dir / f"{pdf_stem}.xlsx"
    wb.save(str(excel_path))
    print(f"  Saved Excel: {excel_path.relative_to(BASE_DIR)}")

    # ------------------------------------------------------------------
    # Write log file  (one file per pdf_stem, one line per attempt)
    # Columns: page_path | iteration | completion_tokens | prompt_tokens
    #          | total_tokens | num_chars_reasoning
    # ------------------------------------------------------------------
    log_dir = LOG_DIR / source
    os.makedirs(log_dir, exist_ok=True)
    log_path = log_dir / f"{pdf_stem}.log"

    with open(log_path, "w", encoding="utf-8") as lf:
        # --- run configuration header ---
        lf.write("=" * 80 + "\n")
        lf.write(f"  RUN CONFIG\n")
        lf.write("=" * 80 + "\n")
        lf.write(f"  pdf_stem        : {pdf_stem}\n")
        lf.write(f"  source          : {source}\n")
        lf.write(f"  prompt_key      : {prompt_key}\n")
        lf.write(f"  prompt_text_len : {len(prompt_text)} chars\n")
        lf.write(f"  temp            : {temp}\n")
        lf.write(f"  top_p           : {top_p}\n")
        lf.write(f"  max_tokens      : {VLM_MAX_TOKENS}\n")
        lf.write(f"  model           : {VLM_MODEL}\n")
        lf.write(f"  concurrency     : {CONCURRENCY}\n")
        lf.write(f"  total_pages     : {len(image_paths)}\n")
        lf.write(f"  markdown_dir    : {MARKDOWN_DIR}\n")
        lf.write(f"  excel_dir       : {EXCEL_DIR}\n")
        lf.write(f"  log_dir         : {LOG_DIR}\n")
        lf.write("=" * 80 + "\n\n")
        # --- per-page data ---
        lf.write(
            f"{'page_path':<60} {'iter':>5} {'completion_tokens':>18} {'prompt_tokens':>14}"
            f" {'total_tokens':>13} {'num_chars_reasoning':>20}\n"
        )
        lf.write("-" * 135 + "\n")
        for page_num, iteration, _, _, comp_tok, prompt_tok, total_tok, n_chars in all_rows_sorted:
            img_path = page_image_map[page_num]
            try:
                page_path_str = str(img_path.relative_to(BASE_DIR))
            except ValueError:
                page_path_str = str(img_path)
            lf.write(
                f"{page_path_str:<60} {iteration:>5} {comp_tok:>18} {prompt_tok:>14}"
                f" {total_tok:>13} {n_chars:>20}\n"
            )

    print(f"  Saved Log:   {log_path.relative_to(BASE_DIR)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="VLM pipeline with thinking ON — Image/PDF → Markdown + Excel + Log"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=(
            "Path to a single PNG/PDF, a pdf_stem directory, a source directory, "
            "or the images/ root. "
            "If omitted and --pdf_mode is not set, processes all images under images/."
        ),
    )
    parser.add_argument(
        "--pdf_mode",
        action="store_true",
        help=(
            "Process all PDFs under pdf_docs/ (converts each to images first). "
            "Ignored when --input is provided."
        ),
    )
    parser.add_argument(
        "--prompt",
        default="v5",
        choices=list(PROMPT_REGISTRY.keys()),
        help="Prompt variant to use (default: v5)",
    )
    parser.add_argument(
        "--temp",
        type=float,
        default=0.5,
        help="Sampling temperature (default: 0.5)",
    )
    parser.add_argument(
        "--top_p",
        type=float,
        default=0.7,
        help="Top-p nucleus sampling (default: 0.7)",
    )
    args = parser.parse_args()

    # Load prompt dynamically
    module_path, var_name = PROMPT_REGISTRY[args.prompt]
    mod = importlib.import_module(module_path)
    prompt_text = getattr(mod, var_name)

    # Build output dirs from args
    tag = f"{args.prompt}_temp_{str(args.temp).replace('.','')}_p_{str(args.top_p).replace('.','')}"
    global MARKDOWN_DIR, EXCEL_DIR, LOG_DIR
    MARKDOWN_DIR = BASE_DIR / "output" / f"thinking_markdown_{tag}"
    EXCEL_DIR    = BASE_DIR / "output" / f"thinking_excel_{tag}"
    LOG_DIR      = BASE_DIR / "output" / f"thinking_logs_{tag}"

    print(f"Prompt: {args.prompt}  temp={args.temp}  top_p={args.top_p}")
    print(f"MARKDOWN_DIR: {MARKDOWN_DIR.relative_to(BASE_DIR)}")
    print(f"EXCEL_DIR:    {EXCEL_DIR.relative_to(BASE_DIR)}")
    print(f"LOG_DIR:      {LOG_DIR.relative_to(BASE_DIR)}")

    # ---- PDF mode (--pdf_mode or --input pointing to a PDF/PDF dir) --------
    input_is_pdf = (
        args.input is not None
        and args.input.resolve().is_file()
        and args.input.suffix.lower() == ".pdf"
    )
    input_is_pdf_dir = (
        args.input is not None
        and args.input.resolve().is_dir()
        and any(args.input.resolve().glob("*.pdf"))
        and not any(args.input.resolve().glob("*.png"))
    )

    if args.pdf_mode or input_is_pdf or input_is_pdf_dir:
        if args.input:
            pdf_list = collect_pdfs(args.input.resolve())
        else:
            if not PDF_DOCS_DIR.exists():
                print(f"ERROR: pdf_docs directory not found at {PDF_DOCS_DIR}")
                sys.exit(1)
            pdf_list = []
            for source_dir in sorted(d for d in PDF_DOCS_DIR.iterdir() if d.is_dir()):
                for pdf_path in sorted(source_dir.glob("*.pdf")):
                    pdf_list.append((pdf_path, source_dir.name))

        if not pdf_list:
            print("No PDFs to process.")
            sys.exit(0)

        print(f"Processing {len(pdf_list)} PDF(s) [PDF mode]...")
        for pdf_path, source in pdf_list:
            print(f"\n[{source}] {pdf_path.name}")
            image_paths = pdf_to_images(pdf_path, source)
            asyncio.run(process_pdf_stem(image_paths, source, pdf_path.stem, prompt_text, args.temp, args.top_p, prompt_key=args.prompt))

        print("\nDone.")
        return

    # ---- Image mode --------------------------------------------------------
    if args.input:
        entries = collect_images(args.input.resolve())
    else:
        if not IMAGES_DIR.exists():
            print(f"ERROR: images directory not found at {IMAGES_DIR}")
            sys.exit(1)
        pngs = sorted(p for p in IMAGES_DIR.rglob("*.png")
                      if ".ipynb_checkpoints" not in p.parts)
        entries = []
        for p in pngs:
            try:
                rel = p.relative_to(IMAGES_DIR)
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) == 3:
                entries.append((p, parts[0], parts[1]))

    if not entries:
        print("No images to process.")
        sys.exit(0)

    # Group by (source, pdf_stem) so we write one Excel + one log per pdf_stem
    grouped: dict[tuple[str, str], list[Path]] = {}
    for img_path, source, pdf_stem in entries:
        key = (source, pdf_stem)
        grouped.setdefault(key, []).append(img_path)

    total_pages = sum(len(v) for v in grouped.values())
    print(f"Processing {total_pages} page(s) across {len(grouped)} pdf_stem(s) [image mode]...")

    for (source, pdf_stem), img_paths in sorted(grouped.items()):
        print(f"\n[{source}] {pdf_stem}  ({len(img_paths)} pages)")
        # Sort by page number extracted from filename (page_N.png)
        img_paths_sorted = sorted(img_paths, key=lambda p: int(re.search(r"(\d+)", p.stem).group(1)))
        asyncio.run(process_pdf_stem(img_paths_sorted, source, pdf_stem, prompt_text, args.temp, args.top_p, prompt_key=args.prompt))

    print("\nDone.")


if __name__ == "__main__":
    main()
