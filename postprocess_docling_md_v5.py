import re
from pathlib import Path
from collections import Counter
from datetime import datetime


# ============================================================
# Docling Markdown post-processor for LLM-oriented cleanup
# Version: 5
# Change from v4: added remove_likely_single_letter_artifacts() — removes
#                 low-risk PyPdfium extraction artifacts like ', l,', ', r,', ', k,'
# ============================================================
#
# Purpose:
# - Clean Markdown generated from PDFs by Docling.
# - Apply only low-risk automatic fixes by default.
# - Flag uncertain corruption instead of guessing.
# - Keep destructive behavior auditable.
#
# Intended use:
# - Run this AFTER Docling PDF -> Markdown conversion.
# - Input:  .md file
# - Output: *_clean.md next to input file
#
# General principle:
# - Auto-fix conservative, pattern-stable artifacts.
# - Flag ambiguous OCR/layout damage for review.
# - Optional destructive cleanup is prompted and off by default.
# ============================================================


# -----------------------------
# Configurable defaults
# -----------------------------

# Common key-value fields often damaged by PDF layout extraction.
# Extend this list for other document families.
FIELD_NAMES = [
    "Title",
    "Author",
    "Date",
    "Version",
    "City Manager",
    "Security Provider",
    "Gangs Present",
]

# Fields whose values are commonly wrapped across multiple lines.
# Keep this list narrow to avoid joining normal paragraphs.
WRAPPED_FIELD_NAMES = [
    "Gangs Present",
]

# Markers that indicate a new structural block.
# Used to stop field-continuation joining.
STRUCTURAL_START_PATTERNS = [
    r"^#{1,6}\s+",              # Markdown heading
    r"^<!--.*?-->\s*$",         # HTML comment / image placeholder
    r"^- ",                     # Markdown bullet
    r"^▶",                      # Decorative section marker
    r"^◀",                      # Decorative section marker
    r"^\(?[A-Z][0-9]+\)",       # Location IDs like A1 or (A1)
    r"^[A-Z][a-zA-Z ]+:\s+",    # Generic key-value line
]


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
    # Guarded yes/no prompt.
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


def validate_input_file(path: Path) -> None:
    # Ensure the input exists and is a Markdown file.
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    if not path.is_file():
        raise ValueError(f"Input path is not a file: {path}")

    if path.suffix.lower() != ".md":
        raise ValueError("Input file must be a .md Markdown file")


def make_output_path(input_file: Path) -> Path:
    # Save next to the input file.
    # Avoid repeated "_clean_clean.md" where possible.
    if input_file.stem.endswith("_clean"):
        return input_file.with_name(input_file.stem + "_v2.md")

    return input_file.with_name(input_file.stem + "_clean.md")


def ensure_output_allowed(output_file: Path) -> None:
    # Avoid silent overwrite unless the user explicitly allows it.
    if output_file.exists():
        overwrite = ask_yes_no(
            f"Output file already exists: {output_file}\nOverwrite existing file?",
            default=False,
        )
        if not overwrite:
            raise FileExistsError(f"Output file already exists: {output_file}")


# -----------------------------
# Basic normalization
# -----------------------------

def normalize_line_endings(text: str) -> str:
    # Normalize Windows/Mac line endings to Unix-style line endings.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_trailing_whitespace(text: str) -> str:
    # Remove trailing spaces without changing content.
    return "\n".join(line.rstrip() for line in text.split("\n"))


def collapse_excessive_blank_lines(text: str) -> str:
    # Collapse extreme vertical whitespace while preserving normal Markdown spacing.
    while "\n\n\n\n" in text:
        text = text.replace("\n\n\n\n", "\n\n\n")
    return text


# -----------------------------
# Conservative text artifact repair
# -----------------------------

def fix_hyphenation(text: str) -> str:
    # Conservative repair for PDF line-break hyphenation.
    # Only joins lowercase word fragments split by "-\n".
    # This avoids damaging many real compounds like "cost-effective".
    text = re.sub(r"([a-zäöü])- *\n([a-zäöü])", r"\1\2", text)

    # Also handle the spaced variant "word -\nfragment".
    text = re.sub(r"([a-zäöü]) +-\n([a-zäöü])", r"\1\2", text)

    return text


def fix_low_risk_layout_artifacts(text: str) -> str:
    # Low-risk known artifacts.
    # Keep this list conservative and explicit.
    replacements = {
        "oneto-one": "one-to-one",
        "one to-one": "one-to-one",
        "8.5x1 1": "8.5x11",
        "8.5 x1 1": "8.5x11",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Normalize common spacing before punctuation created by extraction.
    # Conservative: only for obvious punctuation, not all symbols.
    text = re.sub(r"\s+([,.;!?])", r"\1", text)

    return text


def collapse_double_spaces(text: str) -> str:
    # Collapse multiple consecutive spaces to a single space on each line.
    # Skips code blocks (lines starting with 4 spaces or inside ``` fences)
    # to avoid damaging intentional formatting.
    lines = text.split("\n")
    result = []
    in_code_fence = False

    for line in lines:
        if line.startswith("```"):
            in_code_fence = not in_code_fence  # toggle fence state

        if in_code_fence or line.startswith("    "):
            result.append(line)  # preserve code lines untouched
        else:
            result.append(re.sub(r" {2,}", " ", line))  # collapse double spaces

    return "\n".join(result)


def remove_likely_single_letter_artifacts(text: str) -> str:
    # Very conservative cleanup for observed PyPdfium extraction artifacts.
    # Removes only lowercase single-letter consonants l/r/k when they appear
    # as comma-separated fragments inside prose or lists.
    #
    # Examples:
    # "heavy pistol, l, cyber eye" -> "heavy pistol, cyber eye"
    # "near, r, far" -> "near, far"
    # "interface attack, k, cyber-samurai" -> "interface attack, cyber-samurai"
    #
    # This intentionally does NOT remove uppercase single letters such as A, B, C
    # because those can be valid table labels, grades, options, vitamins, or variables.
    # It also does NOT remove vowels because "a" and "i" may be meaningful words.
    text = re.sub(r",\s+[lrk]\s*,\s+", ", ", text)
    return text


# -----------------------------
# Header / footer / page number cleanup
# -----------------------------

def remove_repeated_page_headers(text: str):
    # Detect repeated Markdown headings that look like page headers.
    # Keep the first occurrence to preserve a real document title.
    lines = text.split("\n")

    header_pattern = re.compile(r"^#{1,3} [A-ZÄÖÜ0-9 .:_\-]{5,}$")
    candidates = [line.strip() for line in lines if header_pattern.match(line.strip())]
    counts = Counter(candidates)

    # Require at least 3 occurrences to reduce false positives.
    repeated = {line for line, count in counts.items() if count >= 3}
    seen = set()

    removed_lines = []
    headers_removed = 0
    filtered = []
    i = 0

    while i < len(lines):
        current = lines[i].strip()

        if current in repeated:
            if current not in seen:
                # Preserve first occurrence.
                seen.add(current)
                filtered.append(lines[i])
                i += 1
                continue

            # Remove later repeated occurrences.
            if filtered and filtered[-1] == "":
                filtered.pop()
                removed_lines.append("[blank line before repeated header]")

            removed_lines.append(lines[i])
            headers_removed += 1
            i += 1

            # Remove blank lines immediately after removed header.
            while i < len(lines) and lines[i].strip() == "":
                removed_lines.append("[blank line after repeated header]")
                i += 1

            continue

        filtered.append(lines[i])
        i += 1

    return "\n".join(filtered), headers_removed, removed_lines


def remove_likely_page_numbers(text: str):
    # Remove standalone page numbers only when they are likely page artifacts.
    # Avoid deleting arbitrary standalone numeric values.
    lines = text.split("\n")

    number_positions = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if re.fullmatch(r"\d{1,3}", stripped):
            number_positions.append((idx, int(stripped)))

    removable_indices = set()
    values = [value for _, value in number_positions]

    # Remove if values form even a partial increasing page sequence.
    if len(number_positions) >= 2:
        sorted_values = sorted(values)
        sequence_like = sum(
            1 for a, b in zip(sorted_values, sorted_values[1:])
            if b == a + 1
        )
        if sequence_like >= 1:
            removable_indices.update(idx for idx, _ in number_positions)

    # Guarded early page-number removal for page 1 near front matter.
    for idx, value in number_positions:
        if value == 1 and idx <= 30:
            nearby = "\n".join(lines[max(0, idx - 6): idx + 7])
            if "<!-- image -->" in nearby or re.search(r"\bV\d|\bJANUARY\b|\bCopyright\b", nearby, re.I):
                removable_indices.add(idx)

    removed_lines = []
    filtered = []

    for idx, line in enumerate(lines):
        if idx in removable_indices:
            removed_lines.append(line)
        else:
            filtered.append(line)

    return "\n".join(filtered), len(removed_lines), removed_lines


# -----------------------------
# Metadata / field structure repair
# -----------------------------

def normalize_metadata_spacing(text: str, field_names=None) -> str:
    # Normalize spacing in known key-value fields:
    # "Security Provider : NCPD" -> "Security Provider: NCPD"
    if field_names is None:
        field_names = FIELD_NAMES

    for field in field_names:
        escaped = re.escape(field)
        text = re.sub(
            rf"^{escaped}\s*:\s*",
            f"{field}: ",
            text,
            flags=re.MULTILINE,
        )

    return text


def repair_split_field_colon_lines(text: str, field_names=None) -> str:
    # Fix:
    # Security Provider
    #
    # : NCPD
    #
    # into:
    # Security Provider: NCPD
    if field_names is None:
        field_names = FIELD_NAMES

    for field in field_names:
        escaped = re.escape(field)
        text = re.sub(
            rf"^{escaped}\s*\n+\s*:\s*",
            f"{field}: ",
            text,
            flags=re.MULTILINE,
        )

    return text


def repair_known_field_collisions(text: str, field_names=None) -> str:
    # Fix cases where two known metadata fields were merged onto one line:
    # "Security Provider: X Gangs Present: Y"
    # ->
    # "Security Provider: X"
    # "Gangs Present: Y"
    if field_names is None:
        field_names = FIELD_NAMES

    # First normalize all known field labels wherever they appear.
    for field in field_names:
        escaped = re.escape(field)
        text = re.sub(
            rf"\b{escaped}\s*:\s*",
            f"{field}: ",
            text,
        )

    # Insert a line break before any known field label that appears after content.
    # This is intentionally limited to known field labels.
    field_group = "|".join(re.escape(field) for field in field_names)

    text = re.sub(
        rf"([^\n])\s+({field_group}):\s+",
        r"\1\n\2: ",
        text,
    )

    return text


def is_structural_start(line: str, field_names=None) -> bool:
    # Decide whether a line starts a new structural block.
    stripped = line.strip()

    if stripped == "":
        return False

    if field_names is None:
        field_names = FIELD_NAMES

    for field in field_names:
        if stripped.startswith(f"{field}:"):
            return True

    for pattern in STRUCTURAL_START_PATTERNS:
        if re.match(pattern, stripped):
            return True

    return False


def repair_wrapped_fields(text: str, wrapped_field_names=None, field_names=None) -> str:
    # Join wrapped known field values.
    #
    # Example:
    # Gangs Present: DeadWoods, Kill Krashers,
    #
    # The Reckoners, Weng Fang Tong
    #
    # becomes:
    # Gangs Present: DeadWoods, Kill Krashers, The Reckoners, Weng Fang Tong
    #
    # This is intentionally limited to configured wrapped field names.
    if wrapped_field_names is None:
        wrapped_field_names = WRAPPED_FIELD_NAMES

    if field_names is None:
        field_names = FIELD_NAMES

    lines = text.split("\n")
    result = []
    i = 0

    wrapped_starts = tuple(f"{name}:" for name in wrapped_field_names)

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith(wrapped_starts):
            merged = stripped
            i += 1

            while i < len(lines):
                next_line = lines[i].strip()

                # Skip blank lines inside a wrapped field.
                if next_line == "":
                    i += 1
                    continue

                # Stop on structural boundaries.
                if is_structural_start(next_line, field_names=field_names):
                    break

                # Stop if the next line looks like the start of a normal sentence paragraph.
                # Wrapped lists usually continue with comma-separated names or short noun phrases.
                if re.match(r"^[A-Z][a-z]+ .*?[.!?]$", next_line):
                    break

                merged += " " + next_line
                i += 1

            result.append(merged)
            continue

        result.append(line)
        i += 1

    return "\n".join(result)


# -----------------------------
# Optional visual/map OCR text removal
# -----------------------------

def remove_likely_visual_ocr_blocks(text: str):
    # Optional cleanup for OCR text extracted from maps, diagrams, or full-page images.
    #
    # This is deliberately conservative and prompted separately.
    # It removes only blocks after image placeholders that look like label soup:
    # many short uppercase lines, little sentence structure, no normal Markdown structure.
    lines = text.split("\n")
    result = []
    removed_blocks = 0
    removed_previews = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.strip() == "<!-- image -->":
            result.append(line)
            i += 1

            block = []

            while i < len(lines):
                stripped = lines[i].strip()

                # Stop at likely real content.
                if (
                    stripped.startswith("#")
                    or stripped.startswith("- ")
                    or stripped.startswith("<!-- image -->")
                    or re.match(r"^\(?[A-Z][0-9]+\)", stripped)
                    or any(stripped.startswith(f"{field}:") for field in FIELD_NAMES)
                ):
                    break

                block.append(lines[i])
                i += 1

            non_empty = [b.strip() for b in block if b.strip()]
            uppercase_count = sum(
                1 for b in non_empty
                if b.isupper() and len(b) >= 3
            )
            sentence_like_count = sum(
                1 for b in non_empty
                if re.search(r"[.!?]$", b)
            )

            # Label-soup heuristic.
            looks_like_visual_ocr = (
                len(non_empty) >= 8
                and uppercase_count >= 5
                and sentence_like_count <= 1
            )

            if looks_like_visual_ocr:
                removed_blocks += 1
                preview = " | ".join(non_empty[:8])
                removed_previews.append(preview)
                result.append("<!-- likely visual/map OCR text removed -->")
            else:
                result.extend(block)

            continue

        result.append(line)
        i += 1

    return "\n".join(result), removed_blocks, removed_previews


# -----------------------------
# Corruption detection / flagging
# -----------------------------

def has_spaced_letters(line: str) -> bool:
    # Strip existing HTML comments before checking.
    clean = re.sub(r"<!--.*?-->", "", line)

    # Strip Markdown heading markers.
    clean = re.sub(r"^#{1,6}\s*", "", clean)

    # Detect OCR/layout corruption like:
    # "w h a t i s t h i s"
    return bool(
        re.search(
            r"(?<!\w)([A-Za-zÄÖÜäöüß] ){3,}[A-Za-zÄÖÜäöüß](?!\w)",
            clean,
        )
    )


def flag_corrupted_lines(text: str):
    # Flag suspicious lines instead of attempting unsafe automatic repair.
    lines = text.split("\n")
    flagged = 0
    result = []

    for line in lines:
        if "[CORRUPTED?]" in line:
            result.append(line)
        elif has_spaced_letters(line):
            result.append(line + "  <!-- [CORRUPTED?] -->")
            flagged += 1
        else:
            result.append(line)

    return "\n".join(result), flagged


# -----------------------------
# Reporting
# -----------------------------

def print_removed_lines_report(title: str, removed_lines) -> None:
    # Audit report for destructive cleanup steps.
    print(f"\n--- Removed lines: {title} ---", flush=True)

    if not removed_lines:
        print("(none)", flush=True)
        return

    for line in removed_lines:
        print(f"- {line}", flush=True)


def print_removed_blocks_report(title: str, removed_previews) -> None:
    # Compact report for removed visual/OCR blocks.
    print(f"\n--- Removed blocks: {title} ---", flush=True)

    if not removed_previews:
        print("(none)", flush=True)
        return

    for idx, preview in enumerate(removed_previews, start=1):
        print(f"{idx}. {preview}", flush=True)


# -----------------------------
# Main cleanup pipeline
# -----------------------------

def clean_markdown(text: str, remove_visual_ocr: bool):
    stats = {
        "headers_removed": 0,
        "page_numbers_removed": 0,
        "visual_ocr_blocks_removed": 0,
        "corrupted_lines_flagged": 0,
        "removed_headers": [],
        "removed_page_numbers": [],
        "removed_visual_previews": [],
    }

    # 1. Normalize line endings.
    text = normalize_line_endings(text)

    # 2. Basic whitespace normalization.
    text = strip_trailing_whitespace(text)

    # 3. Conservative hyphenation repair.
    text = fix_hyphenation(text)

    # 4. Remove repeated page headers, keeping first occurrence.
    text, count, removed = remove_repeated_page_headers(text)
    stats["headers_removed"] = count
    stats["removed_headers"] = removed

    # 5. Remove likely page numbers.
    text, count, removed = remove_likely_page_numbers(text)
    stats["page_numbers_removed"] = count
    stats["removed_page_numbers"] = removed

    # 6. Low-risk layout artifact fixes.
    text = fix_low_risk_layout_artifacts(text)

    # 6b. Remove low-risk single-letter comma artifacts from PyPdfium extraction.
    text = remove_likely_single_letter_artifacts(text)

    # 6c. Collapse double spaces from PDF extraction (skips code blocks).
    text = collapse_double_spaces(text)

    # 7. Metadata/key-value spacing repair.
    text = normalize_metadata_spacing(text)

    # 8. Split field labels whose colon/value got pushed to the next line.
    text = repair_split_field_colon_lines(text)

    # 9. Split known field collisions.
    text = repair_known_field_collisions(text)

    # 10. Repair wrapped configured fields.
    text = repair_wrapped_fields(text)

    # 11. Optional visual/map OCR block removal.
    if remove_visual_ocr:
        text, count, previews = remove_likely_visual_ocr_blocks(text)
        stats["visual_ocr_blocks_removed"] = count
        stats["removed_visual_previews"] = previews

    # 12. Flag suspicious corruption after cleanup.
    text, flagged = flag_corrupted_lines(text)
    stats["corrupted_lines_flagged"] = flagged

    # 13. Final whitespace normalization.
    text = collapse_excessive_blank_lines(text)
    text = text.strip() + "\n"

    return text, stats


def main() -> int:
    print("Docling Markdown post-processor for LLM use")
    print("------------------------------------------")

    input_file = ask_path("Enter full path to Markdown file: ")
    validate_input_file(input_file)

    output_file = make_output_path(input_file)
    ensure_output_allowed(output_file)

    remove_visual_ocr = ask_yes_no(
        "Remove likely visual/map OCR text blocks? Use only if map/diagram label text pollutes the Markdown",
        default=False,
    )

    print()
    print(f"Input MD               : {input_file}")
    print(f"Output MD              : {output_file}")
    print(f"Remove visual OCR text : {remove_visual_ocr}")
    print()

    print("Reading file...", flush=True)
    content = input_file.read_text(encoding="utf-8-sig")

    print("Cleaning Markdown...", flush=True)
    cleaned, stats = clean_markdown(content, remove_visual_ocr=remove_visual_ocr)

    print_removed_lines_report("repeated page headers", stats["removed_headers"])
    print_removed_lines_report("likely page numbers", stats["removed_page_numbers"])
    print_removed_blocks_report("likely visual/map OCR blocks", stats["removed_visual_previews"])

    output_file.write_text(cleaned, encoding="utf-8")

    print("\n--- Summary ---", flush=True)
    print(f"Repeated page headers removed : {stats['headers_removed']}", flush=True)
    print(f"Likely page numbers removed   : {stats['page_numbers_removed']}", flush=True)
    print(f"Visual OCR blocks removed     : {stats['visual_ocr_blocks_removed']}", flush=True)
    print(f"Corrupted lines flagged       : {stats['corrupted_lines_flagged']}", flush=True)
    print(f"Output saved to               : {output_file}", flush=True)

    print()
    print("Review any '<!-- [CORRUPTED?] -->' markers manually before using the file as a high-reliability project source.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())

    except FileNotFoundError as e:
        print(f"Error: {e}")
        raise SystemExit(1)

    except FileExistsError as e:
        print(f"Error: {e}")
        raise SystemExit(1)

    except ValueError as e:
        print(f"Error: {e}")
        raise SystemExit(1)

    except UnicodeDecodeError:
        print("Error: Could not read file as UTF-8 text.")
        raise SystemExit(1)

    except Exception as e:
        print(f"Error: Something went wrong — {e}")
        print()
        print("--- Debug details ---")
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
