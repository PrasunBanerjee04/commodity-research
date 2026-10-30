"""IRI login redirects and scoped, private PyCPT-compatible authentication."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import Request, HTTPCookieProcessor

from comm_research.markets.weather.scrapers.ecmwf_s2s import catalogue


def response(body, url=catalogue.ROOT, content_type='text/html'):
    result = MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = body
    result.geturl.return_value = url
    result.headers = {'Content-Type': content_type}
    return result


class IRIAuthTests(unittest.TestCase):
    def test_http_200_login_redirect_fails_before_netcdf_or_caching(self):
        login = response(b'<!DOCTYPE html><title>dlauth</title>',
                         'https://iridl.ldeo.columbia.edu/auth/login?realm=s2s')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(catalogue, 'open_catalogue', return_value=login):
            with self.assertRaisesRegex(RuntimeError, 'Browser login alone'):
                catalogue.fetch(catalogue.ROOT, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_mislabeled_netcdf_error_page_is_rejected_and_not_cached(self):
        body = b'<html><head><title>Error 404Bad Request</title></head><body>error</body></html>'
        bad = response(body, catalogue.ROOT+'S/data.nc', 'application/x-netcdf')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(catalogue, 'open_catalogue', return_value=bad):
            with self.assertRaisesRegex(RuntimeError, 'Error 404Bad Request'):
                catalogue.fetch(catalogue.ROOT+'S/data.nc', directory)
            self.assertEqual(list(Path(directory).iterdir()), [])
        with self.assertRaisesRegex(RuntimeError, 'Error 404Bad Request'):
            catalogue.dates_from_dap(body, b"")

    def test_unexpected_html_has_a_distinct_error(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(catalogue, 'open_catalogue', return_value=response(b'<title>Not found</title>')):
            with self.assertRaisesRegex(RuntimeError, 'HTML instead of catalogue data'):
                catalogue.fetch(catalogue.ROOT+'S/data.nc', directory)

    def test_reads_pycpt_file_and_process_binding_without_disclosing_values(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(Path, 'home', return_value=Path(directory)), \
             patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(catalogue.auth_cookie())
            path = Path(directory)/'.pycpt_dlauth'
            path.write_text(json.dumps({'key': 'fixture-cookie'}))
            self.assertEqual(catalogue.auth_cookie(), 'fixture-cookie')
            with patch.dict(os.environ, {'IRI_DLAUTH_COOKIE': 'fixture-override'}):
                self.assertEqual(catalogue.auth_cookie(), 'fixture-override')
            path.write_text('<title>bad login</title>')
            with self.assertRaisesRegex(RuntimeError, 'regenerate'):
                catalogue.auth_cookie()

    def test_cookies_are_secure_and_cross_host_or_http_redirects_fail(self):
        opener = catalogue.iri_opener('fixture-cookie')
        processor = next(h for h in opener.handlers if isinstance(h, HTTPCookieProcessor))
        cookies = list(processor.cookiejar)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0].domain, 'iridl.ldeo.columbia.edu')
        self.assertTrue(cookies[0].secure)
        for url in ('https://example.com/', 'http://iridl.ldeo.columbia.edu/'):
            with self.assertRaisesRegex(RuntimeError, 'not forwarded'):
                catalogue.IRIRedirects().redirect_request(Request(catalogue.ROOT), None, 302, '', {}, url)
            with self.assertRaises(ValueError):
                catalogue.open_catalogue(url)

    def test_setup_saves_generated_key_with_private_permissions_and_no_clobber(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(Path, 'home', return_value=Path(directory)), \
             patch.object(catalogue.getpass, 'getpass', return_value='fixture-password'), \
             patch.object(catalogue, 'iri_opener') as factory:
            factory.return_value.open.side_effect = [response(b''), response(b'{"key":"fixture-key"}')]
            path = catalogue.setup_iri_auth('test@example.com')
            self.assertEqual(json.loads(path.read_text()), {'key': 'fixture-key'})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn('fixture-password', path.read_text())
            with self.assertRaises(FileExistsError):
                catalogue.setup_iri_auth('test@example.com')
            factory.return_value.open.side_effect = [response(b''), response(b'<title>bad login</title>')]
            with self.assertRaisesRegex(RuntimeError, 'No credentials were saved'):
                catalogue.setup_iri_auth('test@example.com', overwrite=True)
            self.assertEqual(json.loads(path.read_text()), {'key': 'fixture-key'})


if __name__ == '__main__':
    unittest.main()
