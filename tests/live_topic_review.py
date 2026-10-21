"""Topic review uses actual transcript scores and never edits a remote publication."""
import json
import time
import tempfile
from pathlib import Path
from unittest.mock import patch

from shiguang import core, live, live_editorial
from shiguang.live_curator import confirmed_boundaries


def run(check):
    def complete_units():
        owner, key = 'user:complete-units-test', 'complete-units-test'
        core.q("INSERT INTO live_sources(owner,room_id,url,name,settings,enabled) VALUES (?,88442,'https://live.bilibili.com/88442','边界测试',?,0)",
               (owner, json.dumps({'topic_minutes': 2, 'topic_unit': 'story'})))
        source = core.q('SELECT * FROM live_sources WHERE owner=?', (owner,), one=True)
        sid = source['id']
        core.q("INSERT INTO live_sessions(key,source_id,owner,title,started,cursor,topic_start,topic_title,topic_summary) "
               "VALUES (?,?,?,'咨询直播',?,1440,0,'同一案子的完整咨询','案情已说明，正在分析证据，尚未答复完')", (key,sid,owner,time.time()))
        for seq in range(28):
            core.q("INSERT INTO live_recordings(owner,source_id,session_key,seq,offset,duration,state,path,segments) "
                   "VALUES (?,?,?,?,?,60,'ready','/unused/complete-units.mkv',?)",
                   (owner,sid,key,seq,seq*60,json.dumps([[0,30,'补充同一案子的证据，继续回答当事人的追问。'],[30,60,'继续讨论。']])))
        candidate = {'at': 1500, 'next_title': '证据与责任分析', 'previous_summary': '同一案子的背景'}
        result = {'highlights': [], 'topic_changes': [candidate], 'current_topic': '证据与责任分析',
                  'current_summary': '仍在回答同一当事人的追问'}
        def analyze(responses):
            with patch.object(live.llm, 'llm_json', side_effect=responses) as model, patch.object(live, 'queue_render'):
                live.analyze({'payload': {'session':key}}, lambda _: True)
                return model
        try:
            # Longer than both the old default limit and this source's explicit two-minute limit.
            model = analyze([result, {'boundaries':[{'index':0, 'previous_complete':False, 'next_independent':False,
                                                     'confidence':.95, 'reason':'仍为同一案子的追问'}]}])
            assert not core.q('SELECT id FROM live_clips WHERE session_key=?', (key,))
            assert core.q('SELECT topic_start FROM live_sessions WHERE key=?', (key,), one=True)['topic_start'] == 0
            assert json.loads(model.call_args.args[1])['topic_unit'] == 'story'
            # An unconfirmed switch near the window edge is reconsidered after later dialogue arrives.
            core.q('UPDATE live_sessions SET cursor=1440 WHERE key=?', (key,))
            late = {**result, 'topic_changes':[{**candidate, 'at':1550}]}
            assert analyze([late]).call_count == 1
            assert not core.q('SELECT id FROM live_clips WHERE session_key=?', (key,))
            core.q('UPDATE live_sources SET settings=? WHERE id=?', (json.dumps({'topic_unit':'topic'}), sid))
            switched = {**late, 'current_topic':'另一个聊天话题'}
            approval = {'boundaries':[{'index':0,'previous_complete':True,'next_independent':True,
                                       'confidence':.95,'reason':'已持续转到独立话题'}]}
            # 1550 is before the saved cursor 1560, but has not yet been cut.
            model = analyze([switched, approval])
            assert json.loads(model.call_args.args[1])['topic_unit'] == 'topic'
            clips = core.q('SELECT start,end FROM live_clips WHERE session_key=?', (key,))
            assert [(c['start'],c['end']) for c in clips] == [(0,1550)]
            # On recording end, keep the whole remaining discussion, without a timed continuation.
            core.q("UPDATE live_sessions SET status='ended',cursor=1560 WHERE key=?", (key,))
            analyze([{**result, 'topic_changes':[]}])
            clips = core.q('SELECT start,end FROM live_clips WHERE session_key=? ORDER BY start', (key,))
            assert [(c['start'],c['end']) for c in clips] == [(0,1550),(1550,1680)]
            for bad in (None, {}, {'boundaries':None}, {'boundaries':[True]},
                        {'boundaries':[{**approval['boundaries'][0], 'index':True}]},
                        {'boundaries':[{**approval['boundaries'][0], 'previous_complete':'true'}]}):
                assert confirmed_boundaries(bad,[candidate]) == []
            for unit in ('auto','story','topic'):
                assert live.settings({'topic_unit':unit})['topic_unit'] == unit
            try:
                live.settings({'topic_unit':'chapters'})
                raise AssertionError('invalid topic unit accepted')
            except ValueError:
                pass
        finally:
            core.q("DELETE FROM tasks WHERE kind IN ('live_render','live_score') AND json_extract(payload,'$.clip') IN (SELECT id FROM live_clips WHERE owner=?)", (owner,))
            for table in ('live_clips','live_recordings','live_sessions','live_sources'):
                core.q(f'DELETE FROM {table} WHERE owner=?', (owner,))
    check('live segmentation: long complete stories, reviewed switches, delayed boundaries, chat units and final flush', complete_units)

    def topic_scores():
        assert live_editorial.normalized_scores({'topic': 90}) is None
        assert live_editorial.normalized_scores({'topic': True, 'traffic': 80, 'fun': 80, 'completeness': 90}) is None
        assert live_editorial.normalized_scores({'topic': '85', 'traffic': '80', 'fun': '75', 'completeness': '90'})[0] == 80.2
        assert live_editorial.normalized_scores({'topic': 'NaN', 'traffic': 80, 'fun': 75, 'completeness': 90}) is None
        owner = 'user:topic-review-test'
        core.q("INSERT INTO live_sources(owner,room_id,uid,url,name,enabled,settings) VALUES (?,88441,1,'https://live.bilibili.com/88441','测试主播',0,'{}')", (owner,))
        sid = core.q('SELECT MAX(id) id FROM live_sources', one=True)['id']
        key = 'topic-review-test'
        core.q("INSERT INTO live_sessions(key,source_id,owner,title,started) VALUES (?,?,?,'实际咨询',?)", (key, sid, owner, time.time()))
        core.q("INSERT INTO live_recordings(owner,source_id,session_key,seq,offset,duration,state,path,segments) VALUES (?,?,?,0,0,60,'ready','/unused/topic-review.mkv',?)",
               (owner, sid, key, json.dumps([[0, 60, '观众咨询AI知识号起号，模型比较只是背景，主播建议先做内容再考虑变现。']])) )
        core.q("INSERT INTO live_clips(owner,source_id,session_key,lane,start,end,title,state) VALUES (?,?,?,'topic',0,60,'咨询用AI做内容并比较模型，随后计划以知识讲解形式起号，准备长期投入。','ready')", (owner, sid, key))
        cid = core.q('SELECT MAX(id) id FROM live_clips', one=True)['id']
        payload = {'payload': {'clip': cid, 'version': 1}}
        result = {'kind': 'content', 'title': '测试主播：AI知识号怎么起步？', 'summary': '先做内容',
                  'scores': {'topic': 85, 'traffic': 80, 'fun': 75, 'completeness': 90}, 'reason': '完整咨询有具体选题与结论'}
        try:
            with patch.object(live_editorial.llm, 'llm_json', return_value=result) as model:
                rated = live_editorial._score_topic(payload, lambda _: True)
                assert rated['scored'] and rated['score'] == 80.2
                supplied = json.loads(model.call_args.args[1])
                assert '模型比较只是背景' in supplied['transcript'][0][2]
            row = core.q('SELECT * FROM live_clips WHERE id=?', (cid,), one=True)
            assert row['title'] == result['title'] and json.loads(row['scores'])['completeness'] == 90
            with patch.object(live_editorial.llm, 'llm_json', return_value={**result, 'kind': 'advertisement'}):
                live_editorial._score_topic(payload, lambda _: True)
            row = core.q('SELECT * FROM live_clips WHERE id=?', (cid,), one=True)
            assert row['state'] == 'rejected' and row['content_kind'] == 'advertisement'
            core.q("UPDATE live_clips SET state='ready' WHERE id=?", (cid,))
            with patch.object(live_editorial.llm, 'llm_json', return_value={'kind': 'advertisement'}):
                assert live_editorial._score_topic(payload, lambda _: True)['advertisement']
            source = core.q('SELECT * FROM live_sources WHERE id=?', (sid,), one=True)
            assert live.create_clip(owner, source, key, 'topic', {'content_kind': 'advertisement'}) is None
            core.q("UPDATE live_clips SET state='submitted',content_kind='content',title='已发布的原始标题' WHERE id=?", (cid,))
            with patch.object(live_editorial.llm, 'llm_json', return_value=result):
                live_editorial._score_topic(payload, lambda _: True)
            row = core.q('SELECT * FROM live_clips WHERE id=?', (cid,), one=True)
            assert row['state'] == 'submitted' and row['title'] == '已发布的原始标题'
            def edited_during_scoring(*args, **kwargs):
                core.q("UPDATE live_clips SET version=2,scores='{}',score=0 WHERE id=?", (cid,))
                return result
            with patch.object(live_editorial.llm, 'llm_json', side_effect=edited_during_scoring):
                assert not live_editorial._score_topic(payload, lambda _: True)['scored']
            assert core.q('SELECT scores FROM live_clips WHERE id=?', (cid,), one=True)['scores'] == '{}'
        finally:
            for table in ('live_clips', 'live_recordings', 'live_sessions', 'live_sources'):
                core.q(f'DELETE FROM {table} WHERE owner=?', (owner,))
    check('live topics: actual scores, concise titles, ads excluded, published metadata protected and stale scores rejected', topic_scores)

    def draft_retention():
        owner, now = 'user:retention-test', 100000
        ids = {}
        def add(label, score=74, age=13, state='ready', scores=None, **fields):
            ratings = scores if scores is not None else {k: score for k in live_editorial.DIMENSIONS}
            core._write("INSERT INTO live_clips(owner,source_id,session_key,lane,start,end,title,state,score,scores,created) "
                        "VALUES (?,0,'retention-test','highlight',0,60,?,?,?,?,?)",
                        (owner, label, state, score, json.dumps(ratings), now-age*3600))
            cid = core.q('SELECT MAX(id) id FROM live_clips', one=True)['id'];ids[label] = cid
            for key, value in fields.items():
                core._write(f'UPDATE live_clips SET {key}=? WHERE id=?', (value, cid))
            return cid
        try:
            old = add('old');boundary = add('boundary', age=12)
            add('fresh', age=11.99);add('75', score=75);add('unrated', score=0, scores={})
            for state in ('submitted','uncertain','uploading','awaiting_upload','rendering','queued'):
                add(state, state=state)
            add('previous-attempt', submit_started=now-3600);add('posted-id', bvid='BVtest')
            parent = add('merged-parent');add('merged-child', state='submitted', merged_into=parent)
            busy = add('busy')
            core._write("INSERT INTO tasks(kind,target,state,payload) VALUES ('live_score','retention-test','running',?)", (json.dumps({'clip':busy}),))
            with tempfile.TemporaryDirectory() as directory, patch.object(live, 'ROOT', Path(directory)):
                assets = Path(directory)/'clips';assets.mkdir()
                old_file=assets/f'{old}-v1.mp4';old_file.write_bytes(b'old')
                old_cover=assets/f'{old}-v2-cover.jpg';old_cover.write_bytes(b'cover')
                kept=assets/f'{ids["75"]}-v1.mp4';kept.write_bytes(b'keep')
                recording=Path(directory)/'recordings';recording.mkdir();raw=recording/'original.mkv';raw.write_bytes(b'raw')
                assert set(live_editorial.cleanup_unposted(now)) == {old,boundary}
                assert not old_file.exists() and not old_cover.exists() and kept.exists() and raw.exists()
                assert live_editorial.cleanup_unposted(now) == []
            assert len(core.q('SELECT id FROM live_clips WHERE owner=?', (owner,))) == len(ids)-2
        finally:
            core._write("DELETE FROM tasks WHERE target='retention-test'")
            core._write('DELETE FROM live_clips WHERE owner=?', (owner,))
    check('live retention: 75-point and 12-hour boundaries, unrated and publication safety, generated files only', draft_retention)
