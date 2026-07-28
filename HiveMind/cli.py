#!/usr/bin/env python3
"""
Hive Mind CLI — Command-line interface for DE portfolio optimization.

Usage:
    python cli.py optimize [--pairs BTC,ETH,SOL] [--objective sharpe]
    python cli.py ensemble [--pairs BTC,ETH,SOL,ADA,DOT,AVAX]
    python cli.py backtest [--train 90] [--test 30]
    python cli.py benchmark [--dim 10] [--fes 50000]
    python cli.py regime [--pairs BTC,ETH,SOL]
    python cli.py dashboard [--port 8073]
    python cli.py audit
"""

import os
import argparse
import json
import time

from de_engine import (
    DifferentialEvolution, DEConfig, DEVariant,
    sphere, rastrigin, rosenbrock, ackley, schwefel,
)
from portfolio_optimizer import (
    optimize_portfolio, walk_forward_backtest,
    fetch_portfolio_returns, generate_synthetic_returns,
    PAIR_NAMES,
)
from hive_mind import (
    run_hive_mind, hive_mind_walk_forward, hive_mind_purged_cv,
    detect_regime, HiveMindConfig,
)


def resolve_pairs(pair_str: str) -> list:
    """Convert 'BTC,ETH,SOL' to Kraken pair format, excluding blacklisted pairs."""
    from portfolio_optimizer import _is_blacklisted
    name_to_pair = {v: k for k, v in PAIR_NAMES.items()}
    names = [n.strip().upper() for n in pair_str.split(",")]
    pairs = []
    for name in names:
        if name in name_to_pair:
            kraken_pair = name_to_pair[name]
        elif name + "USD" in PAIR_NAMES:
            kraken_pair = name + "USD"
        else:
            print(f"  WARNING: Unknown pair: {name}, skipping")
            continue
        if _is_blacklisted(kraken_pair):
            print(f"  BLACKLISTED: {name} ({kraken_pair}) — 0% WR across fleet, excluded")
            continue
        pairs.append(kraken_pair)
    return pairs


def cmd_optimize(args):
    """Single-objective portfolio optimization."""
    if args.quiet:
        verbose = False
    else:
        verbose = True

    print("=" * 60)
    print("  HIVE MIND -- Single Objective Optimization")
    print("=" * 60)

    variant = DEVariant(args.variant)

    if args.synthetic:
        returns = generate_synthetic_returns(n_assets=8, n_days=365, seed=args.seed)
        names = ["BTC", "ETH", "SOL", "ADA", "DOT", "AVAX", "LINK", "ATOM"]
        if not args.quiet:
            print(f"  Using synthetic data (8 assets, 365 days)")
    else:
        pairs = resolve_pairs(args.pairs)
        if not pairs:
            print("  ERROR: No valid pairs specified")
            return

        if not args.quiet:
            print(f"  Fetching {len(pairs)} pairs from Kraken...")
        returns, valid_pairs = fetch_portfolio_returns(pairs, lookback_days=args.days)

        if returns is None or len(valid_pairs) < 2:
            print("  ERROR: Could not fetch sufficient data")
            return

        names = [PAIR_NAMES.get(p, p) for p in valid_pairs]

    result = optimize_portfolio(
        returns, names,
        objective=args.objective,
        variant=variant,
        max_fes=args.fes,
        seed=args.seed,
        verbose=verbose,
    )

    if not args.quiet:
        print(result.summary())

    if args.save:
        output = {
            "objective": args.objective,
            "variant": args.variant,
            "weights": {name: float(w) for name, w in zip(names, result.weights)},
            "sharpe": result.sharpe_ratio,
            "ann_return": result.annualized_return,
            "ann_vol": result.annualized_vol,
            "max_drawdown": result.max_drawdown,
            "cvar_95": result.cvar_95,
            "fes_used": result.de_result.fes_used,
            "wall_time": result.de_result.wall_time,
        }
        with open(args.save, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\n  Saved results to: {args.save}")


def cmd_ensemble(args):
    """Hive Mind ensemble optimization."""
    variant_map = {
        "jso": DEVariant.JSO,
        "l_srtde": DEVariant.L_SRTDE,
        "l_shade": DEVariant.L_SHADE,
    }

    # Validate variants
    invalid = [v.strip() for v in args.variants.split(",") if v.strip() not in variant_map]
    if invalid:
        print(f"  ERROR: Unknown variants: {invalid}. Valid: {list(variant_map.keys())}")
        return
    variants = [variant_map[v.strip()] for v in args.variants.split(",")
                if v.strip() in variant_map]
    if not variants:
        print("  ERROR: No valid variants specified")
        return

    # Validate objectives
    valid_objectives = {"sharpe", "cvar", "mean_cvar", "max_div", "risk_parity",
                         "sortino", "calmar"}  # UPGRADE: new objectives
    objectives = [o.strip() for o in args.objectives.split(",")]
    invalid_obj = [o for o in objectives if o not in valid_objectives]
    if invalid_obj:
        print(f"  ERROR: Unknown objectives: {invalid_obj}. Valid: {sorted(valid_objectives)}")
        return

    if args.synthetic:
        returns = generate_synthetic_returns(n_assets=8, n_days=365, seed=args.seed)
        names = ["BTC", "ETH", "SOL", "ADA", "DOT", "AVAX", "LINK", "ATOM"]
    else:
        pairs = resolve_pairs(args.pairs)
        returns, valid_pairs = fetch_portfolio_returns(pairs, lookback_days=args.days)
        if returns is None:
            print("  ERROR: Could not fetch data")
            return
        names = [PAIR_NAMES.get(p, p) for p in valid_pairs]

    config = HiveMindConfig(
        variants=variants,
        objectives=objectives,
        max_fes=args.fes,
        min_assets=args.min_assets,
        max_assets=args.max_assets,
        min_weight=args.min_weight,
        max_weight=args.max_weight,
        regime_adaptive=not args.no_regime,
        seed=args.seed,
        verbose=not args.quiet,
    )

    result = run_hive_mind(returns, names, config)

    if args.save:
        output = {
            "regime": result.regime.regime,
            "regime_confidence": result.regime.confidence,
            "hurst": result.regime.hurst,
            "weights": {name: float(w) for name, w in
                       zip(names, result.consensus_weights)},
            "sharpe": result.sharpe_ratio,
            "ann_return": result.annualized_return,
            "ann_vol": result.annualized_vol,
            "max_drawdown": result.max_drawdown,
            "n_active": result.n_active,
            "n_members": len(result.member_results),
            "wall_time": result.wall_time,
        }
        with open(args.save, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\n  Saved to: {args.save}")


def cmd_backtest(args):
    """Walk-forward backtest."""
    # Print expected period count
    if args.synthetic:
        expected_periods = (360 - args.train) // args.test  # synthetic uses 360 days
    else:
        expected_periods = (args.days - args.train) // args.test
    if not args.quiet:
        print(f"  Expected periods: ~{expected_periods}")

    if args.synthetic:
        returns = generate_synthetic_returns(n_assets=8, n_days=360, seed=args.seed)
        names = ["BTC", "ETH", "SOL", "ADA", "DOT", "AVAX", "LINK", "ATOM"]
    else:
        pairs = resolve_pairs(args.pairs)
        returns, valid_pairs = fetch_portfolio_returns(pairs, lookback_days=args.days)
        if returns is None:
            print("  ERROR: Could not fetch data")
            return
        names = [PAIR_NAMES.get(p, p) for p in valid_pairs]

    # UPGRADE: Purged cross-validation mode
    if hasattr(args, 'cv') and args.cv:
        config = HiveMindConfig(
            variants=[DEVariant.JSO, DEVariant.L_SRTDE, DEVariant.L_SHADE],
            objectives=["sharpe", "cvar"],
            max_fes=args.fes,
            verbose=not args.quiet,
            seed=args.seed,
        )
        results = hive_mind_purged_cv(
            returns, names,
            n_folds=args.folds,
            purge_gap=args.purge,
            config=config,
        )
    elif args.ensemble:
        # Warn when --ensemble ignores single-objective flags
        if args.objective != "sharpe" or args.variant != "jso":
            print("  NOTE: --objective and --variant are ignored in --ensemble mode")

        config = HiveMindConfig(
            variants=[DEVariant.JSO, DEVariant.L_SRTDE, DEVariant.L_SHADE],
            objectives=["sharpe", "cvar"],
            max_fes=args.fes,
            verbose=not args.quiet,
            seed=args.seed,
        )
        results = hive_mind_walk_forward(
            returns, names,
            train_window=args.train,
            test_window=args.test,
            purge_gap=args.purge,
            config=config,
        )
    else:
        results = walk_forward_backtest(
            returns, names,
            train_window=args.train,
            test_window=args.test,
            objective=args.objective,
            variant=DEVariant(args.variant),
            max_fes=args.fes,
            seed=args.seed,
        )

    if args.save:
        with open(args.save, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n  Saved to: {args.save}")


def cmd_benchmark(args):
    """Run DE benchmark validation."""
    print("=" * 60)
    print("  DE Engine Benchmark Suite")
    print("=" * 60)

    benchmarks = [
        ("Sphere",     sphere,     [(-100, 100)]),
        ("Rastrigin",  rastrigin,  [(-5.12, 5.12)]),
        ("Rosenbrock", rosenbrock, [(-5, 10)]),
        ("Ackley",     ackley,     [(-32.768, 32.768)]),
        ("Schwefel",   schwefel,   [(-500, 500)]),
    ]

    variants = [DEVariant.JSO, DEVariant.L_SRTDE, DEVariant.L_SHADE]

    for name, func, bound_template in benchmarks:
        bounds = bound_template * args.dim
        print(f"\n{'-'*60}")
        print(f"  {name} (D={args.dim})")
        print(f"{'-'*60}")

        for variant in variants:
            config = DEConfig(
                variant=variant,
                max_fes=args.fes,
                seed=args.seed,
            )
            de = DifferentialEvolution(config)
            result = de.optimize(func, bounds)

            if not args.quiet:
                print(f"  {variant.value:>10s}: best={result.best_fitness:.2e}  "
                      f"FEs={result.fes_used}  time={result.wall_time:.2f}s")

    if not args.quiet:
        print(f"\n{'='*60}")


def cmd_regime(args):
    """Detect current market regime."""
    if args.synthetic:
        returns = generate_synthetic_returns(n_assets=8, n_days=365, seed=args.seed)
        names = ["BTC", "ETH", "SOL", "ADA", "DOT", "AVAX", "LINK", "ATOM"]
    else:
        pairs = resolve_pairs(args.pairs)
        returns, valid_pairs = fetch_portfolio_returns(pairs, lookback_days=args.days)
        if returns is None:
            print("  ERROR: Could not fetch data")
            return
        names = [PAIR_NAMES.get(p, p) for p in valid_pairs]

    regime = detect_regime(returns)

    if not args.quiet:
        print(f"\n{'='*60}")
        print(f"  Market Regime Detection")
        print(f"{'='*60}")
        print(f"  Regime:     {regime.regime.upper()}")
        print(f"  Confidence: {regime.confidence:.0%}")
        print(f"  Hurst:      {regime.hurst:.4f} ", end="")
        if regime.hurst > 0.55:
            print("(trending)")
        elif regime.hurst < 0.45:
            print("(mean-reverting)")
        else:
            print("(random walk)")
        print(f"  Ann. Vol:   {regime.volatility*100:.1f}%")
        print(f"  Trend:      {regime.trend*100:+.1f}%")
        print(f"{'='*60}")


def cmd_audit(args):
    """Display the Grok document audit."""
    audit_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AUDIT.md")
    try:
        with open(audit_path, "r") as f:
            print(f.read())
    except FileNotFoundError:
        print(f"  AUDIT.md not found at {audit_path}")


REOPTIMIZE_INTERVAL = 1800  # 30 minutes between re-optimizations


def _build_snapshot(result, names, variants, objectives, args, backtest_data=None):
    """Build JSON-serializable snapshot from optimization result."""
    import math

    def safe_float(v):
        if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
            return 0.0
        return v

    return {
        "timestamp": time.time(),
        "mode": "ensemble",
        "status": "complete",
        "regime": {
            "regime": result.regime.regime,
            "confidence": round(result.regime.confidence, 3),
            "volatility": round(result.regime.volatility, 4),
            "trend": round(result.regime.trend, 4),
            "hurst": round(result.regime.hurst, 4),
        },
        "consensus": {
            "weights": {name: round(float(w), 6) for name, w in zip(names, result.consensus_weights)},
            "annualized_return": safe_float(round(result.annualized_return, 6)),
            "annualized_vol": safe_float(round(result.annualized_vol, 6)),
            "sharpe_ratio": safe_float(round(result.sharpe_ratio, 4)),
            "max_drawdown": safe_float(round(result.max_drawdown, 6)),
            "cvar_95": safe_float(round(result.cvar_95, 6)),
            "n_active": result.n_active,
        },
        "members": [
            {
                "label": label,
                "variant": label.split("/")[0] if "/" in label else label,
                "objective": label.split("/")[1] if "/" in label else label,
                "weights": {name: round(float(w), 6) for name, w in zip(names, mr.weights)},
                "sharpe_ratio": safe_float(round(mr.sharpe_ratio, 4)),
                "annualized_return": safe_float(round(mr.annualized_return, 6)),
                "annualized_vol": safe_float(round(mr.annualized_vol, 6)),
                "max_drawdown": safe_float(round(mr.max_drawdown, 6)),
                "cvar_95": safe_float(round(mr.cvar_95, 6)),
                "voting_weight": round(float(vw), 4),
                "convergence": [safe_float(round(c, 8)) for c in mr.de_result.convergence],
                "fes_used": mr.de_result.fes_used,
                "wall_time": round(mr.de_result.wall_time, 3),
            }
            for label, mr, vw in zip(result.member_labels, result.member_results, result.voting_weights)
        ],
        "backtest": backtest_data,
        "benchmark": {"results": []},
        "config": {
            "variants": [v.value for v in variants],
            "objectives": objectives,
            "max_fes": args.fes,
            "min_assets": 3,
            "max_assets": 8,
            "risk_per_trade_pct": 1.0,
            "default_equity": 10000,
        },
        "logs": [],
    }


def cmd_dashboard(args):
    """Run optimization and serve results via web dashboard. Re-optimizes every 30 min."""
    import threading
    import math
    import sys as _sys
    from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer

    # Fleet event bus
    _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "CommandCenter"))
    try:
        from event_publisher import EventPublisher
        _event_pub = EventPublisher("http://127.0.0.1:9000", "hivemind")
    except ImportError:
        _event_pub = None

    print("=" * 60)
    print("  HIVE MIND -- Web Dashboard")
    print("=" * 60)

    variant_map = {"jso": DEVariant.JSO, "l_srtde": DEVariant.L_SRTDE, "l_shade": DEVariant.L_SHADE}
    variants = [variant_map[v.strip()] for v in args.variants.split(",") if v.strip() in variant_map]
    if not variants:
        variants = [DEVariant.JSO, DEVariant.L_SRTDE]
    objectives = [o.strip() for o in args.objectives.split(",")]

    # Shared state for the HTTP handler
    _snapshot_lock = threading.Lock()
    _snapshot = {"timestamp": time.time(), "status": "optimizing", "regime": {}, "consensus": {}, "members": []}
    _last_regime = [None]  # mutable for closure

    def _run_optimization():
        """Run ensemble optimization and update snapshot."""
        if args.synthetic:
            from portfolio_optimizer import generate_synthetic_returns
            returns = generate_synthetic_returns(n_assets=8, n_days=365, seed=args.seed)
            names = ["BTC", "ETH", "SOL", "ADA", "DOT", "AVAX", "LINK", "ATOM"]
        else:
            pairs = resolve_pairs(args.pairs)
            returns, valid_pairs = fetch_portfolio_returns(pairs, lookback_days=args.days)
            if returns is None:
                print("  ERROR: Could not fetch data")
                return
            names = [PAIR_NAMES.get(p, p) for p in valid_pairs]

        config = HiveMindConfig(
            variants=variants, objectives=objectives,
            max_fes=args.fes, seed=int(time.time()) % 2**31,
            min_assets=3, max_assets=8,
        )

        result = run_hive_mind(returns, names, config)
        snap = _build_snapshot(result, names, variants, objectives, args)

        with _snapshot_lock:
            _snapshot.update(snap)

        # Publish regime change to fleet event bus
        new_regime = result.regime.regime
        if _event_pub and _last_regime[0] and new_regime != _last_regime[0]:
            try:
                _event_pub.emit("REGIME_CHANGE", {
                    "from": _last_regime[0],
                    "to": new_regime,
                    "source": "hivemind",
                    "confidence": round(result.regime.confidence, 3),
                })
            except Exception:
                pass
        _last_regime[0] = new_regime

        print(f"  [{time.strftime('%H:%M:%S')}] Optimization complete: "
              f"regime={new_regime} sharpe={result.sharpe_ratio:.2f} "
              f"n_active={result.n_active}")

    def _optimizer_loop():
        """Background loop: optimize, sleep, repeat."""
        while True:
            try:
                _run_optimization()
            except Exception as e:
                print(f"  Optimization error: {e}")
            time.sleep(REOPTIMIZE_INTERVAL)

    # Run first optimization synchronously so we have data before serving
    print(f"\n  Running initial Hive Mind ensemble...")
    _run_optimization()

    # Start background re-optimization thread
    threading.Thread(target=_optimizer_loop, daemon=True, name="HiveMindOptimizer").start()
    print(f"  Re-optimizing every {REOPTIMIZE_INTERVAL // 60} minutes")

    # Serve dashboard
    port = args.port

    import sys as _sys
    _sys.path.insert(0, r"D:\CommandCenter")
    try:
        from port_guard import ensure_port, write_pidfile, cleanup_pidfile
        import atexit
        ensure_port(port, "hivemind")
        write_pidfile("hivemind", port)
        atexit.register(cleanup_pidfile, "hivemind")
    except Exception as _e:
        print(f"[PORT_GUARD] Warning: {_e}")

    here = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(here, "dashboard.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        html_content = "<html><body><h1>dashboard.html not found</h1></body></html>"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass
        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_content.encode("utf-8"))
            elif self.path == "/api/snapshot":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with _snapshot_lock:
                    self.wfile.write(json.dumps(_snapshot).encode("utf-8"))
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

    # ThreadingHTTPServer: the plain HTTPServer serves one request at a time,
    # so while this bot computes, its port stops answering and Command Center's
    # health check reports it DOWN even though it is healthy.
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"\n  Dashboard: http://localhost:{port}")
    print(f"  API:       http://localhost:{port}/api/snapshot")
    print(f"  Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Hive Mind -- DE-based Crypto Portfolio Optimizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # --- optimize ---
    p_opt = sub.add_parser("optimize", help="Single-objective optimization")
    p_opt.add_argument("--pairs", default="BTC,ETH,SOL,ADA,DOT,AVAX")
    p_opt.add_argument("--objective", default="sharpe",
                       choices=["sharpe", "cvar", "mean_cvar", "max_div", "risk_parity",
                                "sortino", "calmar"])
    p_opt.add_argument("--variant", default="jso",
                       choices=["jso", "l_srtde", "l_shade"])
    p_opt.add_argument("--fes", type=int, default=50000)
    p_opt.add_argument("--days", type=int, default=180)
    p_opt.add_argument("--seed", type=int, default=42)
    p_opt.add_argument("--synthetic", action="store_true")
    p_opt.add_argument("--save", type=str, default=None)
    p_opt.add_argument("--quiet", action="store_true", help="Suppress verbose output")

    # --- ensemble ---
    p_ens = sub.add_parser("ensemble", help="Hive Mind ensemble optimization")
    p_ens.add_argument("--pairs", default="BTC,ETH,SOL,ADA,DOT,AVAX,LINK,ATOM")
    p_ens.add_argument("--variants", default="jso,l_srtde")
    p_ens.add_argument("--objectives", default="sharpe,cvar,risk_parity")
    p_ens.add_argument("--fes", type=int, default=25000)
    p_ens.add_argument("--days", type=int, default=180)
    p_ens.add_argument("--min-assets", type=int, default=3)
    p_ens.add_argument("--max-assets", type=int, default=6)
    p_ens.add_argument("--min-weight", type=float, default=0.03)
    p_ens.add_argument("--max-weight", type=float, default=0.35)
    p_ens.add_argument("--no-regime", action="store_true")
    p_ens.add_argument("--seed", type=int, default=42)
    p_ens.add_argument("--synthetic", action="store_true")
    p_ens.add_argument("--save", type=str, default=None)
    p_ens.add_argument("--quiet", action="store_true", help="Suppress verbose output")

    # --- backtest ---
    p_bt = sub.add_parser("backtest", help="Walk-forward backtest")
    p_bt.add_argument("--pairs", default="BTC,ETH,SOL,ADA,DOT,AVAX")
    p_bt.add_argument("--train", type=int, default=90)
    p_bt.add_argument("--test", type=int, default=30)
    p_bt.add_argument("--objective", default="sharpe")
    p_bt.add_argument("--variant", default="jso")
    p_bt.add_argument("--fes", type=int, default=20000)
    p_bt.add_argument("--days", type=int, default=360)
    p_bt.add_argument("--ensemble", action="store_true")
    p_bt.add_argument("--cv", action="store_true",
                       help="Use purged k-fold cross-validation instead of walk-forward")
    p_bt.add_argument("--folds", type=int, default=5,
                       help="Number of CV folds (default 5, only with --cv)")
    p_bt.add_argument("--purge", type=int, default=0,
                       help="Purge gap in days between train/test (default 0, try 5)")
    p_bt.add_argument("--seed", type=int, default=42)
    p_bt.add_argument("--synthetic", action="store_true")
    p_bt.add_argument("--save", type=str, default=None)
    p_bt.add_argument("--quiet", action="store_true", help="Suppress verbose output")

    # --- benchmark ---
    p_bm = sub.add_parser("benchmark", help="DE benchmark validation")
    p_bm.add_argument("--dim", type=int, default=10)
    p_bm.add_argument("--fes", type=int, default=50000)
    p_bm.add_argument("--seed", type=int, default=42)
    p_bm.add_argument("--quiet", action="store_true", help="Suppress verbose output")

    # --- regime ---
    p_reg = sub.add_parser("regime", help="Detect market regime")
    p_reg.add_argument("--pairs", default="BTC,ETH,SOL,ADA,DOT,AVAX")
    p_reg.add_argument("--days", type=int, default=180)
    p_reg.add_argument("--seed", type=int, default=42)
    p_reg.add_argument("--synthetic", action="store_true")
    p_reg.add_argument("--quiet", action="store_true", help="Suppress verbose output")

    # --- audit ---
    p_aud = sub.add_parser("audit", help="Show Grok document audit report")
    p_aud.add_argument("--quiet", action="store_true", help="Suppress verbose output")

    # --- dashboard ---
    p_dash = sub.add_parser("dashboard", help="Run ensemble and serve web dashboard")
    p_dash.add_argument("--pairs", default="BTC,ETH,SOL,ADA,DOT,AVAX,LINK,ATOM")
    p_dash.add_argument("--variants", default="jso,l_srtde")
    p_dash.add_argument("--objectives", default="sharpe,cvar,risk_parity")
    p_dash.add_argument("--fes", type=int, default=25000)
    p_dash.add_argument("--days", type=int, default=180)
    p_dash.add_argument("--seed", type=int, default=42)
    p_dash.add_argument("--synthetic", action="store_true")
    p_dash.add_argument("--port", type=int, default=8073)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    commands = {
        "optimize": cmd_optimize,
        "ensemble": cmd_ensemble,
        "backtest": cmd_backtest,
        "benchmark": cmd_benchmark,
        "regime": cmd_regime,
        "audit": cmd_audit,
        "dashboard": cmd_dashboard,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
