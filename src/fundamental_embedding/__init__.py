"""Shared point-in-time dataset contracts; discontinued model trainers removed."""

from .api import (
    EXPERT_FEATURES,
    PRICING_FEATURE_NAMES,
    FundamentalPricingDataset,
    FundamentalPricingSnapshot,
)
from .dataset import QuarterlyPricingDatasetBuilder
from .exposure import CompanyExposureEncoder, RobustFeatureTransformer
from .split_api import (
    FACTOR_FEATURE_DIRECTIONS,
    FACTOR_NAMES,
    CompanyExposureBatch,
    MarketPricingState,
    SplitPricingConfig,
    SplitPricingEvaluation,
)

__all__ = [
    "EXPERT_FEATURES",
    "FACTOR_FEATURE_DIRECTIONS",
    "FACTOR_NAMES",
    "PRICING_FEATURE_NAMES",
    "CompanyExposureBatch",
    "CompanyExposureEncoder",
    "FundamentalPricingDataset",
    "FundamentalPricingSnapshot",
    "MarketPricingState",
    "QuarterlyPricingDatasetBuilder",
    "RobustFeatureTransformer",
    "SplitPricingConfig",
    "SplitPricingEvaluation",
]
