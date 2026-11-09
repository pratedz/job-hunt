"""Recovery searches use fake network, temporary SQLite and an explicit clock."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from _fake import FakeClock, FakeTransport, search_page, fixture
from _util import CFG, profile, memory_conn
from jobhunt import cli, coverage, pipeline, store, score as score_mod
from jobhunt.llm import FakeLLM
from _util import entry, reply
from jobhunt.http import PoliteFetcher

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def config(**kw):
    data = profile(**{'coverage.enabled': True,
        'coverage.targets': [{'name': n, 'queries': [n]} for n in ('A','B','C','D')],
        'coverage.linkedin_requests': 1, 'coverage.jobsdb_requests': 1,
        'sources.linkedin': True, 'sources.jobsdb': True, 'sources.companies': False,
        'sources.jobthai': False, 'companies': [],
        'search.linkedin_queries': [['role','Bangkok']], 'search.keywords': ['role'],
        'search.linkedin_max_pages': 1, 'search.max_pages': 1, **kw})
    return replace(CFG, profile=data)


def network(cfg, routes):
    transport, clock = FakeTransport(routes), FakeClock()
    return PoliteFetcher(cfg.profile, transport=transport, now=clock.now, sleep=clock.sleep), transport


class Rotation(unittest.TestCase):
    def test_failed_first_target_does_not_starve_later_targets_across_reopen(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as root:
            db = Path(root)/'test.sqlite'
            visited=[]
            for index in range(4):
                conn=store.connect(db)
                fetcher,_=network(cfg, {'keywords=A&': (500,'error',{}), 'seeMoreJobPostings': (200,'',{})})
                _, checks=coverage.collect(cfg,conn,fetcher,'linkedin',search_cap=22,now=NOW+timedelta(days=index))
                visited.append(checks[0]['target'])
                conn.close()
            self.assertEqual(visited,['A','B','C','D'])
            conn=store.connect(db)
            self.addCleanup(conn.close)
            rows=coverage.report(cfg,conn,now=NOW+timedelta(days=4))['checks']
            a=next(r for r in rows if r['source']=='linkedin' and r['target']=='A')
            self.assertEqual(a['status'],'error')
            self.assertIsNone(a['last_success'])

    def test_reordering_config_keeps_progress(self):
        cfg=config()
        conn=memory_conn(self)
        fetcher,_=network(cfg, {'seeMoreJobPostings':(200,'',{})})
        coverage.collect(cfg,conn,fetcher,'linkedin',search_cap=22,now=NOW)
        cfg.profile['coverage']['targets'].reverse()
        _,checks=coverage.collect(cfg,conn,fetcher,'linkedin',search_cap=22,now=NOW+timedelta(hours=1))
        self.assertEqual(checks[0]['target'],'B')

    def test_sources_have_independent_progress(self):
        cfg=config();conn=memory_conn(self)
        fetcher,_=network(cfg, {'seeMoreJobPostings':(200,'',{}), 'jobsearch':(200,{'data':[]},{})})
        coverage.collect(cfg,conn,fetcher,'linkedin',search_cap=22,now=NOW)
        _,checks=coverage.collect(cfg,conn,fetcher,'jobsdb',search_cap=75,now=NOW)
        self.assertEqual(checks[0]['target'],'A')
        self.assertEqual(checks[0]['status'],'empty')

    def test_block_does_not_record_unattempted_targets(self):
        cfg=config(**{'coverage.linkedin_requests':4});conn=memory_conn(self)
        fetcher,transport=network(cfg, {'seeMoreJobPostings':(429,'blocked',{})})
        _,checks=coverage.collect(cfg,conn,fetcher,'linkedin',search_cap=22,now=NOW)
        self.assertEqual(len(transport.urls),1)
        self.assertEqual(len(checks),1)
        self.assertEqual(checks[0]['status'],'blocked')
        data=coverage.report(cfg,conn,now=NOW)
        self.assertEqual(data['status_counts']['never'],7)

    def test_disabled_and_dry_run_do_not_change_state(self):
        cfg=config();conn=memory_conn(self)
        fetcher,transport=network(cfg, {'seeMoreJobPostings':(200,'',{})})
        before=conn.total_changes
        coverage.collect(cfg,conn,fetcher,'linkedin',search_cap=22,dry_run=True,now=NOW)
        self.assertEqual(conn.total_changes,before)
        self.assertEqual(coverage.report(cfg,conn,now=NOW)['status_counts']['never'],8)
        cfg.profile['sources']['linkedin']=False
        coverage.collect(cfg,conn,fetcher,'linkedin',search_cap=22,now=NOW)
        self.assertEqual(len(transport.urls),1)
        self.assertEqual(coverage.report(cfg,conn,now=NOW)['status_counts']['disabled'],4)


class Evidence(unittest.TestCase):
    def test_shell_malformed_and_empty_are_distinct(self):
        for source,body,status in [('linkedin','<html><body>loading</body></html>','error'),
                                   ('jobsdb',{},'error'),('jobsdb',{'data':[]},'empty')]:
            cfg=config();conn=memory_conn(self)
            fetcher,_=network(cfg, {'https://':(200,body,{})})
            _,rows=coverage.collect(cfg,conn,fetcher,source,search_cap=22,now=NOW)
            self.assertEqual(rows[0]['status'],status)

    def test_exact_alias_matches_without_substring_false_positive(self):
        cfg=config(**{'coverage.targets':[{'name':'Sea','queries':['Sea'],'aliases':['Sea Limited']}]})
        conn=memory_conn(self)
        page=search_page(('1','Key Account'),('2','Management Trainee')).replace('>Acme<','>Seagate<',1).replace('>Acme<','>Sea Limited<',1)
        fetcher,_=network(cfg, {'seeMoreJobPostings':(200,page,{})})
        jobs,rows=coverage.collect(cfg,conn,fetcher,'linkedin',search_cap=22,now=NOW)
        self.assertEqual(len(jobs),2)
        self.assertEqual(rows[0]['matched'],1)

    def test_full_page_is_partial_and_all_checks_are_bounded(self):
        cfg=config();conn=memory_conn(self)
        page=search_page(*[(str(i),'Key Account') for i in range(10)])
        fetcher,_=network(cfg, {'seeMoreJobPostings':(200,page,{})})
        _,rows=coverage.collect(cfg,conn,fetcher,'linkedin',search_cap=22,now=NOW)
        self.assertEqual(rows[0]['status'],'partial')
        self.assertEqual(rows[0]['last_success'],NOW.isoformat())
        self.assertTrue(rows[0]['bounded'])

    def test_staleness_and_failure_preserve_last_success(self):
        cfg=config(**{'coverage.targets':[{'name':'A','queries':['A']}]});conn=memory_conn(self)
        f,_=network(cfg,{'seeMoreJobPostings':(200,'',{})})
        coverage.collect(cfg,conn,f,'linkedin',search_cap=22,now=NOW)
        f,_=network(cfg,{'seeMoreJobPostings':(500,'oops',{})})
        coverage.collect(cfg,conn,f,'linkedin',search_cap=22,now=NOW+timedelta(days=11))
        row=coverage.report(cfg,conn,now=NOW+timedelta(days=11))['checks'][0]
        self.assertEqual(row['last_success'],NOW.isoformat())
        self.assertTrue(row['stale']);self.assertEqual(row['status'],'error')

    def test_inspection_writes_nothing(self):
        cfg=config();conn=memory_conn(self)
        before=conn.total_changes
        coverage.report(cfg,conn,now=NOW)
        self.assertEqual(conn.total_changes,before)
        self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='coverage_checks'").fetchone())


class Integration(unittest.TestCase):
    def test_recovery_then_broad_search_keeps_budget_and_reaches_jd(self):
        cfg=config(**{'coverage.linkedin_requests':6,
                      'coverage.targets':[{'name':str(i),'queries':[str(i)]} for i in range(8)],
                      'search.linkedin_queries':[[str(i),'Bangkok'] for i in range(8)],
                      'search.linkedin_max_pages':2})
        conn=memory_conn(self)
        f,t=network(cfg, {'seeMoreJobPostings':(200,search_page(('111','Management Trainee')),{}),
                         'jobPosting/111':(200,fixture('linkedin_guest_jobposting.html'),{})})
        data=pipeline.discover(cfg,conn,f,['linkedin'],now=NOW)
        searches=[u for u in t.urls if 'seeMoreJobPostings' in u]
        self.assertEqual(len(searches),22)
        self.assertEqual(len(data['coverage_attempts']),6)
        self.assertEqual(data['jd']['fetched'],1)
        self.assertLessEqual(len(t.urls),45)
        self.assertTrue(store.unscored_jobs(conn,10)[0]['jd_text'])
        scored = score_mod.run(cfg, conn, FakeLLM([lambda prompt, ids: reply(*[entry(i) for i in ids])]))
        self.assertEqual(scored['scored'],1)

    def test_source_selection_and_single_company_do_not_sweep_other_sources(self):
        cfg=config();conn=memory_conn(self)
        f,t=network(cfg,{'jobsearch':(200,{'data':[]},{})})
        pipeline.discover(cfg,conn,f,['jobsdb'],fetch_descriptions=False,now=NOW)
        self.assertTrue(all('th.jobsdb.com' in u for u in t.urls))
        before=len(t.urls)
        pipeline.discover(cfg,conn,f,['companies'],only_company='A',fetch_descriptions=False,now=NOW)
        self.assertEqual(len(t.urls),before)

    def test_watch_dry_run_has_no_side_effects(self):
        cfg=config(**{'sources.companies':True,'companies':[{'name':'A','adapter':'watch','url':'https://example.com/jobs'}]})
        conn=memory_conn(self)
        f,t=network(cfg,{'example.com':(200,'<a href="/jobs/1">Trainee</a>',{})})
        before=conn.total_changes
        pipeline.discover(cfg,conn,f,['companies'],dry_run=True,now=NOW)
        self.assertEqual(conn.total_changes,before)
        self.assertIsNone(store.get_watch(conn,'A'))

    def test_company_report_distinguishes_watch_and_unsupported(self):
        cfg=config(**{'sources.companies':True,'companies':[{'name':'A','adapter':'watch'},{'name':'B','adapter':'none'}]})
        conn=memory_conn(self)
        coverage.record_direct(cfg,conn,[{'company':'A','adapter':'watch','status':'ok','postings':2,'kept':2},
                                        {'company':'B','adapter':'none','status':'skipped'}],now=NOW)
        rows=[r for r in coverage.report(cfg,conn,now=NOW)['checks'] if r['kind']=='direct']
        self.assertEqual([r['status'] for r in rows],['watch','unsupported'])

    def test_broken_watch_is_not_recorded_as_success(self):
        cfg=config(**{'sources.companies':True,'companies':[{'name':'A','adapter':'watch','url':'https://example.com/jobs'}]})
        conn=memory_conn(self)
        f,t=network(cfg,{'example.com':(500,'<a href="/jobs/1">irrelevant error link</a>',{})})
        data=pipeline.discover(cfg,conn,f,['companies'],fetch_descriptions=False,now=NOW)
        self.assertEqual(data['sources']['companies']['errors'],1)
        row=next(r for r in data['coverage']['checks'] if r['kind']=='direct')
        self.assertEqual(row['status'],'error')
        self.assertIsNone(row['last_success'])
        self.assertIsNone(store.get_watch(conn,'A'))

    def test_coverage_cli_missing_db_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as root:
            db=Path(root)/'missing.sqlite'
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                result=cli.main(['--db',str(db),'coverage','--json'])
            self.assertEqual(result,0)
            self.assertIn('target_count',json.loads(out.getvalue()))
            self.assertFalse(db.exists())

    def test_cli_inspection_keeps_existing_database_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            db=Path(root)/'existing.sqlite'
            conn=store.connect(db)
            store.add_job(conn, {'source':'manual','title':'Key Account','company':'Acme'})
            conn.close()
            before=db.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--db',str(db),'coverage','--json']),0)
            self.assertEqual(db.read_bytes(),before)

    def test_daily_keeps_discovery_failures_and_coverage_warnings(self):
        from test_daily import make_daily, fake_services
        services=fake_services()
        job,cfg,conn,tracker,tmp=make_daily(test=self,fetcher=services['fetcher'],llm=FakeLLM([]))
        self.addCleanup(tmp.cleanup)
        result={'sources':{'linkedin':{'found':0,'new':0,'errors':1,'host_stopped':1,'partial':0}},
                'coverage':{'warnings':['one check blocked']},'jd':{'fetched':0}}
        with patch.object(pipeline,'discover',return_value=result):
            summary=job.run()
        self.assertEqual(summary['sources']['linkedin']['errors'],1)
        self.assertEqual(summary['sources']['linkedin']['host_stopped'],1)
        self.assertEqual(summary['coverage_warnings'],['one check blocked'])
        self.assertTrue(any('linkedin' in error for error in summary['errors']))

    def test_legacy_test_profiles_do_not_enable_coverage(self):
        self.assertFalse(profile()['coverage']['enabled'])
        self.assertTrue(profile(**{'coverage.enabled':True})['coverage']['enabled'])



class UniverseCadence(unittest.TestCase):
    def test_default_budget_clips_disabled_and_zero_budget_cannot_progress(self):
        cfg = config(**{'coverage.linkedin_requests': 999, 'sources.jobsdb': False})
        cycle = coverage.cadence(cfg)
        self.assertEqual(cycle['linkedin']['effective_requests'], cycle['linkedin']['default_search_cap'])
        self.assertIsNone(cycle['jobsdb']['minimum_runs_per_cycle'])
        cfg.profile['coverage']['linkedin_requests'] = 0
        report = coverage.report(cfg, memory_conn(self), now=NOW)
        self.assertTrue(any('zero recovery budget' in w for w in report['warnings']))

    def test_expansion_exposes_slow_rotation_without_extending_stale_window(self):
        cfg = config(**{'coverage.targets': [{'name': str(n), 'queries': [str(n)]} for n in range(25)],
                        'coverage.linkedin_requests': 2, 'coverage.jobsdb_requests': 10,
                        'coverage.stale_after_days': 10})
        report = coverage.report(cfg, memory_conn(self), now=NOW)
        self.assertEqual(report['cadence']['linkedin']['minimum_runs_per_cycle'], 13)
        self.assertEqual(report['cadence']['jobsdb']['minimum_runs_per_cycle'], 3)
        self.assertTrue(any('one run/day' in w for w in report['warnings']))
        self.assertEqual(report['industry_target_counts'], {'unclassified': 25})
        self.assertIn('minimum cycle: 13 runs', coverage.format_report(report))
        self.assertEqual(len(report['board_only_targets']), 25)

    def test_multiple_direct_feeds_can_map_to_one_target(self):
        cfg = config(**{'coverage.targets': [{'name': 'Group', 'queries': ['Group'],
                         'direct_names': ['Group graduates']}, {'name': 'Board only'}],
                        'companies': [{'name': 'Group graduates', 'adapter': 'watch'}]})
        report = coverage.report(cfg, memory_conn(self), now=NOW)
        self.assertEqual(report['board_only_targets'], ['Board only'])

    def test_registry_matches_active_config_and_preserves_role_search_capacity(self):
        from jobhunt.config import load
        cfg = load()
        registry = json.loads((cfg.root / 'config/employer-universe.json').read_text())
        active = {r['name']: r for r in registry['employers'] if r['selection_state'] == 'active_crosssearch'}
        configured = {r['name']: r for r in coverage.targets(cfg)}
        self.assertEqual(set(active), set(configured))
        for name, target in configured.items():
            self.assertEqual(target['queries'], active[name]['queries'])
            self.assertEqual(target['industry'], active[name]['industry'])
            if active[name]['origin'] == 'index_led_research':
                self.assertTrue(active[name]['thailand_evidence_url'].startswith('https://'))
                self.assertTrue(active[name]['index_status'])
                self.assertEqual(active[name]['direct_mode'], 'not_integrated')
        cycle = coverage.cadence(cfg)
        broad_li = len(cfg.get('search.linkedin_queries')) * cfg.get('search.linkedin_max_pages')
        broad_jdb = len(cfg.get('search.keywords')) * cfg.get('search.max_pages')
        self.assertLessEqual(cycle['linkedin']['effective_requests'] + broad_li, cycle['linkedin']['default_search_cap'])
        self.assertLessEqual(cycle['jobsdb']['effective_requests'] + broad_jdb, cycle['jobsdb']['default_search_cap'])

if __name__=='__main__':unittest.main()
