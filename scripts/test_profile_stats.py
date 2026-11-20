"""Offline privacy checks. No workflow, token, network or generated public file."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr, chdir
from io import BytesIO, StringIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, parse_qs
import json
import os
import sys

import profile_stats as stats

START, END = "2025-11-19T12:00:00Z", "2026-11-19T12:00:00Z"


class PrivacyTests(unittest.TestCase):
    def cards(self):
        # Synthetic test-only metrics, never written to profile/ or presented as real.
        return stats.render({"calendar": 12, "commits": 4, "repositories": 2,
                             "languages": {"Python": 20, "SQL": 10}}, START, END)

    def test_renderer_accepts_its_aggregate_output(self):
        for name, svg in self.cards().items():
            stats.validate_svg(svg, name)

    def test_rejects_private_text_in_visible_or_root_content(self):
        svg = self.cards()["stats.svg"]
        for modified in (svg.replace("Contribuições · calendário", "secret-repository-name"),
                         svg.replace('</svg>', '<text>secret-repository-name</text></svg>'),
                         svg.replace('><rect', '>secret-repository-name<rect', 1)):
            with self.assertRaises(stats.SafeFailure):
                stats.validate_svg(modified, "stats.svg")

    def test_rejects_active_content_links_and_metadata(self):
        svg = self.cards()["stats.svg"]
        for content in ('<script>alert(1)</script>', '<a href="https://example.org">x</a>',
                        '<metadata>secret</metadata>', '<image href="file:///secret"/>'):
            with self.assertRaises(stats.SafeFailure):
                stats.validate_svg(svg.replace('</svg>', content + '</svg>'), "stats.svg")
        with self.assertRaises(stats.SafeFailure):
            stats.validate_svg(svg.replace('<rect ', '<rect onload="alert(1)" ', 1), "stats.svg")

    def test_unsupported_language_labels_cannot_be_rendered(self):
        with self.assertRaises(stats.SafeFailure):
            stats.language_rows({"secret-repository-name": 123})

    def test_remainder_keeps_bytes_in_denominator(self):
        rows = stats.language_rows({lang: i + 1 for i, lang in enumerate(sorted(stats.LANGUAGES))})
        self.assertLessEqual(len(rows), 8)
        self.assertEqual(sum(size for _, size in rows), sum(range(1, len(stats.LANGUAGES) + 1)))

    def test_refuses_additional_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, svg in self.cards().items():
                (root / name).write_text(svg, encoding='utf-8')
            stats.validate_directory(root)
            (root / 'api-response.json').write_text('{}')
            with self.assertRaises(stats.SafeFailure):
                stats.validate_directory(root)

    def test_readme_activation_preserves_all_other_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / 'README.md'
            original = '# Profile\n\n## GitHub Stats\nOLD\n\n---\n\n## Tech Stack\nPRESERVED\n'
            readme.write_text(original, encoding='utf-8')
            stats.activate(readme)
            updated = readme.read_text(encoding='utf-8')
            self.assertTrue(updated.startswith('# Profile\n\n## GitHub Stats\n'))
            self.assertTrue(updated.endswith('\n---\n\n## Tech Stack\nPRESERVED\n'))
            self.assertIn('src="./profile/stats.svg"', updated)

    def test_private_metadata_is_discarded_and_forks_excluded(self):
        class FakePrivate:
            def request(self, path, **kwargs):
                if path == '/user':
                    return {'login': stats.USER}
                if '/commits/' in path:
                    return {'sha': 'b' * 40}
                return {'Python': 10, 'secret-project-name': 2}

            def pages(self, path, params, **kwargs):
                if path == '/user/repos':
                    return iter([
                        {'id': 1, 'name': 'secret-project-name', 'private': True, 'fork': False,
                         'owner': {'login': stats.USER}, 'default_branch': 'secret-branch'},
                        {'id': 2, 'name': 'private-fork', 'private': True, 'fork': True,
                         'owner': {'login': stats.USER}},
                    ])
                return iter([{'sha': 'a' * 40, 'author': {'login': stats.USER},
                              'commit': {'message': 'secret-message'}}] * 2)

        class FakePublic:
            def request(self, *args, **kwargs):
                return {'data': {'user': {'contributionsCollection': {
                    'contributionCalendar': {'totalContributions': 12}}}}}

        aggregates = stats.collect(FakePrivate(), FakePublic(), START, END)
        self.assertEqual(aggregates['commits'], 1)
        self.assertEqual(aggregates['repositories'], 1)
        self.assertEqual(aggregates['languages'], {'Python': 10, 'Outras': 2})
        output = ''.join(stats.render(aggregates, START, END).values())
        for private in ('secret-project-name', 'secret-branch', 'secret-message', 'a' * 40):
            self.assertNotIn(private, output)

    def test_redirect_is_rejected_before_reusing_authorization(self):
        with self.assertRaises(stats.SafeFailure):
            stats.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.org')

    def test_rejects_negative_and_boolean_metrics(self):
        for value in (-1, True, '12', None):
            with self.assertRaises(stats.SafeFailure):
                stats.integer(value)


class StrictSVGTests(unittest.TestCase):
    def cards(self):
        return PrivacyTests().cards()

    def reject(self, value, name='stats.svg'):
        with self.assertRaises(stats.SafeFailure):
            stats.validate_svg(value, name)

    def test_rejects_unused_namespaces_anywhere(self):
        svg = self.cards()['stats.svg']
        for namespace in ('xmlns:audit="https://example.invalid/PRIVATE"',
                          'xmlns:token="github_pat_SYNTHETIC_NOT_A_REAL_TOKEN"',
                          'xmlns:xml="http://www.w3.org/XML/1998/namespace"'):
            with self.subTest(namespace=namespace):
                self.reject(svg.replace('<svg ', '<svg ' + namespace + ' ', 1))
                self.reject(svg.replace('<text ', '<text ' + namespace + ' ', 1))

    def test_rejects_namespace_redeclaration_and_reset(self):
        svg = self.cards()['stats.svg']
        for ns in ('xmlns=""', 'xmlns="http://www.w3.org/2000/svg"', 'xmlns="https://example.invalid/PRIVATE"'):
            self.reject(svg.replace('<text ', '<text ' + ns + ' ', 1))

    def test_rejects_namespaced_attributes(self):
        svg = self.cards()['stats.svg']
        for attribute in ('xml:base="https://example.invalid/PRIVATE"', 'xml:lang="PRIVATE"',
                          'xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="https://example.invalid"'):
            self.reject(svg.replace('<text ', '<text ' + attribute + ' ', 1))

    def test_rejects_entities_dtd_comments_cdata_and_processing_instructions(self):
        svg = self.cards()['stats.svg']
        for content in ('<!--PRIVATE-->', '<![CDATA[PRIVATE]]>', '<?audit PRIVATE?>',
                        '<!DOCTYPE svg [<!ENTITY secret SYSTEM "file:///PRIVATE">]>',
                        '<!DOCTYPE svg SYSTEM "https://example.invalid/PRIVATE">'):
            self.reject(content + svg)
            self.reject(svg.replace('</svg>', content + '</svg>'))
        for entity in ('&#80;ython', '&#x50;ython', '&secret;'):
            self.reject(self.cards()['top-langs.svg'].replace('Python', entity), 'top-langs.svg')

    def test_rejects_external_references_and_css(self):
        svg = self.cards()['stats.svg']
        for attr in ('href="https://example.invalid"', 'style="fill:url(https://example.invalid)"',
                     'onclick="alert(1)"', 'data-private="PRIVATE"'):
            self.reject(svg.replace('<rect ', '<rect ' + attr + ' ', 1))
        for content in ('<foreignObject>PRIVATE</foreignObject>', '<use href="#secret"/>',
                        '<style>text{fill:url(https://example.invalid)}</style>'):
            self.reject(svg.replace('</svg>', content + '</svg>'))

    def test_rejects_alternate_bytes_and_encodings(self):
        svg = self.cards()['stats.svg'].encode('utf-8')
        for raw in (b'\xef\xbb\xbf' + svg, svg + b'\n', svg + b' ', svg.replace(b'><', b'>\n<'),
                    svg.decode().encode('utf-16'), svg.replace(b'<rect ', b'<rect\r\n ', 1), b'\xff' + svg):
            self.reject(raw)

    def test_requires_exact_layout_labels_and_numeric_format(self):
        svg = self.cards()['stats.svg']
        for old, new in (('>12</text>', '>012</text>'), ('x="419"', 'x="0419"'),
                         ('font-size="12"', 'font-size="13"'), (stats.CALENDAR_NOTICE, 'Cobertura privada confirmada.')):
            self.reject(svg.replace(old, new))
        self.reject(svg.replace('</svg>', '<text>Python</text></svg>'))

    def test_validates_period_dates_and_update_date(self):
        svg = self.cards()['stats.svg']
        self.reject(svg.replace('2025-11-19', '2025-11-18'))
        self.reject(svg.replace('Atualizado em 2026-11-19', 'Atualizado em 2026-11-20'))
        self.reject(svg.replace('2026-11-19', '2026-02-30'))

    def test_rejects_invalid_language_shares_and_labels(self):
        svg = self.cards()['top-langs.svg']
        for old, new in (('66.7%', '99.9%'), ('Python', 'PRIVATE'), ('33.3%', '100.1%'), ('SQL', 'Python')):
            self.reject(svg.replace(old, new), 'top-langs.svg')

    def test_empty_languages_and_small_rounding_shares_are_valid(self):
        for sizes in ({}, {'Python': 1}, {name: i + 1 for i, name in enumerate(sorted(stats.LANGUAGES))},
                      {'Python': 10000000, 'SQL': 1}):
            cards = stats.render({'calendar': 0, 'commits': 0, 'repositories': 1, 'languages': sizes}, START, END)
            for name, svg in cards.items():
                self.assertEqual(stats.validate_svg(svg, name), svg.encode('utf-8'))

    def test_read_bytes_detects_newline_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stats.write_cards(root, {n: v.encode() for n, v in self.cards().items()})
            p = root / 'stats.svg'
            p.write_bytes(p.read_bytes() + b'\r\n')
            with self.assertRaises(stats.SafeFailure):
                stats.validate_directory(root)

    def test_unknown_filename_is_rejected(self):
        self.reject(self.cards()['stats.svg'], 'private.svg')

    def test_written_bytes_match_validated_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verified = {n: stats.validate_svg(v, n) for n, v in self.cards().items()}
            stats.write_cards(root, verified)
            self.assertEqual(stats.validate_directory(root), verified)
            self.assertTrue(all((root / n).read_bytes() == v for n, v in verified.items()))

    def test_git_index_must_match_validated_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cards = {n: v.encode() for n, v in self.cards().items()}
            stats.write_cards(root, cards)
            with patch.object(stats.subprocess, 'check_output', side_effect=[cards[n] for n in stats.FILES]):
                stats.verify_index(root)
            different = cards['stats.svg'].replace(b'>12</text>', b'>13</text>')
            with patch.object(stats.subprocess, 'check_output', return_value=different):
                with self.assertRaises(stats.SafeFailure):
                    stats.verify_index(root)


class Response:
    status = 200
    def __init__(self, data=None, link='', raw=None):
        self.raw = json.dumps(data).encode() if raw is None else raw
        self.headers = {'Link': link}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, limit): return self.raw[:limit]


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
    def open(self, request, **kwargs):
        self.calls.append(request)
        item = next(self.responses)
        if isinstance(item, Exception): raise item
        return item


def api_fixture(responses):
    # No actual token and no actual network access, even if an assertion fails.
    with patch.object(stats, 'build_opener', return_value=Opener(responses)):
        return stats.GitHub('SYNTHETIC_TEST_CREDENTIAL')


class PaginationTests(unittest.TestCase):
    def link(self, page=2, path='/user/repos', **changes):
        query = {'visibility': 'all', 'per_page': '100', 'page': str(page), **changes}
        return '<https://api.github.com' + path + '?' + urlencode(query) + '>; rel="next"'

    def test_short_page_with_next_is_followed(self):
        api = api_fixture([Response([{'id': 1}], self.link()), Response([{'id': 2}])])
        self.assertEqual(list(api.pages('/user/repos', {'visibility': 'all'})), [{'id': 1}, {'id': 2}])
        self.assertEqual(len(api.opener.calls), 2)

    def test_full_page_without_next_is_final(self):
        api = api_fixture([Response(list(range(100)))])
        self.assertEqual(len(list(api.pages('/user/repos', {'visibility': 'all'}))), 100)
        self.assertEqual(len(api.opener.calls), 1)

    def test_multiple_pages_and_relations(self):
        second = self.link(3) + ', ' + self.link(1).replace('rel="next"', 'rel="prev"')
        api = api_fixture([Response([1], self.link()), Response([2], second), Response([3])])
        self.assertEqual(list(api.pages('/user/repos', {'visibility': 'all'})), [1, 2, 3])

    def test_rejects_untrusted_next_destinations(self):
        for target in ('http://api.github.com/user/repos', 'https://api.github.com.evil.invalid/user/repos',
                       'https://api.github.com@evil.invalid/user/repos', 'https://api.github.com:443/user/repos',
                       'https://evil.invalid/user/repos', '//api.github.com/user/repos',
                       'https://api.github.com/user/repos#PRIVATE', 'https://api.github.com\\@evil.invalid/user/repos'):
            api = api_fixture([Response([1], '<' + target + '>; rel="next"')])
            with self.assertRaises(stats.SafeFailure):
                list(api.pages('/user/repos', {'visibility': 'all'}))
            self.assertEqual(len(api.opener.calls), 1)

    def test_rejects_path_filter_or_page_size_changes(self):
        for header in (self.link(path='/other'), self.link(visibility='public'), self.link(per_page='1'),
                       self.link().replace('visibility=all&', ''), self.link().replace('page=2', 'page=2&page=3')):
            api = api_fixture([Response([1], header)])
            with self.assertRaises(stats.SafeFailure):
                list(api.pages('/user/repos', {'visibility': 'all'}))

    def test_rejects_loops_skips_and_duplicate_next(self):
        for header in (self.link(1), self.link(3), self.link() + ', ' + self.link()):
            api = api_fixture([Response([1], header)])
            with self.assertRaises(stats.SafeFailure):
                list(api.pages('/user/repos', {'visibility': 'all'}))

    def test_rejects_malformed_link_and_inconsistent_last(self):
        for header in ('PRIVATE', '<https://api.github.com>; rel="unexpected"', self.link().replace('rel="next"', 'rel="last"')):
            api = api_fixture([Response([1], header)])
            with self.assertRaises(stats.SafeFailure):
                list(api.pages('/user/repos', {'visibility': 'all'}))

    def test_rejects_empty_next_and_invalid_body(self):
        for response in (Response([], self.link()), Response({'id': 1}), Response(list(range(101)))):
            api = api_fixture([response])
            with self.assertRaises(stats.SafeFailure):
                list(api.pages('/user/repos', {'visibility': 'all'}))

    def test_page_limit_aborts(self):
        api = api_fixture([Response([1], self.link()), Response([2], self.link(3))])
        with patch.object(stats, 'MAX_PAGES', 2):
            with self.assertRaisesRegex(stats.SafeFailure, 'PAGINATION_LIMIT'):
                list(api.pages('/user/repos', {'visibility': 'all'}))

    def test_commit_filters_survive_all_pages(self):
        params = {'sha': 'a' * 40, 'author': stats.USER, 'since': START, 'until': END}
        link = '<https://api.github.com/repos/paesdj1987/fixture/commits?' + urlencode({**params, 'per_page': 100, 'page': 2}) + '>; rel="next"'
        api = api_fixture([Response([1], link), Response([2])])
        list(api.pages('/repos/paesdj1987/fixture/commits', params))
        for request in api.opener.calls:
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertTrue(all(query[k] == [v] for k, v in params.items()))

    def test_next_cannot_change_author_pinned_sha_or_period(self):
        params = {'sha': 'a' * 40, 'author': stats.USER, 'since': START, 'until': END}
        for key, value in (('sha', 'b' * 40), ('author', 'other'), ('since', END), ('until', START)):
            header = '<https://api.github.com/repos/paesdj1987/fixture/commits?' + urlencode({**params, key: value, 'per_page': 100, 'page': 2}) + '>; rel="next"'
            api = api_fixture([Response([1], header)])
            with self.assertRaises(stats.SafeFailure):
                list(api.pages('/repos/paesdj1987/fixture/commits', params))
            self.assertEqual(len(api.opener.calls), 1)


class APIFailureTests(unittest.TestCase):
    def test_authentication_and_rate_errors_are_safe(self):
        for status, code in ((401, 'API_AUTH'), (403, 'API_FORBIDDEN'), (429, 'API_RATE_LIMIT'), (500, 'API_SERVER')):
            err = HTTPError('https://example.invalid/PRIVATE', status, 'PRIVATE', {}, BytesIO(b'PRIVATE'))
            api = api_fixture([err])
            logs = StringIO()
            with redirect_stdout(logs), redirect_stderr(logs):
                with self.assertRaisesRegex(stats.SafeFailure, code):
                    api.request('/user')
            self.assertEqual(logs.getvalue(), '')

    def test_later_page_failure_does_not_return_success(self):
        link = PaginationTests().link()
        api = api_fixture([Response([1], link), HTTPError('PRIVATE', 403, 'PRIVATE', {}, None)])
        with self.assertRaises(stats.SafeFailure):
            list(api.pages('/user/repos', {'visibility': 'all'}))

    def test_graphql_partial_errors_abort(self):
        api = api_fixture([Response({'data': {'value': 1}, 'errors': [{'message': 'PRIVATE'}]})])
        with self.assertRaisesRegex(stats.SafeFailure, 'API_GRAPHQL'):
            api.request('/graphql', {'query': 'query {}'})

    def test_network_bad_json_oversize_and_status_abort(self):
        for response in (URLError('PRIVATE'), Response(raw=b'not JSON'), Response(raw=b'\xff'),
                         Response(raw=b'x' * 12_000_001)):
            api = api_fixture([response])
            with self.assertRaises(stats.SafeFailure): api.request('/user')
        response = Response({})
        response.status = 204
        with self.assertRaises(stats.SafeFailure): api_fixture([response]).request('/user')

    def test_empty_repository_is_the_only_allowed_http_fallback(self):
        err = lambda message: HTTPError('PRIVATE', 409, 'PRIVATE', {}, BytesIO(json.dumps({'message': message}).encode()))
        self.assertEqual(api_fixture([err('Git Repository is empty.')]).request('/repos/fixture/commits', allow_empty=True), [])
        with self.assertRaises(stats.SafeFailure):
            api_fixture([err('Conflict')]).request('/repos/fixture/commits', allow_empty=True)

    def test_api_url_validation_precedes_any_network_request(self):
        api = api_fixture([])
        for url in ('//evil.invalid', 'https://evil.invalid/', '/user\nPRIVATE', '/user?x=%zz'):
            with self.assertRaises(stats.SafeFailure): api.request(url)
        self.assertEqual(api.opener.calls, [])


class CollectionFixture:
    def __init__(self):
        self.repos = [{'id': i, 'name': 'private-fixture-' + str(i), 'private': i == 2, 'fork': False,
                       'owner': {'login': stats.USER}, 'default_branch': 'feature/default', 'size': 1} for i in (1, 2)]
        self.tip_calls, self.commit_calls = [], []
        self.commit_author = stats.USER
        self.owner = stats.USER
    def request(self, path, **kwargs):
        if path == '/user': return {'login': self.owner}
        if '/languages' in path: return {'Python': 10, 'PRIVATE_LANGUAGE_SENTINEL': 3}
        self.tip_calls.append(path)
        return {'sha': 'b' * 40}
    def pages(self, path, params, **kwargs):
        if path == '/user/repos': return iter(self.repos)
        self.commit_calls.append(params.copy())
        # Simulate branch advancement after resolution: a branch-name request would fail.
        if params['sha'] != 'b' * 40: raise AssertionError('Mutable branch used')
        return iter([{'sha': 'a' * 40, 'author': {'login': self.commit_author}},
                     {'sha': ('c' if 'fixture-1/' in path else 'd') * 40, 'author': {'login': self.commit_author}}])


class CalendarFixture:
    def request(self, *args, **kwargs):
        return {'data': {'user': {'contributionsCollection': {'contributionCalendar': {'totalContributions': 12}}}}}


class CollectionTests(unittest.TestCase):
    def test_sha_is_resolved_once_and_preserved_when_branch_moves(self):
        private = CollectionFixture()
        result = stats.collect(private, CalendarFixture(), START, END)
        self.assertEqual(len(private.tip_calls), 2)
        self.assertTrue(all(path.endswith('/commits/feature%2Fdefault') for path in private.tip_calls))
        self.assertTrue(all(call == {'sha': 'b' * 40, 'author': stats.USER, 'since': START, 'until': END} for call in private.commit_calls))
        self.assertEqual(result['commits'], 3)

    def test_public_private_language_bytes_are_aggregated_without_private_labels(self):
        result = stats.collect(CollectionFixture(), CalendarFixture(), START, END)
        self.assertEqual(result['repositories'], 2)
        self.assertEqual(result['languages'], {'Python': 20, 'Outras': 6})
        output = ''.join(stats.render(result, START, END).values())
        self.assertNotIn('private-fixture', output)
        self.assertNotIn('PRIVATE_LANGUAGE_SENTINEL', output)
        self.assertIn(stats.CALENDAR_NOTICE, output)

    def test_rejects_wrong_token_owner_and_public_only_access(self):
        private = CollectionFixture()
        private.owner = 'somebody-else'
        with self.assertRaisesRegex(stats.SafeFailure, 'TOKEN_OWNER'):
            stats.collect(private, CalendarFixture(), START, END)
        private.owner = stats.USER
        private.repos = private.repos[:1]
        with self.assertRaisesRegex(stats.SafeFailure, 'PRIVATE_ACCESS_REQUIRED'):
            stats.collect(private, CalendarFixture(), START, END)

    def test_rejects_other_authors_and_unlinked_authors(self):
        for author in ('somebody-else', ''):
            private = CollectionFixture()
            private.commit_author = author
            with self.assertRaisesRegex(stats.SafeFailure, 'COMMIT_AUTHOR'):
                stats.collect(private, CalendarFixture(), START, END)

    def test_duplicate_repository_is_an_error(self):
        private = CollectionFixture()
        private.repos.append(private.repos[-1])
        with self.assertRaises(stats.SafeFailure): stats.collect(private, CalendarFixture(), START, END)

    def test_missing_branch_only_allowed_for_empty_repositories(self):
        private = CollectionFixture()
        for repo in private.repos: repo['default_branch'] = None
        with self.assertRaisesRegex(stats.SafeFailure, 'BRANCH_MISSING'):
            stats.collect(private, CalendarFixture(), START, END)
        for repo in private.repos: repo['size'] = 0
        self.assertEqual(stats.collect(private, CalendarFixture(), START, END)['commits'], 0)

    def test_calendar_failure_never_falls_back_to_commit_count(self):
        public = CalendarFixture()
        with patch.object(public, 'request', side_effect=stats.SafeFailure('API_GRAPHQL')):
            with self.assertRaises(stats.SafeFailure): stats.collect(CollectionFixture(), public, START, END)

    def test_empty_and_invalid_tip_handling(self):
        private = CollectionFixture()
        original = private.request
        for tip in ([], {'sha': 'PRIVATE'}):
            def request(path, **kwargs):
                return tip if '/commits/' in path else original(path, **kwargs)
            with patch.object(private, 'request', side_effect=request):
                if tip == []:
                    self.assertEqual(stats.collect(private, CalendarFixture(), START, END)['commits'], 0)
                else:
                    with self.assertRaises(stats.SafeFailure): stats.collect(private, CalendarFixture(), START, END)


class MainRegressionTests(unittest.TestCase):
    def environment(self):
        return {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'paesdj1987/paesdj1987',
                'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
                'PROFILE_STATS_READ_TOKEN': 'github_pat_SYNTHETIC_TEST_ONLY', 'PUBLIC_API_TOKEN': 'SYNTHETIC'}

    def test_untrusted_events_fail_before_constructing_api(self):
        for key, value in (('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_EVENT_NAME', 'schedule'),
                           ('GITHUB_REF', 'refs/heads/untrusted'), ('GITHUB_REPOSITORY', 'fork/profile'),
                           ('PROFILE_STATS_READ_TOKEN', ''), ('GITHUB_ACTIONS', 'false')):
            env = {**self.environment(), key: value}
            with patch.dict(os.environ, env, clear=True), patch.object(sys, 'argv', ['stats', 'generate']), patch.object(stats, 'GitHub') as api:
                with self.assertRaises(stats.SafeFailure): stats.main()
                api.assert_not_called()

    def test_collect_failure_writes_no_svg_and_logs_no_private_data(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'cards'
            logs = StringIO()
            with patch.dict(os.environ, self.environment(), clear=True), patch.object(sys, 'argv', ['stats', 'generate', '--directory', str(output)]), patch.object(stats, 'GitHub'), patch.object(stats, 'collect', side_effect=RuntimeError('PRIVATE_TOKEN_AND_REPOSITORY')), redirect_stdout(logs), redirect_stderr(logs):
                self.assertEqual(stats.entrypoint(), 1)
            self.assertFalse(output.exists())
            self.assertNotIn('PRIVATE_TOKEN_AND_REPOSITORY', logs.getvalue())
            self.assertIn('UNEXPECTED', logs.getvalue())

    def test_entrypoint_exposes_only_fixed_safe_error_codes(self):
        for error, expected in ((stats.SafeFailure('API_AUTH'), 'API_AUTH'), (stats.SafeFailure('PRIVATE'), 'UNEXPECTED')):
            logs = StringIO()
            with patch.object(stats, 'main', side_effect=error), redirect_stderr(logs):
                self.assertEqual(stats.entrypoint(), 1)
            self.assertIn(expected, logs.getvalue())
            self.assertNotIn('[PRIVATE]', logs.getvalue())

    def test_generation_uses_exact_365_day_interval_and_canonical_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'cards'
            metrics = {'calendar': 12, 'commits': 4, 'repositories': 2, 'languages': {'Python': 10}}
            with patch.dict(os.environ, self.environment(), clear=True), patch.object(sys, 'argv', ['stats', 'generate', '--directory', str(output)]), patch.object(stats, 'GitHub'), patch.object(stats, 'collect', return_value=metrics) as collect, redirect_stdout(StringIO()):
                stats.main()
            start, end = collect.call_args.args[-2:]
            self.assertEqual(stats.datetime.fromisoformat(end) - stats.datetime.fromisoformat(start), stats.timedelta(days=365))
            self.assertEqual(set(stats.validate_directory(output)), set(stats.FILES))

    def test_prepare_publishes_validated_snapshot_not_reread_artifact(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            artifact = Path('artifact')
            cards = {n: v.encode() for n, v in PrivacyTests().cards().items()}
            stats.write_cards(artifact, cards)
            Path('README.md').write_text('# Profile\n\n## GitHub Stats\nOLD\n\n---\n\n## Tech Stack\nPRESERVED\n', encoding='utf-8')
            original_validate = stats.validate_directory
            def validate_and_change_input(path):
                verified = original_validate(path)
                if path == artifact:
                    (artifact / 'stats.svg').write_bytes(b'PRIVATE_MUTATION_AFTER_VALIDATION')
                return verified
            with patch.object(stats, 'validate_directory', side_effect=validate_and_change_input), patch.object(sys, 'argv', ['stats', 'prepare', '--directory', 'artifact']), redirect_stdout(StringIO()):
                stats.main()
            self.assertEqual(original_validate(Path('profile')), cards)
            self.assertTrue(Path('README.md').read_text(encoding='utf-8').endswith('## Tech Stack\nPRESERVED\n'))

    def test_schedule_absent_and_publication_hard_disabled(self):
        workflow = Path('.github/workflows/profile-stats.yml').read_text(encoding='utf-8')
        self.assertNotIn('\n  schedule:', workflow)
        self.assertNotIn('PROFILE_STATS_AUTO_PUBLISH', workflow)
        publish = workflow.split('\n  publish:', 1)[1]
        self.assertIn('if: ${{ false }}', publish)
        self.assertNotIn('PROFILE_STATS_READ_TOKEN', publish)
        self.assertIn('verify-index --directory profile', publish)
        self.assertIn('profile_stats.py prepare', publish)


if __name__ == '__main__':
    unittest.main()
