"""Shared market data helpers for MCP and local agent tools."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 250

# Symbol -> preferred source. The matched source is the head of its market's
# fallback chain (registry.FALLBACK_CHAINS), so an unavailable preferred source
# still degrades gracefully to the rest of the chain. US/HK equities route to
# the throttle-tolerant Yahoo public endpoint first (lower IP-ban risk than the
# yfinance SDK), A-shares to the Tencent quote endpoint.
_SOURCE_PATTERNS = [
    (re.compile(r"^local:", re.I), "local"),
    (re.compile(r"^\d{6}\.(SZ|SH|BJ)$", re.I), "tencent"),
    (re.compile(r"^[A-Z]+\.US$", re.I), "yahoo"),
    (re.compile(r"^\d{3,5}\.HK$", re.I), "yahoo"),
    (re.compile(r"^[A-Z]+-USDT$", re.I), "okx"),
    (re.compile(r"^[A-Z]+/USDT$", re.I), "ccxt"),
]


def detect_source(code: str) -> str:
    """Infer the best loader source for a normalized symbol."""
    for pattern, source in _SOURCE_PATTERNS:
        if pattern.match(code):
            return source
    return "tushare"


# Exchange-suffix aliases from conventions callers commonly know (Yahoo
# Finance uses ".SS" for the Shanghai Stock Exchange) mapped to this repo's
# canonical suffix. Without this, an alias silently misses the tencent/
# tushare A-share regexes above and falls through to the "tushare" default
# for every call, which then depends on tushare being installed/authorized
# even for a plain Shanghai OHLCV request.
_EXCHANGE_SUFFIX_ALIASES = {".SS": ".SH"}


def normalize_code(code: str) -> str:
    """Map a known exchange-suffix alias to this repo's canonical form."""
    upper = code.upper()
    for alias, canonical in _EXCHANGE_SUFFIX_ALIASES.items():
        if upper.endswith(alias):
            return code[: -len(alias)] + canonical
    return code


def get_loader(source: str):
    """Get loader class via registry with fallback support."""
    from backtest.loaders.registry import get_loader_cls_with_fallback

    return get_loader_cls_with_fallback(source)


def cap_rows(records: list, max_rows: int) -> list | dict[str, object]:
    """Bound a per-symbol row list to keep tool payloads within budget."""
    n = len(records)
    if max_rows < 0:
        max_rows = DEFAULT_MAX_ROWS
    if max_rows == 0 or n <= max_rows:
        return records
    step = math.ceil(n / max_rows)
    sampled = records[::step]
    if sampled[-1] is not records[-1]:
        sampled = sampled + [records[-1]]
    return {
        "rows": n,
        "returned": len(sampled),
        "truncated": True,
        "policy": f"every-{step}th-row (even stride; last bar pinned)",
        "hint": "narrow the date range, coarsen interval, or set max_rows=0 for all rows",
        "data": sampled,
    }


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _maybe_audited_loader(
    loader: Any,
    *,
    requested_source: str,
    selected_source: str,
    codes: list[str],
    interval: str,
):
    """Wrap a loader with observe-mode data audit metadata when enabled."""
    try:
        from src.reliability.config import reliability_enabled
    except Exception:
        return loader

    if not reliability_enabled():
        return loader

    try:
        from src.reliability.data.contracts import DataSetContract
        from src.reliability.data.loader_wrapper import AuditedDataLoader
        from src.reliability.data.source_manifest import SourceManifest
    except Exception:
        logger.exception("failed to initialize data audit wrapper; using raw loader")
        return loader

    selected = str(getattr(loader, "name", selected_source))
    manifest = SourceManifest(
        requested_source=requested_source,
        selected_source=selected,
        fallback_chain=[selected],
        attempted_sources=[selected],
        runtime_source=selected,
        cache_hit=False,
    )
    return AuditedDataLoader(
        loader,
        source=requested_source,
        selected_source=selected,
        source_manifest=manifest,
        dataset_contract=DataSetContract(
            dataset_id=f"{selected}:ohlcv:{interval}",
            asset_class=_infer_asset_class(codes),
            frequency=interval,
            calendar=selected,
            fields=["open", "high", "low", "close", "volume"],
            timezone=_infer_timezone(codes),
        ),
        dataset_kind="ohlcv",
        market_rule_config={
            "asset_class": _infer_asset_class(codes),
        },
    )


def _attach_audit_metadata(results: dict[str, Any], loader: Any) -> None:
    report = getattr(loader, "last_audit_report", None)
    if report is None:
        return

    metadata = results.setdefault("_metadata", {})
    audit_ids = metadata.setdefault("data_audit_ids", [])
    if report.audit_id not in audit_ids:
        audit_ids.append(report.audit_id)

    refs = metadata.setdefault("artifact_refs", [])
    refs.extend(ref.model_dump(mode="json") for ref in report.artifact_refs)


def _infer_asset_class(codes: list[str]) -> str:
    if any(re.match(r"^\d{6}\.(SZ|SH|BJ)$", code, re.I) for code in codes):
        return "ashare"
    if any(re.match(r"^[A-Z]+\.US$", code, re.I) for code in codes):
        return "us_equity"
    if any(re.match(r"^\d{3,5}\.HK$", code, re.I) for code in codes):
        return "hk_equity"
    if any(re.match(r"^[A-Z]+[-/]USDT$", code, re.I) for code in codes):
        return "crypto"
    return "other"


def _infer_timezone(codes: list[str]) -> str:
    asset_class = _infer_asset_class(codes)
    if asset_class == "ashare":
        return "Asia/Shanghai"
    if asset_class in {"us_equity", "crypto"}:
        return "UTC"
    if asset_class == "hk_equity":
        return "Asia/Hong_Kong"
    return "UTC"


def fetch_market_data(
    *,
    codes: list[str],
    start_date: str,
    end_date: str,
    source: str = "auto",
    interval: str = "1D",
    max_rows: int = DEFAULT_MAX_ROWS,
    loader_resolver: Callable[[str], type] = get_loader,
) -> dict[str, Any]:
    """Fetch normalized OHLCV data through the repository loader layer."""
    results: dict[str, Any] = {}

    # Route/fetch on the normalized code (canonical exchange suffix) but key
    # results by whatever the caller originally requested, so an alias like
    # ".SS" both resolves correctly AND echoes back under the caller's symbol.
    original_by_normalized = {normalize_code(code): code for code in codes}
    normalized_codes = list(original_by_normalized)

    if source == "auto":
        groups: dict[str, list[str]] = {}
        for code in normalized_codes:
            src = detect_source(code)
            groups.setdefault(src, []).append(code)
    else:
        groups = {source: list(normalized_codes)}

    for src, src_codes in groups.items():
        loader_cls = loader_resolver(src)
        loader = loader_cls()
        loader = _maybe_audited_loader(
            loader,
            requested_source=source,
            selected_source=src,
            codes=src_codes,
            interval=interval,
        )
        try:
            data_map = loader.fetch(src_codes, start_date, end_date, interval=interval)
        except Exception:
            logger.exception(
                "market-data loader %r failed for %s; codes fall through to _unresolved",
                src,
                src_codes,
            )
            data_map = {}
        _attach_audit_metadata(results, loader)
        for symbol, df in data_map.items():
            original_symbol = original_by_normalized.get(symbol, symbol)
            records = df.reset_index().to_dict(orient="records")
            for row in records:
                for key, value in row.items():
                    row[key] = _json_safe(value)
            results[original_symbol] = cap_rows(records, max_rows)

    unresolved = [code for code in codes if code not in results]
    if unresolved:
        results["_unresolved"] = unresolved
        # An explicit non-auto source (e.g. a caller-forced "tushare") that
        # resolves nothing gives the caller zero signal to change approach —
        # loader-level failures (auth/permission errors, etc.) are logged
        # server-side but never reach this JSON body. Without this hint a
        # caller has been observed retrying the identical failing call dozens
        # of times instead of switching source.
        if source != "auto" and len(unresolved) == len(codes):
            results["_hint"] = (
                f"All codes failed with source={source!r}. Retry with source='auto' "
                "(or omit source) to fall back across free public endpoints instead "
                "of repeating the same forced source."
            )

    return results


def fetch_market_data_json(**kwargs: Any) -> str:
    """Fetch market data and return strict JSON."""
    return json.dumps(fetch_market_data(**kwargs), ensure_ascii=False, indent=2, allow_nan=False)
