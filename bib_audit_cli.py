#!/usr/bin/env python3
"""Audit and conservatively correct a BibTeX bibliography using public scholarly APIs.

Resolution order:
  1. DOI lookup in Crossref, then DataCite, then OpenAlex.
  2. If the DOI is absent/invalid/inconsistent, title search in Crossref and OpenAlex.
  3. Candidates are scored against title, authors, year, and container title.

Outputs:
  - a corrected BibTeX file (original fields preserved unless a correction is reliable)
  - a detailed CSV report
  - a human-readable Markdown summary
  - a JSON cache allowing safe resume after interruption
  - a JSON decisions store recording manual feedback

Nothing is silently deleted. Ambiguous matches are reported for manual review.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import requests
from bibtexparser.bparser import BibTexParser
from bibtexparser.bwriter import BibTexWriter
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "4.2.0-cli"
DEFAULT_TIMEOUT = 25
# Adaptive thresholds: an officially resolved DOI requires less similarity
# than a title discovery. These thresholds prevent blindly lowering safety.
TITLE_STRONG = 0.94
TITLE_GOOD = 0.86
TITLE_DOI_MIN = 0.72
TOTAL_STRONG = 0.88
TOTAL_CONTEXTUAL = 0.80
DOI_CONSISTENCY_MIN = 0.68


def strip_tex(value: str) -> str:
    """Normalize common TeX constructs sufficiently for metadata matching."""
    value = html.unescape(value or "")
    value = re.sub(r"\\(?:textit|textbf|emph|mathrm|mathbf|mathit|url)\s*\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"\\[a-zA-Z]+\s*\{([^{}]*)\}", r"\1", value)
    value = value.replace("{", "").replace("}", "")
    value = value.replace("~", " ")
    value = re.sub(r"\\[&%_$#]", " ", value)
    return value


def normalize(value: str) -> str:
    value = strip_tex(value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def tokens(value: str) -> set[str]:
    return {t for t in normalize(value).split() if len(t) > 1}


def similarity(a: str, b: str) -> float:
    """Hybrid token similarity with a sequence component."""
    from difflib import SequenceMatcher
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    containment = len(ta & tb) / min(len(ta), len(tb)) if ta and tb else 0.0
    return 0.45 * jac + 0.35 * seq + 0.20 * containment


def clean_doi(value: str) -> str:
    value = (value or "").strip().strip("{}")
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    return value.strip().rstrip(".,;)")


def valid_doi(value: str) -> bool:
    return bool(re.fullmatch(r"10\.\d{4,9}/\S+", clean_doi(value), flags=re.I))


def get_year(entry: Dict[str, Any]) -> Optional[int]:
    for key in ("year", "date"):
        match = re.search(r"(?:19|20)\d{2}", str(entry.get(key, "")))
        if match:
            return int(match.group())
    return None


def bib_author_surnames(author_field: str) -> List[str]:
    result = []
    for author in re.split(r"\s+and\s+", author_field or "", flags=re.I):
        author = strip_tex(author).strip()
        if not author:
            continue
        surname = author.split(",", 1)[0] if "," in author else author.split()[-1]
        surname = normalize(surname)
        if surname:
            result.append(surname)
    return result


def author_similarity(bib_authors: str, candidate_authors: Sequence[str]) -> float:
    a, b = set(bib_author_surnames(bib_authors)), {normalize(x) for x in candidate_authors if x}
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def format_bibtex_authors(authors: Sequence[Dict[str, str]]) -> str:
    formatted = []
    for person in authors:
        family = (person.get("family") or "").strip()
        given = (person.get("given") or "").strip()
        literal = (person.get("literal") or person.get("name") or "").strip()
        if family and given:
            formatted.append(f"{family}, {given}")
        elif family:
            formatted.append(family)
        elif literal:
            formatted.append("{" + literal + "}")
    return " and ".join(formatted)


def first_nonempty(*values: Any) -> str:
    for value in values:
        if isinstance(value, list):
            value = value[0] if value else ""
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def title_variants(title: str) -> List[str]:
    """Produce conservative title variants for subtitle/version/punctuation differences."""
    raw = strip_tex(title or "").strip()
    variants = [raw]
    # Subtitle-free variants are useful for publisher records omitting subtitles.
    for separator in (":", " - ", " — ", " – "):
        if separator in raw:
            head = raw.split(separator, 1)[0].strip()
            if len(head.split()) >= 4:
                variants.append(head)
    # Remove common non-semantic suffixes and source labels.
    cleaned = re.sub(r"\s*[\[(](?:preprint|working paper|technical report|extended version|version\s*\d+)[^\])]*[\])]\s*$", "", raw, flags=re.I)
    variants.append(cleaned.strip())
    result = []
    seen = set()
    for value in variants:
        key = normalize(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def title_similarity(a: str, b: str) -> float:
    """Best score over full and subtitle-free title variants."""
    return max((similarity(x, y) for x in title_variants(a) for y in title_variants(b)), default=0.0)


def compact_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(value))


def key_author_token(candidate: "Candidate") -> str:
    """Create a stable ASCII surname token for a BibTeX key."""
    if not candidate.authors:
        return ""
    raw = candidate.authors[0].get("family") or candidate.authors[0].get("literal") or candidate.authors[0].get("name") or ""
    if not candidate.authors[0].get("family") and raw:
        raw = raw.split()[-1]
    return compact_name(raw)


def key_title_token(title: str) -> str:
    stop = {"a", "an", "the", "of", "on", "in", "for", "to", "and", "de", "des", "du", "la", "le", "les", "un", "une", "et"}
    for token in normalize(title).split():
        if len(token) >= 3 and token not in stop:
            return token
    return "work"


def unique_key(proposed: str, current: str, used: set[str]) -> str:
    if proposed == current or proposed not in used:
        return proposed
    suffix = "a"
    while proposed + suffix in used:
        suffix = chr(ord(suffix) + 1)
    return proposed + suffix


def corrected_key(current: str, candidate: "Candidate", used: set[str]) -> str:
    """Replace noauthor/nodate only when resolved metadata supplies the missing value."""
    new = current
    author = key_author_token(candidate)
    year = str(candidate.year or "")
    if re.search(r"noauthor", new, flags=re.I) and author:
        new = re.sub(r"noauthor", author, new, flags=re.I)
    if re.search(r"nodate", new, flags=re.I) and year:
        new = re.sub(r"nodate", year, new, flags=re.I)
    # If replacement produced an unusable key, fall back to author_title_year.
    new = re.sub(r"[^A-Za-z0-9_:\-./]+", "_", new).strip("_")
    if not new:
        new = "_".join(x for x in (author, key_title_token(candidate.title), year) if x)
    return unique_key(new, current, used)


@dataclass
class Candidate:
    source: str
    title: str = ""
    doi: str = ""
    authors: List[Dict[str, str]] = None  # type: ignore[assignment]
    year: Optional[int] = None
    container: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    url: str = ""
    item_type: str = ""
    score: float = 0.0
    title_score: float = 0.0
    author_score: float = 0.0
    year_score: float = 0.0
    container_score: float = 0.0
    evidence_count: int = 1

    def __post_init__(self) -> None:
        if self.authors is None:
            self.authors = []
        self.doi = clean_doi(self.doi)

    @property
    def surnames(self) -> List[str]:
        return [a.get("family") or a.get("literal") or a.get("name") or "" for a in self.authors]


@dataclass
class ReportRow:
    key: str
    entry_type: str
    status: str
    source: str
    confidence: str
    original_doi: str
    resolved_doi: str
    title_score: str
    author_score: str
    year_score: str
    changes: str
    warnings: str
    original_title: str
    resolved_title: str


class ScholarClient:
    def __init__(self, api_key: str = "", timeout: int = DEFAULT_TIMEOUT,
                 delay: float = 0.5, cache_path: Optional[Path] = None) -> None:
        self.api_key, self.timeout, self.delay = api_key, timeout, delay
        self.session = requests.Session()
        retry = Retry(total=5, connect=5, read=5, status=5, backoff_factor=1.0,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            "User-Agent": f"BibAudit/{VERSION}",
            "Accept": "application/json",
        })
        self.cache_path = cache_path
        self.cache: Dict[str, Any] = {}
        if cache_path and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                logging.warning("Unreadable cache, a new cache will be created.")

    def save_cache(self) -> None:
        if self.cache_path:
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        signature = url + "?" + json.dumps(params, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(signature.encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        time.sleep(self.delay)
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            if response.status_code == 404:
                self.cache[key] = None
                return None
            response.raise_for_status()
            data = response.json()
            self.cache[key] = data
            if len(self.cache) % 20 == 0:
                self.save_cache()
            return data
        except (requests.RequestException, ValueError) as exc:
            logging.warning("API request failed %s: %s", url, exc)
            return None

    def crossref_doi(self, doi: str) -> Optional[Candidate]:
        data = self.get_json(f"https://api.crossref.org/works/{quote(clean_doi(doi), safe='')}")
        return parse_crossref(data.get("message", {})) if data and data.get("message") else None

    def crossref_search(self, entry: Dict[str, Any], rows: int = 20) -> List[Candidate]:
        """Run complementary Crossref searches instead of trusting one ranking."""
        title = strip_tex(entry.get("title", ""))
        author = " ".join(bib_author_surnames(entry.get("author", ""))[:2])
        year = str(get_year(entry) or "")
        container = strip_tex(first_nonempty(entry.get("journal"), entry.get("booktitle")))
        select = "DOI,title,author,issued,published,container-title,volume,issue,page,publisher,URL,type"
        queries = []
        for variant in title_variants(title):
            queries.append({"query.title": variant, "rows": rows, "select": select})
        queries.append({"query.bibliographic": " ".join(x for x in (title, author, year, container) if x),
                        "rows": rows, "select": select})
        if author:
            queries.append({"query.title": title, "query.author": author, "rows": rows,
                            "select": select})
        results: List[Candidate] = []
        for params in queries:
            data = self.get_json("https://api.crossref.org/works", params)
            results.extend(parse_crossref(x) for x in (data or {}).get("message", {}).get("items", []))
        return results

    def datacite_doi(self, doi: str) -> Optional[Candidate]:
        data = self.get_json(f"https://api.datacite.org/dois/{quote(clean_doi(doi), safe='')}")
        attrs = (data or {}).get("data", {}).get("attributes")
        return parse_datacite(attrs) if attrs else None

    def openalex_doi(self, doi: str) -> Optional[Candidate]:
        params: Dict[str, Any] = {}
        if self.api_key:
            params["api_key"] = self.api_key
        data = self.get_json(f"https://api.openalex.org/works/https://doi.org/{quote(clean_doi(doi), safe='/')}", params)
        return parse_openalex(data) if data and data.get("id") else None

    def openalex_search(self, entry: Dict[str, Any], count: int = 20) -> List[Candidate]:
        """Search both exact and stemmed OpenAlex title indexes, optionally by year."""
        title = strip_tex(entry.get("title", ""))
        year = get_year(entry)
        base: Dict[str, Any] = {"per-page": count}
        if self.api_key:
            base["api_key"] = self.api_key
        variants = []
        for title_variant in title_variants(title):
            variants.extend([dict(base, **{"search.exact": title_variant}), dict(base, search=title_variant)])
            if year:
                variants.append(dict(base, search=title_variant, filter=f"publication_year:{year}"))
        results: List[Candidate] = []
        for params in variants:
            data = self.get_json("https://api.openalex.org/works", params)
            results.extend(parse_openalex(x) for x in (data or {}).get("results", []))
        return results


def crossref_year(data: Dict[str, Any]) -> Optional[int]:
    for field in ("published-print", "published-online", "published", "issued", "created"):
        parts = data.get(field, {}).get("date-parts", [])
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return None


def parse_crossref(d: Dict[str, Any]) -> Candidate:
    authors = [{"family": a.get("family", ""), "given": a.get("given", ""), "literal": a.get("name", "")}
               for a in d.get("author", [])]
    return Candidate("Crossref", first_nonempty(d.get("title")), d.get("DOI", ""), authors,
                     crossref_year(d), first_nonempty(d.get("container-title")), str(d.get("volume", "")),
                     str(d.get("issue", "")), str(d.get("page", "")), str(d.get("publisher", "")),
                     str(d.get("URL", "")), str(d.get("type", "")))


def parse_datacite(d: Dict[str, Any]) -> Candidate:
    authors = []
    for a in d.get("creators", []):
        authors.append({"family": a.get("familyName", ""), "given": a.get("givenName", ""), "literal": a.get("name", "")})
    titles = d.get("titles", [])
    title = titles[0].get("title", "") if titles else ""
    container = (d.get("container") or {}).get("title", "")
    return Candidate("DataCite", title, d.get("doi", ""), authors, d.get("publicationYear"), container,
                     publisher=str(d.get("publisher", "")), url=str(d.get("url", "")),
                     item_type=str((d.get("types") or {}).get("bibtex", "")))


def parse_openalex(d: Dict[str, Any]) -> Candidate:
    authors = []
    for a in d.get("authorships", []):
        name = (a.get("author") or {}).get("display_name", "")
        parts = name.split()
        authors.append({"family": parts[-1] if parts else "", "given": " ".join(parts[:-1]), "literal": name})
    location = d.get("primary_location") or {}
    source = location.get("source") or {}
    biblio = d.get("biblio") or {}
    return Candidate("OpenAlex", d.get("title", ""), d.get("doi", ""), authors, d.get("publication_year"),
                     source.get("display_name", ""), str(biblio.get("volume") or ""),
                     str(biblio.get("issue") or ""), page_range(biblio), "", location.get("landing_page_url", ""),
                     str(d.get("type", "")))


def page_range(biblio: Dict[str, Any]) -> str:
    first, last = biblio.get("first_page"), biblio.get("last_page")
    return f"{first}--{last}" if first and last and str(first) != str(last) else str(first or "")


def score_candidate(entry: Dict[str, Any], candidate: Candidate) -> Candidate:
    candidate.title_score = title_similarity(entry.get("title", ""), candidate.title)
    candidate.author_score = author_similarity(entry.get("author", ""), candidate.surnames)
    by, cy = get_year(entry), candidate.year
    candidate.year_score = 1.0 if by and cy and by == cy else (0.65 if by and cy and abs(by - cy) == 1 else 0.0)
    bib_container = first_nonempty(entry.get("journal"), entry.get("booktitle"))
    candidate.container_score = similarity(bib_container, candidate.container) if bib_container and candidate.container else 0.0
    weights = [(candidate.title_score, 0.62)]
    if entry.get("author") and candidate.authors:
        weights.append((candidate.author_score, 0.20))
    if by and cy:
        weights.append((candidate.year_score, 0.12))
    if bib_container and candidate.container:
        weights.append((candidate.container_score, 0.06))
    candidate.score = sum(v * w for v, w in weights) / sum(w for _, w in weights)
    return candidate


def candidate_identity(c: Candidate) -> Tuple[str, str]:
    doi = clean_doi(c.doi).casefold()
    return ("doi", doi) if doi else ("title", normalize(c.title))


def rank_candidates(entry: Dict[str, Any], candidates: Iterable[Candidate]) -> List[Candidate]:
    """Cluster duplicate API hits and retain the richest record per scholarly work."""
    groups: Dict[Tuple[str, str], List[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate_identity(candidate), []).append(score_candidate(entry, candidate))
    ranked: List[Candidate] = []
    for group in groups.values():
        best = max(group, key=lambda c: (c.score, len(c.authors), bool(c.doi)))
        best.evidence_count = len({c.source for c in group})
        # Independent agreement is useful evidence, but deliberately capped.
        best.score = min(1.0, best.score + 0.025 * (best.evidence_count - 1))
        ranked.append(best)
    return sorted(ranked, key=lambda c: c.score, reverse=True)


def acceptance_reason(entry: Dict[str, Any], best: Candidate, margin: float, doi_verified: bool = False) -> str:
    author_available = bool(entry.get("author") and best.authors)
    year_available = bool(get_year(entry) and best.year)
    container_available = bool(first_nonempty(entry.get("journal"), entry.get("booktitle")) and best.container)
    author_ok = best.author_score >= 0.45
    year_ok = best.year_score >= 0.65
    container_ok = best.container_score >= 0.60
    agreements = sum((author_ok, year_ok, container_ok))

    if doi_verified:
        if best.title_score >= 0.80:
            return "reliable: officially resolved DOI and matching title"
        if best.title_score >= 0.66 and agreements >= 1:
            return "reliable: officially resolved DOI and matching context"
        return "resolved DOI but substantial discrepancy"

    # Exact or near-exact titles are often enough when local metadata are sparse.
    if best.title_score >= 0.975 and margin >= 0.005:
        return "reliable: virtually identical title"
    if best.title_score >= 0.93 and margin >= 0.012 and (agreements >= 1 or not (author_available or year_available or container_available)):
        return "reliable: very strongly matching title"
    if best.title_score >= 0.86 and agreements >= 2 and margin >= 0.015:
        return "reliable: matching title and multiple metadata"
    if best.title_score >= 0.82 and best.evidence_count >= 2 and agreements >= 1 and margin >= 0.012:
        return "reliable: match confirmed by multiple sources"
    if best.title_score >= 0.78 and agreements >= 3 and margin >= 0.020:
        return "reliable: strong contextual match"
    if margin < 0.012:
        return "truly ambiguous candidates"
    return "insufficient confidence after enriched analysis"


def choose_best(entry: Dict[str, Any], candidates: Iterable[Candidate]) -> Tuple[Optional[Candidate], str]:
    ranked = rank_candidates(entry, candidates)
    if not ranked:
        return None, "no candidate"
    margin = ranked[0].score - ranked[1].score if len(ranked) > 1 else 1.0
    reason = acceptance_reason(entry, ranked[0], margin, doi_verified=False)
    return ranked[0], reason


def field_change(entry: Dict[str, Any], field: str, new_value: Any, changes: List[str], update: bool = True) -> None:
    new = str(new_value or "").strip()
    old = str(entry.get(field, "")).strip()
    if not new or normalize(old) == normalize(new):
        return
    if update:
        changes.append(f"{field}: {old or '[absent]'} -> {new}")
        entry[field] = new


def apply_candidate(entry: Dict[str, Any], c: Candidate, overwrite: bool) -> List[str]:
    changes: List[str] = []
    # Reliable identity fields are corrected; optional bibliographic fields fill gaps by default.
    field_change(entry, "doi", c.doi, changes, True)
    field_change(entry, "title", c.title, changes, True)
    field_change(entry, "author", format_bibtex_authors(c.authors), changes, True)
    if c.year:
        field_change(entry, "year", c.year, changes, True)
    container_field = "journal" if entry.get("ENTRYTYPE", "").lower() == "article" else "booktitle"
    if c.container:
        field_change(entry, container_field, c.container, changes, overwrite or not entry.get(container_field))
    for field, value in (("volume", c.volume), ("number", c.issue), ("pages", c.pages), ("publisher", c.publisher)):
        if value:
            field_change(entry, field, value, changes, overwrite or not entry.get(field))
    if c.url and not entry.get("url"):
        field_change(entry, "url", c.url, changes, True)
    return changes


def process_entry(entry: Dict[str, Any], client: ScholarClient, overwrite: bool, used_keys: set[str]) -> ReportRow:
    key, original_title = entry.get("ID", "[no key]"), entry.get("title", "")
    original_doi = clean_doi(entry.get("doi", ""))
    warnings: List[str] = []
    direct: List[Candidate] = []
    if original_doi and valid_doi(original_doi):
        for lookup in (client.crossref_doi, client.datacite_doi, client.openalex_doi):
            candidate = lookup(original_doi)
            if candidate:
                direct.append(score_candidate(entry, candidate))
        consistent = [c for c in direct if c.title_score >= DOI_CONSISTENCY_MIN]
        if consistent:
            ranked = rank_candidates(entry, consistent)
            best = ranked[0]
            margin = best.score - ranked[1].score if len(ranked) > 1 else 1.0
            decision = acceptance_reason(entry, best, margin, doi_verified=True)
            if decision.startswith(("reliable", "fiable")):
                changes = apply_candidate(entry, best, overwrite)
                new_key = corrected_key(key, best, used_keys)
                if new_key != key:
                    changes.append(f"citation_key: {key} -> {new_key}")
                    used_keys.discard(key)
                    used_keys.add(new_key)
                    entry["ID"] = new_key
                warnings.append(decision)
                status = "corrected" if changes else "verified"
            else:
                changes = []
                warnings.append(decision + "; no automatic modification.")
                status = "to review"
            return make_row(entry, key, status, best, original_doi, original_title, changes, warnings)
        warnings.append("DOI missing from APIs or inconsistent with BibTeX title; performed title search.")
    elif original_doi:
        warnings.append("Invalid DOI syntax; performed title search.")

    title = entry.get("title", "")
    if not title:
        return make_row(entry, key, "not found", None, original_doi, original_title, [], warnings + ["Missing title."])
    candidates = client.crossref_search(entry) + client.openalex_search(entry)
    best, decision = choose_best(entry, candidates)
    if best and decision.startswith(("reliable", "fiable")):
        changes = apply_candidate(entry, best, overwrite)
        new_key = corrected_key(key, best, used_keys)
        if new_key != key:
            changes.append(f"citation_key: {key} -> {new_key}")
            used_keys.discard(key)
            used_keys.add(new_key)
            entry["ID"] = new_key
        warnings.append(decision)
        return make_row(entry, key, "corrected" if changes else "verified", best, original_doi, original_title, changes, warnings)
    if best:
        warnings.append(f"{decision}; no automatic modification.")
        return make_row(entry, key, "to review", best, original_doi, original_title, [], warnings)
    warnings.append("No match found by DOI or title.")
    return make_row(entry, key, "not found", None, original_doi, original_title, [], warnings)


def make_row(entry: Dict[str, Any], key: str, status: str, c: Optional[Candidate], original_doi: str,
             original_title: str, changes: Sequence[str], warnings: Sequence[str]) -> ReportRow:
    return ReportRow(key, entry.get("ENTRYTYPE", ""), status, c.source if c else "",
                     f"{c.score:.3f}" if c else "", original_doi, c.doi if c else "",
                     f"{c.title_score:.3f}" if c else "", f"{c.author_score:.3f}" if c else "",
                     f"{c.year_score:.3f}" if c else "", " | ".join(changes), " | ".join(warnings),
                     original_title, c.title if c else "")


def write_reports(rows: Sequence[ReportRow], csv_path: Path, md_path: Path) -> None:
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows else list(ReportRow.__annotations__))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    lines = ["# BibTeX Verification Report", "", f"- Analyzed entries: **{len(rows)}**"]
    for status in ("verified", "corrected", "to review", "not found", "error", "kept", "manually corrected"):
        if status in counts:
            lines.append(f"- {status.capitalize()}: **{counts[status]}**")
    lines += ["", "## Entries requiring manual review", ""]
    problematic = [r for r in rows if r.status in ("to review", "not found", "error", "à revoir", "non trouvé", "erreur")]
    if not problematic:
        lines.append("None.")
    else:
        for r in problematic:
            lines.append(f"### `{r.key}` - {r.status}")
            lines.append(f"- Title: {r.original_title}")
            if r.resolved_title:
                lines.append(f"- Best candidate: {r.resolved_title}")
            if r.resolved_doi:
                lines.append(f"- Candidate DOI: `{r.resolved_doi}`")
            if r.warnings:
                lines.append(f"- Reason: {r.warnings}")
            lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")


class DecisionStore:
    """Persistent storage for user manual decisions."""

    def __init__(self, path: Optional[Path] = None, reset: bool = False) -> None:
        self.path = path
        self.decisions: Dict[str, Dict[str, Any]] = {}
        if path and path.exists() and not reset:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.decisions = data
            except Exception as exc:
                logging.warning("Unable to load existing decisions (%s): %s", path, exc)

    def save(self) -> None:
        if self.path:
            try:
                self.path.write_text(json.dumps(self.decisions, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                logging.warning("Failed to save decisions to %s: %s", self.path, exc)

    def get(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = entry.get("ID", "")
        if key and key in self.decisions:
            return self.decisions[key]
        title = entry.get("title", "")
        if title:
            title_key = "title:" + normalize(title)
            if title_key in self.decisions:
                return self.decisions[title_key]
        return None

    def record(self, key: str, choice: str, status: str, changes: Sequence[str],
               note: str, entry: Dict[str, Any]) -> None:
        rec = {
            "key": key,
            "choice": choice,
            "status": status,
            "changes": list(changes),
            "note": note,
            "fields": {k: v for k, v in entry.items()},
            "updated_at": time.time(),
        }
        self.decisions[key] = rec
        title = entry.get("title", "")
        if title:
            self.decisions["title:" + normalize(title)] = rec
        self.save()

    def count(self) -> int:
        return len([k for k in self.decisions if not k.startswith("title:")])


def feedback_review(entry: Dict[str, Any], row: ReportRow, client: ScholarClient,
                    overwrite: bool, index: int = 0, total: int = 0) -> Tuple[str, List[str], str, str]:
    """Ask for a decision immediately when an entry needs manual review."""
    header = f" Entry {f'[{index}/{total}] ' if index and total else ''}: {row.key} "
    print("\n" + "=" * max(78, len(header) + 4))
    print(header)
    print("=" * max(78, len(header) + 4))
    print(f"Bib Title : {row.original_title or '[absent]'}")
    print(f"Bib DOI   : {row.original_doi or '[absent]'}")
    print(f"Authors   : {entry.get('author', '[absent]')}")
    print(f"Year/Jour : {entry.get('year', '')} / {entry.get('journal') or entry.get('booktitle') or ''}")
    print("-" * 78)
    print(f"Candidate : {row.resolved_title or '[no candidate]'}")
    print(f"Found DOI : {row.resolved_doi or '[absent]'}")
    print(f"Source    : {row.source or '[unknown]'}")
    print(f"Scores    : Title={row.title_score or '-'} | Authors={row.author_score or '-'} | Year={row.year_score or '-'} | Overall={row.confidence or '-'}")
    print(f"Reason    : {row.warnings or '-'}")
    print("\n[a] accept candidate  [m] manually edit  [k] keep original  [q] quit and save")

    while True:
        choice = input("Your choice [a/m/k/q]: ").strip().casefold()
        if choice in {"a", "m", "k", "g", "q"}:
            if choice == "g":
                choice = "k"
            break
        print("Invalid choice. Please use a, m, k, or q.")

    if choice == "q":
        return "quit", [], "Interruption requested by user", "q"

    if choice == "k":
        note = input("Optional comment: ").strip()
        return "kept", [], note or "Original entry kept by user decision", "k"

    if choice == "m":
        changes: List[str] = []
        defaults = {
            "doi": row.resolved_doi or entry.get("doi", ""),
            "title": row.resolved_title or entry.get("title", ""),
            "author": entry.get("author", ""),
            "year": entry.get("year", ""),
        }
        for field, label in (("doi", "DOI"), ("title", "Title"),
                             ("author", "BibTeX Authors"), ("year", "Year")):
            value = input(f"{label} [{defaults[field]}]: ").strip() or str(defaults[field])
            old = str(entry.get(field, "")).strip()
            new = clean_doi(value) if field == "doi" else value.strip()
            if new and normalize(new) != normalize(old):
                entry[field] = new
                changes.append(f"{field}: {old or '[absent]'} -> {new}")
        note = input("Optional comment: ").strip()
        return "manually corrected" if changes else "kept", changes, note, "m"

    # Accept: reload the exact proposed work. This normally comes straight from cache.
    candidate: Optional[Candidate] = None
    if row.resolved_doi:
        candidate = (client.crossref_doi(row.resolved_doi)
                     or client.datacite_doi(row.resolved_doi)
                     or client.openalex_doi(row.resolved_doi))
    if candidate is None:
        ranked = rank_candidates(entry, client.crossref_search(entry) + client.openalex_search(entry))
        if ranked:
            candidate = ranked[0]
    if candidate is None:
        print("Unable to reload candidate. Please use manual edit instead.")
        return feedback_review(entry, row, client, overwrite, index=index, total=total)
    changes = apply_candidate(entry, candidate, overwrite)
    note = input("Optional comment: ").strip()
    return "corrected" if changes else "verified", changes, note, "a"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BibAudit: adaptive verification and conservative correction via Crossref, DataCite, and OpenAlex.")
    p.add_argument("input", type=Path, help="Input .bib or .bib.txt file to audit")
    p.add_argument("--output", type=Path, default=Path("bib-verified.bib"), help="Corrected BibTeX output file")
    p.add_argument("--report", type=Path, default=Path("audit-report.csv"), help="Detailed CSV audit report")
    p.add_argument("--summary", type=Path, default=Path("audit-summary.md"), help="Human-readable Markdown summary")
    p.add_argument("--cache", type=Path, default=Path(".bib-audit-cache.json"), help="Resumable JSON cache file")
    p.add_argument("--decisions", type=Path, default=Path(".bib-audit-decisions.json"),
                   help="JSON decisions store (allows interrupting with 'q' and safely resuming)")
    p.add_argument("--reset-decisions", action="store_true",
                   help="Ignore and reset previously recorded manual decisions")
    p.add_argument("--openalex-api-key", default="", help="Optional OpenAlex API key for higher rate limits")
    p.add_argument("--overwrite-bibliographic-fields", action="store_true",
                   help="Overwrite journal/booktitle, volume, issue, pages, and publisher even if already present")
    p.add_argument("--delay", "--delay-between-requests", dest="delay", type=float, default=0.5,
                   help="Minimum delay between HTTP requests in seconds (default: 0.5)")
    p.add_argument("--limit", type=int, default=0, help="Entry limit for test runs; 0 = process all")
    p.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")
    p.add_argument("--feedback", action="store_true", help="Prompt interactively for ambiguous entries")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    if not args.input.exists():
        logging.error("File not found: %s", args.input)
        return 2
    parser = BibTexParser(common_strings=True)
    # Force an identity customization. This prevents any environment-specific
    # default or previously copied convert_to_unicode callback from running.
    parser.customization = lambda record: record
    try:
        text = args.input.read_text(encoding="utf-8-sig")
        import unicodedata
        original_combining = unicodedata.combining

        def safe_combining(value: str) -> int:
            if not isinstance(value, str) or len(value) != 1:
                return 0
            return original_combining(value)

        unicodedata.combining = safe_combining
        try:
            database = parser.parse(text)
        finally:
            unicodedata.combining = original_combining
    except Exception as exc:
        logging.error("BibTeX parsing failed with BibAudit %s: %s", VERSION, exc)
        if "combining()" in str(exc):
            logging.error("Unicode safety guard was not applied as expected in this execution.")
        elif isinstance(exc, KeyError):
            logging.error("Undefined BibTeX macro detected: %s. Standard months are supported in this version.", exc)
        return 2
    entries = database.entries[:args.limit or None]
    logging.info("%d entries to check", len(entries))
    client = ScholarClient(args.openalex_api_key, delay=max(args.delay, 0), cache_path=args.cache)
    decisions = DecisionStore(args.decisions, reset=args.reset_decisions)
    if decisions.count() > 0:
        logging.info("%d manual decision(s) loaded from %s (automatic resume enabled)",
                     decisions.count(), args.decisions)
    rows: List[ReportRow] = []
    used_keys = {entry.get("ID", "") for entry in database.entries}
    interrupted = False

    try:
        for i, entry in enumerate(entries, 1):
            key = entry.get("ID", "[no key]")
            logging.info("[%d/%d] %s", i, len(entries), key)
            try:
                saved_decision = decisions.get(entry) if args.feedback else None
                if saved_decision:
                    row = process_entry(entry, client, args.overwrite_bibliographic_fields, used_keys)
                    if "fields" in saved_decision:
                        old_key = entry.get("ID", "")
                        entry.clear()
                        entry.update(saved_decision["fields"])
                        new_key = entry.get("ID", "")
                        if old_key and new_key and old_key != new_key:
                            used_keys.discard(old_key)
                            used_keys.add(new_key)
                    row.key = entry.get("ID", key)
                    row.status = saved_decision.get("status", row.status)
                    if saved_decision.get("changes"):
                        row.changes = " | ".join(saved_decision["changes"])
                    note = saved_decision.get("note", "")
                    if note:
                        row.warnings = (row.warnings + " | " if row.warnings else "") + "Feedback (resumed): " + note
                    logging.info("  -> Previous decision restored for '%s' [%s]: %s",
                                 key, saved_decision.get("choice", ""), row.status)
                    rows.append(row)
                    continue

                row = process_entry(entry, client, args.overwrite_bibliographic_fields, used_keys)
                if args.feedback and row.status in ("to review", "not found", "à revoir", "non trouvé"):
                    status, feedback_changes, note, choice = feedback_review(
                        entry, row, client, args.overwrite_bibliographic_fields, index=i, total=len(entries))
                    if status == "quit":
                        interrupted = True
                        rows.append(row)
                        print("\n" + "=" * 78)
                        logging.info("Stopped by user ('q' option).")
                        logging.info("Your decisions (%d total) are saved in %s.", decisions.count(), args.decisions)
                        logging.info("You can resume the review at any time by running the same command.")
                        print("=" * 78 + "\n")
                        break
                    row.status = status
                    if feedback_changes:
                        row.changes = " | ".join(feedback_changes)
                    if note:
                        row.warnings = (row.warnings + " | " if row.warnings else "") + "Feedback: " + note
                    decisions.record(key, choice=choice, status=row.status,
                                     changes=feedback_changes, note=note, entry=entry)
                rows.append(row)
            except KeyboardInterrupt:
                interrupted = True
                print("\n" + "=" * 78)
                logging.warning("Interruption detected (Ctrl+C). Your choices remain saved in %s.", args.decisions)
                print("=" * 78 + "\n")
                break
            except Exception as exc:
                logging.exception("Error on %s", entry.get("ID", "[no key]"))
                rows.append(make_row(entry, entry.get("ID", "[no key]"), "error", None,
                                     clean_doi(entry.get("doi", "")), entry.get("title", ""), [], [str(exc)]))
    except KeyboardInterrupt:
        interrupted = True
        logging.warning("External interruption requested.")
    finally:
        client.save_cache()
        decisions.save()

    writer = BibTexWriter()
    writer.indent = "  "
    writer.order_entries_by = None
    args.output.write_text(writer.write(database), encoding="utf-8")
    write_reports(rows, args.report, args.summary)
    if interrupted:
        logging.info("Partial save completed: %s, %s, %s", args.output, args.report, args.summary)
    else:
        logging.info("Completed: %s, %s, %s", args.output, args.report, args.summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
