import re
import time
import traceback
from pathlib import Path
from datetime import datetime

from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling_core.types.doc import ImageRefMode


# -----------------------------
# User input helpers
# -----------------------------

def ask_path(prompt_text: str) -> Path:
    # Accept pasted Windows paths with backslashes.
    # Strip surrounding quotation marks from paths copied via "Copy as path".
    raw = input(prompt_text).strip().strip('"').strip("'")

    if not raw:
        raise ValueError("No path entered.")

    return Path(raw)


def ask_yes_no(prompt_text: str, default: bool) -> bool:
    # Simple guarded yes/no prompt.
    # Empty input uses the displayed default.
    default_label = "Y/n" if default else "y/N"

    while True:
        answer = input(f"{prompt_text} [{default_label}]: ").strip().lower()

        if not answer:
            return default

        if answer in {"y", "yes", "j", "ja"}:
            return True

        if answer in {"n", "no", "nein"}:
            return False

        print("Please enter y or n.")


def validate_pdf_path(path: Path) -> None:
    # Validate before Docling starts loading models.
    if not path.exists():
        raise FileNotFoundError(f"Input path does not exist: {path}")

    if not path.is_file():
        raise ValueError(f"Input path is not a file: {path}")

    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Input file must be a PDF: {path}")

    # Fail early with a clear error for empty files.
    if path.stat().st_size == 0:
        raise ValueError(f"Input PDF is empty: {path}")


def validate_input_folder(path: Path) -> None:
    # Validate before Docling starts loading models.
    if not path.exists():
        raise FileNotFoundError(f"Input folder does not exist: {path}")

    if not path.is_dir():
        raise ValueError(f"Input path is not a folder: {path}")


def find_pdf_files(input_folder: Path) -> list[Path]:
    # Convert all PDF files directly inside the selected folder.
    # This intentionally does not crawl subfolders.
    return sorted(
        path for path in input_folder.iterdir()
        if path.is_file() and path.suffix.lower() == ".pdf"
    )


def make_output_path(input_pdf: Path, output_dir: Path) -> Path:
    # Stable output filename.
    # Example: source.pdf -> source.md
    return output_dir / f"{input_pdf.stem}.md"


def prevent_overwrite(path: Path) -> None:
    # Avoid silent data loss.
    if path.exists():
        raise FileExistsError(f"Output file already exists: {path}")


# -----------------------------
# LLM-oriented Markdown cleanup
# -----------------------------

def normalize_markdown_for_llm(md: str) -> str:
    # Normalize line endings for predictable downstream cleanup.
    md = md.replace("\r\n", "\n").replace("\r", "\n")

    # Remove trailing whitespace.
    lines = [line.rstrip() for line in md.split("\n")]
    md = "\n".join(lines)

    # Collapse excessive empty lines.
    # Keeps Markdown readable without over-normalizing lists/tables.
    while "\n\n\n\n" in md:
        md = md.replace("\n\n\n\n", "\n\n\n")

    return md.strip() + "\n"


def repair_split_key_value_lines(md: str) -> tuple[str, int]:
    # Repairs a common PDF layout artifact where a short key and its value are split:
    #   Field Name
    #
    #   : value
    # becomes:
    #   Field Name: value
    #
    # This is intentionally conservative:
    # - key line must be short
    # - key line must start with a letter
    # - key line must not be a Markdown heading/list/table/comment
    # - value must be on the next colon-prefixed line
    repairs = 0

    key_pattern = r"(?![#|<\-*])([A-Za-z][A-Za-z0-9 /&()\-]{1,60})"

    def repl(match: re.Match) -> str:
        nonlocal repairs
        repairs += 1
        key = match.group(1).strip()
        value = match.group(2).strip()
        return f"{key}: {value}"

    md = re.sub(
        rf"(?m)^{key_pattern}\s*\n\s*\n?\s*:\s*([^\n]+)",
        repl,
        md,
    )

    return md, repairs


def fix_known_low_risk_artifacts(md: str) -> tuple[str, int]:
    # Fix known low-risk artifacts observed in Docling Markdown output.
    # Keep this list narrow. Broad word correction can corrupt valid source text.
    replacements = {
        r"\boneto-one\b": "one-to-one",
        r"\bone to-one\b": "one-to-one",
        r"\b8\.5x1 1\b": "8.5x11",
    }

    changes = 0
    for pattern, replacement in replacements.items():
        md, count = re.subn(pattern, replacement, md)
        changes += count

    return md, changes


def remove_likely_visual_ocr_blocks(md: str) -> tuple[str, int]:
    # Optional removal of likely OCR text extracted from full-page maps/illustrations.
    # This is deliberately conservative and only targets dense runs of very short all-caps map-label lines
    # immediately after an image placeholder.
    # Default in main() is OFF because some PDFs contain useful image text.
    lines = md.split("\n")
    result = []
    removed_blocks = 0
    i = 0

    while i < len(lines):
        result.append(lines[i])

        if lines[i].strip() != "<!-- image -->":
            i += 1
            continue

        # Inspect the following block until a Markdown heading, list item, numbered location, or paragraph-like line.
        j = i + 1
        block = []
        while j < len(lines):
            stripped = lines[j].strip()

            # Stop at content that is likely normal prose or structure.
            if re.match(r"^(#{1,6}\s|[-*]\s|\([A-Z]\d+\)|[A-Z][a-z].{30,})", stripped):
                break

            block.append(lines[j])
            j += 1

        nonempty = [line.strip() for line in block if line.strip()]

        # Map OCR blocks often contain many short uppercase labels, cardinal fragments, and place names.
        short_upper = [
            line for line in nonempty
            if len(line) <= 35 and re.fullmatch(r"[A-Z0-9 .,'’\-]+", line)
        ]

        # Remove only if the block is large enough to be visual/map OCR noise.
        if len(nonempty) >= 12 and len(short_upper) / max(len(nonempty), 1) >= 0.60:
            removed_blocks += 1
            result.append("<!-- likely visual/map OCR text omitted -->")
            i = j
        else:
            i += 1

    return "\n".join(result), removed_blocks


def postprocess_markdown_for_llm(md: str, remove_visual_ocr_blocks: bool) -> tuple[str, dict[str, int]]:
    # Lightweight post-processing inside the conversion script.
    # More aggressive cleanup should stay in the separate post-processing script.
    stats = {
        "low_risk_artifact_fixes": 0,
        "split_key_value_repairs": 0,
        "visual_ocr_blocks_removed": 0,
    }

    md = normalize_markdown_for_llm(md)

    md, count = fix_known_low_risk_artifacts(md)
    stats["low_risk_artifact_fixes"] += count

    md, count = repair_split_key_value_lines(md)
    stats["split_key_value_repairs"] += count

    if remove_visual_ocr_blocks:
        md, count = remove_likely_visual_ocr_blocks(md)
        stats["visual_ocr_blocks_removed"] += count

    md = normalize_markdown_for_llm(md)
    return md, stats


def build_metadata_header(input_pdf: Path, use_ocr: bool, force_full_page_ocr: bool) -> str:
    # Add lightweight provenance useful for LLM project files.
    # Do not use YAML front matter unless your downstream tooling expects it.
    converted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return (
        f"# {input_pdf.stem}\n\n"
        f"Source file: `{input_pdf.name}`\n"
        f"Converted with: `Docling`\n"
        f"PDF backend: `PyPdfiumDocumentBackend`\n"
        f"Converted at: `{converted_at}`\n"
        f"OCR enabled: `{use_ocr}`\n"
        f"Force full-page OCR: `{force_full_page_ocr}`\n\n"
        "---\n\n"
    )


# -----------------------------
# Docling conversion
# -----------------------------

def build_converter(use_ocr: bool, force_full_page_ocr: bool) -> DocumentConverter:
    # Docling PDF pipeline options.
    # Default OCR is controlled by the user because OCR can damage born-digital PDFs
    # with stylized headings and can inject noisy map/image text.
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = use_ocr

    # Test preset: prefer the PDF's embedded text layer where possible.
    pipeline_options.force_backend_text = True

    # Full-page OCR should normally be off.
    # Enable it only when embedded text is bad or missing.
    if use_ocr:
        pipeline_options.ocr_options.force_full_page_ocr = force_full_page_ocr

    # Use table structure extraction for better Markdown tables.
    pipeline_options.do_table_structure = True

    # Accurate mode is slower but better for LLM reference material.
    # Docling documents TableFormerMode.FAST and ACCURATE; ACCURATE is the quality-oriented setting.
    pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE

    # Keep remote services disabled.
    # This avoids accidental upload of local documents to external services.
    pipeline_options.enable_remote_services = False

    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_options,
                backend=PyPdfiumDocumentBackend
            )
        }
    )


def convert_pdf_to_markdown(
    input_pdf: Path,
    output_md: Path,
    use_ocr: bool,
    force_full_page_ocr: bool,
    remove_visual_ocr_blocks: bool,
) -> dict[str, int]:
    converter = build_converter(
        use_ocr=use_ocr,
        force_full_page_ocr=force_full_page_ocr,
    )

    print("Starting Docling conversion...", flush=True)
    start = time.time()

    # raises_on_error=False lets us inspect status and report cleanly.
    result = converter.convert(input_pdf, raises_on_error=False)

    runtime = time.time() - start

    if result.status != ConversionStatus.SUCCESS:
        # Surface Docling's detailed error messages when available.
        details = []
        for item in getattr(result, "errors", []) or []:
            details.append(getattr(item, "error_message", str(item)))

        if details:
            detail_text = "\n".join(f"- {msg}" for msg in details)
            raise RuntimeError(
                f"Docling conversion failed with status: {result.status}\n{detail_text}"
            )

        raise RuntimeError(f"Docling conversion failed with status: {result.status}")

    # Export with image placeholders.
    # This is preferable for ChatGPT project files because embedded images can massively bloat Markdown.
    markdown = result.document.export_to_markdown(
        image_mode=ImageRefMode.PLACEHOLDER
    )

    markdown, stats = postprocess_markdown_for_llm(
        markdown,
        remove_visual_ocr_blocks=remove_visual_ocr_blocks,
    )
    markdown = build_metadata_header(input_pdf, use_ocr, force_full_page_ocr) + markdown

    output_md.write_text(markdown, encoding="utf-8")

    print(f"Conversion finished in {runtime:.1f} seconds.", flush=True)
    return stats


def convert_pdf_folder_to_markdown(
    input_pdfs: list[Path],
    output_dir: Path,
    use_ocr: bool,
    force_full_page_ocr: bool,
    remove_visual_ocr_blocks: bool,
    overwrite_existing: bool,
) -> dict[str, int]:
    # Build the converter once and reuse it for all PDFs.
    converter = build_converter(
        use_ocr=use_ocr,
        force_full_page_ocr=force_full_page_ocr,
    )

    summary = {
        "pdf_files_found": len(input_pdfs),
        "converted": 0,
        "skipped_existing": 0,
        "failed": 0,
        "low_risk_artifact_fixes": 0,
        "split_key_value_repairs": 0,
        "visual_ocr_blocks_removed": 0,
    }

    print(f"Starting Docling batch conversion for {len(input_pdfs)} PDF file(s)...", flush=True)
    start = time.time()

    for index, input_pdf in enumerate(input_pdfs, start=1):
        output_md = make_output_path(input_pdf, output_dir)

        print(f"[{index}/{len(input_pdfs)}] {input_pdf.name}", flush=True)

        if output_md.exists() and not overwrite_existing:
            summary["skipped_existing"] += 1
            print(f"[SKIP] Output already exists: {output_md}", flush=True)
            continue

        try:
            result = converter.convert(input_pdf, raises_on_error=False)

            if result.status != ConversionStatus.SUCCESS:
                summary["failed"] += 1

                details = []
                for item in getattr(result, "errors", []) or []:
                    details.append(getattr(item, "error_message", str(item)))

                print(f"[FAIL] Docling conversion failed with status: {result.status}", flush=True)
                for msg in details:
                    print(f"  - {msg}", flush=True)

                continue

            markdown = result.document.export_to_markdown(
                image_mode=ImageRefMode.PLACEHOLDER
            )

            markdown, stats = postprocess_markdown_for_llm(
                markdown,
                remove_visual_ocr_blocks=remove_visual_ocr_blocks,
            )
            markdown = build_metadata_header(input_pdf, use_ocr, force_full_page_ocr) + markdown

            output_md.write_text(markdown, encoding="utf-8")

            summary["converted"] += 1
            summary["low_risk_artifact_fixes"] += stats["low_risk_artifact_fixes"]
            summary["split_key_value_repairs"] += stats["split_key_value_repairs"]
            summary["visual_ocr_blocks_removed"] += stats["visual_ocr_blocks_removed"]

            print(f"[OK] {output_md}", flush=True)

        except Exception as e:
            summary["failed"] += 1
            print(f"[FAIL] {input_pdf.name}: {e}", flush=True)

    runtime = time.time() - start
    print(f"Batch conversion finished in {runtime:.1f} seconds.", flush=True)

    return summary


# -----------------------------
# Main program
# -----------------------------

def main() -> int:
    print("Docling PDF folder to Markdown converter for LLM use")
    print("--------------------------------------------------")

    input_folder = ask_path("Enter full path to folder containing PDF files: ")
    validate_input_folder(input_folder)

    input_pdfs = find_pdf_files(input_folder)

    if not input_pdfs:
        raise FileNotFoundError(f"No PDF files found directly inside folder: {input_folder}")

    # Default output folder is inside the input folder.
    output_dir = input_folder / "docling_md"
    output_dir.mkdir(parents=True, exist_ok=True)

    use_ocr = ask_yes_no(
        "Use OCR? Recommended OFF for born-digital PDFs; ON for scanned/image-only PDFs",
        default=False,
    )

    force_full_page_ocr = False
    if use_ocr:
        force_full_page_ocr = ask_yes_no(
            "Force full-page OCR? Use only for bad/missing embedded text layers",
            default=False,
        )

    remove_visual_ocr_blocks = ask_yes_no(
        "Remove likely visual/map OCR text blocks? Safer OFF unless maps/images pollute the Markdown",
        default=False,
    )

    overwrite_existing = ask_yes_no(
        "Overwrite existing Markdown files in the output folder?",
        default=False,
    )

    print()
    print(f"Input folder           : {input_folder}")
    print(f"PDF files found        : {len(input_pdfs)}")
    print(f"Output folder          : {output_dir}")
    print(f"OCR enabled            : {use_ocr}")
    print(f"Force full-page OCR    : {force_full_page_ocr}")
    print(f"Remove visual OCR text : {remove_visual_ocr_blocks}")
    print(f"Overwrite existing MD  : {overwrite_existing}")
    print()

    summary = convert_pdf_folder_to_markdown(
        input_pdfs=input_pdfs,
        output_dir=output_dir,
        use_ocr=use_ocr,
        force_full_page_ocr=force_full_page_ocr,
        remove_visual_ocr_blocks=remove_visual_ocr_blocks,
        overwrite_existing=overwrite_existing,
    )

    print()
    print("--- Summary ---")
    print(f"PDF files found           : {summary['pdf_files_found']}")
    print(f"Converted successfully    : {summary['converted']}")
    print(f"Skipped existing outputs  : {summary['skipped_existing']}")
    print(f"Failed conversions        : {summary['failed']}")
    print(f"Low-risk artifact fixes   : {summary['low_risk_artifact_fixes']}")
    print(f"Split key-value repairs   : {summary['split_key_value_repairs']}")
    print(f"Visual OCR blocks removed : {summary['visual_ocr_blocks_removed']}")
    print(f"Markdown output folder    : {output_dir}")
    print()
    print("Next recommended step:")
    print("Run your Markdown post-processing cleanup script on the generated .md files.")

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())

    except FileExistsError as e:
        print(f"Error: {e}")
        print("Delete or rename the existing output file, then run again.")
        raise SystemExit(1)

    except Exception as e:
        print(f"Error: {e}")
        print()
        print("--- Debug details ---")
        traceback.print_exc()
        raise SystemExit(1)
