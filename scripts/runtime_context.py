#!/usr/bin/env python3
"""
runtime_context.py - the live target record and the read-only safety rails.

The audit may touch one live system. This module records which system that
is and enforces three rules around it.

1. Fail fast. A run with a missing or unreachable live target stops before
   any lens starts. A missing target means the runtime lens is skipped. An
   unreachable target means the run aborts with a clear message.
2. Reference only. The file stores the credential reference, such as a vault
   path or variable name. It never stores a value. The write path redacts
   values that look like a password, token, secret, or API key.
3. Read only. After one login POST, the network log shows reads only. A
   mutating control is inspected for presence and state. It is never
   activated.

Usage:
    python3 runtime_context.py init <project_root> --url URL \\
        --environment ENV --role ROLE --credential-ref REF [--scope-notes TEXT]
    python3 runtime_context.py check <project_root> [--timeout SECONDS]

All helpers are importable. The audit flow calls them from Stage 1 before
any lens starts. See context-intake.md for the intake procedure.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DIRNAME = ".readiness-audit"
FILENAME = "runtime-context.json"
INSPECTIONS_FILE = "runtime-inspections.json"

REDACTED = "[REDACTED]"

READ_METHODS = ("GET", "HEAD", "OPTIONS")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
LOGIN_URL_HINTS = (
    "login", "auth", "signin", "sign-in", "sign_in",
    "session", "token", "oauth", "sso", "authenticate",
)

_TIMEOUT_DEFAULT = 5


def _now():
    return datetime.now(timezone.utc).isoformat()


def _dir(root: Path) -> Path:
    return Path(root).expanduser().resolve() / DIRNAME


def _context_file(root: Path) -> Path:
    return _dir(root) / FILENAME


def _inspections_file(root: Path) -> Path:
    return _dir(root) / INSPECTIONS_FILE


# --------------------------------------------------------------------------
# Secret hygiene - reference only, never a value
# --------------------------------------------------------------------------

_SECRET_KEY_HINTS = (
    "password", "passwd", "pwd", "token", "secret",
    "api_key", "apikey", "api-key", "bearer", "authorization",
    "cookie", "session", "private_key", "privatekey",
    "client_secret", "access_key", "access_token", "refresh_token",
)

_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|pass|token|secret"
    r"|api[_\- ]?key|client[_\- ]?secret"
    r"|access[_\- ]?token|refresh[_\- ]?token"
    r"|auth[_\- ]?token|authorization|bearer)\b"
    r"[\"']?\s*[:=]\s*[\"']?([^\s,;\"'}]+)"
)

_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9\-._~+/=]+")

_KNOWN_PREFIX_RE = re.compile(
    r"\b(sk-[A-Za-z0-9\-_]{8,}"
    r"|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}"
    r"|xox[bpas]-[A-Za-z0-9\-_]{8,}"
    r"|AKIA[0-9A-Z]{16})\b"
)

_URL_PASSWORD_RE = re.compile(r"(?i)(https?://[^/\s:]+:)[^@\s/]+(@)")

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9\-_]*\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+")


def _is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def _looks_like_secret_value(text: str) -> bool:
    """Check if a bare string looks like a credential value.

    A reference names where the value lives, such as a vault path. A value
    is the secret itself. This check catches values that carry no key name,
    such as API prefixes, JWT blobs, and long random tokens.
    """
    s = str(text).strip()
    if not s:
        return False
    if _KNOWN_PREFIX_RE.search(s) or _JWT_RE.search(s):
        return True
    if re.fullmatch(r"[A-Za-z0-9\-_+/=]{32,}", s):
        has_letter = bool(re.search(r"[A-Za-z]", s))
        has_digit = bool(re.search(r"[0-9]", s))
        if has_letter and has_digit and "/" not in s and ":" not in s:
            return True
    return False


def redact_text(text: str) -> str:
    """Remove credential-looking values from one string.

    Each match keeps its key name and stores [REDACTED] as the value. Plain
    prose without a credential pattern passes through unchanged.
    """
    if text is None:
        return ""
    out = str(text)
    out = _SECRET_VALUE_RE.sub(lambda m: m.group(1) + ": " + REDACTED, out)
    out = _BEARER_RE.sub(lambda m: m.group(1) + " " + REDACTED, out)
    out = _KNOWN_PREFIX_RE.sub(REDACTED, out)
    out = _URL_PASSWORD_RE.sub(lambda m: m.group(1) + REDACTED + m.group(2), out)
    return out


def redact(value):
    """Remove credential-looking values from strings, lists, and dicts.

    Dict values under a secret-like key become [REDACTED]. Free text passes
    through redact_text. Other scalars pass through unchanged.
    """
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if _is_secret_key(key):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


# --------------------------------------------------------------------------
# Runtime context capture - URL, environment, role, reference, scope notes
# --------------------------------------------------------------------------

def set_runtime_context(root, url, environment, role, credential_ref,
                        scope_notes=""):
    """Record the live target the audit may touch.

    Store the credential reference only, such as a vault path. Store no
    value. The write path redacts secret-looking input before it reaches
    disk. Return the stored record.
    """
    base = Path(root).expanduser().resolve()
    cleaned_url = redact_text(str(url or "").strip())
    cleaned_env = redact_text(str(environment or "").strip())
    cleaned_role = redact_text(str(role or "").strip())
    cleaned_note = redact_text(str(scope_notes or "").strip())

    raw_ref = str(credential_ref or "").strip()
    if raw_ref and _looks_like_secret_value(raw_ref):
        cleaned_ref = REDACTED
    else:
        cleaned_ref = redact_text(raw_ref)

    record = {
        "url": cleaned_url,
        "environment": cleaned_env,
        "role": cleaned_role,
        "credential_ref": cleaned_ref,
        "credential_source": "reference",
        "scope_notes": cleaned_note,
        "updated_at": _now(),
    }
    target = _context_file(base)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def init_runtime_context(root, url, environment, role, credential_ref,
                         scope_notes=""):
    """Create the runtime context file. This is an alias for set_runtime_context."""
    return set_runtime_context(root, url, environment, role, credential_ref,
                               scope_notes)


def load_runtime_context(root):
    """Read the stored runtime context. Return None when no file exists."""
    path = _context_file(Path(root).expanduser().resolve())
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


# --------------------------------------------------------------------------
# Fail-fast gate - missing or unreachable target stops the run
# --------------------------------------------------------------------------

def verify_target_reachable(url, timeout=_TIMEOUT_DEFAULT):
    """Check that the live target answers within a short timeout.

    Any HTTP answer counts as reachable, even an error status. Only a
    missing URL, a bad URL, or a network failure counts as unreachable.
    Return (reachable, message).
    """
    text = str(url or "").strip()
    if not text:
        return (False, "Live target is missing: no URL is set. "
                       "Record the URL before any lens starts.")
    try:
        parts = urllib.parse.urlparse(text)
    except ValueError as exc:
        return (False, "Live target URL is not valid: %s. "
                       "Set an http or https URL before any lens starts." % exc)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return (False, "Live target URL %r is not valid: use an http or https "
                       "URL with a host name. Record the URL before any "
                       "lens starts." % text)
    request = urllib.request.Request(text, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return (True, "Live target %s is reachable." % text)
    except urllib.error.HTTPError as exc:
        return (True, "Live target %s answered with HTTP %s. "
                      "The target is reachable." % (text, exc.code))
    except Exception as exc:
        return (False, "Live target %s is unreachable: %s. Check the URL "
                       "and the network, then retry before any lens "
                       "starts." % (text, exc))


def check_runtime_ready(root, timeout=_TIMEOUT_DEFAULT):
    """Apply the fail-fast gate for the runtime lens.

    Return (ready, message). A missing target means the runtime lens is
    skipped. An unreachable target means the run aborts before lenses
    start. Call this before any lens starts.
    """
    base = Path(root).expanduser().resolve()
    path = _context_file(base)
    if not path.exists():
        return (False, "Live target is missing: no runtime context file "
                       "exists at .readiness-audit/runtime-context.json. "
                       "Record the live target before any lens starts. "
                       "If no live target exists, skip the runtime lens "
                       "and record the reason.")
    data = load_runtime_context(base)
    if not data:
        return (False, "Live target is missing: the runtime context file "
                       "is not valid. Record the live target again before "
                       "any lens starts. If no live target exists, skip "
                       "the runtime lens and record the reason.")
    url = str(data.get("url") or "").strip()
    if not url:
        return (False, "Live target is missing: the runtime context has "
                       "no URL. Set the URL before any lens starts. "
                       "If no live target exists, skip the runtime lens "
                       "and record the reason.")
    reachable, detail = verify_target_reachable(url, timeout=timeout)
    if not reachable:
        return (False, "%s Abort before lenses start." % detail)
    return (True, "Runtime target %s is ready. Lenses may start." % url)


# --------------------------------------------------------------------------
# Read-only proof - the network log shows reads and one login only
# --------------------------------------------------------------------------

def _is_login_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(hint in lowered for hint in LOGIN_URL_HINTS)


def is_read_only_log(entries):
    """Check that a network log shows reads and one login POST at most.

    Each entry needs a method and a URL. An entry may set login to true to
    mark the single login POST. A POST to a login URL also counts as the
    login. GET, HEAD, and OPTIONS always pass. Any other POST, PUT, PATCH,
    or DELETE is a violation. A second login POST is a violation.
    Return (is_read_only, violations).
    """
    if entries is None:
        return (False, ["Network log is missing: no entries were recorded."])
    if not isinstance(entries, list):
        return (False, ["Network log is not valid: entries must be a list."])
    violations = []
    login_seen = False
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            violations.append("Entry %d is not valid: each entry must state "
                              "a method and a URL." % index)
            continue
        method = str(entry.get("method") or "").strip().upper()
        url = str(entry.get("url") or "").strip()
        label = url or "(no URL)"
        if not method:
            violations.append("Entry %d has no HTTP method for %s. State "
                              "the method for each request." % (index, label))
            continue
        if method in READ_METHODS:
            continue
        if method == "POST" and (entry.get("login") is True
                                 or _is_login_url(url)):
            if login_seen:
                violations.append("Entry %d posts to %s, but a login POST "
                                  "was already used. Only one login POST "
                                  "is allowed." % (index, label))
            else:
                login_seen = True
            continue
        if method == "POST":
            violations.append("Entry %d posts to %s after login. Only reads "
                              "and one login POST are allowed. Remove the "
                              "write before the run passes." % (index, label))
        elif method in ("PUT", "PATCH", "DELETE"):
            violations.append("Entry %d uses %s on %s after login. Only "
                              "reads and one login POST are allowed. Remove "
                              "the write before the run passes."
                              % (index, method, label))
        else:
            violations.append("Entry %d uses method %s on %s. Only GET, "
                              "HEAD, OPTIONS, and one login POST are "
                              "allowed." % (index, method, label))
    return (len(violations) == 0, violations)


# --------------------------------------------------------------------------
# Mutating-control guard - inspect presence and state, activate nothing
# --------------------------------------------------------------------------

def mark_inspected_not_activated(control_id, state="present", note="", root=None):
    """Record inspection of a mutating control without activating it.

    This helper writes a record only. It sends no request, clicks no
    button, and changes no data. The record states presence and state and
    marks activated as false. Return the record.
    """
    cid = str(control_id or "").strip()
    if not cid:
        raise ValueError("Control id is missing: name the control inspected.")
    record = {
        "control_id": redact_text(cid),
        "state": redact_text(str(state or "present").strip() or "present"),
        "note": redact_text(str(note or "").strip()),
        "activated": False,
        "action": "inspected only",
        "updated_at": _now(),
    }
    if root is not None:
        base = Path(root).expanduser().resolve()
        path = _inspections_file(base)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = load_inspections(base)
        kept = [item for item in existing
                if item.get("control_id") != record["control_id"]]
        kept.append(record)
        path.write_text(json.dumps({"inspections": kept}, indent=2) + "\n",
                        encoding="utf-8")
    return record


def load_inspections(root):
    """Read stored control inspections. Return an empty list when none exist."""
    path = _inspections_file(Path(root).expanduser().resolve())
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    items = raw.get("inspections") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


# --------------------------------------------------------------------------
# Command line - init writes the context, check applies the gate
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Record the live target.")
    p_init.add_argument("project_root")
    p_init.add_argument("--url", required=True)
    p_init.add_argument("--environment", required=True)
    p_init.add_argument("--role", required=True)
    p_init.add_argument("--credential-ref", required=True)
    p_init.add_argument("--scope-notes", default="")

    p_check = sub.add_parser("check", help="Apply the fail-fast gate.")
    p_check.add_argument("project_root")
    p_check.add_argument("--timeout", type=float, default=_TIMEOUT_DEFAULT)

    args = parser.parse_args(argv)
    root = Path(args.project_root).expanduser().resolve()
    if args.cmd == "init":
        record = set_runtime_context(
            root, args.url, args.environment, args.role,
            args.credential_ref, args.scope_notes)
        print(json.dumps({"recorded": True, "record": record}, indent=2))
        return 0
    ready, message = check_runtime_ready(root, timeout=args.timeout)
    print(json.dumps({"ready": ready, "message": message}, indent=2))
    if not ready:
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
