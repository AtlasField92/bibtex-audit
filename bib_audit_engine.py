#!/usr/bin/env python3
"""BibAudit Engine: Bibliography auditing, scoring, and resolution engine.

Supports:
- DOI resolution and title search via Crossref, DataCite, OpenAlex.
- Fully configurable sensitivity thresholds and score weightings.
- 3 execution modes: Automatic, Manual, Hybrid (User Feedback).
- Persistent query caching and manual decision storage.
- Step-by-step / asynchronous execution tailored for graphical interfaces and CLIs.
"""
from __future__ import annotations

import csv
import enum
import hashlib
import html
import json
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import requests
from bibtexparser.bparser import BibTexParser
from bibtexparser.bwriter import BibTexWriter
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "4.2.0-gui"
DEFAULT_TIMEOUT = 25


class ExecutionMode(enum.Enum):
    AUTO = "auto"          # Resolves everything automatically based on thresholds
    HYBRID = "hybrid"      # Resolves reliable matches, requests feedback for ambiguous ones (to review / not found)
    MANUAL = "manual"      # Requests confirmation for each entry with a candidate or modification


@dataclass
class SensitivitySettings:
    """Configurable sensitivity thresholds and component weights."""
    # Similarity thresholds
    title_strong: float = 0.94
    title_good: float = 0.86
    title_doi_min: float = 0.72
    doi_consistency_min: float = 0.68
    author_min: float = 0.45
    year_min: float = 0.65
    container_min: float = 0.60
    margin_min: float = 0.012

    # Global score weights (automatically normalized)
    weight_title: float = 0.62
    weight_author: float = 0.20
    weight_year: float = 0.12
    weight_container: float = 0.06

    def reset_defaults(self) -> None:
        self.title_strong = 0.94
        self.title_good = 0.86
        self.title_doi_min = 0.72
        self.doi_consistency_min = 0.68
        self.author_min = 0.45
        self.year_min = 0.65
        self.container_min = 0.60
        self.margin_min = 0.012
        self.weight_title = 0.62
        self.weight_author = 0.20
        self.weight_year = 0.12
        self.weight_container = 0.06

    def apply_preset(self, preset_name: str) -> None:
        p = preset_name.lower()
        if "prudent" in p or "strict" in p:
            self.title_strong = 0.96
            self.title_good = 0.90
            self.title_doi_min = 0.80
            self.doi_consistency_min = 0.75
            self.author_min = 0.60
            self.year_min = 0.80
            self.container_min = 0.70
            self.margin_min = 0.020
            self.weight_title = 0.65
            self.weight_author = 0.20
            self.weight_year = 0.10
            self.weight_container = 0.05
        elif "permissive" in p or "permissif" in p or "flexible" in p or "souple" in p:
            self.title_strong = 0.88
            self.title_good = 0.80
            self.title_doi_min = 0.60
            self.doi_consistency_min = 0.55
            self.author_min = 0.35
            self.year_min = 0.50
            self.container_min = 0.45
            self.margin_min = 0.006
            self.weight_title = 0.55
            self.weight_author = 0.25
            self.weight_year = 0.12
            self.weight_container = 0.08
        else:  # Standard / Balanced
            self.reset_defaults()


@dataclass
class AuditSettings:
    input_file: Path = field(default_factory=lambda: Path("references.bib"))
    output_file: Path = field(default_factory=lambda: Path("bib-verified.bib"))
    report_file: Path = field(default_factory=lambda: Path("audit-report.csv"))
    summary_file: Path = field(default_factory=lambda: Path("audit-summary.md"))
    cache_file: Path = field(default_factory=lambda: Path(".bib-audit-cache.json"))
    decisions_file: Path = field(default_factory=lambda: Path(".bib-audit-decisions.json"))

    mode: ExecutionMode = ExecutionMode.HYBRID
    sensitivity: SensitivitySettings = field(default_factory=SensitivitySettings)

    openalex_api_key: str = ""
    delay: float = 0.5
    timeout: int = DEFAULT_TIMEOUT
    limit: int = 0
    overwrite_bibliographic_fields: bool = False
    reset_decisions: bool = False


# ============================================================================
# TeX and String Normalization Utilities
# ============================================================================

def strip_tex(value: str) -> str:
    """Normalize common TeX commands for metadata comparison."""
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


def similarity(a: str, b: str) -> float:
    """Hybrid Jaccard, sequence, and containment similarity metric."""
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
    raw = strip_tex(title or "").strip()
    variants = [raw]
    for separator in (":", " - ", " — ", " – "):
        if separator in raw:
            head = raw.split(separator, 1)[0].strip()
            if len(head.split()) >= 4:
                variants.append(head)
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
    return max((similarity(x, y) for x in title_variants(a) for y in title_variants(b)), default=0.0)


def compact_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(value))


def key_author_token(candidate: "Candidate") -> str:
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


def unique_key(proposed: str, current: str, used: Set[str]) -> str:
    if proposed == current or proposed not in used:
        return proposed
    suffix = "a"
    while proposed + suffix in used:
        suffix = chr(ord(suffix) + 1)
    return proposed + suffix


def corrected_key(current: str, candidate: "Candidate", used: Set[str]) -> str:
    new = current
    author = key_author_token(candidate)
    year = str(candidate.year or "")
    if re.search(r"noauthor", new, flags=re.I) and author:
        new = re.sub(r"noauthor", author, new, flags=re.I)
    if re.search(r"nodate", new, flags=re.I) and year:
        new = re.sub(r"nodate", year, new, flags=re.I)
    new = re.sub(r"[^A-Za-z0-9_:\-./]+", "_", new).strip("_")
    if not new:
        new = "_".join(x for x in (author, key_title_token(candidate.title), year) if x)
    return unique_key(new, current, used)


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class Candidate:
    source: str
    title: str = ""
    doi: str = ""
    authors: List[Dict[str, str]] = field(default_factory=list)
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

    @property
    def formatted_authors(self) -> str:
        return format_bibtex_authors(self.authors)


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
    candidate: Optional[Candidate] = None
    entry_ref: Optional[Dict[str, Any]] = None


# ============================================================================
# API Client
# ============================================================================

class ScholarClient:
    def __init__(self, api_key: str = "", timeout: int = DEFAULT_TIMEOUT,
                 delay: float = 0.5, cache_path: Optional[Path] = None) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.delay = delay
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
            try:
                self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                logging.warning("Error saving cache: %s", exc)

    def clear_cache(self) -> None:
        self.cache.clear()
        if self.cache_path and self.cache_path.exists():
            try:
                self.cache_path.unlink()
            except Exception as exc:
                logging.warning("Unable to delete cache file: %s", exc)

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        signature = url + "?" + json.dumps(params, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(signature.encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        if self.delay > 0:
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
        clean = clean_doi(doi)
        if not clean:
            return None
        data = self.get_json(f"https://api.crossref.org/works/{quote(clean, safe='')}")
        return parse_crossref(data.get("message", {})) if data and data.get("message") else None

    def crossref_search(self, entry: Dict[str, Any], rows: int = 20) -> List[Candidate]:
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
            queries.append({"query.title": title, "query.author": author, "rows": rows, "select": select})
        results: List[Candidate] = []
        for params in queries:
            data = self.get_json("https://api.crossref.org/works", params)
            results.extend(parse_crossref(x) for x in (data or {}).get("message", {}).get("items", []))
        return results

    def datacite_doi(self, doi: str) -> Optional[Candidate]:
        clean = clean_doi(doi)
        if not clean:
            return None
        data = self.get_json(f"https://api.datacite.org/dois/{quote(clean, safe='')}")
        attrs = (data or {}).get("data", {}).get("attributes")
        return parse_datacite(attrs) if attrs else None

    def openalex_doi(self, doi: str) -> Optional[Candidate]:
        clean = clean_doi(doi)
        if not clean:
            return None
        params: Dict[str, Any] = {}
        if self.api_key:
            params["api_key"] = self.api_key
        data = self.get_json(f"https://api.openalex.org/works/https://doi.org/{quote(clean, safe='/')}", params)
        return parse_openalex(data) if data and data.get("id") else None

    def openalex_search(self, entry: Dict[str, Any], count: int = 20) -> List[Candidate]:
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

    def search_query(self, query: str, count: int = 10) -> List[Candidate]:
        """Generic manual search across Crossref and OpenAlex."""
        results: List[Candidate] = []
        if valid_doi(query):
            for lookup in (self.crossref_doi, self.datacite_doi, self.openalex_doi):
                c = lookup(query)
                if c:
                    results.append(c)
            if results:
                return results

        mock_entry = {"title": query}
        results.extend(self.crossref_search(mock_entry, rows=count))
        results.extend(self.openalex_search(mock_entry, count=count))
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


# ============================================================================
# Scoring & Ranking Logic
# ============================================================================

def score_candidate(entry: Dict[str, Any], candidate: Candidate, sensitivity: Optional[SensitivitySettings] = None) -> Candidate:
    sens = sensitivity or SensitivitySettings()
    candidate.title_score = title_similarity(entry.get("title", ""), candidate.title)
    candidate.author_score = author_similarity(entry.get("author", ""), candidate.surnames)
    by, cy = get_year(entry), candidate.year
    candidate.year_score = 1.0 if by and cy and by == cy else (0.65 if by and cy and abs(by - cy) == 1 else 0.0)
    bib_container = first_nonempty(entry.get("journal"), entry.get("booktitle"))
    candidate.container_score = similarity(bib_container, candidate.container) if bib_container and candidate.container else 0.0

    weights = [(candidate.title_score, sens.weight_title)]
    if entry.get("author") and candidate.authors:
        weights.append((candidate.author_score, sens.weight_author))
    if by and cy:
        weights.append((candidate.year_score, sens.weight_year))
    if bib_container and candidate.container:
        weights.append((candidate.container_score, sens.weight_container))

    total_w = sum(w for _, w in weights)
    candidate.score = sum(v * w for v, w in weights) / total_w if total_w > 0 else 0.0
    return candidate


def candidate_identity(c: Candidate) -> Tuple[str, str]:
    doi = clean_doi(c.doi).casefold()
    return ("doi", doi) if doi else ("title", normalize(c.title))


def rank_candidates(entry: Dict[str, Any], candidates: Iterable[Candidate], sensitivity: Optional[SensitivitySettings] = None) -> List[Candidate]:
    sens = sensitivity or SensitivitySettings()
    groups: Dict[Tuple[str, str], List[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate_identity(candidate), []).append(score_candidate(entry, candidate, sens))
    ranked: List[Candidate] = []
    for group in groups.values():
        best = max(group, key=lambda c: (c.score, len(c.authors), bool(c.doi)))
        best.evidence_count = len({c.source for c in group})
        best.score = min(1.0, best.score + 0.025 * (best.evidence_count - 1))
        ranked.append(best)
    return sorted(ranked, key=lambda c: c.score, reverse=True)


def acceptance_reason(entry: Dict[str, Any], best: Candidate, margin: float,
                      doi_verified: bool = False, sensitivity: Optional[SensitivitySettings] = None) -> str:
    sens = sensitivity or SensitivitySettings()
    author_available = bool(entry.get("author") and best.authors)
    year_available = bool(get_year(entry) and best.year)
    container_available = bool(first_nonempty(entry.get("journal"), entry.get("booktitle")) and best.container)

    author_ok = best.author_score >= sens.author_min
    year_ok = best.year_score >= sens.year_min
    container_ok = best.container_score >= sens.container_min
    agreements = sum((author_ok, year_ok, container_ok))

    if doi_verified:
        if best.title_score >= 0.80:
            return "reliable: officially resolved DOI and matching title"
        if best.title_score >= sens.doi_consistency_min and agreements >= 1:
            return "reliable: officially resolved DOI and matching context"
        return "resolved DOI but substantial discrepancy"

    if best.title_score >= 0.975 and margin >= 0.005:
        return "reliable: virtually identical title"
    if best.title_score >= sens.title_strong and margin >= sens.margin_min and (agreements >= 1 or not (author_available or year_available or container_available)):
        return "reliable: very strongly matching title"
    if best.title_score >= sens.title_good and agreements >= 2 and margin >= sens.margin_min:
        return "reliable: matching title and multiple metadata"
    if best.title_score >= (sens.title_good - 0.04) and best.evidence_count >= 2 and agreements >= 1 and margin >= sens.margin_min:
        return "reliable: match confirmed by multiple sources"
    if best.title_score >= (sens.title_good - 0.08) and agreements >= 3 and margin >= (sens.margin_min * 1.5):
        return "reliable: strong contextual match"
    if margin < sens.margin_min:
        return "truly ambiguous candidates"
    return "insufficient confidence after enriched analysis"


def choose_best(entry: Dict[str, Any], candidates: Iterable[Candidate],
                sensitivity: Optional[SensitivitySettings] = None) -> Tuple[Optional[Candidate], str]:
    ranked = rank_candidates(entry, candidates, sensitivity)
    if not ranked:
        return None, "no candidate"
    margin = ranked[0].score - ranked[1].score if len(ranked) > 1 else 1.0
    reason = acceptance_reason(entry, ranked[0], margin, doi_verified=False, sensitivity=sensitivity)
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


def make_row(entry: Dict[str, Any], key: str, status: str, c: Optional[Candidate], original_doi: str,
             original_title: str, changes: Sequence[str], warnings: Sequence[str]) -> ReportRow:
    return ReportRow(
        key=key,
        entry_type=entry.get("ENTRYTYPE", ""),
        status=status,
        source=c.source if c else "",
        confidence=f"{c.score:.3f}" if c else "",
        original_doi=original_doi,
        resolved_doi=c.doi if c else "",
        title_score=f"{c.title_score:.3f}" if c else "",
        author_score=f"{c.author_score:.3f}" if c else "",
        year_score=f"{c.year_score:.3f}" if c else "",
        changes=" | ".join(changes),
        warnings=" | ".join(warnings),
        original_title=original_title,
        resolved_title=c.title if c else "",
        candidate=c,
        entry_ref=entry,
    )


def process_entry(entry: Dict[str, Any], client: ScholarClient, overwrite: bool,
                  used_keys: Set[str], sensitivity: Optional[SensitivitySettings] = None) -> ReportRow:
    sens = sensitivity or SensitivitySettings()
    key, original_title = entry.get("ID", "[no key]"), entry.get("title", "")
    original_doi = clean_doi(entry.get("doi", ""))
    warnings: List[str] = []
    direct: List[Candidate] = []

    if original_doi and valid_doi(original_doi):
        for lookup in (client.crossref_doi, client.datacite_doi, client.openalex_doi):
            candidate = lookup(original_doi)
            if candidate:
                direct.append(score_candidate(entry, candidate, sens))
        consistent = [c for c in direct if c.title_score >= sens.doi_consistency_min]
        if consistent:
            ranked = rank_candidates(entry, consistent, sens)
            best = ranked[0]
            margin = best.score - ranked[1].score if len(ranked) > 1 else 1.0
            decision = acceptance_reason(entry, best, margin, doi_verified=True, sensitivity=sens)
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
            return make_row(entry, entry.get("ID", key), status, best, original_doi, original_title, changes, warnings)
        warnings.append("DOI missing from APIs or inconsistent with BibTeX title; performed title search.")
    elif original_doi:
        warnings.append("Invalid DOI syntax; performed title search.")

    title = entry.get("title", "")
    if not title:
        return make_row(entry, key, "not found", None, original_doi, original_title, [], warnings + ["Missing title."])

    candidates = client.crossref_search(entry) + client.openalex_search(entry)
    best, decision = choose_best(entry, candidates, sens)
    if best and decision.startswith(("reliable", "fiable")):
        changes = apply_candidate(entry, best, overwrite)
        new_key = corrected_key(key, best, used_keys)
        if new_key != key:
            changes.append(f"citation_key: {key} -> {new_key}")
            used_keys.discard(key)
            used_keys.add(new_key)
            entry["ID"] = new_key
        warnings.append(decision)
        return make_row(entry, entry.get("ID", key), "corrected" if changes else "verified", best, original_doi, original_title, changes, warnings)
    if best:
        warnings.append(f"{decision}; no automatic modification.")
        return make_row(entry, key, "to review", best, original_doi, original_title, [], warnings)
    warnings.append("No match found by DOI or title.")
    return make_row(entry, key, "not found", None, original_doi, original_title, [], warnings)


# ============================================================================
# Decision Persistence
# ============================================================================

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

    def clear(self) -> None:
        self.decisions.clear()
        if self.path and self.path.exists():
            try:
                self.path.unlink()
            except Exception as exc:
                logging.warning("Unable to delete decisions file: %s", exc)

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


# ============================================================================
# Export Functions
# ============================================================================

def write_reports(rows: Sequence[ReportRow], csv_path: Path, md_path: Path) -> None:
    csv_fields = ["key", "entry_type", "status", "source", "confidence", "original_doi",
                  "resolved_doi", "title_score", "author_score", "year_score",
                  "changes", "warnings", "original_title", "resolved_title"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        for row in rows:
            d = {k: getattr(row, k, "") for k in csv_fields}
            writer.writerow(d)

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


def parse_bibtex_file(file_path: Path) -> Tuple[Optional[Any], Optional[str]]:
    """Reads and parses a BibTeX file safely against TeX/Unicode issues."""
    if not file_path.exists():
        return None, f"File not found: {file_path}"
    parser = BibTexParser(common_strings=True)
    parser.customization = lambda record: record
    try:
        text = file_path.read_text(encoding="utf-8-sig")
        original_combining = unicodedata.combining

        def safe_combining(value: str) -> int:
            if not isinstance(value, str) or len(value) != 1:
                return 0
            return original_combining(value)

        unicodedata.combining = safe_combining
        try:
            database = parser.parse(text)
            return database, None
        finally:
            unicodedata.combining = original_combining
    except Exception as exc:
        return None, f"BibTeX parsing error: {exc}"


def export_bibtex_file(database: Any, file_path: Path) -> None:
    writer = BibTexWriter()
    writer.indent = "  "
    writer.order_entries_by = None
    file_path.write_text(writer.write(database), encoding="utf-8")


def format_single_bibtex_entry(entry: Dict[str, Any]) -> str:
    """Generates the formatted BibTeX snippet for a single entry."""
    from bibtexparser.bibdatabase import BibDatabase
    db = BibDatabase()
    db.entries = [entry]
    writer = BibTexWriter()
    writer.indent = "  "
    return writer.write(db).strip()
