"""Shared building blocks for the btctrader services.

Modules:
    config          environment based settings (see docs/KOMPONENTEN.md section 3)
    ftapi           Freqtrade REST client (JWT login, re-login on 401)
    bitvavo_public  public Bitvavo REST endpoints (no API key)
    alerts          Telegram and ntfy notifications that never raise
    db              SQLite ledger schema, WAL connection, UTC time helpers
    jsonl           JSON lines append/tail and atomic JSON writes
    log             stderr logging setup for systemd/journald
"""
