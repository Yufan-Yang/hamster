"""Live clipping regression checks. smoke.py runs these on its disposable database."""
import json
import subprocess
import tempfile
import time
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from shiguang import board, core, live, live_bilibili, live_calibration, live_recorder, live_events
from shiguang.live_curator import evaluate_highlight


def curator_gates():
    config = live.settings()
    candidate = {"start": 10, "end": 40, "titles": ["真实标题"], "confidence": .9,
                 "scores": {"topic": 85, "traffic": 84, "fun": 82, "completeness": 90}}
    assert evaluate_highlight(candidate, config, 0, 60, 0)
    assert evaluate_highlight({**candidate, "start": -1}, config, 0, 60, 0) is None
    assert evaluate_highlight({**candidate, "end": 80}, config, 0, 60, 0) is None
    assert evaluate_highlight({**candidate, "confidence": .4}, config, 0, 60, 0) is None
    assert evaluate_highlight({**candidate, "scores": {"topic": 100, "traffic": 100, "fun": 100, "completeness": 20}}, config, 0, 60, 0) is None
    assert evaluate_highlight({**candidate, "scores": {"topic": float("nan")}}, config, 0, 60, 0) is None
    for bad in ({"max_seconds": 0}, {"auto_highlights": "true"}, {"min_seconds": 100, "max_seconds": 50}):
        try:
            live.settings(bad)
        except ValueError:
            continue
        raise AssertionError(f"invalid settings accepted: {bad}")
    assert live_bilibili.room_reference("https://space.bilibili.com/123?x=1") == ("uid", 123)
    for url in ("http://127.0.0.1/123", "https://live.bilibili.com.evil.test/123", "https://evil.test@live.bilibili.com/123"):
        try:
            live_bilibili.room_reference(url)
        except ValueError:
            continue
        raise AssertionError("unsafe live URL accepted")
    # Exercise the actual credential write; mocking it hid a Path.open(opener=...) failure after every scan.
    with tempfile.TemporaryDirectory(prefix="live-login-") as folder, patch.object(live_bilibili, "ACCOUNTS", Path(folder)/"accounts"):
        info = {"cookie_info": {"cookies": [{"name": n, "value": "test"} for n in ("SESSDATA", "bili_jct", "DedeUserID")]},
                "token_info": {"mid": 123, "access_token": "test-access", "refresh_token": "test-refresh"}}
        assert live_bilibili.save_account("user:test", info) == "123"
        path = live_bilibili.account_path("user:test")
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["token_info"] == info["token_info"]
        assert live_bilibili.client("user:test").cookies.get("SESSDATA") == "test"
        assert list(path.parent.iterdir()) == [path]


def sampling():
    now = time.time()
    items = [{"bvid": f"test{i}", "views": 10000-i*700, "published": now-86400*(1+i%6), "duration": 120,
              "mid": i//2, "author": str(i//2)} for i in range(12)]
    selected = live_calibration.select_samples(items, now)
    assert len(selected) == 8 and len({x["bvid"] for x, _ in selected}) == 8
    assert sum(cohort == "high" for _, cohort in selected) == 4
    assert all(now-x["published"] <= 30*86400 for x, _ in selected)
    fresh = {**items[0], "bvid": "just-uploaded", "views": 100000, "published": now-3600}
    assert all(x["bvid"] != "just-uploaded" for x, _ in live_calibration.select_samples([fresh, *items], now))
    try:
        live_calibration.select_samples([{**x, "published": now-120*86400} for x in items], now)
    except ValueError:
        pass
    else:
        raise AssertionError("old samples treated as current taste")
    restaurant = [{**x, "mid": 37938807, "author": "勇哥餐饮避坑指南", "duration": 23*60, "published": now-35*86400}
                  for x in items]
    chosen = live_calibration.select_samples(restaurant, now, per_cohort=6)
    assert len(chosen) == 12, "long discussions from one clipping account should support an initial rubric"
    mixed = live_calibration.select_samples(items+restaurant, now)
    assert all(now-x["published"] <= 30*86400 for x, _ in mixed), "prefer current examples when enough are available"


def short_live_links():
    assert live_bilibili.room_reference("分享峰哥的直播 https://b23.tv/test123 。") == ("short", "https://b23.tv/test123")
    assert live_bilibili.room_reference("b23.tv/test123") == ("short", "https://b23.tv/test123")
    assert live_bilibili.room_reference("https://live.bilibili.com/blanc/22230707?x=1") == ("room", 22230707)
    assert live_bilibili.room_reference("https://live.bilibili.com/h5/22230707") == ("room", 22230707)
    short = Mock()
    short.__enter__ = Mock(return_value=short); short.__exit__ = Mock(return_value=False)
    short.get.return_value = Mock(status_code=302, headers={"Location": "https://space.bilibili.com/35847683"})
    with patch.object(live_bilibili.requests, "Session", return_value=short):
        assert live_bilibili.resolve_reference("https://b23.tv/test123") == ("uid", 35847683)
        assert short.get.call_args.kwargs["allow_redirects"] is False
        short.get.return_value = Mock(status_code=302, headers={"Location": "http://127.0.0.1/private"})
        short.get.reset_mock()
        try:
            live_bilibili.resolve_reference("https://b23.tv/test123")
        except ValueError:
            pass
        else:
            raise AssertionError("short link followed an untrusted redirect")
        assert short.get.call_count == 1
    with patch.object(live_bilibili, "api", side_effect=[
            {"owner": {"mid": 35847683}}, {"roomid": 22230707},
            {"room_id": 22230707, "uid": 35847683, "live_status": 0}, {"info": {"uname": "峰哥亡命天涯"}}]):
        info = live_bilibili.room_info("https://www.bilibili.com/video/BV1chnM62EdW", lookup_name=True)
        assert info["name"] == "峰哥亡命天涯" and info["room_id"] == 22230707


def validation_independence():
    speech = "".join(chr(0x4e00+i) for i in range(500))
    other = "".join(chr(0x5200+i) for i in range(500))
    rows = [{"id": i, "segments": json.dumps([[0, 60, text]]), "published": time.time()-3*86400, "views": 10000-i}
            for i, text in enumerate((speech, speech[100:400], other))]
    assert [r["id"] for r in live_calibration.independent_samples(rows)] == [0, 2], "overlapping reposts must stay out of validation"
    pairs = [{"id": i, "cohort": "high" if i < 3 else "control"} for i in range(6)]
    scores = {i: {"score": 100-i} for i in range(6)}
    assert live_calibration.rank_accuracy(pairs, scores) == 1
    assert live_calibration.rank_pvalue(pairs, scores) == .05
    assert live_calibration.rank_pvalue(pairs, {i: {"score": 80} for i in range(6)}) == 1
    tiny = [{"id": i, "cohort": "high" if i < 2 else "control"} for i in range(4)]
    assert live_calibration.rank_pvalue(tiny, scores) > .1, "four held-out videos cannot certify audience fit"
    with patch.object(live_calibration, "q", return_value={"rubric": '{}', "evidence": '{"validated":true}'}):
        assert live_calibration.active_rubric(1) == {}, "old protocols must be revalidated"
    reference = {"id": 1, "source_id": 1, "title": "测试", "duration": 60, "segments": '[[0,60,"完整的转写"]]'}
    for bad in (float("nan"), float("inf")):
        with patch.object(live_calibration, "q", return_value={"name": "测试"}), patch.object(live_calibration.llm, "llm_json", return_value={
                "scores": [{"id": 1, "actual_clip": True, "topic": bad, "traffic": 90, "fun": 90}]}):
            try:
                live_calibration.blind_scores([reference], {})
            except ValueError:
                pass
            else:
                raise AssertionError("non-finite calibration scores accepted")
    with patch.object(live_calibration, "q", return_value={"name": "测试"}), patch.object(live_calibration.llm, "llm_json", return_value={
            "scores": [{"id": 1, "actual_clip": False}]}):
        assert live_calibration.blind_scores([reference], {}, exclude_nonclips=True) == {}
        try:
            live_calibration.blind_scores([reference], {})
        except ValueError:
            pass
        else:
            raise AssertionError("unconfirmed source accepted in held-out scoring")


def calibration_candidates():
    now = time.time()
    rows = [{'id': i, 'source_id': 1, 'title': f'样本{i}', 'duration': 3600,
             'published': now-3*86400, 'cohort': 'high' if i < 4 else 'control',
             'segments': json.dumps([[0, 60, '开头'*800], [1700, 1800, '中间'*800], [3500, 3600, '结尾'*800]])}
            for i in range(8)]
    excerpt = live_calibration.transcript_excerpt(rows[0], 900)
    assert len(excerpt) <= 900 and all(word in excerpt for word in ('开头', '中间', '结尾', '省略'))
    rules = [{'when': f'出现具体冲突{i}', 'prefer': '有过程和结果', 'avoid': '仅标题有冲突',
              'support_ids': [0, 1], 'counter_ids': [4, 5]} for i in range(3)]
    options = live_calibration.candidate_rubrics({'candidates': [{'contrasts': rules}]}, rows)
    assert len(options) == 1 and len(options[0][0]['rules']) == 2
    assert {e['role'] for e in options[0][0]['examples']} == {'positive', 'counter'}
    for support in ([999], [True], [4]):
        bad = [{**rule, 'support_ids': support} for rule in rules]
        assert not live_calibration.candidate_rubrics({'candidates': [{'contrasts': bad}]}, rows)
    encoded = [{**rule, 'support_ids': ['0', '1'], 'counter_ids': [4.0, 5.0]} for rule in rules]
    assert live_calibration.candidate_rubrics({'candidates': [{'contrasts': encoded}]}, rows)
    for bad_id in (True, 1.1, float('nan'), '1.5', 'other'):
        assert live_calibration.reference_id(bad_id) is None
    payloads = []
    def score(_prompt, payload, *args):
        data = json.loads(payload); payloads.append(data)
        assert len(data['videos']) <= 4
        assert all('views' not in v and 'cohort' not in v and 'author' not in v for v in data['videos'])
        return {'scores': [{'id': v['id'], 'actual_clip': True, 'topic': 80, 'traffic': 80, 'fun': 80}
                           for v in data['videos']]}
    with patch.object(live_calibration, 'q', return_value={'name': '测试'}), patch.object(live_calibration.llm, 'llm_json', side_effect=score):
        assert len(live_calibration.blind_scores(rows, {})) == 8 and len(payloads) == 2
    valid = {'scores': [{'id': '0', 'actual_clip': True, 'topic': 80, 'traffic': 80, 'fun': 80}]}
    wrong = {'scores': [{**valid['scores'][0], 'id': '999'}]}
    with patch.object(live_calibration, 'q', return_value={'name': '测试'}), patch.object(live_calibration.llm, 'llm_json', side_effect=[wrong, valid]) as evaluate:
        assert set(live_calibration.blind_scores(rows[:1], {})) == {0}
        assert 'validation_feedback' in json.loads(evaluate.call_args.args[1])
    with patch.object(live_calibration, 'q', return_value={'name': '测试'}), patch.object(live_calibration.llm, 'llm_json', return_value=wrong) as evaluate:
        try:
            live_calibration.blind_scores(rows[:1], {})
        except ValueError:
            assert evaluate.call_count == 2
        else:
            raise AssertionError('unknown IDs must remain rejected after a bounded repair')
    baseline = {r['id']: {'score': 80} for r in rows}
    revised = {r['id']: {'score': 90-r['id']} for r in rows}
    assert live_calibration.validation_result(rows, baseline, revised, now)['validated']
    reverse = {r['id']: {'score': r['id']} for r in rows}
    failed = live_calibration.validation_result(rows, baseline, reverse, now)
    assert not failed['validated'] and len(failed['rejection_reasons']) >= 3
    old_rows = [{**r, 'published': now-40*86400} for r in rows]
    assert not live_calibration.validation_result(old_rows, baseline, revised, now)['validated']


def ai_alias_evidence():
    source = {'id': 999991, 'uid': 99, 'room_id': 32197497, 'name': '183的冯予希律师', 'settings': '{}'}
    examples = [
        {'bvid': 'BV0000000001', 'mid': 99, 'title': '冯律直播答疑', 'description': '', 'published': 100},
        {'bvid': 'BV0000000002', 'mid': 44, 'title': '李老八直播', 'description': '同名账号', 'published': 100},
        {'bvid': 'BV0000000003', 'mid': 45, 'title': '冯老师连麦', 'description': 'https://live.bilibili.com/321974970', 'published': 100}]
    def extract(_prompt, payload, *args):
        assert [x['bvid'] for x in json.loads(payload)['identity_examples']] == ['BV0000000001']
        return {'aliases': [{'name': name, 'support_bvids': [bvid]} for name, bvid in
                            [('冯律','BV0000000001'), ('李老八','BV0000000002'), ('凭空编造','BV0000000001')]]}
    try:
        with patch.object(live_calibration.llm, 'llm_json', side_effect=extract) as model:
            assert live_calibration.infer_aliases(source, examples) == ['冯律']
            assert live_calibration.infer_aliases(source, examples) == ['冯律']
            assert model.call_count == 1
        assert '冯律' in live_calibration.search_terms(source)
    finally:
        core.q('DELETE FROM kv WHERE k=?', ('live-source-aliases:999991',))


def reference_acquisition():
    owner, now = 'user:reference-pipeline', time.time()
    core.q("INSERT INTO live_sources(owner,room_id,uid,name,url,settings,enabled) VALUES (?,1234,99,'183的冯予希律师','room','{}',0)", (owner,))
    source = core.q('SELECT * FROM live_sources WHERE owner=?', (owner,), one=True)
    sid = source['id']
    try:
        assert {'183的冯予希律师', '冯予希律师', '冯予希'} <= set(live_calibration.search_terms(source))
        alias_source = {**dict(source), 'settings': json.dumps({'search_keywords': '冯律，冯律师', 'source_aliases': '小冯'})}
        assert {'冯律', '冯律师', '小冯', source['name']} <= set(live_calibration.search_terms(alias_source))
        base = {'bvid': 'BV0000000000', 'title': '直播讨论', 'author': '样本作者', 'mid': 123, 'published': now-86400*3,
                'duration': 120, 'views': 2000, 'description': '直播切片'}
        items = [{**base, 'bvid': f'BV{i:010d}', 'title': f'讨论{i}'} for i in range(35)]
        items[0].update(mid=99)  # Own publication is a style reference, never held-out performance evidence.
        items[1].update(published=now-200*86400)
        items[2].update(views=30)
        items[3].update(published=now-3600)
        for i in (0, 1, 2, 3):
            assert live_calibration.reference_role(items[i], source, now) == 'style'
        assert live_calibration.reference_role(items[4], source, now) == 'evaluation'
        # The model explicitly rejects one item, leaves one undecided, and matches the remainder.
        def metadata(_prompt, payload, *args):
            if 'identity_examples' in json.loads(payload):
                return {'aliases': []}
            values = json.loads(payload)['samples']
            return {'matches': [{'bvid': x['bvid'], 'status': 'rejected' if x['bvid'] == items[5]['bvid'] else 'matched',
                                 'reason': '确认为另一位主播' if x['bvid'] == items[5]['bvid'] else '标题与简介匹配'}
                                for x in values if x['bvid'] != items[4]['bvid']]}
        session = Mock()
        session.get.return_value.json.return_value = {'code': 0, 'data': {'result': [
            {**item, 'pubdate': item['published'], 'play': item['views'], 'duration': '2:00'} for item in items]}}
        with patch.object(live_calibration.channels, 'bili_session', return_value=session), \
             patch.object(live_calibration.channels, 'bili_signed', return_value={}), \
             patch.object(live_calibration.bili, 'account_path', return_value=Path('/tmp/no-test-account')), \
             patch.object(live_calibration.time, 'sleep'), patch.object(live_calibration.llm, 'llm_json', side_effect=metadata):
            discovered = live_calibration.discover(source)
        assert len(discovered) == 34
        assert {x['bvid'] for x in items[:5]} <= {x['bvid'] for x in discovered}
        assert next(x for x in discovered if x['bvid'] == items[4]['bvid'])['metadata_match'] == 'pending'
        audit = core.kv_get(f'live-search-audit:{sid}')
        assert audit['pending'] == 1 and audit['excluded']['明确不是目标主播直播切片'] == 1
        identity = live_calibration.source_identity(source)
        assert identity['uid'] == 99 and [r['bvid'] for r in identity['own_publications']] == [items[0]['bvid']]
        with tempfile.TemporaryDirectory() as folder, patch.object(live, 'workspace', return_value=Path(folder)), \
             patch.object(live_calibration.shutil, 'disk_usage', return_value=Mock(free=100<<30)), \
             patch.object(live_calibration, 'discover', return_value=discovered):
            result = live_calibration.collect({'payload': {'source': sid}}, lambda _: True)
            assert result['references'] == 34 and result['queued'] == 6
            assert live_calibration.pump_references(source) == 0, 'do not enqueue another batch while downloads/ASR are pending'
        refs = core.q('SELECT * FROM live_references WHERE source_id=? ORDER BY id', (sid,))
        assert len(refs) == 34 and sum(r['state'] == 'pending' for r in refs) == 28
        for i, row in enumerate(refs):
            speech = ''.join(chr(0x6000+i*600+n) for n in range(500))
            core.q("UPDATE live_references SET state='ready',segments=? WHERE id=?", (json.dumps([[0,120,speech]]), row['id']))
        assert live_calibration.sample_progress(source)['independent'] == 0, 'metadata match alone cannot enter validation'
        unknown_id = refs[4]['id']
        def verify(_prompt, payload, *args):
            decoded = json.loads(payload)
            assert decoded['source']['own_publications'] == identity['own_publications']
            values = decoded['samples']
            assert all(x['transcript'] for x in values)
            return {'matches': [{'id': str(x['id']), 'status': 'uncertain' if x['id'] == unknown_id else 'matched',
                                 'reason': '转写有明确直播互动与身份依据'} for x in values]}
        with patch.object(live_calibration.llm, 'llm_json', side_effect=verify):
            for start in range(0, len(refs), 8):
                live_calibration.verify_references({'payload': {'source': sid, 'references': [r['id'] for r in refs[start:start+8]]}}, lambda _: True)
        recent, style = live_calibration.sample_pools(source, now)
        assert len(recent) == 29 and len(style) == 4
        with patch.object(live_calibration, 'independent_samples', side_effect=AssertionError('unchanged samples recomputed')):
            cached_recent, cached_style = live_calibration.sample_pools(source, now)
            assert [r['id'] for r in cached_recent] == [r['id'] for r in recent]
            assert [r['id'] for r in cached_style] == [r['id'] for r in style]
            progress = live_calibration.cached_sample_progress(source)
            assert progress['independent'] == 29 and progress['style'] == 4
        with patch.object(live_calibration, 'independent_samples', wraps=live_calibration.independent_samples) as compare:
            core.q('UPDATE live_references SET views=views+1 WHERE id=?', (recent[0]['id'],))
            live_calibration.sample_pools(source, now)
            assert compare.called, 'changed evidence must invalidate the saved sample pool'
        assert unknown_id not in {r['id'] for r in recent+style}
        assert {r['id'] for r in recent}.isdisjoint(r['id'] for r in style)
        # A historical repost overlapping a held-out-eligible sample cannot feed style learning.
        core.q('UPDATE live_references SET segments=(SELECT segments FROM live_references WHERE id=?) WHERE id=?', (refs[6]['id'], refs[1]['id']))
        assert len(live_calibration.sample_pools(source, now)[1]) == 3
        style_ids = {r['id'] for r in live_calibration.sample_pools(source, now)[1]}
        with patch.object(live_calibration.llm, 'llm_json', return_value={'summary':'风格说明', 'observations':[
                {'text':'通过连麦追问补齐背景', 'reference_ids': list(style_ids)}]}):
            live_calibration.learn_style({'payload': {'source': sid}}, lambda _: True)
        assert set(core.kv_get(f'live-style:{sid}')['reference_ids']) == style_ids
        assert not live_calibration.active_rubric(sid), 'learning style must not enable automatic publication'
        # Retrying source verification does not require downloading again.
        core.q("UPDATE live_references SET source_match='pending',updated=? WHERE id=?", (now+1, unknown_id))
        live_calibration.queue_reference_work(source)
        first = core.q("SELECT id FROM tasks WHERE kind='live_verify' AND json_extract(payload,'$.source')=? ORDER BY id DESC", (sid,), one=True)['id']
        core.q("UPDATE tasks SET state='done' WHERE id=?", (first,))
        core.q('UPDATE live_references SET updated=? WHERE id=?', (now+2, unknown_id))
        live_calibration.queue_reference_work(source)
        assert core.q("SELECT COUNT(*) n FROM tasks WHERE kind='live_verify' AND json_extract(payload,'$.source')=?", (sid,), one=True)['n'] == 2
    finally:
        core.q("DELETE FROM tasks WHERE json_extract(payload,'$.source')=? OR (kind='live_reference' AND json_extract(payload,'$.reference') IN (SELECT id FROM live_references WHERE source_id=?))", (sid, sid))
        for table in ('live_references', 'live_rubrics', 'live_sources'):
            core.q(f'DELETE FROM {table} WHERE owner=?', (owner,))
        core.q('DELETE FROM kv WHERE k IN (?,?,?,?)', (f'live-search-audit:{sid}', f'live-style:{sid}', f'live-source-identity:{sid}', f'live-source-aliases:{sid}'))


def calibration_refine_roundtrip():
    owner, now = 'user:calibration-regression', time.time()
    core.q("INSERT INTO live_sources(owner,room_id,uid,url,name,enabled,settings,created,updated) VALUES (?,123,1,'room','校准测试',0,'{}',?,?)", (owner, now, now))
    source = core.q('SELECT * FROM live_sources WHERE owner=?', (owner,), one=True);sid=source['id']
    def add_rows(start, count):
        for i in range(start, start+count):
            speech = ''.join(chr(0x4e00+i*600+n) for n in range(500))
            core.q("INSERT INTO live_references(owner,source_id,bvid,title,author,published,duration,views,state,source_match,segments,updated) "
                   "VALUES (?,?,?,?,?,?,120,?,'ready','matched',?,?)", (owner,sid,f'test{i}',f'样本{i}','同一个切片账号',
                    now-3*86400, int(100*1.3**(39-i)) if i<40 else int(100*1.4**(63-i)), json.dumps([[0,120,speech]]),now))
    try:
        add_rows(0,40)
        unique, style = live_calibration.sample_pools(source)
        snapshot, _ = live_calibration.calibration_snapshot(source, unique, style)
        assert snapshot
        fit_ids={r['id'] for r in snapshot['train']};tuning_ids={r['id'] for r in snapshot['tuning']};test_ids={r['id'] for r in snapshot['heldout']}
        assert len(fit_ids)>=8 and len(tuning_ids)>=4 and len(test_ids)>=8
        assert fit_ids.isdisjoint(tuning_ids|test_ids) and tuning_ids.isdisjoint(test_ids)
        # A pending acquisition must not block an already-usable round.
        core.q("INSERT INTO live_references(owner,source_id,bvid,title,state) VALUES (?,?,'pending-sample','待下载','pending')",(owner,sid))
        assert live_calibration.queue_refine(source)
        calls, proposals, fail = [], [], [False]
        def propose(_prompt,payload,*args):
            data=json.loads(payload);training=data['training'];proposals.append(data)
            assert {r['id'] for r in training}.isdisjoint(test_ids|tuning_ids)
            high=[r['id'] for r in training if r['cohort']=='high'];low=[r['id'] for r in training if r['cohort']=='control']
            return {'candidates':[{'summary':name,'contrasts':[{'when':name+'具备可核对的条件','prefer':'不同观看动机','avoid':'只按题材归类',
                                   'support_ids':high[:2],'counter_ids':low[:2]}]} for name in ('平稳','冲突') ]}
        def scores(rows,rubric,exclude_nonclips=False,anchor=None):
            ids={r['id'] for r in rows};calls.append((ids,rubric))
            if rubric:
                assert ids.isdisjoint(fit_ids), 'candidate selection must not rescore its own fitting data'
                assert anchor is not None
            better=rubric.get('summary')=='冲突' and not fail[0]
            return {r['id']:{'score':(90 if r['cohort']=='high' else 60) if better else 80,
                            'dimensions':{'topic':80,'traffic':80,'fun':80},'reason':'测试'} for r in rows}
        with patch.object(live_calibration.llm,'llm_json',side_effect=propose),patch.object(live_calibration,'blind_scores',side_effect=scores):
            result=live_calibration.refine({'payload':{'source':sid}},lambda _:True)
            assert result['validated']
            active=live_calibration.active_rubric(sid);assert active['summary']=='冲突'
            evidence=json.loads(core.q('SELECT evidence FROM live_rubrics WHERE source_id=?',(sid,),one=True)['evidence'])
            assert evidence['metric']=='within_publisher_pairs' and evidence['validation_evaluated']
            tested=[rid for ids,rubric in calls if rubric and ids<=test_ids for rid in ids]
            assert set(tested)==test_ids and len(tested)==len(test_ids), 'final candidate is reviewed exactly once per video'
            before=(len(calls),len(proposals))
            fail[0]=True
            assert live_calibration.refine({'payload':{'source':sid}},lambda _:True)['reused']
            assert before==(len(calls),len(proposals)), 'repeat clicks must not reroll model scores or candidates'
            add_rows(40,24)
            newer,_=live_calibration.sample_pools(source)
            new_snapshot,_=live_calibration.calibration_snapshot(source,newer)
            assert new_snapshot
            assert test_ids.isdisjoint(r['id'] for k in ('train','tuning','heldout') for r in new_snapshot[k])
            assert (fit_ids|tuning_ids).isdisjoint(r['id'] for r in new_snapshot['heldout'])
            # The new weak candidate fails on tuning; do not spend final-validation calls or replace the incumbent.
            fit_ids={r['id'] for r in new_snapshot['train']};tuning_ids={r['id'] for r in new_snapshot['tuning']}
            test_ids={r['id'] for r in new_snapshot['heldout']}
            assert not live_calibration.refine({'payload':{'source':sid}},lambda _:True)['validation_evaluated']
            reserved=core.kv_get(f'live-calibration-reserved:v6:{sid}')
            assert test_ids.isdisjoint(reserved['tested']), 'tuning failure must leave final test unused'
            assert live_calibration.active_rubric(sid)==active
    finally:
        core.q("DELETE FROM tasks WHERE json_extract(payload,'$.source')=?",(sid,))
        for table in ('live_references','live_rubrics','live_sources'):
            core.q(f'DELETE FROM {table} WHERE owner=?',(owner,))
        for prefix in ('live-calibration-round:v6:','live-calibration-reserved:v6:','live-calibration-wait:'):
            core.q('DELETE FROM kv WHERE k=?',(prefix+str(sid),))


def matched_pair_metrics():
    now=time.time()
    rows=[{'id':i,'bvid':str(i),'source_id':1,'title':str(i),'author':'大号' if i<4 else '小号',
           'reference_info':'{}','segments':'[[0,120,"测试转写"]]','published':now-3*86400,'duration':120,'views':v}
          for i,v in enumerate((100000,80000,20000,10000,1000,800,200,100))]
    pairs=live_calibration.comparable_pairs(rows)
    assert len(pairs)==4 and all(p[0]['author']==p[1]['author'] for p in pairs)
    assert any(p[0]['author']=='小号' for p in pairs)
    old={**rows[1],'id':99,'published':now-80*86400}
    assert not live_calibration.comparable_pairs([rows[0],old]), 'large temporal gaps are not comparable'
    scored=[r for p in pairs for r in p]
    good={r['id']:{'score':90 if r['cohort']=='high' else 60} for r in scored}
    assert live_calibration.rank_accuracy(scored,good)==1
    assert live_calibration.rank_pvalue(scored,good)==1/16
    assert live_calibration.rank_pvalue(scored,{r['id']:{'score':80} for r in scored})==1
    with patch.object(live_calibration,'blind_scores',return_value=good) as model:
        first=live_calibration.cached_scores(scored,{'test-cache':'pairs'})
        assert first==good
        before=model.call_count
        assert live_calibration.cached_scores(scored,{'test-cache':'pairs'})==good and model.call_count==before
        changed=[{**r,'title':r['title']+'新'} for r in scored]
        live_calibration.cached_scores(changed,{'test-cache':'pairs'})
        assert model.call_count>before, 'changed content must not reuse an old review'
    attempted=[]
    def interrupted(batch, *args, **kwargs):
        attempted.append({r['id'] for r in batch})
        if len(attempted)==2:
            raise RuntimeError('模拟后续批次中断')
        return good
    with patch.object(live_calibration,'blind_scores',side_effect=interrupted):
        try:
            live_calibration.cached_scores(scored,{'test-cache':'interrupted'})
        except RuntimeError:
            pass
        else:
            raise AssertionError('expected interruption')
    resumed=[]
    def resume(batch,*args,**kwargs):
        resumed.extend(r['id'] for r in batch)
        return good
    with patch.object(live_calibration,'blind_scores',side_effect=resume):
        assert live_calibration.cached_scores(scored,{'test-cache':'interrupted'})==good
    assert set(resumed)=={r['id'] for r in scored}-attempted[0], 'saved batches survive a later model failure'


def publication_attribution():
    source = {'name': '测试主播', 'room_id': 987654, 'url': 'https://b23.tv/share'}
    assert live_bilibili.publication_title({'title': '一个具体问题'}, source) == '测试主播：一个具体问题'
    assert live_bilibili.publication_title({'title': '测试主播：一个具体问题'}, source) == '测试主播：一个具体问题'
    assert len(live_bilibili.publication_title({'title': '长标题'*40}, source)) == 80
    named = {**source, 'name': '隔壁老谢GBLX', 'settings': json.dumps({'title_name': '老谢'})}
    hook = '明星为什么更离不开 iPhone？老谢：谈生意时 AirDrop 太关键'
    assert live_bilibili.publication_title({'title': hook}, named) == hook
    assert live_bilibili.publication_title({'title': '一个具体问题', 'origin': json.dumps({'name': '原主播'})}, source) == '原主播：一个具体问题'
    for summary in ('', '手动修改的简介', '长简介'*1000):
        clip = {'id': 3, 'version': 2, 'description': summary}
        text = live_bilibili.publication_description(clip, source)
        assert len(text) <= 1800
        assert '原主播：测试主播' in text and '原直播间：https://live.bilibili.com/987654' in text
        assert '拾光切片 #3-v2' in text and source['url'] not in text
        assert clip['description'] == summary
    with tempfile.TemporaryDirectory(prefix='live-publish-cover-') as folder:
        credentials = Path(folder)/'credentials.json'
        credentials.write_text(json.dumps({'token_info': {'mid': '123'}}))
        clip = {'id': 3, 'version': 2, 'owner': 'user:test', 'title': hook,
                'description': '主播个人观察', 'tags': '["直播切片"]', 'file': '/unused/video.mp4'}
        commands = []
        def start(command, stdout, **kwargs):
            commands.append(command)
            stdout.write(b'{"bvid":"BV0000000001","aid":123}')
            return Mock(returncode=0, poll=Mock(return_value=0))
        with patch.object(live_bilibili, 'ready', return_value=True), \
             patch.object(live_bilibili, 'account_path', return_value=credentials), \
             patch.object(live_bilibili, 'client', return_value=Mock()), \
             patch.object(live_bilibili, 'api', return_value={'isLogin': True, 'mid': 123}), \
             patch('shiguang.live_cover.ensure_cover', return_value=Path(folder)/'designed-cover.jpg'), \
             patch.object(live_bilibili.subprocess, 'Popen', side_effect=start):
            result = live_bilibili.publish(clip, named, lambda _: True)
        assert result['bvid'] == 'BV0000000001'
        assert commands[0][commands[0].index('--title')+1] == hook
        assert commands[0][commands[0].index('--cover')+1].endswith('designed-cover.jpg')


def event_monitoring():
    packed = live_events.packet(5, {"cmd": "LIVE", "roomid": 123}) + live_events.packet(5, {"cmd": "PREPARING"})
    compressed = zlib.compress(packed)
    wrapped = live_events.HEADER.pack(len(compressed)+16, 16, 2, 5, 1) + compressed
    assert [v[1]["cmd"] for v in live_events.messages(wrapped)] == ["LIVE", "PREPARING"]
    for bad in (b"bad", live_events.HEADER.pack(0, 16, 1, 5, 1), wrapped[:-1]):
        try:
            list(live_events.messages(bad))
        except ValueError:
            pass
        else:
            raise AssertionError("malformed event packet accepted")
    owner = "user:live-events"
    now = time.time()
    core.q("INSERT INTO live_sources (owner,room_id,uid,url,name,enabled,settings,checked,created,updated) "
           "VALUES (?,123,1,'https://live.bilibili.com/123','事件测试',1,'{}',?,?,?)", (owner, now, now, now))
    source = dict(core.q("SELECT * FROM live_sources WHERE owner=?", (owner,), one=True))
    sid = source["id"]
    def current():
        return dict(core.q("SELECT * FROM live_sources WHERE id=?", (sid,), one=True))
    listener = live_events.Listener(source)
    try:
        assert not live_events.check_due(source, now+60)
        assert not live_events.check_due(source, now+1799)
        assert live_events.check_due(source, now+1800)
        assert live_events.check_due({**source, "checked": None}, now)
        with patch.object(live_events, "request_check") as wake:
            listener.handle(5, {"cmd": "LIVE", "roomid": 999})
            assert not wake.called
            listener.handle(5, {"cmd": "LIVE", "roomid": 123})
            listener.handle(5, {"cmd": "LIVE", "roomid": 123})
            assert wake.call_count == 1
            listener.handle(5, {"cmd": "PREPARING"})
            assert wake.call_count == 2
        assert listener.handle(8, {"code": 0})
        pending = live_events.check_request(source)
        assert pending["reason"] == "connected" and live_events.check_due(source, time.time(), pending)
        assert core.kv_get(f"live-monitor:{sid}")["state"] == "connected"
        # A new event must not be removed when consuming an older request.
        live_events.request_check(source, "LIVE")
        live_events.consume_check(source, pending)
        assert live_events.check_request(source)["reason"] == "LIVE"
        info = {"live": False, "title": "测试"}
        with patch.object(live_recorder.bili, "room_info", return_value=info) as query:
            live_recorder.check_sources([current()], time.time())
            assert query.call_count == 1
            assert current()["live_status"] == "offline"
            live_recorder.check_sources([current()], time.time()+1799)
            assert query.call_count == 1, "offline checks must wait 30 minutes"
            live_recorder.check_sources([current()], time.time()+1801)
            assert query.call_count == 2
            # Manual check bypasses the interval.
            core.q("UPDATE live_sources SET checked=NULL WHERE id=?", (sid,))
            live_recorder.check_sources([current()], time.time())
            assert query.call_count == 3
        live_events.request_check(source, "recording-ended", delay=60)
        pending = live_events.check_request(source)
        assert not live_events.check_due(current(), pending["at"]-1, pending)
        assert live_events.check_due(current(), pending["at"], pending)
        # A LIVE event starts recording even when the normal status check was just completed.
        live_events.request_check(source, "LIVE")
        with patch.object(live_recorder.bili, "room_info", return_value={**info, "live": True}), \
             patch.object(live_recorder.shutil, "disk_usage", return_value=Mock(free=20<<30)), \
             patch.object(live_recorder, "buffer_bytes", return_value=0), \
             patch.object(live_recorder.threading, "Thread") as thread:
            live_recorder.check_sources([current()], time.time())
            assert sid in live_recorder.ACTIVE and thread.return_value.start.call_count == 1
            live_recorder.check_sources([current()], time.time()+1801)
            assert thread.return_value.start.call_count == 1, "do not start duplicate recorders"
            # Other rooms still get checked when all recording slots are occupied.
            live_recorder.ACTIVE.pop(sid)
            with patch.object(live_recorder, "MAX_STREAMS", 0):
                live_events.request_check(source, "LIVE")
                live_recorder.check_sources([current()], time.time())
                assert current()["live_status"] == "live" and "名额已满" in current()["error"]
                assert live_events.check_request(source)["reason"] == "recording-blocked"
        try:
            listener.handle(8, {"code": -101})
        except ValueError:
            pass
        else:
            raise AssertionError("failed authentication treated as connected")
        class FakeListener:
            def __init__(self, row):
                self.source = dict(row)
                self.stop = Mock()
                self.thread = Mock()
                self.thread.is_alive.return_value = True
        with patch.object(live_events, "Listener", FakeListener):
            manager = live_events.Subscriptions()
            manager.sync([source])
            original = manager.listeners[sid]
            manager.sync([source]); assert manager.listeners[sid] is original
            manager.sync([]); original.stop.set.assert_called_once()
            original.thread.is_alive.return_value = False
            manager.sync([]); assert not manager.listeners
    finally:
        live_recorder.ACTIVE.pop(sid, None)
        core.q("DELETE FROM live_sources WHERE id=?", (sid,))
        core.q("DELETE FROM kv WHERE k IN (?,?)", (f"live-monitor:{sid}", f"live-check:{sid}"))


def recorder_lifecycle():
    # This suite runs only on smoke.py's disposable database. Isolate housekeeping from real-library rows.
    for table in ("live_clips", "live_recordings", "live_sessions", "live_references", "live_rubrics", "live_sources", "live_accounts"):
        core.q(f"DELETE FROM {table}")
    core.q("DELETE FROM tasks WHERE kind LIKE 'live_%'")
    owner = "user:live-lifecycle"
    with tempfile.TemporaryDirectory(prefix="live-lifecycle-") as folder, patch.object(live, "ROOT", Path(folder)):
        now = time.time()
        core.q("INSERT INTO live_sources (owner,room_id,uid,url,name,enabled,settings,created,updated) "
               "VALUES (?,1,1,'https://live.bilibili.com/1','测试',0,'{}',?,?)", (owner, now, now))
        sid = core.q("SELECT id FROM live_sources WHERE owner=?", (owner,), one=True)["id"]
        def session(key, cursor, topic_start=0):
            core.q("INSERT INTO live_sessions (key,source_id,owner,title,topic_title,cursor,topic_start,started) "
                   "VALUES (?,?,?,'测试直播','当前话题',?,?,?)", (key, sid, owner, cursor, topic_start, now))
        def recording(key, seq, state="ready"):
            path = Path(folder)/f"{key}-{seq}.mkv"
            path.write_bytes(b"fixture")
            core.q("INSERT INTO live_recordings (owner,source_id,session_key,seq,offset,duration,path,state,segments,created,updated) "
                   "VALUES (?,?,?,?,?,60,?,?,?,?,?)", (owner, sid, key, seq, seq*60, str(path), state,
                      json.dumps([[0, 60, f"第 {seq} 段的讨论"]]), now-13*3600, now))
            return core.q("SELECT * FROM live_recordings WHERE session_key=? AND seq=?", (key, seq), one=True)
        try:
            session("failed-recording", 60)
            recording("failed-recording", 0)
            bad = recording("failed-recording", 1, "transcribing")
            recording("failed-recording", 2)
            tid = board.publish("live_transcribe", f"live-recording:{bad['id']}", payload={"recording": bad["id"]})
            core.q("UPDATE tasks SET state='failed' WHERE id=?", (tid,))
            live_recorder.schedule_analysis()
            assert core.q("SELECT state FROM live_recordings WHERE id=?", (bad["id"],), one=True)["state"] == "failed"
            assert core.q("SELECT id FROM tasks WHERE kind='live_analyze' AND target='live-session:failed-recording'", one=True)
            assert live.analyze({"payload": {"session": "failed-recording"}}, lambda _: True) == {"gap": True, "until": 120}
            with patch.object(live.llm, "llm_json", return_value={"highlights": [], "topic_changes": [], "current_topic": "后续讨论"}) as model:
                assert live.analyze({"payload": {"session": "failed-recording"}}, lambda _: True)["until"] == 180
                prompt = json.loads(model.call_args.args[1])
                assert prompt["available_start"] == 120
                assert all(segment[0] >= 120 for segment in prompt["transcript"])
            clips = core.q("SELECT start,end FROM live_clips WHERE session_key='failed-recording'")
            assert [(c["start"], c["end"]) for c in clips] == [(0, 60)], "known topic must close before the ASR gap"
            # A failed tail must be scheduled even when no ready recording follows it.
            recording("failed-recording", 3, "failed")
            core.q("UPDATE live_sessions SET status='ended' WHERE key='failed-recording'")
            core.q("UPDATE tasks SET state='done' WHERE kind='live_analyze'")
            live_recorder.schedule_analysis()
            assert core.q("SELECT state FROM tasks WHERE kind='live_analyze' AND target='live-session:failed-recording'", one=True)["state"] == "queued"
            assert live.analyze({"payload": {"session": "failed-recording"}}, lambda _: True)["until"] == 240
            assert core.q("SELECT start,end FROM live_clips WHERE session_key='failed-recording' AND start=120 AND end=180", one=True)
            # Force cleanup under buffer pressure; preserve even a segment straddling topic_start.
            session("open-topic", 25*60, 390)
            recordings = [recording("open-topic", seq) for seq in range(25)]
            core.q("INSERT INTO live_clips (owner,source_id,session_key,lane,start,end,title,state) "
                   "VALUES (?,?,'open-topic','highlight',60,120,'待制作亮点','queued')", (owner, sid))
            live_recorder.cleanup(force=True)
            assert not Path(recordings[0]["path"]).exists(), "closed, unneeded footage should be reclaimed"
            assert Path(recordings[1]["path"]).exists(), "queued clip's footage must survive"
            assert all(Path(r["path"]).exists() for r in recordings[6:]), "open topic footage was deleted"
            source = core.q("SELECT * FROM live_sources WHERE id=?", (sid,), one=True)
            cid = live.create_clip(owner, source, "open-topic", "topic", {"start": 390, "end": 1500, "title": "完整话题"})
            core.q("UPDATE live_sessions SET topic_start=cursor WHERE key='open-topic'")
            live_recorder.cleanup(force=True)
            clip = core.q("SELECT * FROM live_clips WHERE id=?", (cid,), one=True)
            assert len(live.render_inputs(clip)[0]) == 19, "topic must remain renderable after closing"
        finally:
            for table in ("live_clips", "live_recordings", "live_sessions", "live_sources"):
                core.q(f"DELETE FROM {table} WHERE owner=?", (owner,))
            core.q("DELETE FROM tasks WHERE kind LIKE 'live_%'")


def recorder_connection_end():
    owner='user:recorder-connection-test'
    core.q("INSERT INTO live_sources(owner,room_id,uid,url,name,settings) VALUES (?,992,1,'https://live.bilibili.com/992','重连测试','{}')", (owner,))
    source=dict(core.q('SELECT * FROM live_sources WHERE owner=?',(owner,),one=True))
    clock=[1000]
    process=Mock();process.poll.return_value=0
    try:
        with tempfile.TemporaryDirectory() as directory, patch.object(live,'ROOT',Path(directory)):
            for still_live, runtime, status, delay in [(True,3600,'interrupted',5),(True,1,'interrupted',60),
                                                       (False,3600,'ended',60),(None,3600,'interrupted',60)]:
                def start(*args,**kwargs):
                    clock[0]+=runtime
                    return process
                with patch.object(live_recorder.time,'time',side_effect=lambda:clock[0]), \
                        patch.object(live_recorder.subprocess,'Popen',side_effect=start), \
                        patch.object(live_recorder.bili,'stream_url',return_value='https://example.test/live'), \
                        patch.object(live_recorder.bili,'room_info',return_value={'live':still_live},
                                     side_effect=RuntimeError('unavailable') if still_live is None else None), \
                        patch.object(live_events,'request_check') as wake:
                    live_recorder.capture(source,{'title':'直播','live_start':'2026-10-19 20:00:00'})
                row=core.q('SELECT * FROM live_sessions WHERE owner=? ORDER BY started DESC LIMIT 1',(owner,),one=True)
                assert row['status']==status and wake.call_args.kwargs['delay']==delay
                assert json.loads(row['origin'])['live_start']=='2026-10-19 20:00:00'
    finally:
        core.q('DELETE FROM live_sessions WHERE owner=?',(owner,))
        core.q('DELETE FROM live_sources WHERE owner=?',(owner,))


def workspace_roundtrip():
    from flask import g
    owner = "user:live-regression"
    core.q("INSERT OR IGNORE INTO users (name, pw, created) VALUES ('live-regression', '', ?)", (time.time(),))
    client = core.app.test_client()
    device = "1"*32
    core.q("INSERT OR REPLACE INTO devices (id, user) VALUES (?, 'live-regression')", (device,))
    client.set_cookie("grabber_device", device)
    with client.session_transaction() as session:
        session["user"] = "live-regression"
    with tempfile.TemporaryDirectory(prefix="live-regression-") as folder, patch.object(live, "ROOT", Path(folder)/"workspace"):
        root = live.workspace()
        source = None
        try:
            with patch.object(live_bilibili, "room_info", return_value={"room_id": 987654, "uid": 123,
                    "url": "https://live.bilibili.com/987654", "name": "测试主播"}):
                response = client.post("/api/live/sources", json={"url": "https://b23.tv/test123"})
                assert response.status_code == 200, response.data
            source = core.q("SELECT * FROM live_sources WHERE owner=?", (owner,), one=True)
            assert source["name"] == "测试主播" and source["enabled"] == 1
            # Adding needs only the shared link; detect the name and keep duplicate submissions idempotent.
            with patch.object(live_bilibili, "room_info", return_value={"room_id": 987654, "uid": 123,
                    "url": "https://live.bilibili.com/987654", "name": "自动识别的主播"}):
                duplicate = client.post("/api/live/sources", json={"url": "https://b23.tv/test123"})
                assert duplicate.status_code == 200 and duplicate.get_json()["already_tracked"]
            source_config = {**live.settings(), "calibration_days": 14}
            core.q("UPDATE live_sources SET settings=? WHERE id=?", (json.dumps(source_config), source["id"]))
            response = client.post(f"/api/live/sources/{source['id']}", json={"settings": {"preferences": "保留原配置"}})
            assert response.status_code == 200
            assert json.loads(core.q("SELECT settings FROM live_sources WHERE id=?", (source["id"],), one=True)["settings"])["calibration_days"] == 14
            # A scan is followed by phone confirmation; it must not invalidate the QR session.
            with patch.object(live.bili, "tv_request", return_value={"code": 0, "data": {
                    "url": "https://passport.bilibili.com/test-login", "auth_code": "test-auth-code"}}):
                qr = client.post("/api/live/login/qr", json={}).get_json()
            token = qr["login"]
            assert qr["qr"].startswith("data:image/png;base64,")
            for code, expected in ((86039, "waiting"), (86090, "scanned"), (86090, "scanned")):
                with patch.object(live.bili, "tv_request", return_value={"code": code}):
                    assert client.get(f"/api/live/login/qr/{token}").get_json()["status"] == expected
                    assert token in live.LOGINS
            with patch.object(live.bili, "tv_request", return_value={"code": -503}):
                assert client.get(f"/api/live/login/qr/{token}").status_code == 502
                assert token in live.LOGINS, "temporary API errors must not discard a scanned QR"
            with patch.object(live.bili, "tv_request", return_value={"code": 0, "data": {}}), \
                 patch.object(live.bili, "save_account", return_value="123"), \
                 patch.object(live.bili, "api", return_value={"uname": "测试投稿账号"}):
                assert client.get(f"/api/live/login/qr/{token}").get_json()["status"] == "done"
                assert token not in live.LOGINS
            live.LOGINS[token] = {"owner": owner, "expires": time.time()+180, "auth_code": "test-auth-code"}
            with patch.object(live.bili, "tv_request", return_value={"code": 86038}):
                assert client.get(f"/api/live/login/qr/{token}").get_json()["status"] == "expired"
                assert token not in live.LOGINS
            core.q("DELETE FROM live_accounts WHERE owner=?", (owner,))
            key = "live-regression-session"
            core.q("INSERT INTO live_sessions (key, source_id, owner, title, started, topic_title) VALUES (?,?,?,?,?,?)",
                   (key, source["id"], owner, "直播回归", time.time(), "开店成本"))
            directory = root/"recordings"/key
            directory.mkdir(parents=True)
            for seq in range(3):
                path = directory/f"chunk-{seq:06d}.mkv"
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10",
                                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "2",
                                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)], check=True, timeout=30)
                core.q("INSERT INTO live_recordings (owner,source_id,session_key,seq,offset,duration,path,state,created,updated) "
                       "VALUES (?,?,?,?,?,?,?,'ready',?,?)", (owner, source["id"], key, seq, seq*2, 2, str(path), time.time(), time.time()))
            response = client.post("/api/live/clips", json={"session": key, "start": .5, "end": 5.5,
                                   "title": "跨三段的切片", "tags": ["直播切片"]})
            assert response.status_code == 200, response.data
            cid = response.get_json()["id"]
            clip = core.q("SELECT * FROM live_clips WHERE id=?", (cid,), one=True)
            paths, seek = live.render_inputs(clip)
            assert len(paths) == 3 and seek == .5
            # Another account cannot inspect or publish this clip, even with a known ID.
            stranger = core.app.test_client()
            assert stranger.get(f"/api/live/clips/{cid}/media").status_code == 404
            assert stranger.post(f"/api/live/clips/{cid}/upload", json={}).status_code == 401
            payload = {"clip": cid, "version": clip["version"]}
            result = live.render({"payload": payload}, lambda progress: True)
            with patch('shiguang.live_cover.ensure_cover', side_effect=AssertionError('cover generated before upload confirmation')):
                live.after_render({"payload": json.dumps(payload)}, result)
            clip = core.q("SELECT * FROM live_clips WHERE id=?", (cid,), one=True)
            assert clip["state"] == "ready" and Path(clip["file"]).is_file()
            from PIL import Image
            from shiguang.live_cover import ensure_cover
            assert clip['cover'] == '', 'ready video must not eagerly generate a cover'
            generated_cover = ensure_cover(clip, dict(source))
            with Image.open(generated_cover) as cover:
                assert cover.size == (1280, 720)
            assert '测试主播' in clip['title']
            with patch('shiguang.live_cover.subprocess.run', side_effect=AssertionError('unchanged cover was regenerated')):
                assert ensure_cover(clip, dict(source)) == generated_cover
            video_mtime = Path(clip['file']).stat().st_mtime_ns
            old_cover = generated_cover
            with patch.object(live, 'queue_render', side_effect=AssertionError('cover edit re-encoded the video')):
                response = client.post(f'/api/live/clips/{cid}', json={
                    'start': clip['start'], 'end': clip['end'], 'title': clip['title'],
                    'cover_title': '开店成本\n到底怎么算？', 'tags': ['直播切片']})
                assert response.status_code == 200, response.data
            clip = core.q('SELECT * FROM live_clips WHERE id=?', (cid,), one=True)
            assert clip['cover'] == '' and old_cover.is_file(), 'editing the draft must not regenerate a cover'
            assert Path(clip['file']).stat().st_mtime_ns == video_mtime
            published_preview = next(c for c in client.get('/api/live').get_json()['clips'] if c['id'] == cid)
            assert '原主播：测试主播' in published_preview['publication_description']
            assert '原直播间：https://live.bilibili.com/987654' in published_preview['publication_description']
            response = client.get(f"/api/live/clips/{cid}/media", headers={"Range": "bytes=0-99"})
            assert response.status_code == 206 and len(response.data) == 100
            assert not live.queue_upload(cid, automatic=True)
            core.q("UPDATE live_sources SET enabled=1, settings=? WHERE id=?",
                   (json.dumps({**live.settings(), "auto_topics": True, "daily_limit": 1}), source["id"]))
            credential = root/"test-account.json"
            credential.write_text("{}")
            core.q("INSERT INTO live_accounts (owner, mid, name) VALUES (?, '123', 'test')", (owner,))
            core.q("INSERT INTO live_clips (owner,source_id,session_key,lane,start,end,title,state,file,version,render_version) "
                   "VALUES (?,?,?,'highlight',10,20,'second','ready',?,1,1)", (owner, source["id"], key, clip["file"]))
            second = core.q("SELECT MAX(id) n FROM live_clips WHERE owner=?", (owner,), one=True)["n"]
            with patch.object(live_bilibili, "account_path", return_value=credential), patch.object(live_bilibili, "ready", return_value=True):
                assert not live.queue_upload(cid, automatic=True), "unvalidated scores must not auto-publish"
                with patch.object(live_calibration, "active_rubric", return_value={"validated": True}):
                    assert not live.queue_upload(cid, automatic=True), 'manual clips require explicit publishing'
                    # Model-created topic clips have no manual-review flag.
                    core.q('UPDATE live_clips SET needs_review=0,scores=?,score=85 WHERE id=?',
                           (json.dumps({'topic': 85, 'traffic': 85, 'fun': 85, 'completeness': 90}), cid))
                    assert live.queue_upload(cid, automatic=True)
                    assert not live.queue_upload(second, automatic=True), "daily limit counts reserved uploads too"
            core.q("DELETE FROM live_clips WHERE id=?", (second,))
            # Execution rechecks a previously queued automatic publication.
            with patch.object(live_calibration, 'active_rubric', return_value={}), patch.object(live_bilibili, 'publish') as submit:
                assert live.upload({'payload': {**payload, 'automatic': True}}, lambda _: True)['paused']
                assert not submit.called
            # Posting is idempotent when a network failure may have happened after submission.
            core.q("UPDATE live_clips SET state='awaiting_upload' WHERE id=?", (cid,))
            with patch.object(live_bilibili, "publish", side_effect=RuntimeError("lost response")) as submit:
                assert live.upload({"payload": payload}, lambda _: True)["uncertain"]
                assert "skipped" in live.upload({"payload": payload}, lambda _: True)
                assert submit.call_count == 1
            assert client.post(f"/api/live/clips/{cid}/retry-upload", json={}).status_code == 400
            # Time edits queue v2 while keeping v1's file; later title edits must not promote that stale file.
            core.q("UPDATE live_clips SET state='ready' WHERE id=?", (cid,))
            edit = {"start": .2, "end": 5.8, "title": "调整时间", "tags": ["直播切片"]}
            assert client.post(f"/api/live/clips/{cid}", json=edit).status_code == 200
            for pending_state in ("queued", "rendering"):
                core.q("UPDATE live_clips SET state=? WHERE id=?", (pending_state, cid))
                assert client.post(f"/api/live/clips/{cid}", json={**edit, "title": "只修改标题"}).status_code == 200
                pending = core.q("SELECT * FROM live_clips WHERE id=?", (cid,), one=True)
                assert pending["state"] == pending_state and pending["version"] == 2 and pending["render_version"] == 1
                assert live.clip_dict(pending)["preview"] == ""
                # A failed v1 task must not poison the pending v2, regardless of queued/rendering state.
                core.q("UPDATE tasks SET state='failed' WHERE kind='live_render' AND json_extract(payload,'$.clip')=? "
                       "AND json_extract(payload,'$.version')=1", (cid,))
                live_recorder.schedule_analysis()
                assert core.q("SELECT state FROM live_clips WHERE id=?", (cid,), one=True)["state"] == pending_state
            try:
                live.render({"payload": payload}, lambda _: True)
            except ValueError:
                pass
            else:
                raise AssertionError("stale render accepted")
            core.q("UPDATE tasks SET state='failed' WHERE kind='live_render' AND json_extract(payload,'$.clip')=? "
                   "AND json_extract(payload,'$.version')=2", (cid,))
            live_recorder.schedule_analysis()
            assert core.q("SELECT state FROM live_clips WHERE id=?", (cid,), one=True)["state"] == "failed"
            assert client.post(f"/api/live/clips/{cid}/render", json={}).status_code == 200
            live_recorder.schedule_analysis()
            assert core.q("SELECT state FROM live_clips WHERE id=?", (cid,), one=True)["state"] == "queued"
            current_payload = {"clip": cid, "version": 2}
            live.after_render({"payload": json.dumps(current_payload)}, live.render({"payload": current_payload}, lambda _: True))
            assert client.post(f"/api/live/clips/{cid}", json={**edit, "title": "新版本成片"}).status_code == 200
            finished = core.q("SELECT * FROM live_clips WHERE id=?", (cid,), one=True)
            assert finished["state"] == "ready" and finished["render_version"] == finished["version"] == 2
            assert client.get(f"/api/live/clips/{cid}/media", headers={"Range": "bytes=0-99"}).status_code == 206
            # The topic lane continues when highlights are empty.
            core.q("UPDATE live_recordings SET duration=60, offset=seq*60, segments=? WHERE session_key=?",
                   (json.dumps([[0, 20, "讨论开店成本和选址。"]]), key))
            with patch.object(live.llm, "llm_json", side_effect=[{"highlights": [], "topic_changes": [
                    {"at": 60, "next_title": "店铺选址", "previous_summary": "先算成本再开店"}], "current_topic": "店铺选址"},
                    {"boundaries": [{"index": 0, "previous_complete": True, "next_independent": True,
                                     "confidence": .95, "reason": "成本咨询结束，下一位观众开始咨询另一家店的选址"}]}]):
                live.analyze({"payload": {"session": key}}, lambda _: True)
            assert core.q("SELECT id FROM live_clips WHERE session_key=? AND lane='topic' AND end=60", (key,), one=True)
            assert not core.q("SELECT id FROM live_clips WHERE session_key=? AND lane='highlight'", (key,), one=True)
            with core.app.test_request_context("/"):
                g.owner = owner
                assert live.owned("live_sources", source["id"])
        finally:
            for token in [k for k, v in live.LOGINS.items() if v["owner"] == owner]:
                live.LOGINS.pop(token, None)
            for table in ("live_clips", "live_recordings", "live_sessions", "live_sources", "live_accounts", "live_references", "live_rubrics"):
                core.q(f"DELETE FROM {table} WHERE owner=?", (owner,))
            core.q("DELETE FROM tasks WHERE kind LIKE 'live_%'")
            core.q("DELETE FROM devices WHERE id=?", (device,))
            core.q("DELETE FROM users WHERE name='live-regression'")


def run(check):
    check("live curator: completeness, confidence, timestamps and settings gates", curator_gates)
    check("live source links: b23 shares, mobile rooms, video author, safe redirects", short_live_links)
    check("live calibration: recent, diverse, balanced high/control samples", sampling)
    check("live calibration: content independence, mature views, permutation gate, finite scores", validation_independence)
    check('live calibration: evidence, transcript coverage, batch isolation and rejection reasons', calibration_candidates)
    check('live references: AI aliases require exact UID/room and verbatim cited evidence', ai_alias_evidence)
    check('live references: aliases, own/history material, transcript verification, bounded queue and split isolation', reference_acquisition)
    check('live calibration: publisher-matched comparisons, exact paired test and frozen scores', matched_pair_metrics)
    check('live calibration: train-only selection, held-out activation and failed replacement', calibration_refine_roundtrip)
    check('live publication: original streamer and canonical room survive edited summaries', publication_attribution)
    check("live events: compressed packets, 30-minute fallback, immediate wakeups, reconnects and capacity", event_monitoring)
    check("live recorder: permanent ASR gaps and unfinished-topic retention", recorder_lifecycle)
    check('live recorder: clean exits, upstream live state, fast reconnection and short-failure backoff', recorder_connection_end)
    check("live workspace: ownership, cross-segment rendering, two lanes, safe upload retries", workspace_roundtrip)
