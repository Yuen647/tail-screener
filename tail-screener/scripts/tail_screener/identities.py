"""Explicit provenance while retaining strict isolation of account builds."""
import hashlib
import json
from pathlib import Path


def identity_manifest(rule_sha256, config, account_identity, snapshot_sha256=None):
    root = Path(__file__).resolve().parents[2]
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((root/'scripts').rglob('*.py'))}
    declared = {'version': 1, 'signal': 'SMA5>SMA20; equality holds; SMA5<SMA20 exits',
                'allocation': '20d mean amount, then signal-day amount; exact ties retained',
                'execution': 'next-open conditional daily bars; T+1; 4.5pct gap; 1pct daily capacity',
                'accounting': 'integer cents; resident single-lot cash and documented capital-transfer tax model; fractional shares unresolved',
                'configuration': config}
    digest = lambda x: hashlib.sha256(json.dumps(x, sort_keys=True, separators=(',', ':'),
                                                allow_nan=False).encode()).hexdigest()
    return {'schema_version': 1, 'rule_sha256': rule_sha256,
            'declared_strategy': declared, 'declared_strategy_sha256': digest(declared),
            'implementation_sha256': digest(files), 'implementation_files': files,
            'snapshot_sha256': snapshot_sha256, 'account_series_id': account_identity,
            'compatibility_policy': 'strict_build_isolation_no_automatic_migration',
            'meaning': 'Declared semantics aid comparison; only full rule identity admits account continuation.'}
