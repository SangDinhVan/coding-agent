"""Find helpers shipped with the trusted controller, never the admitted project."""
import sys
from pathlib import Path


def helper_path(name):
    if name not in {'sandbox_fs', 'sandbox_exec'}:
        raise ValueError('unknown_trusted_helper')
    checkout = Path(__file__).resolve().parents[2] / 'sandbox-image' / (name + '.py')
    installed = Path(sys.prefix) / 'share' / 'sang-coding-agent' / 'helpers' / (name + '.py')
    return checkout if checkout.is_file() else installed
