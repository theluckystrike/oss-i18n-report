#!/usr/bin/env python3
"""Reproducible benchmark of user-visible i18n bugs in open-source apps.

For every target in targets.json:
  1. shallow, blob-less clone (default branch, last N days of history)
  2. discover locale file groups (same path, one file per locale)
  3. sparse-checkout only those files plus a few docs/config files
  4. compare each translated string with its source string using the
     vendored i18n-audit classifier (tools/i18n_audit_seed.py)
  5. detect a translation management system (TMS) from config files,
     docs and the authors of recent locale commits
  6. measure recent merge activity of occasional (outside) contributors
     from git history
Outputs: data/<owner>__<repo>.json, data/summary.csv, data/skipped.csv,
data/pr-targets.csv.

Usage: python3 bench.py [--only owner/repo ...] [--jobs 6] [--refresh]
       python3 bench.py --check PATH     (CI mode: audit one checkout, exit 1 on findings)
"""
import argparse
import collections
import concurrent.futures
import csv
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import i18n_audit_seed as seed  # noqa: E402

DATA = os.path.join(ROOT, 'data')
WORK = os.environ.get('BENCH_WORK', os.path.join(ROOT, '.work'))
TOOL_SHA = '34d6ce1e1c1b55ea7c8895891c73553e736c6c62'
REAL = ('syntax', 'renamed', 'dropped', 'extra', 'broken', 'mixed')
INFO = ('tag', 'plural_count')
KINDS = REAL + INFO
MAX_FINDINGS_STORED = 5000

# ------------------------------------------------------------------ locales
LANGS = set((
    'aa ab af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz '
    'ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii '
    'ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi '
    'mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw '
    'sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk '
    'ur uz ve vi vo wa wo xh yi yo za zh zu '
    'fil ckb kab ast yue haw szl sat chr zgh tok frp hsb dsb sco gsw nds ber kmr mai pcm bal cnr ace arn '
    'bar ceb crh csb fur ksh lij lmo ltg mfe mhr nap oci pap scn sma tzm vec zgh').split())
COMMON = {'de', 'fr', 'es', 'ja', 'zh', 'pt', 'ru', 'it', 'ko', 'nl', 'pl', 'tr', 'uk', 'sv', 'cs', 'ar'}
LOC_RE = re.compile(r'^([A-Za-z]{2,3})((?:[-_](?:[A-Z][a-z]{3}|[a-z]{4}|[A-Za-z]{2}|\d{3}))*)(@[a-z]+)?$')
PSEUDO = {'pseudo', 'ach', 'ach-ug', 'ach_ug', 'en-xa', 'en_xa', 'en-xb', 'qps-ploc', 'zz'}
SOURCE_PREF = ['en', 'en-US', 'en_US', 'en-us', 'en_us', 'en-GB', 'en_GB', 'en-gb', 'en_gb', 'EN']

EXTS = ('.json', '.po', '.yml', '.yaml', '.arb', '.ts', '.js', '.mjs')
HINT = re.compile(r'(locale|i18n|l10n|lang|translat|messages|intl|phrases|strings)', re.I)
EXCLUDE = re.compile(
    r'(^|/)(node_modules|vendor|vendors|__tests__|__mocks__|__fixtures__|tests?|spec|specs|fixtures?|e2e|'
    r'cypress|playwright|examples?|docs?|website|\.github|\.storybook|storybook|dist|build|third[-_]party|'
    r'deps|mocks?|testdata|test-data|stories)(/|$)', re.I)
SKIP_FILES = {'package.json', 'tsconfig.json', 'project.json', 'jsconfig.json', 'index.d.ts'}


def is_locale(seg):
    m = LOC_RE.match(seg)
    if not m:
        return False
    return m.group(1).lower() in LANGS


def lang_of(loc):
    return re.split(r'[-_@]', loc)[0].lower()


def candidates(path):
    """Yield (pattern, loc) for every path segment that looks like a locale code."""
    parts = path.split('/')
    fname = parts[-1]
    for i, seg in enumerate(parts[:-1]):
        if is_locale(seg):
            yield '/'.join(parts[:i] + ['{loc}'] + parts[i + 1:]), seg
    dots = fname.split('.')
    for j in range(len(dots) - 1):
        p = dots[j]
        if is_locale(p):
            yield '/'.join(parts[:-1] + ['.'.join(dots[:j] + ['{loc}'] + dots[j + 1:])]), p
        for m in re.finditer(r'[-_]', p):
            pre, rest = p[:m.start()], p[m.end():]
            if len(pre) >= 3 and pre.isalpha() and is_locale(rest):
                yield '/'.join(parts[:-1] + ['.'.join(dots[:j] + [p[:m.end()] + '{loc}'] + dots[j + 1:])]), rest


def discover(files, exclude=()):
    """Group files into locale groups. Returns list of dicts
    {pattern, ext, source_loc, files: {loc: {ns: path}}}."""
    fileset = set(files)
    cands = collections.defaultdict(dict)  # pattern -> {loc: path}
    per_file = collections.defaultdict(list)
    for f in files:
        low = f.lower()
        if not low.endswith(EXTS) or EXCLUDE.search(f) or not HINT.search(f):
            continue
        base = os.path.basename(f)
        if base in SKIP_FILES or re.search(r'\.(d|test|spec|stories)\.[jt]s$', base) or '.config.' in base:
            continue
        if any(x in f for x in exclude):
            continue
        for pat, loc in candidates(f):
            # namespace grouping: when {loc} is a directory, the rest of the path is a namespace
            head, sep, tail = pat.partition('{loc}/')
            if sep:
                ext = os.path.splitext(tail)[1]
                key = head + '{loc}/*' + ext
                ns = tail
            else:
                key, ns = pat, ''
            cands[key].setdefault(loc, {})[ns] = f
            per_file[f].append(key)
    # assign each file to its largest candidate group
    size = {k: len(v) for k, v in cands.items()}
    groups = collections.defaultdict(lambda: collections.defaultdict(dict))
    for f, keys in per_file.items():
        best = max(keys, key=lambda k: (size[k], -k.count('/')))
        for loc, nsmap in cands[best].items():
            for ns, p in nsmap.items():
                if p == f:
                    groups[best][loc][ns] = p
    out = []
    for pat, locs in groups.items():
        src = next((c for c in SOURCE_PREF if c in locs), None)
        files_by_loc = {k: dict(v) for k, v in locs.items()}
        if src is None and '{loc}' in pat:
            # prefix pattern (main-{loc}.json) whose source has no suffix (main.json)
            for sepch in ('-', '_'):
                plain = pat.replace(sepch + '{loc}', '')
                if plain != pat and plain in fileset:
                    src = '__source__'
                    files_by_loc[src] = {'': plain}
                    break
        if src is None and pat.endswith('.po'):
            src = '__msgid__'  # gettext catalogs whose msgid is the English source
            files_by_loc[src] = {}
        if src is None:
            continue
        others = [l for l in files_by_loc if l != src and l.lower() not in PSEUDO and lang_of(l) != 'en']
        if len(others) < 2 or not any(lang_of(l) in COMMON for l in others):
            continue
        ext = os.path.splitext(pat)[1]
        if ext in ('.ts', '.js', '.mjs') and pat.endswith('/*' + ext):
            # TS/JS locale directories usually re-export everything from index
            if all('index' + ext in v for v in files_by_loc.values() if v):
                files_by_loc = {k: {'': v['index' + ext]} for k, v in files_by_loc.items() if 'index' + ext in v}
        out.append(dict(pattern=pat, ext=ext, source_loc=src, files=files_by_loc))
    return out


def explicit_groups(spec, files):
    fileset = set(files)
    out = []
    for g in spec:
        pat = g['pattern']
        rx = '^' + re.escape(pat).replace(r'\{loc\}', '(?P<loc>[^/]+)').replace(r'\*', '(?P<ns>[^/]+)') + '$'
        locs = collections.defaultdict(dict)
        for f in files:
            m = re.match(rx, f)
            if m:
                locs[m.group('loc')][m.groupdict().get('ns') or ''] = f
        src = g.get('source_loc')
        if g.get('source') and g['source'] in fileset:
            src = '__source__'
            locs[src] = {'': g['source']}
        if not src or src not in locs:
            src = next((c for c in SOURCE_PREF if c in locs), None)
        if not src and pat.endswith('.po'):
            src = '__msgid__'
            locs[src] = {}
        if src:
            out.append(dict(pattern=pat, ext=os.path.splitext(pat)[1], source_loc=src, files=dict(locs)))
    return out


# ------------------------------------------------------------------ loaders
MSG_FIELDS = ('defaultMessage', 'message', 'string', 'translation')
MSG_META = set(MSG_FIELDS) | {'description', 'context', 'placeholders', 'meaning', 'comment', 'developer_comment'}


def collapse(o):
    """Message objects ({"defaultMessage": ..., "description": ...}, Chrome's {"message": ...},
    {"string": ..., "context": ...}) become their message string."""
    if isinstance(o, dict):
        if o and set(o) <= MSG_META:
            for f in MSG_FIELDS:
                if isinstance(o.get(f), str):
                    return o[f]
        return {k: collapse(v) for k, v in o.items()}
    if isinstance(o, list):
        return [collapse(v) for v in o]
    return o


def load_json_file(path):
    with open(path, encoding='utf-8-sig') as f:
        obj = json.load(f)
    if isinstance(obj, list) and obj and all(isinstance(x, dict) and 'id' in x for x in obj):
        # go-i18n format: [{"id": ..., "translation": ...}]
        res = {}
        for x in obj:
            t = x.get('translation')
            if isinstance(t, str):
                res[x['id']] = t
            elif isinstance(t, dict):
                for k, v in t.items():
                    if isinstance(v, str):
                        res[f"{x['id']}_{k}"] = v
        return res
    d = seed.flat(collapse(obj))
    return {k: v for k, v in d.items() if not k.startswith('@') and not k.startswith('$schema')}


def load_yaml_file(path, loc):
    import yaml
    with open(path, encoding='utf-8-sig') as f:
        obj = yaml.safe_load(f)
    if isinstance(obj, dict) and len(obj) == 1:
        (k, v), = obj.items()
        if isinstance(k, str) and (k == loc or is_locale(k)) and isinstance(v, dict):
            obj = v
    return seed.flat(obj) if isinstance(obj, (dict, list)) else {}


def load_po_file(path):
    """gettext PO -> ({key: msgstr}, {key: msgid}). Skips fuzzy and obsolete entries.
    Plural forms become key[n]; key = msgctxt + \\x04 + msgid."""
    entries, cur, field = [], {}, None

    def unq(s):
        try:
            return json.loads(s, strict=False)
        except Exception:
            return s.strip('"')

    def has_str():
        return any(k.startswith('msgstr') for k in cur)

    with open(path, encoding='utf-8-sig') as f:
        for line in f:
            s = line.strip()
            if s.startswith('#~'):
                continue
            if not s or s.startswith('#'):
                if has_str():
                    entries.append(cur)
                    cur, field = {}, None
                if s.startswith('#,') and 'fuzzy' in s:
                    cur['fuzzy'] = True
                continue
            m = re.match(r'^(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?)\s+(".*")$', s)
            if m:
                if m.group(1) in ('msgctxt', 'msgid') and has_str():
                    entries.append(cur)
                    cur = {}
                field = m.group(1)
                cur[field] = unq(m.group(2))
            elif s.startswith('"') and field:
                cur[field] += unq(s)
    if has_str():
        entries.append(cur)
    entries = [e for e in entries if e.get('msgid')]
    strs, ids = {}, {}
    for e in entries:
        if e.get('fuzzy'):
            continue
        base = (e['msgctxt'] + '\x04' if e.get('msgctxt') else '') + e['msgid']
        if 'msgid_plural' in e:
            for k, v in e.items():
                mm = re.match(r'msgstr\[(\d+)\]', k)
                if mm:
                    n = int(mm.group(1))
                    strs[f'{base}[{n}]'] = v
                    ids[f'{base}[{n}]'] = e['msgid'] if n == 0 else e['msgid_plural']
        else:
            strs[base] = e.get('msgstr', '')
            ids[base] = e['msgid']
    return strs, ids


def load_js_files(paths, cwd):
    if not paths:
        return {}
    script = os.path.join(ROOT, 'tools', 'load_js.mjs')
    res = {}
    for i in range(0, len(paths), 50):
        chunk = paths[i:i + 50]
        p = subprocess.run(['node', script] + chunk, cwd=cwd, capture_output=True, text=True, timeout=600)
        if p.returncode == 0 and p.stdout:
            res.update(json.loads(p.stdout))
        else:
            for c in chunk:
                res[c] = {'__error__': (p.stderr or 'node failed')[:300]}
    return res


# ------------------------------------------------------------------ git
def git(args, cwd, check=True, timeout=1800):
    p = subprocess.run(['git'] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {p.stderr.strip()[:300]}")
    return p.stdout


def clone(repo, dest, since, refresh=False):
    marker = os.path.join(dest, '.git', 'bench_mode')
    if os.path.exists(marker) and not refresh:
        return open(marker).read().strip()
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    url = f'https://github.com/{repo}.git'
    base = ['clone', '-q', '--filter=blob:none', '--no-checkout', '--single-branch']
    try:
        git(base + [f'--shallow-since={since}', url, dest], cwd=WORK)
        mode = 'history'
    except RuntimeError:
        shutil.rmtree(dest, ignore_errors=True)
        git(base + ['--depth', '1', url, dest], cwd=WORK)
        mode = 'depth1'
    with open(marker, 'w') as f:
        f.write(mode)
    return mode


def sparse_checkout(dest, paths, dirs=()):
    git(['sparse-checkout', 'init', '--no-cone'], cwd=dest)
    pats = '\n'.join(['/' + d.rstrip('/') + '/' for d in sorted(set(dirs))] + ['/' + p.replace('\\', '\\\\').replace('*', '\\*').replace('?', '\\?').replace('[', '\\[')
                     .replace('!', '\\!').replace('#', '\\#') for p in sorted(set(paths))])
    with open(os.path.join(dest, '.git', 'info', 'sparse-checkout'), 'w') as f:
        f.write(pats + '\n')
    git(['checkout', '-q', 'HEAD'], cwd=dest, timeout=3600)


# ------------------------------------------------------------------ metadata
def http_json(url, cache):
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'oss-i18n-report'}), timeout=30) as r:
            obj = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        obj = {'__error__': str(e)[:200]}
    if '__error__' not in obj:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, 'w') as f:
            json.dump(obj, f)
    return obj


def repo_meta(repo):
    """Stars, last push and owner type from public mirrors of the GitHub API
    (repos.ecosyste.ms, falling back to ungh.cc). api.github.com was not reachable
    from the environment this benchmark was built in."""
    owner = repo.split('/')[0]
    mdir = os.path.join(WORK, 'meta')
    slug = repo.replace('/', '__')
    r = http_json('https://repos.ecosyste.ms/api/v1/hosts/GitHub/repositories/' + repo.replace('/', '%2F'),
                  os.path.join(mdir, 'eco__' + slug + '.json'))
    o = http_json(f'https://repos.ecosyste.ms/api/v1/hosts/GitHub/owners/{owner}', os.path.join(mdir, 'eco_owner__' + owner + '.json'))
    if r.get('stargazers_count') is not None:
        meta = dict(stars=r['stargazers_count'], pushed_at=(r.get('pushed_at') or '')[:10] or None,
                    default_branch=r.get('default_branch'), description=r.get('description'), meta_source='repos.ecosyste.ms')
    else:
        u = http_json(f'https://ungh.cc/repos/{repo}', os.path.join(mdir, 'ungh__' + slug + '.json')).get('repo') or {}
        meta = dict(stars=u.get('stars'), pushed_at=(u.get('pushedAt') or '')[:10] or None,
                    default_branch=u.get('defaultBranch'), description=u.get('description'),
                    meta_source='ungh.cc' if u else 'unavailable')
    kind = o.get('kind')
    meta['owner_type'] = {'organization': 'Organization', 'user': 'User'}.get(kind, 'unknown')
    return meta


# ------------------------------------------------------------------ TMS
TMS_FILES = [
    ('crowdin', re.compile(r'(^|/)\.?crowdin\.(ya?ml|json)$', re.I)),
    ('lokalise', re.compile(r'(^|/)\.?lokalise[^/]*$', re.I)),
    ('tolgee', re.compile(r'(^|/)(\.tolgeerc[^/]*|tolgee\.config\.[^/]+)$', re.I)),
    ('transifex', re.compile(r'(^|/)\.tx/config$')),
    ('weblate', re.compile(r'(^|/)\.?weblate[^/]*$', re.I)),
    ('lingohub', re.compile(r'lingohub', re.I)),
    ('localazy', re.compile(r'(^|/)localazy\.(json|keys\.json)$', re.I)),
    ('locize', re.compile(r'(^|/)\.?locize[^/]*$', re.I)),
    ('phrase', re.compile(r'(^|/)\.phrase(app)?\.ya?ml$', re.I)),
    ('inlang', re.compile(r'(^|/)project\.inlang(/|$)', re.I)),
]
TMS_TEXT = [
    ('crowdin', re.compile(r'crowdin\.com|crowdin/github-action|crowdin\.yml|\bcrowdin\b', re.I)),
    ('weblate', re.compile(r'hosted\.weblate\.org|\bweblate\b', re.I)),
    ('lokalise', re.compile(r'lokalise\.com|\blokalise\b', re.I)),
    ('transifex', re.compile(r'transifex\.com|\btransifex\b', re.I)),
    ('tolgee', re.compile(r'\btolgee\b', re.I)),
    ('lingohub', re.compile(r'\blingohub\b', re.I)),
    ('lingo.dev', re.compile(r'lingo\.dev|lingodotdev', re.I)),
    ('localazy', re.compile(r'\blocalazy\b', re.I)),
    ('locize', re.compile(r'\blocize\b', re.I)),
    ('pontoon', re.compile(r'pontoon\.mozilla\.org', re.I)),
    ('phrase', re.compile(r'phrase\.com|phraseapp', re.I)),
    ('smartling', re.compile(r'\bsmartling\b', re.I)),
]
AUTOMATION_FILES = re.compile(r'(^|/)(\.i18nrc(\.[cm]?js|\.json)?|[^/]*auto[-_]?(gen[-_]?)?(i18n|translat)[^/]*|'
                              r'[^/]*i18n[-_]auto[^/]*|[^/]*(translate|translation)[-_]?(script|sync|gen)[^/]*)$', re.I)
DOC_FILES = re.compile(r'(^|/)(readme|contributing|translat\w*|i18n|locali[sz]\w*|l10n|agents|claude)[^/]*\.(md|mdx|rst|txt)$', re.I)
BOT = re.compile(r'bot\b|\[bot\]|crowdin|weblate|lokalise|tolgee|lingo\.?dev|lingodotdev|transifex|lingohub|'
                 r'github-actions|renovate|dependabot|localazy|locize|action@github', re.I)
BOT_TMS = [(n, re.compile(p, re.I)) for n, p in [
    ('crowdin', r'crowdin'), ('weblate', r'weblate'), ('lokalise', r'lokalise'), ('tolgee', r'tolgee'),
    ('lingo.dev', r'lingo\.?dev|lingodotdev'), ('transifex', r'transifex'), ('lingohub', r'lingohub'),
    ('localazy', r'localazy'), ('locize', r'locize')]]


def doc_paths(files):
    out = []
    for f in files:
        depth = f.count('/')
        if f.startswith('.github/workflows/') and f.endswith(('.yml', '.yaml')):
            out.append(f)
        elif DOC_FILES.search(f) and depth <= 3 and 'node_modules' not in f and 'CHANGELOG' not in f.upper():
            out.append(f)
    return out[:150]


def detect_tms(files, dest, docs, history):
    tms = collections.OrderedDict()
    dirs = collections.defaultdict(set)
    for f in files:
        dirs[os.path.dirname(f)].add(os.path.basename(f))
        for name, rx in TMS_FILES:
            if rx.search(f) and not EXCLUDE.search(f):
                tms.setdefault(name, []).append(f'config file {f}')
    for d, names in dirs.items():
        if 'i18n.json' in names and 'i18n.lock' in names:
            tms.setdefault('lingo.dev', []).append(f'config file {d + "/" if d else ""}i18n.json + i18n.lock')
    for p in docs:
        try:
            with open(os.path.join(dest, p), encoding='utf-8', errors='replace') as f:
                text = f.read(400000)
        except OSError:
            continue
        for name, rx in TMS_TEXT:
            m = rx.search(text)
            if m:
                ctx = text[max(0, m.start() - 60):m.end() + 60].replace('\n', ' ').strip()
                tms.setdefault(name, []).append(f'mentioned in {p}: "...{ctx}..."')
    for name, n in history.get('bot_locale_tms', {}).items():
        tms.setdefault(name, []).append(f'{n} locale commits by {name} in the last {history.get("window_days")} days')
    if not tms and history.get('bot_locale_commits', 0) >= 5 and \
            history['bot_locale_commits'] >= 0.5 * history.get('locale_commits', 0):
        tms['bot-synced'] = [f'{history["bot_locale_commits"]} of {history["locale_commits"]} recent locale commits '
                             f'were made by bots (TMS not identified)']
    automation = sorted({f for f in files if AUTOMATION_FILES.search(f) and not EXCLUDE.search(f)
                         and not re.search(r'(^|/)(src|app|client|server|migrations?)/|\.(md|mdx|sql|tsx|jsx|svg|png)$', f)})[:10]
    return {k: v[:4] for k, v in tms.items()}, automation


# ------------------------------------------------------------------ history
def noreply_login(email):
    m = re.match(r'^(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$', email, re.I)
    return m.group(1).lower() if m else None


def history_stats(dest, locale_files, source_files, window_days, mode):
    if mode == 'depth1':
        return {'available': False, 'window_days': window_days}
    shallow = set()
    sp = os.path.join(dest, '.git', 'shallow')
    if os.path.exists(sp):
        shallow = set(open(sp).read().split())
    out = git(['log', '--no-merges', '--no-renames', '--format=%x01%H%x1f%an%x1f%ae%x1f%aI%x1f%s', '--name-only', 'HEAD'],
              cwd=dest, timeout=3600)
    commits = []
    for chunk in out.split('\x01')[1:]:
        lines = chunk.strip('\n').split('\n')
        h, an, ae, date, subj = (lines[0].split('\x1f') + [''] * 5)[:5]
        if h in shallow:
            continue
        commits.append(dict(h=h, an=an, ae=ae.lower(), date=date[:10], subj=subj, files=[x for x in lines[1:] if x]))
    days = collections.defaultdict(set)

    def akey(c):
        return noreply_login(c['ae']) or c['ae'] or c['an'].lower()

    for c in commits:
        days[akey(c)].add(c['date'])
    stats = dict(available=True, window_days=window_days, commits=len(commits), authors=len(days),
                 outside_commits=0, outside_authors=0, locale_commits=0, outside_locale_commits=0,
                 outside_locale_authors=0, bot_locale_commits=0, last_outside_locale_commit=None,
                 last_outside_commit=None, bot_locale_tms={})
    outside_authors, outside_locale_authors = set(), set()
    for c in commits:
        who = akey(c)
        is_bot = bool(BOT.search(c['an']) or BOT.search(c['ae']))
        occasional = not is_bot and len(days[who]) <= 3
        touches_locale = any(f in locale_files for f in c['files'])
        if occasional:
            stats['outside_commits'] += 1
            outside_authors.add(who)
            stats['last_outside_commit'] = max(stats['last_outside_commit'] or '', c['date'])
        if touches_locale:
            stats['locale_commits'] += 1
            if is_bot or BOT.search(c['subj']):
                stats['bot_locale_commits'] += 1
                for name, rx in BOT_TMS:
                    if rx.search(c['an']) or rx.search(c['ae']) or rx.search(c['subj']):
                        stats['bot_locale_tms'][name] = stats['bot_locale_tms'].get(name, 0) + 1
            if occasional:
                stats['outside_locale_commits'] += 1
                outside_locale_authors.add(who)
                stats['last_outside_locale_commit'] = max(stats['last_outside_locale_commit'] or '', c['date'])
    stats['outside_authors'] = len(outside_authors)
    stats['outside_locale_authors'] = len(outside_locale_authors)
    # a few TMS bots only touch locale files in a handful of commits; require >=2 to count as evidence
    stats['bot_locale_tms'] = {k: v for k, v in stats['bot_locale_tms'].items() if v >= 2}
    return stats


# ------------------------------------------------------------------ audit
SUFX = {'.zero': '.other', '.one': '.other', '.two': '.other', '.few': '.other', '.many': '.other'}


def ref_key(k, src):
    r = seed.ref_key(k, src)
    if r is not None:
        return r
    for s, rep in SUFX.items():
        if k.endswith(s):
            base = k[:-len(s)]
            for cand in (base + rep, base + '.one'):
                if cand in src:
                    return cand
    m = re.match(r'^(.*)\[(\d+)\]$', k)  # PO plural index beyond the source's
    if m:
        for n in (1, 0):
            cand = f'{m.group(1)}[{n}]'
            if cand in src:
                return cand
    return None


COUNT_NAMES = {'count', 'n', 'num', 'number', 'smart_count', 'total', 'amount', 'qty', 'quantity', '0'}
PLURAL_KEY = re.compile(r'(_|\.)(zero|one|two|few|many|other)$|\[\d+\]$')


def tok_name(t):
    return re.sub(r'^[{%$_t(\s]+|[\s})_]+$', '', t).strip()


def refine(kind, miss, extra, key, src):
    """Adjust seed classifications for cases that are not user-visible bugs.
    Returns (kind, miss, extra) or None to drop the finding."""
    m = [x for x in miss.split(',') if x]
    e = [x for x in extra.split(',') if x]
    # i18next nesting: a translator may inline the text of a nested $t(key); that renders fine
    if any(x.startswith('$t(') for x in m + e):
        m = [x for x in m if not x.startswith('$t(')]
        e = [x for x in e if not x.startswith('$t(')]
        if not m and not e:
            return None
    # paired formatting markers such as {{b}}...{{/b}}: losing them drops bold text, not a value
    names = {tok_name(x) for x in m + e}
    marker = {n for n in names if n.startswith('/') or '/' + n in names}
    if marker and kind != 'broken':
        m = [x for x in m if tok_name(x) not in marker]
        e = [x for x in e if tok_name(x) not in marker]
        if not m and not e:
            return 'tag', miss, extra
    # plural forms: a singular or dual form may legitimately omit (or add) the count
    pk = PLURAL_KEY.search(key)
    countlike = all(tok_name(x).lower() in COUNT_NAMES or 'count' in tok_name(x).lower() or x in ('%s', '%d') for x in m + e)
    # adding the count to any plural form is harmless; omitting it is fine only where the
    # form itself implies the number (zero / one / two, or the first PO plural form)
    if pk and (m or e) and countlike and kind != 'broken' and \
            (not m or re.search(r'(zero|one|two)$|\[\d+\]$', key)):
        return 'plural_count', ','.join(m), ','.join(e)
    if kind in ('renamed', 'mixed') and (m or e) and not (m and e):
        kind = 'dropped' if m else 'extra'
    return kind, ','.join(m), ','.join(e)


def detect_mode(strings):
    c = collections.Counter()
    for s in strings:
        if '{{' in s:
            c['i18next'] += 1
        elif re.search(r'\{\s*[A-Za-z_][\w.]*\s*(,\s*(plural|select|selectordinal|number|date|time)\b[^}]*)?\}', s):
            c['icu'] += 1
        if seed.RAILS.search(s):
            c['rails'] += 1
        if seed.PRINTF.search(s) or seed.SPRINTF.search(s):
            c['rocket'] += 1
    if not c:
        return 'i18next', 0
    mode, n = c.most_common(1)[0]
    return mode, n


def find_line(text, key, ext):
    """Best-effort 1-based line number of a key inside a locale file."""
    if not text:
        return None
    if ext == '.po':
        msgid = key.split('\x04')[-1]
        msgid = re.sub(r'\[\d+\]$', '', msgid)
        first = json.dumps(msgid.split('\n')[0], ensure_ascii=False)[1:-1][:60]
        for needle in ('msgid "' + first, first):
            i = text.find(needle)
            if i >= 0:
                return text.count('\n', 0, i) + 1
        return None
    q = '"' if ext in ('.json', '.arb') else ''
    i = text.find(f'"{key}"') if q else -1
    if i >= 0:
        return text.count('\n', 0, i) + 1
    pos = 0
    for seg in key.split('.'):
        hit = -1
        for needle in ([f'"{seg}"'] if q else [f'{seg}:', f'"{seg}"', f"'{seg}'"]):
            j = text.find(needle, pos)
            if j >= 0 and (hit < 0 or j < hit):
                hit = j
        if hit < 0:
            return text.count('\n', 0, pos) + 1 if pos else None
        pos = hit + 1
    return text.count('\n', 0, pos) + 1


def audit_group(g, dest, js_cache, vue=False):
    """Return (per-locale stats, findings, mode)."""
    ext = g['ext']
    loaded = {}
    ids = {}
    errors = []
    ns_of = {}
    for loc, nsmap in g['files'].items():
        merged = {}
        for ns, path in nsmap.items():
            ns_of[path] = ns
            full = os.path.join(dest, path)
            try:
                if ext in ('.json', '.arb'):
                    d = load_json_file(full)
                elif ext in ('.yml', '.yaml'):
                    d = load_yaml_file(full, loc)
                elif ext == '.po':
                    d, idmap = load_po_file(full)
                    if loc == g['source_loc'] or g['source_loc'] == '__msgid__':
                        for k, v in idmap.items():
                            ids.setdefault((ns + ':' if ns else '') + k, v)
                else:
                    obj = js_cache.get(path)
                    if not isinstance(obj, dict) or '__error__' in obj:
                        raise ValueError((obj or {}).get('__error__', 'not loaded'))
                    d = seed.flat(obj)
            except Exception as e:  # noqa: BLE001
                errors.append(f'{path}: {str(e)[:120]}')
                continue
            for k, v in d.items():
                merged[(ns + ':' if ns else '') + k] = (v, path)
        loaded[loc] = merged
    src_loc = g['source_loc']
    src_full = loaded.get(src_loc, {})
    src = {k: v for k, (v, _) in src_full.items()}
    if ext == '.po':
        # PO: the English msgstr wins when present, otherwise the msgid is the source text
        for k, msgid in ids.items():
            if not src.get(k):
                src[k] = msgid
    key_is_source = False
    if src and sum(1 for v in src.values() if not v.strip()) > 0.5 * len(src):
        key_is_source = True  # e.g. open-webui: keys are the English strings, values empty
        src = {k: (v if v.strip() else k.split(':', 1)[-1]) for k, v in src.items()}
    mode, evidence = detect_mode([v for v in src.values() if isinstance(v, str)])
    if g.get('mode'):
        mode, evidence = g['mode'], 10 ** 6
    g['_evidence'] = (mode, evidence)
    if mode == 'icu' and vue:
        mode = 'vue'
    if mode == 'icu' and ext == '.po':
        # only Lingui catalogs follow ICU apostrophe quoting; other PO files (Python str.format) do not
        head = ''
        for nsmap in g['files'].values():
            for pth in nsmap.values():
                try:
                    head += open(os.path.join(dest, pth), encoding='utf-8', errors='replace').read(3000)
                except OSError:
                    pass
                break
            if head:
                break
        if 'lingui' not in head.lower():
            mode = 'pyformat'
    texts = {}
    stats, findings = {}, []
    for loc in sorted(loaded):
        if loc == src_loc or loc.lower() in PSEUDO or loc in ('__source__', '__msgid__'):
            continue
        n = collections.Counter()
        checked = 0
        for k, (v, path) in loaded[loc].items():
            if not isinstance(v, str) or not v.strip():
                continue
            r = ref_key(k, src)
            if r is None:
                if key_is_source:
                    r = k
                    src.setdefault(k, k.split(':', 1)[-1])
                else:
                    continue
            s = src[r]
            if not s or not s.strip():
                continue
            checked += 1
            if s == v:
                continue
            if 'crwdns' in v:  # Crowdin in-context pseudo-translation markers, not shown to users
                continue
            if mode in ('vue', 'pyformat'):  # {x} like ICU, but apostrophes are plain text
                cs, cv, cmode = s.replace("'", '\u2019'), v.replace("'", '\u2019'), 'icu'
            else:
                cs, cv, cmode = s, v, mode
            for kind, miss, extra in seed.classify(cs, cv, k, cmode):
                ref = refine(kind, miss, extra, k, src)
                if ref is None:
                    continue
                kind, miss, extra = ref
                n[kind] += 1
                if path not in texts:
                    try:
                        texts[path] = open(os.path.join(dest, path), encoding='utf-8-sig', errors='replace').read()
                    except OSError:
                        texts[path] = ''
                leaf = k.split(':', 1)[-1] if (ns_of.get(path) and k.startswith(ns_of[path] + ':')) else k
                line = find_line(texts[path], leaf, ext)
                findings.append(dict(locale=loc, file=path, line=line, key=leaf.replace('\x04', ' | '),
                                     kind=kind, missing=miss, extra=extra, source=s, translation=v))
        if checked:
            stats[loc] = dict(checked=checked, **{kk: n.get(kk, 0) for kk in KINDS})
    return stats, findings, mode, errors, len(src)


def pick_examples(findings, k=3):
    real = [f for f in findings if f['kind'] in REAL]
    pool = sorted(real, key=lambda f: (len(f['source']) > 140, len(f['source']) + len(f['translation'])))
    out, kinds, locs = [], set(), set()
    for f in pool:  # first pass: distinct kinds and locales
        if f['kind'] not in kinds and f['locale'] not in locs:
            out.append(f)
            kinds.add(f['kind'])
            locs.add(f['locale'])
        if len(out) == k:
            return out
    for f in pool:
        if f not in out and f['locale'] not in locs:
            out.append(f)
            locs.add(f['locale'])
        if len(out) == k:
            return out
    for f in pool:
        if f not in out:
            out.append(f)
        if len(out) == k:
            break
    return out


def audit_repo(t, since, window_days, refresh):
    repo = t['repo']
    slug = repo.replace('/', '__')
    dest = os.path.join(WORK, 'clones', slug)
    try:
        cmode = clone(repo, dest, since, refresh)
    except Exception as e:  # noqa: BLE001
        return {'repo': repo, 'skipped': f'clone failed: {str(e)[:150]}'}
    files = git(['ls-tree', '-r', '--name-only', '-z', 'HEAD'], cwd=dest).split('\0')
    files = [f for f in files if f]
    groups = explicit_groups(t['groups'], files) if t.get('groups') else discover(files, t.get('exclude', ()))
    docs = doc_paths(files)
    locale_paths = [p for g in groups for nsmap in g['files'].values() for p in nsmap.values()]
    js_dirs = [os.path.dirname(p) for g in groups if g['ext'] in ('.ts', '.js', '.mjs')
               for nsmap in g['files'].values() for p in nsmap.values()]
    sparse_checkout(dest, locale_paths + docs, js_dirs)
    sha = git(['rev-parse', 'HEAD'], cwd=dest).strip()
    commit_date = git(['log', '-1', '--format=%cI', 'HEAD'], cwd=dest).strip()[:10]
    source_files = {p for g in groups for p in g['files'].get(g['source_loc'], {}).values()}
    non_source_locale_files = set(locale_paths) - source_files
    hist = history_stats(dest, non_source_locale_files, source_files, window_days, cmode)
    tms, automation = detect_tms(files, dest, docs, hist)
    meta = repo_meta(repo)
    base = dict(repo=repo, url=f'https://github.com/{repo}', category=t.get('category'), note=t.get('note'), commit=sha,
                commit_date=commit_date, audited_at=datetime.date.today().isoformat(), tool_commit=TOOL_SHA,
                **meta, tms=sorted(tms), tms_evidence=tms, automation=automation, activity=hist)
    if not groups:
        return dict(base, skipped='no locale files with an English source and 2+ translations found')
    js = [p for g in groups if g['ext'] in ('.ts', '.js', '.mjs') for nsmap in g['files'].values() for p in nsmap.values()]
    js_cache = load_js_files(js, dest)
    vue = sum(1 for f in files if f.endswith('.vue')) >= 20  # vue-i18n apps
    per_loc = collections.defaultdict(lambda: collections.Counter())
    all_findings, gout, errors = [], [], []
    for g in groups:
        if g.get('mode') is None and t.get('mode'):
            g['mode'] = t['mode']
        st, fi, mode, errs, nsrc = audit_group(g, dest, js_cache, vue=vue)
        g['_result'] = (st, fi, mode, errs, nsrc)
    votes = collections.Counter()
    for g in groups:
        m, ev = g.get('_evidence', ('i18next', 0))
        votes[m] += ev
    repo_mode = votes.most_common(1)[0][0] if votes and votes.most_common(1)[0][1] else None
    for g in groups:
        m, ev = g.get('_evidence', ('i18next', 0))
        if repo_mode and ev < 5 and m != repo_mode and not t.get('mode'):
            # too few placeholders to tell the syntax: use the repo's majority syntax
            g['mode'] = repo_mode
            g['_result'] = audit_group(g, dest, js_cache, vue=vue)
        st, fi, mode, errs, nsrc = g.pop('_result')
        g.pop('_evidence', None)
        errors += errs
        if nsrc < 5:
            continue
        src_paths = sorted(g['files'].get(g['source_loc'], {}).values())
        gout.append(dict(pattern=g['pattern'], source=src_paths[0] if len(src_paths) == 1 else g['pattern'].replace('{loc}', g['source_loc']),
                         source_locale=g['source_loc'] if not g['source_loc'].startswith('__') else 'en', mode=mode,
                         source_strings=nsrc, locales=sorted(st), findings=sum(1 for f in fi if f['kind'] in REAL)))
        for loc, s in st.items():
            per_loc[loc].update(s)
        all_findings += fi
    if not gout or not per_loc:
        return dict(base, skipped='locale files found but none could be parsed' + (f' ({errors[0]})' if errors else ''))
    by_kind = collections.Counter(f['kind'] for f in all_findings)
    total = sum(by_kind[k] for k in REAL)
    checked = sum(s['checked'] for s in per_loc.values())
    worst = sorted(((loc, sum(s[k] for k in REAL), s['checked']) for loc, s in per_loc.items()),
                   key=lambda x: (-x[1], x[0]))
    all_findings.sort(key=lambda f: (f['kind'] not in REAL, f['locale'], f['file'], f['line'] or 0))
    return dict(base, locale_groups=gout, locales=sorted(per_loc), locales_count=len(per_loc),
                strings_checked=checked, findings_total=total,
                findings_by_kind={k: by_kind.get(k, 0) for k in KINDS},
                findings_per_1k=round(1000 * total / checked, 2) if checked else 0,
                locales_with_findings=sum(1 for w in worst if w[1]),
                worst_locales=[dict(locale=l, findings=n, checked=c) for l, n, c in worst[:5] if n],
                per_locale={loc: dict(s) for loc, s in sorted(per_loc.items())},
                examples=pick_examples(all_findings), parse_errors=errors[:20],
                findings=all_findings[:MAX_FINDINGS_STORED], findings_truncated=len(all_findings) > MAX_FINDINGS_STORED)


# ------------------------------------------------------------------ outputs
def write_outputs(results):
    os.makedirs(DATA, exist_ok=True)
    audited = [r for r in results if 'skipped' not in r]
    skipped = [r for r in results if 'skipped' in r]
    keep = {r['repo'].replace('/', '__') + '.json' for r in results}
    for old in os.listdir(DATA):
        if '__' in old and old.endswith('.json') and old not in keep:
            os.remove(os.path.join(DATA, old))
    for r in results:
        with open(os.path.join(DATA, r['repo'].replace('/', '__') + '.json'), 'w') as f:
            json.dump(r, f, indent=1, ensure_ascii=False, sort_keys=False)
            f.write('\n')
    audited.sort(key=lambda r: (-r['findings_total'], r['repo'].lower()))
    with open(os.path.join(DATA, 'summary.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['repo', 'category', 'stars', 'owner_type', 'last_push', 'commit', 'tms', 'automation', 'locales',
                    'strings_checked', 'findings', 'findings_per_1k'] + [f'kind_{k}' for k in KINDS] +
                   ['locales_with_findings', 'worst_locales'])
        for r in audited:
            w.writerow([r['repo'], r.get('category') or '', r.get('stars') or '', r.get('owner_type'), r.get('pushed_at') or r['commit_date'],
                        r['commit'][:12], ';'.join(r['tms']) or 'none', 'yes' if r['automation'] else 'no', r['locales_count'],
                        r['strings_checked'], r['findings_total'], r['findings_per_1k']] +
                       [r['findings_by_kind'][k] for k in KINDS] +
                       [r['locales_with_findings'], ';'.join(f"{x['locale']}={x['findings']}" for x in r['worst_locales'][:3])])
    with open(os.path.join(DATA, 'skipped.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['repo', 'reason'])
        for r in sorted(skipped, key=lambda r: r['repo'].lower()):
            w.writerow([r['repo'], r['skipped']])
    # PR targets: no TMS detected, at least one finding
    rows = []
    for r in audited:
        if r['tms'] or not r['findings_total']:
            continue
        a = r['activity']
        stars = r.get('stars') or 0
        outside = a.get('outside_commits', 0) if a.get('available') else 0
        score = r['findings_total'] * math.log10(stars + 10) * math.log2(2 + outside)
        if r['automation']:
            score *= 0.5  # locale files regenerated by a script: a manual fix may be overwritten
        notes = [r['note']] if r.get('note') else []
        if r['automation']:
            notes.append('locale files may be regenerated by ' + ', '.join(r['automation'][:2]))
        if not a.get('available'):
            notes.append('no commit history in window; activity unknown')
        if a.get('available') and a.get('outside_locale_commits'):
            notes.append(f"{a['outside_locale_commits']} locale commits by occasional contributors merged in {a['window_days']}d")
        rows.append([r['repo'], stars, r.get('owner_type'), r['findings_total'], r['locales_with_findings'],
                     ';'.join(f"{x['locale']}={x['findings']}" for x in r['worst_locales'][:3]),
                     outside if a.get('available') else '', a.get('outside_authors', '') if a.get('available') else '',
                     a.get('outside_locale_commits', '') if a.get('available') else '', a.get('last_outside_locale_commit') or '',
                     r.get('pushed_at') or r['commit_date'], round(score, 1), '; '.join(notes)])
    rows.sort(key=lambda x: -x[11])
    with open(os.path.join(DATA, 'pr-targets.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['rank', 'repo', 'stars', 'owner_type', 'findings_non_tms', 'locales_with_findings', 'worst_locales',
                    'outside_commits_180d', 'outside_authors_180d', 'outside_locale_commits_180d',
                    'last_outside_locale_commit', 'last_push', 'score', 'notes'])
        for i, row in enumerate(rows, 1):
            w.writerow([i] + row)
    return audited, skipped, rows


def check_local(path, max_print=200):
    """CI mode: audit a local checkout and exit non-zero if any user-visible finding exists."""
    path = os.path.abspath(path)
    p = subprocess.run(['git', 'ls-files', '-z'], cwd=path, capture_output=True, text=True)
    if p.returncode == 0 and p.stdout:
        files = [f for f in p.stdout.split('\0') if f]
    else:
        files = [os.path.relpath(os.path.join(d, f), path) for d, _, fs in os.walk(path) if '/.git' not in d for f in fs]
    groups = discover(files)
    vue = sum(1 for f in files if f.endswith('.vue')) >= 20
    js = [x for g in groups if g['ext'] in ('.ts', '.js', '.mjs') for nsmap in g['files'].values() for x in nsmap.values()]
    js_cache = load_js_files(js, path)
    total, shown = 0, 0
    for g in groups:
        _, findings, mode, errors, _ = audit_group(g, path, js_cache, vue=vue)
        for e in errors:
            print(f'warning: could not parse {e}', file=sys.stderr)
        for f in findings:
            if f['kind'] not in REAL:
                continue
            total += 1
            if shown < max_print:
                shown += 1
                where = f"{f['file']}:{f['line']}" if f.get('line') else f['file']
                print(f"{where}: {f['kind']} [{f['locale']}] {f['key']}: missing={f['missing'] or '-'} extra={f['extra'] or '-'}")
                print(f"    source:      {f['source'][:200]}")
                print(f"    translation: {f['translation'][:200]}")
    print(f'{len(groups)} locale groups checked, {total} findings')
    return 1 if total else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--only', nargs='*', help='owner/repo names to (re)run; others are read from data/')
    ap.add_argument('--jobs', type=int, default=6)
    ap.add_argument('--refresh', action='store_true', help='re-clone even if a cached clone exists')
    ap.add_argument('--window-days', type=int, default=180)
    ap.add_argument('--check', metavar='PATH', help='CI mode: audit a local checkout, exit 1 on findings')
    args = ap.parse_args()
    if args.check:
        sys.exit(check_local(args.check))
    with open(os.path.join(ROOT, 'targets.json')) as f:
        targets = json.load(f)['targets']
    os.makedirs(WORK, exist_ok=True)
    since = (datetime.date.today() - datetime.timedelta(days=args.window_days)).isoformat()
    todo = [t for t in targets if not args.only or t['repo'] in args.only]
    results = {}
    if args.only:  # keep previous results for the others
        for t in targets:
            p = os.path.join(DATA, t['repo'].replace('/', '__') + '.json')
            if t['repo'] not in args.only and os.path.exists(p):
                with open(p) as f:
                    results[t['repo']] = json.load(f)
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
        futs = {ex.submit(audit_repo, t, since, args.window_days, args.refresh): t for t in todo}
        for fu in concurrent.futures.as_completed(futs):
            t = futs[fu]
            try:
                r = fu.result()
            except Exception as e:  # noqa: BLE001
                r = {'repo': t['repo'], 'skipped': f'error: {str(e)[:200]}'}
            results[t['repo']] = r
            msg = r.get('skipped') or f"locales={r['locales_count']} findings={r['findings_total']} tms={','.join(r['tms']) or 'none'}"
            print(f"{t['repo']}: {msg}", flush=True)
    order = [t['repo'] for t in targets]
    audited, skipped, rows = write_outputs([results[r] for r in order if r in results])
    print(f'\naudited={len(audited)} skipped={len(skipped)} findings={sum(r["findings_total"] for r in audited)} pr_targets={len(rows)}')


if __name__ == '__main__':
    main()
