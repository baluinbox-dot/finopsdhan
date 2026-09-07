"""Backtest engine: replays a Strategy class against the local historical
CSV cache (see scripts/backtest/download_historical_data.py) instead of
live Dhan calls. Entirely separate from the live/paper trading engine in
app.engine -- never touches the production DB, never places any order."""
