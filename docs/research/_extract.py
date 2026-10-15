from pypdf import PdfReader

PDF = r"E:\2026ob_projects\GeoMind\高德开放平台整体介绍+AI+空间智能应用 202608.pdf"
OUT = r"E:\2026ob_projects\GeoMind\research\_pdf_text.txt"

reader = PdfReader(PDF)
n = len(reader.pages)
print("PAGES:", n)

lines = []
per_page_stats = []
for i, page in enumerate(reader.pages):
    try:
        t = page.extract_text() or ""
    except Exception as e:
        t = f"[EXTRACT ERROR: {e}]"
    t = t.strip()
    per_page_stats.append((i + 1, len(t)))
    lines.append(f"\n===== PAGE {i+1} =====\n{t}\n")

with open(OUT, "w", encoding="utf-8") as f:
    f.write(f"TOTAL_PAGES={n}\n")
    f.write("".join(lines))

# stats
short = [(p, n) for p, n in per_page_stats if n < 80]
print("PAGES_WITH_LITTLE_TEXT (<80 chars):", short)
print("WROTE:", OUT)
