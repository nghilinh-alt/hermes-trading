# ICT test fixtures

All ground-truth tests in this suite use small, hand-constructed synthetic
candle sequences defined inline in each test file (via `tests/ict/helpers.py`)
rather than committed CSVs of real exchange data.

**Why, given the spec's "hand-label ~20-30 instances from real historical
OHLCV" suggestion:** this sandbox's project `.venv` (`home = /usr/bin`,
fuse-mounted artifacts) doesn't resolve in this environment, and `ccxt` isn't
installed in the system Python used to run pytest here, so a live fetch
wasn't available this session. Hand-constructed candles are also a *more*
precise ground-truth for unit-testing pure geometric detectors than
eyeballing a real chart: every price is exact, so the expected swing/FVG/OB
zone is unambiguous and the test is fully hermetic (no I/O at all, stronger
than a committed CSV). `test_smoke_stress.py` substitutes a seeded
pseudo-random walk (deterministic, no network) for the "realistic messy
data" edge-case coverage (ranging/choppy segments, gaps, no-signal windows)
the spec asks for.

Flagged for Phase 2: swap in real committed BTC/ETH/SOL OHLCV CSVs here
(fetch once via `hermes_trading.adapters.price`'s ccxt setup from a working
venv) before backtesting -- the detectors should be re-validated against at
least one real multi-week window per asset.
