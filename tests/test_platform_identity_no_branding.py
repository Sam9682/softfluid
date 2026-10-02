"""Guard test: no stray platform-identity *branding* literals remain.

The configurable-platform-identity work replaced hardcoded brand references
with values driven from conf/deploy.ini. Two things are deliberately allowed
and therefore NOT flagged by this guard:

  1. The canonical default *value* of the configurable parameters:
       - the folder slug ``opcp-explorer``
       - the display-name default ``OPCP-Explorer_AI_SharedGPU_Docker_Serverless``
     These are the shipped defaults/fallbacks (deploy.ini, env defaults,
     fallback returns, and the tests that validate them).

  2. The deployable-app catalog, which contains independent products that merely
     share the ``opcp`` prefix (e.g. opcp-serverless-brik). Renaming those would
     break the git clones, so these paths are out of scope.

What the guard forbids is the free-standing marketing brand ``OPCP-Explorer``
used as a product name (in prose, comments, docstrings, banners, UI). That form
is detected as ``OPCP-Explorer`` NOT immediately followed by ``_`` (the default
display-name value uses ``OPCP-Explorer_AI_...``).
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Brand prose = 'OPCP-Explorer' not immediately followed by '_' (which would make
# it the allowed display-name default value) and not '.md'/'.pdf' (old-filename
# negative assertions in sibling tests reference the removed artifacts on purpose).
BRAND_PROSE = re.compile(r'OPCP-Explorer(?![_\w])(?!\.md)(?!\.pdf)')

# Files/dirs that are allowed to mention the brand (catalog, this guard, and the
# sibling tests that assert the old branded artifacts were removed).
ALLOWLIST_SUFFIXES = (
    os.path.join('conf', 'default_apps'),
    os.path.join('docs', 'LIST_OF_APP_AVAILABLE.md'),
    os.path.join('docs', 'DEPLOYMENT_GUIDE.md'),
    os.path.join('tests', 'test_platform_identity_no_branding.py'),
    os.path.join('tests', 'test_marketing_pdf_generators.py'),
    os.path.join('tests', 'test_platform_overview_doc_route.py'),
)

SCAN_EXTENSIONS = ('.py', '.html', '.md', '.sh', '.yml', '.yaml', '.ini', '.js', '.service')

SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'shared'}


def _iter_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(SCAN_EXTENSIONS):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, REPO_ROOT)
            if any(rel.endswith(suf) for suf in ALLOWLIST_SUFFIXES):
                continue
            yield rel, full


def test_no_brand_prose_outside_allowlist():
    offenders = []
    for rel, full in _iter_files():
        try:
            with open(full, 'r', encoding='utf-8', errors='ignore') as f:
                for lineno, line in enumerate(f, 1):
                    if BRAND_PROSE.search(line):
                        offenders.append(f'{rel}:{lineno}: {line.strip()}')
        except OSError:
            continue
    assert not offenders, (
        'Found free-standing OPCP-Explorer brand prose (should be driven from '
        'PLTF_NAME or use the canonical default value):\n' + '\n'.join(offenders)
    )


def test_app_catalog_still_intact():
    """The deployable-app catalog must keep its opcp-* entries - this guards
    against an over-eager rename sweeping them away."""
    catalog = os.path.join(REPO_ROOT, 'conf', 'default_apps')
    with open(catalog, 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'opcp-serverless-brik' in content
    assert 'opcp-openstack-first-steps' in content
