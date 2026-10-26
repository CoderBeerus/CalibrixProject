# pdf_extractor.py — CALIBRIX utility
# §MN-5: Use subprocess instead of os.system for safe dependency installation
import sys
import subprocess

import fitz


def extract_pdf(pdf_path, txt_path):
    """Extract text from a PDF and write to a text file."""
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Extracted: {pdf_path} -> {txt_path}")


if __name__ == "__main__":
    extract_pdf("CALIBRIX Upgrade Roadmap.pdf", "roadmap_text.txt")
    extract_pdf(
        r"CALIBRIX_Reports\CALIBRIX_VAL_100C_20260329_194348_Report.pdf",
        "report_text.txt",
    )
