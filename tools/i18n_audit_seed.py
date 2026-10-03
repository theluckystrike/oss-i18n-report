#!/usr/bin/env python3
"""Generalized locale placeholder audit (from medusa-proof/audit.py).

Compares every translated string with its source string and reports
placeholder / tag / syntax mismatches. Supported syntaxes:
  i18next   {{x}} {{x, fmt}} $t(key) <0>..</0> <b>..</b>
  icu       {x} {x, plural|select|number ...} (react-intl, Lingui, Tolgee, vue-i18n) <b>..</b>
  rails     %{x}
  printf    %s %d %1$s
  sprintf   __x__ (Rocket.Chat)
Kinds:
  syntax    placeholder present but written in the wrong syntax ({x} where {{x}} is needed)
  renamed   placeholder name translated / misspelled (same count, different names)
  dropped   source placeholder missing from translation
  extra     translation has a placeholder the source does not (renders raw or wrong value)
  tag       Trans / rich-text tag set differs
  broken    unbalanced braces in translation (source balanced)
  plural_count  i18next plural form (_one etc) omits {{count}}  (usually legit, reported only)
Usage: i18n_audit.py <repo-name> [out.tsv]
"""
import json, re, os, sys, glob, collections

HOME = os.path.expanduser('~/oss-work/i18n')
SUF = ('_zero', '_one', '_two', '_few', '_many', '_other')


def flat(o, p=''):
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            kk = f'{p}.{k}' if p else k
            if isinstance(v, (dict, list)):
                out.update(flat(v, kk))
            elif isinstance(v, str):
                out[kk] = v
    elif isinstance(o, list):
        for i, v in enumerate(o):
            kk = f'{p}.{i}'
            if isinstance(v, (dict, list)):
                out.update(flat(v, kk))
            elif isinstance(v, str):
                out[kk] = v
    return out


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return flat(json.load(f))


def load_po(path):
    """returns {msgid: msgstr} (Lingui style po)"""
    out = {}
    cur = None
    field = None
    entry = {'msgid': '', 'msgstr': ''}

    def unq(s):
        return json.loads(s, strict=False) if s.startswith('"') else s

    def push():
        if entry['msgid'] and entry['msgstr']:
            out[entry['msgid']] = entry['msgstr']

    for line in open(path, encoding='utf-8'):
        line = line.rstrip('\n')
        if line.startswith('msgid '):
            push()
            entry = {'msgid': unq(line[6:]), 'msgstr': ''}
            field = 'msgid'
        elif line.startswith('msgstr '):
            entry['msgstr'] = unq(line[7:])
            field = 'msgstr'
        elif line.startswith('"') and field:
            entry[field] += unq(line)
        else:
            field = None if not line.startswith('"') else field
    push()
    return out


# ---------------------------------------------------------------- tokenizers
I18N = re.compile(r'\{\{\s*-?\s*([^},\s]+)[^}]*\}\}')
NEST = re.compile(r'\$t\(([^)]+)\)')
TAG = re.compile(r'<(/?)([A-Za-z0-9][A-Za-z0-9_-]*)(\s[^<>]*)?(/?)>')
SINGLE = re.compile(r'(?<![{%$])\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}(?!\})')
RAILS = re.compile(r'%\{([A-Za-z_][A-Za-z0-9_]*)\}')
PRINTF = re.compile(r'%(\d+\$)?[sdif@]')
DOLLAR = re.compile(r'\$\{(?!\{)([^}]+)\}')
SPRINTF = re.compile(r'__([A-Za-z][A-Za-z0-9_]*)__')


def icu_args(s, required=False):
    """Return (set of argument names, ok) with a small ICU MessageFormat parser."""
    args = []
    req = []
    ctx = []  # enclosing plural option keys
    i, n = 0, len(s)

    def parse_msg(i, depth):
        # text until matching '}' (depth>0) or end
        while i < n:
            c = s[i]
            if c == "'" and i + 1 < n and s[i + 1] in '{}':  # quoted literal
                j = s.find("'", i + 1)
                i = n if j < 0 else j + 1
                continue
            if c == '{':
                i = parse_arg(i + 1)
                if i < 0:
                    return -1
                continue
            if c == '}':
                return i if depth > 0 else -1
            i += 1
        return n if depth == 0 else -1

    def parse_arg(i):
        m = re.match(r'\s*([^\s,{}]+)\s*', s[i:])
        if not m:
            return -1
        args.append(m.group(1))
        if all(c == 'other' for c in ctx):
            req.append(m.group(1))
        i += m.end()
        if i < n and s[i] == '}':
            return i + 1
        if i < n and s[i] == ',':
            m2 = re.match(r'\s*([A-Za-z]+)\s*', s[i + 1:])
            if not m2:
                return -1
            typ = m2.group(1)
            i = i + 1 + m2.end()
            if typ in ('plural', 'select', 'selectordinal'):
                if i < n and s[i] == ',':
                    i += 1
                # options: key {msg} ...
                while i < n:
                    m3 = re.match(r'\s*(offset:\d+\s*)?([^\s{}]+)\s*\{', s[i:])
                    if not m3:
                        m4 = re.match(r'\s*\}', s[i:])
                        return i + m4.end() if m4 else -1
                    i += m3.end()
                    ctx.append(m3.group(2) if typ != 'select' else 'other')
                    i = parse_msg(i, 1)
                    ctx.pop()
                    if i < 0 or i >= n:
                        return -1
                    i += 1  # closing } of option
                return -1
            # simple formatted arg {x, number, ::style}
            depth = 1
            while i < n and depth:
                if s[i] == '{':
                    depth += 1
                elif s[i] == '}':
                    depth -= 1
                i += 1
            return i if depth == 0 else -1
        return -1

    r = parse_msg(0, 0)
    if required:
        return set(req), r >= 0
    return collections.Counter(args), r >= 0


def tags(s):
    return collections.Counter(('<%s%s>' % (m.group(2), '/' if m.group(4) else '')) for m in TAG.finditer(s) if not m.group(1))


def tokens(s, mode):
    """placeholder multiset (as set, order free) and auxiliary info"""
    t = collections.Counter()
    if mode in ('i18next', 'rocket'):
        t.update('{{%s}}' % x for x in I18N.findall(s))
        t.update('$t(%s)' % x for x in NEST.findall(s))
    if mode == 'icu':
        a, ok = icu_args(s)
        t.update('{%s}' % x for x in a)
    if mode == 'rocket':
        t.update('__%s__' % x for x in SPRINTF.findall(s))
        t.update('%s' for _ in PRINTF.findall(s))
    if mode == 'rails':
        t.update('%%{%s}' % x for x in RAILS.findall(s))
    t.update('${%s}' % x.strip("'") for x in DOLLAR.findall(s))  # template-literal / FreeMarker vars
    return t


def balanced(s, mode):
    if mode == 'icu':
        return icu_args(s)[1]
    return s.count('{{') == s.count('}}')


def classify(src, tr, key, mode, tags_on=True):
    a, b = set(tokens(src, mode)), set(tokens(tr, mode))
    ta, tb = (set(tags(src)), set(tags(tr))) if tags_on else (set(), set())
    out = []
    if not balanced(tr, mode) and balanced(src, mode):
        out.append(('broken', '', ''))
        return out
    if mode == 'icu':
        req = {'{%s}' % x for x in icu_args(src, True)[0]}
        missing, extra = sorted((a & req) - b), sorted(b - a)
    else:
        missing, extra = sorted(a - b), sorted(b - a)
    if missing or extra:
        name = lambda x: re.sub(r'^[{%$_t(]+|[})_]+$', '', x)
        # wrong syntax: the name is present in another syntax
        if mode in ('i18next', 'rocket'):
            alt = set(SINGLE.findall(tr)) | set(RAILS.findall(tr))
        elif mode == 'icu':
            alt = set(I18N.findall(tr)) | set(RAILS.findall(tr))
        else:
            alt = set(I18N.findall(tr)) | set(SINGLE.findall(tr))
        syn = [m for m in missing if name(m) in alt]
        if syn:
            out.append(('syntax', ','.join(syn), ','.join(sorted(extra))))
            missing = [m for m in missing if m not in syn]
            extra = []
        if missing or extra:
            plural = key.endswith(SUF)
            if plural and mode in ('i18next', 'rocket') and missing == ['{{count}}'] and not extra:
                out.append(('plural_count', ','.join(missing), ''))
            elif missing and extra and len(missing) == len(extra):
                out.append(('renamed', ','.join(missing), ','.join(extra)))
            elif missing and not extra:
                out.append(('dropped', ','.join(missing), ''))
            elif extra and not missing:
                out.append(('extra', '', ','.join(extra)))
            else:
                out.append(('mixed', ','.join(missing), ','.join(extra)))
    if ta != tb:
        out.append(('tag', ','.join(sorted(ta - tb)), ','.join(sorted(tb - ta))))
    return out


# ---------------------------------------------------------------- repo configs
def by_dir(pattern, src):
    """pattern with {loc} and optional * for namespace files"""
    def load():
        res = collections.defaultdict(dict)
        for path in glob.glob(os.path.join(HOME, pattern.replace('{loc}', '*'))):
            rel = os.path.relpath(path, HOME)
            rx = '^' + re.escape(pattern).replace(re.escape('{loc}'), '(?P<loc>[^/]+)').replace(r'\*', '(?P<ns>[^/]+)') + '$'
            m = re.match(rx, rel)
            if not m:
                continue
            loc = m.group('loc')
            ns = m.groupdict().get('ns')
            try:
                d = load_po(path) if path.endswith('.po') else load_json(path)
            except Exception as e:
                print('PARSE-ERROR', rel, e, file=sys.stderr)
                continue
            for k, v in d.items():
                res[loc][f'{ns}:{k}' if ns else k] = v
        return res
    return load


def multi(*loaders):
    def load():
        res = collections.defaultdict(dict)
        for prefix, ld in loaders:
            for loc, d in ld().items():
                for k, v in d.items():
                    res[loc][f'{prefix}|{k}'] = v
        return res
    return load


REPOS = {
    'cal.diy': dict(load=by_dir('cal.diy/packages/i18n/locales/{loc}/common.json', 'en'), src='en', mode='i18next'),
    'umami': dict(load=by_dir('umami/public/intl/messages/{loc}.json', 'en-US'), src='en-US', mode='icu'),
    'typebot.io': dict(load=by_dir('typebot.io/apps/builder/src/i18n/{loc}.json', 'en'), src='en', mode='icu'),
    'jan': dict(load=by_dir('jan/web-app/src/locales/{loc}/*.json', 'en'), src='en', mode='i18next'),
    'openaev': dict(load=by_dir('openaev/openaev-front/src/utils/lang/{loc}.json', 'en'), src='en', mode='icu', key_is_source=True),
    'formbricks-web': dict(load=by_dir('formbricks/apps/web/locales/{loc}.json', 'en-US'), src='en-US', mode='icu'),
    'formbricks-surveys': dict(load=by_dir('formbricks/packages/surveys/locales/{loc}.json', 'en-US'), src='en-US', mode='icu'),
    'twenty': dict(load=multi(
        ('front', by_dir('twenty/packages/twenty-front/src/locales/{loc}.po', None)),
        ('emails', by_dir('twenty/packages/twenty-emails/src/locales/{loc}.po', None)),
        ('server', by_dir('twenty/packages/twenty-server/src/engine/core-modules/i18n/locales/{loc}.po', None))),
        src='en', mode='icu', key_is_source=True),
    'ToolJet': dict(load=by_dir('ToolJet/frontend/assets/translations/{loc}.json', 'en'), src='en', mode='i18next'),
    'dify': dict(load=by_dir('dify/web/i18n/locales/{loc}/*.json', 'en-US'), src='en-US', mode='i18next'),
    'infisical': dict(load=by_dir('infisical/frontend/public/locales/{loc}/*.json', 'en'), src='en', mode='i18next'),
    'Rocket.Chat': dict(load=multi(
        ('core', by_dir('Rocket.Chat/packages/i18n/src/locales/{loc}.i18n.json', 'en')),
        ('livechat', by_dir('Rocket.Chat/packages/livechat/src/i18n/{loc}.json', 'en'))), src='en', mode='rocket'),
    'strapi': dict(load=None, src='en', mode='icu'),
    'OpenMetadata': dict(load=multi(
        ('ui', by_dir('OpenMetadata/openmetadata-ui/src/main/resources/ui/src/locale/languages/{loc}.json', 'en-us')),
        ('core', by_dir('OpenMetadata/openmetadata-ui-core-components/src/main/resources/ui/src/locale/languages/{loc}.json', 'en-us'))),
        src='en-us', mode='i18next'),
}


def strapi_load():
    res = collections.defaultdict(dict)
    for path in glob.glob(os.path.join(HOME, 'strapi/packages/**/translations/*.json'), recursive=True):
        rel = os.path.relpath(path, HOME)
        if '/translations/' not in rel or rel.count('/translations/') != 1:
            continue
        d, fn = os.path.split(rel)
        loc = fn[:-5]
        try:
            data = load_json(path)
        except Exception as e:
            print('PARSE-ERROR', rel, e, file=sys.stderr)
            continue
        ns = d.split('/translations')[0].replace('strapi/packages/', '')
        for k, v in data.items():
            res[loc][f'{ns}|{k}'] = v
    return res


REPOS['strapi']['load'] = strapi_load


def ref_key(k, src):
    if k in src:
        return k
    for s in SUF:
        if k.endswith(s):
            base = k[:-len(s)]
            for cand in (base + '_other', base + '_one', base):
                if cand in src:
                    return cand
    return None


def run(repo, out=None):
    cfg = REPOS[repo]
    data = cfg['load']()
    mode = cfg['mode']
    rows = []
    summary = {}
    if cfg.get('key_is_source'):
        # keys are the source strings (Lingui po msgid, openaev en-string keys)
        if repo == 'twenty':
            src = {}
        else:
            src = data.get(cfg['src'], {})
    else:
        src = data[cfg['src']]
    for loc in sorted(data):
        if loc == cfg['src'] or loc in ('$schema', 'languages', 'pseudo-en'):
            continue
        n = collections.Counter()
        for k, v in data[loc].items():
            if not isinstance(v, str) or not v.strip():
                continue
            if cfg.get('key_is_source'):
                s = k.split('|', 1)[1] if '|' in k else k
                if repo == 'openaev':
                    s = src.get(k, k)
                r = k
            else:
                r = ref_key(k, src)
                if r is None:
                    continue
                s = src[r]
            if s == v:
                continue
            for kind, miss, extra in classify(s, v, k, mode):
                n[kind] += 1
                rows.append((loc, k, kind, miss, extra, s.replace('\t', ' ').replace('\n', '\\n'), v.replace('\t', ' ').replace('\n', '\\n')))
        summary[loc] = n
    out = out or os.path.expanduser(f'~/oss-pipeline/v6-claude/i18n-hunt/audit-{repo}.tsv')
    with open(out, 'w') as f:
        f.write('locale\tkey\tkind\tmissing\textra\tsource\ttranslation\n')
        for r in rows:
            f.write('\t'.join(r) + '\n')
    tot = collections.Counter()
    for loc, n in summary.items():
        tot.update(n)
    real = sum(v for k, v in tot.items() if k not in ('plural_count', 'tag'))
    print(f'{repo}: locales={len(summary)} real={real} {dict(tot)}')
    worst = sorted(summary.items(), key=lambda x: -sum(v for k, v in x[1].items() if k not in ('plural_count', 'tag')))[:8]
    print('   top:', ', '.join(f"{l}={sum(v for k, v in n.items() if k not in ('plural_count', 'tag'))}" for l, n in worst))
    return summary


if __name__ == '__main__':
    names = sys.argv[1:] or list(REPOS)
    for nm in names:
        run(nm)
