import unittest
from unittest.mock import Mock, patch
import sys
import tempfile
from pathlib import Path
import json
import types
import viewdeps

class ViewDependencyTests(unittest.TestCase):
    def test_topological_diamond(self):
        self.assertEqual(viewdeps.topo([4,3,2,1],[(1,2),(1,3),(2,4),(3,4)]),[1,2,3,4])
    def test_cycle(self):
        with self.assertRaises(viewdeps.DependencyError): viewdeps.topo([1,2],[(1,2),(2,1)])
    def test_distinct_cross_schema_chain(self):
        conn=Mock(); conn.execute.return_value.fetchall.return_value=[(5,'a','v','v','owner',True,True,None),(6,'b','mv','m','owner',False,False,5),(7,'c','v2','v','owner',True,False,6)]
        nodes,edges=viewdeps.discover(conn,1,'column')
        self.assertEqual([x['oid'] for x in nodes],[5,6,7]); self.assertEqual(edges,[(5,6),(6,7)])
        self.assertEqual(nodes[1]['kind'],'m')
        with self.assertLogs('pgddl',level='WARNING') as logs: viewdeps.report(nodes,20)
        self.assertIn('!!! MATERIALIZED VIEW !!!',' '.join(logs.output))
    def test_interface_widening(self):
        old=[(1,'name',1043,24,100,'character varying(20)',None,False)]
        new=[(1,'name',1043,44,100,'character varying(40)',None,False)]
        self.assertTrue(viewdeps.interface_ok(old,new)); self.assertFalse(viewdeps.interface_ok(new,old))
        self.assertFalse(viewdeps.interface_ok(old,[(1,'name',25,-1,100,'text',None,False)]))
        self.assertFalse(viewdeps.interface_ok(old,[(1,'name',1043,44,101,'character varying(40)',None,False)]))
    def test_diagnostic_backup_unique_and_matview_only(self):
        with tempfile.TemporaryDirectory() as directory:
            args=types.SimpleNamespace(backup_dir=directory,dbname='erp',schema='public')
            step=types.SimpleNamespace(number=20,sql='ALTER TABLE t ALTER COLUMN c TYPE VARCHAR(40)')
            manifest=types.SimpleNamespace(digest='hash')
            nodes=[{'kind':'m','issues':['MATERIALIZED VIEW'],'definition':'SELECT 1','indexes':['CREATE INDEX ...'],'populated':True}]
            a=Path(viewdeps.write_backup(None,nodes,[],args,step,manifest)); b=Path(viewdeps.write_backup(None,nodes,[],args,step,manifest))
            self.assertNotEqual(a,b); self.assertRegex(a.name,r'^\d\d-\d\d-\d{4}_\d\d:\d\d_step-0020_')
            self.assertTrue((a/'LEIA-ME.txt').exists()); self.assertFalse((a/'restore-views.sql').exists())
            self.assertEqual(json.loads((a/'views.json').read_text())['views'][0]['kind'],'m')
            self.assertEqual((a/'views.json').stat().st_mode&0o777,0o600)

    def test_rebuild_no_recursive_lock_and_backup_before_drop(self):
        conn=Mock()
        conn.execute.return_value.fetchone.side_effect=[('public',),(True,),(42,)]
        node=dict(oid=42,schema='bi',name='itens',owner='reader',kind='v')
        captured=dict(node,issues=[],columns=[],acl=[],options=[],comment=None,definition='SELECT 1')
        args=types.SimpleNamespace(lock_timeout=3,_hierarchy=False,_rediscover=Mock(return_value=([node],[])))
        step=types.SimpleNamespace(number=20,sql='ALTER TABLE t ALTER COLUMN c TYPE VARCHAR(40)')
        fake_sql=types.SimpleNamespace(SQL=lambda x:x)
        with patch.dict(sys.modules,{'psycopg':types.SimpleNamespace(sql=fake_sql)}), patch.object(viewdeps,'snapshot',return_value=captured), patch.object(viewdeps,'qname',return_value='bi.itens'), patch.object(viewdeps,'statements',return_value=[]), patch.object(viewdeps,'report'), patch.object(viewdeps,'write_backup',side_effect=lambda *a: conn.execute('BACKUP') and '/backup'):
            self.assertEqual(viewdeps.process(conn,[node],[],args,step,None,rebuild=True),'/backup')
        calls=[c.args[0] for c in conn.execute.call_args_list]
        self.assertFalse(any(c.startswith('LOCK TABLE') for c in calls))
        self.assertLess(calls.index('BACKUP'),calls.index('DROP VIEW bi.itens RESTRICT'))
        self.assertLess(calls.index('DROP VIEW bi.itens RESTRICT'),calls.index(step.sql))
        self.assertEqual(args._rediscover.call_count,2)

    def test_rebuild_detects_metadata_change_before_drop(self):
        conn=Mock(); conn.execute.return_value.fetchone.side_effect=[('public',),(True,)]
        node=dict(oid=42,schema='bi',name='itens',owner='reader',kind='v')
        captured=dict(node,issues=[],definition='SELECT 1')
        args=types.SimpleNamespace(lock_timeout=3,_hierarchy=False,_rediscover=Mock(return_value=([node],[])))
        step=types.SimpleNamespace(number=20,sql='ALTER TABLE t ALTER COLUMN c TYPE varchar(40)')
        with patch.dict(sys.modules,{'psycopg':types.SimpleNamespace(sql=Mock())}), patch.object(viewdeps,'snapshot',side_effect=[captured,dict(captured,definition='SELECT 2')]), patch.object(viewdeps,'report'), patch.object(viewdeps,'write_backup',return_value='/backup'):
            with self.assertRaisesRegex(viewdeps.DependencyError,'Metadados mudaram'):
                viewdeps.process(conn,[node],[],args,step,None,rebuild=True)
        self.assertFalse(any(str(c.args[0]).startswith('DROP VIEW') for c in conn.execute.call_args_list))

if __name__=='__main__': unittest.main()
