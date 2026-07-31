"""
Hive Mind Ensemble Strategy
============================
The core "Hive Mind" concept: run multiple DE variants with multiple
objectives simultaneously, then combine their solutions via rank-weighted
consensus voting. This produces a robust allocation that isn't dependent
on any single optimizer or objective function.

Additional features:
- Cardinality constraints (min/max number of active positions)
- Position size limits (min/max weight per asset)
- Regime detection (bull/bear/sideways) with objective switching
- Ensemble consensus with configurable voting weights
"""

import numpy as np
from typing import List, Dict, Any
from dataclasses import dataclass, field, replace
import time

from de_engine import DEVariant
from portfolio_optimizer import (
    optimize_portfolio, PortfolioResult,
    generate_synthetic_returns, fetch_portfolio_returns,
    KRAKEN_PAIRS, PAIR_NAMES,
)


# ============================================================
# Regime Detection
# ============================================================

@dataclass
class MarketRegime:
    """Detected market regime with confidence."""
    regime: str           # "bull", "bear", "sideways", "crisis"
    confidence: float     # 0-1
    volatility: float     # annualized vol
    trend: float          # annualized return
    hurst: float          # Hurst exponent estimate
    details: Dict[str, float] = field(default_factory=dict)


def estimate_hurst(returns: np.ndarray, max_lag: int = 20) -> float:
    """
    Estimate Hurst exponent via rescaled range (R/S) analysis.

    H > 0.5: trending (persistent)
    H = 0.5: random walk
    H < 0.5: mean-reverting (anti-persistent)
    """
    n = len(returns)
    if n < max_lag * 2:
        return 0.5  # insufficient data

    lags = range(2, min(max_lag + 1, n // 2))
    rs_values = []

    for lag in lags:
        rs_list = []
        for start in range(0, n - lag, lag):
            segment = returns[start:start + lag]
            mean_seg = np.mean(segment)
            cumdev = np.cumsum(segment - mean_seg)
            R = np.max(cumdev) - np.min(cumdev)
            S = np.std(segment, ddof=1)
            if S > 1e-12:
                rs_list.append(R / S)
        if rs_list:
            rs_values.append((np.log(lag), np.log(np.mean(rs_list))))

    if len(rs_values) < 3:
        return 0.5

    # Linear regression in log-log space
    x = np.array([v[0] for v in rs_values])
    y = np.array([v[1] for v in rs_values])
    slope = np.polyfit(x, y, 1)[0]

    return float(np.clip(slope, 0.01, 0.99))


def detect_regime(returns: np.ndarray, window: int = 60) -> MarketRegime:
    """
    Detect current market regime from return data.

    Uses multiple signals:
    - Annualized return (trend direction)
    - Annualized volatility (crisis detection)
    - Hurst exponent (trending vs mean-reverting)
    - Drawdown depth (crisis severity)
    """
    # Use recent window for regime detection
    recent = returns[-window:] if len(returns) > window else returns

    # Equal-weighted portfolio for market signal
    n_assets = returns.shape[1]
    eq_returns = np.mean(recent, axis=1)

    ann_return = np.mean(eq_returns) * 365
    ann_vol = np.std(eq_returns, ddof=1) * np.sqrt(365)

    # Hurst on equal-weighted portfolio
    hurst = estimate_hurst(eq_returns)

    # Drawdown (Fix 5: cumprod instead of cumsum)
    equity = np.cumprod(1 + eq_returns)
    running_max = np.maximum.accumulate(equity)
    drawdowns = (equity - running_max) / running_max
    max_dd = float(np.min(drawdowns))

    # Regime classification
    details = {
        "ann_return": ann_return,
        "ann_vol": ann_vol,
        "max_dd": max_dd,
        "hurst": hurst,
    }

    # Fix 4: Recalibrated thresholds for crypto (old: ann_vol > 0.8 fires constantly)
    if ann_vol > 1.5 or max_dd < -0.30:
        regime = "crisis"
        confidence = min(1.0, ann_vol / 2.0)
    elif ann_return > 0.30 and hurst > 0.55:
        regime = "bull"
        # HM-1 (same treatment as bear): tanh instead of a hard min(1.0, x)
        # clamp. Expressed on the old linear score so the shape matches the
        # bear map exactly — the point where the old map pinned to 100%
        # (linear score 1.0) now reads ~0.58 and stronger evidence keeps
        # moving the needle instead of flatlining at a fake-precise 100%.
        confidence = float(np.tanh((ann_return / 0.8 * hurst / 0.7) / 1.5))
    elif ann_return < -0.20:
        regime = "bear"
        # HM-1: the old min(1.0, |ann_return|/0.5) clamp pinned confidence at
        # exactly 100% for anything past -50% annualized — a live -117% market
        # displayed "100%" as if it were a precise statistic. tanh saturates
        # smoothly and never reaches 1.0, so deeper selloffs still
        # discriminate: 0.5 -> ~0.58, 1.2 -> ~0.92, asymptote < 1.0.
        confidence = float(np.tanh(abs(ann_return) / 0.75))
    else:
        regime = "sideways"
        confidence = 1.0 - abs(ann_return) / 0.30

    return MarketRegime(
        regime=regime,
        confidence=float(np.clip(confidence, 0, 1)),
        volatility=ann_vol,
        trend=ann_return,
        hurst=hurst,
        details=details,
    )


# Fix 3: constrained_objective removed -- it was 80 lines of dead code
# that was never wired into optimize_portfolio. Constraints are applied
# post-hoc in run_hive_mind Step 6, which is the correct approach for
# DE-based optimization where the search space is [0,1]^n and we
# normalize + constrain the consensus output.


# ============================================================
# Hive Mind Ensemble
# ============================================================

@dataclass
class HiveMindConfig:
    """Configuration for the Hive Mind ensemble."""
    # DE variants to use
    variants: List[DEVariant] = field(default_factory=lambda: [
        DEVariant.JSO, DEVariant.L_SRTDE, DEVariant.L_SHADE
    ])
    # Objectives to optimize
    objectives: List[str] = field(default_factory=lambda: [
        "sharpe", "cvar", "risk_parity"
    ])
    # FEs per optimizer run
    max_fes: int = 30_000
    # Ensemble voting method: "rank_weighted", "inverse_obj", "equal"
    voting_method: str = "rank_weighted"
    # Cardinality constraints
    min_assets: int = 3
    max_assets: int = 8
    min_weight: float = 0.02
    max_weight: float = 0.40
    # Regime-adaptive objective weights
    regime_adaptive: bool = True
    # UPGRADE: Transaction cost modeling
    # Current weights for turnover cost calculation (None = first run)
    current_weights: np.ndarray = None
    # Cost per unit of turnover (0.001 = 0.1% per unit rebalanced)
    turnover_cost: float = 0.0
    # Random seed base
    seed: int = 42
    verbose: bool = True


@dataclass
class HiveMindResult:
    """Result from the Hive Mind ensemble."""
    consensus_weights: np.ndarray
    asset_names: List[str]
    regime: MarketRegime
    member_results: List[PortfolioResult]
    member_labels: List[str]
    voting_weights: np.ndarray

    # Consensus portfolio metrics
    annualized_return: float
    annualized_vol: float
    sharpe_ratio: float
    max_drawdown: float
    cvar_95: float
    n_active: int

    # Fix 8: Equal-weight benchmark
    benchmark_sharpe: float

    wall_time: float

    def summary(self) -> str:
        lines = [
            "",
            "+" + "=" * 58 + "+",
            "|" + "  HIVE MIND -- Ensemble Portfolio Consensus".center(58) + "|",
            "+" + "=" * 58 + "+",
            "",
            f"  Market Regime: {self.regime.regime.upper()} "
            f"(confidence={self.regime.confidence:.0%})",
            f"  Hurst: {self.regime.hurst:.3f}  "
            f"Vol: {self.regime.volatility*100:.1f}%  "
            f"Trend: {self.regime.trend*100:+.1f}%",
            "",
            "  --- Consensus Allocation ---",
        ]

        sorted_idx = np.argsort(-self.consensus_weights)
        for idx in sorted_idx:
            w = self.consensus_weights[idx]
            if w > 0.005:
                bar = "#" * int(w * 40)
                lines.append(
                    f"    {self.asset_names[idx]:>6s}: "
                    f"{w*100:5.1f}% {bar}"
                )

        lines.extend([
            "",
            "  --- Portfolio Metrics ---",
            f"    Ann. Return:    {self.annualized_return*100:+.2f}%",
            f"    Ann. Vol:       {self.annualized_vol*100:.2f}%",
            f"    Sharpe Ratio:   {self.sharpe_ratio:.4f}",
            f"    Max Drawdown:   {self.max_drawdown*100:.2f}%",
            f"    CVaR (95%):     {self.cvar_95*100:.2f}%",
            f"    Active Assets:  {self.n_active}",
            "",
            f"  --- Benchmark (Equal-Weight) ---",
            f"    EW Sharpe:      {self.benchmark_sharpe:.4f}",
            f"    vs Consensus:   {self.sharpe_ratio - self.benchmark_sharpe:+.4f}",
            "",
            "  --- Ensemble Members ---",
        ])

        for label, vw, mr in zip(self.member_labels, self.voting_weights,
                                  self.member_results):
            lines.append(
                f"    {label:>20s}  vote={vw:.3f}  "
                f"sharpe={mr.sharpe_ratio:.2f}"
            )

        lines.extend([
            "",
            f"  Wall time: {self.wall_time:.2f}s",
            "=" * 60,
        ])

        return "\n".join(lines)


def get_regime_objective_weights(regime: MarketRegime) -> Dict[str, float]:
    """
    Map regime to objective function weights for ensemble voting.

    Bull:     Emphasize Sharpe (trend capture) + diversification
    Bear:     Emphasize CVaR (tail protection) + risk parity
    Sideways: Balanced across all objectives
    Crisis:   Heavy CVaR + risk parity, minimal Sharpe
    """
    # UPGRADE: Added sortino and calmar weights per regime
    weights = {
        "bull": {"sharpe": 0.40, "cvar": 0.10, "mean_cvar": 0.05,
                 "max_div": 0.10, "risk_parity": 0.05,
                 "sortino": 0.20, "calmar": 0.10},
        "bear": {"sharpe": 0.05, "cvar": 0.25, "mean_cvar": 0.15,
                 "max_div": 0.05, "risk_parity": 0.15,
                 "sortino": 0.15, "calmar": 0.20},
        "sideways": {"sharpe": 0.20, "cvar": 0.15, "mean_cvar": 0.10,
                     "max_div": 0.10, "risk_parity": 0.15,
                     "sortino": 0.15, "calmar": 0.15},
        "crisis": {"sharpe": 0.05, "cvar": 0.30, "mean_cvar": 0.15,
                   "max_div": 0.05, "risk_parity": 0.15,
                   "sortino": 0.10, "calmar": 0.20},
    }
    return weights.get(regime.regime, weights["sideways"])


def run_hive_mind(
    returns: np.ndarray,
    asset_names: List[str],
    config: HiveMindConfig = None,
) -> HiveMindResult:
    """
    Run the full Hive Mind ensemble optimization.

    1. Detect market regime
    2. Run each (variant x objective) combination
    3. Score each member by its OWN objective's metric
    4. Combine via rank-based consensus voting
    5. Apply cardinality constraints to final allocation
    """
    if config is None:
        config = HiveMindConfig()

    t_start = time.perf_counter()
    n_assets = returns.shape[1]

    # --- Step 1: Regime Detection ---
    regime = detect_regime(returns)

    if config.verbose:
        print(f"\n{'='*60}")
        print(f"  HIVE MIND -- Running Ensemble Optimization")
        print(f"{'='*60}")
        print(f"  Regime: {regime.regime.upper()} "
              f"(conf={regime.confidence:.0%}, H={regime.hurst:.3f})")
        print(f"  Variants: {[v.value for v in config.variants]}")
        print(f"  Objectives: {config.objectives}")

    # --- Step 2: Get regime-adaptive weights ---
    if config.regime_adaptive:
        obj_weights = get_regime_objective_weights(regime)
    else:
        obj_weights = {obj: 1.0 / len(config.objectives)
                       for obj in config.objectives}

    # --- Step 3: Run all member optimizations ---
    member_results: List[PortfolioResult] = []
    member_labels: List[str] = []
    member_obj_weights: List[float] = []

    run_idx = 0
    for variant in config.variants:
        for objective in config.objectives:
            label = f"{variant.value}/{objective}"

            if config.verbose:
                print(f"  Running {label}...", end=" ", flush=True)

            # Fix 7: Hash-based seed for better separation
            seed = config.seed + hash((variant.value, objective)) % 10000

            # UPGRADE: Pass current_weights and turnover_cost for cost-aware optimization
            result = optimize_portfolio(
                returns, asset_names,
                objective=objective,
                variant=variant,
                max_fes=config.max_fes,
                seed=seed,
                verbose=False,
                current_weights=config.current_weights,
                turnover_cost=config.turnover_cost,
            )

            member_results.append(result)
            member_labels.append(label)
            member_obj_weights.append(obj_weights.get(objective, 0.1))

            if config.verbose:
                print(f"Sharpe={result.sharpe_ratio:.2f}  "
                      f"MaxDD={result.max_drawdown*100:.1f}%")

            run_idx += 1

    # --- Step 4: Compute voting weights ---
    n_members = len(member_results)

    if config.voting_method == "rank_weighted":
        # Fix 1: Rank each member by its OWN objective's metric (not all by Sharpe)
        scores = []
        for mr in member_results:
            if mr.objective_name == "sharpe":
                scores.append(mr.sharpe_ratio)  # higher = better
            elif mr.objective_name == "cvar":
                scores.append(-mr.cvar_95)  # lower cvar = better, negate for ranking
            elif mr.objective_name == "risk_parity":
                scores.append(-mr.objective_value)  # lower loss = better, negate
            elif mr.objective_name == "max_div":
                scores.append(-mr.objective_value)  # lower (more negative) = better
            elif mr.objective_name == "mean_cvar":
                scores.append(mr.sharpe_ratio)  # balanced metric
            # UPGRADE: Sortino scoring — higher is better (like Sharpe)
            elif mr.objective_name == "sortino":
                scores.append(-mr.objective_value)  # neg_sortino minimized, negate for ranking
            # UPGRADE: Calmar scoring — higher is better
            elif mr.objective_name == "calmar":
                scores.append(-mr.objective_value)  # neg_calmar minimized, negate for ranking
            else:
                scores.append(mr.sharpe_ratio)  # fallback

        scores = np.array(scores)
        ranks = np.argsort(np.argsort(-scores))  # 0 = best
        rank_scores = 1.0 / (ranks + 1)

        obj_w = np.array(member_obj_weights)
        raw_weights = rank_scores * obj_w

    elif config.voting_method == "inverse_obj":
        # Weight by inverse of objective value (lower obj = better)
        obj_vals = np.array([abs(r.objective_value) for r in member_results])
        obj_vals = np.clip(obj_vals, 1e-10, None)
        raw_weights = (1.0 / obj_vals) * np.array(member_obj_weights)

    else:  # equal
        raw_weights = np.array(member_obj_weights)

    voting_weights = raw_weights / np.sum(raw_weights)

    # --- Step 5: Rank-based consensus (not naive weight averaging) ---
    # Fix 2: Instead of averaging weight vectors (which blends incompatible
    # objectives), use rank-based averaging: for each asset, average its RANK
    # across all members (weighted by voting weights), then convert ranks
    # back to weights.
    all_weights = np.array([r.weights for r in member_results])
    # Convert weights to ranks for each member
    member_ranks = np.zeros_like(all_weights)
    for m in range(n_members):
        # argsort of argsort gives ranks; negate weights so highest gets rank 0
        member_ranks[m] = np.argsort(np.argsort(-all_weights[m]))

    # Weighted average of ranks
    avg_ranks = np.average(member_ranks, axis=0, weights=voting_weights)

    # Convert average ranks to weights: lower average rank = higher weight
    # Use inverse-rank weighting
    inv_ranks = 1.0 / (avg_ranks + 1)
    consensus = inv_ranks / np.sum(inv_ranks)

    # --- Step 6: Apply cardinality constraints (post-hoc) ---
    # NOTE: Constraints are applied after consensus, not during optimization.
    # This is intentional -- DE optimizes unconstrained objectives, and we
    # enforce portfolio structure requirements on the blended output.

    # Zero out positions below min_weight
    consensus[consensus < config.min_weight] = 0

    # Cap at max_weight
    consensus = np.minimum(consensus, config.max_weight)

    # Enforce min_assets by including top-ranked assets if needed
    active = np.sum(consensus > 0)
    if active < config.min_assets:
        # Use consensus rank order (from avg_ranks) instead of raw weights
        sorted_idx = np.argsort(avg_ranks)  # lowest avg rank = most agreed upon
        for idx in sorted_idx:
            if consensus[idx] == 0:
                consensus[idx] = config.min_weight
            active = np.sum(consensus > 0)
            if active >= config.min_assets:
                break

    # Enforce max_assets by zeroing smallest if needed
    if np.sum(consensus > 0) > config.max_assets:
        sorted_idx = np.argsort(consensus)
        for idx in sorted_idx:
            if consensus[idx] > 0 and np.sum(consensus > 0) > config.max_assets:
                consensus[idx] = 0

    # Final renormalization
    total = np.sum(consensus)
    if total > 0:
        consensus /= total
    else:
        consensus = np.ones(n_assets) / n_assets

    # --- Step 7: Compute consensus portfolio metrics ---
    port_returns = returns @ consensus
    ann_return = np.mean(port_returns) * 365
    ann_vol = np.std(port_returns, ddof=1) * np.sqrt(365)
    sharpe = ann_return / ann_vol if ann_vol > 0 else 0.0

    # Fix 5: cumprod-based max drawdown
    equity = np.cumprod(1 + port_returns)
    running_max = np.maximum.accumulate(equity)
    drawdowns = (equity - running_max) / running_max
    max_dd = float(np.min(drawdowns))

    var_5 = np.percentile(port_returns, 5)
    tail = port_returns[port_returns <= var_5]
    cvar_95 = float(-np.mean(tail)) if len(tail) > 0 else float(-var_5)

    # Fix 8: Equal-weight benchmark comparison
    eq_weights = np.ones(n_assets) / n_assets
    eq_port_returns = returns @ eq_weights
    eq_ann_return = np.mean(eq_port_returns) * 365
    eq_ann_vol = np.std(eq_port_returns, ddof=1) * np.sqrt(365)
    eq_sharpe = eq_ann_return / eq_ann_vol if eq_ann_vol > 0 else 0.0

    wall_time = time.perf_counter() - t_start

    result = HiveMindResult(
        consensus_weights=consensus,
        asset_names=asset_names,
        regime=regime,
        member_results=member_results,
        member_labels=member_labels,
        voting_weights=voting_weights,
        annualized_return=ann_return,
        annualized_vol=ann_vol,
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        cvar_95=cvar_95,
        n_active=int(np.sum(consensus > 0.005)),
        benchmark_sharpe=eq_sharpe,
        wall_time=wall_time,
    )

    if config.verbose:
        print(result.summary())

    return result


def hive_mind_walk_forward(
    returns: np.ndarray,
    asset_names: List[str],
    train_window: int = 90,
    test_window: int = 30,
    purge_gap: int = 0,
    config: HiveMindConfig = None,
) -> Dict[str, Any]:
    """
    Walk-forward backtest of the Hive Mind ensemble strategy.

    UPGRADE: Added purge_gap parameter. When > 0, inserts a gap of
    purge_gap days between training and test sets to prevent lookahead
    bias from autocorrelated returns. Even a 5-day purge gap makes a
    meaningful difference for daily crypto returns.
    """
    if config is None:
        config = HiveMindConfig(verbose=False)

    T, N = returns.shape
    # UPGRADE: Account for purge gap in period calculation
    effective_step = test_window + purge_gap
    n_periods = (T - train_window - purge_gap) // test_window

    if n_periods < 2:
        raise ValueError("Insufficient data for walk-forward")

    purge_label = f"  Purge gap: {purge_gap}d" if purge_gap > 0 else ""

    print(f"\n{'='*60}")
    print(f"  HIVE MIND Walk-Forward Backtest")
    print(f"  {n_periods} periods x {len(config.variants)*len(config.objectives)} members each")
    if purge_gap > 0:
        print(f"  Purge gap: {purge_gap} days (lookahead bias prevention)")
    print(f"{'='*60}")

    oos_returns = []
    period_data = []

    for period in range(n_periods):
        train_start = period * test_window
        train_end = train_start + train_window
        # UPGRADE: Insert purge gap between train and test
        test_start = train_end + purge_gap
        test_end = min(test_start + test_window, T)

        train = returns[train_start:train_end]
        test = returns[test_start:test_end]

        if len(test) == 0:
            break

        # Fix 6: Use dataclasses.replace instead of manual field copy
        config_period = replace(config, seed=config.seed + period * 100, verbose=False)

        hm_result = run_hive_mind(train, asset_names, config_period)

        # OOS performance
        oos_port = test @ hm_result.consensus_weights
        oos_returns.extend(oos_port.tolist())

        oos_ret = float(np.sum(oos_port))
        oos_vol = float(np.std(oos_port, ddof=1) * np.sqrt(365))
        oos_sharpe = (np.mean(oos_port) / np.std(oos_port, ddof=1) * np.sqrt(365)
                      if np.std(oos_port) > 0 else 0)

        period_data.append({
            "period": period + 1,
            "regime": hm_result.regime.regime,
            "oos_return": oos_ret,
            "oos_sharpe": float(oos_sharpe),
            "n_active": hm_result.n_active,
        })

        print(f"  Period {period+1:>2d}: regime={hm_result.regime.regime:>8s}  "
              f"OOS={oos_ret*100:+.2f}%  Sharpe={oos_sharpe:.2f}  "
              f"Active={hm_result.n_active}")

    # Aggregate
    oos_arr = np.array(oos_returns)
    total_return = float(np.sum(oos_arr))
    ann_return = float(np.mean(oos_arr) * 365)
    ann_vol = float(np.std(oos_arr, ddof=1) * np.sqrt(365))
    oos_sharpe = ann_return / ann_vol if ann_vol > 0 else 0

    # Fix 5: cumprod-based max drawdown in walk-forward too
    equity = np.cumprod(1 + oos_arr)
    running_max = np.maximum.accumulate(equity)
    drawdowns = (equity - running_max) / running_max
    max_dd = float(np.min(drawdowns))

    print(f"\n{'-'*60}")
    print(f"  AGGREGATE: Return={total_return*100:+.2f}%  "
          f"Sharpe={oos_sharpe:.2f}  MaxDD={max_dd*100:.2f}%")
    print(f"{'='*60}")

    return {
        "total_return": total_return,
        "ann_return": ann_return,
        "ann_vol": ann_vol,
        "oos_sharpe": oos_sharpe,
        "max_dd": max_dd,
        "purge_gap": purge_gap,
        "periods": period_data,
    }


# UPGRADE: Purged k-fold cross-validation for more robust OOS estimation
def hive_mind_purged_cv(
    returns: np.ndarray,
    asset_names: List[str],
    n_folds: int = 5,
    purge_gap: int = 5,
    config: HiveMindConfig = None,
) -> Dict[str, Any]:
    """
    Purged k-fold cross-validation of the Hive Mind ensemble strategy.

    Unlike walk-forward which only moves forward, purged CV uses all data
    for both training and testing across k folds. A configurable purge gap
    between train and test sets prevents lookahead bias from autocorrelated
    returns.

    For each fold:
    1. Hold out fold i as test set
    2. Remove purge_gap days on each side of the test set from training
    3. Train on remaining data
    4. Evaluate on test fold

    Args:
        returns: (T, N) return matrix
        asset_names: asset name list
        n_folds: number of CV folds (default 5)
        purge_gap: days to purge between train/test (default 5)
        config: HiveMindConfig

    Returns:
        Dict with aggregate OOS metrics across all folds
    """
    if config is None:
        config = HiveMindConfig(verbose=False)

    T, N = returns.shape
    fold_size = T // n_folds

    if fold_size < 30:
        raise ValueError(f"Fold size too small ({fold_size}d). Need at least 30 days per fold.")

    print(f"\n{'='*60}")
    print(f"  HIVE MIND Purged {n_folds}-Fold Cross-Validation")
    print(f"  Fold size: {fold_size}d, Purge gap: {purge_gap}d")
    print(f"  {len(config.variants)*len(config.objectives)} ensemble members per fold")
    print(f"{'='*60}")

    all_oos_returns = []
    fold_data = []

    for fold in range(n_folds):
        test_start = fold * fold_size
        test_end = min(test_start + fold_size, T)

        # UPGRADE: Purge gap — remove purge_gap days on each side of test from train
        purge_start = max(0, test_start - purge_gap)
        purge_end = min(T, test_end + purge_gap)

        # Build training set: everything outside the purged zone
        train_indices = list(range(0, purge_start)) + list(range(purge_end, T))

        if len(train_indices) < 60:
            print(f"  Fold {fold+1}: SKIPPED (insufficient training data: {len(train_indices)}d)")
            continue

        train = returns[train_indices]
        test = returns[test_start:test_end]

        config_fold = replace(config, seed=config.seed + fold * 200, verbose=False)
        hm_result = run_hive_mind(train, asset_names, config_fold)

        # OOS performance
        oos_port = test @ hm_result.consensus_weights
        all_oos_returns.extend(oos_port.tolist())

        oos_ret = float(np.sum(oos_port))
        oos_sharpe = (np.mean(oos_port) / np.std(oos_port, ddof=1) * np.sqrt(365)
                      if np.std(oos_port) > 0 else 0)

        fold_data.append({
            "fold": fold + 1,
            "regime": hm_result.regime.regime,
            "oos_return": oos_ret,
            "oos_sharpe": float(oos_sharpe),
            "n_active": hm_result.n_active,
            "train_days": len(train_indices),
            "test_days": len(test),
            "purged_days": purge_gap * 2,
        })

        print(f"  Fold {fold+1:>2d}: regime={hm_result.regime.regime:>8s}  "
              f"OOS={oos_ret*100:+.2f}%  Sharpe={oos_sharpe:.2f}  "
              f"Active={hm_result.n_active}  "
              f"Train={len(train_indices)}d  Test={len(test)}d")

    if not all_oos_returns:
        raise ValueError("No folds completed successfully")

    # Aggregate
    oos_arr = np.array(all_oos_returns)
    total_return = float(np.sum(oos_arr))
    ann_return = float(np.mean(oos_arr) * 365)
    ann_vol = float(np.std(oos_arr, ddof=1) * np.sqrt(365))
    oos_sharpe = ann_return / ann_vol if ann_vol > 0 else 0

    equity = np.cumprod(1 + oos_arr)
    running_max = np.maximum.accumulate(equity)
    drawdowns = (equity - running_max) / running_max
    max_dd = float(np.min(drawdowns))

    print(f"\n{'-'*60}")
    print(f"  AGGREGATE ({n_folds}-fold purged CV): "
          f"Return={total_return*100:+.2f}%  "
          f"Sharpe={oos_sharpe:.2f}  MaxDD={max_dd*100:.2f}%")
    print(f"{'='*60}")

    return {
        "total_return": total_return,
        "ann_return": ann_return,
        "ann_vol": ann_vol,
        "oos_sharpe": oos_sharpe,
        "max_dd": max_dd,
        "n_folds": n_folds,
        "purge_gap": purge_gap,
        "folds": fold_data,
    }


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  HIVE MIND -- Ensemble DE Portfolio Strategy")
    print("=" * 60)

    # --- Synthetic demo ---
    returns = generate_synthetic_returns(n_assets=8, n_days=365, seed=42)
    names = ["BTC", "ETH", "SOL", "ADA", "DOT", "AVAX", "LINK", "ATOM"]

    # Run single Hive Mind optimization
    # UPGRADE: L-SHADE re-enabled in all configs
    config = HiveMindConfig(
        variants=[DEVariant.JSO, DEVariant.L_SRTDE, DEVariant.L_SHADE],
        objectives=["sharpe", "cvar", "risk_parity"],
        max_fes=20_000,
        min_assets=3,
        max_assets=6,
        min_weight=0.03,
        max_weight=0.35,
        regime_adaptive=True,
        seed=42,
    )

    result = run_hive_mind(returns, names, config)

    # Walk-forward backtest
    # UPGRADE: L-SHADE re-enabled in all configs
    wf_config = HiveMindConfig(
        variants=[DEVariant.JSO, DEVariant.L_SRTDE, DEVariant.L_SHADE],
        objectives=["sharpe", "cvar"],
        max_fes=15_000,
        min_assets=3,
        max_assets=6,
        verbose=False,
        seed=42,
    )

    wf_results = hive_mind_walk_forward(
        returns, names,
        train_window=90, test_window=30,
        config=wf_config,
    )

    # UPGRADE: Walk-forward with 5-day purge gap
    print("\n  --- Walk-Forward with Purge Gap ---")
    wf_purged_results = hive_mind_walk_forward(
        returns, names,
        train_window=90, test_window=30,
        purge_gap=5,
        config=wf_config,
    )

    # UPGRADE: Purged 5-fold cross-validation
    cv_config = HiveMindConfig(
        variants=[DEVariant.JSO, DEVariant.L_SRTDE, DEVariant.L_SHADE],
        objectives=["sharpe", "cvar"],
        max_fes=15_000,
        verbose=False,
        seed=42,
    )
    cv_results = hive_mind_purged_cv(
        returns, names,
        n_folds=5, purge_gap=5,
        config=cv_config,
    )

    # --- Live Kraken demo ---
    print("\n> Fetching live Kraken data...")
    live_returns, valid_pairs = fetch_portfolio_returns(
        KRAKEN_PAIRS[:8], lookback_days=180
    )

    if live_returns is not None and len(valid_pairs) >= 4:
        live_names = [PAIR_NAMES.get(p, p) for p in valid_pairs]

        # UPGRADE: L-SHADE re-enabled in all configs
        live_config = HiveMindConfig(
            variants=[DEVariant.JSO, DEVariant.L_SRTDE, DEVariant.L_SHADE],
            objectives=["sharpe", "cvar", "risk_parity"],
            max_fes=25_000,
            min_assets=3,
            max_assets=6,
            seed=42,
        )

        live_result = run_hive_mind(live_returns, live_names, live_config)
    else:
        print("  Insufficient live data")

    print("\nHive Mind complete.")
