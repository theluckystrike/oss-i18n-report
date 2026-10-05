#!/usr/bin/env python3
"""Build the static report site in docs/ from data/*.json (written by bench.py)."""
import csv
import datetime
import glob
import html
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
DOCS = os.path.join(ROOT, 'docs')
REPO_URL = 'https://github.com/theluckystrike/oss-i18n-report'
TOOL_URL = 'https://github.com/theluckystrike/i18n-audit'
REAL = ('syntax', 'renamed', 'dropped', 'extra', 'broken', 'mixed')
INFO = ('tag', 'plural_count')

KIND_TEXT = {
    'dropped': ('Placeholder dropped',
                'The translation leaves out a placeholder that the source string has. Users see the sentence without '
                'the value it was meant to show: a name, a count, a date or a link.'),
    'renamed': ('Placeholder renamed',
                'The translation has the same number of placeholders, but a name changed, often because the variable '
                'name itself was translated. The app passes the original name, so the slot renders empty or as raw text.'),
    'broken': ('Unbalanced braces',
               'The braces in the translation do not balance while the source does. Depending on the library this '
               'shows raw braces to users or makes the whole message fail to format.'),
    'extra': ('Extra placeholder',
              'The translation contains a placeholder that the source does not have, often left over from an older '
              'version of the source string. If the app no longer passes that value, users see raw text like '
              '{{name}} or an empty gap.'),
    'syntax': ('Wrong placeholder syntax',
               'The placeholder name is right, but it is written in another library\'s syntax, for example {name} '
               'where the app uses {{name}}. Users see the literal braces.'),
    'mixed': ('Mixed changes',
              'Some placeholders are missing and different ones were added, in unequal numbers. Usually a '
              'combination of the cases above in one string.'),
    'tag': ('Rich-text tag differences (informational)',
            'The set of markup tags such as <0>...</0> or <b>...</b> differs from the source. This is sometimes '
            'deliberate, so it is reported but not counted in the totals.'),
    'plural_count': ('Plural form and the count (informational)',
                     'A plural form omits the count, for example a singular "One item", or adds it where the source '
                     'form does not show it. Both are usually correct, so they are reported but not counted.'),
}

STAMP = ''


def esc(s):
    return html.escape('' if s is None else str(s), quote=True)


def clean_ok(s):
    """True if a string contains no em-dash and no emoji (the site avoids both in what it displays)."""
    return '—' not in s and not re.search('[\U0001F000-\U0001FAFF☀-➿]', s)


def plain(s):
    """Repo descriptions for display: no emoji, no em-dash."""
    s = re.sub('[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]', '', s or '')
    return re.sub(r'\s*\u2014\s*', ': ', s).strip()


def fmt(n):
    return f'{n:,}' if isinstance(n, int) else esc(n)


def page(title, body, depth=0, desc=''):
    pre = '../' * depth
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc or 'A reproducible benchmark of user-visible translation bugs in company-backed open-source apps.')}">
<link rel="stylesheet" href="{pre}style.css">
</head>
<body>
<header class="site"><div class="wrap"><a class="brand" href="{pre}index.html">State of i18n in open-source apps</a>
<nav><a href="{pre}index.html#method">Method</a><a href="{pre}index.html#kinds">Bug kinds</a><a href="{pre}index.html#repos">Repos</a><a href="{pre}index.html#ci">Fix in CI</a></nav></div></header>
<main class="wrap">
{body}
</main>
<footer class="wrap"><p>Built with <a href="{TOOL_URL}">i18n-audit</a>. Source, data and method: <a href="{REPO_URL}">{REPO_URL.replace('https://', '')}</a>. {STAMP}</p>
<p>Want this checked and fixed in your own app? <a href="https://theluckystrike.github.io/oss-maintenance/i18n-audit/">Fixed-price i18n audit</a>. To support the free tool: <a href="https://github.com/sponsors/theluckystrike">GitHub Sponsors</a>.</p></footer>
<script src="{pre}sortable.js" defer></script>
</body>
</html>
'''


def code_pair(src, tr):
    return (f'<div class="pair"><div><span class="lbl">Source</span><code>{esc(src)}</code></div>'
            f'<div><span class="lbl">Translation</span><code>{esc(tr)}</code></div></div>')


def bar_rows(items, max_v, unit=''):
    """items: (label_html, value, title). Single-series horizontal bars."""
    out = ['<div class="bars" role="list">']
    for label, v, tip in items:
        w = 0 if not max_v else max(0.6, 100 * v / max_v) if v else 0
        out.append(f'<div class="bar" role="listitem" title="{esc(tip)}"><span class="bl">{label}</span>'
                   f'<span class="bt"><span class="bf" style="width:{w:.1f}%"></span></span>'
                   f'<span class="bv">{esc(v)}{unit}</span></div>')
    out.append('</div>')
    return '\n'.join(out)


def load():
    res = []
    for p in sorted(glob.glob(os.path.join(DATA, '*__*.json'))):
        with open(p) as f:
            res.append(json.load(f))
    return res


def kind_examples(audited):
    """One real example per kind, short and from a non-English locale, spread across repos."""
    used_repos = set()
    out = {}
    for kind in REAL + INFO:
        cands = []
        for r in audited:
            for f in r.get('findings', []):
                has_ph = re.search(r'\{|%[\w{(]|__\w+__', f['source'])
                if f['kind'] == kind and clean_ok(f['source'] + f['translation']) and 8 <= len(f['source']) <= 90 \
                        and (has_ph or kind == 'tag') and 'VAR_' not in f['translation']:
                    cands.append((r['repo'] in used_repos, len(f['source']), r['repo'], f))
        cands.sort(key=lambda x: x[:3])
        if cands:
            out[kind] = cands[0][3]
            used_repos.add(cands[0][2])
    return out


def repo_page(r):
    slug = r['repo'].replace('/', '__')
    k = r['findings_by_kind']
    rows = ''.join(f'<tr><td>{esc(x["locale"])}</td><td class="num">{x["findings"]}</td><td class="num">{fmt(x["checked"])}</td></tr>'
                   for x in r['worst_locales'])
    groups = ''.join(f'<tr><td><code>{esc(g["pattern"])}</code></td><td>{esc(g["mode"])}</td><td class="num">{fmt(g["source_strings"])}</td>'
                     f'<td class="num">{len(g["locales"])}</td><td class="num">{g["findings"]}</td></tr>' for g in r['locale_groups'])
    ex = ''.join(f'<li><p><a href="{esc(r["url"])}/blob/{r["commit"]}/{esc(e["file"])}{"#L" + str(e["line"]) if e.get("line") else ""}">'
                 f'<code>{esc(e["file"])}{":" + str(e["line"]) if e.get("line") else ""}</code></a> '
                 f'<span class="tag-kind">{esc(e["kind"])}</span> locale <b>{esc(e["locale"])}</b>, key <code>{esc(e["key"])}</code></p>'
                 f'{code_pair(e["source"], e["translation"])}</li>' for e in r['examples'] if clean_ok(e['source'] + e['translation']))
    listed = [f for f in r['findings'] if f['kind'] in REAL and clean_ok(f['source'] + f['translation'])][:40]
    flist = ''.join(f'<tr><td>{esc(f["locale"])}</td><td><a href="{esc(r["url"])}/blob/{r["commit"]}/{esc(f["file"])}{"#L" + str(f["line"]) if f.get("line") else ""}">'
                    f'{esc(os.path.basename(f["file"]))}{":" + str(f["line"]) if f.get("line") else ""}</a></td><td>{esc(f["kind"])}</td>'
                    f'<td><code>{esc(f["missing"] or "")}</code></td><td><code>{esc(f["extra"] or "")}</code></td>'
                    f'<td class="txt">{esc(f["source"][:160])}</td><td class="txt">{esc(f["translation"][:160])}</td></tr>' for f in listed)
    tms = ', '.join(r['tms']) or 'none detected'
    ev = ''.join(f'<li><b>{esc(n)}</b>: {esc(v[0])}</li>' for n, v in r['tms_evidence'].items())
    a = r['activity']
    act = (f'{a["commits"]} commits on the default branch in the last {a["window_days"]} days; '
           f'{a["outside_commits"]} by {a["outside_authors"]} occasional contributors, '
           f'{a["outside_locale_commits"]} of which touched translation files.') if a.get('available') else 'Commit history not available.'
    body = f'''
<p class="crumb"><a href="../index.html#repos">All repos</a></p>
<h1>{esc(r["repo"])}</h1>
<p class="lede">{esc(plain(r.get("description")))}</p>
<div class="stats">
<div><b>{r["findings_total"]}</b><span>findings</span></div>
<div><b>{r["locales_count"]}</b><span>locales</span></div>
<div><b>{fmt(r["strings_checked"])}</b><span>strings checked</span></div>
<div><b>{r["findings_per_1k"]}</b><span>per 1,000 strings</span></div>
</div>
<table class="kv"><tbody>
<tr><th>Commit audited</th><td><a href="{esc(r["url"])}/tree/{r["commit"]}"><code>{r["commit"][:12]}</code></a> ({esc(r["commit_date"])})</td></tr>
<tr><th>Stars</th><td>{fmt(r.get("stars") or "n/a")}</td></tr>
<tr><th>Owner type</th><td>{esc(r.get("owner_type"))}</td></tr>
<tr><th>Translation management</th><td>{esc(tms)}{"<ul class='ev'>" + ev + "</ul>" if ev else ""}</td></tr>
{"<tr><th>Note</th><td>" + esc(r["note"]) + "</td></tr>" if r.get("note") else ""}
<tr><th>Recent activity</th><td>{esc(act)}</td></tr>
<tr><th>Findings by kind</th><td>{", ".join(f"{kk} {k[kk]}" for kk in REAL if k[kk]) or "none"}{"; informational: " + ", ".join(f"{kk} {k[kk]}" for kk in INFO if k[kk]) if any(k[kk] for kk in INFO) else ""}</td></tr>
</tbody></table>
<h2>Locale files</h2>
<div class="scroll"><table><thead><tr><th>Pattern</th><th>Syntax</th><th class="num">Source strings</th><th class="num">Locales</th><th class="num">Findings</th></tr></thead><tbody>{groups}</tbody></table></div>
{"<h2>Locales with the most findings</h2><div class='scroll'><table><thead><tr><th>Locale</th><th class='num'>Findings</th><th class='num'>Strings checked</th></tr></thead><tbody>" + rows + "</tbody></table></div>" if rows else ""}
{"<h2>Examples</h2><ul class='examples'>" + ex + "</ul>" if ex else ""}
{"<h2>Findings</h2><p class='note'>Up to 40 shown. The full list, with every key and line, is in <a href='" + REPO_URL + "/blob/main/data/" + slug + ".json'>data/" + slug + ".json</a>.</p><div class='scroll'><table class='sortable findings'><thead><tr><th>Locale</th><th>File</th><th>Kind</th><th>Missing</th><th>Extra</th><th>Source</th><th>Translation</th></tr></thead><tbody>" + flist + "</tbody></table></div>" if flist else ""}
'''
    with open(os.path.join(DOCS, 'r', slug + '.html'), 'w') as f:
        f.write(page(f'{r["repo"]}: i18n findings', body, depth=1, desc=f'Translation placeholder findings for {r["repo"]}.'))


def main():
    global STAMP
    results = load()
    audited = [r for r in results if 'skipped' not in r]
    skipped = sorted([r for r in results if 'skipped' in r], key=lambda r: r['repo'].lower())
    audited.sort(key=lambda r: (-r['findings_total'], r['repo'].lower()))
    dates = sorted({r['audited_at'] for r in audited})
    STAMP = f'Data collected {dates[-1] if dates else ""}.'
    os.makedirs(os.path.join(DOCS, 'r'), exist_ok=True)
    for old in glob.glob(os.path.join(DOCS, 'r', '*.html')):
        os.remove(old)
    total = sum(r['findings_total'] for r in audited)
    checked = sum(r['strings_checked'] for r in audited)
    locales = sum(r['locales_count'] for r in audited)
    with_f = sum(1 for r in audited if r['findings_total'])
    by_kind = {k: sum(r['findings_by_kind'][k] for r in audited) for k in REAL + INFO}
    tms_n = sum(1 for r in audited if r['tms'])
    tms_total = sum(r['findings_total'] for r in audited if r['tms'])
    no_tms_total = total - tms_total
    no_tms_checked = sum(r['strings_checked'] for r in audited if not r['tms'])
    tms_checked = checked - no_tms_checked
    rates = sorted(r['findings_per_1k'] for r in audited)
    median = rates[len(rates) // 2] if rates else 0
    zero = sum(1 for r in audited if not r['findings_total'])

    # distribution of findings per 1k strings
    buckets = [('0', lambda x: x == 0), ('0.01 to 0.5', lambda x: 0 < x <= 0.5), ('0.5 to 1', lambda x: 0.5 < x <= 1),
               ('1 to 2', lambda x: 1 < x <= 2), ('2 to 5', lambda x: 2 < x <= 5), ('more than 5', lambda x: x > 5)]
    dist = [(esc(lbl), sum(1 for r in audited if fn(r['findings_per_1k'])), f'{lbl} findings per 1,000 strings') for lbl, fn in buckets]
    dist_html = bar_rows(dist, max(v for _, v, _ in dist) or 1)
    kinds_html = bar_rows([(esc(KIND_TEXT[k][0]), by_kind[k], f'{by_kind[k]} findings') for k in REAL],
                          max(by_kind[k] for k in REAL) or 1)
    top = [r for r in audited if r['findings_total']][:15]
    top_html = bar_rows([(f'<a href="r/{r["repo"].replace("/", "__")}.html">{esc(r["repo"])}</a>', r['findings_total'],
                          f'{r["findings_total"]} findings in {r["locales_with_findings"]} locales') for r in top],
                        top[0]['findings_total'] if top else 1)

    ex = kind_examples(audited)
    kinds_sec = []
    for k in REAL + INFO:
        title, text = KIND_TEXT[k]
        e = ex.get(k)
        exh = ''
        if e:
            exh = (f'<p class="note">Example from the {esc(e["locale"])} translation, key <code>{esc(e["key"])}</code>:</p>'
                   f'{code_pair(e["source"], e["translation"])}')
        kinds_sec.append(f'<article class="kind"><h3>{esc(title)} <span class="count">{by_kind[k]}</span></h3><p>{esc(text)}</p>{exh}</article>')

    rows = []
    for r in audited:
        slug = r['repo'].replace('/', '__')
        worst = ', '.join(f'{esc(x["locale"])} ({x["findings"]})' for x in r['worst_locales'][:3])
        rows.append(f'<tr><td><a href="r/{slug}.html">{esc(r["repo"])}</a></td><td>{esc(r.get("category") or "")}</td>'
                    f'<td class="num" data-v="{r.get("stars") or 0}">{fmt(r.get("stars") or "")}</td>'
                    f'<td>{esc(", ".join(r["tms"]) or "none")}</td><td class="num">{r["locales_count"]}</td>'
                    f'<td class="num" data-v="{r["strings_checked"]}">{fmt(r["strings_checked"])}</td>'
                    f'<td class="num">{r["findings_total"]}</td><td class="num">{r["findings_per_1k"]}</td><td>{worst}</td></tr>')
    sk = ''.join(f'<li><a href="https://github.com/{esc(r["repo"])}">{esc(r["repo"])}</a>: {esc(r["skipped"])}</li>' for r in skipped)

    body = f'''
<h1>User-visible translation bugs in {len(audited)} company-backed open-source apps</h1>
<p class="lede">Translations are written by volunteers, agencies and machines, often for strings that change after they were translated. Small slips in placeholders are easy to make and hard to spot in review, and they show up in the product as raw <code>{{{{name}}}}</code> text or sentences with a missing number. This benchmark counts them, so maintainers can find and fix them, and shows how to keep them out with a CI check.</p>
<div class="stats">
<div><b>{len(audited)}</b><span>repos audited</span></div>
<div><b>{fmt(total)}</b><span>findings</span></div>
<div><b>{fmt(checked)}</b><span>translated strings checked</span></div>
<div><b>{locales}</b><span>locale file sets</span></div>
</div>
<p>{with_f} of {len(audited)} repos have at least one finding and {zero} have none. The median repo has {median} findings per 1,000 translated strings. Repos that use a translation management system (TMS) account for {fmt(tms_total)} findings in {fmt(tms_checked)} strings ({(1000 * tms_total / tms_checked if tms_checked else 0):.2f} per 1,000); repos without one account for {fmt(no_tms_total)} in {fmt(no_tms_checked)} strings ({(1000 * no_tms_total / no_tms_checked if no_tms_checked else 0):.2f} per 1,000). A TMS was detected in {tms_n} repos.</p>
<p class="note">A finding is a mismatch that is very likely visible to users. It is not a judgement of the project: every repo here ships dozens of languages, which is already more than most software does. Some findings sit on keys the code no longer uses; counts are an upper bound on what users see.</p>

<h2 id="distribution">Distribution</h2>
<div class="grid2">
<figure><figcaption>Repos by findings per 1,000 translated strings</figcaption>{dist_html}</figure>
<figure><figcaption>Findings by kind, all repos</figcaption>{kinds_html}</figure>
</div>
<figure><figcaption>Repos with the most findings</figcaption>{top_html}</figure>

<h2 id="kinds">What the findings look like</h2>
<p>Each translated string is compared with its English source string. The examples below are real strings from the audited repos, shown without the project name.</p>
<div class="kinds">{''.join(kinds_sec)}</div>

<h2 id="repos">All repos</h2>
<p class="note">Click a column header to sort. Findings exclude the two informational kinds. TMS shows the translation management system detected from config files, docs or commit authors.</p>
<div class="scroll"><table class="sortable" id="repo-table"><thead><tr><th>Repo</th><th>Category</th><th class="num">Stars</th><th>TMS</th><th class="num">Locales</th><th class="num">Strings</th><th class="num" aria-sort="descending">Findings</th><th class="num">Per 1k</th><th>Most findings in</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p>Downloads: <a href="{REPO_URL}/blob/main/data/summary.csv">summary.csv</a>, <a href="{REPO_URL}/blob/main/data/pr-targets.csv">pr-targets.csv</a> (repos without a TMS, where fixes can go straight to the locale files), <a href="{REPO_URL}/tree/main/data">per-repo JSON with every finding</a>.</p>
{"<h3>Not audited</h3><ul class='skipped'>" + sk + "</ul>" if sk else ""}

<h2 id="method">Method</h2>
<ol class="method">
<li><b>Targets.</b> {len(audited) + len(skipped)} company-backed apps, plugins and starters with translations in the repository (<a href="{REPO_URL}/blob/main/targets.json">targets.json</a>). Repos without locale files are listed above as not audited.</li>
<li><b>Checkout.</b> A shallow, blob-less clone of the default branch, then a sparse checkout of only the locale files and a few docs and config files. The audited commit is recorded per repo.</li>
<li><b>Locale discovery.</b> Files whose paths differ only by a locale code (<code>locales/de/common.json</code>, <code>translations/de.json</code>, <code>de.po</code>, <code>client.de.yml</code>) form a group. A group needs an English source and at least two translations. Tests, fixtures, docs and examples are ignored. JSON, ARB, YAML, gettext PO, and TS/JS modules (bundled with esbuild) are read.</li>
<li><b>Comparison.</b> Every translated string whose key exists in the English source is compared with it by the classifier from <a href="{TOOL_URL}">i18n-audit</a> (pinned copy in <code>tools/</code>). The placeholder syntax (i18next, ICU, Rails, printf) is detected per group. Untranslated strings and strings identical to the source are not findings. Fuzzy PO entries are skipped, and two harmless cases are not counted: an i18next nested <code>$t(key)</code> replaced by its text, and <code>{{{{count}}}}</code> added to a plural form.</li>
<li><b>TMS detection.</b> Config files (Crowdin, Lokalise, Tolgee, Lingo.dev, Transifex, Weblate, LingoHub and others), mentions in README, CONTRIBUTING and workflow files, and bot authors of recent commits to locale files.</li>
<li><b>Metadata.</b> Stars and owner type come from public mirrors of the GitHub API (repos.ecosyste.ms, ungh.cc). Contribution activity comes from git history: an occasional contributor is a non-bot author active on at most 3 days in the last 180 days of the default branch.</li>
</ol>
<p>Reproduce with <code>python3 bench.py &amp;&amp; python3 build_site.py</code>. Limits: line numbers are best effort; a string can be correct for a library feature the classifier does not model; keys unused by the code are still counted.</p>

<h2 id="ci">How to fix this in CI</h2>
<p>All of these bugs are mechanical, so a check can catch them before merge, whatever produced the translation.</p>
<ol class="method">
<li>Run <a href="{TOOL_URL}">i18n-audit</a> on pull requests that touch locale files. It compares each translation with its source string and fails on dropped, renamed, extra or malformed placeholders and on lost rich-text tags.</li>
<li>If translations come from a TMS, turn on its placeholder QA check too (Crowdin, Lokalise, Weblate and Transifex all have one) so that errors are caught where translators work, and keep the CI check as a backstop for synced files.</li>
<li>For machine translation scripts, protect placeholders before translation (replace them with tokens and restore them after) and validate the output with the same check.</li>
<li>Fix the existing findings once: the per-repo JSON files list each one with file, line, key, source and translation.</li>
</ol>
<pre class="snippet"><code># .github/workflows/i18n.yml
name: i18n
on:
  pull_request:
    paths: ["**/locales/**", "**/translations/**", "**/i18n/**"]
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {{ python-version: "3.12" }}
      # see the i18n-audit README for the current install and CLI options
      - run: pip install git+https://github.com/theluckystrike/i18n-audit
      - run: i18n-audit .</code></pre>
'''
    with open(os.path.join(DOCS, 'index.html'), 'w') as f:
        f.write(page('State of i18n in open-source apps', body))
    for r in audited:
        repo_page(r)
    open(os.path.join(DOCS, '.nojekyll'), 'w').close()
    print(f'site: {len(audited)} repos, {total} findings, {len(skipped)} skipped')


if __name__ == '__main__':
    main()
