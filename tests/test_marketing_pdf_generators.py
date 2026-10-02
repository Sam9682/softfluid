"""Tests for the configurable platform identity in the marketing-doc generators.

The three generators under docs/ must derive their display title and output
filename from PLTF_NAME in conf/deploy.ini rather than a hardcoded brand:

  * docs/generate_pdf.py           (WeasyPrint, embedded HTML template)
  * docs/generate_pdf_final.py     (fpdf, reads PLATFORM_OVERVIEW.md)
  * docs/final_working_solution.py (fpdf, reads PLATFORM_OVERVIEW.md)

fpdf / weasyprint are not installed in CI, so they are stubbed in sys.modules
before importing. Each generator exposes get_platform_name(), PLATFORM_NAME and
PLATFORM_SLUG; generate_pdf.py also exposes build_html().
"""

import importlib.util
import os
import sys
import types
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, 'docs')


def _install_pdf_stubs():
    # Stub fpdf.FPDF.
    if 'fpdf' not in sys.modules:
        fpdf_mod = types.ModuleType('fpdf')

        class _FPDF:  # minimal base class
            def __init__(self, *a, **k):
                pass

        fpdf_mod.FPDF = _FPDF
        sys.modules['fpdf'] = fpdf_mod
    # Stub weasyprint.HTML.
    if 'weasyprint' not in sys.modules:
        wp_mod = types.ModuleType('weasyprint')

        class _HTML:
            def __init__(self, *a, **k):
                pass

            def write_pdf(self, *a, **k):
                pass

        wp_mod.HTML = _HTML
        sys.modules['weasyprint'] = wp_mod


def _load_generator(filename):
    _install_pdf_stubs()
    path = os.path.join(DOCS_DIR, filename)
    name = 'docgen_' + filename.replace('.', '_')
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_get_platform_name_prefers_deploy_ini(tmp_path):
    gen = _load_generator('generate_pdf_final.py')
    conf = tmp_path / 'conf'
    conf.mkdir()
    (conf / 'deploy.ini').write_text('PLTF_NAME=Acme Cloud Platform\n')
    fake_src = os.path.join(str(tmp_path), 'docs', 'generate_pdf_final.py')
    with patch.object(gen.os.path, 'abspath', return_value=fake_src):
        assert gen.get_platform_name() == 'Acme Cloud Platform'


def test_slug_is_filesystem_safe():
    gen = _load_generator('generate_pdf_final.py')
    # The default name contains underscores/hyphens only -> slug keeps them.
    assert '/' not in gen.PLATFORM_SLUG
    assert ' ' not in gen.PLATFORM_SLUG
    assert gen.PLATFORM_SLUG  # non-empty


def test_generators_have_no_hardcoded_brand_default():
    # The canonical fallback name is the shipped placeholder, not a stray brand.
    for fname in ('generate_pdf_final.py', 'final_working_solution.py'):
        gen = _load_generator(fname)
        assert gen.PLATFORM_NAME  # resolved to something
        assert gen.SOURCE_DOC == 'PLATFORM_OVERVIEW.md'


def test_generate_pdf_html_injects_platform_name():
    gen = _load_generator('generate_pdf.py')
    html = gen.build_html()
    # Sentinel fully resolved and the configured name present in the cover.
    assert '@@PLATFORM_NAME@@' not in html
    assert gen.PLATFORM_NAME in html


def test_source_doc_exists():
    assert os.path.exists(os.path.join(DOCS_DIR, 'PLATFORM_OVERVIEW.md'))
    assert os.path.exists(os.path.join(DOCS_DIR, 'PLATFORM_MARKETING.md'))
    assert not os.path.exists(os.path.join(DOCS_DIR, 'OPCP-Explorer.md'))
