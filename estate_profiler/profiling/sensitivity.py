"""Sensitivity detection: local heuristics with optional Cloud DLP escalation.

The local pass runs offline at zero API cost over every sampled file, which is
what makes an estate-wide sweep affordable. It is tuned to be *indicative*:
checksum-validated where a checksum exists (Luhn, Aadhaar Verhoeff, IBAN mod-97)
and context-boosted elsewhere, so the ranking is trustworthy even though
individual hits are not adjudicated.

Cloud DLP is then pointed only at the top-ranked files, which is where
high-confidence, auditable findings actually matter.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Detector definitions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Detector:
    name: str
    category: str          # pii | financial | health | credential | identifier
    pattern: re.Pattern
    weight: int            # contribution to the file's sensitivity score per unique hit
    validator: str | None = None
    # Nearby words that make a match far more likely to be real.
    context: tuple[str, ...] = ()


_CONTEXT_WINDOW = 60

DETECTORS: tuple[Detector, ...] = (
    Detector("email", "pii", re.compile(r"\b[\w.%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), 2),
    Detector("phone", "pii",
             re.compile(r"(?<![\d-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?![\d-])"), 2),
    Detector("us_ssn", "pii",
             re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), 12,
             context=("ssn", "social security", "taxpayer")),
    Detector("credit_card", "financial",
             re.compile(r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))[ -]?\d{4}[ -]?\d{4}[ -]?\d{1,4}\b"),
             12, validator="luhn", context=("card", "visa", "mastercard", "amex", "cvv", "expiry")),
    Detector("iban", "financial",
             re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), 8, validator="iban",
             context=("iban", "bank", "account", "swift", "bic")),
    Detector("us_bank_routing", "financial",
             re.compile(r"\b(?:0[0-9]|1[0-2]|2[1-9]|3[0-2]|6[1-9]|7[0-2]|80)\d{7}\b"), 5,
             validator="aba", context=("routing", "aba", "transit", "account")),
    Detector("india_pan", "identifier",
             re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), 8, context=("pan", "permanent account", "income tax")),
    Detector("india_aadhaar", "identifier",
             re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b"), 12, validator="verhoeff",
             context=("aadhaar", "aadhar", "uid", "uidai")),
    Detector("uk_nino", "identifier",
             re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]\b"), 8,
             context=("national insurance", "nino", "ni number")),
    Detector("passport", "identifier",
             re.compile(r"\b[A-Z]{1,2}\d{6,8}\b"), 3, context=("passport", "travel document", "mrz")),
    Detector("date_of_birth", "pii",
             re.compile(r"\b(?:0?[1-9]|[12]\d|3[01])[/-](?:0?[1-9]|1[0-2])[/-](?:19|20)\d{2}\b"), 2,
             context=("dob", "date of birth", "birth date", "born")),
    Detector("ip_address", "identifier",
             re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"), 1),
    Detector("aws_access_key", "credential",
             re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b"), 20),
    Detector("gcp_api_key", "credential", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 20),
    Detector("gcp_sa_key", "credential",
             re.compile(r'"type"\s*:\s*"service_account"'), 25),
    Detector("private_key", "credential",
             re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), 25),
    Detector("jwt", "credential",
             re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), 15),
    Detector("slack_token", "credential", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), 20),
    Detector("github_token", "credential", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36}\b"), 20),
    Detector("password_assignment", "credential",
             re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|client[_-]?secret)\b\s*[:=]\s*[\"']?[^\s\"',;]{6,}"),
             10),
    Detector("connection_string", "credential",
             re.compile(r"(?i)\b(?:mongodb|postgres(?:ql)?|mysql|redis|amqp)://[^\s:@]+:[^\s@]+@"), 18),
)

# PHI is signalled by vocabulary rather than a format, so it is scored
# separately: a density of clinical terms, not a per-match pattern.
_PHI_TERMS = (
    "diagnosis", "patient", "medical record", "mrn", "icd-10", "icd10", "prescription",
    "treatment plan", "clinical", "lab result", "blood pressure", "discharge summary",
    "health insurance", "medicaid", "medicare", "hipaa", "allergies", "dosage", "mg/dl",
)
_PHI_RE = re.compile("|".join(re.escape(t) for t in _PHI_TERMS), re.IGNORECASE)

# Contract/legal and HR vocabulary: not sensitive per se, but drives retention
# and access-control decisions, so the report surfaces it as a content class.
_CLASS_TERMS: dict[str, tuple[str, ...]] = {
    "contract": ("agreement", "hereby", "party of the first", "indemnif", "governing law",
                 "term and termination", "confidentiality agreement", "msa", "statement of work"),
    "hr": ("salary", "compensation", "performance review", "offer letter", "payroll",
           "employee id", "termination letter", "appraisal"),
    "finance": ("invoice", "purchase order", "remittance", "accounts payable", "balance sheet",
                "general ledger", "vat", "gst"),
    "legal_hold": ("litigation", "subpoena", "legal hold", "privileged", "attorney-client"),
}
_CLASS_RES = {k: re.compile("|".join(re.escape(t) for t in v), re.IGNORECASE) for k, v in _CLASS_TERMS.items()}


# --------------------------------------------------------------------------
# Checksum validators
# --------------------------------------------------------------------------


def _luhn(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if not 12 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _iban(value: str) -> bool:
    compact = re.sub(r"[^A-Z0-9]", "", value.upper())
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    try:
        return int(numeric) % 97 == 1
    except ValueError:
        return False


def _aba(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if len(digits) != 9:
        return False
    checksum = (
        3 * (digits[0] + digits[3] + digits[6])
        + 7 * (digits[1] + digits[4] + digits[7])
        + (digits[2] + digits[5] + digits[8])
    )
    return checksum % 10 == 0


_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6), (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4), (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2), (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _verhoeff(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if len(digits) != 12:
        return False
    check = 0
    for index, digit in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[index % 8][digit]]
    return check == 0


_VALIDATORS = {"luhn": _luhn, "iban": _iban, "aba": _aba, "verhoeff": _verhoeff}


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


@dataclass
class SensitivityResult:
    score: int = 0                                  # 0-100, higher = more sensitive
    level: str = "none"                             # none | low | medium | high | critical
    findings: dict[str, int] = field(default_factory=dict)   # detector -> unique match count
    categories: list[str] = field(default_factory=list)
    content_classes: list[str] = field(default_factory=list)
    dlp_infotypes: dict[str, int] = field(default_factory=dict)
    dlp_checked: bool = False

    @property
    def has_credentials(self) -> bool:
        return "credential" in self.categories


def _level_for(score: int) -> str:
    if score >= 70:
        return "critical"
    if score >= 40:
        return "high"
    if score >= 18:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def analyse(text: str, name: str = "") -> SensitivityResult:
    """Score one file's extracted text (plus its filename) for sensitive content."""
    result = SensitivityResult()
    if not text and not name:
        return result

    haystack = f"{name}\n{text}"
    lowered = haystack.lower()
    raw_score = 0.0
    categories: set[str] = set()

    for detector in DETECTORS:
        matches = detector.pattern.findall(haystack)
        if not matches:
            continue
        # findall returns tuples when the pattern has groups; normalise to strings.
        values = {m if isinstance(m, str) else "".join(m) for m in matches}

        if detector.validator:
            validator = _VALIDATORS[detector.validator]
            values = {v for v in values if validator(v)}
            if not values:
                continue

        if detector.context and not any(term in lowered for term in detector.context):
            # Format matched but nothing nearby corroborates it. Keep it at a
            # fraction of weight rather than dropping it outright.
            contribution = detector.weight * 0.25
        else:
            contribution = float(detector.weight)

        count = len(values)
        result.findings[detector.name] = count
        categories.add(detector.category)
        # Sub-linear in count: 500 emails is a mailing list, not 250x one email.
        raw_score += contribution * (1 + math.log10(count))

    phi_hits = len(set(m.lower() for m in _PHI_RE.findall(haystack)))
    if phi_hits >= 3:
        result.findings["phi_vocabulary"] = phi_hits
        categories.add("health")
        raw_score += 10 + 2 * phi_hits

    for label, pattern in _CLASS_RES.items():
        if len(set(m.lower() for m in pattern.findall(haystack))) >= 2:
            result.content_classes.append(label)
    if "legal_hold" in result.content_classes:
        raw_score += 8

    result.categories = sorted(categories)
    result.score = max(0, min(100, round(raw_score)))
    result.level = _level_for(result.score)
    return result


# --------------------------------------------------------------------------
# Cloud DLP escalation
# --------------------------------------------------------------------------

DLP_INFO_TYPES = (
    "PERSON_NAME", "EMAIL_ADDRESS", "PHONE_NUMBER", "STREET_ADDRESS",
    "US_SOCIAL_SECURITY_NUMBER", "CREDIT_CARD_NUMBER", "IBAN_CODE",
    "PASSPORT", "DATE_OF_BIRTH", "MEDICAL_RECORD_NUMBER",
    "INDIA_AADHAAR_INDIVIDUAL", "INDIA_PAN_INDIVIDUAL",
    "AUTH_TOKEN", "GCP_CREDENTIALS", "AWS_CREDENTIALS", "ENCRYPTION_KEY",
)

_DLP_MAX_CHARS = 100_000  # well under the inspect_content payload limit


def inspect_with_dlp(text: str, project_id: str) -> dict[str, int]:
    """Return {info_type: finding_count} from Cloud DLP, or {} if unavailable.

    Failures are swallowed on purpose: DLP is an enrichment, and an estate scan
    must not abort because one file tripped a quota or the API is disabled.
    """
    if not text.strip() or not project_id:
        return {}
    try:
        from google.cloud import dlp_v2
    except ImportError:
        return {}

    try:
        client = dlp_v2.DlpServiceClient()
        response = client.inspect_content(
            request={
                "parent": f"projects/{project_id}/locations/global",
                "inspect_config": {
                    "info_types": [{"name": t} for t in DLP_INFO_TYPES],
                    "min_likelihood": dlp_v2.Likelihood.POSSIBLE,
                    "include_quote": False,
                    "limits": {"max_findings_per_request": 200},
                },
                "item": {"value": text[:_DLP_MAX_CHARS]},
            }
        )
    except Exception:
        return {}

    counts: dict[str, int] = {}
    for finding in response.result.findings:
        key = finding.info_type.name
        counts[key] = counts.get(key, 0) + 1
    return counts


def merge_dlp(result: SensitivityResult, dlp_counts: dict[str, int]) -> SensitivityResult:
    """Fold DLP findings into a local result, raising the score where DLP agrees."""
    result.dlp_checked = True
    if not dlp_counts:
        return result
    result.dlp_infotypes = dlp_counts
    # DLP confirming anything at all is strong evidence; weight high-risk types more.
    high_risk = {
        "US_SOCIAL_SECURITY_NUMBER", "CREDIT_CARD_NUMBER", "IBAN_CODE", "PASSPORT",
        "MEDICAL_RECORD_NUMBER", "INDIA_AADHAAR_INDIVIDUAL", "INDIA_PAN_INDIVIDUAL",
        "AUTH_TOKEN", "GCP_CREDENTIALS", "AWS_CREDENTIALS", "ENCRYPTION_KEY",
    }
    bump = sum(12 if t in high_risk else 3 for t in dlp_counts)
    result.score = max(0, min(100, result.score + bump))
    result.level = _level_for(result.score)
    if any(t in high_risk for t in dlp_counts) and "pii" not in result.categories:
        result.categories = sorted(set(result.categories) | {"pii"})
    return result
