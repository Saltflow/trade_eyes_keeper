#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FinancialReportManager behavior tests (force analyze & limits)
"""

import pytest

from src.financial_report_manager import FinancialReportManager


def _build_config(force=True, max_force=None, max_per_run=3):
    return {
        "stocks": ["600000", "600001", "600002", "600003"],
        "financial_reports": {
            "enable": True,
            "auto_enable": False,
            "conditional_enable": False,
            "force_analyze": force,
            "max_stocks_per_run": max_per_run,
            "max_force_stocks": max_force,
        },
    }


def test_force_analyze_uses_all_configured_stocks():
    config = _build_config(force=True, max_force=10)
    manager = FinancialReportManager(config)

    should_analyze, stocks = manager.should_analyze_financial_reports()

    assert should_analyze is True
    assert stocks == config["stocks"]


def test_analyze_financial_reports_respects_force_limit(monkeypatch):
    config = _build_config(force=True, max_force=2)
    manager = FinancialReportManager(config)

    called = []

    def _fake_analyze(stock_code):
        called.append(stock_code)
        return [
            {
                "stock_code": stock_code,
                "report_type": "annual",
                "period_date": "2024-12-31",
                "analysis": {"overall_assessment": "ok"},
            }
        ]

    monkeypatch.setattr(manager, "_analyze_stock", _fake_analyze)

    results = manager.analyze_financial_reports(config["stocks"])

    assert called == config["stocks"][:2]
    assert set(results.keys()) == set(config["stocks"][:2])
    assert all(reports for reports in results.values())


def test_non_force_mode_uses_regular_limit(monkeypatch):
    config = _build_config(force=False, max_force=10, max_per_run=2)
    manager = FinancialReportManager(config)

    called = []

    def _fake_analyze(stock_code):
        called.append(stock_code)
        return [
            {
                "stock_code": stock_code,
                "report_type": "annual",
                "period_date": "2024-12-31",
                "analysis": {"overall_assessment": "ok"},
            }
        ]

    monkeypatch.setattr(manager, "_analyze_stock", _fake_analyze)

    results = manager.analyze_financial_reports(config["stocks"])

    assert called == config["stocks"][:2]
    assert set(results.keys()) == set(config["stocks"][:2])
    assert all(reports for reports in results.values())
