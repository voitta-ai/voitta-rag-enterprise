"""Compare PDF parsing between two installs of the app (e.g. MinerU 3 vs 4).

Step 1 runs in each install's own virtualenv and records what its
``PdfParser`` produces for a list of PDFs:

    <venv-a>/bin/python scripts/pdf_parse_compare.py run --out runs/a --list pdfs.txt
    <venv-b>/bin/python scripts/pdf_parse_compare.py run --out runs/b --list pdfs.txt

Step 2 compares the two runs and prints a gate report (needs PyMuPDF and
the PDFs at their recorded paths for the text-layer recall):

    python scripts/pdf_parse_compare.py compare runs/a runs/b [--json report.json]

``run`` writes ``<out>/results.jsonl`` (one record per PDF: status, error,
parse time, page count, markdown length, image count and pages, char→page
anchor count, layout block counts by type, peak daemon RSS and GPU memory)
and ``<out>/md/<n>.md``. It resumes: PDFs already in results.jsonl are
skipped. Page renders are switched off — they are PyMuPDF, not MinerU, and
would only add noise to the timings.

Two content measures are reported:

* similarity between the two runs' markup-free word sequences — sensitive
  to any change, but cannot tell an improvement from a regression;
* recall of the PDF's own text layer (PyMuPDF), per run — an outside
  reference, so a drop is a loss. Scanned PDFs have no text layer and are
  judged by similarity and by eye only.

The gate thresholds are the ones in docs/plans/mineru4-conversion.md.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from itertools import pairwise
from pathlib import Path


def _md_name(pdf: str) -> str:
    return hashlib.sha1(pdf.encode()).hexdigest()[:16] + ".md"


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


class _DaemonSampler:
    """Samples the MinerU daemon's RSS (with children) and GPU memory."""

    def __init__(self) -> None:
        self.peak_rss_mb = 0.0
        self.peak_gpu_mb = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> _DaemonSampler:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _loop(self) -> None:
        import psutil

        from voitta_rag_enterprise.services.parsers import pdf_parser

        while not self._stop.wait(0.25):
            daemon = pdf_parser._DAEMON
            proc = daemon._proc if daemon is not None else None
            if proc is None or proc.poll() is not None:
                continue
            try:
                p = psutil.Process(proc.pid)
                procs = [p, *p.children(recursive=True)]
                rss = sum(q.memory_info().rss for q in procs) / 2**20
                pids = {q.pid for q in procs}
            except psutil.Error:
                continue
            self.peak_rss_mb = max(self.peak_rss_mb, rss)
            self.peak_gpu_mb = max(self.peak_gpu_mb, _gpu_mb(pids))


def _gpu_mb(pids: set[int]) -> float:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    total = 0.0
    for line in out.splitlines():
        pid, _, mem = line.partition(",")
        if pid.strip().isdigit() and int(pid) in pids:
            total += float(mem)
    return total


def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    (out / "md").mkdir(parents=True, exist_ok=True)
    data_dir = tempfile.mkdtemp(prefix="pdfcmp-data-")
    os.environ.update(
        VOITTA_DATA_DIR=data_dir,
        VOITTA_PDF_RENDER_PAGES="false",
        VOITTA_USE_FAKE_PDF_PARSER="false",
    )
    from voitta_rag_enterprise.services.parsers.base import UnsupportedDocumentError
    from voitta_rag_enterprise.services.parsers.pdf_parser import PdfParser

    pdfs = [line.strip() for line in Path(args.list).read_text().splitlines() if line.strip()]
    results_path = out / "results.jsonl"
    done = set()
    if results_path.exists():
        done = {json.loads(line)["pdf"] for line in results_path.read_text().splitlines() if line}
    parser = PdfParser()
    with results_path.open("a") as fh:
        for n, pdf in enumerate(pdfs, 1):
            if pdf in done:
                continue
            rec: dict = {"pdf": pdf}
            started = time.perf_counter()
            with _DaemonSampler() as sampler:
                try:
                    result = parser.parse(Path(pdf))
                except UnsupportedDocumentError as e:
                    rec.update(status="unsupported", error=str(e))
                except Exception as e:
                    rec.update(status="error", error=f"{type(e).__name__}: {e}"[:2000])
                else:
                    if result.error:
                        rec.update(status="error", error=result.error)
                    else:
                        (out / "md" / _md_name(pdf)).write_text(result.content, encoding="utf-8")
                        rec.update(
                            status="ok",
                            pages=result.metadata.get("page_count"),
                            buckets=result.metadata.get("buckets"),
                            parse_s=round(result.metadata.get("parse_time_seconds", 0.0), 2),
                            md_chars=len(result.content),
                            images=len(result.images),
                            image_pages=[i.page for i in result.images],
                            anchors=len(result.char_to_page),
                            anchor_pages=len({p for _, p in result.char_to_page}),
                            layout=dict(Counter(b.get("type") for b in result.page_layout)),
                            titles=sum(
                                1 for b in result.page_layout
                                if b.get("type") == "title" or isinstance(b.get("text_level"), int)
                            ),
                        )
            rec["wall_s"] = round(time.perf_counter() - started, 2)
            rec["peak_rss_mb"] = round(sampler.peak_rss_mb)
            rec["peak_gpu_mb"] = round(sampler.peak_gpu_mb)
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print(f"[{n}/{len(pdfs)}] {rec['status']:<11} {rec.get('parse_s', '-')}s  {Path(pdf).name}", flush=True)
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def _load(run_dir: Path) -> dict[str, dict]:
    recs = {}
    for line in (run_dir / "results.jsonl").read_text().splitlines():
        if line:
            rec = json.loads(line)
            recs[rec["pdf"]] = rec
    return recs


# difflib is quadratic in the worst case; past this many words compare
# whitespace-normalised lines instead (books, 500-page manuals).
_WORD_LEVEL_MAX = 40_000

# Real tags only (``<td rowspan=2>``, ``</table>``): a bare "<" in prose
# ("x < y … >") must not swallow the text up to the next ">".
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>\n]*)?/?>")
_IMG_REF_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# Markdown emphasis and backslash escapes: 4.x bolds labels and best table
# values ("**From:**", "**.7869**"); both versions escape "~ $ _" differently.
_EMPHASIS_RE = re.compile(r"\*\*|__|(?<!\w)[*_]|[*_](?!\w)")
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~$|<>])")
_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", re.MULTILINE)


def _normalise(md: str) -> list[str]:
    """Words of the document with markup removed.

    Both MinerU versions emit the same content in different markup: 3.x
    writes tables as HTML, 4.x as Markdown pipe tables (HTML only for merged
    cells), and image refs are content hashes in 3.x but page-based names in
    4.x. Comparing raw markdown would score those as content changes, so
    tags, entities, table rules, pipes, emphasis and backslash escapes are
    stripped and every image ref becomes one ``<img>`` token.

    LaTeX is left as is: 3.x spells it with every symbol space-separated,
    4.x compactly, so formula-heavy papers score low here even when nothing
    is lost — which is what the text-layer recall below is for.
    """
    md = _IMG_REF_RE.sub(" <img> ", md)
    md = _TABLE_RULE_RE.sub(" ", md)
    md = _HTML_TAG_RE.sub(" ", md.replace("<img>", "\0IMG\0"))
    md = html.unescape(md).replace("\0IMG\0", " <img> ")
    md = md.replace("|", " ")
    md = _ESCAPE_RE.sub(r"\1", _EMPHASIS_RE.sub("", md))
    return md.split()


def _similarity(a: str, b: str) -> float:
    """Similarity of the markup-free word sequences (difflib ratio).

    Very long documents (books) would make the word-level match quadratic;
    for those, the score is the overlap of word-bigram multisets
    (2·|A∩B| / (|A|+|B|)), which is order-insensitive beyond two words but
    still drops when text is lost, duplicated or garbled.
    """
    wa, wb = _normalise(a), _normalise(b)
    if not wa and not wb:
        return 1.0
    if max(len(wa), len(wb)) > _WORD_LEVEL_MAX:
        ba, bb = Counter(pairwise(wa)), Counter(pairwise(wb))
        total = sum(ba.values()) + sum(bb.values())
        return 2 * sum((ba & bb).values()) / total if total else 1.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


# A page with fewer words than this in its text layer counts as scanned
# (or image-only); text-layer recall is not computed for such documents.
_TEXT_LAYER_MIN_WORDS_PER_PAGE = 25


def _text_layer_words(pdf: str) -> Counter | None:
    """Words of the PDF's own text layer, or None for scanned documents."""
    import fitz

    with fitz.open(pdf) as doc:
        words = Counter(" ".join(page.get_text() for page in doc).split())
        pages = len(doc)
    if sum(words.values()) < _TEXT_LAYER_MIN_WORDS_PER_PAGE * max(1, pages):
        return None
    return words


def _recall(md: str, truth: Counter) -> float:
    """Share of the text layer's words (as a multiset) present in ``md``."""
    return sum((Counter(_normalise(md)) & truth).values()) / sum(truth.values())


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


def cmd_compare(args: argparse.Namespace) -> int:
    a_dir, b_dir = Path(args.a), Path(args.b)
    a, b = _load(a_dir), _load(b_dir)
    common = sorted(set(a) & set(b))
    rows = []
    for pdf in common:
        ra, rb = a[pdf], b[pdf]
        row = {"pdf": pdf, "a": ra["status"], "b": rb["status"]}
        if ra["status"] == rb["status"] == "ok":
            ma = (a_dir / "md" / _md_name(pdf)).read_text(encoding="utf-8")
            mb = (b_dir / "md" / _md_name(pdf)).read_text(encoding="utf-8")
            truth = _text_layer_words(pdf) if Path(pdf).exists() else None
            if truth is not None:
                row["recall"] = (round(_recall(ma, truth), 4), round(_recall(mb, truth), 4))
            row.update(
                sim=round(_similarity(ma, mb), 4),
                chars=(ra["md_chars"], rb["md_chars"]),
                images=(ra["images"], rb["images"]),
                anchor_pages=(ra["anchor_pages"], rb["anchor_pages"]),
                pages=ra["pages"],
                parse_s=(ra["parse_s"], rb["parse_s"]),
                titles=(ra.get("titles", 0), rb.get("titles", 0)),
                layout=(ra["layout"], rb["layout"]),
                gpu=(ra["peak_gpu_mb"], rb["peak_gpu_mb"]),
                rss=(ra["peak_rss_mb"], rb["peak_rss_mb"]),
            )
        rows.append(row)

    both = [r for r in rows if "sim" in r]
    sims = [r["sim"] for r in both]
    low = sorted((r for r in both if r["sim"] < 0.85), key=lambda r: r["sim"])
    img_a = sum(r["images"][0] for r in both)
    img_b = sum(r["images"][1] for r in both)
    t_a = [r["parse_s"][0] for r in both]
    t_b = [r["parse_s"][1] for r in both]
    new_failures = [r for r in rows if r["a"] == "ok" and r["b"] != "ok"]
    fixed = [r for r in rows if r["a"] != "ok" and r["b"] == "ok"]
    statuses = Counter((r["a"], r["b"]) for r in rows)
    layout_a: Counter = Counter()
    layout_b: Counter = Counter()
    for r in both:
        layout_a.update(r["layout"][0])
        layout_b.update(r["layout"][1])

    with_layer = [r for r in both if "recall" in r]
    recall_a = statistics.mean(r["recall"][0] for r in with_layer) if with_layer else float("nan")
    recall_b = statistics.mean(r["recall"][1] for r in with_layer) if with_layer else float("nan")
    recall_drops = sorted(
        (r for r in with_layer if r["recall"][1] < r["recall"][0] - 0.05),
        key=lambda r: r["recall"][1] - r["recall"][0],
    )

    median_sim = statistics.median(sims) if sims else float("nan")
    low_share = len(low) / len(both) if both else float("nan")
    img_delta = (img_b - img_a) / img_a if img_a else float("nan")
    time_ratio = statistics.median(t_b) / statistics.median(t_a) if t_a and statistics.median(t_a) else float("nan")
    gates = {
        "median similarity >= 0.95": median_sim >= 0.95,
        "<= 5% of files below 0.85": low_share <= 0.05,
        "text-layer recall: b >= a on average": recall_b >= recall_a,
        "text-layer recall: no file drops > 5 points": not recall_drops,
        "images within +-10%": abs(img_delta) <= 0.10,
        "no new failures": not new_failures,
        "median parse time <= 1.5x": time_ratio <= 1.5,
    }

    print(f"PDFs compared: {len(common)}   both ok: {len(both)}")
    print(f"status pairs (a, b): {dict(statuses)}")
    print(f"text similarity: median {median_sim:.4f}  p10 {_pct(sims, 0.10):.4f}  min {min(sims, default=float('nan')):.4f}")
    print(f"files below 0.85: {len(low)} ({low_share:.1%})")
    print(f"text-layer word recall ({len(with_layer)} PDFs with a text layer): mean a {recall_a:.4f}  b {recall_b:.4f}")
    print(f"  files where b gains > 5 points: {sum(1 for r in with_layer if r['recall'][1] > r['recall'][0] + 0.05)}"
          f"   loses > 5 points: {len(recall_drops)}")
    for r in recall_drops:
        print(f"    {r['recall'][0]:.3f} → {r['recall'][1]:.3f}  {Path(r['pdf']).name[:80]}")
    print(f"images: a {img_a}  b {img_b}  delta {img_delta:+.1%}")
    print(f"parse time: median a {statistics.median(t_a):.2f}s  b {statistics.median(t_b):.2f}s  ratio {time_ratio:.2f}"
          f"   total a {sum(t_a):.0f}s  b {sum(t_b):.0f}s")
    print(f"peak GPU MB: a max {max((r['gpu'][0] for r in both), default=0)}  b max {max((r['gpu'][1] for r in both), default=0)}")
    print(f"peak RSS MB: a max {max((r['rss'][0] for r in both), default=0)}  b max {max((r['rss'][1] for r in both), default=0)}")
    print(f"titles: a {sum(r['titles'][0] for r in both)}  b {sum(r['titles'][1] for r in both)}")
    print("layout blocks by type (a → b):")
    for t in sorted(set(layout_a) | set(layout_b), key=str):
        print(f"  {t!s:<16} {layout_a[t]:>7} → {layout_b[t]:>7}")
    print("anchor pages / pages: a {:.3f}  b {:.3f}".format(
        sum(r["anchor_pages"][0] for r in both) / max(1, sum(r["pages"] for r in both)),
        sum(r["anchor_pages"][1] for r in both) / max(1, sum(r["pages"] for r in both)),
    ))
    if new_failures:
        print("NEW FAILURES:")
        for r in new_failures:
            print(f"  {r['pdf']}\n    {b[r['pdf']].get('error', '')[:300]}")
    if fixed:
        print(f"fixed by b: {len(fixed)}")
    if low:
        print("lowest similarity:")
        for r in low[:25]:
            rec = "recall {:.2f}→{:.2f}".format(*r["recall"]) if "recall" in r else "no text layer   "
            print(f"  {r['sim']:.3f} {rec} chars {r['chars'][0]:>7}→{r['chars'][1]:<7} img {r['images'][0]}→{r['images'][1]}  {Path(r['pdf']).name[:80]}")
    print("gates:")
    for name, ok in gates.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if args.json:
        Path(args.json).write_text(json.dumps({"rows": rows, "gates": gates}, indent=1))
    return 0 if all(gates.values()) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="parse PDFs with this install and record results")
    r.add_argument("--out", required=True)
    r.add_argument("--list", required=True, help="text file, one PDF path per line")
    c = sub.add_parser("compare", help="compare two runs and print the gate report")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--json")
    args = ap.parse_args()
    return cmd_run(args) if args.cmd == "run" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
