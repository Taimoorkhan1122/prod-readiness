#!/usr/bin/env python3
"""
absence_probe.py - turn "I looked for X and did not find it" into a citable fact.

The most dangerous claim an audit can make is a confident absence. A model asked
to find what is missing will happily assert that a system has no rate limiting
when it simply did not grep for the right thing. This script does that grepping
deterministically: for every expected control it records the patterns searched,
how many files matched, and where. A lens agent may only write a [NOT FOUND]
finding by citing a ledger row whose hit count is zero.

It also decides, per control, whether a zero-hit result *should* be reported as
NOT FOUND or as UNVERIFIED. Controls that normally live outside a source
repository (backups, PITR, alert routing) default to UNVERIFIED - unless the
repo ships infrastructure-as-code, in which case the repo does cover them and a
miss becomes a real NOT FOUND. That single rule prevents most over-claiming.

Usage:
    python3 absence_probe.py <project_root> [--out DIR] [--json-only]

Writes <project_root>/.readiness-audit/evidence/absence-ledger.{json,md}
unless --out is given. Prints a short summary to stdout.
"""
import argparse
import json
import re
import sys
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 20000
MAX_HITS_RECORDED = 8

EXCLUDE_DIRS = {
    ".git", "node_modules", "vendor", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".next", ".nuxt", "out", "target", ".gradle", ".idea",
    ".vscode", "coverage", ".pytest_cache", ".mypy_cache", ".terraform",
    "bower_components", ".readiness-audit", ".security-audit", "Pods",
    ".turbo", ".svelte-kit", "storybook-static", ".cache",
}

TEXT_SUFFIXES = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".py", ".go", ".rb", ".java", ".kt", ".kts", ".php", ".cs", ".rs", ".scala",
    ".sql", ".prisma", ".graphql",
    ".yml", ".yaml", ".json", ".toml", ".ini", ".conf", ".cfg", ".properties",
    ".tf", ".tfvars", ".hcl", ".bicep",
    ".sh", ".bash", ".zsh", ".ps1",
    ".md", ".mdx", ".txt", ".xml", ".gradle", ".env", ".example", ".sample",
}

TEXT_NAMES = {
    "dockerfile", "makefile", "procfile", "jenkinsfile", "caddyfile",
    "docker-compose.yml", "docker-compose.yaml", ".env", ".env.example",
    ".dockerignore", ".gitignore", ".nvmrc", ".tool-versions",
}


def _is_texty(p: Path) -> bool:
    if p.suffix.lower() in TEXT_SUFFIXES:
        return True
    n = p.name.lower()
    if n in TEXT_NAMES or n.startswith("dockerfile") or n.startswith(".env"):
        return True
    return False


def collect(root: Path):
    """One walk, one read. Every control is evaluated against this corpus."""
    files = []
    truncated = False
    for p in root.rglob("*"):
        if len(files) >= MAX_FILES:
            truncated = True
            break
        if p.is_dir():
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if not _is_texty(p):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            text = p.read_text(errors="replace")
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        files.append((rel, text, text.lower()))
    return files, truncated


# ---------------------------------------------------------------------------
# Control registry.
#
#   polarity "control" -> we expect this to exist; zero hits is a candidate
#                         finding.
#   polarity "sink"    -> we expect this NOT to exist, or to exist only with
#                         guards; hits are what the lens must go and read.
#   scope "repo"       -> a source repository is the right place to find it, so
#                         zero hits supports [NOT FOUND].
#   scope "infra"      -> normally configured outside the repo, so zero hits
#                         supports [UNVERIFIED] - promoted to [NOT FOUND] when
#                         the repo does ship IaC.
# ---------------------------------------------------------------------------
C = lambda i, lens, label, content=(), paths=(), polarity="control", scope="repo", \
      signal=False, requires=None: {
    "id": i, "lens": lens, "label": label, "content": list(content),
    "paths": list(paths), "polarity": polarity, "scope": scope,
    # signal: existence tells us which branch of the audit applies; absence is
    # not itself a defect (no frontend is not a missing frontend).
    "signal": signal,
    # requires: this control only makes sense when another one is present. No
    # broker means a missing dead-letter queue is not a finding, it is a
    # category that does not apply - which is what stops the report filling up
    # with demands for machinery the system does not use.
    "requires": requires,
}

CONTROLS = [
    # ---- security -------------------------------------------------------
    C("rate_limiting", "security", "Request rate limiting / throttling",
      [r"rate[-_ ]?limit", r"\bthrottler?\b", r"express-rate-limit", r"slowapi",
       r"@nestjs/throttler", r"limiter\.", r"ratelimit"]),
    C("security_headers", "security", "Security response headers (helmet/CSP/HSTS)",
      [r"\bhelmet\b", r"content-security-policy", r"strict-transport-security",
       r"x-frame-options", r"securityheaders"]),
    C("csrf_protection", "security", "CSRF protection",
      [r"\bcsrf\b", r"xsrf", r"samesite\s*[:=]"]),
    C("input_validation", "security", "Server-side input validation / schema parsing",
      [r"class-validator", r"\bzod\b", r"joi\.", r"yup\.", r"pydantic",
       r"marshmallow", r"validationpipe", r"express-validator", r"@isstring",
       r"jsonschema"]),
    C("authn", "security", "Authentication implementation",
      [r"passport", r"jsonwebtoken", r"\bjwt\b", r"next-auth", r"authguard",
       r"oauth", r"session\(", r"bcrypt", r"argon2", r"clerk", r"supabase\.auth"]),
    C("authz", "security", "Authorization / permission checks distinct from authn",
      [r"canactivate", r"\brbac\b", r"\bcasl\b", r"permission", r"\brole[s]?guard",
       r"authoriz", r"@roles?\(", r"policy"]),
    C("token_expiry", "security", "Token expiry / rotation / revocation config",
      [r"expiresin", r"refresh[-_ ]?token", r"token.{0,12}revok", r"blacklist.{0,10}token",
       r"maxage"]),
    C("tenant_scoping", "security", "Explicit tenant/org scoping on data access",
      [r"tenant[_ ]?id", r"organi[sz]ation[_ ]?id", r"workspace[_ ]?id",
       r"account[_ ]?id\b", r"row level security", r"set_config\('app"]),
    C("secrets_committed", "security", "Committed secret-bearing files",
      paths=[r"(^|/)\.env$", r"(^|/)\.env\.(local|prod|production|staging)$",
             r"(^|/)credentials\.json$", r"(^|/)(id_rsa|.*\.pem|.*\.p12|.*\.pfx)$",
             r"serviceaccount.*\.json$"],
      polarity="sink"),
    C("secrets_manager", "security", "Managed secret store integration",
      [r"secretsmanager", r"parameter ?store", r"\bvault\b", r"key ?vault",
       r"doppler", r"sops", r"gcp.{0,10}secret", r"1password"], scope="infra"),
    C("cors_config", "security", "Explicit CORS configuration",
      [r"enablecors", r"\bcors\(", r"access-control-allow-origin",
       r"allowed_origins", r"corsoptions"]),
    C("audit_logging", "security", "Audit trail of security-relevant actions",
      [r"audit[_ ]?log", r"auditlog", r"activity[_ ]?log", r"security[_ ]?event"]),
    C("account_lockout", "security", "Brute-force lockout / login attempt limiting",
      [r"lockout", r"failed[_ ]?login", r"login[_ ]?attempt", r"max[_ ]?attempts"]),
    C("dependency_scanning", "security", "Dependency vulnerability scanning",
      [r"npm audit", r"yarn audit", r"pnpm audit", r"snyk", r"dependabot",
       r"trivy", r"\bgrype\b", r"safety check", r"osv-scanner", r"renovate"],
      paths=[r"\.github/dependabot\.ya?ml", r"renovate\.json"]),
    C("encryption_at_rest", "security", "Encryption at rest for stored data",
      [r"encrypt.{0,12}at.{0,4}rest", r"\bkms\b", r"pgcrypto", r"storage_encrypted",
       r"field.{0,10}encrypt"], scope="infra"),
    # sinks
    C("ssrf_url_fetch", "security", "Backend fetch of user-influenced URLs (SSRF sink)",
      [r"(axios|fetch|request|httpx|requests)\.(get|post|request)\s*\(\s*[a-z_]*url",
       r"urllib\.request\.urlopen", r"http\.get\(\s*[a-z_]*url",
       r"new url\(\s*(req|request|body|query|params)"], polarity="sink"),
    C("path_traversal_sink", "security", "File path built from request input (traversal sink)",
      [r"path\.join\([^)]*\b(req|request|params|query|body|filename)\b",
       r"readfile(sync)?\([^)]*\b(req|request|params|query|body)\b",
       r"sendfile\(", r"os\.path\.join\([^)]*request"], polarity="sink"),
    C("raw_sql_concat", "security", "String-built SQL (injection sink)",
      [r"\b(select|insert into|update|delete from)\b[^;]{0,200}\$\{",
       r"f[\"'][^\"']{0,120}\b(select|insert|update|delete)\b[^\"']{0,120}\{",
       r"(query|execute|raw)\s*\(\s*[`\"'][^`\"']*(select|insert|update|delete)[^`\"']*[`\"']\s*\+",
       r"createquerybuilder\([^)]*\)\.where\([`\"'][^`\"']*\$\{"], polarity="sink"),
    C("open_redirect_sink", "security", "Redirect target from request input",
      [r"redirect\(\s*(req|request)\.(query|body|params)",
       r"res\.redirect\([^)]*\b(url|next|return_?to|redirect_?uri)\b"], polarity="sink"),

    # ---- backend --------------------------------------------------------
    C("external_call_timeout", "backend", "Timeouts on outbound calls",
      [r"timeout\s*[:=]", r"abortsignal\.timeout", r"request_?timeout",
       r"connecttimeout", r"deadline"]),
    C("retry_policy", "backend", "Retry policy on external calls",
      [r"\bretr(y|ies)\b", r"axios-retry", r"backoff", r"tenacity", r"p-retry",
       r"maxattempts"]),
    C("circuit_breaker", "backend", "Circuit breaker around external dependencies",
      [r"circuit[_ ]?breaker", r"opossum", r"\bhystrix\b", r"resilience4j",
       r"pybreaker"]),
    C("idempotency", "backend", "Idempotency keys on write operations",
      [r"idempotenc", r"idempotency[-_ ]?key", r"dedup(e|lication)?[_ ]?key",
       r"request[_ ]?id.{0,20}unique"]),
    C("message_broker", "backend", "Message broker / queue / pub-sub",
      [r"\bbullmq\b", r"\bbull\b", r"rabbitmq", r"amqplib", r"\bkafka\b",
       r"\bsqs\b", r"\bsns\b", r"pubsub", r"celery", r"sidekiq", r"nats",
       r"@nestjs/bull", r"redis.{0,10}stream"]),
    C("dead_letter_queue", "backend", "Dead-letter queue / poison message handling",
      [r"dead[-_ ]?letter", r"\bdlq\b", r"failedqueue", r"redrive"]),
    C("event_schema_versioning", "backend", "Event schema versioning / registry",
      [r"schema[_ ]?registry", r"avro", r"event[_ ]?version", r"\bcloudevents\b",
       r"\"version\"\s*:\s*\"?\d.*event"]),
    C("consumer_lag_monitoring", "backend", "Consumer lag / queue depth observability",
      [r"consumer[_ ]?lag", r"queue[_ ]?depth", r"backlog.{0,10}metric",
       r"getjobcounts", r"waiting.{0,10}count"], scope="infra"),
    C("caching_layer", "backend", "Caching layer",
      [r"cache[-_ ]?manager", r"\bredis\b", r"memcached", r"@cacheable",
       r"cacheinterceptor", r"unstable_cache", r"revalidate"]),
    C("cache_invalidation", "backend", "Explicit cache invalidation / TTL policy",
      [r"cache.{0,10}(del|evict|invalidat|purge)", r"\bttl\b", r"revalidatetag",
       r"expire\("]),
    C("cache_stampede_guard", "backend", "Single-flight / jitter protection on cache misses",
      [r"single[-_ ]?flight", r"stampede", r"mutex.{0,15}cache", r"jitter",
       r"lock.{0,10}(acquire|redlock)"]),
    C("graceful_shutdown", "backend", "Graceful shutdown / drain handling",
      [r"enableshutdownhooks", r"sigterm", r"beforeexit", r"graceful.{0,10}shutdown",
       r"onmoduledestroy", r"lifespan"]),
    C("api_versioning", "backend", "API versioning strategy",
      [r"enableversioning", r"/v[12]/", r"api[-_ ]?version", r"accept-version"]),
    C("feature_flags", "backend", "Feature flags / kill switches",
      [r"feature[_ ]?flag", r"launchdarkly", r"unleash", r"posthog.{0,10}flag",
       r"flagsmith", r"is_enabled\("]),
    C("health_endpoint", "backend", "Health / readiness endpoint",
      [r"/health", r"/healthz", r"/readyz", r"/livez", r"terminus", r"healthcheck"]),

    # ---- frontend -------------------------------------------------------
    C("frontend_present", "frontend", "Frontend application present",
      [r"\breact\b", r"\bvue\b", r"\bsvelte\b", r"\bangular\b", r"next\.config",
       r"\"react-dom\""],
      paths=[r"\.(tsx|jsx|vue|svelte)$", r"(^|/)index\.html$"]),
    C("error_boundary", "frontend", "Error boundary / global UI error handling",
      [r"errorboundary", r"componentdidcatch", r"error\.tsx", r"global-error",
       r"onerrorcaptured"]),
    C("loading_empty_states", "frontend", "Loading / empty state handling",
      [r"isloading", r"ispending", r"\bskeleton\b", r"suspense", r"loading\.tsx",
       r"emptystate"]),
    C("offline_handling", "frontend", "Offline / network-failure handling",
      [r"navigator\.online", r"\boffline\b", r"service ?worker", r"workbox"]),
    C("a11y_tooling", "frontend", "Accessibility tooling in the repo",
      [r"eslint-plugin-jsx-a11y", r"\baxe-core\b", r"@axe-core", r"lighthouse",
       r"pa11y", r"jest-axe"]),
    C("cross_browser_testing", "frontend", "Cross-browser / device test config",
      [r"browsers\s*:\s*\[", r"webkit", r"firefox", r"browserslist",
       r"devices\[", r"projects\s*:\s*\["],
      paths=[r"playwright\.config\.[jt]s", r"browserslistrc"]),
    C("client_storage_sensitive", "frontend", "Sensitive data in browser storage (sink)",
      [r"localstorage\.setitem\([^)]*(token|jwt|secret|password|key)",
       r"sessionstorage\.setitem\([^)]*(token|jwt|secret|password)",
       r"document\.cookie\s*="], polarity="sink"),

    # ---- devops ---------------------------------------------------------
    C("iac", "devops", "Infrastructure as code",
      [r"resource\s+\"aws_", r"apiversion:\s*apps/", r"awstemplateformatversion"],
      paths=[r"\.tf$", r"\.tfvars$", r"(^|/)k8s/", r"(^|/)kubernetes/",
             r"(^|/)helm/", r"(^|/)charts/", r"cloudformation", r"(^|/)pulumi\.",
             r"(^|/)cdk\.json$", r"serverless\.ya?ml$"]),
    C("ci_pipeline", "devops", "CI pipeline definition",
      paths=[r"\.github/workflows/.*\.ya?ml$", r"\.gitlab-ci\.ya?ml$",
             r"(^|/)bitbucket-pipelines\.ya?ml$", r"(^|/)Jenkinsfile$",
             r"\.circleci/config\.ya?ml$", r"azure-pipelines\.ya?ml$",
             r"\.buildkite/"]),
    C("tests_in_ci", "devops", "Tests wired into CI",
      [r"run:\s*.*\b(npm|yarn|pnpm|pytest|go test|mvn|gradle).*\btest\b",
       r"script:\s*.*test"]),
    C("deploy_automation", "devops", "Automated deploy step",
      [r"\bdeploy\b", r"kubectl apply", r"helm upgrade", r"terraform apply",
       r"flyctl deploy", r"vercel deploy", r"eb deploy", r"argocd"]),
    C("rollback_path", "devops", "Documented or automated rollback",
      [r"\brollback\b", r"helm rollback", r"kubectl rollout undo", r"revert.{0,10}deploy",
       r"blue[-_ ]?green", r"canary"], scope="infra"),
    C("post_deploy_smoke", "devops", "Post-deploy smoke verification",
      [r"smoke[-_ ]?test", r"post[-_ ]?deploy", r"health.{0,10}check.{0,15}after",
       r"verify.{0,10}deployment"], scope="infra"),
    C("container_build", "devops", "Container build definition",
      paths=[r"(^|/)dockerfile", r"docker-compose\.ya?ml$"]),
    C("container_nonroot", "devops", "Container runs as non-root",
      [r"^\s*user\s+(?!root)\S+", r"runasnonroot", r"runasuser"]),
    C("container_pinned_base", "devops", "Base image pinned by digest",
      [r"^\s*from\s+\S+@sha256:"]),
    C("resource_limits", "devops", "Container CPU/memory limits",
      [r"resources:\s*\n\s*limits", r"mem_limit", r"cpus:", r"memory:\s*\"?\d"]),
    C("liveness_readiness_probes", "devops", "Liveness/readiness probes",
      [r"livenessprobe", r"readinessprobe", r"startupprobe", r"healthcheck:"]),
    C("structured_logging", "devops", "Structured logging",
      [r"\bpino\b", r"winston", r"structlog", r"zerolog", r"logrus",
       r"json.{0,10}logger", r"logger\.(info|warn|error)\(\s*\{"]),
    C("metrics", "devops", "Application metrics emission",
      [r"prom-client", r"prometheus", r"statsd", r"opentelemetry", r"datadog",
       r"micrometer", r"/metrics"], scope="infra"),
    C("tracing", "devops", "Distributed tracing",
      [r"opentelemetry", r"\bjaeger\b", r"\bzipkin\b", r"traceparent", r"\bsentry\b"],
      scope="infra"),
    C("alerting", "devops", "Alert rules / on-call routing",
      [r"alertmanager", r"pagerduty", r"opsgenie", r"alert.{0,10}rule",
       r"slo|error[_ ]?budget"], scope="infra"),
    C("env_config_template", "devops", "Externalised config template",
      paths=[r"\.env\.example$", r"\.env\.sample$", r"(^|/)env\.example",
             r"(^|/)config\.example"]),
    C("runbook", "devops", "Runbook / operational documentation",
      [r"\brunbook\b", r"on[-_ ]?call", r"incident.{0,10}response", r"\bpostmortem\b"],
      paths=[r"(^|/)docs?/.*(runbook|ops|incident)"]),

    # ---- qa -------------------------------------------------------------
    C("test_framework", "qa", "Test framework configured",
      [r"\bjest\b", r"vitest", r"\bmocha\b", r"pytest", r"\bunittest\b",
       r"testing-library", r"go test", r"junit", r"rspec"],
      paths=[r"jest\.config", r"vitest\.config", r"pytest\.ini", r"(^|/)tox\.ini$"]),
    C("test_files", "qa", "Test files present",
      paths=[r"\.(spec|test)\.[jt]sx?$", r"(^|/)tests?/", r"(^|/)__tests__/",
             r"(^|/)test_[^/]+\.py$", r"_test\.go$", r"Test\.java$"]),
    C("e2e_tests", "qa", "End-to-end tests",
      [r"playwright", r"cypress", r"puppeteer", r"selenium", r"testcafe"],
      paths=[r"(^|/)e2e/", r"cypress\.config"]),
    C("authz_boundary_tests", "qa", "Authorization boundary tests",
      [r"(describe|test|it)\([^)]*\b(403|forbidden|unauthori[sz]ed|permission|other tenant|cross[- ]tenant)\b"]),
    C("load_testing", "qa", "Load / performance testing",
      [r"\bk6\b", r"\blocust\b", r"artillery", r"\bjmeter\b", r"gatling",
       r"autocannon"]),
    C("coverage_config", "qa", "Coverage measurement configured",
      [r"collectcoverage", r"coveragethreshold", r"--cov", r"nyc", r"codecov",
       r"coveralls"]),
    C("synthetic_test_data", "qa", "Synthetic test data generation",
      [r"\bfaker\b", r"factory[-_ ]?bot", r"factory_boy", r"fishery", r"\bmirage\b",
       r"seed.{0,10}(data|script)"]),
    C("pii_in_fixtures", "qa", "Real-looking PII in fixtures or dumps (sink)",
      [r"@(gmail|yahoo|hotmail|outlook)\.com",
       r"\b\d{3}-\d{2}-\d{4}\b",
       r"\b4[0-9]{12}(?:[0-9]{3})?\b"],
      paths=[r"(^|/)(fixtures?|seeds?|dumps?|testdata)/"], polarity="sink"),
    C("prod_creds_in_test", "qa", "Production-looking credentials in test config (sink)",
      [r"(prod|production)[_-]?(url|host|key|token|password)\s*[:=]",
       r"sk_live_", r"pk_live_", r"rk_live_"], polarity="sink"),

    # ---- database -------------------------------------------------------
    C("migrations", "database", "Schema migrations",
      [r"migration", r"alembic", r"knex", r"flyway", r"liquibase", r"goose"],
      paths=[r"(^|/)migrations?/", r"(^|/)db/migrate/", r"(^|/)prisma/migrations/"]),
    C("reversible_migrations", "database", "Down / reversible migrations",
      [r"\bpublic async down\b", r"def downgrade", r"\.down\s*=", r"exports\.down",
       r"-- ?\+goose down", r"<!-- ?rollback"]),
    C("index_definitions", "database", "Explicit index definitions",
      [r"create index", r"@index\(", r"@@index\(", r"addindex", r"db_index=true",
       r"createindex"]),
    C("foreign_keys", "database", "Foreign key constraints",
      [r"foreign key", r"references\s+\w+\s*\(", r"@manytoone", r"@joincolumn",
       r"on delete", r"forcign", r"ondelete"]),
    C("connection_pooling", "database", "Connection pool configuration",
      [r"pool\s*[:=]", r"max.{0,5}connections", r"pgbouncer", r"poolsize",
       r"connection[_ ]?limit"]),
    C("query_timeout", "database", "Statement / query timeout",
      [r"statement_timeout", r"query[_ ]?timeout", r"lock_timeout",
       r"maxquerytime"]),
    C("transaction_boundaries", "database", "Explicit transaction boundaries",
      [r"begin transaction", r"\$transaction", r"transaction\(", r"@transactional",
       r"withtransaction", r"session\.begin"]),
    C("soft_delete", "database", "Soft delete columns",
      [r"deleted_?at", r"is_?deleted", r"@deletedatecolumn", r"softdelete",
       r"archived_?at"]),
    C("soft_delete_purge", "database", "Purge job for soft-deleted rows",
      [r"purge", r"hard[-_ ]?delete", r"cleanup.{0,15}(deleted|expired)",
       r"vacuum.{0,10}job"]),
    C("backup_config", "database", "Backup configuration",
      [r"\bbackup\b", r"pg_dump", r"mysqldump", r"snapshot", r"backup_retention"],
      scope="infra"),
    C("pitr", "database", "Point-in-time recovery",
      [r"point[-_ ]?in[-_ ]?time", r"\bpitr\b", r"wal[-_ ]?archiv", r"binlog",
       r"continuous.{0,10}backup"], scope="infra"),
    C("restore_drill", "database", "Evidence of a tested restore",
      [r"restore.{0,15}(drill|test|verif|rehears)", r"pg_restore",
       r"disaster[-_ ]?recovery"], scope="infra"),
    C("retention_policy", "database", "Data retention / deletion policy",
      [r"retention", r"\bgdpr\b", r"right to be forgotten", r"data[_ ]?deletion",
       r"anonymi[sz]e"]),
    C("archival_strategy", "database", "Archival / partitioning for cold data",
      [r"partition by", r"create table.{0,30}partition", r"archive[_ ]?table",
       r"cold[_ ]?storage", r"glacier"]),
    C("object_storage_lifecycle", "database", "Object storage lifecycle rules",
      [r"lifecycle_rule", r"lifecycle_configuration", r"expiration\s*\{",
       r"transition.{0,10}storage_class"], scope="infra"),
    C("slow_query_logging", "database", "Slow query logging",
      [r"slow[_ ]?quer", r"log_min_duration", r"long_query_time",
       r"maxquerytime"], scope="infra"),

    # ---- ai-security ----------------------------------------------------
    C("llm_sdk", "ai-security", "LLM / model provider SDK",
      [r"@anthropic-ai", r"\bopenai\b", r"langchain", r"llamaindex",
       r"@google/generative-ai", r"bedrock-runtime", r"huggingface",
       r"ollama", r"litellm", r"vercel/ai"]),
    C("prompt_templates", "ai-security", "Prompt construction sites",
      [r"system[_ ]?prompt", r"\bmessages\s*:\s*\[", r"chatprompttemplate",
       r"role:\s*[\"']system"]),
    C("model_pinning", "ai-security", "Pinned model identifiers",
      [r"claude-[a-z0-9.\-]+", r"gpt-[0-9][a-z0-9.\-]*", r"gemini-[a-z0-9.\-]+",
       r"model\s*[:=]\s*[\"'][a-z0-9][^\"']{4,}"]),
    C("llm_token_limits", "ai-security", "Token / output limits on model calls",
      [r"max_?tokens", r"max_output_tokens", r"maxtokens"]),
    C("llm_cost_controls", "ai-security", "Cost or usage controls on inference",
      [r"token.{0,10}(budget|quota|usage|count)", r"cost.{0,10}(limit|cap|track)",
       r"spend.{0,10}limit"]),
    C("llm_output_validation", "ai-security", "Validation of model output before use",
      [r"parse.{0,10}(response|completion|output)", r"safeparse",
       r"guardrail", r"sanitiz.{0,15}(output|response)", r"json\.parse\(.{0,30}completion"]),
    C("llm_tool_calling", "ai-security", "Model-driven tool / function calling (sink)",
      [r"tool_?choice", r"function_?call", r"\btools\s*:\s*\[", r"tool_use",
       r"agentexecutor"], polarity="sink"),
    C("llm_human_in_loop", "ai-security", "Human approval gate on model-triggered actions",
      [r"human[-_ ]?in[-_ ]?the[-_ ]?loop", r"require.{0,10}approval",
       r"confirm.{0,15}before", r"pending[_ ]?approval"]),
]


# Existence tells us which branch of the audit applies; absence is not itself a
# defect. "No frontend found" is not a missing frontend.
SIGNAL_ONLY = {
    "frontend_present", "llm_sdk", "message_broker", "caching_layer",
    "container_build", "soft_delete",
}

# A control that only makes sense when something else is present. Without a
# broker, a missing dead-letter queue is not a gap - it is a category that does
# not apply. This is what stops the report demanding machinery the system has
# no use for, which is the fastest way to get a whole audit ignored.
REQUIRES = {
    "dead_letter_queue": "message_broker",
    "event_schema_versioning": "message_broker",
    "consumer_lag_monitoring": "message_broker",
    "cache_invalidation": "caching_layer",
    "cache_stampede_guard": "caching_layer",
    "container_nonroot": "container_build",
    "container_pinned_base": "container_build",
    "resource_limits": "container_build",
    "liveness_readiness_probes": "container_build",
    "error_boundary": "frontend_present",
    "loading_empty_states": "frontend_present",
    "offline_handling": "frontend_present",
    "a11y_tooling": "frontend_present",
    "cross_browser_testing": "frontend_present",
    "client_storage_sensitive": "frontend_present",
    "prompt_templates": "llm_sdk",
    "model_pinning": "llm_sdk",
    "llm_token_limits": "llm_sdk",
    "llm_cost_controls": "llm_sdk",
    "llm_output_validation": "llm_sdk",
    "llm_human_in_loop": "llm_sdk",
    "llm_tool_calling": "llm_sdk",
    "reversible_migrations": "migrations",
    "soft_delete_purge": "soft_delete",
    "tests_in_ci": "ci_pipeline",
    "e2e_tests": "test_files",
    "authz_boundary_tests": "test_files",
    "coverage_config": "test_files",
    "synthetic_test_data": "test_files",
}


def compile_controls():
    for c in CONTROLS:
        c["signal"] = c["id"] in SIGNAL_ONLY
        c["requires"] = REQUIRES.get(c["id"])
        c["_content"] = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in c["content"]]
        c["_paths"] = [re.compile(p, re.IGNORECASE) for p in c["paths"]]
    return CONTROLS


def evaluate(controls, files):
    results = {}
    for c in controls:
        hits = []
        for rel, text, lower in files:
            matched_by = None
            for rx in c["_paths"]:
                if rx.search(rel):
                    matched_by = "path"
                    break
            if not matched_by:
                for rx in c["_content"]:
                    m = rx.search(text)
                    if m:
                        matched_by = "content"
                        break
            if matched_by:
                if len(hits) < MAX_HITS_RECORDED:
                    hits.append({"path": rel, "matched_by": matched_by})
                else:
                    hits.append(None)  # counted, not recorded
        recorded = [h for h in hits if h]
        results[c["id"]] = {
            "id": c["id"],
            "lens": c["lens"],
            "label": c["label"],
            "polarity": c["polarity"],
            "scope": c["scope"],
            "signal": c["signal"],
            "requires": c["requires"],
            "patterns_searched": c["content"] + c["paths"],
            "hit_count": len(hits),
            "hits": recorded,
            "hits_truncated": len(hits) > len(recorded),
        }
    return results


def verdicts(results, iac_present: bool):
    """Turn raw hit counts into the evidence state a lens is allowed to claim."""
    for r in results.values():
        n = r["hit_count"]
        dep = r.get("requires")
        if dep and results.get(dep, {}).get("hit_count", 0) == 0 and n == 0:
            r["verdict"] = "NOT_APPLICABLE"
            r["supports_state"] = "none"
            r["note"] = (f"Depends on `{dep}`, which is not present, so this control has "
                         "nothing to apply to. Not a gap.")
            continue
        if r.get("signal") and n == 0:
            r["verdict"] = "NO_SIGNAL_IN_SCOPE"
            r["supports_state"] = "none"
            r["note"] = ("Branch selector, not a control. Absence means this part of the "
                         "audit does not apply; it is not a finding.")
            continue
        if r["polarity"] == "sink":
            r["verdict"] = "SINK_PRESENT" if n else "NO_SINK_FOUND"
            r["supports_state"] = "CONFIRMED-candidate" if n else "none"
            r["note"] = ("Hits are code to read, not a finding by themselves."
                         if n else "No sink of this shape in scope.")
            continue
        if n:
            r["verdict"] = "SIGNAL_PRESENT"
            r["supports_state"] = "none"
            r["note"] = "Something matching this control exists; the lens must judge whether it is adequate, not whether it exists."
        elif r["scope"] == "repo":
            r["verdict"] = "NO_SIGNAL_IN_SCOPE"
            r["supports_state"] = "NOT_FOUND"
            r["note"] = "A source repository is the right place for this, so zero hits supports a NOT FOUND finding."
        else:
            if iac_present:
                r["verdict"] = "NO_SIGNAL_IN_SCOPE"
                r["supports_state"] = "NOT_FOUND"
                r["note"] = "Normally configured outside the repo, but this repo ships IaC, so the repo does cover it. Zero hits supports NOT FOUND."
            else:
                r["verdict"] = "OUT_OF_SCOPE_UNSEEN"
                r["supports_state"] = "UNVERIFIED"
                r["note"] = "Normally configured outside the repo and no IaC is present, so absence here proves nothing. Report as UNVERIFIED and say what evidence would resolve it."
    return results


def lens_signals(results):
    def present(cid):
        return results[cid]["hit_count"] > 0
    return {
        "frontend_present": present("frontend_present"),
        "llm_present": present("llm_sdk"),
        "broker_present": present("message_broker"),
        "cache_present": present("caching_layer"),
        "iac_present": present("iac"),
        "ci_present": present("ci_pipeline"),
        "container_present": present("container_build"),
        "tests_present": present("test_files") or present("test_framework"),
        "migrations_present": present("migrations"),
    }


def render_md(ledger):
    L = ledger
    out = ["# Absence ledger", "",
           f"Files scanned: {L['files_scanned']}"
           + (" (TRUNCATED - repository exceeded the scan cap)" if L["truncated"] else ""),
           "",
           "Every `[NOT FOUND]` finding must cite a row below whose hit count is 0 and "
           "whose *Supports* column says `NOT_FOUND`. A row saying `UNVERIFIED` means the "
           "repository is the wrong place to look - report it as unverified, not as absent.",
           ""]
    for lens in ["security", "backend", "frontend", "devops", "qa", "database", "ai-security"]:
        rows = [r for r in L["controls"].values() if r["lens"] == lens]
        if not rows:
            continue
        out += [f"## {lens}", "",
                "| Control | Polarity | Hits | Verdict | Supports | Example paths |",
                "| --- | --- | --- | --- | --- | --- |"]
        for r in sorted(rows, key=lambda x: x["id"]):
            paths = ", ".join(h["path"] for h in r["hits"][:3]) or "-"
            if r["hits_truncated"]:
                paths += ", ..."
            out.append(f"| `{r['id']}` - {r['label']} | {r['polarity']} | {r['hit_count']} "
                       f"| {r['verdict']} | {r['supports_state']} | {paths} |")
        out.append("")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_root")
    ap.add_argument("--out", help="output directory (default <root>/.readiness-audit/evidence)")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    files, truncated = collect(root)
    controls = compile_controls()
    results = evaluate(controls, files)
    iac_present = results["iac"]["hit_count"] > 0
    results = verdicts(results, iac_present)

    ledger = {
        "schema": 1,
        "project_root": str(root),
        "files_scanned": len(files),
        "truncated": truncated,
        "iac_present": iac_present,
        "lens_signals": lens_signals(results),
        "controls": results,
    }

    outdir = Path(args.out) if args.out else root / ".readiness-audit" / "evidence"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "absence-ledger.json").write_text(json.dumps(ledger, indent=2) + "\n")
    if not args.json_only:
        (outdir / "absence-ledger.md").write_text(render_md(ledger))

    absent = [r["id"] for r in results.values()
              if r["polarity"] == "control" and r["supports_state"] == "NOT_FOUND"]
    unver = [r["id"] for r in results.values() if r["supports_state"] == "UNVERIFIED"]
    sinks = [r["id"] for r in results.values() if r["verdict"] == "SINK_PRESENT"]
    print(json.dumps({
        "files_scanned": len(files),
        "truncated": truncated,
        "lens_signals": ledger["lens_signals"],
        "not_found_candidates": absent,
        "unverified_candidates": unver,
        "sinks_to_read": sinks,
        "written_to": str(outdir),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
