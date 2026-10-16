from __future__ import annotations

import re
import asyncio
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any

from sqlmodel import Session, select

from app.adapters.sec import SECAdapter, parse_sec_datetime
from app.config import get_settings
from app.models import SecCompany, SecFiling
from app.services.market_cache import MarketCacheService


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class SECResearchService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.adapter = SECAdapter()
        self.cache = MarketCacheService()

    async def ensure_company(self, session: Session, symbol: str) -> SecCompany | None:
        symbol = symbol.upper()
        stmt = select(SecCompany).where(SecCompany.symbol == symbol)
        existing = session.exec(stmt).first()
        if existing:
            return existing

        mapping = self.cache.get_json(session, "sec", "ticker_map", max_age_seconds=86400)
        if mapping is None:
            mapping = await self.adapter.ticker_map()
            self.cache.set_json(session, "sec", "ticker_map", mapping)
        payload = mapping.get(symbol)
        if not payload:
            return None

        company = SecCompany(
            symbol=symbol,
            cik=payload["cik"],
            name=payload["name"],
        )
        session.add(company)
        session.commit()
        session.refresh(company)
        return company

    async def sync_symbol(self, session: Session, symbol: str, *, limit: int = 8) -> dict[str, Any]:
        company = await self.ensure_company(session, symbol)
        if not company:
            return {
                "symbol": symbol.upper(),
                "company_name": symbol.upper(),
                "cik": None,
                "fundamentals": {},
                "filings": [],
                "catalysts": [],
            }

        submissions_key = f"submissions:{company.cik}"
        companyfacts_key = f"companyfacts:{company.cik}"

        submissions = self.cache.get_json(
            session,
            "sec",
            submissions_key,
            max_age_seconds=self.settings.sec_filings_cache_seconds,
        )
        if submissions is None:
            submissions = await self.adapter.get_submissions(company.cik)
            self.cache.set_json(session, "sec", submissions_key, submissions)

        companyfacts = self.cache.get_json(
            session,
            "sec",
            companyfacts_key,
            max_age_seconds=self.settings.sec_companyfacts_cache_seconds,
        )
        if companyfacts is None:
            try:
                companyfacts = await self.adapter.get_companyfacts(company.cik)
                self.cache.set_json(session, "sec", companyfacts_key, companyfacts)
            except Exception:
                companyfacts = {}

        filings = await self._sync_recent_filings(session, company, submissions, limit=limit)
        fundamentals = self._extract_fundamentals(companyfacts)
        return {
            "symbol": company.symbol,
            "company_name": company.name,
            "cik": company.cik,
            "fundamentals": fundamentals,
            "filings": filings,
            "catalysts": self._top_catalysts(filings),
        }

    async def _sync_recent_filings(
        self,
        session: Session,
        company: SecCompany,
        submissions: dict[str, Any],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        recent = (submissions.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accession_numbers = recent.get("accessionNumber") or []
        filing_dates = recent.get("filingDate") or []
        report_dates = recent.get("reportDate") or []
        primary_docs = recent.get("primaryDocument") or []
        item_codes = recent.get("items") or []
        descriptions = recent.get("primaryDocDescription") or []
        accepted_at = recent.get("acceptanceDateTime") or []

        filings: list[dict[str, Any]] = []
        document_fetches = 0
        for idx, accession in enumerate(accession_numbers[: limit * 2]):
            form_type = str(forms[idx] or "")
            if form_type not in {"10-K", "10-Q", "8-K", "13D", "13D/A", "13G", "13G/A", "4", "S-3", "S-3ASR", "DEF 14A"}:
                continue
            filing_date = self._parse_date(filing_dates[idx]) or _utcnow()
            stmt = select(SecFiling).where(
                SecFiling.symbol == company.symbol,
                SecFiling.accession_number == accession,
            )
            record = session.exec(stmt).first()
            if not record:
                record = SecFiling(
                    symbol=company.symbol,
                    cik=company.cik,
                    accession_number=accession,
                    form_type=form_type,
                    filing_date=filing_date,
                    report_date=report_dates[idx] or None,
                    primary_document=primary_docs[idx] or None,
                    item_codes=item_codes[idx] or None,
                    description=descriptions[idx] or None,
                    filing_url=self._filing_url(company.cik, accession, primary_docs[idx] or ""),
                )
                session.add(record)
                session.commit()
                session.refresh(record)

            failure_key = f"document_failure:{accession}"
            document_backoff = self.cache.get_json(session, "sec", failure_key, max_age_seconds=21600)
            if (not record.text_excerpt and form_type in {"8-K", "10-Q", "10-K"}
                    and record.primary_document and filing_date >= _utcnow() - timedelta(days=14)
                    and document_fetches < 2 and not document_backoff):
                document_fetches += 1
                try:
                    filing_text = await asyncio.wait_for(self.adapter.get_primary_document_text(
                        cik=company.cik,
                        accession_number=accession,
                        primary_document=record.primary_document,
                    ), timeout=12)
                    record.text_excerpt = self._extract_excerpt(filing_text)
                    session.add(record)
                    session.commit()
                except Exception:
                    self.cache.set_json(session, "sec", failure_key, {"unavailable": True})

            filing_signals = self._filing_signals(
                form_type=form_type,
                item_codes=item_codes[idx] or "",
                description=descriptions[idx] or "",
                excerpt=record.text_excerpt or "",
            )
            filings.append(
                {
                    "symbol": company.symbol,
                    "accession_number": accession,
                    "form_type": form_type,
                    "filing_date": filing_date.isoformat(),
                    "report_date": report_dates[idx] or None,
                    "items": item_codes[idx] or None,
                    "description": descriptions[idx] or None,
                    "excerpt": record.text_excerpt,
                    "accepted_at": parse_sec_datetime(accepted_at[idx]).isoformat() if parse_sec_datetime(accepted_at[idx]) else None,
                    "filing_url": record.filing_url,
                    "signal_summary": self._signal_summary(form_type, item_codes[idx] or "", record.text_excerpt or ""),
                    "signals": filing_signals["signals"],
                    "risk_flags": filing_signals["risk_flags"],
                    "signal_score": filing_signals["score"],
                }
            )
            if len(filings) >= limit:
                break
        return filings

    def _extract_fundamentals(self, companyfacts: dict[str, Any]) -> dict[str, Any]:
        us_gaap = (companyfacts.get("facts") or {}).get("us-gaap") or {}
        revenue_series = self._quarterly_fact_series(
            us_gaap,
            [
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "SalesRevenueNet",
            ],
            unit="USD",
        )
        net_income_series = self._quarterly_fact_series(us_gaap, ["NetIncomeLoss"], unit="USD")
        gross_profit_series = self._quarterly_fact_series(us_gaap, ["GrossProfit"], unit="USD")
        eps_series = self._quarterly_fact_series(us_gaap, ["EarningsPerShareDiluted"], unit="USD/shares")
        shares_series = self._quarterly_fact_series(
            us_gaap,
            ["WeightedAverageNumberOfDilutedSharesOutstanding"],
            unit="shares",
        )

        revenue_growth_yoy = self._series_yoy_growth(revenue_series)
        revenue_growth_yoy_prev = self._series_yoy_growth(revenue_series[1:]) if len(revenue_series) >= 3 else None
        net_income_growth_yoy = self._series_yoy_growth(net_income_series)
        eps_growth_yoy = self._series_yoy_growth(eps_series)
        gross_margin_latest = self._margin_from_series(gross_profit_series, revenue_series)
        shares_growth_yoy = self._series_yoy_growth(shares_series)

        return {
            "revenue": self._latest_fact_value(
                us_gaap,
                [
                    "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "Revenues",
                    "SalesRevenueNet",
                ],
                unit="USD",
            ),
            "net_income": self._latest_fact_value(us_gaap, ["NetIncomeLoss"], unit="USD"),
            "eps_diluted": self._latest_fact_value(us_gaap, ["EarningsPerShareDiluted"], unit="USD/shares"),
            "gross_profit": self._latest_fact_value(us_gaap, ["GrossProfit"], unit="USD"),
            "quarterly": {
                "revenue": revenue_series[:6],
                "net_income": net_income_series[:6],
                "gross_profit": gross_profit_series[:6],
                "eps_diluted": eps_series[:6],
                "shares_diluted": shares_series[:6],
            },
            "trend_summary": {
                "revenue_growth_yoy_pct": revenue_growth_yoy,
                "revenue_growth_yoy_prev_pct": revenue_growth_yoy_prev,
                "revenue_acceleration_pct": self._safe_delta(revenue_growth_yoy, revenue_growth_yoy_prev),
                "net_income_growth_yoy_pct": net_income_growth_yoy,
                "eps_growth_yoy_pct": eps_growth_yoy,
                "gross_margin_pct": gross_margin_latest,
                "share_dilution_yoy_pct": shares_growth_yoy,
                "profitable_latest_quarter": self._latest_positive(net_income_series),
            },
        }

    def _latest_fact_value(
        self,
        namespace: dict[str, Any],
        fact_names: list[str],
        *,
        unit: str,
    ) -> dict[str, Any] | None:
        for fact_name in fact_names:
            fact = namespace.get(fact_name) or {}
            units = fact.get("units") or {}
            values = units.get(unit) or []
            if not values:
                continue
            latest = sorted(
                values,
                key=lambda item: (item.get("end") or "", item.get("filed") or ""),
                reverse=True,
            )[0]
            return {
                "fact": fact_name,
                "value": latest.get("val"),
                "end": latest.get("end"),
                "fy": latest.get("fy"),
                "fp": latest.get("fp"),
                "filed": latest.get("filed"),
                "form": latest.get("form"),
            }
        return None

    def _filing_url(self, cik: str, accession_number: str, primary_document: str) -> str:
        accession_path = accession_number.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/{primary_document}"

    def _extract_excerpt(self, filing_text: str) -> str:
        normalized = re.sub(r"\s+", " ", filing_text)
        for keyword in (
            "guidance",
            "outlook",
            "earnings",
            "results of operations",
            "spin-off",
            "offering",
            "restructuring",
            "demand",
            "AI",
        ):
            match = re.search(keyword, normalized, flags=re.IGNORECASE)
            if match:
                start = max(0, match.start() - 140)
                end = min(len(normalized), match.end() + 280)
                return normalized[start:end].strip()
        return normalized[:420].strip()

    def _signal_summary(self, form_type: str, item_codes: str, excerpt: str) -> str:
        signals: list[str] = []
        if form_type == "8-K":
            if "2.02" in item_codes:
                signals.append("earnings release filed")
            if "5.02" in item_codes:
                signals.append("management change disclosed")
            if "8.01" in item_codes:
                signals.append("business update disclosed")
        elif form_type in {"10-Q", "10-K"}:
            signals.append("periodic results filing")
        elif form_type.startswith("13D"):
            signals.append("active ownership stake update")
        elif form_type.startswith("13G"):
            signals.append("passive ownership stake update")
        elif form_type == "4":
            signals.append("insider transaction filing")
        elif form_type.startswith("S-3"):
            signals.append("potential offering shelf activity")
        elif form_type == "DEF 14A":
            signals.append("proxy / governance filing")

        excerpt_lower = excerpt.lower()
        for phrase, label in (
            ("guidance", "guidance mentioned"),
            ("offering", "offering language present"),
            ("spin-off", "spin-off language present"),
            ("artificial intelligence", "AI language present"),
            ("demand", "demand language present"),
            ("restructuring", "restructuring language present"),
        ):
            if phrase in excerpt_lower:
                signals.append(label)
        return "; ".join(signals[:4]) if signals else "recent SEC filing detected"

    def _filing_signals(
        self,
        *,
        form_type: str,
        item_codes: str,
        description: str,
        excerpt: str,
    ) -> dict[str, Any]:
        text = " ".join(part for part in (description, excerpt) if part).lower()
        signals: list[str] = []
        risk_flags: list[str] = []
        score = 0.0

        if form_type == "8-K" and "2.02" in item_codes:
            signals.append("fresh earnings update")
        if form_type == "8-K" and "5.02" in item_codes:
            risk_flags.append("management change")
            score -= 0.35
        if form_type == "8-K" and "8.01" in item_codes:
            signals.append("business update")
        if form_type in {"10-Q", "10-K"}:
            signals.append("periodic results filed")
        if form_type == "4":
            if any(phrase in text for phrase in ("purchase", "acquired", "buy")):
                signals.append("insider buy filing")
                score += 0.65
            elif any(phrase in text for phrase in ("sale", "sold", "dispose", "disposed")):
                risk_flags.append("insider selling")
                score -= 0.45
            else:
                signals.append("insider filing")
        if form_type.startswith("13D"):
            signals.append("active ownership change")
            score += 0.55
        if form_type.startswith("13G"):
            signals.append("ownership disclosure")
            score += 0.25
        if form_type.startswith("S-3"):
            risk_flags.append("capital raise shelf")
            score -= 0.9
        if form_type == "DEF 14A":
            signals.append("proxy or governance update")

        phrase_map = (
            ("guidance raise", ("guidance raised", 0.85, False)),
            ("raises guidance", ("guidance raised", 0.85, False)),
            ("reaffirm", ("guidance reaffirmed", 0.4, False)),
            ("strong demand", ("strong demand commentary", 0.45, False)),
            ("spin-off", ("spin-off or separation language", 0.8, False)),
            ("separation", ("separation language", 0.45, False)),
            ("offering", ("offering language", -0.85, True)),
            ("public offering", ("public offering", -0.95, True)),
            ("at-the-market", ("ATM or financing program", -0.85, True)),
            ("restructuring", ("restructuring", -0.2, True)),
            ("weaker demand", ("weaker demand", -0.55, True)),
            ("soft demand", ("soft demand", -0.55, True)),
        )
        for needle, (label, weight, risk_flag) in phrase_map:
            if needle in text:
                if weight >= 0:
                    signals.append(label)
                else:
                    risk_flags.append(label)
                score += weight
                if risk_flag and label not in risk_flags:
                    risk_flags.append(label)

        return {
            "signals": self._dedupe_strings(signals)[:5],
            "risk_flags": self._dedupe_strings(risk_flags)[:5],
            "score": round(score, 2),
        }

    def _top_catalysts(self, filings: list[dict[str, Any]]) -> list[str]:
        ranked: list[tuple[float, str]] = []
        for filing in filings[:6]:
            filing_date = str(filing.get("filing_date") or "")[:10]
            form_type = str(filing.get("form_type") or "")
            for signal in filing.get("signals") or []:
                ranked.append((float(filing.get("signal_score") or 0.0), f"{form_type} {filing_date}: {signal}"))
            for risk_flag in filing.get("risk_flags") or []:
                ranked.append((abs(float(filing.get("signal_score") or 0.0)) + 0.1, f"{form_type} {filing_date}: risk {risk_flag}"))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return self._dedupe_strings([label for _score, label in ranked])[:5]

    def _quarterly_fact_series(
        self,
        namespace: dict[str, Any],
        fact_names: list[str],
        *,
        unit: str,
    ) -> list[dict[str, Any]]:
        entries = self._fact_entries(namespace, fact_names, unit=unit)
        periods: dict[str, dict[str, Any]] = {}
        for entry in sorted(entries, key=lambda row: row.get("filed") or ""):
            if entry.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
                continue
            try:
                start = date.fromisoformat(entry["start"])
                end = date.fromisoformat(entry["end"])
                filed = date.fromisoformat(entry["filed"])
            except (KeyError, TypeError, ValueError):
                continue
            # SEC duration facts contain quarters AND YTD/annual values with the same end.
            if not 70 <= (end - start).days <= 110 or filed > datetime.now(UTC).date():
                continue
            periods[end.isoformat()] = entry
        return sorted(periods.values(), key=lambda row: row["end"], reverse=True)

    def _fact_entries(
        self,
        namespace: dict[str, Any],
        fact_names: list[str],
        *,
        unit: str,
    ) -> list[dict[str, Any]]:
        for fact_name in fact_names:
            fact = namespace.get(fact_name) or {}
            values = ((fact.get("units") or {}).get(unit) or [])
            if not values:
                continue
            normalized: list[dict[str, Any]] = []
            for item in values:
                value = item.get("val")
                if value is None:
                    continue
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if not isfinite(numeric):
                    continue
                normalized.append(
                    {
                        "fact": fact_name,
                        "value": numeric,
                        "start": item.get("start"),
                        "end": item.get("end"),
                        "accession": item.get("accn"),
                        "fy": item.get("fy"),
                        "fp": item.get("fp"),
                        "filed": item.get("filed"),
                        "form": item.get("form"),
                    }
                )
            if normalized:
                return normalized
        return []

    def _series_yoy_growth(self, series: list[dict[str, Any]]) -> float | None:
        if len(series) < 2:
            return None
        try:
            latest_end = date.fromisoformat(series[0]["end"])
            prior = [row for row in series[1:] if 350 <= (latest_end - date.fromisoformat(row["end"])).days <= 380]
        except (KeyError, TypeError, ValueError):
            return None
        if not prior:
            return None
        return self._pct_change(float(series[0]["value"]), float(prior[0]["value"]))

    def _margin_from_series(
        self,
        numerator_series: list[dict[str, Any]],
        denominator_series: list[dict[str, Any]],
    ) -> float | None:
        if not numerator_series or not denominator_series:
            return None
        if (numerator_series[0].get("start"), numerator_series[0].get("end")) != (denominator_series[0].get("start"), denominator_series[0].get("end")):
            return None
        numerator = float(numerator_series[0].get("value") or 0.0)
        denominator = float(denominator_series[0].get("value") or 0.0)
        if denominator <= 0:
            return None
        return round((numerator / denominator) * 100.0, 2)

    def _latest_positive(self, series: list[dict[str, Any]]) -> bool | None:
        if not series:
            return None
        return bool(float(series[0].get("value") or 0.0) > 0)

    def _pct_change(self, current: float, previous: float) -> float | None:
        if previous == 0:
            return None
        return round(((current - previous) / abs(previous)) * 100.0, 2)

    def _safe_delta(self, current: float | None, previous: float | None) -> float | None:
        if current is None or previous is None:
            return None
        return round(current - previous, 2)

    def _dedupe_strings(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            if value and value not in seen:
                deduped.append(value)
                seen.add(value)
        return deduped

    def _parse_date(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).replace(tzinfo=UTC).replace(tzinfo=None)
        except ValueError:
            return None
