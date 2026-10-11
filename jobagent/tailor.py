"""Per-job resume tailoring. Deterministic and conservative:
 - never rewrites or invents bullets
 - adds only skills from the user's confirmed bank that the posting actually mentions
 - skills marked "used" go on the Languages / Tools lines, "course" skills go on a Coursework line
 - reorders the two project blocks by relevance to the posting"""
import re, shutil
from pathlib import Path
from copy import deepcopy
from docx import Document
from .skills_catalog import CATALOG

ALIAS = {"JavaScript": ["js", "javascript", "node"], "TypeScript": ["ts", "typescript"], "Go": ["golang"], "Kubernetes": ["k8s"],
         "PostgreSQL": ["postgres", "postgresql"], "C++": ["cpp", "c\\+\\+"], "C#": ["c#", "csharp"], "CI/CD": ["ci/cd", "cicd", "continuous integration"],
         "REST APIs": ["rest", "restful"], "LLMs": ["llm", "llms", "large language model"], "Machine Learning": ["machine learning", "ml"],
         "Deep Learning": ["deep learning"], "NLP": ["nlp", "natural language"], "RAG": ["rag", "retrieval[- ]augmented"], "Computer Vision": ["computer vision"],
         "Data Structures": ["data structures"], "Algorithms": ["algorithms"], "Operating Systems": ["operating systems"], "Distributed Systems": ["distributed systems"],
         "AWS EC2": ["ec2"], "AWS S3": ["s3"], "AWS Lambda": ["lambda"], "Terraform": ["terraform"], "OOP": ["object[- ]oriented", "oop"],
         "Unit Testing": ["unit test(ing|s)?"], "System Design": ["system design"], "Microservices": ["microservices?"], "GraphQL": ["graphql"]}
AMBIGUOUS = {"Go", "R", "C", "Make", "Cursor", "Ray", "Vault", "Gin", "Hashing", "Debugging", "Monitoring", "Notion", "Optimization", "Statistics"}
LANG = set(CATALOG["Languages"].split("|"))
CAT_OF = {s: c for c, v in CATALOG.items() for s in v.split("|")}


def _pat(skill):
    alts = [re.escape(skill.lower())] + [a if "\\" in a or "[" in a else re.escape(a) for a in ALIAS.get(skill, [])]
    return re.compile(r"(?<![\w+#])(" + "|".join(alts) + r")(?![\w#]|\+\+)", re.I)


def matched_skills(text: str, bank: dict) -> list[tuple[str, str]]:
    out = []
    for skill, level in bank.items():
        if skill in AMBIGUOUS and skill not in ALIAS:
            continue
        if skill == "Go":   # only when written as a language: "golang", or Go sitting in a list of other technologies
            T = r"(?:Python|Java|Rust|C\+\+|Kubernetes|Docker|TypeScript|Kotlin|Scala|Node)"
            if re.search(r"\bgolang\b", text, re.I) or re.search(rf"\b{T}\b[,/ ]+(?:and |or )?Go\b|\bGo\b[,/ ]+(?:and |or )?{T}\b", text):
                out.append((skill, level))
            continue
        if _pat(skill).search(text):
            out.append((skill, level))
    return out


def _has(line: str, skill: str) -> bool:
    return re.search(r"(?<![\w+#])" + re.escape(skill) + r"(?![\w#]|\+\+)", line, re.I) is not None


CONCEPTS = {"Machine Learning", "Deep Learning", "NLP", "LLMs", "Generative AI", "RAG", "Vector Databases", "Embeddings", "Semantic Search", "Prompt Engineering",
            "AI Agents", "Tool Calling", "MCP", "LLM Evaluation", "Fine-Tuning", "LoRA", "Quantization", "Model Deployment", "Feature Engineering",
            "Supervised Learning", "Unsupervised Learning", "Reinforcement Learning", "Neural Networks", "Transformers", "Computer Vision",
            "Recommendation Systems", "MLOps", "Microservices", "REST APIs", "API Design", "Authentication", "Infrastructure as Code", "Cloud Architecture",
            "Serverless", "Data Pipelines", "ETL", "Data Warehousing", "Data Modeling", "Monitoring", "Containerization", "Virtualization", "Load Balancing"}


def bucket_of(skill):
    """Which existing resume line a skill belongs on: languages / frameworks & tools / interests (concepts)."""
    if skill in LANG:
        return "lang"
    if CAT_OF.get(skill) in ("CS fundamentals", "Quant / math", "Security") or skill in CONCEPTS:
        return "interest"
    return "tool"


def _append_interests(par, items):
    if items:
        par.runs[-1].text = par.runs[-1].text.rstrip() + ", " + ", ".join(items)


def _para_idx(doc, pred, start=0):
    for i in range(start, len(doc.paragraphs)):
        if pred(doc.paragraphs[i].text):
            return i
    return None


def _append_to_list(par, items, before_bold=None):
    """Append ', a, b' to the list in a skills paragraph, ahead of a following bold label such as 'Interests'."""
    if not items:
        return
    runs = par.runs
    target = runs[-1]
    if before_bold:
        for i, r in enumerate(runs):
            if r.bold and r.text.strip().startswith(before_bold):
                j = i - 1
                while j > 0 and not runs[j].text.strip():
                    j -= 1
                target = runs[j]
                break
    target.text = target.text.rstrip() + ", " + ", ".join(items)


def _project_blocks(doc):
    ps = doc.paragraphs
    a = _para_idx(doc, lambda t: t.strip().upper() == "PROJECTS")
    b = _para_idx(doc, lambda t: t.strip().upper() == "EXPERIENCE")
    if a is None or b is None:
        return []
    blocks, cur = [], None
    for i in range(a + 1, b):
        p = ps[i]
        if p.runs and p.runs[0].bold and p.text.strip():
            cur = [p]; blocks.append(cur)
        elif cur is not None and p.text.strip():
            cur.append(p)
    return blocks


def _compact(doc, gap_pt):
    """Shrink the empty spacer paragraphs between sections to buy room; content is never touched."""
    from docx.shared import Pt
    for p in doc.paragraphs:
        if not p.text.strip():
            p.paragraph_format.space_after = Pt(0); p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.line_spacing = Pt(gap_pt)
            for r in p.runs:
                r.font.size = Pt(max(2, gap_pt - 1))


def tailor(src_docx, job_text, bank, out_path, max_add=8, max_course=12, gap_pt=None):
    """Returns dict(info). Writes out_path."""
    doc = Document(str(src_docx))
    ms = matched_skills(job_text, bank)
    lang_line = _para_idx(doc, lambda t: t.startswith("Programming Languages"))
    tool_line = _para_idx(doc, lambda t: t.startswith("Frameworks & Tools"))
    added = {"lang": [], "tool": [], "interest": []}
    if lang_line is not None and tool_line is not None:
        text_all = doc.paragraphs[lang_line].text + " " + doc.paragraphs[tool_line].text
        for skill, level in ms:
            if _has(text_all, skill):
                continue
            added[bucket_of(skill)].append(skill)
        caps = {"lang": max_add // 2, "tool": max_add, "interest": max_course // 2}
        for k in added:
            added[k] = added[k][:caps[k]]
        _append_to_list(doc.paragraphs[lang_line], added["lang"])
        _append_to_list(doc.paragraphs[tool_line], added["tool"], before_bold="Interests")
        _append_interests(doc.paragraphs[tool_line], added["interest"])
    added_used = added["lang"] + added["tool"]
    added_course = added["interest"]
    # project order by relevance (count of matched skill names inside each block)
    blocks = _project_blocks(doc)
    reordered = False
    if len(blocks) == 2:
        score = lambda bl: sum(1 for s, _ in ms if _has(" ".join(p.text for p in bl), s))
        if score(blocks[1]) > score(blocks[0]):
            first = blocks[0][0]._p
            for p in blocks[1]:
                first.addprevious(p._p)
            reordered = True
    if gap_pt is not None:
        _compact(doc, gap_pt)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return {"matched": [s for s, _ in ms], "added_used": added_used, "added_course": added_course, "reordered": reordered}


SOFFICE = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"


def to_pdf(docx_path) -> Path | None:
    import subprocess, tempfile
    docx_path = Path(docx_path)
    if not Path(SOFFICE).exists():
        return None
    with tempfile.TemporaryDirectory() as prof:   # private profile dir so parallel/open LibreOffice instances don't clash
        r = subprocess.run([SOFFICE, f"-env:UserInstallation=file://{prof}", "--headless", "--convert-to", "pdf", "--outdir", str(docx_path.parent), str(docx_path)],
                           capture_output=True, timeout=120)
    pdf = docx_path.with_suffix(".pdf")
    return pdf if pdf.exists() else None


def page_count(pdf_path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf_path)).pages)


def tailor_fit(src_docx, job_text, bank, out_docx, pages_fn=None):
    """Tailor, then shrink the additions until the resume still fits the original page count.
    Returns (final_path, info). final_path is a PDF when LibreOffice is available, else the .docx."""
    out_docx = Path(out_docx)
    base_pages = None
    attempts = [(8, 12, None), (8, 12, 6), (6, 8, 4), (4, 5, 3), (2, 2, 3), (0, 0, None)]
    for max_add, max_course, gap in attempts:
        info = tailor(src_docx, job_text, bank, out_docx, max_add=max_add, max_course=max_course, gap_pt=gap)
        pdf = to_pdf(out_docx) if pages_fn is None else None
        n = pages_fn(out_docx, max_add, max_course, gap) if pages_fn else (page_count(pdf) if pdf else None)
        if n is None:                      # cannot measure: stay with a conservative size
            if (max_add, max_course) == (4, 5):
                return out_docx, {**info, "pages": None}
            continue
        if base_pages is None:
            base = Path(out_docx).with_name("_base.docx"); shutil.copy(src_docx, base)
            bpdf = to_pdf(base); base_pages = page_count(bpdf) if bpdf else 1
            for f in (base, base.with_suffix(".pdf")): f.unlink(missing_ok=True)
        if n <= base_pages:
            return (pdf or out_docx), {**info, "pages": n}
    # nothing fit: ship the untouched original
    shutil.copy(src_docx, out_docx)
    pdf = to_pdf(out_docx)
    return (pdf or out_docx), {"matched": [], "added_used": [], "added_course": [], "reordered": False, "pages": base_pages}
