"""
Classification — write operations.

Every state change and every authorization check for this module lives here. This file
exists from day one even while empty: R-12 is the risk that "service-layer discipline
erodes under Django's idiom, scattering authorization into views/serializers", and the
named mitigation is that the convention is already in place, so putting a rule in a view
is never the path of least resistance.

Rules for this file [doc: Arch §3.1, FR-3]:
  - Callers pass the acting user; functions authorize before mutating. DRF permission
    classes are defence-in-depth, never the enforcement point.
  - Wrap multi-write operations in `transaction.atomic`.
  - Enqueue Celery tasks via `transaction.on_commit` so a worker cannot observe an
    uncommitted row [doc: Arch §2.4, §4.1].
  - Reads belong in selectors.py.

T3.1–T3.3 add the composition roots: the factories below are the one place where settings and
database rows become the plain constructor arguments the Django-free classifiers take. Nothing here
classifies; nothing here decides *which* classifier a report gets, which is T3.4's degradation
policy [doc: plan T3.2, T3.4].

[doc: Arch §3, §6 (FR-10, FR-12, FR-13, FR-13a, NFR-13)]
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Literal

import structlog
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from urbenmend.classification.contracts import (
    Classification,
    ClassificationError,
    ClassificationRequest,
    ClassificationUnavailable,
    ClassifierSource,
    coerce_category,
    parse_severity,
)
from urbenmend.classification.keywords import KeywordFallbackClassifier
from urbenmend.classification.llm import (
    LLMClassificationAdapter,
    LLMProvider,
    OpenAICompatibleLLMProvider,
)
from urbenmend.classification.selectors import active_category_slugs, active_keyword_rules

logger = structlog.get_logger(__name__)

_CACHE_PREFIX = "classification"
_PROMPT_CACHE_VERSION = "v1"

ClassificationOutcome = Literal[
    "classified",
    "already_classified",
    "missing",
    "moderated",
    "ineligible",
    "stale",
]


def build_keyword_fallback() -> KeywordFallbackClassifier:
    """Compose the deterministic fallback from the current keyword table (FR-13a).

    ⚠️ **Rules are read here, per build, not cached in a module global.** `SeverityKeyword` is
    admin-managed (FR-30/NFR-11), so an operator retiring a bad rule mid-outage must see it take
    effect on the next report — not after the worker is restarted. Caching this is a legitimate
    optimisation *later*, but it belongs behind an explicit invalidation, not behind a module
    attribute that silently pins the rule set for the process's lifetime.

    ⚠️ **Settings are read at call time, not bound as module-level constants.** T1.8 learned this
    the hard way with the throttle rates: a value captured at import cannot be reached by
    `override_settings`, so every test of a configured behaviour silently asserts the default.
    """
    return KeywordFallbackClassifier(
        active_keyword_rules(),
        matched_confidence=settings.CLASSIFICATION_FALLBACK_MATCHED_CONFIDENCE,
        unmatched_confidence=settings.CLASSIFICATION_FALLBACK_UNMATCHED_CONFIDENCE,
    )


def build_llm_provider() -> LLMProvider:
    """Instantiate the configured provider (❓Q9 deferred — see `settings.base`).

    ⚠️ **`ImproperlyConfigured`, not a silent degrade to `UnconfiguredLLMProvider`.** A typo in a
    dotted path is a deployment mistake, categorically different from the provider being down: FR-13a
    exists so an *outage* does not stop triage, and quietly treating a config error as an outage
    would leave a deployment believing it had LLM classification while every report came from the
    keyword engine — invisible in the API, and visible in NFR-9's fallback-rate KPI only as an
    unexplained step change nobody could attribute.

    ⚠️ **Therefore T3.4 must not widen its `except` to cover this.** Its degradation catches
    `ClassificationError`; `ImproperlyConfigured` is deliberately outside that hierarchy so it cannot
    be swallowed by the fallback path. It is also why the subclass check below is a check and not an
    `assert` — `-O` strips asserts, and this is the guard that turns "some object with a `complete`
    method" into a contract.

    Raises:
        ImproperlyConfigured: the path cannot be imported, or does not name an `LLMProvider`.
    """
    path = settings.CLASSIFICATION_LLM_PROVIDER
    if path == "openai_compatible":
        if not settings.CLASSIFICATION_LLM_API_KEY:
            raise ImproperlyConfigured("CLASSIFICATION_LLM_API_KEY is required")
        return OpenAICompatibleLLMProvider(
            endpoint=settings.CLASSIFICATION_LLM_ENDPOINT,
            api_key=settings.CLASSIFICATION_LLM_API_KEY,
            model=settings.CLASSIFICATION_LLM_MODEL,
        )
    try:
        provider_class = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(
            f"CLASSIFICATION_LLM_PROVIDER={path!r} cannot be imported: {exc}"
        ) from exc

    if not (isinstance(provider_class, type) and issubclass(provider_class, LLMProvider)):
        raise ImproperlyConfigured(
            f"CLASSIFICATION_LLM_PROVIDER={path!r} must name a subclass of "
            "urbenmend.classification.llm.LLMProvider."
        )
    return provider_class()


def build_llm_classifier() -> LLMClassificationAdapter:
    """Compose the hosted-LLM adapter from settings (T3.2, NFR-13, Arch §6).

    ⚠️ **The NFR-13 caps are applied here, at composition, so no provider can opt out of them.** The
    token ceiling and the timeout travel on each `LLMPrompt` (see `llm.LLMPrompt`) rather than being
    a provider's own configuration — a provider that set its own would let the second provider
    someone adds quietly ignore the cap, and the breach would arrive as an invoice.
    """
    return LLMClassificationAdapter(
        build_llm_provider(),
        max_output_tokens=settings.CLASSIFICATION_LLM_MAX_OUTPUT_TOKENS,
        timeout_seconds=settings.CLASSIFICATION_LLM_TIMEOUT_SECONDS,
        max_attempts=settings.CLASSIFICATION_LLM_MAX_ATTEMPTS,
        backoff_seconds=settings.CLASSIFICATION_LLM_BACKOFF_SECONDS,
    )


def _cache_failure(operation: str, exc: Exception) -> ClassificationUnavailable:
    logger.warning(
        "classification.control_unavailable",
        operation=operation,
        error_type=type(exc).__name__,
    )
    return ClassificationUnavailable(
        "The classification control store is unavailable; use the keyword fallback."
    )


def _cache_get(key: str) -> object:
    try:
        value: object = cache.get(key)
    except Exception as exc:
        raise _cache_failure("get", exc) from exc
    return value


def _cache_delete_best_effort(key: str) -> None:
    try:
        cache.delete(key)
    except Exception as exc:
        logger.warning(
            "classification.control_cleanup_failed",
            operation="delete",
            error_type=type(exc).__name__,
        )


def _cache_set_best_effort(key: str, value: object, timeout: int) -> None:
    try:
        cache.set(key, value, timeout=timeout)
    except Exception as exc:
        logger.warning(
            "classification.control_cleanup_failed",
            operation="set",
            error_type=type(exc).__name__,
        )


def _positive_setting(name: str) -> int:
    value = int(getattr(settings, name))
    if value <= 0:
        raise ImproperlyConfigured(f"{name} must be greater than zero.")
    return value


def _response_cache_key(request: ClassificationRequest) -> str:
    payload = json.dumps(
        {
            "version": _PROMPT_CACHE_VERSION,
            "provider": settings.CLASSIFICATION_LLM_PROVIDER,
            "text": request.text,
            "language": request.language,
            "categories": request.allowed_categories,
            "max_output_tokens": settings.CLASSIFICATION_LLM_MAX_OUTPUT_TOKENS,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{_CACHE_PREFIX}:response:{hashlib.sha256(payload).hexdigest()}"


def _serialize_classification(result: Classification) -> dict[str, object]:
    return {
        "category": result.category,
        "severity": result.severity.value,
        "confidence": result.confidence,
        "source": result.source.value,
        "model": result.model,
        "rationale": result.rationale,
    }


def _deserialize_classification(
    value: object, request: ClassificationRequest
) -> Classification | None:
    if not isinstance(value, dict):
        return None

    model = value.get("model")
    rationale = value.get("rationale")
    raw_confidence = value.get("confidence")
    if (
        not isinstance(model, str)
        or not isinstance(rationale, str)
        or isinstance(raw_confidence, bool)
        or not isinstance(raw_confidence, (int, float, str))
    ):
        return None

    try:
        confidence = float(raw_confidence)
        source = ClassifierSource(str(value.get("source")))
        severity = parse_severity(value.get("severity"))
        result = Classification(
            category=coerce_category(value.get("category"), request.allowed_categories),
            severity=severity,
            confidence=confidence,
            source=source,
            model=model,
            rationale=rationale,
        )
    except (TypeError, ValueError):
        return None

    return result if result.source == ClassifierSource.LLM else None


def _cached_llm_result(request: ClassificationRequest) -> Classification | None:
    if int(settings.CLASSIFICATION_LLM_CACHE_SECONDS) <= 0:
        return None

    key = _response_cache_key(request)
    value = _cache_get(key)
    if value is None:
        return None

    result = _deserialize_classification(value, request)
    if result is None:
        _cache_delete_best_effort(key)
        return None

    logger.info("classification.llm.cache_hit", model=result.model)
    return result


def _store_llm_result(request: ClassificationRequest, result: Classification) -> None:
    timeout = int(settings.CLASSIFICATION_LLM_CACHE_SECONDS)
    if timeout <= 0:
        return
    _cache_set_best_effort(
        _response_cache_key(request),
        _serialize_classification(result),
        timeout,
    )


def _reserve_counter(key: str, *, amount: int, limit: int, timeout: int) -> bool:
    try:
        if cache.add(key, amount, timeout=timeout):
            current = amount
        else:
            current = int(cache.incr(key, amount))
    except Exception as exc:
        raise _cache_failure("reserve", exc) from exc
    return current <= limit


def _rate_limit_key(scope: str, identity: str, window_seconds: int) -> str:
    window = int(time.time()) // window_seconds
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]
    return f"{_CACHE_PREFIX}:rate:{scope}:{digest}:{window}"


def _reserve_llm_call(user_scope: str) -> None:
    window = _positive_setting("CLASSIFICATION_LLM_RATE_WINDOW_SECONDS")
    user_limit = int(settings.CLASSIFICATION_LLM_USER_RATE_LIMIT)
    global_limit = int(settings.CLASSIFICATION_LLM_GLOBAL_RATE_LIMIT)

    if user_limit <= 0 or global_limit <= 0:
        raise ClassificationUnavailable("The configured LLM call limit has been reached.")

    timeout = window * 2
    if not _reserve_counter(
        _rate_limit_key("user", user_scope, window),
        amount=1,
        limit=user_limit,
        timeout=timeout,
    ):
        raise ClassificationUnavailable("The per-user LLM call limit has been reached.")
    if not _reserve_counter(
        _rate_limit_key("global", "all", window),
        amount=1,
        limit=global_limit,
        timeout=timeout,
    ):
        raise ClassificationUnavailable("The global LLM call limit has been reached.")


def _estimated_token_cost(request: ClassificationRequest) -> int:
    prompt_characters = len(request.text) + sum(map(len, request.allowed_categories)) + 700
    estimated_input = math.ceil(prompt_characters / 4)
    return estimated_input + int(settings.CLASSIFICATION_LLM_MAX_OUTPUT_TOKENS)


def _reserve_daily_budget(request: ClassificationRequest) -> None:
    budget = int(settings.CLASSIFICATION_LLM_DAILY_TOKEN_BUDGET)
    if budget <= 0:
        raise ClassificationUnavailable("The configured daily LLM budget has been reached.")

    day = timezone.now().date().isoformat()
    allowed = _reserve_counter(
        f"{_CACHE_PREFIX}:budget:{day}",
        amount=_estimated_token_cost(request),
        limit=budget,
        timeout=172_800,
    )
    if not allowed:
        raise ClassificationUnavailable("The configured daily LLM budget has been reached.")


def _circuit_key(suffix: str) -> str:
    provider = str(settings.CLASSIFICATION_LLM_PROVIDER)
    digest = hashlib.sha256(provider.encode("utf-8")).hexdigest()[:24]
    return f"{_CACHE_PREFIX}:circuit:{digest}:{suffix}"


def _ensure_circuit_closed() -> None:
    if _cache_get(_circuit_key("open")):
        raise ClassificationUnavailable("The LLM circuit breaker is open.")


def _record_llm_success() -> None:
    _cache_delete_best_effort(_circuit_key("failures"))
    _cache_delete_best_effort(_circuit_key("open"))


def _record_llm_failure() -> None:
    threshold = _positive_setting("CLASSIFICATION_LLM_CIRCUIT_FAILURE_THRESHOLD")
    recovery = _positive_setting("CLASSIFICATION_LLM_CIRCUIT_RECOVERY_SECONDS")
    failures_key = _circuit_key("failures")
    try:
        if cache.add(failures_key, 1, timeout=recovery):
            failures = 1
        else:
            failures = int(cache.incr(failures_key))
        if failures >= threshold:
            cache.set(_circuit_key("open"), True, timeout=recovery)
            cache.delete(failures_key)
            logger.warning(
                "classification.llm.circuit_opened",
                threshold=threshold,
                recovery_seconds=recovery,
            )
    except Exception as exc:
        logger.warning(
            "classification.control_cleanup_failed",
            operation="record_failure",
            error_type=type(exc).__name__,
        )


def classify_with_fallback(request: ClassificationRequest, *, user_scope: str) -> Classification:
    """Classify through the LLM when permitted, otherwise deterministically fall back."""
    try:
        cached = _cached_llm_result(request)
        if cached is not None:
            return cached

        _ensure_circuit_closed()
        _reserve_llm_call(user_scope)
        _reserve_daily_budget(request)
    except ClassificationError as exc:
        logger.warning(
            "classification.fallback_selected",
            reason_type=type(exc).__name__,
        )
        return build_keyword_fallback().classify(request)

    try:
        result = build_llm_classifier().classify(request)
    except ClassificationError as exc:
        _record_llm_failure()
        logger.warning(
            "classification.fallback_selected",
            reason_type=type(exc).__name__,
        )
        return build_keyword_fallback().classify(request)

    _record_llm_success()
    _store_llm_result(request, result)
    return result


def _needs_review(confidence: float) -> bool:
    threshold = settings.CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD
    if threshold is None:
        return False
    value = float(threshold)
    if not 0.0 <= value <= 1.0:
        raise ImproperlyConfigured(
            "CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD must be between 0.0 and 1.0."
        )
    return confidence < value


def classify_report_record(report_id: str) -> ClassificationOutcome:
    """Classify one persisted Report and atomically store the result (T3.5)."""
    # Local imports avoid the reporting.services -> classification.tasks import cycle documented
    # at the enqueue boundary.
    from urbenmend.classification.models import Category
    from urbenmend.reporting.models import ClassificationSource, Report, ReportStatus

    try:
        report_uuid = uuid.UUID(report_id)
    except (TypeError, ValueError):
        logger.warning("classification.report_invalid_id", report_id=report_id)
        return "missing"

    report = Report.objects.select_related("category").filter(pk=report_uuid).first()
    if report is None:
        return "missing"
    if report.status in {ReportStatus.HIDDEN, ReportStatus.REMOVED}:
        return "moderated"
    if report.is_classified:
        return "already_classified"
    if report.status not in {ReportStatus.SUBMITTED, ReportStatus.PROCESSING}:
        return "ineligible"

    snapshot_description = report.description
    snapshot_language = report.language
    request = ClassificationRequest(
        text=snapshot_description,
        language=snapshot_language,
        allowed_categories=active_category_slugs(),
    )
    result = classify_with_fallback(request, user_scope=str(report.author_id))

    with transaction.atomic():
        current = Report.objects.select_for_update().get(pk=report_uuid)
        if current.status in {ReportStatus.HIDDEN, ReportStatus.REMOVED}:
            return "moderated"
        if current.is_classified:
            return "already_classified"
        if current.status not in {ReportStatus.SUBMITTED, ReportStatus.PROCESSING}:
            return "ineligible"
        if current.description != snapshot_description or current.language != snapshot_language:
            return "stale"

        changed = [
            "severity_signal",
            "confidence",
            "classification_model",
            "classification_rationale",
            "classified_at",
            "classification_needs_review",
            "updated_at",
        ]
        current.severity_signal = result.severity.value
        current.confidence = result.confidence
        current.classification_model = result.model
        current.classification_rationale = result.rationale
        current.classified_at = timezone.now()
        current.classification_needs_review = _needs_review(result.confidence)

        # An Authority/Admin correction wins over automation for category. The single persisted
        # source field therefore remains `authority`; the automated model and rationale are still
        # retained for explainability and evaluation.
        if current.classification_source != ClassificationSource.AUTHORITY:
            current.category = Category.objects.get(slug=result.category)
            current.classification_source = result.source.value
            changed.extend(["category", "classification_source"])

        if current.status == ReportStatus.SUBMITTED:
            current.status = ReportStatus.PROCESSING
            changed.append("status")

        current.save(update_fields=changed)

    return "classified"
