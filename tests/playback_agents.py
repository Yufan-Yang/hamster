"""Evidence-backed playback agents with isolated media and real queue leases."""
import importlib.util
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('playback_fixtures',Path(__file__).with_name('steward.py'))
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)
from shiguang import board,core,library,llm,media_quality as quality,resume_recap as recap,steward,web  # noqa:E402


class PlaybackTests(unittest.TestCase):
    setUp=fixtures.StewardTests.setUp
    client=fixtures.StewardTests.client
    collection=fixtures.StewardTests.collection
    job=fixtures.StewardTests.job
    media=fixtures.StewardTests.media
    row=fixtures.StewardTests.row

    def seed(self,owner='user:alice',part=0,checkpoint=300,age=4000):
        jid,files=self.media(owner=owner,parts=part+1)
        a=json.loads(self.row(jid)['analysis']);a.update(folder='Tutorials',chapters={'0':[{'t':0,'title':'Start'},{'t':900,'title':'Invalid tail'}]},key_points=['Useful point'],point_times=[900])
        core.q('UPDATE jobs SET analysis=? WHERE id=?',(json.dumps(a),jid))
        for path in files:
            core.q('INSERT INTO probes(path,mtime,duration,vcodec) VALUES(?,?,600,?)',(str(path),path.stat().st_mtime,'h264'))
            def stamp(sec):return f'00:{sec//60:02}:{sec%60:02},000'
            path.with_suffix('.zh.srt').write_text('\n\n'.join(f'{i}\n{stamp(i*10)} --> {stamp(i*10+9)}\nThis is useful source information number {i}, with a concrete explanation.' for i in range(40)))
        core.q('INSERT INTO watch(owner,job_id,part,pos,dur,done,updated) VALUES(?,?,?,?,600,0,?)',(owner,jid,part,checkpoint,time.time()-age))
        core.kv_set(steward.POLICY_PREFIX+owner,{**steward.DEFAULT_POLICY,'automatic':True})
        return jid,files

    def work(self):
        task=board.claim_task('recap-test',['resume_recap']);self.assertIsNotNone(task)
        state=board.run_claimed(task,'recap-test')
        return state,task

    def model(self,system,user,*args,**kwargs):
        data=json.loads(user);items=data['excerpts']
        return {'quotes':[{'source_id':items[0]['id'],'quote':items[0]['text'][:100]}]}

    def test_quality_filters_bad_jumps_without_editing_any_metadata(self):
        jid,_=self.seed();before=self.row(jid)['analysis']
        r=self.alice.get(f'/api/points/{jid}')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['times'],[None])
        self.assertEqual(r.json['chapters']['0'],[{'t':0,'title':'Start'}])
        self.assertEqual(r.json['unavailable']['0'][0]['title'],'Invalid tail')
        self.assertEqual(r.json['quality']['rejected'],2)
        self.assertEqual(r.headers['Cache-Control'],'no-store')
        quality.observe(self.row(jid));self.assertEqual(self.row(jid)['analysis'],before)

    def test_quality_uses_fresh_duration_without_running_media_tools(self):
        jid,files=self.seed()
        with patch.object(library,'ffprobe',side_effect=AssertionError('no foreground decoding')):
            self.assertEqual(quality.review(self.row(jid))['quality']['rejected'],2)
        os.utime(files[0],(time.time()+5,time.time()+5))
        result=quality.review(self.row(jid))
        self.assertEqual(result['quality']['status'],'unverified')
        self.assertEqual(result['quality']['rejected'],0)

    def test_quality_rejects_nan_boolean_strings_and_normalizes_valid_order(self):
        jid,_=self.seed();a=json.loads(self.row(jid)['analysis'])
        a['chapters']={'0':[{'t':50,'title':'valid'},{'t':20,'title':'wrong order'},{'t':True,'title':'bool'},
                            {'t':'0);evil()','title':'string'},{'t':float('nan'),'title':'nan'}]}
        a['point_times']=[-1,False,float('inf'),20]
        core.q('UPDATE jobs SET analysis=? WHERE id=?',(json.dumps(a),jid))
        result=quality.review(self.row(jid))
        self.assertEqual([c['t'] for c in result['chapters']['0']],[20,50])
        self.assertEqual(result['times'],[None,None,None,{'part':0,'t':20}])

    def test_quality_checks_hidden_permissions_and_expired_tokens(self):
        jid,_=self.seed()
        self.assertEqual(self.bob.get(f'/api/points/{jid}').status_code,404)
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)',(self.owner,'[]',json.dumps([jid])))
        self.assertEqual(self.alice.get(f'/api/points/{jid}').status_code,404)
        self.assertEqual(self.alice.get(f'/api/points/{jid}',headers={'X-Privacy-Token':'expired'}).status_code,403)

    def test_quality_rechecks_permission_after_inspection(self):
        jid,_=self.seed();original=quality.review
        def changed(*args,**kwargs):
            result=original(*args,**kwargs);core.q('UPDATE jobs SET owner=? WHERE id=?',('user:bob',jid));return result
        with patch.object(quality,'review',side_effect=changed):
            r=self.alice.get(f'/api/points/{jid}')
        self.assertEqual(r.status_code,409)

    def test_snapshot_excludes_future_and_crossing_cues(self):
        jid,files=self.seed(checkpoint=120)
        files[0].with_suffix('.zh.srt').write_text('1\n00:01:00,000 --> 00:01:20,000\nAlready before the checkpoint with enough words.\n\n2\n00:01:30,000 --> 00:02:10,000\nCROSSING_SECRET\n\n3\n00:02:20,000 --> 00:02:30,000\nFUTURE_SECRET\n')
        revision,sources,blocks=recap.snapshot(self.row(jid),0,120)
        self.assertTrue(revision and sources)
        self.assertNotIn('SECRET',json.dumps(blocks))
        self.assertTrue(all(x['end']<=120 for x in blocks))

    def test_review_rejects_fabricated_future_duplicate_or_translated_quotes(self):
        evidence=[{'id':1,'t':10,'end':20,'text':'This is the actual verified source sentence.'}]
        for raw in [{'quotes':[{'source_id':2,'quote':evidence[0]['text']}]},
                    {'quotes':[{'source_id':1,'quote':'A completely invented explanation.'}]},
                    {'quotes':[{'source_id':1,'quote':evidence[0]['text']}]*2}]:
            with self.assertRaises(ValueError):recap.review(raw,evidence,30)
        with self.assertRaises(ValueError):recap.review({'quotes':[{'source_id':1,'quote':evidence[0]['text']}]},evidence,15)

    def test_real_queue_generates_reviewed_cache_without_modifying_playback(self):
        jid,_=self.seed();before=dict(core.q('SELECT * FROM watch WHERE job_id=?',(jid,),one=True))
        first=self.alice.post(f'/api/recap/{jid}',json={});second=self.alice.post(f'/api/recap/{jid}',json={})
        self.assertEqual(first.json['id'],second.json['id'])
        with patch.object(llm,'llm_json',side_effect=self.model) as model:
            self.assertEqual(self.work()[0],'done');self.assertEqual(model.call_count,1)
        r=self.alice.get(f"/api/recap/{jid}?id={first.json['id']}")
        self.assertEqual(r.json['state'],'ready');self.assertTrue(r.json['review']['verbatim'])
        self.assertTrue(all(x['end']<=300 for x in r.json['quotes']))
        self.assertEqual(dict(core.q('SELECT * FROM watch WHERE job_id=?',(jid,),one=True)),before)
        self.assertEqual(r.headers['Cache-Control'],'no-store')

    def test_model_only_receives_bounded_subtitles_before_checkpoint(self):
        jid,_=self.seed();a=json.loads(self.row(jid)['analysis']);a['summary']='FUTURE_FULL_SUMMARY_SECRET'
        core.q('UPDATE jobs SET analysis=? WHERE id=?',(json.dumps(a),jid));recap.prepare(self.row(jid))
        def model(system,user,*args,**kwargs):
            self.assertNotIn('FUTURE_FULL_SUMMARY_SECRET',user)
            self.assertTrue(all(x['end']<=300 for x in json.loads(user)['excerpts']))
            return self.model(system,user,*args,**kwargs)
        with patch.object(llm,'llm_json',side_effect=model):self.work()

    def test_invalid_model_output_does_not_persist_any_quotes(self):
        jid,_=self.seed();record=recap.prepare(self.row(jid))
        with patch.object(llm,'llm_json',return_value={'quotes':[{'source_id':999,'quote':'invented secret content'}]}):self.work()
        r=self.alice.get(f"/api/recap/{jid}?id={record['id']}")
        self.assertEqual(r.json['state'],'rejected');self.assertNotIn('quotes',r.json)

    def test_movie_music_essential_recent_and_completed_do_not_auto_generate(self):
        jid,_=self.seed()
        for library_name in ('TV','Movies','Music'):
            a=json.loads(self.row(jid)['analysis']);a['library']=library_name
            core.q('UPDATE jobs SET analysis=? WHERE id=?',(json.dumps(a),jid))
            self.assertEqual(recap.prepare(self.row(jid))['state'],'unavailable')
        a['library']='Videos';core.q('UPDATE jobs SET analysis=? WHERE id=?',(json.dumps(a),jid))
        core.kv_set(steward.POLICY_PREFIX+self.owner,{**steward.DEFAULT_POLICY,'automatic':True,'profile':'essential'})
        self.assertEqual(recap.prepare(self.row(jid))['state'],'unavailable')
        core.kv_set(steward.POLICY_PREFIX+self.owner,{**steward.DEFAULT_POLICY,'automatic':True})
        core.q('UPDATE watch SET updated=? WHERE job_id=?',(time.time(),jid))
        self.assertEqual(recap.prepare(self.row(jid))['state'],'unavailable')
        core.q('UPDATE watch SET done=1,updated=0 WHERE job_id=?',(jid,))
        self.assertEqual(recap.prepare(self.row(jid))['state'],'unavailable')

    def test_policy_pause_prevents_queued_model_call(self):
        jid,_=self.seed();recap.prepare(self.row(jid))
        core.kv_set(steward.POLICY_PREFIX+self.owner,steward.DEFAULT_POLICY)
        with patch.object(llm,'llm_json') as model:self.work();model.assert_not_called()

    def test_source_changed_during_model_and_progress_clear_discard_result(self):
        for change in ('subtitle','clear','owner','rewind','part'):
            with self.subTest(change=change):
                jid,files=self.seed();record=recap.prepare(self.row(jid),manual=True)
                def changed(*args,**kwargs):
                    if change=='subtitle':files[0].with_suffix('.zh.srt').write_text('changed source')
                    elif change=='clear':self.alice.delete(f'/api/watch/{jid}')
                    elif change=='owner':core.q('UPDATE jobs SET owner=? WHERE id=?',('user:bob',jid))
                    elif change=='rewind':core.q('UPDATE watch SET pos=0 WHERE job_id=?',(jid,))
                    else:core.q('UPDATE watch SET part=1 WHERE job_id=?',(jid,))
                    return self.model(*args,**kwargs)
                with patch.object(llm,'llm_json',side_effect=changed):self.work()
                r=self.alice.get(f"/api/recap/{jid}?id={record['id']}")
                self.assertTrue(r.status_code in (404,409) or r.json.get('state')!='ready')

    def test_checkpoint_does_not_follow_continuing_playback(self):
        jid,_=self.seed();record=recap.prepare(self.row(jid))
        core.q('UPDATE watch SET pos=400,updated=? WHERE job_id=?',(time.time(),jid))
        with patch.object(llm,'llm_json',side_effect=self.model):self.work()
        r=self.alice.get(f"/api/recap/{jid}?id={record['id']}")
        self.assertEqual(r.json['checkpoint'],300)
        self.assertTrue(all(q['end']<=300 for q in r.json['quotes']))

    def test_fixed_client_checkpoint_and_future_request_rejection(self):
        jid,_=self.seed(checkpoint=350)
        r=self.alice.post(f'/api/recap/{jid}',json={'manual':False,'part':0,'checkpoint':300})
        self.assertEqual(r.json['checkpoint'],300)
        with patch.object(llm,'llm_json',side_effect=self.model):self.work()
        self.assertEqual(self.alice.get(f"/api/recap/{jid}?id={r.json['id']}").json['state'],'ready')
        self.assertEqual(self.alice.post(f'/api/recap/{jid}',json={'part':0,'checkpoint':999}).json['state'],'changed')

    def test_equivalent_integer_and_float_checkpoints_reuse_reviewed_result(self):
        jid,_=self.seed(checkpoint=180)
        first=recap.prepare(self.row(jid))
        with patch.object(llm,'llm_json',side_effect=self.model):self.work()
        second=self.alice.post(f'/api/recap/{jid}',json={'part':0,'checkpoint':180}).json
        self.assertEqual(first['id'],second['id']);self.assertEqual(second['state'],'ready')
        self.assertEqual(recap._usage(self.owner)['total'],1)

    def test_limits_include_failures_manual_requests_and_cleared_artifacts(self):
        for i in range(recap.AUTO_DAILY):
            jid,_=self.seed();self.assertEqual(recap.prepare(self.row(jid))['state'],'pending')
            self.alice.delete(f'/api/watch/{jid}')
        jid,_=self.seed();self.assertEqual(recap.prepare(self.row(jid))['state'],'budget')
        self.assertEqual(recap.prepare(self.row(jid),manual=True)['state'],'pending')
        self.assertEqual(recap._usage(self.owner)['total'],4)

    def test_foreign_admin_and_hidden_scope_cannot_read_personal_recap(self):
        jid,_=self.seed();record=recap.prepare(self.row(jid))
        for client in (self.bob,self.admin):
            self.assertEqual(client.get(f"/api/recap/{jid}?id={record['id']}").status_code,404)
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)',(self.owner,'[]',json.dumps([jid])))
        self.assertEqual(self.alice.get(f"/api/recap/{jid}?id={record['id']}").status_code,404)
        self.assertEqual(self.alice.post(f'/api/recap/{jid}',json={}).status_code,404)

    def test_clear_and_manual_retry_have_new_fenced_generations(self):
        jid,_=self.seed();record=recap.prepare(self.row(jid))
        with patch.object(llm,'llm_json',return_value={'quotes':'bad'}):self.work()
        self.assertEqual(recap.prepare(self.row(jid))['state'],'rejected')
        self.assertEqual(recap.prepare(self.row(jid),manual=True)['state'],'pending')
        with patch.object(llm,'llm_json',side_effect=self.model):self.work()
        self.assertEqual(recap._usage(self.owner)['total'],2)
        self.assertEqual(self.alice.get(f"/api/recap/{jid}?id={record['id']}").json['state'],'ready')

    def test_private_recap_requires_live_unlock_even_after_generation(self):
        jid,_=self.seed();core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)',(self.owner,'[]',json.dumps([jid])))
        token={'owner':self.owner,'reveal':True,'expires':time.time()+60}
        with patch.dict(web.privacy_tokens,{'recap-token':token}):
            r=self.alice.post(f'/api/recap/{jid}',json={},headers={'X-Privacy-Token':'recap-token'})
            self.assertEqual(r.status_code,202)
            with patch.object(llm,'llm_json',side_effect=self.model):self.work()
            self.assertEqual(self.alice.get(f"/api/recap/{jid}?id={r.json['id']}").status_code,404)
            token['expires']=time.time()-1
            self.assertEqual(self.alice.get(f"/api/recap/{jid}?id={r.json['id']}",headers={'X-Privacy-Token':'recap-token'}).status_code,403)

    def test_preserved_subtitle_size_and_mtime_still_invalidates_recap(self):
        jid,files=self.seed();record=recap.prepare(self.row(jid))
        with patch.object(llm,'llm_json',side_effect=self.model):self.work()
        path=files[0].with_suffix('.zh.srt');stat=path.stat()
        path.write_bytes(path.read_bytes().replace(b'useful',b'boring'))
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        self.assertEqual(self.alice.get(f"/api/recap/{jid}?id={record['id']}").status_code,409)

    def test_provider_unavailable_is_quiet_and_does_not_retry_automatically(self):
        jid,_=self.seed();record=recap.prepare(self.row(jid))
        with patch.object(llm,'llm_json',side_effect=RuntimeError('provider unavailable')) as model:
            self.assertEqual(self.work()[0],'done');self.assertEqual(model.call_count,1)
        self.assertEqual(recap.prepare(self.row(jid))['state'],'unavailable')
        self.assertEqual(core.q('SELECT count(*) FROM incidents',one=True)[0],0)
        self.assertEqual(self.alice.get(f"/api/recap/{jid}?id={record['id']}").json['state'],'unavailable')

    def test_total_manual_budget_and_empty_results_are_not_padded(self):
        for i in range(recap.TOTAL_DAILY):
            jid,_=self.seed();self.assertEqual(recap.prepare(self.row(jid),manual=True)['state'],'pending')
            with patch.object(llm,'llm_json',return_value={'quotes':[]}):self.work()
            self.assertEqual(recap.prepare(self.row(jid))['state'],'empty')
        jid,_=self.seed()
        self.assertEqual(recap.prepare(self.row(jid),manual=True)['state'],'budget')
        self.assertEqual(recap._usage(self.owner)['total'],recap.TOTAL_DAILY)

    def test_pausing_keeps_existing_reviewed_cache_available(self):
        jid,_=self.seed();record=recap.prepare(self.row(jid))
        with patch.object(llm,'llm_json',side_effect=self.model):self.work()
        core.kv_set(steward.POLICY_PREFIX+self.owner,steward.DEFAULT_POLICY)
        self.assertEqual(recap.prepare(self.row(jid))['id'],record['id'])
        self.assertEqual(recap.prepare(self.row(jid))['state'],'ready')

    def test_missing_media_cannot_be_reported_as_verified(self):
        jid,_=self.seed();core.q("UPDATE jobs SET files='[]',analysis='{}' WHERE id=?",(jid,))
        self.assertEqual(quality.review(self.row(jid))['quality']['status'],'unverified')

    def test_duplicate_chapters_are_merged_without_false_error_or_lost_titles(self):
        jid,_=self.seed();a=json.loads(self.row(jid)['analysis'])
        a.update(point_times=[],chapters={'0':[{'t':50,'title':'One'},{'t':50,'title':'Two'},{'t':20,'title':'Earlier'}]})
        core.q('UPDATE jobs SET analysis=? WHERE id=?',(json.dumps(a),jid));before=self.row(jid)['analysis']
        result=quality.review(self.row(jid))
        self.assertEqual(result['quality']['rejected'],0)
        self.assertEqual(result['quality']['merged_duplicates'],1)
        self.assertEqual(result['chapters']['0'],[{'t':20,'title':'Earlier'},{'t':50,'title':'One / Two'}])
        self.assertEqual(self.row(jid)['analysis'],before)

    def test_actual_model_router_formats_the_prompt_and_saves_reviewed_output(self):
        jid,_=self.seed();record=recap.prepare(self.row(jid))
        def codex(system,user,*args,**kwargs):
            self.assertIn('{"quotes":[{"source_id":integer',system)
            self.assertNotIn('{{',system)
            return self.model(system,user,*args,**kwargs)
        with patch.object(llm,'codex_ready',return_value=True),patch.object(llm,'codex_json',side_effect=codex) as model:
            self.assertEqual(self.work()[0],'done');self.assertEqual(model.call_count,1)
        self.assertEqual(self.alice.get(f"/api/recap/{jid}?id={record['id']}").json['state'],'ready')


if __name__=='__main__':unittest.main()
