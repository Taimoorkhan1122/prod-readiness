#!/usr/bin/env python3
"""
evidence_scan.py - the "what exists" half of the evidence pass.

absence_probe.py answers "what did we look for and not find". This answers
"what is actually here": languages, dependency manifests with pinned versions,
entry points, datastore and infrastructure config, test and migration counts,
and data-growth signals. Seven lenses read this one file instead of each
running their own wholesale scan, which is what keeps the audit affordable and
keeps every lens reasoning about the same evidence body.

It never prints the contents of anything that looks like a credential - only
that the file exists and what kind it appears to be.

Usage:
    python3 evidence_scan.py <project_root> [--out DIR]
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

EXCLUDE_DIRS = {
    ".git", "node_modules", "vendor", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".next", ".nuxt", "out", "target", ".gradle", ".idea",
    ".vscode", "coverage", ".pytest_cache", ".mypy_cache", ".terraform",
    "bower_components", ".readiness-audit", ".security-audit", "Pods",
    ".turbo", ".svelte-kit", "storybook-static", ".cache",
}

SECRET_LIKE = re.compile(
    r"(^|/)(\.env(\..+)?|.*\.pem|.*\.key|.*\.p12|.*\.pfx|id_rsa|credentials\.json|"
    r".*service[-_]?account.*\.json)$", re.IGNORECASE)

IAC_PAT = re.compile(
    r"(\.tf$|\.tfvars$|\.hcl$|/k8s/|/kubernetes/|/helm/|/charts/|cloudformation|"
    r"pulumi\.|cdk\.json$|serverless\.ya?ml$|\.bicep$)", re.IGNORECASE)
CI_PAT = re.compile(
    r"(\.github/workflows/|\.gitlab-ci\.ya?ml$|bitbucket-pipelines\.ya?ml$|"
    r"Jenkinsfile$|\.circleci/|azure-pipelines\.ya?ml$|\.buildkite/)", re.IGNORECASE)
CONTAINER_PAT = re.compile(r"(dockerfile|docker-compose\.ya?ml$|\.dockerignore$)", re.IGNORECASE)
TEST_PAT = re.compile(
    r"(\.(spec|test)\.[jt]sx?$|(^|/)tests?/|(^|/)__tests__/|(^|/)test_[^/]+\.py$|"
    r"_test\.go$|Test\.java$|_spec\.rb$)", re.IGNORECASE)
MIGRATION_PAT = re.compile(r"((^|/)migrations?/|(^|/)db/migrate/|prisma/migrations/)", re.IGNORECASE)
DOC_PAT = re.compile(r"(readme|architecture|adr|runbook|onboarding|contributing)", re.IGNORECASE)

MANIFESTS = [
    "package.json", "requirements.txt", "pyproject.toml", "Pipfile", "go.mod",
    "Gemfile", "pom.xml", "build.gradle", "build.gradle.kts", "composer.json",
    "Cargo.toml", "*.csproj",
]

ENTRY_HINTS = re.compile(
    r"(main\.[jt]s$|index\.[jt]s$|app\.module\.ts$|server\.[jt]s$|main\.py$|"
    r"app\.py$|wsgi\.py$|asgi\.py$|main\.go$|Application\.java$|Program\.cs$)",
    re.IGNORECASE)

ROUTE_PAT = re.compile(
    r"(@(Get|Post|Put|Patch|Delete)\(|app\.(get|post|put|patch|delete)\(|"
    r"router\.(get|post|put|patch|delete)\(|@(app|router)\.(get|post|put|delete)\(|"
    r"export async function (GET|POST|PUT|PATCH|DELETE))")


def read(p: Path, limit=1_500_000):
    try:
        if p.stat().st_size > limit:
            return ""
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_package_json(text):
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return {}
    deps = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        deps.update(d.get(key) or {})
    return deps


def parse_requirements(text):
    deps = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"([A-Za-z0-9._\-\[\]]+)\s*([=<>~!]=?.*)?", line)
        if m:
            deps[m.group(1)] = (m.group(2) or "").strip() or "unpinned"
    return deps


def parse_go_mod(text):
    deps = {}
    for m in re.finditer(r"^\s*([\w./\-]+)\s+(v[\w.\-+]+)", text, re.MULTILINE):
        deps[m.group(1)] = m.group(2)
    return deps


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_root")
    ap.add_argument("--out")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    ext_counts = Counter()
    iac, ci, container, tests, migrations, docs, secretish, entries = [], [], [], [], [], [], [], []
    manifests = {}
    route_count = 0
    total_files = 0

    for p in root.rglob("*"):
        if p.is_dir() or any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        rel = p.relative_to(root).as_posix()
        total_files += 1
        ext_counts[p.suffix.lower() or "(none)"] += 1

        if IAC_PAT.search(rel):
            iac.append(rel)
        if CI_PAT.search(rel):
            ci.append(rel)
        if CONTAINER_PAT.search(rel):
            container.append(rel)
        if TEST_PAT.search(rel):
            tests.append(rel)
        if MIGRATION_PAT.search(rel):
            migrations.append(rel)
        if DOC_PAT.search(p.name) and p.suffix.lower() in (".md", ".mdx", ".rst", ".txt"):
            docs.append(rel)
        if SECRET_LIKE.search(rel):
            secretish.append(rel)  # path and kind only, never contents
        if ENTRY_HINTS.search(rel):
            entries.append(rel)

        if p.name in MANIFESTS or p.suffix == ".csproj":
            text = read(p)
            if p.name == "package.json":
                manifests[rel] = parse_package_json(text)
            elif p.name in ("requirements.txt", "Pipfile"):
                manifests[rel] = parse_requirements(text)
            elif p.name == "go.mod":
                manifests[rel] = parse_go_mod(text)
            else:
                manifests[rel] = {"_parsed": False, "_note": "manifest present, not parsed"}

        if p.suffix.lower() in (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rb"):
            route_count += len(ROUTE_PAT.findall(read(p, 400_000)))

    def cap(lst, n=40):
        return {"count": len(lst), "sample": sorted(lst)[:n],
                "truncated": len(lst) > n}

    inventory = {
        "schema": 1,
        "project_root": str(root),
        "total_files": total_files,
        "extensions_top": dict(ext_counts.most_common(20)),
        "manifests": manifests,
        "entry_points": cap(entries),
        "route_handler_count": route_count,
        "infrastructure_as_code": cap(iac),
        "ci_config": cap(ci),
        "container_config": cap(container),
        "test_files": cap(tests, 25),
        "migration_files": cap(migrations, 25),
        "documentation": cap(docs),
        "credential_shaped_files": cap(secretish),
        "_note": "credential_shaped_files lists paths and kinds only; contents are never read or reported.",
    }

    outdir = Path(args.out) if args.out else root / ".readiness-audit" / "evidence"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "inventory.json").write_text(
        json.dumps(inventory, indent=2) + "\n", encoding="utf-8"
    )

    print(json.dumps({
        "total_files": total_files,
        "route_handlers": route_count,
        "tests": inventory["test_files"]["count"],
        "migrations": inventory["migration_files"]["count"],
        "iac_files": inventory["infrastructure_as_code"]["count"],
        "ci_files": inventory["ci_config"]["count"],
        "credential_shaped_files": inventory["credential_shaped_files"]["count"],
        "written_to": str(outdir / "inventory.json"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
