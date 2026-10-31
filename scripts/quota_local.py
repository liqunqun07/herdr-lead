#!/usr/bin/env python3
"""Herdr team quota snapshot (read-only, stdlib). Author: Claude.
Sources: menubar-panel helper scripts (cached data, no keychain write, no login-state change).
Levels: ok > low (<=LOW) > critical (<=CRITICAL) > exhausted (<=0). unknown = no percentage available."""
import json, os, subprocess, sys, time
sys.dont_write_bytecode = True
LOW = float(os.environ.get('HERDR_QUOTA_LOW', 5))
CRITICAL = float(os.environ.get('HERDR_QUOTA_CRITICAL', 5))
RES = os.path.expanduser('~/Applications/Codex账号切换_codex.app/Contents/Resources')

def level(remaining):
    if remaining is None: return 'unknown'
    if remaining <= 0: return 'exhausted'
    if remaining <= CRITICAL: return 'critical'
    if remaining <= LOW: return 'low'
    return 'ok'

def _run(script, *args):
    p = subprocess.run(['/usr/bin/python3', os.path.join(RES, script), *args], capture_output=True, text=True, timeout=40)
    d = json.loads(p.stdout)
    if not d.get('ok'): raise RuntimeError(d.get('error') or 'helper failed')
    return d

def _fmt_reset(ts):
    return time.strftime('%m-%d %H:%M', time.localtime(ts)) if ts else None

def claude():
    d = _run('claude_quota_claude.py')
    acc = next((a for a in d['accounts'] if a.get('current')), d['accounts'][0])
    vals = [w['remaining'] for w in (acc.get('short', {}), acc.get('week', {})) if w.get('remaining') is not None]
    r = min(vals) if vals else None
    return dict(remaining=r, level=level(r), detail=dict(short=acc.get('short'), week=acc.get('week')),
                resets_at=_fmt_reset((acc.get('short') or {}).get('reset_at')))

def agy():
    d = _run('agy_store_codex.py', 'status')
    cur = d.get('current')
    def fams(acc):
        out = {}
        for g in acc.get('groups', []):
            fam = 'gemini' if g['label'].startswith('Gemini') else 'claude_gpt'
            r = g.get('remaining')
            if r is None: continue
            if fam not in out or r < out[fam]['remaining']:
                out[fam] = dict(remaining=r, level=level(r), window=g['label'], resets_at=_fmt_reset(g.get('reset_at')))
        return out
    accounts = {a['email']: fams(a) for a in d.get('accounts', [])}
    cf = accounts.get(cur, {})
    best_cur = max(v['remaining'] for v in cf.values()) if cf else None
    others = []
    for email, f in accounts.items():
        if email == cur or not f: continue
        fam, v = max(f.items(), key=lambda kv: kv[1]['remaining'])
        others.append(dict(account=email, family=fam, remaining=v['remaining'], level=level(v['remaining'])))
    others.sort(key=lambda x: -x['remaining'])
    return dict(remaining=best_cur, level=level(best_cur), current_account=cur, families=cf, other_accounts=others[:3])

def codex():
    d = _run('account_store_codex.py', 'status')
    cur = d.get('current') or {}
    return dict(remaining=None, level='unknown', plan=cur.get('plan'), recovery=cur.get('recovery'),
                note='Codex 面板无剩余百分比；以屏幕额度报错行为准')

def snapshot():
    out = {}
    for name, fn in (('claude', claude), ('agy', agy), ('codex', codex)):
        try: out[name] = fn()
        except Exception as e: out[name] = dict(remaining=None, level='unknown', error=str(e))
    for n in ('claudeccc', 'claudezju'): out[n] = dict(remaining=None, level='unknown', note='无面板额度数据')
    return out

def advice(ai, snap=None):
    """Manual fallback hints for the leader. Scripts never send /model or switch accounts."""
    snap = snap or snapshot(); s = snap.get(ai, {}); tips = []
    if ai == 'agy':
        fams = s.get('families', {})
        ok = [f for f, v in fams.items() if v['remaining'] > LOW]
        if ok: tips.append('同账号仍有额度的模型族：' + '、'.join('%s(%.0f%%)' % (f, fams[f]['remaining']) for f in ok) + '；leader 在 Agy pane 手动 /model 切换（首屏 /model 菜单先 Esc）')
        oth = [o for o in s.get('other_accounts', []) if o['remaining'] > LOW]
        if oth: tips.append('其他账号：' + '、'.join('%s %s(%.0f%%)' % (o['account'], o['family'], o['remaining']) for o in oth) + '；经菜单栏面板切换，须先获用户同意')
    elif ai == 'codex': tips.append('Codex 可在 pane 内手动 /model 换档位；否则转 claudeccc 或 claude')
    else: tips.append('可手动 /model 换档位；或转同类其他 AI')
    others = [n for n, v in snap.items() if n != ai and v['level'] in ('ok', 'low')]
    if others: tips.append('其他有额度的 AI：' + '、'.join(others))
    return tips

if __name__ == '__main__':
    snap = snapshot()
    if '--advice' in sys.argv:
        ai = sys.argv[sys.argv.index('--advice') + 1]
        print(json.dumps(dict(ai=ai, quota=snap.get(ai), advice=advice(ai, snap)), ensure_ascii=False))
    else: print(json.dumps(dict(thresholds=dict(low=LOW, critical=CRITICAL), time=time.strftime('%FT%T'), ai=snap), ensure_ascii=False))
