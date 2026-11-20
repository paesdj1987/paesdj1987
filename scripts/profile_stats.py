"""Aggregate GitHub metadata without persisting repository identities or API responses.

generate requires Actions Secrets; validate/activate never authenticate.
Only the Python standard library is used. Never print caught exceptions or payloads.
"""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, parse_qsl, quote
from urllib.request import Request, HTTPRedirectHandler, build_opener
import xml.etree.ElementTree as ET

USER = "paesdj1987"
FILES = ("stats.svg", "top-langs.svg")
COLORS = {"background": "#0d1117", "title": "#58a6ff", "text": "#c9d1d9",
          "accent": "#22d3ee", "border": "#30363d"}
# Closed vocabulary: API strings can never become public labels or SVG markup.
LANGUAGES = {"Python", "SQL", "JavaScript", "TypeScript", "HTML", "CSS", "C", "C++",
             "C#", "Java", "Go", "Rust", "Ruby", "PHP", "Shell", "PowerShell", "R",
             "Jupyter Notebook", "Dockerfile", "Makefile", "Scala", "Kotlin", "Swift",
             "Dart", "Lua", "Vue", "Svelte", "HCL", "PLpgSQL", "TSQL", "PLSQL"}
ROWS = ("Contribuições · calendário", "Commits · branches padrão", "Repositórios analisados")
PALETTE = ("#58a6ff", "#22d3ee", "#a5b4fc", "#38bdf8", "#818cf8", "#67e8f9", "#94a3b8", "#64748b")
SVG_NS = "http://www.w3.org/2000/svg"
MAX_PAGES = 1000
CALENDAR_NOTICE = "Calendário: cobertura privada não comprovada."
SAFE_CODES = {"INVALID_DATA", "API_URL", "API_REDIRECT", "API_STATUS", "API_SIZE",
              "API_JSON", "API_GRAPHQL", "API_NETWORK", "API_HTTP", "API_AUTH",
              "API_FORBIDDEN", "API_RATE_LIMIT", "API_SERVER", "API_LINK", "API_PAGE",
              "PAGINATION_LIMIT", "TOKEN_OWNER", "PRIVATE_ACCESS_REQUIRED", "BRANCH_MISSING",
              "COMMIT_AUTHOR", "SVG_SIZE", "SVG_MARKUP", "SVG_NAMESPACE", "SVG_SCHEMA",
              "SVG_CANONICAL", "SVG_DATE", "OUTPUT_FILES", "OUTPUT_FILE", "INDEX_BYTES",
              "README_SECTION", "ACTIONS_ONLY", "REPOSITORY", "TRUSTED_EVENT", "BRANCH",
              "FINE_GRAINED_TOKEN_REQUIRED", "CALENDAR_TOKEN_REQUIRED"}


class SafeFailure(Exception):
    """A fixed error code, containing no API-derived strings."""


def require(condition, code="INVALID_DATA"):
    if not condition:
        raise SafeFailure(code)


def integer(value):
    require(type(value) is int and 0 <= value < 10**15)
    return value


def api_url(value):
    require(isinstance(value, str) and not re.search(r'[\x00-\x20\x7f\\]', value), "API_URL")
    if value.startswith('/') and not value.startswith('//'):
        value = 'https://api.github.com' + value
    parts = urlsplit(value)
    require(parts.scheme == 'https' and parts.netloc == 'api.github.com'
            and parts.path.startswith('/') and not parts.fragment, "API_URL")
    require(not re.search(r'%(?![0-9A-Fa-f]{2})', value), "API_URL")
    return value


def next_page(link, current, base):
    """GitHub's Link header, never response length, controls pagination."""
    if not link:
        return None
    require(isinstance(link, str) and len(link) < 16000, "API_LINK")
    current_page = int(dict(parse_qsl(urlsplit(current).query))['page'])
    relations = {}
    for part in link.split(','):
        match = re.fullmatch(r'\s*<([^<>]+)>;\s*rel="(next|prev|first|last)"\s*', part)
        require(match is not None, "API_LINK")
        target, relation = match.groups()
        require(relation not in relations, "API_LINK")
        target = api_url(target)
        parsed = urlsplit(target)
        require(parsed.path == urlsplit(current).path, "API_LINK")
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        require(len(query) == len(pairs) and re.fullmatch(r'[1-9][0-9]*', query.get('page', '')), "API_LINK")
        page = int(query.pop('page'))
        require(query == base, "API_LINK")
        require((relation != 'next' or page == current_page + 1)
                and (relation != 'prev' or page == current_page - 1)
                and (relation != 'first' or page == 1)
                and (relation != 'last' or page >= current_page), "API_LINK")
        relations[relation] = (target, page)
    require('next' in relations or 'last' not in relations or relations['last'][1] == current_page, "API_LINK")
    return relations.get('next', (None, None))[0]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authorization to a location supplied by an HTTP response.
        raise SafeFailure("API_REDIRECT")


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, path, body=None, allow_empty=False, with_headers=False):
        url = api_url(path)
        payload = None if body is None else json.dumps(body).encode()
        req = Request(url, data=payload, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "paesdj1987-profile-stats",
        })
        try:
            with self.opener.open(req, timeout=30) as response:
                require(response.status == 200, "API_STATUS")
                raw = response.read(12_000_001)
                require(len(raw) <= 12_000_000, "API_SIZE")
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise SafeFailure("API_JSON") from None
                require(not isinstance(result, dict) or not result.get("errors"), "API_GRAPHQL")
                return (result, response.headers.get('Link', '')) if with_headers else result
        except HTTPError as error:
            # Only this documented empty-repository condition becomes zero commits.
            # All permission, rate-limit and other errors abort publication.
            if allow_empty and error.code == 409:
                try:
                    data = json.loads(error.read(4096))
                    if data.get("message", "").rstrip(".") == "Git Repository is empty":
                        return ([], '') if with_headers else []
                except (ValueError, AttributeError):
                    pass
            code = {401: 'API_AUTH', 403: 'API_FORBIDDEN', 429: 'API_RATE_LIMIT'}.get(error.code)
            raise SafeFailure(code or ('API_SERVER' if error.code >= 500 else 'API_HTTP')) from None
        except (URLError, TimeoutError, OSError):
            raise SafeFailure('API_NETWORK') from None

    def pages(self, path, params, allow_empty=False):
        require('?' not in path and 'page' not in params and 'per_page' not in params, 'API_PAGE')
        base = {key: str(value) for key, value in {**params, 'per_page': 100}.items()}
        url = api_url(path + '?' + urlencode({**base, 'page': 1}))
        seen = set()
        for _ in range(MAX_PAGES):
            require(url not in seen, 'API_LINK')
            seen.add(url)
            data, link = self.request(url, allow_empty=allow_empty, with_headers=True)
            require(isinstance(data, list) and len(data) <= 100, 'API_PAGE')
            following = next_page(link, url, base)
            require(data or not following, 'API_PAGE')
            yield from data
            if following is None:
                return
            url = following
        raise SafeFailure("PAGINATION_LIMIT")


def calendar_total(api, start, end):
    # Use the repository-scoped GITHUB_TOKEN, never the cross-repository PAT.
    # This asks for the calendar aggregate that GitHub exposes for the public profile.
    query = """query($login:String!,$from:DateTime!,$to:DateTime!) {
      user(login:$login) {
        contributionsCollection(from:$from,to:$to) {
          contributionCalendar { totalContributions }
        }
      }
    }"""
    result = api.request("/graphql", {"query": query, "variables": {
        "login": USER, "from": start, "to": end}})
    return integer(result["data"]["user"]["contributionsCollection"]
                   ["contributionCalendar"]["totalContributions"])


def collect(private_api, public_api, start, end):
    require(private_api.request("/user").get("login", "").lower() == USER, "TOKEN_OWNER")
    repos = list(private_api.pages("/user/repos", {"affiliation": "owner", "visibility": "all"}))
    repos = [r for r in repos if r["owner"]["login"].lower() == USER and not r["fork"]]
    # Prevent a public-only fallback from being advertised as private coverage.
    require(any(r["private"] for r in repos), "PRIVATE_ACCESS_REQUIRED")
    seen_repos, commits, languages = set(), set(), Counter()
    for repo in repos:
        require(repo["id"] not in seen_repos)
        seen_repos.add(repo["id"])
        name = repo["name"]
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", name) and name not in {'.', '..'})
        endpoint = f"/repos/{USER}/{name}"
        sizes = private_api.request(endpoint + "/languages")
        require(isinstance(sizes, dict))
        for language, size in sizes.items():
            languages[language if language in LANGUAGES else "Outras"] += integer(size)
        # Resolve once: every page traverses the same reachable Git history.
        if repo.get("default_branch"):
            branch = repo["default_branch"]
            require(isinstance(branch, str))
            tip = private_api.request(endpoint + '/commits/' + quote(branch, safe=''), allow_empty=True)
            if tip == []:  # Only the explicit empty-repository HTTP condition returns [].
                continue
            require(isinstance(tip, dict) and re.fullmatch(r'[0-9a-f]{40,64}', tip.get('sha', '')))
            for commit in private_api.pages(endpoint + "/commits", {
                "sha": tip['sha'], "author": USER, "since": start, "until": end}):
                sha = commit.get("sha", "")
                require(re.fullmatch(r"[0-9a-f]{40,64}", sha))
                require(isinstance(commit.get('author'), dict)
                        and commit['author'].get('login', '').lower() == USER, 'COMMIT_AUTHOR')
                commits.add(sha)
        else:
            require(type(repo.get('size')) is int and repo['size'] == 0, 'BRANCH_MISSING')
    # Repository names, URLs, messages and SHA identifiers never cross this boundary.
    return {"calendar": calendar_total(public_api, start, end), "commits": len(commits),
            "repositories": len(seen_repos), "languages": dict(languages)}


def language_rows(sizes):
    require(isinstance(sizes, dict) and all(k in LANGUAGES | {"Outras"} for k in sizes))
    entries = sorted(((k, integer(v)) for k, v in sizes.items() if v), key=lambda x: (-x[1], x[0]))
    # Keep all bytes in the denominator, including less frequent languages.
    main = [(k, v) for k, v in entries if k != "Outras"][:7]
    remainder = sum(v for k, v in entries if k not in dict(main))
    return main + ([("Outras", remainder)] if remainder else [])


def text(x, y, value, *, color="text", size=12, anchor="start"):
    return (f'<text x="{x}" y="{y}" fill="{COLORS[color]}" font-size="{size}" '
            f'text-anchor="{anchor}" font-family="Segoe UI,Arial,sans-serif">{escape(str(value))}</text>')


def frame(width, title, body, start, end, snapshot=False):
    # Fixed labels and aggregate numeric values only; no metadata, links or scripts.
    return (f'<svg xmlns="{SVG_NS}" width="{width}" height="240" viewBox="0 0 {width} 240" '
            f'role="img" aria-label="{escape(title)}">'
            f'<rect x="0.5" y="0.5" width="{width - 1}" height="239" rx="8" '
            f'fill="{COLORS["background"]}" stroke="{COLORS["border"]}"/>'
            + text(18, 30, title, color="title", size=16) + body
            + text(18, 211, 'Fotografia atual · bytes de código' if snapshot else
                   f'{start[:10]} a {end[:10]} · UTC', size=10)
            + text(18, 229, 'Atualizado em ' + end[:10], size=10) + '</svg>')


def stats_svg(values, start, end):
    body = ''.join(text(18, 70 + i * 36, label) +
                   text(419, 70 + i * 36, value, color="accent", anchor="end")
                   for i, (label, value) in enumerate(zip(ROWS, values)))
    body += text(18, 185, "Calendário e commits têm critérios distintos.", size=10)
    body += text(18, 168, CALENDAR_NOTICE, size=10)
    return frame(440, "Atividade · públicos e privados", body, start, end)


def languages_svg(rows, start, end):
    body = text(18, 51, "Bytes de código · repositórios analisados", size=10)
    for i, (label, percentage) in enumerate(rows):
        y = 70 + i * 17
        body += (f'<rect x="18" y="{y - 8}" width="7" height="7" rx="2" fill="{PALETTE[i]}"/>'
                 + text(32, y, label, size=11)
                 + text(320, y, percentage, color="accent", size=11, anchor="end"))
    if not rows:
        body += text(18, 80, "Sem linguagens detectadas.", size=11)
    return frame(340, "Linguagens · públicos e privados", body, start, end, snapshot=True)


def render(metrics, start, end):
    values = [integer(metrics[k]) for k in ("calendar", "commits", "repositories")]
    rows = language_rows(metrics["languages"])
    total = sum(value for _, value in rows)
    percentages = [(label, f'{value / total * 100:.1f}%') for label, value in rows]
    return dict(zip(FILES, (stats_svg(values, start, end), languages_svg(percentages, start, end))))


def validate_svg(svg, filename):
    """Rebuild from aggregate slots; accept only byte-identical canonical output.

    Nothing from XML attributes, namespaces or arbitrary text is forwarded.
    Reject alternate encodings, entities and hidden XML rather than normalizing them.
    """
    require(filename in FILES, 'SVG_SCHEMA')
    raw = svg.encode('utf-8') if isinstance(svg, str) else svg
    require(isinstance(raw, bytes) and len(raw) <= 32_000, 'SVG_SIZE')
    require(not any(token in raw for token in (b'<!', b'<?', b'&', b'\r', b'\x00')), 'SVG_MARKUP')
    try:
        raw.decode('utf-8', errors='strict')
        parser = ET.iterparse(BytesIO(raw), events=('start-ns',))
        namespaces = [value for _, value in parser]
        root = parser.root
    except (UnicodeError, ET.ParseError):
        raise SafeFailure('SVG_SCHEMA') from None
    require(namespaces == [('', SVG_NS)], 'SVG_NAMESPACE')
    require(root.tag == f'{{{SVG_NS}}}svg', 'SVG_SCHEMA')
    texts = [node.text or '' for node in root.findall(f'{{{SVG_NS}}}text')]
    require(len(texts) >= 5, 'SVG_SCHEMA')
    update = re.fullmatch(r'Atualizado em ([0-9]{4}-[0-9]{2}-[0-9]{2})', texts[-1])
    require(update is not None, 'SVG_DATE')
    try:
        end_date = datetime.strptime(update[1], '%Y-%m-%d').date()
        start_date = end_date - timedelta(days=365)
    except ValueError:
        raise SafeFailure('SVG_DATE') from None
    start, end = start_date.isoformat(), end_date.isoformat()
    if filename == FILES[0]:
        require(len(texts) == 11, 'SVG_SCHEMA')
        numbers = [texts[i] for i in (2, 4, 6)]
        require(all(re.fullmatch(r'0|[1-9][0-9]{0,14}', n) for n in numbers), 'SVG_SCHEMA')
        canonical = stats_svg([integer(int(n)) for n in numbers], start, end)
    else:
        if texts[2] == 'Sem linguagens detectadas.':
            require(len(texts) == 5, 'SVG_SCHEMA')
            rows = []
        else:
            require((len(texts) - 4) % 2 == 0, 'SVG_SCHEMA')
            slots = texts[2:-2]
            rows = list(zip(slots[::2], slots[1::2]))
            require(1 <= len(rows) <= 8, 'SVG_SCHEMA')
            require(len({label for label, _ in rows}) == len(rows), 'SVG_SCHEMA')
            require(all(label in LANGUAGES | {'Outras'} for label, _ in rows), 'SVG_SCHEMA')
            require(all(re.fullmatch(r'(?:0|[1-9][0-9]?|100)\.[0-9]%', p) for _, p in rows), 'SVG_SCHEMA')
            tenths = [int(p[:-1].replace('.', '')) for _, p in rows]
            require(all(n <= 1000 for n in tenths) and 2 * abs(sum(tenths) - 1000) <= len(rows), 'SVG_SCHEMA')
            require(all(label != 'Outras' for label, _ in rows[:-1]), 'SVG_SCHEMA')
        canonical = languages_svg(rows, start, end)
    encoded = canonical.encode('utf-8')
    require(raw == encoded, 'SVG_CANONICAL')
    return encoded


def validate_directory(directory):
    require(directory.is_dir() and not directory.is_symlink()
            and {p.name for p in directory.iterdir()} == set(FILES), "OUTPUT_FILES")
    verified = {}
    for name in FILES:
        p = directory / name
        require(p.is_file() and not p.is_symlink() and p.stat().st_size <= 32_000, "OUTPUT_FILE")
        verified[name] = validate_svg(p.read_bytes(), name)
    return verified


def write_cards(directory, verified):
    require(set(verified) == set(FILES), 'OUTPUT_FILES')
    for name in FILES:
        validate_svg(verified[name], name)
    require(not directory.is_symlink(), 'OUTPUT_FILE')
    directory.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        path = directory / name
        require(not path.is_symlink(), 'OUTPUT_FILE')
        path.write_bytes(verified[name])
    require(validate_directory(directory) == verified, 'OUTPUT_FILE')


def verify_index(directory):
    verified = validate_directory(directory)
    for name in FILES:
        staged = subprocess.check_output(['git', 'show', f':profile/{name}'], stderr=subprocess.DEVNULL)
        require(validate_svg(staged, name) == verified[name], 'INDEX_BYTES')


def activate(readme):
    source = readme.read_text(encoding="utf-8")
    heading = "## GitHub Stats\n"
    require(source.count(heading) == 1, "README_SECTION")
    before, rest = source.split(heading)
    require('\n---\n\n## Tech Stack' in rest, "README_SECTION")
    _, after = rest.split('\n---\n\n## Tech Stack', 1)
    section = '''
<p align="center">
  <a href="https://github.com/paesdj1987">
    <img width="440" src="./profile/stats.svg" alt="Atividade agregada: contribuições do calendário, commits e repositórios analisados." />
  </a>
  <a href="https://github.com/paesdj1987?tab=repositories">
    <img width="340" src="./profile/top-langs.svg" alt="Linguagens agregadas dos repositórios públicos e privados analisados." />
  </a>
</p>

<sub>Commits de paesdj1987 no histórico alcançável pelo SHA capturado da branch padrão de cada repositório próprio acessível ao token, público ou privado, sem forks. Não inclui todas as branches. SHAs repetidos contam uma vez. Período: últimos 365 dias em UTC. O calendário é um indicador separado retornado pela API; sua cobertura de atividade privada não está comprovada.</sub>

<sub>Linguagens: proporção de bytes de código nos mesmos repositórios, incluindo arquivados; é uma fotografia atual, não uma métrica de atividade ou proficiência. Cobertura limitada ao acesso concedido. Os SVGs publicam somente métricas agregadas, sem identificar repositórios privados.</sub>

[![Calendário de contribuições no GitHub](https://img.shields.io/badge/GitHub-Calendário_de_contribuições-164E63?style=flat-square&logo=github&logoColor=white)](https://github.com/paesdj1987)

[Ver repositórios públicos](https://github.com/paesdj1987?tab=repositories) · [Ver perfil e contribuições](https://github.com/paesdj1987)
'''
    readme.write_text(before + heading + section + '\n---\n\n## Tech Stack' + after, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["generate", "validate", "activate", "prepare", "verify-index"])
    parser.add_argument("--directory", type=Path, default=Path("profile"))
    args = parser.parse_args()
    if args.command == "generate":
        # Never run outside the reviewed Actions context; no PR events are accepted.
        require(os.environ.get("GITHUB_ACTIONS") == "true", "ACTIONS_ONLY")
        require(os.environ.get("GITHUB_REPOSITORY") == f"{USER}/{USER}", "REPOSITORY")
        require(os.environ.get("GITHUB_EVENT_NAME") == 'workflow_dispatch', "TRUSTED_EVENT")
        require(os.environ.get("GITHUB_REF") == "refs/heads/main", "BRANCH")
        token = os.environ.get("PROFILE_STATS_READ_TOKEN", "")
        require(token.startswith("github_pat_"), "FINE_GRAINED_TOKEN_REQUIRED")
        public_token = os.environ.get("PUBLIC_API_TOKEN", "")
        require(bool(public_token), "CALENDAR_TOKEN_REQUIRED")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start = (now - timedelta(days=365)).isoformat().replace('+00:00', 'Z')
        end = now.isoformat().replace('+00:00', 'Z')
        metrics = collect(GitHub(token), GitHub(public_token), start, end)
        output = render(metrics, start, end)
        # Validate the complete pair before writing either SVG.
        for name, svg in output.items():
            validate_svg(svg, name)
        write_cards(args.directory, {name: validate_svg(svg, name) for name, svg in output.items()})
    elif args.command == 'verify-index':
        verify_index(args.directory)
    else:
        verified = validate_directory(args.directory)
        if args.command == 'prepare':
            # Write the validated in-memory bytes, never re-copy a mutable artifact.
            write_cards(Path('profile'), verified)
            activate(Path('README.md'))
        elif args.command == "activate":
            activate(Path("README.md"))
    print("Cards agregados validados.")


def entrypoint():
    try:
        main()
    except Exception as error:
        # Exception text/tracebacks could disclose private repo URLs or messages.
        code = str(error) if isinstance(error, SafeFailure) and str(error) in SAFE_CODES else 'UNEXPECTED'
        print(f"::error::Falha segura [{code}]. Nenhum detalhe privado foi registrado; publicação interrompida.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(entrypoint())
